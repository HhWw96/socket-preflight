#!/usr/bin/env python3
"""Verify bad/fixed outcomes and the same-byte handoff from the release archive."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_systemd(report, runs):
    rows = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["systemd"].startswith("systemd 257 ") and len(rows) == 5
    stages = {}
    for row in rows[1:]:
        case = {"debian-bad.zip": "bad", "debian-fixed.zip": "fixed"}[row["bundle"]]
        receipt = runs[case]["gate"]
        assert row["bundle_sha256"] == receipt["bundle_sha256"] and row["pass"]
        result = receipt["rollout"]
        stage = row["stage"]
        snapshot = (result["candidate"]["baseline"] if stage == "baseline"
                    else result[stage]["candidate"])
        endpoints = {unit["name"]: [item["endpoint"] for item in unit["comparable_listeners"]]
                     for unit in snapshot["units"]}
        assert endpoints == {unit: [value] for unit, value in row["effective"].items()}
        trials = row["attempts"]
        assert len(trials) == (6 if stage == "transition" else 3) and all(trial["pass"] for trial in trials)
        if stage == "transition" and case == "bad":
            assert all(trial["second_exit"] != 0 and trial["kernel_bind_errno"] == "EADDRINUSE" for trial in trials)
        stages[f"{case}/{stage}"] = len(trials)
    assert stages == {"bad/baseline": 3, "bad/transition": 6, "bad/candidate": 3, "fixed/transition": 6}
    return {"report_sha256": sha(report), "systemd": rows[0]["systemd"], "stages": stages}


def verify(release):
    with tempfile.TemporaryDirectory(prefix="deployment-rehearsal-") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(release) as archive:
            assert not archive.testzip()
            entry, = (name for name in archive.namelist() if name.endswith(".cjs"))
            assert Path(entry).name == entry
            for name in (entry, "gate_bundle.py", "deployment_pipeline.py",
                         "samples/debian-bad.zip", "samples/debian-fixed.zip"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        cli = root / entry
        command = [sys.executable, root / "deployment_pipeline.py"]
        runs = {}
        for case, code in (("bad", 1), ("fixed", 0)):
            bundle = root / "samples" / f"debian-{case}.zip"
            out = root / case
            process = subprocess.run([*command, "run", "--bundle", bundle, "--cli", cli, "--out", out],
                                     capture_output=True, timeout=40)
            assert process.returncode == code and not process.stderr, process
            report = out / "receipt.json"
            detail = json.loads(report.read_text())
            assert detail["exit_code"] == code and detail["bundle_sha256"] == sha(bundle)
            assert detail["cli_sha256"] == sha(cli)
            marker = out / "would-deploy.json"
            assert marker.is_file() == (code == 0)
            if marker.is_file():
                assert json.loads(marker.read_text()) == {"decision": "would_deploy",
                                                          "bundle_sha256": sha(bundle), "cli_sha256": sha(cli)}
            runs[case] = {"gate": detail, "release_marker": json.loads(marker.read_text()) if marker.is_file() else None}
        changed = root / "changed.zip"
        changed.write_bytes(bundle.read_bytes() + b"changed after the gate")
        denied = root / "mismatched-marker.json"
        process = subprocess.run([*command, "release", "--bundle", changed, "--cli", cli,
                                  "--receipt", report, "--marker", denied], capture_output=True, timeout=40)
        assert process.returncode == 3 and not denied.exists(), process
        runs["changed_zip"] = {"exit_code": 3, "stderr": process.stderr.decode("utf-8", "replace").strip(),
                               "bundle_sha256": sha(changed)}
    return {"host": platform.platform(), "release_sha256": sha(release), "runs": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--systemd-report", type=Path)
    args = parser.parse_args()
    result = verify(args.release)
    if args.systemd_report:
        result["systemd"] = check_systemd(args.systemd_report, result["runs"])
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Pipeline rehearsal passed: bad blocked, fixed allowed, changed ZIP blocked")


if __name__ == "__main__":
    main()
