///|
fn valid_environment(env : Environment) -> Bool {
  (env.ipv6 == "enabled" || env.ipv6 == "disabled" || env.ipv6 == "unknown") &&
  (
    env.bindv6only == "0" ||
    env.bindv6only == "1" ||
    env.bindv6only == "unknown"
  )
}

///|
fn expected_paths(plan : FilePlan) -> Array[String] {
  let paths = []
  if plan.fragment is Some(path) {
    paths.push(path)
  }
  for path in plan.drop_ins {
    paths.push(path)
  }
  paths
}

///|
fn blank_unit(input : UnitInput, issues : Array[Issue]) -> UnitReport {
  {
    name: input.name,
    plan: input.plan,
    active_listen: [],
    resets: [],
    comparable_listeners: [],
    issues,
  }
}

///|
fn normalized_listener(
  unit : String,
  setting : ListenSetting,
  binding : HostPort,
  mode : Assignment?,
  env : Environment,
) -> Listener {
  let base : Listener = {
    unit,
    endpoint: "\{binding.host}:\{binding.port}",
    address: binding.host,
    port: binding.port,
    family: binding.family,
    mapped_ipv4: binding.mapped_ipv4,
    setting,
    ipv6_mode: None,
    ipv6_mode_source: None,
  }
  if binding.family == "ipv4" {
    return base
  }
  let requested_mode = mode.map_or("default", assignment => assignment.value)
  let resolved_mode = match (requested_mode, env.bindv6only) {
    ("default", "0") => "both"
    ("default", "1") => "ipv6-only"
    ("default", _) => "unknown"
    (mode, _) => mode
  }
  {
    ..base,
    endpoint: "[\{binding.host}]:\{binding.port}",
    ipv6_mode: Some(resolved_mode),
    ipv6_mode_source: mode.map(assignment => assignment.source),
  }
}

///|
fn scan_unit(input : UnitInput, env : Environment) -> UnitReport {
  if !input.discovery_issues.is_empty() {
    return blank_unit(input, input.discovery_issues.copy())
  }
  guard input.plan.fragment is Some(fragment) else {
    return blank_unit(input, [
      input_issue(
        "missing_fragment",
        "No main fragment exists for the selected socket unit.",
        None,
      ),
    ])
  }
  if input.files.map(file => file.path) != expected_paths(input.plan) {
    return blank_unit(input, [
      input_issue(
        "invalid_file_plan",
        "File contents must match the selected paths in load order.",
        Some(file_source(fragment)),
      ),
    ])
  }
  if input.files[0].text.is_empty() {
    return blank_unit(input, [
      uncovered(
        "masked_fragment",
        "An empty main fragment masks the unit.",
        Some(file_source(fragment)),
      ),
    ])
  }
  let issues : Array[Issue] = []
  let mut active_listen : Array[ListenSetting] = []
  let resets : Array[ListenReset] = []
  let settings : Map[String, Assignment] = Map([])
  let mut unreliable = false
  for file in input.files {
    let (assignments, malformed) = parse_assignments(file)
    for issue in malformed {
      issues.push(issue)
      unreliable = true
    }
    for assignment in assignments {
      let { section, key, value, source, } = assignment
      if section == "Unit" {
        if !value.is_empty() {
          settings[key] = assignment
        }
      } else if listen_key(key) {
        if value.is_empty() {
          resets.push({ key, source, cleared: active_listen, })
          active_listen = []
        } else {
          active_listen.push({ key, value, source, })
        }
      } else {
        match key {
          "BindIPv6Only" =>
            match v6_mode(value) {
              Some(mode) => settings[key] = { ..assignment, value: mode, }
              None => {
                issues.push(
                  uncovered(
                    "invalid_ipv6_mode",
                    "Invalid or unsupported BindIPv6Only value.",
                    Some(source),
                  ),
                )
                unreliable = true
              }
            }
          "ReusePort" | "PrivateNetwork" | "Transparent" =>
            match systemd_bool(value) {
              Some(on) =>
                settings[key] = {
                  ..assignment,
                  value: if on {
                    "yes"
                  } else {
                    "no"
                  },
                }
              None => {
                issues.push(
                  uncovered(
                    "invalid_boolean",
                    "\{key} needs an explicit systemd boolean.",
                    Some(source),
                  ),
                )
                unreliable = true
              }
            }
          "BindToDevice" | "NetworkNamespacePath" | "SocketProtocol" =>
            settings[key] = assignment
          _ if key.has_prefix("Listen") => {
            issues.push(
              uncovered(
                "unknown_listen_setting",
                "An unmodeled Listen setting was encountered.",
                Some(source),
              ),
            )
            unreliable = true
          }
          _ => ()
        }
      }
    }
  }
  if active_listen.is_empty() && !unreliable {
    issues.push(
      input_issue(
        "no_active_listeners",
        "The unit has no Listen setting after applying resets.",
        Some(file_source(fragment)),
      ),
    )
  }
  for
    key in [
      "BindToDevice", "SocketProtocol", "ReusePort", "PrivateNetwork", "NetworkNamespacePath",
      "JoinsNamespaceOf", "Transparent",
    ] {
    if settings.get(key) is Some(setting) {
      let outside_scope = match key {
        "BindToDevice" => !setting.value.is_empty() && setting.value != "*"
        "JoinsNamespaceOf" | "NetworkNamespacePath" => !setting.value.is_empty()
        // SocketProtocol uses a parser without an empty-value reset in v257.
        "SocketProtocol" => true
        _ => setting.value == "yes"
      }
      if outside_scope {
        issues.push(
          uncovered(
            "unsupported_binding_option",
            "\{key} is outside this binding model.",
            Some(setting.source),
          ),
        )
        unreliable = true
      }
    }
  }
  let comparable_listeners = []
  for setting in active_listen {
    if setting.key != "ListenStream" {
      issues.push(
        uncovered(
          "unsupported_listen_type",
          "Active \{setting.key}= is outside TCP ListenStream coverage.",
          Some(setting.source),
        ),
      )
      continue
    }
    match parse_host_port(setting.value, env) {
      Err((code, message)) =>
        issues.push(uncovered(code, message, Some(setting.source)))
      Ok(binding) if !unreliable => {
        let listener = normalized_listener(
          input.name,
          setting,
          binding,
          settings.get("BindIPv6Only"),
          env,
        )
        if (listener.address == "::" || listener.mapped_ipv4 is Some(_)) &&
          listener.ipv6_mode == Some("unknown") {
          issues.push(
            uncovered(
              "unknown_bindv6only",
              "IPv4 overlap depends on the target net.ipv6.bindv6only. Supply --bindv6only 0 or 1.",
              Some(listener.ipv6_mode_source.unwrap_or(setting.source)),
            ),
          )
        }
        if listener.mapped_ipv4 is Some(_) && listener.ipv6_mode != Some("both") {
          if listener.ipv6_mode == Some("ipv6-only") {
            issues.push(
              uncovered(
                "mapped_ipv4_requires_dual_stack",
                "An IPv4-mapped address cannot bind in IPv6-only mode.",
                Some(listener.ipv6_mode_source.unwrap_or(setting.source)),
              ),
            )
          }
          continue
        }
        comparable_listeners.push(listener)
      }
      Ok(_) => ()
    }
  }
  {
    name: input.name,
    plan: input.plan,
    active_listen,
    resets,
    comparable_listeners,
    issues,
  }
}

///|
/// Audit one operator-declared group of sockets that would listen concurrently
/// in the same namespace. The checker never starts units or opens sockets.
pub fn analyze(inputs : Array[UnitInput], env : Environment) -> Report {
  if !valid_environment(env) {
    return command_error(
      "Expected ipv6=enabled|disabled|unknown and bindv6only=0|1|unknown.",
    )
  }
  if inputs.is_empty() || inputs.length() > max_units {
    return command_error(
      "Choose 1–\{max_units} distinct socket units in the same namespace.",
    )
  }
  analyze_group(inputs, env)
}

///|
priv struct PortBindings {
  sequence : Array[Listener]
  exact : Map[String, Array[Listener]]
  mut has_wildcard : Bool
}

///|
fn record_port_conflicts(
  bindings : PortBindings,
  listener : Listener,
  conflicts : Array[Conflict],
) -> Int {
  let mut count = 0
  if listener.address == "0.0.0.0" || listener.address == "::" {
    bindings.has_wildcard = true
  }
  if bindings.has_wildcard {
    for first in bindings.sequence {
      if conflict_kind(first, listener) is Some(code) {
        count += 1
        if conflicts.length() < max_reported_conflicts {
          conflicts.push({ code, port: listener.port, first, second: listener, })
        }
      }
    }
  } else {
    // Without wildcard coverage, only equal concrete addresses can overlap.
    // Native and IPv4-mapped listeners share the effective IPv4 bucket.
    let address = effective_ipv4(listener).unwrap_or(listener.address)
    let prior = bindings.exact.get(address).unwrap_or([])
    count = prior.length()
    let remaining = max_reported_conflicts - conflicts.length()
    let keep = if count < remaining { count } else { remaining }
    for first in prior[:keep] {
      if conflict_kind(first, listener) is Some(code) {
        conflicts.push({ code, port: listener.port, first, second: listener, })
      }
    }
    prior.push(listener)
    bindings.exact[address] = prior
  }
  bindings.sequence.push(listener)
  count
}

///|
fn analyze_group(inputs : Array[UnitInput], env : Environment) -> Report {
  if inputs.length() > max_units {
    return command_error(
      "Choose at most \{max_units} distinct socket units per stage.",
    )
  }
  let ordered = inputs.copy()
  ordered.sort_by((a, b) => compare_ascii_paths(a.name, b.name))
  let mut previous = ""
  for input in ordered {
    if !supported_unit_name(input.name) || input.name == previous {
      return command_error(
        "Unit names must be distinct literal .socket basenames (no templates or paths).",
      )
    }
    previous = input.name
  }
  let units = ordered.map(input => scan_unit(input, env))
  let mut active_count = 0
  for unit in units {
    active_count += unit.comparable_listeners.length()
  }
  if active_count > max_listeners {
    return make_report(Some(env), [], [], 0, [
      input_issue(
        "too_many_listeners",
        "Maximum \{max_listeners} comparable listeners per group.",
        None,
      ),
    ])
  }
  let by_port : Map[Int32, PortBindings] = Map([])
  let conflicts : Array[Conflict] = []
  let mut count = 0
  for unit in units {
    for listener in unit.comparable_listeners {
      let bindings = match by_port.get(listener.port) {
        Some(bindings) => bindings
        None => {
          let bindings : PortBindings = { sequence: [], exact: Map([]), has_wildcard: false }
          by_port[listener.port] = bindings
          bindings
        }
      }
      count += record_port_conflicts(bindings, listener, conflicts)
    }
  }
  make_report(Some(env), units, conflicts, count, [])
}
