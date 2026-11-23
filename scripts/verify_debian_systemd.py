#!/usr/bin/env python3
"""Run the locked Debian rollout as isolated systemd sockets and compare it to the gate."""

import argparse
import io
import json
from pathlib import Path
import platform
import zipfile

from build_debian_example import OVERRIDE, PACKAGES, sha, override
from verify_extended_systemd import probe_bind
from verify_rollout_systemd import single_start
from verify_systemd import (REPETITIONS, RUNTIME, VENDOR, SandboxUnits,
                            properties, require_disposable_vm, run, try_order)

PORTS = {"9090", "19091"}
LOCKED_UNITS = {item["unit"]: item["unit_sha256"] for item in PACKAGES.values()}
STUB = "[Unit]\nDefaultDependencies=no\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n"
ISSUE_SERVICE = "cockpit-issue.service"
STAGES = (
    ("bad", "baseline", "before", {"cockpit.socket": "[::]:9090"}),
    ("bad", "transition", "overlap", {"cockpit.socket": "[::]:9090", "ssh.socket": "127.0.0.1:9090"}),
    ("bad", "candidate", "after", {"ssh.socket": "127.0.0.1:9090"}),
    ("fixed", "transition", "overlap", {"cockpit.socket": "[::]:9090", "ssh.socket": "127.0.0.1:19091"}),
)


def add_issue_stub(sandbox):
    old = run("systemctl", "show", ISSUE_SERVICE, "--property=LoadState", "--value", check=False)
    if old.stdout.strip() != "not-found":
        raise ValueError("Cockpit issue service already exists in this environment")
    path = Path("/") / RUNTIME / ISSUE_SERVICE
    with path.open("x", encoding="utf-8") as file:
        sandbox.files.append(path)
        file.write(STUB)
    sandbox.units.append(ISSUE_SERVICE)


def verify_stage(bundle, stage_name, folder, endpoints, coexist):
    data = bundle.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        # Only the original hash-locked units and the exact demo override are loaded.
        plan = json.loads(source.read("plan.json"))
        if (plan[stage_name] != {"root": folder, "units": list(endpoints)}
                or plan["environment"] != {"ipv6": "enabled", "bindv6only": "0"}):
            raise ValueError("Stage definition differs from the locked experiment")
        files = {}
        for unit in endpoints:
            path = VENDOR + unit
            original = source.read(f"{folder}/{path}")
            if sha(original) != LOCKED_UNITS[unit]:
                raise ValueError(f"Original Debian unit was altered: {unit}")
            files[path] = original
        if "ssh.socket" in endpoints:
            content = source.read(f"{folder}/{OVERRIDE}")
            if content != override(int(endpoints["ssh.socket"].rsplit(":", 1)[1])):
                raise ValueError("Unexpected SSH experiment override")
            files[OVERRIDE] = content

    sandbox = SandboxUnits()
    try:
        anchor, namespace = sandbox.start_private_network()
        default = run("nsenter", "--net=" + namespace, "cat", "/proc/sys/net/ipv6/bindv6only").stdout.strip()
        if default != "0":
            raise ValueError(f"Experiment requires net.ipv6.bindv6only=0, got {default!r}")
        copied = {path: sandbox.write(path, content.decode("utf-8")) for path, content in files.items()}
        if "cockpit.socket" in endpoints:
            add_issue_stub(sandbox)
        control = {}
        for unit in endpoints:
            sandbox.write(RUNTIME + unit.removesuffix(".socket") + ".service", STUB)
            control[unit] = (f"[Unit]\nDefaultDependencies=no\nRequires={anchor}\nAfter={anchor}\n"
                             f"[Socket]\nNetworkNamespacePath={namespace}\n")
            if unit == "cockpit.socket":
                control[unit] += "ExecStartPost=\nExecStopPost=\n"
            sandbox.write(RUNTIME + unit + ".d/00-oracle.conf", control[unit])
        run("systemctl", "daemon-reload")
        names = {unit: sandbox.name(unit) for unit in endpoints}
        manager = {unit: properties(name) for unit, name in names.items()}
        if "cockpit.socket" in names and run("systemctl", "show", names["cockpit.socket"],
                                              "-p", "ExecStartPost,ExecStopPost", "--value").stdout.strip():
            raise ValueError("The original Cockpit hooks are still active")
        verify = run("systemd-analyze", "verify", "--man=no", *names.values(), check=False)
        for unit, props in manager.items():
            actual = [line.removesuffix(" (Stream)") for line in props["Listen"]]
            if (actual != [endpoints[unit]] or props["NetworkNamespacePath"] != namespace
                    or Path(props["FragmentPath"]).resolve() != copied[VENDOR + unit].resolve()):
                raise ValueError(f"Systemd did not load the expected {unit}: {props}")
        if OVERRIDE in copied and copied[OVERRIDE].resolve() not in [
                Path(path).resolve() for path in manager["ssh.socket"]["DropInPaths"].split()]:
            raise ValueError("SSH override was not loaded")

        attempts = []
        ordered = list(names.values())
        by_name = {name: unit for unit, name in names.items()}
        for trial in range(1, REPETITIONS + 1):
            if len(ordered) == 1:
                attempts.append({"repeat": trial, **single_start(namespace, ordered[0], ports=PORTS)})
            else:
                for order in (ordered, ordered[::-1]):
                    attempts.append({"repeat": trial, **try_order(
                        namespace, order, coexist, dict.fromkeys(ordered, 1),
                        conflict_probe=lambda name: probe_bind(
                            namespace, endpoints[by_name[name]], manager[by_name[name]]["BindIPv6Only"]),
                        ports=PORTS)})
        return {"bundle": bundle.name, "bundle_sha256": sha(data), "stage": stage_name,
                "bindv6only": default, "file_sha256": {path: sha(content) for path, content in files.items()},
                "effective": endpoints, "manager": manager, "lab_overrides": control,
                "lab_stub": STUB, "verify_exit": verify.returncode, "verify_stderr": verify.stderr,
                "attempts": attempts, "pass": verify.returncode == 0 and all(item["pass"] for item in attempts)}
    finally:
        sandbox.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-vm", required=True, action="store_true")
    parser.add_argument("--inputs", required=True, type=Path)
    args = parser.parse_args()
    require_disposable_vm()
    version = run("systemctl", "--version").stdout.splitlines()[0]
    info = platform.freedesktop_os_release()
    if info.get("VERSION_ID") != "13" or info.get("ID") != "debian" or not version.startswith("systemd 257 "):
        raise SystemExit("Run this experiment in the disposable Debian 13 / systemd 257 lab")
    print(json.dumps({"os": info, "kernel": platform.release(), "systemd": version}), flush=True)
    passed = True
    for case, stage, folder, endpoints in STAGES:
        result = verify_stage(args.inputs / f"debian-{case}.zip", stage, folder, endpoints,
                              coexist=case == "fixed" or stage != "transition")
        print(json.dumps(result), flush=True)
        passed = passed and result["pass"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
