///|
#derive(@json.ToJson)
pub(all) struct Comparison {
  schema_version : Int32
  version : String
  status : String
  exit_code : Int32
  complete : Bool
  baseline : Report?
  candidate : Report?
  issues : Array[Issue]
  new_conflicts : Array[Conflict]
  existing_conflicts : Array[Conflict]
  resolved_conflicts : Array[Conflict]
  uncertain_new : Array[Conflict]
  uncertain_resolved : Array[Conflict]
}

///|
pub extend Comparison with @json.ToJson::{to_json}

///|
pub fn comparison_error(message : String) -> Comparison {
  {
    schema_version: 1,
    version,
    status: "input_error",
    exit_code: 3,
    complete: false,
    baseline: None,
    candidate: None,
    issues: [input_issue("invalid_command", message, None)],
    new_conflicts: [],
    existing_conflicts: [],
    resolved_conflicts: [],
    uncertain_new: [],
    uncertain_resolved: [],
  }
}

///|
fn listener_identity(listener : Listener) -> String {
  let endpoint = listener.mapped_ipv4.map_or(listener.endpoint, ip => {
    "\{ip}:\{listener.port}"
  })
  listener.unit + "=" + endpoint
}

///|
fn conflict_identity(conflict : Conflict) -> String {
  let first = listener_identity(conflict.first)
  let second = listener_identity(conflict.second)
  if compare_ascii_paths(first, second) < 0 {
    first + "|" + second
  } else {
    second + "|" + first
  }
}

///|
fn conflict_index(report : Report) -> Map[String, Conflict] {
  let index : Map[String, Conflict] = Map([])
  for conflict in report.conflicts {
    let key = conflict_identity(conflict)
    if !index.contains(key) {
      index[key] = conflict
    }
  }
  index
}

///|
fn covered_units(report : Report) -> Map[String, Bool] {
  let covered : Map[String, Bool] = Map([])
  if report.issues.is_empty() && report.conflicts_omitted == 0 {
    for unit in report.units {
      if unit.issues.is_empty() {
        covered[unit.name] = true
      }
    }
  }
  covered
}

///|
fn compatible_scopes(
  baseline : Report,
  candidate : Report,
  allow_group_changes : Bool,
  allow_environment_changes : Bool,
) -> Bool {
  match (baseline.environment, candidate.environment) {
    (Some(a), Some(b)) =>
      (allow_environment_changes || (a.ipv6 == b.ipv6 && a.bindv6only == b.bindv6only)) &&
      (
        allow_group_changes ||
        baseline.units.map(u => u.name) == candidate.units.map(u => u.name)
      )
    _ => false
  }
}

///|
/// Compare two reports produced by analyze for the same units and target.
/// An absent finding is evidence only if both involved units were covered.
pub fn compare_reports(baseline : Report, candidate : Report) -> Comparison {
  compare_groups(baseline, candidate, false)
}

///|
/// Compare the same socket units across an explicitly planned target environment
/// change. Uncovered bindings still prevent confirming absence.
pub fn compare_target_reports(baseline : Report, candidate : Report) -> Comparison {
  compare_groups(baseline, candidate, false, allow_environment_changes=true)
}

///|
fn pair_absent(
  conflict : Conflict,
  covered : Map[String, Bool],
  report : Report,
  allow_group_changes : Bool,
) -> Bool {
  let first = conflict.first.unit
  let second = conflict.second.unit
  (covered.contains(first) && covered.contains(second)) ||
  (
    allow_group_changes &&
    report.exit_code != 3 &&
    (
      !report.units.any(unit => unit.name == first) ||
      !report.units.any(unit => unit.name == second)
    )
  )
}

///|
fn compare_groups(
  baseline : Report,
  candidate : Report,
  allow_group_changes : Bool,
  allow_environment_changes~ : Bool = false,
) -> Comparison {
  let before = conflict_index(baseline)
  let after = conflict_index(candidate)
  let before_covered = covered_units(baseline)
  let after_covered = covered_units(candidate)
  let compatible = compatible_scopes(
    baseline, candidate, allow_group_changes, allow_environment_changes,
  )
  let new_conflicts : Array[Conflict] = []
  let existing_conflicts : Array[Conflict] = []
  let resolved_conflicts : Array[Conflict] = []
  let uncertain_new : Array[Conflict] = []
  let uncertain_resolved : Array[Conflict] = []
  let after_keys = after.keys().collect()
  after_keys.sort_by(compare_ascii_paths)
  for key in after_keys {
    let conflict = after[key]
    if compatible && before.contains(key) {
      existing_conflicts.push(conflict)
    } else if compatible &&
      pair_absent(conflict, before_covered, baseline, allow_group_changes) {
      new_conflicts.push(conflict)
    } else {
      uncertain_new.push(conflict)
    }
  }
  let before_keys = before.keys().collect()
  before_keys.sort_by(compare_ascii_paths)
  for key in before_keys {
    let conflict = before[key]
    if compatible && after.contains(key) {
      continue
    }
    if compatible &&
      pair_absent(conflict, after_covered, candidate, allow_group_changes) {
      resolved_conflicts.push(conflict)
    } else {
      uncertain_resolved.push(conflict)
    }
  }
  let invalid_scan = baseline.exit_code == 3 || candidate.exit_code == 3
  let issues = if !invalid_scan && !compatible {
    [
      input_issue(
        "different_scopes",
        if allow_environment_changes {
          "Use the same unit names and an explicit environment for each scan."
        } else {
          "Use the same unit names and target environment for both scans."
        },
        None,
      ),
    ]
  } else {
    []
  }
  let input_error = invalid_scan || !compatible
  let complete = !input_error &&
    before_covered.length() == baseline.units.length() &&
    after_covered.length() == candidate.units.length()
  let (status, exit_code) = if input_error {
    ("input_error", 3)
  } else if !new_conflicts.is_empty() {
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
    baseline: Some(baseline),
    candidate: Some(candidate),
    issues,
    new_conflicts,
    existing_conflicts,
    resolved_conflicts,
    uncertain_new,
    uncertain_resolved,
  }
}
