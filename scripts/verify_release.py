#!/usr/bin/env python3
"""Run both package suites against the same archive on Windows or Linux."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


SCRIPTS = Path(__file__).resolve().parent
EVIDENCE = SCRIPTS.parent / "verification"


def run_suite(script, archive, report, *flags):
    subprocess.run([sys.executable, SCRIPTS / script, archive, "--report", report, *flags], check=True)
    return json.loads(report.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="socket-release-") as temporary:
        root = Path(temporary)
        cli = run_suite("verify_cli.py", archive, root / "cli.json",
                        "--systemd-report", EVIDENCE / "v0.0.1-systemd-linux.jsonl",
                        "--extended-systemd-report", EVIDENCE / "v0.0.2-systemd-linux.jsonl",
                        "--extended-systemd-report", EVIDENCE / "v0.0.3-systemd-linux.jsonl")
        rollout = run_suite("verify_rollout.py", archive, root / "rollout.json",
                            "--systemd-report", EVIDENCE / "v0.0.8-rollout-systemd.jsonl")
        assert cli["archive_sha256"] == rollout["archive_sha256"]
        assert cli["version"] == rollout["version"]
    if args.report:
        args.report.write_text(json.dumps({"cli": cli, "rollout": rollout}, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")


if __name__ == "__main__":
    main()
