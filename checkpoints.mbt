///|
pub let max_checkpoints : Int = 8

///|
pub(all) struct Checkpoint {
  name : String
  units : Array[UnitInput]
}

///|
pub(all) struct CheckpointRolloutInput {
  baseline : Array[UnitInput]
  checkpoints : Array[Checkpoint]
  candidate : Array[UnitInput]
  environment : Environment
}

///|
pub(all) struct StageInput {
  units : Array[UnitInput]
  environment : Environment
  rebind_all : Bool
}

///|
pub(all) struct StagedCheckpoint {
  name : String
  stage : StageInput
}

///|
#derive(@json.ToJson)
pub(all) struct CheckpointResult {
  name : String
  result : Comparison
}

///|
pub extend CheckpointResult with @json.ToJson::{to_json}

///|
#derive(@json.ToJson)
pub(all) struct CheckpointRolloutReport {
  schema_version : Int
  version : String
  status : String
  exit_code : Int
  complete : Bool
  checkpoints : Array[CheckpointResult]
  candidate : Comparison?
  issues : Array[Issue]
}

///|
pub extend CheckpointRolloutReport with @json.ToJson::{to_json}

///|
pub fn supported_checkpoint_name(name : String) -> Bool {
  !name.is_empty() &&
  name.length() <= 32 &&
  name != "baseline" &&
  name != "candidate" &&
  name.iter().next().map_or(false, ascii_alnum) &&
  name.iter().all(char => ascii_alnum(char) || char == '-' || char == '_')
}

///|
fn checkpoint_rollout_error(message : String) -> CheckpointRolloutReport {
  {
    schema_version: 2,
    version,
    status: "input_error",
    exit_code: 3,
    complete: false,
    checkpoints: [],
    candidate: None,
    issues: [input_issue("invalid_plan", message, None)],
  }
}

///|
/// Evaluate each ordered checkpoint and the final candidate against the same
/// baseline. Results at individual checkpoints reuse the candidate's baseline.
pub fn analyze_checkpoint_rollout(input : CheckpointRolloutInput) -> CheckpointRolloutReport {
  analyze_checkpoints(input, false)
}

///|
/// Require every declared checkpoint and the candidate to have zero confirmed
/// active conflicts, including conflicts already present in the baseline.
pub fn analyze_strict_checkpoint_rollout(input : CheckpointRolloutInput) -> CheckpointRolloutReport {
  analyze_checkpoints(input, true)
}

///|
fn analyze_checkpoints(
  input : CheckpointRolloutInput,
  require_clear : Bool,
) -> CheckpointRolloutReport {
  let stage = fn(units : Array[UnitInput]) -> StageInput {
    { units, environment: input.environment, rebind_all: false, }
  }
  let checkpoints = input.checkpoints.map(checkpoint => {
    StagedCheckpoint::{ name: checkpoint.name, stage: stage(checkpoint.units), }
  })
  evaluate_stages(
    stage(input.baseline), checkpoints, stage(input.candidate), require_clear~,
    check_rebinds=false,
  )
}

///|
fn binding_signatures(report : Report) -> Map[String, Array[String]] {
  let signatures : Map[String, Array[String]] = Map([])
  for unit in report.units {
    if unit.issues.is_empty() {
      let bindings = unit.comparable_listeners.map(listener => {
        let mode = listener.ipv6_mode.unwrap_or("")
        "\{listener.family}|\{listener.address}|\{listener.port}|\{mode}"
      })
      bindings.sort_by(compare_ascii_paths)
      signatures[unit.name] = bindings
    }
  }
  signatures
}

///|
fn pending_rebind_units(
  previous_stage : (StageInput, Report),
  current_stage : (StageInput, Report),
  pending : Array[String],
) -> Array[String] {
  let (previous, previous_scan) = previous_stage
  let (current, current_scan) = current_stage
  if current.rebind_all {
    return []
  }
  let environment_changed = previous.environment.ipv6 != current.environment.ipv6 ||
    previous.environment.bindv6only != current.environment.bindv6only
  let before = binding_signatures(previous_scan)
  let after = binding_signatures(current_scan)
  let retained : Array[String] = []
  for unit in current.units {
    if previous.units.any(old => old.name == unit.name) {
      let bindings_changed = match (before.get(unit.name), after.get(unit.name)) {
        (Some(old), Some(new)) => old != new
        _ => true
      }
      if environment_changed || bindings_changed || pending.contains(unit.name) {
        retained.push(unit.name)
      }
    }
  }
  retained.sort_by(compare_ascii_paths)
  retained
}

///|
fn append_stage_input_issues(report : Report, stage : String, issues : Array[Issue]) -> Unit {
  let append = fn(issue : Issue) {
    if issue.severity == "input_error" {
      issues.push({ ..issue, message: "\{stage}: \{issue.message}", })
    }
  }
  report.issues.each(append)
  report.units.each(unit => unit.issues.each(append))
}

///|
/// Compare against the baseline, guarding effective binding and environment
/// changes between adjacent stages when a unit may keep old sockets.
/// Other stages keep their comparison results while binding coverage is incomplete.
pub fn analyze_staged_rollout(
  baseline : StageInput,
  checkpoints : Array[StagedCheckpoint],
  candidate : StageInput,
  require_clear~ : Bool = false,
) -> CheckpointRolloutReport {
  evaluate_stages(baseline, checkpoints, candidate, require_clear~, check_rebinds=true)
}

///|
fn evaluate_stages(
  baseline : StageInput,
  checkpoints : Array[StagedCheckpoint],
  candidate : StageInput,
  require_clear~ : Bool,
  check_rebinds~ : Bool,
) -> CheckpointRolloutReport {
  if checkpoints.length() > max_checkpoints {
    return checkpoint_rollout_error("Choose at most \{max_checkpoints} checkpoints.")
  }
  let names : Map[String, Bool] = Map([])
  for checkpoint in checkpoints {
    if !supported_checkpoint_name(checkpoint.name) || names.contains(checkpoint.name) {
      return checkpoint_rollout_error("Use distinct literal checkpoint names.")
    }
    names[checkpoint.name] = true
  }
  let stages = checkpoints.copy()
  stages.push({ name: "candidate", stage: candidate, })
  if !valid_environment(baseline.environment) ||
    stages.any(checkpoint => !valid_environment(checkpoint.stage.environment)) {
    return checkpoint_rollout_error("Invalid target IPv6 environment.")
  }
  let before = analyze_group(baseline.units, baseline.environment)
  let mut previous = (baseline, before)
  let mut pending : Array[String] = []
  let mut invalid_input = before.exit_code == 3
  let comparisons : Array[CheckpointResult] = []
  let mut candidate_result : Comparison? = None
  let outcomes : Array[Comparison] = []
  let issues : Array[Issue] = []
  let mut coverage_missing = false
  for checkpoint in stages {
    let current = checkpoint.stage
    let scan = analyze_group(current.units, current.environment)
    if check_rebinds {
      let stage = (current, scan)
      pending = pending_rebind_units(previous, stage, pending)
      previous = stage
    }
    if !pending.is_empty() {
      coverage_missing = true
      let names = pending.join(", ")
      issues.push(uncovered(
        "unverified_rebind",
        "\{checkpoint.name}: listener recreation remains unverified for \{names}. Confirm all listeners are recreated with rebind_all.",
        None,
      ))
      if scan.exit_code == 3 {
        invalid_input = true
        append_stage_input_issues(scan, checkpoint.name, issues)
      }
      continue
    }
    let comparison = compare_groups(before, scan, true, allow_environment_changes=true)
    outcomes.push(comparison)
    if checkpoint.name == "candidate" {
      candidate_result = Some(comparison)
    } else {
      comparisons.push({
        name: checkpoint.name,
        result: { ..comparison, baseline: None, },
      })
    }
  }
  if candidate_result is None && before.exit_code == 3 {
    append_stage_input_issues(before, "baseline", issues)
  }
  let (status, exit_code, complete) = rollout_outcome(outcomes)
  let (status, exit_code) = if invalid_input || exit_code == 3 {
    ("input_error", 3)
  } else if require_clear &&
    outcomes.any(result => result.candidate.map_or(false, scan => scan.conflict_count > 0)) {
    ("conflict", 1)
  } else if exit_code == 0 && coverage_missing {
    ("inconclusive", 2)
  } else {
    (status, exit_code)
  }
  {
    schema_version: 2,
    version,
    status,
    exit_code,
    complete: complete && !coverage_missing && exit_code != 3,
    checkpoints: comparisons,
    candidate: candidate_result,
    issues,
  }
}
