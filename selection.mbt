///|
// systemd compares basenames byte by byte. The MoonBit String Compare order
// must not decide overrides, especially for files with different name lengths.
fn compare_ascii_paths(left : String, right : String) -> Int32 {
  let n = if left.length() < right.length() {
    left.length()
  } else {
    right.length()
  }
  for i in 0..<n {
    if left[i] != right[i] {
      return left[i].to_int() - right[i].to_int()
    }
  }
  left.length() - right.length()
}

///|
/// Select one fragment and one file per drop-in basename. Layers are supplied
/// from highest to lowest priority. Different basenames are then sorted globally.
pub fn select_files(layers : Array[Layer]) -> FilePlan {
  let mut fragment = None
  let selected : Map[String, String] = Map([])
  let shadowed : Array[Shadow] = []
  for layer in layers {
    if layer.fragment is Some(path) {
      match fragment {
        None => fragment = Some(path)
        Some(winner) => shadowed.push({ path, winner, })
      }
    }
    for drop_in in layer.drop_ins {
      match selected.get(drop_in.basename) {
        None => selected[drop_in.basename] = drop_in.path
        Some(winner) => shadowed.push({ path: drop_in.path, winner, })
      }
    }
  }
  let names = selected.keys().collect()
  names.sort_by(compare_ascii_paths)
  shadowed.sort_by((a, b) => compare_ascii_paths(a.path, b.path))
  { fragment, drop_ins: names.map(name => selected[name]), shadowed, }
}

///|
fn ascii_alnum(c : Char) -> Bool {
  (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
}

///|
fn simple_basename(name : String, suffix : String) -> Bool {
  match name.strip_suffix(suffix) {
    Some(stem) if !stem.is_empty() && name.length() <= 255 => {
      let mut first = true
      for c in stem {
        if !ascii_alnum(c) && (first || (c != '-' && c != '_' && c != '.')) {
          return false
        }
        first = false
      }
      true
    }
    _ => false
  }
}

///|
pub fn supported_unit_name(name : String) -> Bool {
  simple_basename(name, ".socket")
}

///|
pub fn supported_drop_in_name(name : String) -> Bool {
  simple_basename(name, ".conf")
}

///|
/// Type-wide directory followed by dash prefixes, from shortest to longest.
pub fn inherited_drop_in_dirs(name : String) -> Array[String] {
  let dirs = ["socket.d"]
  if name.strip_suffix(".socket") is Some(stem) && supported_unit_name(name) {
    for i = 0; i + 1 < stem.length(); i = i + 1 {
      if stem[i:i + 1] == "-" {
        dirs.push(stem[:i + 1].to_owned() + ".socket.d")
      }
    }
  }
  dirs
}
