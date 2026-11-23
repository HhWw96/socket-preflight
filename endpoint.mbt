///|
fn unsigned_decimal(text : StringView, upper : Int32) -> Int32? {
  if text.is_empty() || (text.length() > 1 && text[0] == '0') {
    return None
  }
  let mut number = 0
  for c in text {
    if c < '0' || c > '9' {
      return None
    }
    number = number * 10 + c.to_int() - '0'.to_int()
    if number > upper {
      return None
    }
  }
  Some(number)
}

///|
fn port_number(text : StringView) -> Int32? {
  unsigned_decimal(text, 65535).filter(n => n > 0)
}

///|
fn ipv4_octets(text : StringView) -> Array[Int32]? {
  let octets = []
  for part in text.split(".") {
    match unsigned_decimal(part, 255) {
      Some(n) if octets.length() < 4 => octets.push(n)
      _ => return None
    }
  }
  if octets.length() == 4 {
    Some(octets)
  } else {
    None
  }
}

///|
fn hex_digit(c : Char) -> Int32? {
  if c >= '0' && c <= '9' {
    Some(c.to_int() - '0'.to_int())
  } else if c >= 'a' && c <= 'f' {
    Some(10 + c.to_int() - 'a'.to_int())
  } else if c >= 'A' && c <= 'F' {
    Some(10 + c.to_int() - 'A'.to_int())
  } else {
    None
  }
}

///|
fn ipv6_side(text : StringView, dotted_tail : Bool) -> Array[Int32]? {
  let words = []
  if text.is_empty() {
    return Some(words)
  }
  let parts = text.split(":").collect()
  for i, part in parts {
    if part.contains(".") && dotted_tail && i + 1 == parts.length() {
      match ipv4_octets(part) {
        Some(octets) => {
          words.push(octets[0] * 256 + octets[1])
          words.push(octets[2] * 256 + octets[3])
        }
        None => return None
      }
    } else {
      if part.is_empty() || part.length() > 4 {
        return None
      }
      let mut word = 0
      for c in part {
        match hex_digit(c) {
          Some(digit) => word = word * 16 + digit
          None => return None
        }
      }
      words.push(word)
    }
    if words.length() > 8 {
      return None
    }
  }
  Some(words)
}

///|
fn ipv6_words(text : StringView) -> Array[Int32]? {
  match text.split_once("::") {
    Some((left, right)) =>
      match (ipv6_side(left, false), ipv6_side(right, true)) {
        (Some(prefix), Some(suffix)) if prefix.length() + suffix.length() < 8 => {
          let words = prefix.copy()
          for _ in 0..<(8 - prefix.length() - suffix.length()) {
            words.push(0)
          }
          for word in suffix {
            words.push(word)
          }
          Some(words)
        }
        _ => None
      }
    None => ipv6_side(text, true).filter(words => words.length() == 8)
  }
}

///|
fn hextet(word : Int32) -> String {
  let digits = "0123456789abcdef"
  let b = StringBuilder()
  let mut started = false
  for shift = 12; shift >= 0; shift = shift - 4 {
    let digit = (word >> shift) & 15
    if started || digit != 0 || shift == 0 {
      b.write_string(digits[digit:digit + 1].to_owned())
      started = true
    }
  }
  b.to_string()
}

///|
fn canonical_v6(words : Array[Int32]) -> String {
  let mut best_start = 0
  let mut best_length = 1
  let mut i = 0
  while i < words.length() {
    if words[i] != 0 {
      i += 1
      continue
    }
    let start = i
    while i < words.length() && words[i] == 0 {
      i += 1
    }
    if i - start > best_length {
      best_start = start
      best_length = i - start
    }
  }
  let parts = words.map(hextet)
  if best_length == 1 {
    parts.join(":")
  } else {
    parts[:best_start].to_owned().join(":") +
    "::" +
    parts[best_start + best_length:].to_owned().join(":")
  }
}

///|
priv struct HostPort {
  host : String
  port : Int32
  family : String
  mapped_ipv4 : String?
}

///|
fn mapped_ipv4_address(words : Array[Int32]) -> String? {
  if !words[:5].all(w => w == 0) || words[5] != 65535 {
    return None
  }
  let a = words[6] >> 8
  let b = words[6] & 255
  let c = words[7] >> 8
  let d = words[7] & 255
  Some("\{a}.\{b}.\{c}.\{d}")
}

///|
fn parse_host_port(
  value : String,
  env : Environment,
) -> Result[HostPort, (String, String)] {
  if value.contains("%") {
    return Err(
      (
        "scoped_or_expanded_address", "Address requires a scope or specifier expansion.",
      ),
    )
  }
  if value.has_prefix("[") {
    let suffix = value.strip_prefix("[").unwrap()
    match suffix.split_once("]:") {
      Some((host, port)) =>
        match (ipv6_words(host), port_number(port)) {
          (Some(words), Some(p)) => {
            let mapped_ipv4 = mapped_ipv4_address(words)
            if mapped_ipv4 is Some(address) &&
              (address == "0.0.0.0" || words[6] >> 8 >= 224) {
              return Err(
                (
                  "unsupported_mapped_ipv4", "Mapped IPv4 wildcard, multicast and reserved high addresses are outside this version.",
                ),
              )
            }
            if (words[0] & 0xffc0) == 0xfe80 || (words[0] & 0xff00) == 0xff00 {
              return Err(
                (
                  "ipv6_scope", "Link-local and multicast IPv6 addresses are outside this version.",
                ),
              )
            }
            if env.ipv6 != "enabled" {
              return Err(
                (
                  "ipv6_support", "An IPv6 literal needs --ipv6 enabled for the target.",
                ),
              )
            }
            let address = match mapped_ipv4 {
              Some(ip) => "::ffff:" + ip
              None => canonical_v6(words)
            }
            return Ok({ host: address, port: p, family: "ipv6", mapped_ipv4, })
          }
          _ => ()
        }
      None => ()
    }
  } else if value.split_once(":") is Some((host, port)) {
    if ipv4_octets(host) is Some(octets) && port_number(port) is Some(p) {
      if octets[0] >= 224 {
        return Err(
          (
            "ipv4_scope", "Multicast and reserved high IPv4 addresses are outside this version.",
          ),
        )
      }
      return Ok({
        host: host.to_owned(),
        port: p,
        family: "ipv4",
        mapped_ipv4: None,
      })
    }
  } else if port_number(value[:]) is Some(port) {
    return match env.ipv6 {
      "enabled" => Ok({ host: "::", port, family: "ipv6", mapped_ipv4: None, })
      "disabled" =>
        Ok({ host: "0.0.0.0", port, family: "ipv4", mapped_ipv4: None, })
      _ =>
        Err(
          (
            "ipv6_support", "A bare port needs the target IPv6 capability to select a family.",
          ),
        )
    }
  }
  Err(
    (
      "unsupported_address", "Need a literal IPv4:port, [IPv6]:port, or decimal port (1–65535, no leading zeros).",
    ),
  )
}

///|
fn effective_ipv4(listener : Listener) -> String? {
  if listener.family == "ipv4" {
    Some(listener.address)
  } else {
    listener.mapped_ipv4
  }
}

///|
fn is_dual_wildcard(listener : Listener) -> Bool {
  listener.address == "::" && listener.ipv6_mode == Some("both")
}

///|
fn conflict_kind(a : Listener, b : Listener) -> String? {
  if a.port != b.port {
    return None
  }
  if a.family == b.family && a.address == b.address {
    return Some("same_address")
  }
  match (effective_ipv4(a), effective_ipv4(b)) {
    (Some(x), Some(y)) if x == y || x == "0.0.0.0" || y == "0.0.0.0" =>
      Some(
        if a.family == b.family {
          "wildcard_overlap"
        } else {
          "ipv4_mapped_overlap"
        },
      )
    (Some(_), None) if is_dual_wildcard(b) => Some("dual_stack_overlap")
    (None, Some(_)) if is_dual_wildcard(a) => Some("dual_stack_overlap")
    (None, None) if a.address == "::" || b.address == "::" =>
      Some("wildcard_overlap")
    _ => None
  }
}
