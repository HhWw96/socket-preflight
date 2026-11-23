#!/usr/bin/env python3
"""Exercise the distributed ZIP gate and its receipts on both host platforms."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile
import warnings
import zipfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(release, inputs):
    checks = {}
    with tempfile.TemporaryDirectory(prefix="socket gate test ") as work:
        root = Path(work)
        tool = root / "tool"
        tool.mkdir()
        with zipfile.ZipFile(release) as archive:
            assert not archive.testzip()
            entry, = [name for name in archive.namelist() if name.endswith(".cjs")]
            assert Path(entry).name == entry
            cli = tool / entry
            cli.write_bytes(archive.read(entry))
            gate = tool / "gate_bundle.py"
            gate.write_bytes(archive.read(gate.name))
        cli_hash = sha(cli)

        def run(name, bundle, expected):
            digest = sha(bundle)
            report = root / f"{name}.json"
            result = subprocess.run([sys.executable, gate, "--bundle", bundle,
                                     "--cli", cli, "--report", report], capture_output=True, timeout=40)
            assert result.returncode == expected, (name, result.stderr, result.returncode)
            detail = json.loads(report.read_text())
            assert detail["exit_code"] == expected
            assert detail["bundle_sha256"] == digest and detail["cli_sha256"] == cli_hash
            assert digest == sha(bundle) and cli_hash == sha(cli)
            checks[name] = detail
            return detail

        bad = inputs / "debian-bad.zip"
        fixed = inputs / "debian-fixed.zip"
        result = run("bad", bad, 1)
        finding, = result["rollout"]["transition"]["new_conflicts"]
        assert finding["code"] == "dual_stack_overlap"
        assert finding["first"]["setting"]["source"]["path"] == "usr/lib/systemd/system/cockpit.socket"
        assert finding["first"]["setting"]["source"]["line"] == 7
        assert finding["second"]["setting"]["source"]["line"] == 3
        assert not result["rollout"]["candidate"]["new_conflicts"]
        assert run("fixed", fixed, 0)["rollout"]["complete"]

        with zipfile.ZipFile(bad) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        plan = json.loads(entries["plan.json"])

        def variant(name, extra=(), updated_plan=None, expected=3):
            target = root / f"{name}.zip"
            with zipfile.ZipFile(target, "w") as archive:
                for key, value in entries.items():
                    archive.writestr(key, json.dumps(updated_plan) if key == "plan.json" and updated_plan is not None else value)
                for entry, content in extra:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        archive.writestr(entry, content)
            return run(name, target, expected)

        variant("unmodelled_environment", updated_plan={
            **plan, "environment": {"ipv6": "enabled", "bindv6only": "unknown"}}, expected=2)
        variant("escape_member", [("../escaped", b"test")])
        assert not (root / "escaped").exists()
        variant("case_fold_directory", [("Before/test.txt", b"test")])
        variant("duplicate", [("plan.json", entries["plan.json"])])
        variant("type_collision", [("before", b"test")])
        variant("device_name", [("notes/CON.txt", b"test")])
        variant("absolute_plan_root", updated_plan={**plan, "candidate": {**plan["candidate"], "root": "C:/outside"}})
        variant("parent_plan_root", updated_plan={**plan, "candidate": {**plan["candidate"], "root": "../outside"}})
        link = zipfile.ZipInfo("notices/linked")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        variant("link_member", [(link, b"../plan.json")])
        corrupt = root / "corrupt.zip"
        corrupt.write_bytes(b"invalid archive")
        run("corrupt", corrupt, 3)
        if int(entry.rsplit(".", 2)[-2]) >= 12:
            broken = root / "broken-member.zip"
            with zipfile.ZipFile(broken, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("plan.json", entries["plan.json"])
                offset = archive.getinfo("plan.json").header_offset
            content = bytearray(broken.read_bytes())
            size = len("plan.json")
            content[offset + 30 + size] = 0xFF
            broken.write_bytes(content)
            run("broken_member", broken, 3)
        missing = root / "missing" / "report.json"
        process = subprocess.run([sys.executable, gate, "--bundle", fixed, "--cli", cli,
                                  "--report", missing], capture_output=True, timeout=40)
        assert process.returncode == 3 and not missing.exists()
        checks["unwritable_receipt"] = {"exit_code": 3, "stderr": process.stderr.decode("utf-8", "replace").strip()}
    return {"host": platform.platform(), "release_sha256": sha(release), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.release, args.inputs)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{len(result['checks'])} bundle gate checks passed", flush=True)


if __name__ == "__main__":
    main()
