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
  budget : Budget,
  roots : Map[String, String],
  expected_fields~ : Int = 2,
) -> Result[Array[@preflight.UnitInput], String] {
  guard json is Some(Object(fields)) &&
    fields.length() == expected_fields &&
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
  let resolved = resolve_stage_root(directory, root)
  roots[label] = resolved
  match load_snapshot(resolved, units, budget~) {
    Ok(inputs) => Ok(inputs)
    Err(message) => Err("\{label}: \{message}")
  }
}

///|
priv enum LoadedRollout {
  Legacy(@preflight.RolloutInput)
  Staged(@preflight.StageInput, Array[@preflight.StagedCheckpoint], @preflight.StageInput, Bool, Map[String, Array[String]])
}

///|
fn plan_environment(value : Json?) -> @preflight.Environment? {
  guard value is Some(Object(env)) &&
    env.length() == 2 &&
    env.get("ipv6") is Some(String(ipv6)) &&
    (ipv6 == "enabled" || ipv6 == "disabled" || ipv6 == "unknown") &&
    env.get("bindv6only") is Some(String(bindv6only)) &&
    (bindv6only == "0" || bindv6only == "1" || bindv6only == "unknown") else {
    return None
  }
  Some({ ipv6, bindv6only, })
}

///|
fn load_staged_plan(
  directory : String,
  label : String,
  json : Json?,
  fallback : @preflight.Environment,
  budget : Budget,
  rebind_units : Map[String, Array[String]],
  roots : Map[String, String],
  named~ : Bool = false,
) -> Result[@preflight.StageInput, String] {
  guard json is Some(Object(fields)) else { return Err("\{label} must be an object.") }
  let mut expected_fields = if named { 3 } else { 2 }
  let environment = match fields.get("environment") {
    None => fallback
    value => {
      expected_fields += 1
      guard plan_environment(value) is Some(environment) else {
        return Err("\{label} environment needs valid ipv6 and bindv6only values.")
      }
      environment
    }
  }
  let rebind_all = match fields.get("rebind_all") {
    None => false
    Some(True) => { expected_fields += 1; true }
    Some(False) => { expected_fields += 1; false }
    _ => return Err("\{label} rebind_all must be a boolean.")
  }
  if fields.get("rebind_units") is Some(value) {
    guard label != "baseline" && value is Array(values) &&
      values.length() <= @preflight.max_units else {
      return Err("\{label}: rebind_units requires a list of .socket names at a checkpoint or candidate.")
    }
    let units = []
    for value in values {
      guard value is String(unit) && @preflight.supported_unit_name(unit) else {
        return Err("\{label}: rebind_units requires literal .socket names.")
      }
      units.push(unit)
    }
    rebind_units[label] = units
    expected_fields += 1
  }
  match load_plan_stage(directory, label, json, budget, roots, expected_fields~) {
    Ok(units) => Ok({ units, environment, rebind_all, })
    Err(message) => Err(message)
  }
}

///|
fn read_rollout(file : String, roots : Map[String, String]) -> Result[LoadedRollout, String] {
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
  let legacy_fields = if transition is Some(_) { 5 } else { 4 }
  let policy = fields.get("policy")
  let named_fields = if policy is Some(_) { 6 } else { 5 }
  guard fields.get("schema_version") is Some(Number(schema, ..)) &&
    plan_environment(fields.get("environment")) is Some(environment) else {
    return Err(
      "Supply schema_version, environment, baseline and candidate.",
    )
  }
  let checkpoints = match (schema, fields.get("checkpoints")) {
    (1.0, None) if fields.length() == legacy_fields && policy is None => None
    (2.0, Some(Array(stages))) if fields.length() == named_fields &&
      transition is None &&
      stages.length() <= @preflight.max_checkpoints => Some(stages)
    _ =>
      return Err(
        "Use schema_version 1 with optional transition, or schema_version 2 with up to \{@preflight.max_checkpoints} checkpoints.",
      )
  }
  let require_clear = match policy {
    None | Some(String("new-conflicts")) => false
    Some(String("require-clear")) => true
    _ => return Err("Policy must be new-conflicts or require-clear.")
  }
  let limit = 4 * 1024 * 1024
  let budget : Budget = { remaining: limit, }
  if checkpoints is Some(checkpoints) {
    let rebind_units : Map[String, Array[String]] = Map([])
    let baseline = match load_staged_plan(directory, "baseline", fields.get("baseline"), environment, budget, rebind_units, roots) {
      Ok(stage) => stage
      Err(message) => return Err(message)
    }
    let candidate = match load_staged_plan(directory, "candidate", fields.get("candidate"), environment, budget, rebind_units, roots) {
      Ok(stage) => stage
      Err(message) => return Err(message)
    }
    let names : Map[String, Bool] = Map([])
    let stages : Array[@preflight.StagedCheckpoint] = []
    for stage in checkpoints {
      guard stage is Object(fields) &&
        fields.get("name") is Some(String(name)) &&
        @preflight.supported_checkpoint_name(name) &&
        !names.contains(name) else {
        return Err("Use distinct literal checkpoint names.")
      }
      names[name] = true
      match load_staged_plan(directory, name, Some(stage), environment, budget, rebind_units, roots, named=true) {
        Ok(stage) => stages.push({ name, stage, })
        Err(message) => return Err(message)
      }
    }
    return Ok(Staged(baseline, stages, candidate, require_clear, rebind_units))
  }
  let baseline = match
    load_plan_stage(directory, "baseline", fields.get("baseline"), budget, roots) {
    Ok(stage) => stage
    Err(message) => return Err(message)
  }
  budget.remaining = limit
  let candidate = match
    load_plan_stage(directory, "candidate", fields.get("candidate"), budget, roots) {
    Ok(stage) => stage
    Err(message) => return Err(message)
  }
  budget.remaining = limit
  let transition = match transition {
    None => None
    stage =>
      match load_plan_stage(directory, "transition", stage, budget, roots) {
        Ok(inputs) => Some(inputs)
        Err(message) => return Err(message)
      }
  }
  Ok(Legacy({ baseline, candidate, transition, environment, }))
}

///|
fn show_rollout_stage(label : String, stage : @preflight.Comparison) -> Unit {
  println("[\{label}]")
  show_comparison(stage)
  if stage.candidate is Some(current) {
    show_issues(current, label + " ")
  }
}

///|
fn emit_checkpoint_rollout(report : @preflight.CheckpointRolloutReport, json : Bool) -> Unit {
  if json {
    println(@json.to_json(report).stringify(indent=2))
  } else {
    println("ROLLOUT \{report.status.to_upper()}")
    for checkpoint in report.checkpoints {
      show_rollout_stage(checkpoint.name, checkpoint.result)
    }
    if report.candidate is Some(candidate) {
      show_rollout_stage("candidate", candidate)
      if candidate.baseline is Some(baseline) {
        show_issues(baseline, "baseline ")
      }
    }
    report.issues.each(issue => show_issue("rollout", issue))
  }
  exit_status(report.exit_code)
}

///|
fn run_rollout(args : ArrayView[String]) -> Unit {
  let json = args.any(arg => arg == "--json")
  let sarif = args.any(arg => arg == "--sarif")
  let options = []
  for argument in args {
    if argument != "--json" && argument != "--sarif" {
      options.push(argument)
    }
  }
  let roots : Map[String, String] = Map([])
  let required_args = if json || sarif { 3 } else { 2 }
  let input = if !(json && sarif) && args.length() == required_args &&
    options.length() == 2 &&
    options[0] == "--plan" &&
    !options[1].is_empty() &&
    !options[1].has_prefix("--") {
    read_rollout(options[1], roots)
  } else {
    Err("Supply --plan <plan.json> and optionally one of --json or --sarif.")
  }
  let report = match input {
    Ok(Legacy(plan)) => @preflight.analyze_rollout(plan)
    Ok(Staged(baseline, stages, candidate, require_clear, rebind_units)) => {
      let result = @preflight.analyze_staged_rollout_with_rebinds(
        baseline, stages, candidate, rebind_units, require_clear~,
      )
      if sarif {
        let stages = result.checkpoints.map(checkpoint => (checkpoint.name, Some(checkpoint.result)))
        stages.push(("candidate", result.candidate))
        emit_rollout_sarif(stages, roots, result.issues, result.exit_code, require_clear~)
      } else {
        emit_checkpoint_rollout(result, json)
      }
      return
    }
    Err(message) => @preflight.rollout_error(message)
  }
  if sarif {
    emit_rollout_sarif(
      [("transition", report.transition), ("candidate", report.candidate)],
      roots, report.issues, report.exit_code,
    )
    return
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
        show_rollout_stage(label, stage)
      }
    }
    if report.candidate is Some(result) && result.baseline is Some(baseline) {
      show_issues(baseline, "baseline ")
    }
    report.issues.each(issue => show_issue("rollout", issue))
  }
  exit_status(report.exit_code)
}
