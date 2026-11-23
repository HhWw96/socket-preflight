#!/usr/bin/env python3
"""Check inherited drop-ins and IPv4-mapped bindings against a real systemd."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform

from extended_cases import cases
from verify_systemd import (REPETITIONS, RUNTIME, VENDOR, SandboxUnits, listeners_in,
                            properties, require_disposable_vm, run, try_order)

PROBE_BIND = """
import errno, socket, sys
endpoint, mode = sys.argv[1:]
host, port = endpoint.rsplit(":", 1)
host = host.strip("[]")
v6 = ":" in host
with socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_STREAM) as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if v6 and mode in ("both", "ipv6-only"):
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, int(mode == "ipv6-only"))
    try:
        s.bind((host, int(port)))
        s.listen()
    except OSError as error:
        print(errno.errorcode[error.errno])
    else:
        print("bound")
"""


def probe_bind(namespace, endpoint, mode):
    return run("nsenter", "--net=" + namespace, "python3", "-c", PROBE_BIND, endpoint, mode).stdout.strip()


def invalid_bind(namespace, name):
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    result = run("systemctl", "start", name, check=False)
    log = run("journalctl", "--no-pager", "--output=cat", "--since", since, "-u", name).stdout
    active = listeners_in(namespace)
    run("systemctl", "reset-failed", name, check=False)
    return {"unit": name, "start_exit": result.returncode, "journal": log, "listeners": active,
            "pass": result.returncode != 0 and "Invalid argument" in log and not active}


def verify_case(case):
    sandbox = SandboxUnits()
    names = [sandbox.name(unit) for unit in case.units]
    try:
        anchor, namespace = sandbox.start_private_network()
        copied = {rel: sandbox.write(rel, text) for rel, text in case.files.items()}
        controls = []
        for unit in case.units:
            sandbox.write(RUNTIME + unit.removesuffix(".socket") + ".service",
                          "[Unit]\nDefaultDependencies=no\n[Service]\nExecStart=/usr/bin/true\n")
            controls.append(sandbox.write(RUNTIME + unit + ".d/00-oracle.conf",
                                          f"[Unit]\nDefaultDependencies=no\nRequires={anchor}\nAfter={anchor}\n"
                                          f"[Socket]\nNetworkNamespacePath={namespace}\n").resolve())
        run("systemctl", "daemon-reload")
        expected_counts = dict(zip(names, (len(items) for items in case.endpoints), strict=True))
        manager = [properties(name) for name in names]
        path_names = {file.resolve(): rel for rel, file in copied.items()}
        selected = [[path_names.get(Path(p).resolve(), p) for p in item["DropInPaths"].split()
                     if Path(p).resolve() != control] for item, control in zip(manager, controls, strict=True)]
        endpoints = [[s.removesuffix(" (Stream)") for s in item["Listen"]] for item in manager]
        merge_pass = (
            all(item.get("NetworkNamespacePath") == namespace for item in manager)
            and all(Path(item["FragmentPath"]).resolve() == copied[VENDOR + unit].resolve()
                    for item, unit in zip(manager, case.units, strict=True))
            and selected == [list(paths) for paths in case.selected]
            and endpoints == [list(items) for items in case.endpoints]
        )
        checked = run("systemd-analyze", "verify", "--man=no", *names, check=False)
        by_name = dict(zip(names, manager, strict=True))

        def conflict_probe(name):
            props = by_name[name]
            endpoint, = props["Listen"]
            return probe_bind(namespace, endpoint.removesuffix(" (Stream)"), props["BindIPv6Only"])

        attempts = []
        for repeat in range(REPETITIONS):
            if case.outcome == "invalid_bind":
                attempts.append({"repeat": repeat + 1, **invalid_bind(namespace, names[0])})
            else:
                for order in (names, names[::-1]):
                    attempts.append({"repeat": repeat + 1,
                                     **try_order(namespace, order, case.outcome == "coexist", expected_counts,
                                                 conflict_probe=conflict_probe)})
        return {"scenario": case.name, "suite": case.suite, "outcome": case.outcome,
                "fixture_sha256": hashlib.sha256(json.dumps(asdict(case), sort_keys=True).encode()).hexdigest(),
                "manager": manager, "selected": selected, "endpoints": endpoints, "merge_pass": merge_pass,
                "verify_exit": checked.returncode, "verify_stderr": checked.stderr,
                "namespace_inode": Path(namespace).stat().st_ino, "attempts": attempts}
    finally:
        sandbox.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-vm", action="store_true", required=True)
    parser.add_argument("--suite", choices=("drop-ins", "mapped"))
    args = parser.parse_args()
    require_disposable_vm()
    print(json.dumps({"environment": {"kernel": platform.release(), "os": platform.freedesktop_os_release(),
                                      "systemd": run("systemd", "--version").stdout.splitlines()[0]}}), flush=True)
    passed = True
    for case in cases():
        if args.suite and args.suite != case.suite:
            continue
        result = verify_case(case)
        print(json.dumps(result), flush=True)
        passed = passed and result["merge_pass"] and result["verify_exit"] == 0 and all(a["pass"] for a in result["attempts"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
