///|
#external
priv type SnapshotProbe

///|
extern "js" fn open_probe(root : String) -> SnapshotProbe =
  #| root => {
  #|   const fs = process.getBuiltinModule("node:fs");
  #|   const path = process.getBuiltinModule("node:path");
  #|   return { fs, path, root: path.resolve(root), kinds: new Map(), lists: new Map(), listBudget: 4096 };
  #| }

///|
// This adapter checks path and file types. The file priority, syntax and
// listener semantics are decided in MoonBit, not by Node or the running host.
extern "js" fn probe(
  snapshot : SnapshotProbe,
  relative : String,
  operation : String,
) -> FixedArray[String] =
  #| (snapshot, relative, operation) => {
  #|   const { fs, path, root, kinds, lists } = snapshot;
  #|   function pathKind(file, part) {
  #|     try {
  #|       const entry = fs.lstatSync(file);
  #|       if (entry.isSymbolicLink()) return "link";
  #|       if (part && process.platform === "win32" && path.basename(fs.realpathSync.native(file)) !== part) return "missing";
  #|       if (entry.isDirectory()) return "directory";
  #|       return entry.isFile() ? "file" : "other";
  #|     } catch (error) {
  #|       if (error.code === "ENOENT") return "missing";
  #|       if (error.code === "ELOOP") return "link";
  #|       return "io_error";
  #|     }
  #|   }
  #|   try {
  #|     if (operation === "list" && lists.has(relative)) return lists.get(relative);
  #|     const parts = relative ? relative.split("/") : [];
  #|     if (parts.some(part => !part || part === "." || part === ".." || /[\\:\x00]/.test(part))) return ["invalid_path"];
  #|     let current = root;
  #|     let kind;
  #|     for (let i = -1; i < parts.length; i++) {
  #|       if (i >= 0) current = path.join(current, parts[i]);
  #|       const needsDirectory = i === -1 || i + 1 < parts.length;
  #|       // Selected files are revalidated from the root before opening them.
  #|       const cacheable = operation !== "read" && (needsDirectory || operation === "list");
  #|       kind = cacheable ? kinds.get(current) : undefined;
  #|       if (kind === undefined) {
  #|         kind = pathKind(current, parts[i]);
  #|         if (cacheable && kinds.size < 4096) kinds.set(current, kind);
  #|       }
  #|       if (kind === "link" || kind === "missing" || kind === "io_error") return [kind];
  #|       if (needsDirectory && kind !== "directory") return ["not_directory"];
  #|     }
  #|     if (operation === "kind") return [kind];
  #|     if (operation === "list" && kind === "directory") {
  #|       const dir = fs.opendirSync(current);
  #|       try {
  #|         const names = [];
  #|         let total = 0;
  #|         for (let item; (item = dir.readSync()) !== null;) {
  #|           if (++total > 4096) return ["too_many_entries"];
  #|           if (item.name.startsWith(".") || !item.name.endsWith(".conf")) continue;
  #|           if (names.length === 128) return ["too_many_drop_ins"];
  #|           names.push(item.name);
  #|         }
  #|         names.sort();
  #|         const result = ["listed", ...names];
  #|         if (snapshot.listBudget >= result.length) {
  #|           snapshot.listBudget -= result.length;
  #|           lists.set(relative, result);
  #|         }
  #|         return result;
  #|       } finally { dir.closeSync(); }
  #|     }
  #|     if (operation === "read" && kind === "file") {
  #|       const fd = fs.openSync(current, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
  #|       try {
  #|         const info = fs.fstatSync(fd);
  #|         if (!info.isFile()) return ["other"];
  #|         if (info.size > 256 * 1024) return ["file_too_large"];
  #|         const data = new Uint8Array(info.size);
  #|         for (let offset = 0; offset < data.length;) {
  #|           const count = fs.readSync(fd, data, offset, data.length - offset, offset);
  #|           if (!count) return ["file_changed"];
  #|           offset += count;
  #|         }
  #|         const after = fs.fstatSync(fd);
  #|         if (after.size !== info.size || after.mtimeMs !== info.mtimeMs) return ["file_changed"];
  #|         if (data.length === 0) return ["read", ""];
  #|         return ["read", new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(data)];
  #|       } finally { fs.closeSync(fd); }
  #|     }
  #|     return [kind];
  #|   } catch (error) {
  #|     if (error.code === "ENOENT") return ["missing"];
  #|     if (error.code === "ELOOP") return ["link"];
  #|     if (error.code === "ERR_ENCODING_INVALID_ENCODED_DATA") return ["invalid_utf8"];
  #|     return ["io_error"];
  #|   }
  #| }

///|
let supported_roots : Array[String] = [
  "etc/systemd/system", "run/systemd/system", "usr/lib/systemd/system",
]

///|
let other_roots : Array[String] = [
  "etc/systemd/system.control", "run/systemd/system.control", "run/systemd/transient",
  "run/systemd/generator.early", "etc/systemd/system.attached", "run/systemd/system.attached",
  "run/systemd/generator", "usr/local/lib/systemd/system", "lib/systemd/system",
  "run/systemd/generator.late",
]

///|
priv struct Budget {
  mut remaining : Int32
}

///|
fn path_issue(
  path : String,
  kind : String,
  context : String,
) -> @preflight.Issue {
  let source = Some(@preflight.file_source(path))
  match kind {
    "link" =>
      @preflight.uncovered(
        "symlink", "Symbolic links, aliases and linked masks are outside this version.",
        source,
      )
    "directory" | "file" | "missing" | "not_directory" | "other" =>
      @preflight.input_issue(
        "unexpected_file_type",
        "Cannot read \{context} as expected (\{kind}).",
        source,
      )
    _ =>
      @preflight.input_issue(kind, "Cannot read \{context} (\{kind}).", source)
  }
}

///|
fn search_drop_ins(
  snapshot : SnapshotProbe,
  directory : String,
  drop_ins : Array[@preflight.DropIn],
  issues : Array[@preflight.Issue],
) -> Unit {
  let contents = probe(snapshot, directory, "list")
  match contents[0] {
    "missing" => ()
    "listed" =>
      for name in contents[1:] {
        let drop_path = directory + "/" + name
        if @preflight.supported_drop_in_name(name) {
          drop_ins.push({ basename: name, path: drop_path, })
        } else {
          issues.push(
            @preflight.uncovered(
              "unsupported_filename",
              "Use conventional ASCII drop-in filenames.",
              Some(@preflight.file_source(drop_path)),
            ),
          )
        }
      }
    kind => issues.push(path_issue(directory, kind, "the drop-in directory"))
  }
}

///|
fn detect_extra_dir(
  snapshot : SnapshotProbe,
  path : String,
  issues : Array[@preflight.Issue],
) -> Unit {
  let result = probe(snapshot, path, "list")
  if result[0] == "missing" || (result[0] == "listed" && result.length() == 1) {
    return
  }
  issues.push(
    @preflight.uncovered(
      "unsupported_unit_path",
      "An applicable drop-in directory is outside the supported search paths.",
      Some(@preflight.file_source(path)),
    ),
  )
}

///|
fn load_unit(
  snapshot : SnapshotProbe,
  name : String,
  budget : Budget,
) -> @preflight.UnitInput {
  let issues : Array[@preflight.Issue] = []
  let inherited = @preflight.inherited_drop_in_dirs(name)
  let layers = supported_roots.map(layer => {
    let path = layer + "/" + name
    let fragment = if probe(snapshot, path, "kind")[0] == "missing" {
      None
    } else {
      Some(path)
    }
    let drop_ins : Array[@preflight.DropIn] = []
    search_drop_ins(snapshot, path + ".d", drop_ins, issues)
    // Within a load path, walk from the full name to the shortest prefix.
    for i = inherited.length() - 1; i > 0; i = i - 1 {
      search_drop_ins(snapshot, layer + "/" + inherited[i], drop_ins, issues)
    }
    @preflight.Layer::{ fragment, drop_ins, }
  })
  // systemd searches type-wide directories after all unit-specific paths.
  for layer in supported_roots {
    let drop_ins : Array[@preflight.DropIn] = []
    search_drop_ins(snapshot, layer + "/" + inherited[0], drop_ins, issues)
    layers.push({ fragment: None, drop_ins, })
  }
  let plan = @preflight.select_files(layers)
  for layer in other_roots {
    let path = layer + "/" + name
    if probe(snapshot, path, "kind")[0] != "missing" {
      issues.push(
        @preflight.uncovered(
          "unsupported_unit_path",
          "A relevant file may exist in an unmodeled unit search path.",
          Some(@preflight.file_source(path)),
        ),
      )
    }
    detect_extra_dir(snapshot, path + ".d", issues)
    for dir in inherited {
      detect_extra_dir(snapshot, layer + "/" + dir, issues)
    }
  }
  let files : Array[@preflight.FileText] = []
  if issues.is_empty() && plan.fragment is Some(fragment) {
    let paths = [fragment]
    for path in plan.drop_ins {
      paths.push(path)
    }
    for path in paths {
      let reply = probe(snapshot, path, "read")
      if reply[0] != "read" {
        issues.push(path_issue(path, reply[0], "the selected unit file"))
        break
      }
      let content = reply[1]
      budget.remaining -= content.length()
      if budget.remaining < 0 {
        issues.push(
          @preflight.input_issue(
            "input_too_large",
            "Selected configuration exceeds 4 Mi characters.",
            Some(@preflight.file_source(path)),
          ),
        )
        break
      }
      files.push({ path, text: content, })
    }
  }
  { name, plan, files, discovery_issues: issues, }
}
