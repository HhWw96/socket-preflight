#!/usr/bin/env python3
"""Run the bundled snapshots with a real manager in a disposable Linux VM."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import time
import uuid


VENDOR = "usr/lib/systemd/system/"
ADMIN = "etc/systemd/system/"
RUNTIME = "run/systemd/system/"
UNIT_NAMES = ("admin", "public")
REPETITIONS = 3


def run(*args, check=True):
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
                          env={**os.environ, "LC_ALL": "C"})
    if check and proc.returncode:
        raise RuntimeError(f"{args}: exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def read_samples(root):
    samples = {}
    for variant in ("conflict", "fixed"):
        folder = root / variant
        samples[variant] = {str(f.relative_to(folder).as_posix()): f.read_text(encoding="utf-8")
                            for f in folder.rglob("*") if f.is_file()}
    return samples


def example_variants(samples):
    base, fixed = samples["conflict"], samples["fixed"]
    ipv6_only = {**base, VENDOR + "public.socket": base[VENDOR + "public.socket"].replace("=both", "=ipv6-only")}
    wildcard = {**base, ADMIN + "admin.socket.d/20-site.conf": base[ADMIN + "admin.socket.d/20-site.conf"].replace("127.0.0.1", "0.0.0.0")}
    mixed = dict(fixed)
    extra = ADMIN + "admin.socket.d/30-this-long-name-must-apply-before-the-reset.conf"
    mixed[extra] = base[ADMIN + "admin.socket.d/20-site.conf"]
    return (
        ("dual-stack-v4-loopback", base, False, [45391, 45392]),
        ("dual-stack-v4-wildcard", wildcard, False, [45391, 45392]),
        ("ipv6-only-coexists", ipv6_only, True, [45391, 45392]),
        ("reset-coexists", fixed, True, [45393]),
        ("unequal-name-lengths", mixed, True, [45393]),
    )


def validate_sample(files):
    name_pattern = r"(?:etc|run|usr/lib)/systemd/system/(?:admin|public)\.socket(?:\.d/[0-9][a-z0-9-]+\.conf)?"
    allowed = re.compile(r"\[Unit\]|\[Socket\]|Description=[A-Za-z0-9 ]+|BindIPv6Only=(?:both|ipv6-only)|ListenStream=(?:(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]):4539[1-4])?")
    for path, content in files.items():
        if not re.fullmatch(name_pattern, path) or not all(not line or allowed.fullmatch(line) for line in content.splitlines()):
            raise ValueError(f"Outside the test fixture format: {path}")
    for name in UNIT_NAMES:
        if VENDOR + name + ".socket" not in files:
            raise ValueError("The bundled pair of fixtures is required.")


class SandboxUnits:
    def __init__(self):
        self.prefix = "mbpf" + uuid.uuid4().hex[:10] + "-"
        self.files = []
        self.dirs = []
        self.units = []

    def name(self, name):
        return self.prefix + name

    def write(self, relative, content):
        path = PurePosixPath(relative)
        base = next((base for base in (ADMIN, RUNTIME, VENDOR) if str(path).startswith(base)), None)
        if base is None:
            raise RuntimeError("Unexpected oracle search path")
        basename = path.relative_to(base)
        if len(basename.parts) not in (1, 2) or ".." in basename.parts:
            raise RuntimeError("Unexpected oracle path")
        parent = Path("/" + base).resolve(strict=True)
        type_wide = len(basename.parts) == 2 and basename.parts[0] == "socket.d"
        target = parent / (str(basename) if type_wide else self.name(str(basename)))
        if len(basename.parts) == 2 and not target.parent.exists():
            target.parent.mkdir()
            self.dirs.append(target.parent)
        with target.open("x", encoding="utf-8") as f:
            self.files.append(target)
            f.write(content)
        if len(basename.parts) == 1:
            self.units.append(target.name)
        return target

    def start_private_network(self):
        anchor = self.name("anchor.service")
        self.write(RUNTIME + "anchor.service", "[Unit]\nDefaultDependencies=no\n[Service]\nType=exec\nPrivateNetwork=yes\nExecStart=/usr/bin/sleep infinity\n")
        run("systemctl", "daemon-reload")
        run("systemctl", "start", anchor)
        pid = int(run("systemctl", "show", "--value", "-p", "MainPID", anchor).stdout.strip())
        namespace = f"/proc/{pid}/ns/net"
        if pid <= 1 or Path(namespace).stat().st_ino == Path("/proc/1/ns/net").stat().st_ino:
            raise RuntimeError("The network namespace is not isolated")
        return anchor, namespace

    def cleanup(self):
        if self.units:
            run("systemctl", "stop", *self.units, check=False)
            run("systemctl", "reset-failed", *self.units, check=False)
        for file in reversed(self.files):
            file.unlink(missing_ok=True)
        for folder in reversed(self.dirs):
            folder.rmdir()
        run("systemctl", "daemon-reload")


def properties(unit):
    output = run("systemctl", "show", unit,
                 "-p", "FragmentPath,DropInPaths,Listen,BindIPv6Only,NetworkNamespacePath,ActiveState,SubState,Result").stdout
    props = {}
    listeners = []
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "Listen":
            listeners.append(value)
        else:
            props[key] = value
    props["Listen"] = listeners
    return props


def ports_in_property(values):
    return sorted(int(port) for value in values for port in re.findall(r":(4539[1-4]) \(Stream\)", value))


def listeners_in(namespace, ports=None):
    output = run("nsenter", "--net=" + namespace, "ss", "-H", "-ltn").stdout
    if ports is not None:
        return [row for row in output.splitlines() if row.split()[3].rsplit(":", 1)[-1] in ports]
    return [row for row in output.splitlines() if re.search(r":4539[1-4]\b", row.split()[3])]


def try_order(namespace, order, coexist, expected_counts, conflict_probe=None, ports=None):
    run("systemctl", "start", order[0])
    first = properties(order[0])
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    second = run("systemctl", "start", order[1], check=False)
    second_props = properties(order[1])
    logs = ""
    errno_name = None
    if not coexist:
        for _ in range(10):
            logs = run("journalctl", "--no-pager", "--output=cat", "--since", since, "-u", order[1]).stdout
            if "Address already in use" in logs or "EADDRINUSE" in logs:
                break
            time.sleep(0.1)
        if conflict_probe:
            errno_name = conflict_probe(order[1])
    listeners = listeners_in(namespace, ports)
    expected_listeners = expected_counts[order[0]] + (expected_counts[order[1]] if coexist else 0)
    good = first.get("SubState") == "listening" and len(listeners) == expected_listeners
    if coexist:
        good = good and second.returncode == 0 and second_props.get("SubState") == "listening"
    else:
        good = good and second.returncode != 0 and (
            "Address already in use" in logs or "EADDRINUSE" in logs or errno_name == "EADDRINUSE"
        )
        if conflict_probe:
            good = good and errno_name == "EADDRINUSE"
    result = {"order": order, "expected_coexist": coexist, "second_exit": second.returncode,
              "second_state": second_props, "listeners": listeners, "journal": logs,
              "start_stderr": second.stderr, "pass": good}
    if errno_name is not None:
        result["kernel_bind_errno"] = errno_name
    run("systemctl", "stop", *order)
    run("systemctl", "reset-failed", *order, check=False)
    if listeners_in(namespace, ports):
        raise RuntimeError("Test listeners did not close between runs")
    return result


def run_variant(label, files, coexist, expected_admin):
    validate_sample(files)
    sandbox = SandboxUnits()
    names = [sandbox.name(name + ".socket") for name in UNIT_NAMES]
    try:
        anchor, namespace = sandbox.start_private_network()

        copied = {path: sandbox.write(path, text) for path, text in sorted(files.items())}
        for name in UNIT_NAMES:
            sandbox.write(RUNTIME + name + ".service", "[Unit]\nDefaultDependencies=no\n[Service]\nExecStart=/usr/bin/true\n")
            sandbox.write(RUNTIME + name + ".socket.d/00-oracle.conf",
                          f"[Unit]\nDefaultDependencies=no\nRequires={anchor}\nAfter={anchor}\n[Socket]\nNetworkNamespacePath={namespace}\n")
        run("systemctl", "daemon-reload")
        props = [properties(name) for name in names]
        if any(item.get("NetworkNamespacePath") != namespace for item in props):
            raise RuntimeError("Manager did not load the isolated namespace")
        fragments = [str(copied[VENDOR + name + ".socket"]) for name in UNIT_NAMES]
        verify = run("systemd-analyze", "verify", "--man=no", *names, check=False)

        admin_drop_ins = {Path(path).resolve() for path in props[0].get("DropInPaths", "").split()}
        winner = copied[ADMIN + "admin.socket.d/20-site.conf"]
        shadow = copied[VENDOR + "admin.socket.d/20-site.conf"]
        expected_counts = {names[0]: len(expected_admin), names[1]: 1}
        correct_merge = (all(Path(item.get("FragmentPath", "")).resolve() == Path(fragment).resolve()
                             for item, fragment in zip(props, fragments))
                         and winner in admin_drop_ins and shadow not in admin_drop_ins
                         and ports_in_property(props[0]["Listen"]) == expected_admin
                         and ports_in_property(props[1]["Listen"]) == [45391]
                         and props[1].get("BindIPv6Only") == ("ipv6-only" if label == "ipv6-only-coexists" else "both"))
        if label in ("reset-coexists", "unequal-name-lengths"):
            reset = next(path for rel, path in copied.items() if rel.endswith("-reset.conf"))
            drop_in_order = [Path(path).resolve() for path in props[0].get("DropInPaths", "").split()]
            correct_merge = correct_merge and reset in drop_in_order and drop_in_order.index(reset) > drop_in_order.index(winner)
        attempts = []
        for repeat in range(REPETITIONS):
            for order in (names, names[::-1]):
                attempts.append({"repeat": repeat + 1, **try_order(namespace, order, coexist, expected_counts)})
        return {"scenario": label, "merge_pass": correct_merge, "manager": props,
                "verify_exit": verify.returncode, "verify_stderr": verify.stderr,
                "namespace_inode": Path(namespace).stat().st_ino, "attempts": attempts}
    finally:
        sandbox.cleanup()


def require_disposable_vm():
    if platform.system() != "Linux" or os.geteuid() != 0 or os.environ.get("CI") != "true":
        raise SystemExit("Use root in a disposable Linux VM with CI=true and --disposable-vm.")
    if Path("/proc/1/comm").read_text().strip() != "systemd" or any(not shutil.which(c) for c in ("systemctl", "systemd-analyze", "journalctl", "nsenter", "ss")):
        raise SystemExit("systemd PID 1, journalctl, nsenter and ss are required.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-vm", action="store_true", required=True)
    parser.add_argument("--fixture-json", type=Path, help="JSON copy of the two bundled example trees")
    args = parser.parse_args()
    require_disposable_vm()
    if args.fixture_json:
        samples = json.loads(args.fixture_json.read_text(encoding="utf-8"))
    else:
        samples = read_samples(Path(__file__).resolve().parents[1] / "examples")
    for sample in samples.values():
        validate_sample(sample)
    meta = {"kernel": platform.release(), "os": platform.freedesktop_os_release(),
            "systemd": run("systemd", "--version").stdout.splitlines()[0],
            "fixture_sha256": hashlib.sha256(json.dumps(samples, sort_keys=True).encode()).hexdigest()}
    print(json.dumps({"environment": meta}), flush=True)
    passed = True
    for label, data, coexist, expected_admin in example_variants(samples):
        result = run_variant(label, data, coexist, expected_admin)
        print(json.dumps(result), flush=True)
        passed = passed and result["merge_pass"] and result["verify_exit"] == 0 and all(a["pass"] for a in result["attempts"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
