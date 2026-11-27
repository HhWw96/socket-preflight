#!/usr/bin/env python3
"""Run the downstream example against the local module in a fresh MoonBit workspace."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from build import ROOT, moon_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--moon")
    args = parser.parse_args()
    moon = moon_command(args.moon)
    env = os.environ.copy()
    toolchain = moon.parents[1]
    if (toolchain / "lib/core/moon.mod").is_file():
        env["MOON_HOME"] = str(toolchain)
    env.pop("MOON_WORK", None)
    with tempfile.TemporaryDirectory(prefix="socket-rollout-consumer-") as temporary:
        workspace = Path(temporary)
        project = workspace / "preflight"
        project.mkdir()
        for path in (*ROOT.glob("*.mbt"), ROOT / "moon.mod", ROOT / "moon.pkg", ROOT / "README.md"):
            if not path.name.endswith("_wbtest.mbt"):
                shutil.copyfile(path, project / path.name)
        consumer = workspace / "consumer"
        shutil.copytree(ROOT / "examples/moonbit_consumer", consumer)
        (workspace / "moon.work").write_text('members = ["preflight", "consumer"]\n', encoding="utf-8")
        output = subprocess.check_output(
            [moon, "run", "--target", "js", "--frozen", "--deny-warn", "."],
            cwd=consumer, env=env, text=True, encoding="utf-8", timeout=60,
        ).strip()
        assert output.splitlines() == [
            "transition: legacy.socket and next.socket overlap on TCP 45391",
            "candidate: selective recreation verified",
        ], output
        print(output)


if __name__ == "__main__":
    main()
