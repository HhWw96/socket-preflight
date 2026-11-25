#!/usr/bin/env python3
"""Build the release from this checkout and verify its package and MoonBit consumer."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from build import ROOT, module_version


def run(script, *args):
    subprocess.run([sys.executable, ROOT / "scripts" / script, *args],
                   cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--moon", help="MoonBit executable path")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "_build/source-validation")
    args = parser.parse_args()
    reports = args.report_dir.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    compiler = ["--moon", args.moon] if args.moon else []
    run("build.py", *compiler)
    archive = ROOT / "dist" / f"socket-preflight-v{module_version()}.zip"
    release_report = reports / "release.json"
    run("verify_release.py", archive, "--report", release_report)
    run("verify_bundle_gate.py", archive, "--inputs", ROOT / "verification/debian",
        "--report", reports / "bundle-gate.json")
    run("verify_deployment_pipeline.py", archive, "--report", reports / "pipeline.json")
    run("verify_consumer.py", *compiler)
    if sys.platform == "linux":
        results = json.loads(release_report.read_text(encoding="utf-8"))["cli"]["results"]
        passed = {result["case"] for result in results if result["status"] != "skipped"}
        expected = {"links-are-not-followed", "shared-directory-link-is-never-followed"}
        if not expected <= passed:
            raise SystemExit("Linux validation must exercise both snapshot symlink checks.")


if __name__ == "__main__":
    main()
