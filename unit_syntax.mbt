///|
priv struct LogicalLine {
  value : String
  source : Source
}

///|
priv struct Assignment {
  section : String
  key : String
  value : String
  source : Source
}

///|
let config_space : String = " \t\r\n\u{b}\u{c}"

///|
fn trailing_escape(value : String) -> Bool {
  let mut escaped = false
  for i = value.length() - 1; i >= 0; i = i - 1 {
    if value[i] != '\\' {
      break
    }
    escaped = !escaped
  }
  escaped
}

///|
fn logical_lines(file : FileText) -> Array[LogicalLine] {
  let result : Array[LogicalLine] = []
  let parts : Array[String] = []
  let mut start = 1
  let mut end = 0
  let mut number = 0
  // split() also produces a synthetic final empty record after a trailing LF.
  let physical = file.text.split("\n").collect()
  for i, text in physical {
    if i + 1 == physical.length() && text.is_empty() {
      break
    }
    number += 1
    let crless = match text.strip_suffix("\r") {
      Some(rest) => rest
      None => text
    }
    let raw = if number == 1 {
      match crless.strip_prefix("\u{feff}") {
        Some(rest) => rest
        None => crless
      }
    } else {
      crless
    }
    let trimmed = raw.trim_start(chars=config_space)
    if trimmed.has_prefix("#") || trimmed.has_prefix(";") {
      continue
    }
    if parts.is_empty() {
      start = number
    }
    end = number
    let value = raw.to_owned()
    if trailing_escape(value) {
      parts.push(value[:value.length() - 1].to_owned())
      continue
    }
    parts.push(value)
    result.push({
      value: parts.join(" "),
      source: { path: file.path, line: Some(start), end_line: Some(end), },
    })
    parts.clear()
  }
  if !parts.is_empty() {
    // systemd parses a pending continuation at EOF as a final logical line.
    result.push({
      value: parts.join(" "),
      source: { path: file.path, line: Some(start), end_line: Some(end), },
    })
  }
  result
}

///|
fn parse_assignments(file : FileText) -> (Array[Assignment], Array[Issue]) {
  let assignments : Array[Assignment] = []
  let issues : Array[Issue] = []
  let mut number = 1
  for i in 0..<file.text.length() {
    let charcode = file.text[i]
    if charcode == '\u{0}' ||
      (
        charcode == '\r' &&
        (i + 1 == file.text.length() || file.text[i + 1] != '\n')
      ) {
      issues.push(
        input_issue(
          "invalid_line",
          "NUL and bare CR are outside the supported line format.",
          Some({ path: file.path, line: Some(number), end_line: Some(number), }),
        ),
      )
      return (assignments, issues)
    }
    if charcode == '\n' {
      number += 1
    }
  }
  let mut section = ""
  for line in logical_lines(file) {
    let value = line.value.trim(chars=config_space).to_owned()
    if value.is_empty() {
      continue
    }
    if value.has_prefix("[") {
      match value.strip_prefix("[").bind(left => left.strip_suffix("]")) {
        Some(name) if !name.is_empty() &&
          !name.contains("[") &&
          !name.contains("]") => section = name.to_owned()
        _ =>
          issues.push(
            input_issue(
              "invalid_section",
              "Malformed section header.",
              Some(line.source),
            ),
          )
      }
      continue
    }
    match value.split_once("=") {
      Some((key, rhs)) if !section.is_empty() &&
        !key.trim(chars=config_space).is_empty() => {
        let key = key.trim(chars=config_space).to_owned()
        if section == "Socket" ||
          (section == "Unit" && key == "JoinsNamespaceOf") {
          assignments.push({
            section,
            key,
            value: rhs.trim(chars=config_space).to_owned(),
            source: line.source,
          })
        }
      }
      _ =>
        issues.push(
          input_issue(
            "invalid_assignment",
            "Expected an assignment inside a section.",
            Some(line.source),
          ),
        )
    }
  }
  (assignments, issues)
}

///|
fn systemd_bool(value : String) -> Bool? {
  match value.to_lower() {
    "1" | "yes" | "y" | "true" | "t" | "on" => Some(true)
    "0" | "no" | "n" | "false" | "f" | "off" => Some(false)
    _ => None
  }
}

///|
fn v6_mode(value : String) -> String? {
  match value {
    "both" | "ipv6-only" | "default" => Some(value)
    _ =>
      systemd_bool(value).map(enabled => {
        if enabled {
          "ipv6-only"
        } else {
          "both"
        }
      })
  }
}

///|
fn listen_key(key : String) -> Bool {
  match key {
    "ListenStream"
    | "ListenDatagram"
    | "ListenSequentialPacket"
    | "ListenFIFO"
    | "ListenNetlink"
    | "ListenSpecial"
    | "ListenMessageQueue"
    | "ListenUSBFunction" => true
    _ => false
  }
}
