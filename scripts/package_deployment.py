#!/usr/bin/env python3
"""Build a deterministic deployment ZIP from a prepared snapshot directory."""

import argparse
from itertools import islice
import os
from pathlib import Path
import stat
import sys
import tempfile
import zipfile

from gate_bundle import MAX_ENTRIES, MAX_EXPANDED, index_bundle, read_bounded, safe_parts, sha, validate_plan_roots


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


def create_bundle(source, output):
    if not stat.S_ISDIR(checked_stat(source).st_mode):
        raise ValueError("Input must be a directory containing plan.json")
    source = source.resolve(strict=True)
    output = output.parent.resolve(strict=True) / output.name
    if output.is_relative_to(source):
        raise ValueError("Keep the output ZIP outside the input directory")
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        total = 0
        with zipfile.ZipFile(temporary, "w") as archive:
            for count, (path, name, info) in enumerate(snapshot_entries(source), 1):
                is_directory = stat.S_ISDIR(info.st_mode)
                total += 0 if is_directory else info.st_size
                if count > MAX_ENTRIES or total > MAX_EXPANDED:
                    raise ValueError("Snapshot exceeds the entry or expanded-size limit")
                entry = zipfile.ZipInfo(name + "/" if is_directory else name)
                entry.create_system = 3
                entry.external_attr = (0o40755 << 16) | 0x10 if is_directory else 0o100644 << 16
                archive.writestr(entry, b"" if is_directory else read_snapshot_file(path, info),
                                 compress_type=zipfile.ZIP_DEFLATED)
        data = read_bounded(temporary)
        with zipfile.ZipFile(temporary) as archive:
            paths, _ = index_bundle(archive)
            validate_plan_roots(archive.read("plan.json"), paths)
        # Publish atomically without overwriting an existing deployment artifact.
        os.link(temporary, output)
        return sha(data)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        digest = create_bundle(args.input, args.output)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        print(f"Cannot package deployment: {error}", file=sys.stderr)
        return 3
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
