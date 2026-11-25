#!/usr/bin/env python3
"""Exercise the distributed ZIP gate and its receipts on both host platforms."""

import argparse
import hashlib
import json
import os
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
            pack = tool / "package_deployment.py"
            if pack.name in archive.namelist():
                pack.write_bytes(archive.read(pack.name))
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

        if pack.is_file():
            def package(source, output, expected):
                result = subprocess.run([sys.executable, pack, "--input", source,
                                         "--output", output], capture_output=True, timeout=30)
                assert result.returncode == expected, (result.stdout, result.stderr)
                return result

            source = root / "prepared"
            with zipfile.ZipFile(fixed) as archive:
                archive.extractall(source)
            (source / "empty").mkdir()
            first, second = root / "repacked.zip", root / "repacked-again.zip"
            assert package(source, first, 0).stdout.decode().strip() == sha(first)
            os.utime(source / "plan.json", (1_700_000_000, 1_700_000_000))
            package(source, second, 0)
            assert first.read_bytes() == second.read_bytes()
            with zipfile.ZipFile(first) as archive:
                assert "empty/" in archive.namelist()
            assert run("repacked-fixed", first, 0)["rollout"]["complete"]
            package(source, first, 3)
            assert first.read_bytes() == second.read_bytes()
            checks["package-repeatable-and-no-overwrite"] = {"pass": True}
            plan_path = source / "plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["candidate"]["root"] = "missing"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            missing_root = root / "missing-root.zip"
            package(source, missing_root, 3)
            assert not missing_root.exists()
            checks["package-validates-stage-roots"] = {"pass": True}

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

        if int(entry.rsplit(".", 2)[-2]) >= 17:
            multistage = {key: value for key, value in plan.items() if key != "transition"}
            multistage["schema_version"] = 2
            multistage["checkpoints"] = [{"name": "during", **plan["transition"]}]
            result = variant("named-checkpoint-deployment-bundle", updated_plan=multistage, expected=1)
            assert result["rollout"]["checkpoints"][0]["result"]["new_conflicts"][0]["port"] == 9090
            if int(entry.rsplit(".", 2)[-2]) >= 18:
                carried = {**multistage, "baseline": plan["transition"]}
                variant("default-policy-allows-existing-conflict", updated_plan=carried, expected=0)
                strict = variant("strict-policy-blocks-existing-conflict",
                                 updated_plan={**carried, "policy": "require-clear"}, expected=1)
                assert strict["rollout"]["status"] == "conflict"
                comparison = strict["rollout"]["checkpoints"][0]["result"]
                assert comparison["existing_conflicts"] and not comparison["new_conflicts"]
            multistage["checkpoints"][0]["root"] = "../outside"
            rejected = variant("checkpoint-root-stays-inside-bundle", updated_plan=multistage)
            assert rejected["rollout"] is None

        if int(entry.rsplit(".", 2)[-2]) >= 24:
            units = ["v4.socket", "v6.socket"]
            stage = {"root": "target", "units": units}
            scope = {"schema_version": 2, "checkpoints": [],
                     "environment": {"ipv6": "enabled", "bindv6only": "1"},
                     "baseline": stage, "policy": "require-clear",
                     "candidate": {**stage, "environment": {"ipv6": "enabled", "bindv6only": "0"}}}
            snapshots = [
                ("target/usr/lib/systemd/system/v4.socket", b"[Socket]\nListenStream=127.0.0.1:45397\n"),
                ("target/usr/lib/systemd/system/v6.socket", b"[Socket]\nListenStream=[::]:45397\n"),
            ]
            missing = variant("stage-rebind-unverified-blocks", snapshots, scope, expected=2)
            assert missing["rollout"]["issues"][0]["code"] == "unverified_rebind"
            scope["candidate"]["rebind_all"] = True
            conflict = variant("stage-rebind-confirmed-detects-conflict", snapshots, scope, expected=1)
            assert conflict["rollout"]["candidate"]["new_conflicts"][0]["port"] == 45397
            if int(entry.rsplit(".", 2)[-2]) >= 25:
                staged = {key: value for key, value in plan.items() if key != "transition"}
                staged.update(schema_version=2, checkpoints=[{"name": "overlap", **plan["transition"]}],
                              candidate={**plan["candidate"], "environment": {
                                  "ipv6": "enabled", "bindv6only": "1"}})
                mixed = variant("receipt-retains-conflict-with-unverified-candidate",
                                updated_plan=staged, expected=1)["rollout"]
                assert not mixed["complete"] and mixed.get("candidate") is None
                assert mixed["checkpoints"][0]["result"]["new_conflicts"][0]["port"] == 9090
                assert mixed["issues"][0]["code"] == "unverified_rebind"

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
