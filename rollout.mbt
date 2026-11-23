///|
/// Each array lists the system socket units intended to listen concurrently in
/// that stage of a single namespace. An empty array declares no active units.
pub(all) struct RolloutInput {
  baseline : Array[UnitInput]
  transition : Array[UnitInput]?
  candidate : Array[UnitInput]
  environment : Environment
}

///|
#derive(@json.ToJson)
pub(all) struct RolloutReport {
  schema_version : Int32
  version : String
  status : String
  exit_code : Int32
  complete : Bool
  transition : Comparison?
  candidate : Comparison?
  issues : Array[Issue]
}

///|
pub extend RolloutReport with @json.ToJson::{to_json}

///|
pub fn rollout_error(message : String) -> RolloutReport {
  {
    schema_version: 1,
    version,
    status: "input_error",
    exit_code: 3,
    complete: false,
    transition: None,
    candidate: None,
    issues: [input_issue("invalid_plan", message, None)],
  }
}

///|
/// Compare the declared transition and candidate groups independently against
/// the baseline. This function consumes selected files and performs no I/O.
pub fn analyze_rollout(input : RolloutInput) -> RolloutReport {
  let { baseline, transition, candidate, environment, } = input
  if !valid_environment(environment) {
    return rollout_error(
      "Expected ipv6=enabled|disabled|unknown and bindv6only=0|1|unknown.",
    )
  }
  let before = analyze_group(baseline, environment)
  let candidate_result = compare_groups(
    before,
    analyze_group(candidate, environment),
    true,
  )
  let overlap = transition.map(stage => {
    compare_groups(before, analyze_group(stage, environment), true)
  })
  let overlap_code = overlap.map_or(0, result => result.exit_code)
  let complete = candidate_result.complete &&
    overlap.map_or(true, result => result.complete)
  let (status, exit_code) = if candidate_result.exit_code == 3 ||
    overlap_code == 3 {
    ("input_error", 3)
  } else if candidate_result.exit_code == 1 || overlap_code == 1 {
    ("regression", 1)
  } else if !complete {
    ("inconclusive", 2)
  } else {
    ("clear", 0)
  }
  {
    schema_version: 1,
    version,
    status,
    exit_code,
    complete,
    transition: overlap,
    candidate: Some(candidate_result),
    issues: [],
  }
}
