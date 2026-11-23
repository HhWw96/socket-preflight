///|
extern "js" fn split_plan_path(file : String) -> FixedArray[String] =
  #| file => {
  #|   const path = process.getBuiltinModule("node:path");
  #|   const absolute = path.resolve(file);
  #|   return [path.dirname(absolute), path.basename(absolute)];
  #| }

///|
extern "js" fn resolve_stage_root(directory : String, root : String) -> String =
  #| (directory, root) => process.getBuiltinModule("node:path").resolve(directory, root)

///|
fn load_plan_stage(
  directory : String,
  label : String,
  json : Json?,
) -> Result[Array[@preflight.UnitInput], String] {
  guard json is Some(Object(fields)) &&
    fields.length() == 2 &&
    fields.get("root") is Some(String(root)) &&
    !root.is_empty() &&
    fields.get("units") is Some(Array(names)) &&
    names.length() <= @preflight.max_units else {
    return Err(
      "\{label} requires root and a list of at most \{@preflight.max_units} units.",
    )
  }
  let units = []
  let seen : Map[String, Bool] = Map([])
  for value in names {
    guard value is String(unit) &&
      @preflight.supported_unit_name(unit) &&
      !seen.contains(unit) else {
      return Err("\{label} units must be distinct literal .socket basenames.")
    }
    seen[unit] = true
    units.push(unit)
  }
  units.sort()
  match load_snapshot(resolve_stage_root(directory, root), units) {
    Ok(inputs) => Ok(inputs)
    Err(message) => Err("\{label}: \{message}")
  }
}

///|
fn read_rollout(file : String) -> Result[@preflight.RolloutInput, String] {
  let location = split_plan_path(file)
  let directory = location[0]
  let content = probe(open_probe(directory), location[1], "read")
  if content[0] != "read" {
    return Err("Cannot read rollout plan (\{content[0]}).")
  }
  let parsed = Some(
    @json.parse(content[1][:], max_nesting_depth=12, reject_duplicate_keys=true),
  ) catch {
    _ => None
  }
  guard parsed is Some(Object(fields)) else {
    return Err("The rollout plan must be a UTF-8 JSON object with unique keys.")
  }
  let transition = fields.get("transition")
  let expected = if transition is Some(_) { 5 } else { 4 }
  guard fields.length() == expected &&
    fields.get("schema_version") is Some(Number(schema, ..)) &&
    schema == 1.0 &&
    fields.get("environment") is Some(Object(environment)) &&
    environment.length() == 2 &&
    environment.get("ipv6") is Some(String(ipv6)) &&
    environment.get("bindv6only") is Some(String(bindv6only)) else {
    return Err(
      "Use schema_version 1, environment, baseline, candidate and optional transition.",
    )
  }
  let baseline = match
    load_plan_stage(directory, "baseline", fields.get("baseline")) {
    Ok(stage) => stage
    Err(message) => return Err(message)
  }
  let candidate = match
    load_plan_stage(directory, "candidate", fields.get("candidate")) {
    Ok(stage) => stage
    Err(message) => return Err(message)
  }
  let transition = match transition {
    None => None
    stage =>
      match load_plan_stage(directory, "transition", stage) {
        Ok(inputs) => Some(inputs)
        Err(message) => return Err(message)
      }
  }
  Ok({ baseline, candidate, transition, environment: { ipv6, bindv6only, }, })
}

///|
fn run_rollout(args : ArrayView[String]) -> Unit {
  let json = args.any(arg => arg == "--json")
  let options = []
  for argument in args {
    if argument != "--json" {
      options.push(argument)
    }
  }
  let required_args = if json { 3 } else { 2 }
  let input = if args.length() == required_args &&
    options.length() == 2 &&
    options[0] == "--plan" &&
    !options[1].is_empty() &&
    !options[1].has_prefix("--") {
    read_rollout(options[1])
  } else {
    Err("Supply --plan <plan.json> and optional --json.")
  }
  let report = match input {
    Ok(plan) => @preflight.analyze_rollout(plan)
    Err(message) => @preflight.rollout_error(message)
  }
  if json {
    println(@json.to_json(report).stringify(indent=2))
  } else {
    println("ROLLOUT \{report.status.to_upper()}")
    for
      (label, result) in [
        ("transition", report.transition),
        ("candidate", report.candidate),
      ] {
      if result is Some(stage) {
        println("[\{label}]")
        show_comparison(stage)
        if stage.candidate is Some(current) {
          show_issues(current, label + " ")
        }
      }
    }
    if report.candidate is Some(result) && result.baseline is Some(baseline) {
      show_issues(baseline, "baseline ")
    }
    report.issues.each(issue => show_issue("rollout", issue))
  }
  exit_status(report.exit_code)
}
