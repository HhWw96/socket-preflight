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
}
