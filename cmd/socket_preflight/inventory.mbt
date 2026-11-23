///|
#derive(@json.ToJson)
priv struct FragmentLocation {
  path : String
  kind : String
  modeled_path : Bool
}

///|
#derive(@json.ToJson)
priv struct InventoryCandidate {
  name : String
  literal_name : Bool
  fragments : Array[FragmentLocation]
}

///|
#derive(@json.ToJson)
priv struct InventoryReport {
  schema_version : Int
  status : String
  exit_code : Int
  candidates : Array[InventoryCandidate]
  issues : Array[@preflight.Issue]
}

///|
fn inventory_error(message : String) -> InventoryReport {
  {
    schema_version: 1,
    status: "input_error",
    exit_code: 3,
    candidates: [],
    issues: [@preflight.input_issue("invalid_inventory", message, None)],
  }
}

///|
fn inventory(root : String) -> InventoryReport {
  let snapshot = open_probe(root)
  let kind = probe(snapshot, "", "kind")[0]
  if kind != "directory" {
    return inventory_error("Snapshot root must be a readable directory (\{kind}).")
  }
  let by_name : Map[String, Array[FragmentLocation]] = Map([])
  let issues = []
  for index, roots in [supported_roots, other_roots] {
    for layer in roots {
      let listed = probe(snapshot, layer, "inventory")
      match listed[0] {
        "missing" => ()
        "listed" =>
          for name in listed[1:] {
            let path = layer + "/" + name
            let kind = probe(snapshot, path, "kind")[0]
            let locations = by_name.get(name).unwrap_or([])
            locations.push({ path, kind, modeled_path: index == 0, })
            by_name[name] = locations
            if kind == "missing" || kind == "io_error" {
              issues.push(
                @preflight.uncovered(
                  "inventory_entry_unavailable",
                  "A listed fragment could not be inspected (\{kind}).",
                  Some(@preflight.file_source(path)),
                ),
              )
            }
          }
        failure =>
          issues.push(
            @preflight.uncovered(
              "inventory_directory_unavailable",
              "Could not list socket fragments (\{failure}).",
              Some(@preflight.file_source(layer)),
            ),
          )
      }
    }
  }
  let names = by_name.keys().collect()
  names.sort()
  let candidates = names.map(name => {
    {
      name,
      literal_name: @preflight.supported_unit_name(name),
      fragments: by_name[name],
    }
  })
  let (status, exit_code) = if issues.is_empty() { ("ok", 0) } else { ("partial", 2) }
  { schema_version: 1, status, exit_code, candidates, issues, }
}

///|
fn run_inventory(args : ArrayView[String]) -> Unit {
  let json = args.any(arg => arg == "--json")
  let options = args.iter().filter(arg => arg != "--json").collect()
  let report = if options.length() == 2 &&
    args.length() == (if json { 3 } else { 2 }) &&
    options[0] == "--root" &&
    !options[1].is_empty() &&
    !options[1].has_prefix("--") {
    inventory(options[1])
  } else {
    inventory_error("Supply --root <snapshot-dir> and optional --json.")
  }
  if json {
    println(@json.to_json(report).stringify(indent=2))
  } else {
    println("INVENTORY \{report.status.to_upper()} | \{report.candidates.length()} socket name(s)")
    for candidate in report.candidates {
      println("  " + @json.to_json(candidate.name).stringify())
      for location in candidate.fragments {
        let model = if location.modeled_path { "" } else { ", outside scan paths" }
        let path = @json.to_json(location.path).stringify()
        println("    \{path} (\{location.kind}\{model})")
      }
    }
    for issue in report.issues {
      let source = issue.source.map_or("", source => @json.to_json(source.path).stringify())
      println("inventory [\{issue.code}] \{source} \{issue.message}")
    }
  }
  exit_status(report.exit_code)
}
