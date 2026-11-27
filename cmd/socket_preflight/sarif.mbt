///|
extern "js" fn sarif_uri(root : String, source : String) -> FixedArray[String] =
  #| (root, source) => {
  #|   const path = process.getBuiltinModule("node:path");
  #|   const url = process.getBuiltinModule("node:url");
  #|   const full = path.resolve(root, source);
  #|   const relative = path.relative(process.cwd(), full);
  #|   if (path.isAbsolute(relative)) return [url.pathToFileURL(full).href, ""];
  #|   return [relative.split(path.sep).map(encodeURIComponent).join("/"), "%SRCROOT%"];
  #| }

///|
extern "js" fn sarif_workdir_uri() -> String =
  #| () => {
  #|   const path = process.getBuiltinModule("node:path");
  #|   const url = process.getBuiltinModule("node:url");
  #|   return url.pathToFileURL(process.cwd() + path.sep).href;
  #| }

///|
fn sarif_message(text : String) -> Json {
  @json.to_json(Map([("text", @json.to_json(text))]))
}

///|
fn sarif_location(root : String, source : @preflight.Source, related~ : Bool = false) -> Json {
  let uri = sarif_uri(root, source.path)
  let artifact : Map[String, Json] = Map([("uri", @json.to_json(uri[0]))])
  if !uri[1].is_empty() {
    artifact["uriBaseId"] = @json.to_json(uri[1])
  }
  let physical : Map[String, Json] = Map([("artifactLocation", @json.to_json(artifact))])
  if source.line is Some(line) {
    let region = Map([("startLine", @json.to_json(line))])
    if source.end_line is Some(end) && end > line {
      region["endLine"] = @json.to_json(end)
    }
    physical["region"] = @json.to_json(region)
  }
  let location : Map[String, Json] = Map([("physicalLocation", @json.to_json(physical))])
  if related {
    location["id"] = @json.to_json(1)
  }
  @json.to_json(location)
}

///|
fn sarif_conflict(
  root : String,
  conflict : @preflight.Conflict,
  classification : String,
  level : String,
) -> Json {
  let { first, second, port, code, } = conflict
  let explanation = "\{classification}: \{first.unit} \{first.endpoint} overlaps \{second.unit} \{second.endpoint} (TCP \{port}, \{code})."
  @json.to_json(
    Map([
      ("ruleId", @json.to_json("TCP_BIND_OVERLAP")),
      ("level", @json.to_json(level)),
      ("message", sarif_message(explanation)),
      ("locations", @json.to_json([sarif_location(root, first.setting.source)])),
      ("relatedLocations", @json.to_json([sarif_location(root, second.setting.source, related=true)])),
    ]),
  )
}

///|
fn sarif_issue(root : String, label : String, issue : @preflight.Issue) -> Json {
  let (rule, level) = if issue.severity == "input_error" {
    ("INVALID_INPUT", "error")
  } else {
    ("INCOMPLETE_INPUT", "warning")
  }
  let fields : Map[String, Json] = Map([
    ("ruleId", @json.to_json(rule)),
    ("level", @json.to_json(level)),
    ("message", sarif_message("\{label} [\{issue.code}] \{issue.message}")),
  ])
  if issue.source is Some(source) {
    fields["locations"] = @json.to_json([sarif_location(root, source)])
  }
  @json.to_json(fields)
}

///|
fn append_sarif_issues(
  results : Array[Json],
  report : @preflight.Report,
  root : String,
  label : String,
) -> Unit {
  for unit in report.units {
    for issue in unit.issues {
      results.push(sarif_issue(root, label + " " + unit.name, issue))
    }
  }
  for issue in report.issues {
    results.push(sarif_issue(root, label, issue))
  }
  if report.conflicts_omitted > 0 {
    results.push(sarif_issue(
      root, label,
      @preflight.uncovered(
        "omitted_conflicts",
        "\{report.conflicts_omitted} additional conflict locations were omitted.",
        None,
      ),
    ))
  }
}

///|
fn emit_sarif(results : Array[Json], code : Int) -> Unit {
  let rules = [
    ("TCP_BIND_OVERLAP", "Overlapping TCP socket listeners"),
    ("INCOMPLETE_INPUT", "Incomplete socket snapshot coverage"),
    ("INVALID_INPUT", "Invalid socket snapshot or command"),
  ].map(rule => {
    let (id, title) = rule
    @json.to_json(Map([("id", @json.to_json(id)), ("shortDescription", sarif_message(title))]))
  })
  let driver : Json = @json.to_json(Map([
    ("name", @json.to_json("SocketPreflight")),
    ("semanticVersion", @json.to_json(@preflight.version)),
    ("rules", @json.to_json(rules)),
  ]))
  let base = @json.to_json(Map([("uri", @json.to_json(sarif_workdir_uri()))]))
  let run : Json = @json.to_json(Map([
    ("tool", @json.to_json(Map([("driver", driver)]))),
    ("originalUriBaseIds", @json.to_json(Map([("%SRCROOT%", base)]))),
    ("invocations", @json.to_json([
      @json.to_json(Map([("executionSuccessful", @json.to_json(code != 3))])),
    ])),
    ("results", @json.to_json(results)),
  ]))
  let log : Json = @json.to_json(Map([
    ("$schema", @json.to_json("https://json.schemastore.org/sarif-2.1.0.json")),
    ("version", @json.to_json("2.1.0")),
    ("runs", @json.to_json([run])),
  ]))
  println(log.stringify(indent=2))
  exit_status(code)
}

///|
fn emit_scan_sarif(report : @preflight.Report, root : String) -> Unit {
  let results = report.conflicts.map(conflict => sarif_conflict(root, conflict, "Conflict", "error"))
  append_sarif_issues(results, report, root, "scan")
  emit_sarif(results, report.exit_code)
}

///|
fn emit_comparison_sarif(
  report : @preflight.Comparison,
  baseline : String,
  candidate : String,
) -> Unit {
  let results = report.new_conflicts.map(conflict => sarif_conflict(candidate, conflict, "New conflict", "error"))
  for conflict in report.uncertain_new {
    results.push(sarif_conflict(candidate, conflict, "Unconfirmed change", "warning"))
  }
  if report.baseline is Some(before) {
    append_sarif_issues(results, before, baseline, "baseline")
  }
  if report.candidate is Some(after) {
    append_sarif_issues(results, after, candidate, "candidate")
  }
  for issue in report.issues {
    results.push(sarif_issue(candidate, "compare", issue))
  }
  emit_sarif(results, report.exit_code)
}

///|
fn emit_rollout_sarif(
  stages : Array[(String, @preflight.Comparison?)],
  roots : Map[String, String],
  issues : Array[@preflight.Issue],
  exit_code : Int,
  require_clear~ : Bool = false,
) -> Unit {
  let results : Array[Json] = []
  let mut baseline_emitted = false
  for item in stages {
    let (label, stage) = item
    if stage is Some(result) && roots.get(label) is Some(root) {
      if !baseline_emitted && result.baseline is Some(before) &&
        roots.get("baseline") is Some(baseline) {
        append_sarif_issues(results, before, baseline, "baseline")
        baseline_emitted = true
      }
      if result.candidate is Some(after) {
        append_sarif_issues(results, after, root, label)
        let conflicts = if require_clear { after.conflicts } else { result.new_conflicts }
        for conflict in conflicts {
          results.push(sarif_conflict(root, conflict, label + ": Conflict", "error"))
        }
      }
      if !require_clear {
        for conflict in result.uncertain_new {
          results.push(sarif_conflict(root, conflict, label + ": Unconfirmed change", "warning"))
        }
      }
      for issue in result.issues {
        results.push(sarif_issue(root, label, issue))
      }
    }
  }
  for issue in issues {
    // A flattened rollout issue may refer to different snapshot roots.
    results.push(sarif_issue(".", "rollout", { ..issue, source: None, }))
  }
  emit_sarif(results, exit_code)
}
