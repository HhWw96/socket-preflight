"""Shared inputs for the extended archive and Linux manager checks."""

from dataclasses import dataclass


ADMIN = "etc/systemd/system/"
RUNTIME = "run/systemd/system/"
VENDOR = "usr/lib/systemd/system/"
PORT = 45391
PAIR = ("admin.socket", "public.socket")


@dataclass(frozen=True)
class Case:
    name: str
    suite: str
    units: tuple[str, str]
    files: dict[str, str]
    endpoints: tuple[tuple[str, ...], tuple[str, ...]]
    selected: tuple[tuple[str, ...], tuple[str, ...]]
    outcome: str
    conflict: str | None = None


def listener(endpoint, mode=None, reset=False, port=PORT):
    text = "[Socket]\n"
    if mode:
        text += f"BindIPv6Only={mode}\n"
    if reset:
        text += "ListenStream=\n"
    return text + f"ListenStream={endpoint}:{port}\n"


def cases():
    long_name = "edge-zone-public.socket"
    prefix_name = "edge-zone-.socket.d/20-socket-preflight.conf"
    wide_file = ADMIN + "socket.d/40-socket-preflight.conf"
    specific_file = VENDOR + long_name + ".d/40-socket-preflight.conf"
    shared = {
        VENDOR + "admin.socket": listener("127.0.0.1"),
        VENDOR + long_name: listener("[::]", "both"),
        ADMIN + prefix_name: listener("[::]", "both", reset=True, port=45392),
        ADMIN + "edge-.socket.d/20-socket-preflight.conf": listener("[::]", "both", reset=True),
        VENDOR + long_name + ".d/20-socket-preflight.conf": listener("[::]", "both", reset=True),
        wide_file: "[Socket]\nBacklog=32\n",
        specific_file: "[Socket]\nBacklog=64\n",
    }
    yield Case("prefix-and-type-wide-precedence", "drop-ins", ("admin.socket", long_name), shared,
               (("127.0.0.1:45391",), ("[::]:45392",)), ((wide_file,), (ADMIN + prefix_name, specific_file)),
               "coexist")

    names = ("admin.socket", "public.socket")
    mapped = "[::ffff:127.0.0.1]"
    for name, left, right, left_mode, right_mode, outcome, conflict in (
        ("mapped-v4-overlap", mapped, "127.0.0.1", "both", None, "conflict", "ipv4_mapped_overlap"),
        ("mapped-v4-wildcard", mapped, "0.0.0.0", "both", None, "conflict", "ipv4_mapped_overlap"),
        ("mapped-disjoint-v4", mapped, "127.0.0.2", "both", None, "coexist", None),
        ("mapped-ipv6-only-wildcard", mapped, "[::]", "both", "ipv6-only", "coexist", None),
        ("mapped-dual-stack-wildcard", mapped, "[::]", "both", "both", "conflict", "dual_stack_overlap"),
        ("mapped-hex-equivalence", "[0:0:0:0:0:FFFF:7F00:1]", mapped, "both", "both", "conflict", "same_address"),
        ("mapped-explicit-ipv6-only-fails", mapped, "127.0.0.1", "ipv6-only", None, "invalid_bind", None),
    ):
        data = {VENDOR + name: listener(endpoint, mode)
                for name, endpoint, mode in zip(names, (left, right), (left_mode, right_mode), strict=True)}
        yield Case(name, "mapped", names, data, ((f"{mapped}:{PORT}",), (f"{right}:{PORT}",)),
                   ((), ()), outcome, conflict)
