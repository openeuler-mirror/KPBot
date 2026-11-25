#!/usr/bin/env python3
"""Check selected Kconfig or boot observations, live on Linux or from a capture."""

import argparse
import gzip
import json
from pathlib import Path
import re
import struct
import sys
import uuid


def evidence_path(root, relative):
    """Keep an archived evidence reference inside the selected capture or run."""
    if Path(relative).is_absolute():
        raise ValueError("expected a relative evidence path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError(f"evidence path leaves the selected directory: {relative}") from None
    if path.exists() and not path.is_file():
        raise ValueError(f"expected an evidence file: {relative}")
    return path


def settings(path):
    open_file = gzip.open if path.suffix == ".gz" else open
    values = {}
    with open_file(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            disabled = re.fullmatch(r"# (CONFIG_\w+) is not set", line)
            enabled = re.fullmatch(r"(CONFIG_\w+)=(.+)", line)
            if disabled:
                key, value = disabled[1], "n"
            elif enabled:
                key, value = enabled.groups()
            elif not line or line.startswith("#"):
                continue
            else:
                raise ValueError(f"{path}:{number}: invalid Kconfig assignment")
            if key in values and values[key] != value:
                raise ValueError(f"{path}:{number}: conflicting values for {key}")
            values[key] = value
    return values


def check_settings(actual_path, fragments, checks):
    expected = {}
    for fragment in fragments:
        values = settings(fragment)
        if not values:
            raise ValueError(f"empty expectation fragment: {fragment}")
        for key, value in values.items():
            if key in expected and expected[key] != value:
                raise ValueError(f"conflicting expectations for {key}")
            expected[key] = value
    actual = settings(actual_path)
    for key, value in expected.items():
        checks[key] = {"expected": value, "actual": actual.get(key)}


def kernel_build_id(path):
    # /sys/kernel/notes is a sequence of raw ELF notes, not an ELF file.
    data = path.read_bytes()
    for order in ("<", ">"):
        pos = 0
        while pos + 12 <= len(data):
            namesz, descsz, kind = struct.unpack_from(order + "III", data, pos)
            name = pos + 12
            desc = name + ((namesz + 3) & ~3)
            pos = desc + ((descsz + 3) & ~3)
            if pos > len(data):
                break
            if kind == 3 and data[name:name + namesz] == b"GNU\0" and descsz:
                return data[desc:desc + descsz].hex()
    raise ValueError(f"no GNU build ID in {path}")


def boot_parameters(cmdline):
    # Linux next_arg groups double-quoted text. Single quotes and backslashes
    # are ordinary characters. Arguments after -- belong to init.
    tokens = set()
    for match in re.finditer(r'(?:[^\s"]+|"[^"]*(?:"|$))+', cmdline.strip()):
        token = match.group()
        if token.startswith('"'):
            token = token[1:]
            if token.endswith('"'):
                token = token[:-1]
        name, sep, value = token.partition("=")
        if name == "--" and not sep:
            break
        if value.startswith('"'):
            value = value[1:]
            if value.endswith('"'):
                value = value[:-1]
        tokens.add(name + sep + value)
    return tokens


def check_boot(args, result):
    checks = result["checks"]
    root = args.root.resolve(strict=True)
    release = evidence_path(root, "proc/sys/kernel/osrelease").read_text().strip()
    boot_file = evidence_path(root, "proc/sys/kernel/random/boot_id")
    boot_id = str(uuid.UUID(boot_file.read_text().strip()))
    result["boot_id"] = boot_id
    checks["kernel_release"] = {"expected": args.release, "actual": release}
    if args.previous_boot_id:
        previous = str(uuid.UUID(args.previous_boot_id))
        result["previous_boot_id"] = previous
        checks["new_boot"] = {"expected": True, "actual": previous != boot_id}
    if args.build_id:
        checks["build_id"] = {"expected": args.build_id.lower(),
                              "actual": kernel_build_id(evidence_path(root, "sys/kernel/notes"))}
    if args.has_arg or args.lacks_arg:
        tokens = boot_parameters(evidence_path(root, "proc/cmdline").read_text())
        for arg in args.has_arg + args.lacks_arg:
            checks[f"cmdline:{arg}"] = {"expected": arg in args.has_arg, "actual": arg in tokens}
    if args.expect:
        config = evidence_path(root, "proc/config.gz")
        if not config.is_file():
            if not release or Path(release).name != release or release in (".", ".."):
                raise ValueError("invalid observed kernel release")
            config = evidence_path(root, f"boot/config-{release}")
        result["config_source"] = config.relative_to(root).as_posix()
        check_settings(config, args.expect, checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    config = modes.add_parser("config", help="check a resolved .config against requested selections")
    config.add_argument("--config", type=Path, required=True)
    config.add_argument("--expect", type=Path, action="append", required=True, metavar="FRAGMENT")
    boot = modes.add_parser("boot", help="check observations collected in the target kernel")
    boot.add_argument("--release", required=True, help="expected exact kernelrelease")
    boot.add_argument("--previous-boot-id", help="require a different boot from this UUID")
    boot.add_argument("--build-id", help="expected GNU build ID in hexadecimal")
    boot.add_argument("--expect", type=Path, action="append", default=[], metavar="FRAGMENT")
    boot.add_argument("--has-arg", action="append", default=[], help="required command-line token")
    boot.add_argument("--lacks-arg", action="append", default=[], help="excluded command-line token")
    boot.add_argument("--root", type=Path, default=Path("/"), help="live / or root of a captured file tree")
    boot.add_argument("--run-id", help="run that collected this boot")
    boot.add_argument("--target-id", help="host or sandbox that supplied this capture")
    args = parser.parse_args()
    checks = {}
    result = {"scope": args.mode, "checks": checks}
    try:
        if args.mode == "config":
            check_settings(args.config, args.expect, checks)
        else:
            if args.run_id or args.target_id:
                if not (args.run_id and args.target_id):
                    raise ValueError("--run-id and --target-id must be supplied together")
                result.update(run_id=args.run_id, target_id=args.target_id)
            if set(args.has_arg) & set(args.lacks_arg):
                raise ValueError("a boot parameter is both required and excluded")
            check_boot(args, result)
        matched = all(c["expected"] == c["actual"] for c in checks.values())
        result["status"] = "matched" if matched else "unverified"
    except (OSError, EOFError, ValueError, RuntimeError) as error:
        result.update(status="unverified", error=str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "matched" else 1


if __name__ == "__main__":
    sys.exit(main())
