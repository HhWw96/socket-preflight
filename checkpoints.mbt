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
  if !valid_environment(input.environment) {
    return checkpoint_rollout_error("Invalid target IPv6 environment.")
  }
  if input.checkpoints.length() > max_checkpoints {
    return checkpoint_rollout_error("Choose at most \{max_checkpoints} checkpoints.")
  }
  let names : Map[String, Bool] = Map([])
  for checkpoint in input.checkpoints {
    if !supported_checkpoint_name(checkpoint.name) || names.contains(checkpoint.name) {
      return checkpoint_rollout_error("Use distinct literal checkpoint names.")
    }
    names[checkpoint.name] = true
  }
  let before = analyze_group(input.baseline, input.environment)
  let candidate = compare_groups(before, analyze_group(input.candidate, input.environment), true)
  let checkpoints = input.checkpoints.map(checkpoint => {
    let comparison = compare_groups(
      before,
      analyze_group(checkpoint.units, input.environment),
      true,
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
