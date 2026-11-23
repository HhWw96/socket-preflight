///|
pub let version : String = "0.0.9"

///|
pub let max_units : Int32 = 64

///|
let max_listeners : Int32 = 512

///|
let max_reported_conflicts : Int32 = 256

///|
/// Paths are relative to the snapshot root. Line numbers are one-based.
/// File-level diagnostics leave both line numbers absent.
#derive(@json.ToJson)
pub(all) struct Source {
  path : String
  line : Int32?
  end_line : Int32?
}

///|
#derive(@json.ToJson)
pub(all) struct Issue {
  severity : String
  code : String
  message : String
  source : Source?
}

///|
pub(all) struct FileText {
  path : String
  text : String
}

///|
pub(all) struct DropIn {
  basename : String
  path : String
}

///|
pub(all) struct Layer {
  fragment : String?
  drop_ins : Array[DropIn]
}

///|
#derive(@json.ToJson)
pub(all) struct Shadow {
  path : String
  winner : String
}

///|
#derive(@json.ToJson)
pub(all) struct FilePlan {
  fragment : String?
  drop_ins : Array[String]
  shadowed : Array[Shadow]
}

///|
/// Environment supplied by the operator for the target manager and namespace.
#derive(@json.ToJson)
pub(all) struct Environment {
  ipv6 : String
  bindv6only : String
}

///|
/// `files` contains the selected main fragment and drop-ins in plan order.
/// Discovery issues describe missing or unsupported inputs found by the caller.
pub(all) struct UnitInput {
  name : String
  plan : FilePlan
  files : Array[FileText]
  discovery_issues : Array[Issue]
}

///|
#derive(@json.ToJson)
pub(all) struct ListenSetting {
  key : String
  value : String
  source : Source
}

///|
#derive(@json.ToJson)
pub(all) struct ListenReset {
  key : String
  source : Source
  cleared : Array[ListenSetting]
}

///|
#derive(@json.ToJson)
pub(all) struct Listener {
  unit : String
  endpoint : String
  address : String
  port : Int32
  family : String
  mapped_ipv4 : String?
  setting : ListenSetting
  ipv6_mode : String?
  ipv6_mode_source : Source?
}

///|
#derive(@json.ToJson)
pub(all) struct UnitReport {
  name : String
  plan : FilePlan
  active_listen : Array[ListenSetting]
  resets : Array[ListenReset]
  comparable_listeners : Array[Listener]
  issues : Array[Issue]
}

///|
#derive(@json.ToJson)
pub(all) struct Conflict {
  code : String
  port : Int32
  first : Listener
  second : Listener
}

///|
#derive(@json.ToJson)
pub(all) struct Report {
  schema_version : Int32
  version : String
  status : String
  exit_code : Int32
  environment : Environment?
  units : Array[UnitReport]
  conflicts : Array[Conflict]
  conflict_count : Int32
  conflicts_omitted : Int32
  issues : Array[Issue]
}

///|
pub extend Source with @json.ToJson::{to_json}

///|
pub extend Issue with @json.ToJson::{to_json}

///|
pub extend Shadow with @json.ToJson::{to_json}

///|
pub extend FilePlan with @json.ToJson::{to_json}

///|
pub extend Environment with @json.ToJson::{to_json}

///|
pub extend ListenSetting with @json.ToJson::{to_json}

///|
pub extend ListenReset with @json.ToJson::{to_json}

///|
pub extend Listener with @json.ToJson::{to_json}

///|
pub extend UnitReport with @json.ToJson::{to_json}

///|
pub extend Conflict with @json.ToJson::{to_json}

///|
pub extend Report with @json.ToJson::{to_json}

///|
pub fn file_source(path : String) -> Source {
  { path, line: None, end_line: None, }
}

///|
pub fn input_issue(code : String, message : String, source : Source?) -> Issue {
  { severity: "input_error", code, message, source, }
}

///|
pub fn uncovered(code : String, message : String, source : Source?) -> Issue {
  { severity: "uncovered", code, message, source, }
}

///|
pub fn command_error(message : String) -> Report {
  make_report(None, [], [], 0, [input_issue("invalid_command", message, None)])
}

///|
fn make_report(
  environment : Environment?,
  units : Array[UnitReport],
  conflicts : Array[Conflict],
  conflict_count : Int32,
  issues : Array[Issue],
) -> Report {
  let input_error = issues.any(i => i.severity == "input_error") ||
    units.any(u => u.issues.any(i => i.severity == "input_error"))
  let incomplete = !issues.is_empty() || units.any(u => !u.issues.is_empty())
  let (status, exit_code) = if input_error {
    ("input_error", 3)
  } else if conflict_count > 0 {
    ("conflict", 1)
  } else if incomplete {
    ("inconclusive", 2)
  } else {
    ("clear", 0)
  }
  {
    schema_version: 1,
    version,
    status,
    exit_code,
    environment,
    units,
    conflicts,
    conflict_count,
    conflicts_omitted: conflict_count - conflicts.length(),
    issues,
  }
}
