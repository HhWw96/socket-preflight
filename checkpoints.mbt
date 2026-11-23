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
  analyze_staged_rollout(stage(input.baseline), checkpoints, stage(input.candidate), require_clear~)
}

///|
/// Compare against the baseline, guarding environment changes between adjacent
/// stages when a unit may keep sockets created under the old environment.
pub fn analyze_staged_rollout(
  baseline : StageInput,
  checkpoints : Array[StagedCheckpoint],
  candidate : StageInput,
  require_clear~ : Bool = false,
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
  let mut previous = baseline
  let issues = []
  for checkpoint in stages {
    let current = checkpoint.stage
    let previous_env = previous.environment
    let env = current.environment
    if (previous_env.ipv6 != env.ipv6 || previous_env.bindv6only != env.bindv6only) &&
      !current.rebind_all &&
      current.units.any(unit => previous.units.any(old => old.name == unit.name)) {
      issues.push(uncovered(
        "unverified_rebind",
        "\{checkpoint.name}: target environment changed with shared units. Confirm all listeners are recreated with rebind_all.",
        None,
      ))
    }
    previous = current
  }
  if !issues.is_empty() {
    return {
      schema_version: 2, version, status: "inconclusive", exit_code: 2,
      complete: false, checkpoints: [], candidate: None, issues,
    }
  }
  let before = analyze_group(baseline.units, baseline.environment)
  let candidate = compare_groups(before, analyze_group(candidate.units, candidate.environment),
                                 true, allow_environment_changes=true)
  let checkpoints = checkpoints.map(checkpoint => {
    let comparison = compare_groups(
      before,
      analyze_group(checkpoint.stage.units, checkpoint.stage.environment),
      true,
      allow_environment_changes=true,
    )
    { name: checkpoint.name, result: { ..comparison, baseline: None, }, }
  })
  let outcomes = checkpoints.map(checkpoint => checkpoint.result)
  outcomes.push(candidate)
  let (status, exit_code, complete) = rollout_outcome(outcomes)
  let (status, exit_code) = if require_clear &&
    exit_code != 3 &&
    outcomes.any(result => result.candidate.map_or(false, scan => scan.conflict_count > 0)) {
    ("conflict", 1)
  } else {
    (status, exit_code)
  }
  {
    schema_version: 2,
    version,
    status,
    exit_code,
    complete,
    checkpoints,
    candidate: Some(candidate),
    issues: [],
  }
}
