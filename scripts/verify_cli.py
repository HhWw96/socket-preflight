#!/usr/bin/env python3
"""Exercise the extracted release, including path, provenance and exit-code rules."""

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile

from extended_cases import cases, listener
from verify_systemd import example_variants, read_samples


PAYLOAD = "[Socket]\nListenStream=127.0.0.1:45391\n"
VENDOR = "usr/lib/systemd/system/"
ADMIN = "etc/systemd/system/"
RUNTIME = "run/systemd/system/"


def put(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--systemd-report", type=Path, help="Compare against a completed verify_systemd.py JSONL receipt")
    parser.add_argument("--extended-systemd-report", type=Path, action="append", default=[],
                        help="Compare against a completed verify_extended_systemd.py JSONL receipt (repeatable)")
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js 24 is required.")
    results = []
    with tempfile.TemporaryDirectory(prefix="socket-preflight-复验 ") as temporary:
        workspace = Path(temporary)
        with zipfile.ZipFile(args.archive) as archive:
            files = archive.namelist()
            if archive.testzip() is not None or any(Path(name).is_absolute() or ".." in Path(name).parts for name in files):
                raise AssertionError("Invalid release ZIP contents.")
            archive.extractall(workspace)
        entry, = workspace.glob("*.cjs")

        def cli(argv):
            return subprocess.run([node, entry, *argv], cwd=workspace, capture_output=True,
                                  encoding="utf-8", text=True, timeout=15)

        def scan(case, root, code, *flags, units=("admin.socket", "public.socket"), conflicts=0, issue=None):
            proc = cli(["scan", "--root", str(root), "--json", *flags, *units])
            try:
                report = json.loads(proc.stdout)
            except ValueError as exc:
                raise AssertionError((case, proc.returncode, proc.stdout, proc.stderr)) from exc
            expected_status = {0: "clear", 1: "conflict", 2: "inconclusive", 3: "input_error"}[code]
            assert (proc.returncode, report["exit_code"], report["status"]) == (code, code, expected_status), (case, report)
            assert report["conflict_count"] == conflicts, (case, report)
            codes = [i["code"] for i in report["issues"] + [i for u in report["units"] for i in u["issues"]]]
            assert issue is None or issue in codes, (case, codes)
            assert "warning" not in proc.stderr.lower(), (case, proc.stderr)
            results.append({"case": case, "status": expected_status, "exit_code": code, "conflict_count": conflicts, "issues": codes})
            return report

        version_proc = cli(["--version"])
        version = version_proc.stdout.strip()
        assert version_proc.returncode == 0 and version == f"SocketPreflight {entry.stem.removeprefix('socket-preflight-')}"
        patch = int(version.rsplit(".", 1)[-1])
        samples = workspace / "examples"
        conflicted = scan("shipped-bad-snapshot", samples / "conflict", 1, "--ipv6", "enabled", conflicts=1)
        item = conflicted["conflicts"][0]
        assert item["code"] == "dual_stack_overlap"
        for side in ("first", "second"):
            source = item[side]["setting"]["source"]
            file_lines = (samples / "conflict" / source["path"]).read_text(encoding="utf-8").splitlines()
            assert file_lines[source["line"] - 1] == "ListenStream=" + item[side]["setting"]["value"]
        assert item["first"]["setting"]["source"]["path"] == ADMIN + "admin.socket.d/20-site.conf"
        assert item["second"]["ipv6_mode_source"]["line"] == 6
        reversed_units = scan("argument-order-independent", samples / "conflict", 1, "--ipv6", "enabled",
                              units=("public.socket", "admin.socket"), conflicts=1)
        assert reversed_units == conflicted
        fixed = scan("shipped-reset-snapshot", samples / "fixed", 0, "--ipv6", "enabled")
        assert fixed["units"][0]["active_listen"][0]["value"] == "127.0.0.1:45393"
        assert [event["value"] for event in fixed["units"][0]["resets"][0]["cleared"]] == ["127.0.0.1:45392", "127.0.0.1:45391"]
        assert len(fixed["units"][0]["plan"]["shadowed"]) == 1

        fixtures = read_samples(samples)
        fixture_sha256 = hashlib.sha256(json.dumps(fixtures, sort_keys=True).encode()).hexdigest()
        cli_variants = {"dual-stack-v4-loopback": conflicted, "reset-coexists": fixed}
        for label, files, coexist, _ in example_variants(fixtures):
            if label in cli_variants:
                continue
            root = workspace / label
            for relative, content in files.items():
                put(root, relative, content)
            cli_variants[label] = scan(label, root, int(not coexist), "--ipv6", "enabled",
                                       conflicts=int(not coexist))

        linux_match = None
        if args.systemd_report:
            rows = [json.loads(line) for line in args.systemd_report.read_text(encoding="utf-8").splitlines()]
            assert rows[0]["environment"]["fixture_sha256"] == fixture_sha256
            assert set(row["scenario"] for row in rows[1:]) == set(cli_variants)
            for row in rows[1:]:
                report = cli_variants[row["scenario"]]
                assert row["merge_pass"] and row["verify_exit"] == 0
                assert len(row["attempts"]) == 6 and all(trial["pass"] for trial in row["attempts"])
                assert report["exit_code"] == int(not row["attempts"][0]["expected_coexist"])
                for unit, reference in zip(report["units"], row["manager"], strict=True):
                    actual = sorted(listener["endpoint"] for listener in unit["comparable_listeners"])
                    expected = sorted(value.removesuffix(" (Stream)") for value in reference["Listen"])
                    assert actual == expected, (row["scenario"], unit["name"], actual, expected)
            linux_match = {"scenarios": len(cli_variants), "attempts": sum(len(row["attempts"]) for row in rows[1:]),
                           "report_sha256": hashlib.sha256(args.systemd_report.read_bytes()).hexdigest()}

        def snapshot(name, first=PAYLOAD, second=PAYLOAD):
            root = workspace / name
            put(root, VENDOR + "admin.socket", first)
            put(root, VENDOR + "public.socket", second)
            return root

        generic = snapshot("different-length-file-names")
        put(generic, ADMIN + "public.socket.d/10-a-much-longer-name.conf", PAYLOAD)
        put(generic, RUNTIME + "public.socket.d/20-reset.conf", "[Socket]\nListenStream=\nListenStream=127.0.0.1:45392\n")
        sorted_result = scan("order-is-lexical-not-by-length-or-directory", generic, 0)
        assert sorted_result["units"][1]["plan"]["drop_ins"] == [ADMIN + "public.socket.d/10-a-much-longer-name.conf", RUNTIME + "public.socket.d/20-reset.conf"]

        generic = snapshot("fragment-order")
        put(generic, RUNTIME + "public.socket", "[Socket]\nListenStream=127.0.0.1:45392\n")
        put(generic, ADMIN + "public.socket", "[Socket]\nListenStream=127.0.0.1:45393\n")
        fragments = scan("admin-fragment-wins", generic, 0)
        assert fragments["units"][1]["plan"]["fragment"] == ADMIN + "public.socket"
        assert len(fragments["units"][1]["plan"]["shadowed"]) == 2

        v6 = snapshot("kernel-default", second="[Socket]\nListenStream=[::]:45391\nBindIPv6Only=default\n")
        scan("unknown-kernel-default", v6, 2, "--ipv6", "enabled", issue="unknown_bindv6only")
        scan("kernel-default-dual", v6, 1, "--ipv6", "enabled", "--bindv6only", "0", conflicts=1)
        scan("kernel-default-v6-only", v6, 0, "--ipv6", "enabled", "--bindv6only", "1")
        scan("ipv6-capability-not-assumed", v6, 2, issue="ipv6_support")

        generic = snapshot("known-finding-and-unknown")
        put(generic, VENDOR + "extra.socket", "[Socket]\nListenDatagram=45391\n")
        scan("pair-survives-unrelated-unknown", generic, 1, units=("extra.socket", "admin.socket", "public.socket"), conflicts=1, issue="unsupported_listen_type")

        generic = snapshot("unknown-overlay")
        put(generic, ADMIN + "socket.d/10-type-wide.conf", "[Socket]\nReusePort=yes\n")
        wide = scan("type-wide-binding-option-applies-to-both", generic, 2, issue="unsupported_binding_option")
        assert all(u["issues"][0]["source"]["path"] == ADMIN + "socket.d/10-type-wide.conf" for u in wide["units"])

        generic = snapshot("inherited-name")
        full = "apps-region-public.socket"
        units = ("admin.socket", full)
        reset = "[Socket]\nListenStream=\nListenStream=127.0.0.1:45392\n"
        put(generic, VENDOR + full, PAYLOAD)
        put(generic, VENDOR + full + ".d/20-priority.conf", PAYLOAD)
        put(generic, ADMIN + "apps-region-.socket.d/20-priority.conf", reset)
        put(generic, ADMIN + "apps-.socket.d/20-priority.conf", PAYLOAD)
        merged = scan("prefix-precedence-within-and-across-roots", generic, 0, units=units)
        winner = ADMIN + "apps-region-.socket.d/20-priority.conf"
        assert merged["units"][1]["plan"]["drop_ins"] == [winner]
        assert len([row for row in merged["units"][1]["plan"]["shadowed"] if row["winner"] == winner]) == 2
        assert merged["units"][1]["comparable_listeners"][0]["setting"]["source"]["path"] == winner

        generic = snapshot("specific-versus-type-wide")
        put(generic, VENDOR + "public.socket.d/20-shared.conf", reset)
        put(generic, ADMIN + "socket.d/20-shared.conf", "")
        result = scan("vendor-specific-over-admin-type-wide", generic, 0)
        assert result["units"][0]["plan"]["drop_ins"] == [ADMIN + "socket.d/20-shared.conf"]
        assert result["units"][1]["plan"]["shadowed"] == [
            {"path": ADMIN + "socket.d/20-shared.conf", "winner": VENDOR + "public.socket.d/20-shared.conf"}
        ]

        generic = snapshot("type-and-exact-lexical-order")
        put(generic, VENDOR + "public.socket.d/10-long-additional.conf", "[Socket]\nListenStream=127.0.0.1:45393\n")
        put(generic, ADMIN + "socket.d/20-global-reset.conf", reset)
        put(generic, RUNTIME + "socket.d/20-global-reset.conf", PAYLOAD)
        result = scan("type-wide-collides-after-global-sort", generic, 1, conflicts=1)
        assert result["units"][1]["plan"]["drop_ins"] == [
            VENDOR + "public.socket.d/10-long-additional.conf", ADMIN + "socket.d/20-global-reset.conf"
        ]
        assert len(result["units"][1]["resets"][0]["cleared"]) == 2

        generic = snapshot("other-type-overlay")
        put(generic, "run/systemd/generator.early/socket.d/10-generated.conf", PAYLOAD)
        scan("unsupported-generator-type-wide", generic, 2, issue="unsupported_unit_path")

        extended = {}
        for case in cases():
            if patch < 3 and case.suite == "mapped":
                continue
            root = workspace / case.name
            for path, value in case.files.items():
                put(root, path, value)
            code = {"coexist": 0, "conflict": 1, "invalid_bind": 2}[case.outcome]
            issue = "mapped_ipv4_requires_dual_stack" if case.outcome == "invalid_bind" else None
            report = scan(case.name, root, code, "--ipv6", "enabled", units=case.units, conflicts=int(code == 1), issue=issue)
            assert [u["plan"]["drop_ins"] for u in report["units"]] == [list(paths) for paths in case.selected]
            endpoints = [[value["endpoint"] for value in u["comparable_listeners"]] for u in report["units"]]
            expected = [list(items) for items in case.endpoints]
            if code == 2:
                expected[0] = []
            assert endpoints == expected, (case.name, endpoints, expected)
            if case.conflict:
                assert report["conflicts"][0]["code"] == case.conflict
                if case.suite == "mapped":
                    assert report["conflicts"][0]["first"]["mapped_ipv4"] == "127.0.0.1"
            extended[case.name] = (case, report)

        extra_linux = []
        for path in args.extended_systemd_report:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
            assert rows[0]["environment"]["systemd"] and len(rows) > 1
            suite = rows[1]["suite"]
            assert {row["scenario"] for row in rows[1:]} == {name for name, (case, _) in extended.items() if case.suite == suite}
            for row in rows[1:]:
                case, report = extended[row["scenario"]]
                assert row["fixture_sha256"] == hashlib.sha256(json.dumps(asdict(case), sort_keys=True).encode()).hexdigest()
                assert row["merge_pass"] and row["outcome"] == case.outcome and row["verify_exit"] == 0
                assert len(row["attempts"]) == (3 if case.outcome == "invalid_bind" else 6)
                assert all(attempt["pass"] for attempt in row["attempts"])
                assert [u["plan"]["drop_ins"] for u in report["units"]] == row["selected"]
                for index, unit in enumerate(report["units"]):
                    if case.outcome == "invalid_bind" and index == 0:
                        assert not unit["comparable_listeners"]
                        continue
                    assert [value["endpoint"] for value in unit["comparable_listeners"]] == row["endpoints"][index]
            extra_linux.append({"suite": suite, "scenarios": len(rows) - 1,
                                "attempts": sum(len(row["attempts"]) for row in rows[1:]),
                                "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

        if patch >= 3:
            generic = snapshot("mapped-kernel-default", second=listener("[::ffff:127.0.0.1]", "default"))
            scan("mapped-default-unknown", generic, 2, "--ipv6", "enabled", issue="unknown_bindv6only")
            scan("mapped-default-is-dual", generic, 1, "--ipv6", "enabled", "--bindv6only", "0", conflicts=1)
            scan("mapped-default-is-ipv6-only", generic, 2, "--ipv6", "enabled", "--bindv6only", "1",
                 issue="mapped_ipv4_requires_dual_stack")
            scan("mapped-ipv6-capability-unknown", generic, 2, issue="ipv6_support")
            generic = snapshot("mapped-wildcard-outside-coverage", second=listener("[::ffff:0.0.0.0]", "both"))
            scan("mapped-wildcard-is-uncovered", generic, 2, "--ipv6", "enabled", issue="unsupported_mapped_ipv4")

        generic = snapshot("generator")
        put(generic, "run/systemd/generator.early/public.socket", PAYLOAD)
        scan("unsupported-generator-fragment", generic, 2, issue="unsupported_unit_path")

        generic = snapshot("masked")
        put(generic, ADMIN + "public.socket", "")
        scan("mask-blocks-vendor", generic, 2, issue="masked_fragment")

        generic = snapshot("binding-options", second=PAYLOAD + "ReusePort=yes\n")
        scan("reuseport-is-inconclusive", generic, 2, issue="unsupported_binding_option")
        generic = snapshot("no-empty-rollback", second=PAYLOAD + "SocketProtocol=sctp\nSocketProtocol=\n")
        scan("empty-socketprotocol-does-not-reset", generic, 2, issue="unsupported_binding_option")

        generic = snapshot("bad-encoding")
        put(generic, ADMIN + "public.socket", b"[Socket]\n\xff\x00")
        scan("bad-selected-file-encoding", generic, 3, issue="invalid_utf8")
        generic = snapshot("bad-unselected-file")
        put(generic, VENDOR + "public.socket.d/20-site.conf", b"\xff")
        put(generic, ADMIN + "public.socket.d/20-site.conf", "[Socket]\nListenStream=\nListenStream=127.0.0.1:45392\n")
        scan("shadowed-file-content-is-not-read", generic, 0)
        scan("missing-unit", workspace / "bad-encoding", 3,
             units=("admin.socket", "missing.socket"), issue="missing_fragment")
        scan("duplicate-name-rejected", samples / "conflict", 3,
             units=("admin.socket", "admin.socket"), issue="invalid_command")
        scan("path-traversal-rejected", samples / "conflict", 3,
             units=("admin.socket", "../public.socket"), issue="invalid_command")

        generic = snapshot("link-test")
        original = generic / ADMIN / "public.socket"
        original.parent.mkdir(parents=True, exist_ok=True)
        outside = workspace / "outside.socket"
        outside.write_bytes(b"\xff")
        try:
            original.symlink_to(outside)
        except OSError as error:
            results.append({"case": "links-are-not-followed", "status": "skipped", "reason": type(error).__name__})
        else:
            scan("links-are-not-followed", generic, 2, issue="symlink")
            if patch >= 5:
                linked_root = snapshot("linked-shared-directory")
                outside_dir = workspace / "unexported-drop-ins"
                put(outside_dir, "10-site.conf", b"\xff")
                link = linked_root / ADMIN / "socket.d"
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside_dir, target_is_directory=True)
                report = scan("shared-directory-link-is-never-followed", linked_root, 2, issue="symlink")
                assert all(unit["issues"][0]["code"] == "symlink" for unit in report["units"])

        if patch >= 5:
            generic = snapshot("case-sensitive-linux-snapshot")
            put(generic, ADMIN + "Public.socket", b"\xff")
            put(generic, "run/Systemd/system/socket.d/10-wrong-case.conf", b"\xff")
            report = scan("exact-case-preserved-with-cached-paths", generic, 1, conflicts=1)
            assert all(unit["plan"]["fragment"].startswith(VENDOR) for unit in report["units"])

        proc = cli(["scan", "--root", "examples/conflict", "--ipv6", "enabled", "admin.socket", "public.socket"])
        assert proc.returncode == 1 and "20-site.conf:2" in proc.stdout and "public.socket:5" in proc.stdout
        results.append({"case": "human-readable-two-sided-location", "status": "conflict", "exit_code": 1})

        if patch >= 4:
            def compare(label, baseline, candidate, code, *, units=("admin.socket", "public.socket"),
                        counts=(0, 0, 0, 0, 0), complete=True):
                proc = cli(["compare", "--baseline", str(baseline), "--candidate", str(candidate),
                            "--ipv6", "enabled", "--json", *units])
                report = json.loads(proc.stdout)
                expected = {0: "clear", 1: "regression", 2: "inconclusive", 3: "input_error"}[code]
                assert (proc.returncode, report["exit_code"], report["status"], report["complete"]) == (
                    code, code, expected, complete), (label, report)
                categories = ("new_conflicts", "existing_conflicts", "resolved_conflicts", "uncertain_new", "uncertain_resolved")
                assert tuple(len(report[key]) for key in categories) == counts, (label, counts, report)
                results.append({"case": label, "status": expected, "exit_code": code, "counts": dict(zip(categories, counts))})
                return report

            fixed_root, bad_root = samples / "fixed", samples / "conflict"
            compare("new-conflict-blocks-deployment", fixed_root, bad_root, 1, counts=(1, 0, 0, 0, 0))
            compare("resolved-conflict-does-not-block", bad_root, fixed_root, 0, counts=(0, 0, 1, 0, 0))
            compare("carried-conflict-does-not-block", bad_root, bad_root, 0, counts=(0, 1, 0, 0, 0))

            before = snapshot("compare-before", second=PAYLOAD + "ListenStream=127.0.0.1:45392\n")
            after = snapshot("compare-after", first=PAYLOAD + "ListenStream=127.0.0.1:45393\n",
                             second=PAYLOAD + "ListenStream=127.0.0.1:45393\n")
            put(before, VENDOR + "admin.socket", PAYLOAD + "ListenStream=127.0.0.1:45392\n")
            result = compare("one-new-existing-and-resolved", before, after, 1, counts=(1, 1, 1, 0, 0))
            assert (result["new_conflicts"][0]["port"], result["existing_conflicts"][0]["port"],
                    result["resolved_conflicts"][0]["port"]) == (45393, 45391, 45392)

            shifted = snapshot("line-shift", first="# inserted comment\n" + PAYLOAD)
            result = compare("provenance-changes-keep-binding-identity",
                             snapshot("plain-comparison"), shifted, 0, counts=(0, 1, 0, 0, 0))
            assert result["candidate"]["conflict_count"] == 1
            assert result["existing_conflicts"][0]["first"]["setting"]["source"]["line"] == 3

            if patch >= 14:
                wildcard = "ListenStream=0.0.0.0:45391\n"
                ordered = snapshot("intra-unit-wildcard", first=PAYLOAD + wildcard)
                reversed_order = snapshot("intra-unit-reordered",
                                          first="[Socket]\n" + wildcard + PAYLOAD.split("\n", 1)[1])
                single = ("admin.socket",)
                intra = scan("intra-unit-wildcard-conflict", ordered, 1, units=single, conflicts=1)
                finding = intra["conflicts"][0]
                assert finding["code"] == "wildcard_overlap"
                assert [(finding[k]["unit"], finding[k]["setting"]["source"]["line"])
                        for k in ("first", "second")] == [("admin.socket", 2), ("admin.socket", 3)]
                compare("intra-unit-order-does-not-change-identity", ordered, reversed_order, 0,
                        units=single, counts=(0, 1, 0, 0, 0))
                compare("intra-unit-regression-blocks", shifted, ordered, 1,
                        units=single, counts=(1, 0, 0, 0, 0))
                compare("intra-unit-reset-resolves", ordered, shifted, 0,
                        units=single, counts=(0, 0, 1, 0, 0))

            mapped = snapshot("mapped-representation", first=listener("[::ffff:127.0.0.1]", "both"))
            compare("mapped-and-native-ipv4-keep-identity", shifted, mapped, 0, counts=(0, 1, 0, 0, 0))

            gap = snapshot("compare-gap", first=PAYLOAD + "ReusePort=yes\n")
            compare("baseline-gap-keeps-addition-uncertain", gap, shifted, 2, counts=(0, 0, 0, 1, 0), complete=False)
            compare("candidate-gap-keeps-removal-uncertain", shifted, gap, 2, counts=(0, 0, 0, 0, 1), complete=False)
            for root in (before, after):
                put(root, VENDOR + "extra.socket", "[Socket]\nListenDatagram=45391\n")
            compare("unrelated-gap-preserves-proven-regression", before, after, 1,
                    units=("public.socket", "extra.socket", "admin.socket"), counts=(1, 1, 1, 0, 0), complete=False)

            dense = workspace / "compare-truncated"
            dense_units = tuple(f"edge-{i:02}.socket" for i in range(24))
            for unit in dense_units:
                put(dense, VENDOR + unit, PAYLOAD)
            result = compare("truncated-reports-cannot-prove-no-regression", dense, dense, 2,
                             units=dense_units, counts=(0, 256, 0, 0, 0), complete=False)
            assert result["baseline"]["conflicts_omitted"] == 20

            result = compare("invalid-candidate-root", fixed_root, workspace / "missing-root", 3, complete=False)
            assert result["candidate"]["issues"][0]["code"] == "invalid_command"
            wrong_args = cli(["compare", "--root", str(fixed_root), "--json", "admin.socket", "public.socket"])
            assert wrong_args.returncode == 3 and json.loads(wrong_args.stdout)["issues"][0]["code"] == "invalid_command"
            results.append({"case": "compare-requires-two-roots", "status": "input_error", "exit_code": 3})
            proc = cli(["compare", "--baseline", "examples/fixed", "--candidate", "examples/conflict",
                        "--ipv6", "enabled", "admin.socket", "public.socket"])
            assert proc.returncode == 1 and "1 new, 0 existing, 0 resolved" in proc.stdout and "20-site.conf:2" in proc.stdout
            results.append({"case": "human-readable-regression-details", "status": "regression", "exit_code": 1})
    proof = {
        "version": version,
        "host": platform.system(),
        "node": subprocess.check_output([node, "--version"], text=True).strip(),
        "archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        "fixture_sha256": fixture_sha256,
        "linux_manager_cross_check": linux_match,
        "extended_linux_cross_checks": extra_linux,
        "results": results,
        "totals": dict(Counter("skip" if row["status"] == "skipped" else "pass" for row in results)),
    }
    if args.report:
        args.report.parent.mkdir(exist_ok=True, parents=True)
        args.report.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(proof["totals"], sort_keys=True))


if __name__ == "__main__":
    main()
