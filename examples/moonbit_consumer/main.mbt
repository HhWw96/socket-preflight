///|
fn selected_socket(name : String, text : String) -> @preflight.UnitInput {
  let path = "usr/lib/systemd/system/" + name
  let plan = @preflight.select_files([{ fragment: Some(path), drop_ins: [] }])
  { name, plan, files: [{ path, text }], discovery_issues: [] }
}

///|
fn main {
  let legacy = selected_socket(
    "legacy.socket", "[Socket]\nBindIPv6Only=both\nListenStream=[::]:45391\n",
  )
  let next = selected_socket(
    "next.socket", "[Socket]\nListenStream=127.0.0.1:45391\n",
  )
  let result = @preflight.analyze_rollout({
    baseline: [legacy],
    transition: Some([legacy, next]),
    candidate: [next],
    environment: { ipv6: "enabled", bindv6only: "unknown" },
  })
  guard result.transition is Some(overlap) &&
    result.exit_code == 1 &&
    overlap.new_conflicts.length() == 1 else {
    abort("Expected exactly one new conflict in the transition")
  }
  let conflict = overlap.new_conflicts[0]
  println("transition: \{conflict.first.unit} and \{conflict.second.unit} overlap on TCP \{conflict.port}")
  let baseline : @preflight.StageInput = {
    units: [legacy], environment: { ipv6: "enabled", bindv6only: "1" },
    rebind_all: false,
  }
  let narrowed = selected_socket(
    "legacy.socket", "[Socket]\nBindIPv6Only=ipv6-only\nListenStream=[::]:45391\n",
  )
  let candidate = { ..baseline, units: [narrowed, next], }
  let pending = @preflight.analyze_staged_rollout(baseline, [], candidate, require_clear=true)
  let passed = @preflight.analyze_staged_rollout_with_rebinds(
    baseline, [], candidate, Map([("candidate", ["legacy.socket"])]), require_clear=true,
  )
  guard pending.exit_code == 2 && passed.exit_code == 0 && passed.complete else {
    abort("Expected verified recreation of legacy.socket to allow the candidate")
  }
  println("candidate: selective recreation verified")
}
