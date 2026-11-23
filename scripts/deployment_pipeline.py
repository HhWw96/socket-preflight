#!/usr/bin/env python3
"""Run a local gate and release rehearsal, or verify the handoff in a CI job."""

import argparse
import json
from pathlib import Path
import sys

from gate_bundle import read_bounded, run_gate, save_receipt, sha


def release(bundle, cli, receipt, marker):
    detail = json.loads(read_bounded(receipt))
    rollout = detail.get("rollout") if isinstance(detail, dict) else None
    if (not isinstance(rollout, dict) or detail.get("schema_version") != 1
            or detail.get("exit_code") != 0 or rollout.get("exit_code") != 0
            or rollout.get("status") != "clear" or rollout.get("complete") is not True
            or detail.get("bundle_sha256") != sha(read_bounded(bundle))
            or detail.get("cli_sha256") != sha(read_bounded(cli))):
        raise ValueError("No passing receipt for these deployment ZIP and CLI bytes")
    if marker.resolve() in {bundle.resolve(), cli.resolve(), receipt.resolve()}:
        raise ValueError("Pass a separate output path for the release marker")
    save_receipt(marker, {"decision": "would_deploy",
                          "bundle_sha256": detail["bundle_sha256"], "cli_sha256": detail["cli_sha256"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("run", help="Run the local two-step deployment rehearsal")
    local.add_argument("--out", type=Path, required=True, help="A new directory for the rehearsal")
    downstream = commands.add_parser("release", help="Recheck the ZIP and CLI against a saved gate receipt")
    downstream.add_argument("--receipt", type=Path, required=True)
    downstream.add_argument("--marker", type=Path, required=True)
    for command in (local, downstream):
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--cli", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "release":
            release(args.bundle, args.cli, args.receipt, args.marker)
            return 0
        args.out.mkdir(parents=True, exist_ok=False)
        report = args.out / "receipt.json"
        detail = run_gate(args.bundle, args.cli)
        save_receipt(report, detail)
        result = detail["exit_code"]
        if result:
            print(f"Deployment rehearsal blocked with exit {result}. Receipt saved at {report}")
            return result
        release(args.bundle, args.cli, report, args.out / "would-deploy.json")
        print(f"Deployment rehearsal allowed. Receipt saved at {report}")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
