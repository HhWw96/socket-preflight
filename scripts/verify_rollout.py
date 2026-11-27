#!/usr/bin/env python3
"""Check the packaged deployment example against its isolated Linux receipt."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname
import zipfile

from verify_rollout_systemd import CASES, fixture


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_systemd(receipt, example):
    rows = [json.loads(line) for line in receipt.read_text(encoding="utf-8-sig").splitlines()]
    evidence = {row["stage"]: row for row in rows[1:]}
    assert evidence.keys() == CASES.keys() and len(rows) == len(CASES) + 1
    for stage, units in CASES.items():
        row = evidence[stage]
        data = fixture(example, stage)
        assert row["fixture_sha256"] == hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        assert row["merge_pass"] and row["effective"] == {unit: [endpoint] for unit, endpoint in units.items()}
        assert row["verify"]["exit_code"] == row["list_sockets_before_start"]["exit_code"] == 0
        assert all(attempt["pass"] for attempt in row["attempts"])
        assert len(row["attempts"]) == (6 if len(units) == 2 else 3)
        if stage == "overlap":
            assert all(attempt["second_exit"] != 0 and attempt["kernel_bind_errno"] == "EADDRINUSE"
                       for attempt in row["attempts"])
    return {"sha256": sha(receipt), "stages": len(evidence),
            "trials": sum(len(row["attempts"]) for row in evidence.values()),
            "environment": rows[0]["environment"]}


def run_cli(node, entry, *args, code):
    result = subprocess.run([node, entry, *args], capture_output=True, encoding="utf-8", timeout=20)
    assert result.returncode == code and not result.stderr, (args, result)
    if "--json" in args and "--sarif" not in args:
        report = json.loads(result.stdout)
        assert report["exit_code"] == code
        return report
    return result.stdout.strip()


def check_plans(node, entry, example, checks):
    patch = int(entry.stem.rsplit(".", 1)[-1])

    def gate(label, plan, code, schema=1, status=None):
        report = run_cli(node, entry, "rollout", "--plan", str(plan), "--json", code=code)
        assert report["schema_version"] == schema
        assert report["status"] == (status or {0: "clear", 1: "regression", 2: "inconclusive", 3: "input_error"}[code])
        checks[label] = {"exit_code": code, "complete": report["complete"]}
        return report

    bad = gate("overlap_only", example / "bad.json", 1)
    assert bad["complete"] and not bad["candidate"]["new_conflicts"]
    pair, = bad["transition"]["new_conflicts"]
    assert [(pair[side]["unit"], pair[side]["setting"]["source"]["line"]) for side in ("first", "second")] == [
        ("legacy.socket", 3), ("next.socket", 2)]
    for label, stage in (("before", bad["candidate"]["baseline"]),
                         ("overlap", bad["transition"]["candidate"]),
                         ("after", bad["candidate"]["candidate"])):
        assert {u["name"]: [a["endpoint"] for a in u["comparable_listeners"]] for u in stage["units"]} == {
            unit: [endpoint] for unit, endpoint in CASES[label].items()}
    human = run_cli(node, entry, "rollout", "--plan", str(example / "bad.json"), code=1)
    assert "[transition]" in human and "legacy.socket:3" in human and "next.socket:2" in human
    fixed = gate("reset_allows_coexistence", example / "fixed.json", 0)
    assert fixed["complete"]
    reset, = fixed["transition"]["candidate"]["units"][1]["resets"]
    assert reset["cleared"][0]["value"] == "127.0.0.1:45391"
    checks["reset_allows_coexistence"]["endpoints"] = {
        u["name"]: [a["endpoint"] for a in u["comparable_listeners"]]
        for u in fixed["transition"]["candidate"]["units"]}
    assert checks["reset_allows_coexistence"]["endpoints"] == {u: [a] for u, a in CASES["fixed"].items()}

    raw = json.loads((example / "bad.json").read_text(encoding="utf-8"))
    plan = example / "test-plan.json"
    both = raw["transition"]

    def variant(label, code, **changes):
        plan.write_text(json.dumps({**raw, **changes}), encoding="utf-8")
        return gate(label, plan, code)

    existing = variant("carried_conflict", 0, baseline=both, candidate=both)
    assert len(existing["candidate"]["existing_conflicts"]) == 1
    resolved = variant("explicit_removal_resolves", 0, baseline=both)
    assert len(resolved["candidate"]["resolved_conflicts"]) == 1
    with_empty = variant("zero_unit_stage", 0, transition={"root": "overlap", "units": []})
    assert with_empty["transition"]["candidate"]["units"] == []
    missing = variant("declared_unit_missing", 3, candidate={"root": "after", "units": ["missing.socket"]})
    assert missing["candidate"]["candidate"]["units"][0]["issues"][0]["code"] == "missing_fragment"
    unknown = variant("ipv6_unknown", 2, environment={"ipv6": "unknown", "bindv6only": "unknown"})
    assert not unknown["complete"]
    variant("duplicate_unit", 3, baseline={"root": "before", "units": ["legacy.socket"] * 2})
    variant("missing_empty_stage_root", 3, transition={"root": "missing-root", "units": []})
    variant("misspelled_transition", 3, transiton=both)
    plan.write_text((example / "bad.json").read_text(encoding="utf-8").replace(
        '"schema_version": 1,', '"schema_version": 1, "schema_version": 1,'), encoding="utf-8")
    gate("duplicate_json_key", plan, 3)
    plan.write_text(json.dumps({key: value for key, value in raw.items() if key != "transition"}), encoding="utf-8")
    assert gate("overlap_not_declared", plan, 0).get("transition") is None

    dense = example / "dense/usr/lib/systemd/system"
    dense.mkdir(parents=True)
    names = [f"pair-{index:02}.socket" for index in range(24)]
    for name in names:
        (dense / name).write_text("[Socket]\nListenStream=127.0.0.1:45391\n", encoding="utf-8")
    scope = {"root": "dense", "units": names}
    incomplete = variant("truncation_is_inconclusive", 2, baseline=scope, transition=scope, candidate=scope)
    assert incomplete["candidate"]["candidate"]["conflicts_omitted"] == 20

    checkpoints = example / "checkpoints.json"
    if checkpoints.is_file():
        result = gate("named-checkpoint-blocks-clear-candidate", checkpoints, 1, schema=2)
        assert result["schema_version"] == 2
        assert [(stage["name"], stage["result"]["exit_code"]) for stage in result["checkpoints"]] == [
            ("canary", 0), ("coexist", 1)]
        assert result["candidate"]["exit_code"] == 0
        assert result["checkpoints"][1]["result"]["new_conflicts"][0]["port"] == 45391
        staged_plan = json.loads(checkpoints.read_text(encoding="utf-8"))
        stages = staged_plan["checkpoints"]
        stages[1]["root"] = "fixed"
        if patch >= 27:
            staged_plan["candidate"]["rebind_all"] = True
        plan.write_text(json.dumps(staged_plan), encoding="utf-8")
        gate("fixed-checkpoints-pass", plan, 0, schema=2)
        stages[1]["name"] = stages[0]["name"]
        plan.write_text(json.dumps(staged_plan), encoding="utf-8")
        gate("duplicate-checkpoint-name", plan, 3)
        stages[1]["name"] = "candidate"
        plan.write_text(json.dumps(staged_plan), encoding="utf-8")
        gate("reserved-checkpoint-name", plan, 3)

        if int(entry.stem.rsplit(".", 1)[-1]) >= 18:
            old = dict(raw["transition"])
            phase = {"name": "coexist", **old}
            policy_plan = {**staged_plan, "baseline": old, "checkpoints": [phase]}
            plan.write_text(json.dumps(policy_plan), encoding="utf-8")
            gate("existing-conflict-continues-with-default-policy", plan, 0, schema=2)
            policy_plan["policy"] = "require-clear"
            plan.write_text(json.dumps(policy_plan), encoding="utf-8")
            blocked = gate("strict-policy-blocks-existing-conflict", plan, 1, schema=2, status="conflict")
            stage = blocked["checkpoints"][0]["result"]
            assert len(stage["existing_conflicts"]) == 1 and not stage["new_conflicts"]
            policy_plan["checkpoints"] = [{**phase, "root": "fixed"}]
            if patch >= 27:
                policy_plan["checkpoints"][0]["rebind_all"] = True
            plan.write_text(json.dumps(policy_plan), encoding="utf-8")
            gate("strict-policy-allows-a-resolved-baseline-conflict", plan, 0, schema=2)
            policy_plan["policy"] = "typo"
            plan.write_text(json.dumps(policy_plan), encoding="utf-8")
            gate("strict-policy-spelling-is-validated", plan, 3)

    if patch >= 24:
        stage_dir = example / "target-env/usr/lib/systemd/system"
        stage_dir.mkdir(parents=True)
        for name, endpoint in (("v6.socket", "[::]:45397"), ("v4.socket", "127.0.0.1:45397")):
            (stage_dir / name).write_text(f"[Socket]\nListenStream={endpoint}\n", encoding="utf-8")
        a = {"ipv6": "enabled", "bindv6only": "1"}
        b = {"ipv6": "enabled", "bindv6only": "0"}
        shared = {"root": "target-env", "units": ["v4.socket", "v6.socket"]}

        def target(label, code, before, after, schema=2, status=None, **attrs):
            plan.write_text(json.dumps({"schema_version": 2, "environment": before, "baseline": shared,
                "checkpoints": [], "candidate": {**shared, "environment": after, **attrs}}), encoding="utf-8")
            return gate(label, plan, code, schema=schema, status=status)

        check = target("environment-change-needs-rebind", 2, a, b)
        assert check["issues"][0]["code"] == "unverified_rebind"
        check = target("confirmed-rebind-creates-overlap", 1, a, b, rebind_all=True)
        assert check["candidate"]["new_conflicts"][0]["port"] == 45397
        assert check["candidate"]["candidate"]["environment"] == b
        check = target("confirmed-rebind-resolves-overlap", 0, b, a, rebind_all=True)
        assert check["candidate"]["resolved_conflicts"][0]["port"] == 45397
        target("stage-rebind-must-be-boolean", 3, a, b, schema=1, rebind_all="true")
        target("stage-environment-must-be-complete", 3, a, {"ipv6": "enabled"}, schema=1)
        plan.write_text(json.dumps({"schema_version": 2, "environment": a, "baseline": shared,
            "checkpoints": [{**shared, "name":"flip", "environment":b, "rebind_all":True}],
            "candidate": shared}), encoding="utf-8")
        check = gate("return-to-default-needs-rebind-too", plan, 1 if patch >= 25 else 2, schema=2)
        assert "candidate" in check["issues"][0]["message"]
        if patch >= 25:
            assert check["checkpoints"][0]["result"]["new_conflicts"][0]["port"] == 45397
        plan.write_text(json.dumps({"schema_version": 2, "environment": a, "baseline": shared,
            "policy": "require-clear",
            "checkpoints": [{**shared, "name":"flip", "environment":b, "rebind_all":True}],
            "candidate": {**shared, "rebind_all": True}}), encoding="utf-8")
        check = gate("strict-stage-environment-change-is-blocked", plan, 1, schema=2, status="conflict")
        assert check["checkpoints"][0]["result"]["new_conflicts"]

    if patch >= 25:
        staged = json.loads((example / "checkpoints.json").read_text(encoding="utf-8"))
        changed = {**staged["candidate"], "environment": b}

        def staged_gate(label, code, status=None, **updates):
            plan.write_text(json.dumps({**staged, **updates}), encoding="utf-8")
            return gate(label, plan, code, schema=2, status=status)

        result = staged_gate("confirmed-overlap-survives-later-rebind-uncertainty", 1,
                             candidate=changed)
        assert not result["complete"] and result.get("candidate") is None
        assert [stage["name"] for stage in result["checkpoints"]] == ["canary", "coexist"]
        conflict, = result["checkpoints"][1]["result"]["new_conflicts"]
        assert [(conflict[side]["unit"], conflict[side]["setting"]["source"]["line"])
                for side in ("first", "second")] == [("legacy.socket", 3), ("next.socket", 2)]
        assert result["issues"][0]["message"].startswith("candidate:")
        human = run_cli(node, entry, "rollout", "--plan", str(plan), code=1)
        assert "[coexist]" in human and "legacy.socket:3" in human and "next.socket:2" in human
        strict = staged_gate("strict-conflict-survives-later-rebind-uncertainty", 1,
                             status="conflict", candidate=changed, policy="require-clear")
        assert not strict["complete"]

        result = staged_gate("input-error-precedes-conflict-and-unverified-rebind", 3,
                             candidate={**changed, "root": "before"})
        assert result["checkpoints"][1]["result"]["new_conflicts"]
        assert {issue["code"] for issue in result["issues"]} == {"unverified_rebind", "missing_fragment"}

        pending = {**shared, "name": "change", "environment": b}
        retained = {**pending, "name": "retained"}
        result = staged_gate("uncertainty-follows-retained-units", 2, baseline=shared,
                             environment=a, checkpoints=[pending, retained],
                             candidate={**shared, "environment": b})
        assert result["checkpoints"] == [] and result.get("candidate") is None
        assert [issue["message"].split(":", 1)[0] for issue in result["issues"]] == [
            "change", "retained", "candidate"]

        rebind = {**retained, "environment": a, "rebind_all": True}
        result = staged_gate("confirmed-recreation-restores-later-coverage", 2, baseline=shared,
                             environment=a, checkpoints=[pending, rebind], candidate=shared)
        assert [stage["name"] for stage in result["checkpoints"]] == ["retained"]
        assert result["checkpoints"][0]["result"]["complete"] and result["candidate"]["complete"]
        assert not result["complete"] and len(result["issues"]) == 1

        removed = {**pending, "name": "removed", "units": []}
        result = staged_gate("removed-and-returned-unit-has-fresh-coverage", 2, baseline=shared,
                             environment=a, checkpoints=[pending, removed], candidate=shared)
        assert [stage["name"] for stage in result["checkpoints"]] == ["removed"]
        assert result["candidate"]["complete"] and len(result["issues"]) == 1

    if patch >= 27:
        check_reconfigured_bindings(example, plan, gate, selective=patch >= 29)
    if patch >= 30:
        check_sarif(node, entry, example, checks)


def check_sarif(node, entry, example, checks):
    def results(label, plan, code, *flags):
        args = ("rollout", "--plan", str(plan))
        if not flags:
            run_cli(node, entry, *args, "--json", code=code)
        log = json.loads(run_cli(node, entry, *args, "--sarif", *flags, code=code))
        assert log["version"] == "2.1.0"
        sarif, = log["runs"]
        assert sarif["invocations"][0]["executionSuccessful"] == (code != 3)
        base = sarif["originalUriBaseIds"]["%SRCROOT%"]["uri"]
        for item in sarif["results"]:
            for key in ("locations", "relatedLocations"):
                for result in item.get(key, []):
                    physical = result["physicalLocation"]
                    path = urlsplit(urljoin(base, physical["artifactLocation"]["uri"])).path
                    source = Path(url2pathname(path))
                    assert source.is_relative_to(example) and source.is_file(), source
        checks[label] = {"exit_code": code, "results": len(sarif["results"])}
        return sarif["results"]

    for plan, stage in (("bad.json", "transition"), ("checkpoints.json", "coexist")):
        found = results("sarif-" + stage, example / plan, 1)
        diagnostic, = [item for item in found if item["ruleId"] == "TCP_BIND_OVERLAP"]
        assert diagnostic["level"] == "error" and stage + ":" in diagnostic["message"]["text"]
        assert "TCP 45391" in diagnostic["message"]["text"]
        for field, name, line in (("locations", "legacy.socket", 3), ("relatedLocations", "next.socket", 2)):
            located = diagnostic[field][0]["physicalLocation"]
            assert located["artifactLocation"]["uri"].endswith("/overlap/usr/lib/systemd/system/" + name)
            assert located["region"]["startLine"] == line

    assert results("sarif-clear", example / "fixed.json", 0) == []
    assert results("sarif-bad-plan", example / "absent.json", 3)[0]["ruleId"] == "INVALID_INPUT"
    assert results("sarif-exclusive-format", example / "bad.json", 3, "--json")[0]["ruleId"] == "INVALID_INPUT"
    raw = json.loads((example / "checkpoints.json").read_text(encoding="utf-8"))
    plan = example / "sarif-test.json"
    plan.write_text(json.dumps({**raw, "environment": {"ipv6": "unknown", "bindv6only": "unknown"}}),
                    encoding="utf-8")
    assert any(item["level"] == "warning" for item in results("sarif-coverage", plan, 2))
    stage = {key: val for key, val in raw["checkpoints"][-1].items() if key != "name"}
    plan.write_text(json.dumps({**raw, "policy": "require-clear",
                                "baseline": stage, "candidate": stage, "checkpoints": []}), encoding="utf-8")
    assert results("sarif-strict-existing-conflict", plan, 1)[0]["level"] == "error"


def check_reconfigured_bindings(example, plan, gate, selective):
    def snapshot(name, units):
        directory = example / name / "usr/lib/systemd/system"
        directory.mkdir(parents=True)
        for unit, content in units.items():
            (directory / unit).write_text("[Socket]\n" + content, encoding="utf-8")
        return {"root": name, "units": list(units)}

    legacy, next_unit = "legacy.socket", "next.socket"
    old_binding = "ListenStream=0.0.0.0:45391\n"
    next_binding = "ListenStream=127.0.0.1:45391\n"
    initial = snapshot("before-reconfiguration", {legacy: old_binding})
    collision = snapshot("old-listener-overlap", {legacy: old_binding, next_unit: next_binding})
    moved = snapshot("port-changed", {legacy: "ListenStream=0.0.0.0:45392\n", next_unit: next_binding})
    missing = snapshot("missing-retained-unit", {next_unit: next_binding})
    v6_binding = "ListenStream=[::]:45391\n"
    dual = snapshot("old-dual-stack", {legacy: v6_binding + "BindIPv6Only=both\n"})
    v6_only = snapshot("new-v6-only", {legacy: v6_binding + "BindIPv6Only=ipv6-only\n",
                                      next_unit: next_binding})
    environment = {"ipv6": "enabled", "bindv6only": "1"}

    def check(label, before, candidate, code, checkpoints=(), status=None, schema=2):
        spec = {"schema_version": 2, "policy": "require-clear", "environment": environment,
                "baseline": before, "checkpoints": list(checkpoints), "candidate": candidate}
        plan.write_text(json.dumps(spec), encoding="utf-8")
        return gate(label, plan, code, schema=schema, status=status)

    for label, before, after in (("changed-port", initial, moved), ("changed-ipv6-mode", dual, v6_only)):
        blocked = check(label + "-needs-rebind", before, after, 2)
        assert blocked.get("candidate") is None and not blocked["complete"]
        issue, = blocked["issues"]
        assert issue["code"] == "unverified_rebind" and legacy in issue["message"]
        assert check(label + "-with-confirmed-rebind", before, {**after, "rebind_all": True}, 0)["complete"]

    overlap = {"name": "overlap", **collision}
    report = check("confirmed-conflict-precedes-later-binding-reconfiguration", initial, moved,
                   1, [overlap], status="conflict")
    assert not report["complete"] and report["issues"][0]["code"] == "unverified_rebind"
    assert report["checkpoints"][0]["result"]["new_conflicts"][0]["port"] == 45391

    invalid = check("invalid-retained-unit-precedes-binding-reconfiguration",
                    initial, {**missing, "units": [legacy, next_unit]}, 3, [overlap])
    assert {issue["code"] for issue in invalid["issues"]} == {"unverified_rebind", "missing_fragment"}

    if selective:
        monitor = "monitor.socket"
        watched = snapshot("watched-before", {legacy: old_binding, monitor: "ListenStream=45393\n"})
        stable = snapshot("watched-stable", {legacy: "ListenStream=45392\n",
                          monitor: "ListenStream=45393\n", next_unit: next_binding})
        changed = snapshot("watched-changed", {legacy: "ListenStream=45392\n",
                           monitor: "ListenStream=45394\n", next_unit: next_binding})
        declared = {**stable, "rebind_units": [legacy]}
        assert check("selective-rebind-preserves-monitor", watched, declared, 0)["complete"]
        waiting = {**changed, "rebind_units": [legacy]}
        result = check("selective-rebind-leaves-other-binding-pending", watched, waiting, 2)
        issue, = result["issues"]
        assert monitor in issue["message"] and legacy not in issue["message"]
        checkpoint = {**waiting, "name": "first"}
        report = check("selective-rebind-keeps-earlier-coverage-gap", watched,
                       {**changed, "rebind_units": [monitor]}, 2, [checkpoint])
        assert report["candidate"]["complete"] and len(report["issues"]) == 1
        for label, declarations, schema in (
            ("unknown", {"rebind_units": ["unknown.socket"]}, 2),
            ("duplicate", {"rebind_units": [legacy, legacy]}, 2),
            ("mixed", {"rebind_units": [legacy], "rebind_all": True}, 2),
            ("wrong-type", {"rebind_units": legacy}, 1),
        ):
            check(f"invalid-selective-rebind-{label}", watched, {**stable, **declarations}, 3,
                  status="input_error", schema=schema)
        check("selective-rebind-not-allowed-at-baseline", {**watched, "rebind_units": [legacy]}, stable, 3, schema=1)


def verify(archive, receipt):
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js 24 is required.")
    with tempfile.TemporaryDirectory(prefix="socket rollout 复验 ") as temp:
        root = Path(temp)
        with zipfile.ZipFile(archive) as bundle:
            assert not bundle.testzip()
            assert all(not Path(name).is_absolute() and ".." not in Path(name).parts for name in bundle.namelist())
            bundle.extractall(root)
        entry, = root.glob("*.cjs")
        example = root / "examples/rollout"
        version = run_cli(node, entry, "--version", code=0)
        assert version == "SocketPreflight " + entry.stem.removeprefix("socket-preflight-")
        checks = {}
        for stage, code in (("overlap", 1), ("fixed", 0)):
            report = run_cli(node, entry, "scan", "--root", str(example / stage),
                             "--ipv6", "enabled", "--json", *CASES[stage], code=code)
            actual = {unit["name"]: [item["endpoint"] for item in unit["comparable_listeners"]]
                      for unit in report["units"]}
            assert actual == {unit: [endpoint] for unit, endpoint in CASES[stage].items()}
            checks[stage + "_scan"] = {"exit_code": code, "endpoints": actual}
            if code:
                pair, = report["conflicts"]
                assert pair["code"] == "dual_stack_overlap"
                for side in ("first", "second"):
                    listener = pair[side]
                    source = listener["setting"]["source"]
                    lines = (example / stage / source["path"]).read_text(encoding="utf-8").splitlines()
                    assert lines[source["line"] - 1] == f"ListenStream={listener['endpoint']}"
        systemd = check_systemd(receipt, example) if receipt else None
        if int(version.rsplit(".", 1)[-1]) >= 7:
            check_plans(node, entry, example, checks)
    return {"version": version, "archive_sha256": sha(archive), "host": platform.platform(),
            "node": subprocess.check_output([node, "--version"], text=True).strip(),
            "checks": checks, "systemd": systemd}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--systemd-report", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.archive, args.systemd_report)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{result['version']}: {len(result['checks'])} rollout checks passed", flush=True)


if __name__ == "__main__":
    main()
