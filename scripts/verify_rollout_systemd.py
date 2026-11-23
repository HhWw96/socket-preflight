#!/usr/bin/env python3
"""Inspect and actually start the four reconstructed deployment snapshots."""

import argparse
import hashlib
import json
from pathlib import Path
import platform

from verify_extended_systemd import probe_bind
from verify_systemd import (REPETITIONS, RUNTIME, VENDOR, SandboxUnits, listeners_in,
                            properties, require_disposable_vm, run, try_order)


CASES = {
    "before": {"legacy.socket": "[::]:45391"},
    "overlap": {"legacy.socket": "[::]:45391", "next.socket": "127.0.0.1:45391"},
    "after": {"next.socket": "127.0.0.1:45391"},
    "fixed": {"legacy.socket": "[::]:45391", "next.socket": "127.0.0.1:45392"},
}
FIX = "etc/systemd/system/next.socket.d/20-transition.conf"


def fixture(root, stage):
    units = CASES[stage]
    paths = [VENDOR + unit for unit in units]
    if stage == "fixed":
        paths.append(FIX)
    data = {path: (root / stage / path).read_text(encoding="utf-8") for path in paths}
    allowed = {"[Socket]", "BindIPv6Only=both", "ListenStream=", "ListenStream=[::]:45391",
               "ListenStream=127.0.0.1:45391", "ListenStream=127.0.0.1:45392"}
    if any(set(content.splitlines()) - allowed for content in data.values()):
        raise ValueError(f"Unexpected directive in the packaged {stage} experiment")
    return data


def single_start(namespace, name):
    start = run("systemctl", "start", name, check=False)
    state = properties(name)
    listeners = listeners_in(namespace)
    active_list = run("systemctl", "list-sockets", "--no-legend", "--no-pager", name, check=False)
    run("systemctl", "stop", name)
    return {"unit": name, "start_exit": start.returncode, "start_stderr": start.stderr,
            "state": state, "listeners": listeners, "list_sockets_while_active": active_list.stdout,
            "pass": start.returncode == 0 and state["SubState"] == "listening"
                    and len(listeners) == 1 and not listeners_in(namespace)}


def verify_stage(root, stage):
    data = fixture(root, stage)
    units = CASES[stage]
    sandbox = SandboxUnits()
    try:
        anchor, namespace = sandbox.start_private_network()
        copied = {path: sandbox.write(path, content) for path, content in data.items()}
        for unit in units:
            sandbox.write(RUNTIME + unit.removesuffix(".socket") + ".service",
                          "[Unit]\nDefaultDependencies=no\n[Service]\nExecStart=/usr/bin/true\n")
            sandbox.write(RUNTIME + unit + ".d/00-oracle.conf",
                          f"[Unit]\nDefaultDependencies=no\nRequires={anchor}\nAfter={anchor}\n"
                          f"[Socket]\nNetworkNamespacePath={namespace}\n")
        names = [sandbox.name(unit) for unit in units]
        run("systemctl", "daemon-reload")
        manager = {unit: properties(name) for unit, name in zip(units, names, strict=True)}
        verify = run("systemd-analyze", "verify", "--man=no", *names, check=False)
        listed = run("systemctl", "list-sockets", "--all", "--no-legend", "--no-pager", *names, check=False)
        assert not listeners_in(namespace)
        effective = {unit: [line.removesuffix(" (Stream)") for line in props["Listen"]]
                     for unit, props in manager.items()}
        match = effective == {unit: [endpoint] for unit, endpoint in units.items()}
        for unit, props in manager.items():
            match = (match and props["NetworkNamespacePath"] == namespace
                     and Path(props["FragmentPath"]).resolve() == copied[VENDOR + unit].resolve())
        if stage == "fixed":
            match = match and copied[FIX].resolve() in [
                Path(p).resolve() for p in manager["next.socket"]["DropInPaths"].split()
            ]
        endpoints = dict(zip(names, units.values(), strict=True))
        modes = {sandbox.name(unit): props["BindIPv6Only"] for unit, props in manager.items()}
        attempts = []
        for i in range(REPETITIONS):
            if len(names) == 1:
                attempts.append({"repeat": i + 1, **single_start(namespace, names[0])})
            else:
                for order in (names, names[::-1]):
                    attempts.append({"repeat": i + 1, **try_order(
                        namespace, order, stage != "overlap", dict.fromkeys(names, 1),
                        conflict_probe=lambda name: probe_bind(namespace, endpoints[name], modes[name]))})
        return {"stage": stage, "fixture_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                "merge_pass": match, "effective": effective, "manager": manager,
                "verify": {"exit_code": verify.returncode, "stdout": verify.stdout, "stderr": verify.stderr},
                "list_sockets_before_start": {"exit_code": listed.returncode, "stdout": listed.stdout, "stderr": listed.stderr},
                "namespace_inode": Path(namespace).stat().st_ino, "attempts": attempts}
    finally:
        sandbox.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-vm", required=True, action="store_true")
    args = parser.parse_args()
    require_disposable_vm()
    root = Path(__file__).resolve().parents[1] / "examples/rollout"
    print(json.dumps({"environment": {"kernel": platform.release(), "os": platform.freedesktop_os_release(),
                                      "systemd": run("systemd", "--version").stdout.splitlines()[0]}}), flush=True)
    passed = True
    for stage in CASES:
        row = verify_stage(root, stage)
        print(json.dumps(row), flush=True)
        passed = (passed and row["merge_pass"] and row["verify"]["exit_code"] == 0
                  and row["list_sockets_before_start"]["exit_code"] == 0
                  and all(a["pass"] for a in row["attempts"]))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
