#!/usr/bin/env python3
"""Compare two SocketPreflight packages on identical snapshot workloads."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time

from extended_cases import ADMIN, RUNTIME, VENDOR, listener
from verify_cli import put


PROJECT = Path(__file__).resolve().parents[1]
PROFILE = """
const fs = require("node:fs");
const counts = Object.fromEntries(["lstatSync", "realpathNative", "opendirSync", "openSync"].map(name => [name, 0]));
function count(owner, method, label = method) {
  const original = owner[method];
  owner[method] = function (...args) {
    counts[label]++;
    return original.apply(this, args);
  };
}
count(fs, "lstatSync");
count(fs.realpathSync, "native", "realpathNative");
count(fs, "opendirSync");
count(fs, "openSync");
process.on("exit", () => process.stderr.write(JSON.stringify(counts)));
"""


def workloads(workspace):
    pair = ("admin.socket", "public.socket")
    scope = ("--ipv6", "enabled", "--json")
    units = tuple(f"edge-zone-{i:02}.socket" for i in range(64))
    base, candidate = workspace / "baseline", workspace / "candidate"
    sparse, dense = workspace / "same-port-clear", workspace / "same-port-conflicts"
    for root in (base, candidate):
        put(root, VENDOR + "socket.d/01-backlog.conf", "[Socket]\nBacklog=32\n")
        put(root, ADMIN + "socket.d/01-backlog.conf", "[Socket]\nBacklog=64\n")
        put(root, RUNTIME + "edge-.socket.d/11-delay.conf", "[Socket]\nNoDelay=yes\n")
        put(root, ADMIN + "edge-zone-.socket.d/21-keepalive.conf", "[Socket]\nKeepAlive=yes\n")
        for i, unit in enumerate(units):
            put(root, VENDOR + unit, listener("127.0.0.1", port=45300 + i))
    put(candidate, ADMIN + units[1] + ".d/90-site.conf", listener("127.0.0.1", reset=True, port=45300))
    for i, unit in enumerate(units):
        put(sparse, VENDOR + unit, "[Socket]\n" + "".join(
            f"ListenStream=127.0.{i}.{j + 1}:45391\n" for j in range(8)))
        put(dense, VENDOR + unit, "[Socket]\n" + "ListenStream=127.0.0.1:45391\n" * 8)
    samples = PROJECT / "examples/conflict"
    return {
        "shipped_pair": (["scan", "--root", str(samples), *scope, *pair], 1, 1),
        "inherited_drop_ins_64_units": (["scan", "--root", str(base), *scope, *units], 0, 0),
        "sparse_same_port_512_listeners": (["scan", "--root", str(sparse), *scope, *units], 0, 0),
        "dense_same_port_512_listeners": (["scan", "--root", str(dense), *scope, *units], 1, 129_024),
        "compare_two_64_unit_snapshots": (["compare", "--baseline", str(base), "--candidate", str(candidate),
                                          *scope, *units], 1, 1),
    }


def invoke(command, expected_code, expected_conflicts, *, profile=False):
    started = time.perf_counter_ns()
    proc = subprocess.run(command, capture_output=True, timeout=20)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if proc.returncode != expected_code or (proc.stderr and not profile):
        raise AssertionError((command, proc.returncode, proc.stderr))
    report = json.loads(proc.stdout)
    count = report["conflict_count"] if "conflict_count" in report else len(report["new_conflicts"])
    if count != expected_conflicts:
        raise AssertionError((command, expected_conflicts, count))
    report.pop("version")
    for side in ("baseline", "candidate"):
        if side in report:
            report[side].pop("version")
    return elapsed_ms, report, json.loads(proc.stderr) if profile else None


def summarize(samples):
    return {"median_ms": round(statistics.median(samples), 3),
            "min_ms": round(min(samples), 3), "max_ms": round(max(samples), 3),
            "samples_ms": [round(sample, 3) for sample in samples]}


def metadata(node, entry):
    return {"file": entry.name, "sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "version": subprocess.check_output([node, entry, "--version"], text=True).strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--runs", type=int, default=13)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 5:
        parser.error("--runs must be at least 5")
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js 24 is required.")
    entries = (args.baseline.resolve(strict=True), args.candidate.resolve(strict=True))
    rows = {}
    with tempfile.TemporaryDirectory(prefix="socket-preflight-benchmark-") as temporary:
        workspace = Path(temporary)
        hook = workspace / "profile.cjs"
        hook.write_text(PROFILE, encoding="utf-8")
        for name, (argv, code, conflicts) in workloads(workspace).items():
            commands = [[node, entry, *argv] for entry in entries]
            _, expected, _ = invoke(commands[0], code, conflicts)
            times = ([], [])
            for i in range(args.runs + 2):
                for position in (range(2) if i % 2 == 0 else reversed(range(2))):
                    elapsed, report, _ = invoke(commands[position], code, conflicts)
                    if report != expected:
                        raise AssertionError(f"CLI output changed for {name}")
                    if i >= 2:
                        times[position].append(elapsed)
            profiles = []
            for entry in entries:
                _, report, counts = invoke([node, "--require", hook, entry, *argv], code, conflicts, profile=True)
                assert report == expected
                profiles.append(counts)
            before, after = (summarize(samples) for samples in times)
            rows[name] = {"exit_code": code, "expected_conflicts": conflicts, "output_equivalent": True,
                          "baseline": {**before, "fs_calls": profiles[0]},
                          "candidate": {**after, "fs_calls": profiles[1]},
                          "median_change_percent": round(100 * (after["median_ms"] / before["median_ms"] - 1), 1)}
            print(f"{name}: {before['median_ms']:.1f} ms -> {after['median_ms']:.1f} ms", flush=True)
    proof = {"host": platform.platform(), "node": subprocess.check_output([node, "--version"], text=True).strip(),
             "baseline": metadata(node, entries[0]), "candidate": metadata(node, entries[1]),
             "runs_per_version_per_case": args.runs, "warmups_per_version_per_case": 2,
             "measurement": "Warm-cache CLI wall time including Node startup and JSON output capture. FS call counts from separate runs.",
             "workloads": rows}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
