#!/usr/bin/env python3
"""Check an offline deployment ZIP and write an artifact-bound rollout receipt."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
import zlib


MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ENTRIES = 4096
PORTABLE = re.compile(r"[A-Za-z0-9_.+@ -]+")
DEVICE = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", re.IGNORECASE)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_bounded(path):
    with path.open("rb") as source:
        data = source.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError(f"File exceeds the 32 MiB input limit: {path.name}")
    return data


def safe_parts(name):
    parts = name.split("/")
    if len(name) > 1024 or len(parts) > 32:
        raise ValueError("ZIP path exceeds the length or depth limit")
    for part in parts:
        if (len(part) > 255 or part in (".", "..")
                or not PORTABLE.fullmatch(part) or part.endswith((".", " "))
                or DEVICE.fullmatch(part.split(".", 1)[0].rstrip(" "))):
            raise ValueError(f"Non-portable ZIP path: {name!r}")
    return parts


def index_bundle(archive):
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES or sum(member.file_size for member in entries) > MAX_EXPANDED:
        raise ValueError("ZIP exceeds the entry or expanded-size limit")

    # Include implied directories so case and prefix collisions are rejected
    # before the first write, on case-sensitive and case-insensitive hosts.
    paths = {}
    explicit = set()
    resolved = []
    for member in entries:
        name = member.orig_filename
        is_directory = member.is_dir()
        mode = stat.S_IFMT(member.external_attr >> 16)
        if (member.filename != name or member.flag_bits & 1
                or member.external_attr & 0x400
                or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or mode not in (0, stat.S_IFDIR if is_directory else stat.S_IFREG)
                or (is_directory and member.file_size)):
            raise ValueError(f"Unsupported ZIP entry: {name!r}")
        parts = safe_parts(name[:-1] if is_directory else name)
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            key = path.casefold()
            current = (path, index < len(parts) or is_directory)
            if paths.setdefault(key, current) != current:
                raise ValueError(f"Case or type collision in ZIP: {path!r}")
        if key in explicit:
            raise ValueError(f"Duplicate ZIP entry: {path!r}")
        explicit.add(key)
        resolved.append((member, parts))

    if paths.get("plan.json") != ("plan.json", False):
        raise ValueError("ZIP must contain plan.json at its root")
    return paths, resolved


def extract_bundle(data, directory):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        paths, entries = index_bundle(archive)
        for member, parts in entries:
            target = directory.joinpath(*parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    return paths


def validate_plan_roots(content, paths):
    if len(content) > 256 * 1024:
        raise ValueError("plan.json exceeds 256 KiB")
    plan = json.loads(content.decode("utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("plan.json must contain an object")
    schema = plan.get("schema_version")
    policy = "new-conflicts"
    stages = [(name, plan.get(name)) for name in ("baseline", "candidate")]
    if schema == 2:
        policy = plan.get("policy", policy)
        if policy not in ("new-conflicts", "require-clear"):
            raise ValueError("Policy must be new-conflicts or require-clear")
        checkpoints = plan.get("checkpoints")
        if not isinstance(checkpoints, list) or len(checkpoints) > 8 or "transition" in plan:
            raise ValueError("Schema 2 requires up to eight checkpoints")
        stages.extend((f"checkpoint {index}", stage) for index, stage in enumerate(checkpoints, 1))
    elif schema == 1:
        if "policy" in plan:
            raise ValueError("A rollout policy requires schema 2")
        if "transition" in plan:
            stages.append(("transition", plan["transition"]))
    else:
        raise ValueError("Unsupported rollout plan schema")
    for name, stage in stages:
        root = stage.get("root") if isinstance(stage, dict) else None
        if not isinstance(root, str):
            raise ValueError(f"{name} requires a ZIP-relative root")
        if root == ".":
            continue
        try:
            relative = "/".join(safe_parts(root))
        except ValueError as error:
            raise ValueError(f"{name} root must stay inside the ZIP") from error
        if paths.get(relative.casefold()) != (relative, True):
            raise ValueError(f"{name} root is not a directory in the ZIP: {relative!r}")
    return policy


def save_receipt(path, receipt):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(receipt, file, ensure_ascii=True, indent=2)
            file.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_gate(bundle, cli):
    receipt = {"schema_version": 1, "bundle_sha256": None, "cli_sha256": None,
               "exit_code": 3, "rollout": None}
    try:
        data = read_bounded(bundle)
        receipt["bundle_sha256"] = sha(data)
        executable = read_bounded(cli)
        receipt["cli_sha256"] = sha(executable)
        with tempfile.TemporaryDirectory(prefix="socket-gate-") as work:
            private = Path(work)
            staging = private / "staging"
            staging.mkdir()
            paths = extract_bundle(data, staging)
            plan = staging / "plan.json"
            policy = validate_plan_roots(plan.read_bytes(), paths)
            local_cli = private / "socket-preflight.cjs"
            local_cli.write_bytes(executable)
            proc = subprocess.run(["node", local_cli, "rollout", "--plan", plan, "--json"],
                                  cwd=private, capture_output=True, timeout=30)
            result = json.loads(proc.stdout)
            code = proc.returncode
            expected = {0: "clear", 1: "regression", 2: "inconclusive", 3: "input_error"}
            if policy == "require-clear":
                expected[1] = "conflict"
            if (not isinstance(result, dict) or code not in expected
                    or result.get("exit_code") != code or result.get("status") != expected[code]
                    or (code == 0 and result.get("complete") is not True)):
                raise ValueError("CLI exit and rollout JSON disagree")
            receipt["rollout"] = result
            receipt["exit_code"] = code
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error,
            EOFError, NotImplementedError, subprocess.SubprocessError) as error:
        receipt["error"] = str(error)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.report.resolve() in (args.bundle.resolve(), args.cli.resolve()):
            raise ValueError("Report must use a path distinct from the deployment ZIP and CLI")
        receipt = run_gate(args.bundle, args.cli)
        save_receipt(args.report, receipt)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Cannot write gate receipt: {error}", file=sys.stderr)
        return 3
    if "error" in receipt:
        print(receipt["error"], file=sys.stderr)
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
