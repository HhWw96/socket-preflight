#!/usr/bin/env python3
"""Compare a checked release to a real systemd manager in a disposable lab."""

import argparse
from release_version import package_version
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import platform
import re
import tempfile
import time
from zipfile import ZipFile

from gate_bundle import extract_bundle, read_bounded, sha
from verify_debian_systemd import STAGES, STUB, verify_stage
from verify_extended_systemd import probe_bind
from verify_systemd import (ADMIN, REPETITIONS, RUNTIME, VENDOR, SandboxUnits,
                            listeners_in, properties, require_disposable_vm, run, try_order)


def checked_scan(cli, root, endpoints, bindv6only, expected, locations):
    proc = run("node", cli, "scan", "--root", root, "--ipv6", "enabled",
               "--bindv6only", bindv6only, "--json", *endpoints, check=False)
    report = json.loads(proc.stdout)
    assert proc.returncode == report["exit_code"] == expected, report
    actual = {item["name"]: sorted(binding["endpoint"] for binding in item["comparable_listeners"])
              for item in report["units"]}
    assert actual == {name: sorted(values) for name, values in endpoints.items()}, actual
    assert report["conflict_count"] == expected, report
    for conflict in report["conflicts"]:
        sources = [conflict[side]["setting"]["source"] for side in ("first", "second")]
        assert {(Path(source["path"]).name, source["line"]) for source in sources} == locations, sources
        for source in sources:
            assert (root / source["path"]).read_text().splitlines()[source["line"] - 1].startswith("ListenStream=")
    return report


def single_trial(namespace, name, expected, count, ports):
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    start = run("systemctl", "start", name, check=False)
    logs = ""
    if expected:
        for _ in range(10):
            logs = run("journalctl", "--no-pager", "--output=cat", "--since", since, "-u", name).stdout
            if "Address already in use" in logs:
                break
            time.sleep(0.1)
    listeners = listeners_in(namespace, ports)
    if expected:
        passed = start.returncode != 0 and "Address already in use" in logs and not listeners
    else:
        passed = start.returncode == 0 and len(listeners) == count
    run("systemctl", "stop", name)
    run("systemctl", "reset-failed", name, check=False)
    assert not listeners_in(namespace, ports)
    return {"exit": start.returncode, "stderr": start.stderr, "journal": logs, "listeners": listeners, "pass": passed}


def retained_listener_check(namespace, names, ports):
    # The old socket retains its IPv6-only setting after the default changes.
    ipv6, ipv4 = names["ipv6.socket"], names["ipv4.socket"]
    run("systemctl", "start", ipv6)
    run("nsenter", "--net=" + namespace, "sysctl", "-w", "net.ipv6.bindv6only=0")
    run("systemctl", "start", ipv4)
    retained = listeners_in(namespace, ports)
    assert len(retained) == 2
    run("systemctl", "stop", ipv4, ipv6)
    rebound = try_order(namespace, [ipv6, ipv4], False, {ipv6: 1, ipv4: 1},
        conflict_probe=lambda _: probe_bind(namespace, "127.0.0.1:45393", "both"), ports=ports)
    assert rebound["pass"], rebound
    return {"old_listeners_retained": retained, "both_recreated": rebound}


def run_controlled(cli, root, label, files, endpoints, bindv6only, expected, locations, retained=False):
    for path, value in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    scan = checked_scan(cli, root, endpoints, bindv6only, expected, locations)
    lab = SandboxUnits()
    try:
        anchor, ns = lab.start_private_network()
        run("nsenter", "--net=" + ns, "sysctl", "-w", "net.ipv6.bindv6only=" + bindv6only)
        assert run("nsenter", "--net=" + ns, "cat", "/proc/sys/net/ipv6/bindv6only").stdout.strip() == bindv6only
        copied = {path: lab.write(path, value) for path, value in files.items()}
        names = {name: lab.name(name) for name in endpoints}
        for name in endpoints:
            lab.write(RUNTIME + name.removesuffix(".socket") + ".service", STUB)
            lab.write(RUNTIME + name + ".d/00-isolation.conf",
                      f"[Unit]\nDefaultDependencies=no\nRequires={anchor}\nAfter={anchor}\n"
                      f"[Socket]\nNetworkNamespacePath={ns}\n")
        run("systemctl", "daemon-reload")
        verify = run("systemd-analyze", "verify", "--man=no", *names.values(), check=False)
        listed = run("systemctl", "list-sockets", "--no-legend", "--no-pager", *names.values())
        manager = {name: properties(unit) for name, unit in names.items()}
        for name, props in manager.items():
            assert props["NetworkNamespacePath"] == ns
            assert Path(props["FragmentPath"]).resolve() == copied[VENDOR + name].resolve()
            assert sorted(item.removesuffix(" (Stream)") for item in props["Listen"]) == sorted(endpoints[name]), props
            for path in files:
                if path.startswith(ADMIN + name + ".d/"):
                    assert copied[path].resolve() in [Path(v).resolve() for v in props["DropInPaths"].split()]
        ports = {value.rsplit(":", 1)[1] for values in endpoints.values() for value in values}
        attempts = []
        ordered = list(names.values())
        for _ in range(REPETITIONS):
            if len(ordered) == 1:
                attempts.append(single_trial(ns, ordered[0], expected, len(next(iter(endpoints.values()))), ports))
            else:
                reverse_names = {unit: name for name, unit in names.items()}
                for order in (ordered, ordered[::-1]):
                    attempts.append(try_order(ns, order, not expected, dict.fromkeys(ordered, 1),
                        conflict_probe=lambda unit: probe_bind(ns, endpoints[reverse_names[unit]][0],
                            "both" if bindv6only == "0" else "ipv6-only"),
                        ports=ports))
        supplemental = retained_listener_check(ns, names, ports) if retained else None
        return {"scenario": label, "scan": scan, "manager": manager, "verify_exit": verify.returncode,
                "verify_stderr": verify.stderr, "list_sockets_before": listed.stdout,
                "file_sha256": {path: sha(value.encode()) for path, value in files.items()},
                "attempts": attempts, "retained_listener_check": supplemental,
                "pass": verify.returncode == 0 and all(row["pass"] for row in attempts)}
    finally:
        lab.cleanup()


def verify_binding_reconfiguration(cli, work, selective):
    legacy, incoming = "legacy.socket", "incoming.socket"
    port = "45395"
    tcp4 = f"127.0.0.1:{port}"
    legacy_text = f"[Socket]\nListenStream=[::]:{port}\nBindIPv6Only=both\n"
    incoming_text = f"[Socket]\nListenStream={tcp4}\n"
    override = ADMIN + legacy + ".d/20-ipv6-only.conf"
    override_text = "[Socket]\nBindIPv6Only=ipv6-only\n"
    monitor = "monitor.socket"
    monitor_port = "45396"
    monitor_text = f"[Socket]\nListenStream=127.0.0.1:{monitor_port}\n"
    for tree, files in (
        ("old-bindings", {VENDOR + legacy: legacy_text}),
        ("new-bindings", {VENDOR + legacy: legacy_text, VENDOR + incoming: incoming_text,
                          override: override_text}),
    ):
        if selective:
            files[VENDOR + monitor] = monitor_text
        for path, text in files.items():
            target = work / tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    plan = {
        "schema_version": 2, "policy": "require-clear", "checkpoints": [],
        "environment": {"ipv6": "enabled", "bindv6only": "1"},
        "baseline": {"root": "old-bindings", "units": [legacy]},
        "candidate": {"root": "new-bindings", "units": [legacy, incoming]},
    }
    if selective:
        for stage in ("baseline", "candidate"):
            plan[stage]["units"].append(monitor)
    plan_file = work / "reconfigured-bindings.json"
    gate = {}
    for name, rebind, expected in (("reload_only", False, 2), ("recreated", True, 0)):
        if selective:
            plan["candidate"]["rebind_units"] = [legacy] if rebind else []
        else:
            plan["candidate"]["rebind_all"] = rebind
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        proc = run("node", cli, "rollout", "--plan", plan_file, "--json", check=False)
        result = json.loads(proc.stdout)
        assert proc.returncode == result["exit_code"] == expected, result
        assert result["complete"] == rebind, result
        if not rebind:
            issue, = result["issues"]
            assert issue["code"] == "unverified_rebind" and legacy in issue["message"]
        gate[name] = result

    lab = SandboxUnits()
    try:
        anchor, ns = lab.start_private_network()
        run("nsenter", "--net=" + ns, "sysctl", "-w", "net.ipv6.bindv6only=1")
        configs = {legacy: legacy_text, incoming: incoming_text}
        if selective:
            configs[monitor] = monitor_text
        for unit, config in configs.items():
            lab.write(VENDOR + unit, config)
            lab.write(RUNTIME + unit.removesuffix(".socket") + ".service", STUB)
            lab.write(RUNTIME + unit + ".d/00-isolation.conf",
                      f"[Unit]\nDefaultDependencies=no\nRequires={anchor}\nAfter={anchor}\n"
                      f"[Socket]\nNetworkNamespacePath={ns}\n")
        old, new = lab.name(legacy), lab.name(incoming)
        run("systemctl", "daemon-reload")
        run("systemctl", "start", old)
        if selective:
            run("systemctl", "start", lab.name(monitor))
            first = run("nsenter", "--net=" + ns, "ss", "-Hlnte", "sport", "=", ":" + monitor_port)
            assert re.search(r"\bino:[1-9][0-9]*\b", first.stdout), first
        assert properties(old)["SubState"] == "listening"
        assert probe_bind(ns, tcp4, "both") == "EADDRINUSE"

        lab.write(override, override_text)
        run("systemctl", "daemon-reload")
        old_report = properties(old)
        assert old_report["BindIPv6Only"] == "ipv6-only" and old_report["SubState"] == "listening", old_report
        errno = probe_bind(ns, tcp4, "both")
        start = run("systemctl", "start", new, check=False)
        retained = listeners_in(ns, {port})
        assert errno == "EADDRINUSE" and start.returncode != 0 and len(retained) == 1, (errno, start, retained)

        run("systemctl", "stop", new, old)
        run("systemctl", "reset-failed", new, old, check=False)
        assert not listeners_in(ns, {port})
        rebound = try_order(ns, [old, new], True, {old: 1, new: 1}, ports={port})
        assert rebound["pass"], rebound
        if selective:
            last = run("nsenter", "--net=" + ns, "ss", "-Hlnte", "sport", "=", ":" + monitor_port)
            inode = re.findall(r"\bino:[1-9][0-9]*\b", first.stdout)
            assert re.findall(r"\bino:[1-9][0-9]*\b", last.stdout) == inode, last
            rebound["unchanged_monitor_inode"] = inode[0]
        return {
            "reconfigured_binding_check": "bindipv6only-drop-in",
            "gate": gate,
            "reload_only": {"configured_mode": old_report["BindIPv6Only"], "listeners": retained,
                            "new_unit_start_exit": start.returncode, "kernel_bind_errno": errno},
            "recreated": rebound,
            "pass": True,
        }
    finally:
        lab.cleanup()


def verify_release(release, digest, out):
    data = read_bounded(release)
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or sha(data) != digest:
        raise ValueError("Release archive SHA-256 mismatch")
    systemd = run("systemctl", "--version").stdout.splitlines()[0]
    os_release = platform.freedesktop_os_release()
    expected_version = {("debian", "13"): "257", ("ubuntu", "24.04"): "255"}.get(
        (os_release["ID"], os_release["VERSION_ID"]))
    if not expected_version or not systemd.startswith(f"systemd {expected_version} "):
        raise ValueError("Use Debian 13/systemd 257 or Ubuntu 24.04/systemd 255")

    def emit(row):
        print(json.dumps(row), file=out, flush=True)

    emit({"release": release.name, "sha256": digest, "systemd": systemd,
          "os": os_release, "kernel": platform.release(), "node": run("node", "--version").stdout.strip()})
    with tempfile.TemporaryDirectory(prefix="spf-systemd-matrix-") as temp, ZipFile(BytesIO(data)) as package:
        work = Path(temp)
        binary = release.with_suffix(".cjs").name
        cli = work / binary
        cli.write_bytes(package.read(binary))
        release_version = package_version(release)
        staged_environments = release_version >= (0, 0, 24)
        rows = []
        for case, stage, folder, endpoints in STAGES:
            if stage != "transition":
                continue
            sample = work / f"debian-{case}.zip"
            sample.write_bytes(package.read(f"samples/{sample.name}"))
            snapshot = work / case
            snapshot.mkdir()
            extract_bundle(sample.read_bytes(), snapshot)
            expected = int(case == "bad")
            scan = checked_scan(cli, snapshot / folder, {name: [v] for name, v in endpoints.items()}, "0",
                                expected, {("cockpit.socket", 7), ("20-move-port.conf", 3)})
            row = verify_stage(sample, stage, folder, endpoints, coexist=not expected)
            rows.append({**row, "scenario": "debian-" + case, "scan": scan})
            emit(rows[-1])

        for fixed in (False, True):
            name = "repeat.socket"
            label = "same-unit-" + ("fixed" if fixed else "bad")
            address = f"127.0.0.1:{45392 if fixed else 45391}"
            override = "20-add.conf"
            files = {VENDOR + name: "[Socket]\nListenStream=0.0.0.0:45391\n",
                     ADMIN + name + ".d/" + override: f"[Socket]\nListenStream={address}\n"}
            rows.append(run_controlled(cli, work / label, label, files,
                {name: ["0.0.0.0:45391", address]}, "0", int(not fixed), {(name, 2), (override, 2)}))
            emit(rows[-1])

        for mode in ("0", "1"):
            label = f"target-ipv6-only-{mode}"
            endpoints = {"ipv4.socket": ["127.0.0.1:45393"], "ipv6.socket": ["[::]:45393"]}
            files = {VENDOR + name: f"[Socket]\nListenStream={values[0]}\n" for name, values in endpoints.items()}
            rows.append(run_controlled(cli, work / label, label, files, endpoints, mode,
                                      int(mode == "0"), {(name, 2) for name in endpoints},
                                      retained=staged_environments and mode == "1"))
            emit(rows[-1])
        if staged_environments:
            a = {"ipv6": "enabled", "bindv6only": "1"}
            b = {"ipv6": "enabled", "bindv6only": "0"}
            stage = {"root": "target-ipv6-only-1", "units": ["ipv4.socket", "ipv6.socket"]}
            plan_file = work / "plan.json"
            for label, before, after, rebind, expected in (
                ("unverified_rebind", a, b, False, 2), ("new_regression", a, b, True, 1),
                ("resolved_conflict", b, a, True, 0),
            ):
                plan = {"schema_version": 2, "environment": before, "baseline": stage, "checkpoints": [],
                        "candidate": {**stage, "environment": after, "rebind_all": rebind}}
                plan_file.write_text(json.dumps(plan), encoding="utf-8")
                proc = run("node", cli, "rollout", "--json", "--plan", plan_file, check=False)
                report = json.loads(proc.stdout)
                assert proc.returncode == report["exit_code"] == expected, report
                emit({"stage_environment_check": label, "report": report})
        if release_version >= (0, 0, 27):
            emit(verify_binding_reconfiguration(cli, work, selective=release_version >= (0, 0, 29)))
    count = sum(len(row["attempts"]) for row in rows)
    passed = count == 30 and all(row["pass"] for row in rows)
    emit({"scenarios": len(rows), "attempts": count, "pass": passed})
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-vm", required=True, action="store_true")
    parser.add_argument("--release-zip", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    require_disposable_vm()
    with args.report.open("x", encoding="utf-8") as out:
        return verify_release(args.release_zip, args.sha256, out)


if __name__ == "__main__":
    raise SystemExit(main())
