#!/usr/bin/env python3
"""Render a concise GitHub Actions summary from an existing gate receipt."""

import argparse
from html import escape
import json
from pathlib import Path


OUTCOMES = {0: "Passed", 1: "Conflicts blocked", 2: "Coverage incomplete", 3: "Input error"}
DETAIL_LIMIT = 5


def text(value):
    return "".join(char if char.isprintable() else " " for char in str(value))[:360]


def location(source):
    path = text(source["path"])
    line = source.get("line")
    return f"{path}:{line}" if line is not None else path


def issue_text(issue):
    message = f'[{text(issue["code"])}] {text(issue["message"])}'
    source = issue.get("source")
    return f"{message} ({location(source)})" if source else message


def report_issues(report):
    return [
        *report.get("issues", []),
        *(issue for unit in report.get("units", []) for issue in unit.get("issues", [])),
    ]


def add_issues(lines, issues):
    for issue in issues[:DETAIL_LIMIT]:
        lines.append(f"  {issue_text(issue)}")
    if len(issues) > DETAIL_LIMIT:
        lines.append(f"  {len(issues) - DETAIL_LIMIT} more issues in receipt.json")


def add_stage(lines, name, comparison):
    lines.extend(["", f'{text(name)} — {text(comparison["status"])}'])
    findings = [
        (kind, finding)
        for kind in ("new", "existing")
        for finding in comparison.get(f"{kind}_conflicts", [])
    ]
    for kind, finding in findings[:DETAIL_LIMIT]:
        lines.append(f'  {kind.capitalize()} conflict on TCP {finding["port"]}')
        for side in ("first", "second"):
            listener = finding[side]
            lines.append(f'    {text(listener["unit"])} at {location(listener["setting"]["source"])}')
    if len(findings) > DETAIL_LIMIT:
        lines.append(f"  {len(findings) - DETAIL_LIMIT} more conflicts in receipt.json")
    if not comparison["complete"]:
        lines.append("  Coverage incomplete for this comparison")
    scan = comparison.get("candidate", {})
    if scan.get("conflicts_omitted"):
        lines.append(f'  {scan["conflicts_omitted"]} conflict pairs omitted by the CLI')
    add_issues(lines, [*comparison.get("issues", []), *report_issues(scan)])


def render(receipt):
    code = receipt["exit_code"]
    lines = [
        f'Deployment ZIP SHA-256  {text(receipt.get("bundle_sha256") or "unavailable")}',
        f'CLI SHA-256             {text(receipt.get("cli_sha256") or "unavailable")}',
    ]
    if rollout := receipt.get("rollout"):
        coverage = "complete" if rollout["complete"] else "incomplete"
        lines.extend([f'Tool version            {text(rollout["version"])}', f"Coverage                {coverage}"])
        if rollout.get("issues"):
            lines.extend(["", "Rollout issues"])
            add_issues(lines, rollout["issues"])
        if transition := rollout.get("transition"):
            add_stage(lines, "transition", transition)
        for checkpoint in rollout.get("checkpoints", []):
            add_stage(lines, checkpoint["name"], checkpoint["result"])
        if candidate := rollout.get("candidate"):
            add_stage(lines, "candidate", candidate)
            baseline_issues = report_issues(candidate.get("baseline", {}))
            if baseline_issues:
                lines.extend(["", "Baseline issues"])
                add_issues(lines, baseline_issues)
    elif reason := receipt.get("error"):
        lines.extend(["", text(reason)])
    details = escape("\n".join(lines))
    return (
        "## SocketPreflight deployment gate\n\n"
        f"**{OUTCOMES[code]}** · exit code `{code}`\n\n"
        f"<pre>\n{details}\n</pre>\n\n"
        "The receipt artifact contains the full `receipt.json`.\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    print(render(receipt), end="")


if __name__ == "__main__":
    main()
