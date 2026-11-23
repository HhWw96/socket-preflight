///|
fn source_line(result : Listener) -> Int32? {
  result.setting.source.line
}

///|
fn test_unit(name : String, texts : Array[String]) -> UnitInput {
  let fragment = "vendor/" + name
  let paths = texts.mapi((i, _) => {
    if i == 0 {
      fragment
    } else {
      "local/\{name}.d/\{i}.conf"
    }
  })
  {
    name,
    plan: {
      fragment: Some(fragment),
      drop_ins: paths[1:].to_owned(),
      shadowed: [],
    },
    files: texts.mapi((i, text) => { path: paths[i], text, }),
    discovery_issues: [],
  }
}

///|
fn compare_config(
  first : Array[String],
  second : Array[String],
  ipv6~ : String = "enabled",
  bindv6only~ : String = "unknown",
) -> Report {
  analyze([test_unit("a.socket", first), test_unit("b.socket", second)], {
    ipv6,
    bindv6only,
  })
}

///|
test "basename precedence is applied before alphabetical ordering" {
  let selected = select_files([
    {
      fragment: Some("etc/a.socket"),
      drop_ins: [{ basename: "20-change.conf", path: "etc/20-change.conf", }],
    },
    {
      fragment: Some("usr/a.socket"),
      drop_ins: [
        { basename: "90-reset.conf", path: "usr/90-reset.conf", },
        { basename: "20-change.conf", path: "usr/20-change.conf", },
        { basename: "10-early.conf", path: "usr/10-early.conf", },
      ],
    },
  ])
  assert_eq(selected.fragment, Some("etc/a.socket"))
  assert_eq(selected.drop_ins, [
    "usr/10-early.conf", "etc/20-change.conf", "usr/90-reset.conf",
  ])
  assert_eq(selected.shadowed.map(x => x.winner), [
    "etc/20-change.conf", "etc/a.socket",
  ])
}

///|
test "append keeps active records and points to the actual changed line" {
  let report = compare_config(
    ["[Socket]\nListenStream=[::]:45391\nBindIPv6Only=both"],
    [
      "[Socket]\nListenStream=127.0.0.1:45392\n", "[Socket]\n# new listener\nListenStream=127.0.0.1:45391",
    ],
  )
  assert_eq(report.exit_code, 1)
  assert_eq(report.conflicts[0].code, "dual_stack_overlap")
  assert_eq(source_line(report.conflicts[0].first), Some(2))
  assert_eq(source_line(report.conflicts[0].second), Some(3))
  assert_eq(
    report.conflicts[0].second.setting.source.path,
    "local/b.socket.d/1.conf",
  )
  assert_eq(report.units[1].active_listen.length(), 2)
}

///|
test "an empty known Listen directive clears the complete list" {
  let report = compare_config(["[Socket]\nListenStream=127.0.0.1:45391"], [
    "[Socket]\nListenStream=0.0.0.0:45391\nListenDatagram=12345", "[Socket]\nListenDatagram=\nListenStream=127.0.0.1:45392",
  ])
  assert_eq(report.exit_code, 0)
  assert_eq(report.units[1].resets[0].cleared.map(l => l.key), [
    "ListenStream", "ListenDatagram",
  ])
  assert_eq(report.units[1].active_listen.map(l => l.value), ["127.0.0.1:45392"])
}

///|
test "BOM CRLF and continued lines preserve physical ranges" {
  let config = "\u{feff}[Socket]\r\nListenStream=\\\r\n# continuation comment\r\n  [::]:45391\r\nBindIPv6Only=true\r\n"
  let report = compare_config([config], [
    "[Socket]\nListenStream=127.0.0.1:45391",
  ])
  assert_eq(report.exit_code, 0)
  let listener = report.units[0].comparable_listeners[0]
  assert_eq(
    (listener.setting.source.line, listener.setting.source.end_line),
    (Some(2), Some(4)),
  )
  assert_eq(listener.ipv6_mode, Some("ipv6-only"))
}

///|
test "bare ports use target IPv6 capability and kernel default" {
  let a = ["[Socket]\nListenStream=45391"]
  let b = ["[Socket]\nListenStream=127.0.0.1:45391"]
  assert_eq(compare_config(a, b, ipv6="unknown").exit_code, 2)
  assert_eq(compare_config(a, b).exit_code, 2)
  assert_eq(
    compare_config(a, b, bindv6only="0").conflicts[0].code,
    "dual_stack_overlap",
  )
  assert_eq(compare_config(a, b, bindv6only="1").exit_code, 0)
  assert_eq(
    compare_config(a, b, ipv6="disabled").conflicts[0].first.endpoint,
    "0.0.0.0:45391",
  )
}

///|
test "canonical IPv6 equality is independent of wildcard default" {
  let report = compare_config(
    ["[Socket]\nListenStream=[2001:0Db8:0:0:0:0:0:1]:45391"],
    ["[Socket]\nListenStream=[2001:db8::1]:45391"],
  )
  assert_eq(report.conflicts[0].code, "same_address")
  assert_eq(report.conflicts[0].first.endpoint, "[2001:db8::1]:45391")
}

///|
test "loopback IPv6 is not a dual-stack wildcard" {
  let report = compare_config(
    ["[Socket]\nListenStream=[::1]:45391\nBindIPv6Only=both"],
    ["[Socket]\nListenStream=0.0.0.0:45391"],
  )
  assert_eq(report.exit_code, 0)
}

///|
test "scoped and mapped addresses do not produce a clear result" {
  let b = ["[Socket]\nListenStream=0.0.0.0:45391"]
  for
    rvalue in [
      "[::ffff:127.0.0.1]:45391", "[::FFFF:7f00:1]:45391", "[fe80::1]:45391", "[fe80::1]:45391%eth0",
      "[1::2::3]:45391", "0", "045391",
    ] {
    let report = compare_config(["[Socket]\nListenStream=\{rvalue}"], b)
    assert_eq(report.exit_code, 2)
    assert_eq(report.units[0].comparable_listeners.length(), 0)
  }
}

///|
test "binding flags use the effective last value and its source" {
  let a = [
    "[Socket]\nListenStream=0.0.0.0:45391\nBindToDevice=eth0\nReusePort=1",
  ]
  let b = ["[Socket]\nListenStream=127.0.0.1:45391"]
  let blocked = compare_config(a, b)
  assert_eq(blocked.exit_code, 2)
  assert_eq(blocked.units[0].issues.length(), 2)
  let restored = compare_config(
    [a[0], "[Socket]\nBindToDevice=*\nReusePort=no"],
    b,
  )
  assert_eq(restored.exit_code, 1)
  assert_eq(restored.conflicts[0].code, "wildcard_overlap")
}

///|
test "a different namespace and malformed sources never imply clear" {
  let b = ["[Socket]\nListenStream=127.0.0.1:45391"]
  assert_eq(
    compare_config(
      ["[Socket]\nListenStream=0.0.0.0:45391\nPrivateNetwork=yes"],
      b,
    ).exit_code,
    2,
  )
  assert_eq(
    compare_config(["[Socket\nListenStream=0.0.0.0:45391"], b).exit_code,
    3,
  )
  assert_eq(compare_config(["[Socket]\nListenStream="], b).exit_code, 3)
}

///|
test "an unsupported separator inside a comment cannot hide assignments" {
  let b = ["[Socket]\nListenStream=127.0.0.1:45391"]
  for separator in ["\u{0}", "\r"] {
    let a = ["[Socket]\n# comment\{separator}ListenStream=0.0.0.0:45391\n"]
    let result = compare_config(a, b)
    assert_eq(result.exit_code, 3)
    assert_eq(result.units[0].issues[0].source.unwrap().line, Some(2))
  }
}
