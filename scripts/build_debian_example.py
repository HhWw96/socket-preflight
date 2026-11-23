#!/usr/bin/env python3
"""Assemble two deployment ZIPs from the original Debian socket unit files."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen
import zipfile


VENDOR = "usr/lib/systemd/system/"
OVERRIDE = "etc/systemd/system/ssh.socket.d/20-move-port.conf"
PACKAGES = {
    "cockpit-ws": {
        "version": "337-1+deb13u2",
        "pool": "c/cockpit/cockpit-ws_337-1+deb13u2_amd64.deb",
        "size": 1038576,
        "sha256": "f74347e89be9f25da9645f2975cb9d8227333c2461782bd701b6cf9fcc43ff1c",
        "unit": "cockpit.socket",
        "unit_sha256": "96adc7b29940859acac3ec3464720917b86392a28ff5a200d1f89a9f40aa21f5",
    },
    "openssh-server": {
        "version": "1:10.0p1-7+deb13u4",
        "pool": "o/openssh/openssh-server_10.0p1-7+deb13u4_amd64.deb",
        "size": 602372,
        "sha256": "b4a02524fd2be375624d917ee8102a16567e9e8dd786b41c35e22360cdd37f9d",
        "unit": "ssh.socket",
        "unit_sha256": "9c36778605edb201ec5b7d1676887fc00249fa1b2582ec89e158c55b45b4659c",
        "copyright_url": "https://metadata.ftp-master.debian.org/changelogs/main/o/openssh/openssh_10.0p1-7+deb13u4_copyright",
        "copyright_sha256": "af946edbe423f2af7cb2297e68ed177748461b3c8ecee9884ea087bea7b06f92",
    },
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def data_tar(package):
    # Only called on the two verified Debian archives.
    if not package.startswith(b"!<arch>\n"):
        raise ValueError("Not a Debian ar archive")
    pos = 8
    while pos + 60 <= len(package):
        header = package[pos:pos + 60]
        length = int(header[48:58])
        start = pos + 60
        if header[58:] != b"`\n" or not 0 <= length <= len(package) - start:
            raise ValueError("Invalid Debian member bounds")
        if header[:16].rstrip(b" /").startswith(b"data.tar"):
            return tarfile.open(fileobj=io.BytesIO(package[start:start + length]), mode="r:*")
        pos = start + length + length % 2
    raise ValueError("Debian archive has no data tar")


def read_package(name, spec, local):
    url = "https://deb.debian.org/debian/pool/main/" + spec["pool"]
    file = Path(spec["pool"]).name
    if local is None:
        with urlopen(url, timeout=30) as source:
            package = source.read(spec["size"] + 1)
    else:
        with (local / file).open("rb") as source:
            package = source.read(spec["size"] + 1)
    if len(package) != spec["size"] or sha(package) != spec["sha256"]:
        raise ValueError(f"Package does not match the locked SHA-256: {file}")
    with data_tar(package) as archive:
        with archive.extractfile("./" + VENDOR + spec["unit"]) as source:
            unit = source.read()
        if "copyright_url" not in spec:
            with archive.extractfile(f"./usr/share/doc/{name}/copyright") as source:
                notice = source.read()
        else:
            # Debian links this package's docs to openssh-client.
            notice_url = spec["copyright_url"]
            if local is None:
                with urlopen(notice_url, timeout=30) as source:
                    notice = source.read(65537)
            else:
                with (local / notice_url.rsplit("/", 1)[1]).open("rb") as source:
                    notice = source.read(65537)
            if sha(notice) != spec["copyright_sha256"]:
                raise ValueError(f"Copyright file does not match the lock: {name}")
    if sha(unit) != spec["unit_sha256"]:
        raise ValueError(f"Unit bytes differ from the lock: {name}")
    metadata = {**spec, "url": url, "architecture": "amd64", "copyright_sha256": sha(notice)}
    return unit, notice, metadata


def override(port):
    return f"[Socket]\nListenStream=\nListenStream=127.0.0.1:{port}\n".encode("ascii")


def write_bundle(path, entries):
    temporary = path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as bundle:
            for name, content in sorted(entries.items()):
                member = zipfile.ZipInfo(name)
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                # Stored members give byte-for-byte results across zlib versions.
                bundle.writestr(member, content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return sha(path.read_bytes())


def build(output, local):
    cockpit, cockpit_notice, cockpit_source = read_package("cockpit-ws", PACKAGES["cockpit-ws"], local)
    ssh, ssh_notice, ssh_source = read_package("openssh-server", PACKAGES["openssh-server"], local)
    plan = {
        "schema_version": 1,
        "environment": {"ipv6": "enabled", "bindv6only": "0"},
        "baseline": {"root": "before", "units": ["cockpit.socket"]},
        "transition": {"root": "overlap", "units": ["cockpit.socket", "ssh.socket"]},
        "candidate": {"root": "after", "units": ["ssh.socket"]},
    }
    sources = {"cockpit-ws": cockpit_source, "openssh-server": ssh_source}
    common = {
        "plan.json": (json.dumps(plan, indent=2) + "\n").encode(),
        "before/" + VENDOR + "cockpit.socket": cockpit,
        "overlap/" + VENDOR + "cockpit.socket": cockpit,
        "overlap/" + VENDOR + "ssh.socket": ssh,
        "after/" + VENDOR + "ssh.socket": ssh,
        "after/" + OVERRIDE: override(9090),
        "notices/cockpit-ws.txt": cockpit_notice,
        "notices/openssh-server.txt": ssh_notice,
        "provenance.json": (json.dumps({"packages": sources, "experiment_overlay": OVERRIDE}, indent=2) + "\n").encode(),
    }
    output.mkdir(parents=True, exist_ok=True)
    return {name: write_bundle(output / name, {**common, "overlap/" + OVERRIDE: override(port)})
            for name, port in (("debian-bad.zip", 9090), ("debian-fixed.zip", 19091))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--packages-dir", type=Path, help="Use local copies of the locked .deb files and OpenSSH notice")
    args = parser.parse_args()
    try:
        for name, digest in build(args.out, args.packages_dir).items():
            print(f"{digest}  {name}")
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        parser.exit(3, f"{error}\n")


if __name__ == "__main__":
    main()
