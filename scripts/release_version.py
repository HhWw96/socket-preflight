"""Stable release versions used by the build and compatibility checks."""

from pathlib import Path
import re


VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


def stable_version(version):
    if not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError(f"Invalid stable package version: {version!r}")
    return tuple(map(int, version.split(".")))


def package_version(path):
    stem = Path(path).stem
    if not stem.startswith("socket-preflight-v"):
        raise ValueError(f"Invalid release filename: {path}")
    return stable_version(stem.removeprefix("socket-preflight-v"))
