#!/usr/bin/env python3
"""Build a deterministic deployment ZIP from a directory or a rollout plan."""

import argparse
from itertools import islice
import os
from pathlib import Path
import stat
import sys
import tempfile
import zipfile

from gate_bundle import (MAX_ENTRIES, MAX_EXPANDED, MAX_PLAN_BYTES, index_bundle,
                         plan_stage_roots, read_bounded, safe_parts, sha, validate_plan_roots)


def checked_stat(path):
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if reparse or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f"Expected a regular file or directory: {path}")
    return info


def snapshot_entries(directory, prefix=""):
    entries = list(islice(directory.iterdir(), MAX_ENTRIES + 1))
    if len(entries) > MAX_ENTRIES:
        raise ValueError("Snapshot directory exceeds the entry limit")
    for path in sorted(entries, key=lambda path: path.name):
        relative = prefix + path.name
        safe_parts(relative)
        info = checked_stat(path)
        yield path, relative, info
        if stat.S_ISDIR(info.st_mode):
            yield from snapshot_entries(path, relative + "/")


def read_snapshot_file(path, expected):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise ValueError(f"Snapshot entry changed: {path}")
        data = stream.read(expected.st_size + 1)
        after = os.fstat(stream.fileno())
        if (len(data) != expected.st_size or after.st_size != expected.st_size
                or after.st_mtime_ns != expected.st_mtime_ns):
            raise ValueError(f"Snapshot entry changed: {path}")
    return data


def write_member(archive, name, is_directory, data):
    entry = zipfile.ZipInfo(name + "/" if is_directory else name)
    entry.create_system = 3
    entry.external_attr = (0o40755 << 16) | 0x10 if is_directory else 0o100644 << 16
    archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)


def write_bundle(entries, output, plan_data=None):
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        total = 0
        count = 0
        with zipfile.ZipFile(temporary, "w") as archive:
            if plan_data is not None:
                write_member(archive, "plan.json", False, plan_data)
                total, count = len(plan_data), 1
            for path, name, info in entries:
                is_directory = stat.S_ISDIR(info.st_mode)
                total += 0 if is_directory else info.st_size
                count += 1
                if count > MAX_ENTRIES or total > MAX_EXPANDED:
                    raise ValueError("Snapshot exceeds the entry or expanded-size limit")
                member_data = b"" if is_directory else read_snapshot_file(path, info)
                write_member(archive, name, is_directory, member_data)
        data = read_bounded(temporary)
        with zipfile.ZipFile(temporary) as archive:
            paths, _ = index_bundle(archive)
            validate_plan_roots(archive.read("plan.json"), paths)
        # Publish atomically without overwriting an existing deployment artifact.
        os.link(temporary, output)
        return sha(data)
    finally:
        temporary.unlink(missing_ok=True)


def create_bundle(source, output):
    if not stat.S_ISDIR(checked_stat(source).st_mode):
        raise ValueError("Input must be a directory containing plan.json")
    source = source.resolve(strict=True)
    output = output.parent.resolve(strict=True) / output.name
    if output.is_relative_to(source):
        raise ValueError("Keep the output ZIP outside the input directory")
    return write_bundle(snapshot_entries(source), output)


def stage_directories(directory, roots):
    selected = []
    for relative in sorted({root for _, root in roots}):
        if relative == ".":
            raise ValueError("Use --input for a plan with a stage rooted at '.'")
        if any(relative.startswith(name + "/") for _, name, _ in selected):
            continue
        path = directory
        for part in relative.split("/"):
            path = path / part
            info = checked_stat(path)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"Stage root is not a directory: {relative!r}")
        if path.resolve(strict=True).relative_to(directory).as_posix() != relative:
            raise ValueError(f"Stage root spelling differs from the plan: {relative!r}")
        selected.append((path, relative, info))
    return selected


def stage_entries(directories):
    for path, relative, info in directories:
        yield path, relative, info
        yield from snapshot_entries(path, relative + "/")


def create_plan_bundle(plan, output):
    checked_stat(plan.parent)
    directory = plan.parent.resolve(strict=True)
    plan = directory / plan.name
    info = checked_stat(plan)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("--plan must point to a regular JSON file")
    if info.st_size > MAX_PLAN_BYTES:
        raise ValueError("plan.json exceeds 256 KiB")
    plan_data = read_snapshot_file(plan, info)
    roots, _ = plan_stage_roots(plan_data)
    directories = stage_directories(directory, roots)
    output = output.parent.resolve(strict=True) / output.name
    if any(output.is_relative_to(path) for path, _, _ in directories):
        raise ValueError("Keep the output ZIP outside the selected stage directories")
    return write_bundle(stage_entries(directories), output, plan_data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="archive a prepared directory containing plan.json")
    source.add_argument("--plan", type=Path, help="archive a plan file and its declared stage directories")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.plan is not None:
            digest = create_plan_bundle(args.plan, args.output)
        else:
            digest = create_bundle(args.input, args.output)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        print(f"Cannot package deployment: {error}", file=sys.stderr)
        return 3
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
