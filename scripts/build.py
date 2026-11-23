#!/usr/bin/env python3
"""Build, test and package the local release. Python 3.10+ and Node.js 24 required."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ("README.md", "CHANGELOG.md", "LICENSE", "LICENSE-APACHE-2.0", "NOTICE-MOONBIT-CORE", "THIRD_PARTY.md")


def run(args, env):
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def moon_command(override):
    command = override or shutil.which("moon")
    fallback = ROOT.parent / ".moonmmdb-tools/moon-current/bin/moon.exe"
    if not command and fallback.is_file():
        command = str(fallback)
    command = shutil.which(command) if command else None
    if not command:
        raise SystemExit("MoonBit is required. Put moon on PATH or use --moon with its path.")
    return Path(command).resolve()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    argparser = argparse.ArgumentParser(description=__doc__)
    argparser.add_argument("--moon", help="MoonBit executable path")
    args = argparser.parse_args()
    moon = moon_command(args.moon)
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js 24 is required to verify the release CLI.")
    env = os.environ.copy()
    tool_root = moon.parents[1]
    if (tool_root / "lib/core/moon.mod").is_file():
        env["MOON_HOME"] = str(tool_root)
    env.pop("MOON_WORK", None)
    flags = ["--target", "js", "--frozen"]
    run([moon, "check", *flags, "--deny-warn"], env)
    run([moon, "test", *flags, "--deny-warn"], env)
    run([moon, "build", *flags, "--release", "cmd/socket_preflight"], env)

    manifest = (ROOT / "moon.mod").read_text(encoding="utf-8")
    match = re.search(r'^version = "(0\.0\.\d+)"$', manifest, re.MULTILINE)
    if not match:
        raise SystemExit("Expected a 0.0.X version in moon.mod.")
    version = match[1]
    js = ROOT / "_build/js/release/build/cmd/socket_preflight/socket_preflight.js"
    dist = ROOT / "dist"
    name = f"socket-preflight-v{version}"
    entry = dist / f"{name}.cjs"
    archive = dist / f"{name}.zip"
    dist.mkdir(exist_ok=True)
    temporary_entry = entry.with_name(name + ".building.cjs")
    shutil.copyfile(js, temporary_entry)
    version_output = subprocess.check_output([node, temporary_entry], text=True, encoding="utf-8")
    if "same-namespace group" not in version_output:
        raise SystemExit("Built CLI did not return usage help.")
    actual = subprocess.check_output([node, temporary_entry, "--version"], text=True, encoding="utf-8").strip()
    if actual != f"SocketPreflight v{version}":
        raise SystemExit("CLI and manifest versions differ.")
    temporary_entry.replace(entry)

    payload = [(entry.name, entry)]
    payload.extend((name, ROOT / name) for name in (*DOCUMENTS, "DEMO.md"))
    payload.append(("gate_bundle.py", ROOT / "scripts/gate_bundle.py"))
    payload.append(("package_deployment.py", ROOT / "scripts/package_deployment.py"))
    payload.append(("deployment_pipeline.py", ROOT / "scripts/deployment_pipeline.py"))
    for name in ("debian-bad.zip", "debian-fixed.zip"):
        payload.append((f"samples/{name}", ROOT / "verification/debian" / name))
    for path in (ROOT / "examples").rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"Example trees must use regular files, found a symlink: {path}")
        if path.is_file():
            payload.append((path.relative_to(ROOT).as_posix(), path))
    temporary_archive = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary_archive, "w") as bundle:
        for key, file in sorted(payload):
            item = zipfile.ZipInfo(key)
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            bundle.writestr(item, file.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    temporary_archive.replace(archive)
    sums = dist / "SHA256SUMS"
    retained = sorted(path for path in dist.iterdir()
                      if re.fullmatch(r"socket-preflight-v0\.0\.\d+\.(?:cjs|zip)", path.name))
    temporary_sums = sums.with_suffix(".tmp")
    temporary_sums.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in retained), encoding="ascii")
    temporary_sums.replace(sums)
    print(archive)


if __name__ == "__main__":
    main()
