#!/usr/bin/env python3
"""Verify archives against an official SHA512 sidecar before use.

Official ClickHouse release assets publish one ``<name>.tgz.sha512``
sidecar per archive (for example ``clickhouse-common-static-<version>-<arch>
.tgz.sha512`` on https://github.com/ClickHouse/ClickHouse/releases and
https://packages.clickhouse.com/tgz/). Each sidecar line has the standard
coreutils form ``<128 hex digits>  <file name>``. This verifier streams the
local archive and requires the digest to match the entry that names exactly
that archive file; it never executes or extracts unverified content.
"""

import argparse
import hashlib
from pathlib import Path
import re
import sys

CHUNK_SIZE = 1024 * 1024
ENTRY_RE = re.compile(r"^([0-9a-fA-F]{128})[ \t]+\*?(.+?)\s*$")


def parse_sidecar(text):
    """Map base file names to their published SHA-512 digests."""
    entries = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENTRY_RE.match(stripped)
        if not match:
            raise ValueError(
                f"checksum line {line_number} is not '<sha512>  <name>'"
            )
        digest, name = match.groups()
        entries[Path(name.strip()).name] = digest.lower()
    return entries


def file_sha512(path):
    """Stream one archive through SHA-512 without loading it whole."""
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify(archive, sidecar):
    """Return (ok, message) for one archive and its official sidecar."""
    try:
        text = Path(sidecar).read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read checksum file: {exc}"
    try:
        entries = parse_sidecar(text)
    except ValueError as exc:
        return False, str(exc)
    name = Path(archive).name
    expected = entries.get(name)
    if expected is None:
        return False, f"no checksum entry names {name}"
    try:
        actual = file_sha512(Path(archive))
    except OSError as exc:
        return False, f"cannot read archive: {exc}"
    if actual != expected:
        return False, f"SHA-512 mismatch for {name}"
    return True, f"SHA-512 verified for {name}"


def main(argv=None):
    """Check every archive and fail closed before any later use."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checksums",
        type=Path,
        required=True,
        help="official <name>.tgz.sha512 sidecar downloaded with the archive",
    )
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)

    for archive in args.archives:
        if not archive.is_file():
            print(f"error: archive is not a regular file: {archive}")
            return 2
    failed = False
    for archive in args.archives:
        ok, message = verify(archive, args.checksums)
        print(("ok: " if ok else "error: ") + message)
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
