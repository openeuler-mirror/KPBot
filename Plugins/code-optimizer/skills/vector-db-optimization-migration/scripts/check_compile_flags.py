#!/usr/bin/env python3
"""Check macro/flag consistency across translation units.

Parses compile_commands.json and verifies that required macros/flags are present
in every selected entry, and (optionally) that selected flags are uniform.

Typical use:
    python3 check_compile_flags.py \
      --compile-commands build/compile_commands.json \
      --require-macro <MACRO=VALUE> \
      --require-flag=-march=armv8.2-a+sve

Note: flags beginning with '-' must use the '--require-flag=-march=...' form
(equals sign) so argparse does not treat them as another option.

Exit code is 1 when a required macro/flag is missing or --strict-uniform finds
different values for a uniform flag.
"""

import argparse
import json
import os
import shlex
import sys
from collections import OrderedDict, defaultdict

INTERESTING_PREFIXES = (
    "-D",
    "-U",
    "-march",
    "-mcpu",
    "-mtune",
    "-mabi",
    "-msve-vector-bits",
    "-flto",
    "-fopenmp",
    "-ffast-math",
    "-fno-fast-math",
)

UNIFORM_FLAGS_DEFAULT = ("-march", "-mcpu", "-mtune", "-mabi", "-flto", "-fopenmp", "-msve-vector-bits")


def load_entries(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "commands" in data:
        data = data["commands"]
    if not isinstance(data, list):
        raise ValueError("compile_commands.json must contain a list")
    return data


def entry_argv(entry):
    if "arguments" in entry and entry["arguments"]:
        return list(entry["arguments"])
    command = entry.get("command") or entry.get("arguments")
    if not command:
        return []
    if isinstance(command, list):
        return command
    return shlex.split(command)


def normalize_flag(token):
    """Return a canonical key for flags we care about, else None."""
    if token.startswith("-D") and len(token) > 2:
        name_value = token[2:]
        if "=" in name_value:
            name, value = name_value.split("=", 1)
        else:
            name, value = name_value, "1"
        return ("macro", name, value)
    for prefix in ("-march", "-mcpu", "-mtune", "-mabi", "-msve-vector-bits"):
        if token == prefix:
            return ("flag", prefix, "")
        if token.startswith(prefix + "="):
            return ("flag", prefix, token.split("=", 1)[1])
    if token in ("-flto", "-fopenmp", "-ffast-math", "-fno-fast-math"):
        return ("flag", token, "1")
    if token.startswith("-flto=") or token.startswith("-fopenmp="):
        key, value = token.split("=", 1)
        return ("flag", key, value)
    return None


def parse_entry(entry):
    macros = {}
    flags = {}
    for token in entry_argv(entry):
        item = normalize_flag(token)
        if item is None:
            continue
        kind, name, value = item
        if kind == "macro":
            macros[name] = value
        else:
            flags[name] = value
    return macros, flags


def flag_display(key, value):
    if key in ("-march", "-mcpu", "-mtune", "-mabi", "-msve-vector-bits"):
        return "%s=%s" % (key, value)
    if key in ("-flto", "-fopenmp") and value not in ("1", ""):
        return "%s=%s" % (key, value)
    return key


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compile-commands", required=True)
    ap.add_argument("--path-filter", action="append", default=[], help="substring filter on file path (repeatable)")
    ap.add_argument("--require-macro", action="append", default=[], help="NAME or NAME=VALUE (repeatable)")
    ap.add_argument("--require-flag", action="append", default=[], help="exact flag/token, e.g. -march=armv8.2-a+sve (repeatable)")
    ap.add_argument("--uniform-flag", action="append", default=None, help="flag key that must be uniform (repeatable)")
    ap.add_argument("--strict-uniform", action="store_true", help="fail if uniform flags differ across entries")
    ap.add_argument("--json", help="write machine-readable report to this path")
    ap.add_argument("--max-report", type=int, default=20)
    args = ap.parse_args(argv)

    try:
        entries = load_entries(args.compile_commands)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    selected = []
    for entry in entries:
        file_path = entry.get("file") or ""
        directory = entry.get("directory") or ""
        full = file_path if os.path.isabs(file_path) else os.path.join(directory, file_path)
        if args.path_filter and not any(p in full for p in args.path_filter):
            continue
        macros, flags = parse_entry(entry)
        selected.append({"file": full, "macros": macros, "flags": flags})

    if not selected:
        print("error: no compile_commands entries matched the filter", file=sys.stderr)
        return 2

    required_macros = []
    for spec in args.require_macro:
        if "=" in spec:
            name, value = spec.split("=", 1)
        else:
            name, value = spec, None
        required_macros.append((name, value))

    required_flags = []
    for spec in args.require_flag:
        item = normalize_flag(spec)
        if item is None:
            print("error: unsupported --require-flag %r" % spec, file=sys.stderr)
            return 2
        required_flags.append(item)

    if args.uniform_flag is None:
        uniform_flags = list(UNIFORM_FLAGS_DEFAULT)
    else:
        uniform_flags = list(args.uniform_flag)

    missing = []
    for rec in selected:
        for name, value in required_macros:
            actual = rec["macros"].get(name)
            if actual is None or (value is not None and actual != value):
                missing.append(
                    {
                        "file": rec["file"],
                        "kind": "macro",
                        "expected": name if value is None else "%s=%s" % (name, value),
                        "actual": actual,
                    }
                )
        for kind, name, value in required_flags:
            actual = rec["flags"].get(name)
            expected = flag_display(name, value)
            ok = actual is not None and (value in ("", "1") or actual == value)
            if not ok:
                missing.append(
                    {
                        "file": rec["file"],
                        "kind": "flag",
                        "expected": expected,
                        "actual": None if actual is None else flag_display(name, actual),
                    }
                )

    uniform_values = {}
    for key in uniform_flags:
        counter = defaultdict(list)
        for rec in selected:
            value = rec["flags"].get(key)
            if value is not None:
                counter[value].append(rec["file"])
        uniform_values[key] = OrderedDict(
            (k, v) for k, v in sorted(counter.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        )

    inconsistent = []
    for key, values in uniform_values.items():
        if len(values) > 1:
            inconsistent.append(
                {
                    "flag": key,
                    "values": [
                        {"value": flag_display(key, v), "count": len(files), "files": files[: args.max_report]}
                        for v, files in values.items()
                    ],
                }
            )

    macro_sets = defaultdict(int)
    for rec in selected:
        key = tuple(sorted(rec["macros"].items()))
        macro_sets[key] += 1

    result = {
        "compile_commands": args.compile_commands,
        "entries_total": len(entries),
        "entries_selected": len(selected),
        "path_filters": args.path_filter,
        "required_macros": ["%s=%s" % (n, v) if v is not None else n for n, v in required_macros],
        "required_flags": [flag_display(n, v) for _, n, v in required_flags],
        "missing": missing,
        "uniform_flags": uniform_flags,
        "uniform_values": uniform_values,
        "inconsistent": inconsistent,
        "distinct_macro_sets": len(macro_sets),
        "macro_set_entries": [
            {"count": count, "macros": ["%s=%s" % (n, v) for n, v in set_items]}
            for set_items, count in sorted(macro_sets.items(), key=lambda kv: -kv[1])
        ][: args.max_report],
    }

    print(
        "selected=%d/%d distinct_macro_sets=%d missing=%d inconsistent_uniform=%d"
        % (len(selected), len(entries), len(macro_sets), len(missing), len(inconsistent))
    )

    for item in missing[: args.max_report]:
        print(
            "  MISSING %s %s in %s (actual=%s)"
            % (item["kind"], item["expected"], item["file"], item["actual"])
        )
    for item in inconsistent:
        print("  INCONSISTENT %s: %s" % (item["flag"], ", ".join(x["value"] for x in item["values"])))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("report_json=%s" % args.json)

    if missing:
        return 1
    if args.strict_uniform and inconsistent:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
