///|
extern "js" fn exit_status(code : Int32) -> Unit =
  #| code => { process.exitCode = code; }

///|
priv struct Options {
  snapshots : Snapshots
  units : Array[String]
  environment : @preflight.Environment
  json : Bool
}

///|
priv enum Snapshots {
  Single(String)
  BaselineCandidate(String, String)
}

///|
let usage : String =
  #| SocketPreflight
  #|   socket-preflight inventory --root <snapshot-dir> [--json]
  #|   socket-preflight scan --root <snapshot-dir> [--ipv6 enabled|disabled|unknown]
  #|       [--bindv6only 0|1|unknown] [--json] <first.socket> [...]
  #|   socket-preflight compare --baseline <snapshot-dir> --candidate <snapshot-dir>
  #|       [same environment flags] [--json] <first.socket> [...]
  #|   socket-preflight rollout --plan <plan.json> [--json]
  #|   socket-preflight --version
  #| 
  #| Name each system socket unit in one planned concurrent, same-namespace group.
  #| Environment options describe the target kernel, not this machine.
  #| Scan exit codes: 0 clear in scope, 1 conflict, 2 inconclusive, 3 input error.
  #| Compare uses 1 for confirmed new conflicts and 0 for complete coverage with no new conflicts.

///|
fn parse_options(args : ArrayView[String]) -> Result[Options, String] {
  if args[0] != "scan" && args[0] != "compare" {
    return Err("Expected scan or compare. Run --help for usage.")
  }
  let comparing = args[0] == "compare"
  let options : Map[String, String] = Map([])
  let units : Array[String] = []
  let selected : Map[String, Bool] = Map([])
  let mut json = false
  let mut at = 1
  while at < args.length() {
    let text = args[at]
    at += 1
    match text {
      "--json" if !json => json = true
      "--root" | "--baseline" | "--candidate" | "--ipv6" | "--bindv6only" => {
        if at == args.length() ||
          args[at].is_empty() ||
          args[at].has_prefix("--") ||
          options.get(text) is Some(_) {
          return Err("Supply exactly one value for \{text}.")
        }
        options[text] = args[at]
        at += 1
      }
      _ if @preflight.supported_unit_name(text) && selected.get(text) is None => {
        units.push(text)
        selected[text] = true
      }
      _ =>
        return Err(
          "Unknown option, unsupported unit name, or duplicate unit. Run --help for usage.",
        )
    }
  }
  let snapshots = match
    (
      comparing,
      options.get("--root"),
      options.get("--baseline"),
      options.get("--candidate"),
    ) {
    (false, Some(root), None, None) => Single(root)
    (true, None, Some(baseline), Some(candidate)) =>
      BaselineCandidate(baseline, candidate)
    (true, _, _, _) =>
      return Err("Supply --baseline and --candidate for compare.")
    _ =>
      return Err("Supply --root with an exported snapshot directory for scan.")
  }
  if units.is_empty() || units.length() > @preflight.max_units {
    return Err("Supply 1–\{@preflight.max_units} distinct .socket units.")
  }
  units.sort()
  let ipv6 = options.get("--ipv6").unwrap_or("unknown")
  let bindv6only = options.get("--bindv6only").unwrap_or("unknown")
  if ipv6 != "enabled" && ipv6 != "disabled" && ipv6 != "unknown" {
    return Err("--ipv6 accepts enabled, disabled, or unknown.")
  }
  if bindv6only != "0" && bindv6only != "1" && bindv6only != "unknown" {
    return Err("--bindv6only accepts 0, 1, or unknown.")
  }
  Ok({ snapshots, units, environment: { ipv6, bindv6only, }, json, })
}

///|
fn location(source : @preflight.Source) -> String {
  match (source.line, source.end_line) {
    (Some(a), Some(b)) if b > a => "\{source.path}:\{a}-\{b}"
    (Some(a), _) => "\{source.path}:\{a}"
    _ => source.path
  }
}

///|
fn show_listener(listener : @preflight.Listener) -> Unit {
  println(
    "  \{listener.unit} \{listener.endpoint} from \{location(listener.setting.source)}",
  )
  if listener.ipv6_mode is Some(mode) {
    let why = if listener.ipv6_mode_source is Some(source) {
      location(source)
    } else {
      "target default"
    }
    println("    IPv6 mode \{mode} (\{why})")
  }
}

///|
fn show_issue(label : String, issue : @preflight.Issue) -> Unit {
  let at = issue.source.map_or("", source => " at " + location(source))
  println("\{label} [\{issue.code}]\{at} \{issue.message}")
}

///|
fn show_conflict(conflict : @preflight.Conflict) -> Unit {
  let reason = match conflict.code {
    "dual_stack_overlap" =>
      "IPv6 wildcard in dual-stack mode also occupies the IPv4 port"
    "ipv4_mapped_overlap" =>
      "IPv4-mapped IPv6 address overlaps the IPv4 listener"
    "wildcard_overlap" => "wildcard covers the other address in the same family"
    _ => "same canonical address"
  }
  let scope = if conflict.first.unit == conflict.second.unit {
    " (within \{conflict.first.unit})"
  } else {
    ""
  }
  println("TCP \{conflict.port}\{scope}: \{reason}")
  show_listener(conflict.first)
  show_listener(conflict.second)
}

///|
fn show_issues(report : @preflight.Report, prefix : String) -> Unit {
  if report.conflicts_omitted > 0 {
    println(
      "\{prefix}\{report.conflicts_omitted} additional conflict(s) omitted from details.",
    )
  }
  for unit in report.units {
    for issue in unit.issues {
      show_issue(prefix + unit.name, issue)
    }
  }
  for issue in report.issues {
    show_issue(prefix + "input", issue)
  }
}

///|
fn emit(report : @preflight.Report, json : Bool) -> Unit {
  if json {
    println(@json.to_json(report).stringify(indent=2))
  } else {
    println(
      "\{report.status.to_upper()} | \{report.conflict_count} conflict(s) in \{report.units.length()} selected unit(s)",
    )
    report.conflicts.each(show_conflict)
    for unit in report.units {
      for reset in unit.resets {
        println(
          "\{unit.name} reset \{reset.cleared.length()} Listen assignment(s) at \{location(reset.source)}",
        )
      }
    }
    show_issues(report, "")
  }
  exit_status(report.exit_code)
}

///|
fn emit_comparison(report : @preflight.Comparison, json : Bool) -> Unit {
  if json {
    println(@json.to_json(report).stringify(indent=2))
  } else {
    show_comparison(report)
    if report.baseline is Some(baseline) {
      show_issues(baseline, "Baseline ")
    }
    if report.candidate is Some(candidate) {
      show_issues(candidate, "Candidate ")
    }
    for issue in report.issues {
      show_issue("input", issue)
    }
  }
  exit_status(report.exit_code)
}

///|
fn show_comparison(report : @preflight.Comparison) -> Unit {
  println(
    "\{report.status.to_upper()} | \{report.new_conflicts.length()} new, \{report.existing_conflicts.length()} existing, \{report.resolved_conflicts.length()} resolved",
  )
  for
    (label, findings) in [
      ("New", report.new_conflicts),
      ("Existing", report.existing_conflicts),
      ("Resolved", report.resolved_conflicts),
      ("Could not confirm as new", report.uncertain_new),
      ("Could not confirm as resolved", report.uncertain_resolved),
    ] {
    if !findings.is_empty() {
      println(label)
      findings.each(show_conflict)
    }
  }
}

///|
fn scan_snapshot(
  root : String,
  units : Array[String],
  environment : @preflight.Environment,
) -> @preflight.Report {
  match load_snapshot(root, units) {
    Ok(inputs) => @preflight.analyze(inputs, environment)
    Err(message) => @preflight.command_error(message)
  }
}

///|
fn load_snapshot(
  root : String,
  units : Array[String],
  budget~ : Budget = { remaining: 4 * 1024 * 1024, },
) -> Result[Array[@preflight.UnitInput], String] {
  let snapshot = open_probe(root)
  let root_type = probe(snapshot, "", "kind")[0]
  if root_type != "directory" {
    return Err("Snapshot root must be a readable directory (\{root_type}).")
  }
  Ok(units.map(name => load_unit(snapshot, name, budget)))
}

///|
fn main {
  let argv = @env.args()
  let args = argv[2:]
  if args.is_empty() ||
    args == ["--help"] ||
    args == ["-h"] ||
    args == ["inventory", "--help"] ||
    args == ["scan", "--help"] ||
    args == ["compare", "--help"] ||
    args == ["rollout", "--help"] {
    println(usage)
    return
  }
  if args == ["--version"] {
    println("SocketPreflight v\{@preflight.version}")
    return
  }
  if args[0] == "rollout" {
    run_rollout(args[1:])
    return
  }
  if args[0] == "inventory" {
    run_inventory(args[1:])
    return
  }
  match parse_options(args) {
    Err(message) => {
      let json = args.any(arg => arg == "--json")
      if args[0] == "compare" {
        emit_comparison(@preflight.comparison_error(message), json)
      } else {
        emit(@preflight.command_error(message), json)
      }
    }
    Ok(options) => {
      let { snapshots, units, environment, json, } = options
      match snapshots {
        Single(root) => emit(scan_snapshot(root, units, environment), json)
        BaselineCandidate(baseline, candidate) =>
          emit_comparison(
            @preflight.compare_reports(
              scan_snapshot(baseline, units, environment),
              scan_snapshot(candidate, units, environment),
            ),
            json,
          )
      }
    }
  }
}
