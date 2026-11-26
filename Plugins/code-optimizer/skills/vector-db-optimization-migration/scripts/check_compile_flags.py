#!/usr/bin/env python3
"""Check macro/flag consistency across translation units.

Parses compile_commands.json and verifies that required macros/flags are
present in every selected entry, and that uniform flags are consistent.

Parsing semantics (aligned with how the compiler reads its own command
line, in argument order):

- macros: `-DNAME=VALUE` defines, `-UNAME` removes an earlier definition
  on the same command line; both space-separated forms (`-D NAME=VALUE`,
  `-U NAME`) are recognized too. `--require-macro NAME=VALUE` therefore
  fails when any selected TU cancels the macro with -U.
- flags: `-march=value` and the space-separated `-march value` forms are
  equivalent; a later occurrence overrides an earlier one.
- negative boolean flags: `-fno-lto`, `-fno-openmp`, `-fno-fast-math`
  map onto the positive key with value "0", so `-flto -fno-lto` leaves
  LTO explicitly OFF and `--require-flag=-flto` fails for that TU.
- uniformity: a flag that is missing from some selected TUs counts as
  its own "<absent>" value — one TU with `-march` and one without is
  reported (and by default fails) as non-uniform.

Exit code is 1 when a required macro/flag is missing, or when uniform
flags are non-uniform (unless --allow-inconsistent is given). Use
--path-filter to restrict the check to the TUs that actually build the
migrated code; running over the whole project typically reports
irrelevant non-uniformity (e.g. plain C files without -march).

Typical use:
    python3 check_compile_flags.py \
      --compile-commands build/compile_commands.json \
      --path-filter faiss/ \
      --require-macro <MACRO=VALUE> \
      --require-flag=-march=armv8.2-a+sve

Note: flags beginning with '-' must use the '--require-flag=-march=...' form
(equals sign) so argparse does not treat them as another option.
"""

import argparse
import json
import os
import shlex
import sys
from collections import OrderedDict, defaultdict

UNIFORM_FLAGS_DEFAULT = ("-march", "-mcpu", "-mtune", "-mabi", "-flto", "-fopenmp", "-msve-vector-bits")

SPACE_VALUE_FLAGS = ("-march", "-mcpu", "-mtune", "-mabi", "-msve-vector-bits")

# Boolean-style flags: "-flto" sets the key to "1", the matching negative
# form ("-fno-lto") sets the SAME key to "0", so `-flto -fno-lto` leaves
# the flag explicitly off, and `-flto` in one TU vs `-fno-lto` in another
# is a non-uniform pair.
BOOL_FLAGS = {
    "-flto": "-fno-lto",
    "-fopenmp": "-fno-openmp",
    "-ffast-math": "-fno-fast-math",
}

ABSENT = "<absent>"


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


def define_macro(macros, name_value):
    if "=" in name_value:
        name, value = name_value.split("=", 1)
    else:
        name, value = name_value, "1"
    macros[name] = value


def normalize_flag(token):
    """Return a canonical (kind, key, value) for flags we care about, else None.

    Negative boolean flags map onto the positive key with value "0"
    (e.g. -fno-lto -> ("flag", "-flto", "0")) so an explicit disable is
    distinguishable from both "on" and "absent".
    """
    if token.startswith("-D") and len(token) > 2:
        return ("macro",) + split_name_value(token[2:])
    for prefix in SPACE_VALUE_FLAGS:
        if token == prefix:
            return ("flag", prefix, "")
        if token.startswith(prefix + "="):
            return ("flag", prefix, token.split("=", 1)[1])
    for positive, negative in BOOL_FLAGS.items():
        if token == positive:
            return ("flag", positive, "1")
        if token == negative:
            return ("flag", positive, "0")
    if token.startswith("-flto=") or token.startswith("-fopenmp="):
        key, value = token.split("=", 1)
        return ("flag", key, value)
    return None


def split_name_value(name_value):
    if "=" in name_value:
        name, value = name_value.split("=", 1)
    else:
        name, value = name_value, "1"
    return (name, value)


def parse_entry(entry):
    """Compute effective macros and flags, honoring argument order.

    - -D defines a macro ("-DNAME=V" or "-D NAME=V"), -U removes it
      ("-UNAME" or "-U NAME"); later tokens win.
    - Space-separated "-march value" pairs are folded into one flag.
    - Repeated flags keep the last occurrence (compiler override order).
    """
    argv = entry_argv(entry)
    macros = {}
    flags = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if token == "-U":
            # space-separated form: -U NAME
            if nxt is not None and not nxt.startswith("-"):
                macros.pop(nxt, None)
                i += 1
        elif token == "-D":
            # space-separated form: -D NAME[=VALUE]
            if nxt is not None and not nxt.startswith("-"):
                define_macro(macros, nxt)
                i += 1
        elif token.startswith("-U") and len(token) > 2:
            macros.pop(token[2:], None)
        elif token.startswith("-D") and len(token) > 2:
            define_macro(macros, token[2:])
        elif token in SPACE_VALUE_FLAGS:
            if nxt is not None and not nxt.startswith("-"):
                flags[token] = nxt
                i += 1  # consume the value token
            else:
                flags[token] = ""
        else:
            item = normalize_flag(token)
            if item is not None:
                kind, name, value = item
                flags[name] = value
        i += 1
    return macros, flags


def flag_display(key, value):
    if value is None:
        return ABSENT
    if key in SPACE_VALUE_FLAGS:
        return "%s=%s" % (key, value)
    if key in BOOL_FLAGS:
        if value == "0":
            return BOOL_FLAGS[key]  # e.g. -fno-lto
        if value == "1":
            return key
        return "%s=%s" % (key, value)  # e.g. -flto=thin
    return key


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compile-commands", required=True)
    ap.add_argument("--path-filter", action="append", default=[], help="substring filter on file path (repeatable); restrict to the TUs that build the migrated code")
    ap.add_argument("--require-macro", action="append", default=[], help="NAME or NAME=VALUE (repeatable); fails if a TU defines the macro with a different value or cancels it with -U")
    ap.add_argument("--require-flag", action="append", default=[], help="exact flag/token, e.g. -march=armv8.2-a+sve, -flto or -fno-lto (repeatable)")
    ap.add_argument("--uniform-flag", action="append", default=None, help="flag key that must be uniform, absence counts as a value (repeatable)")
    ap.add_argument("--allow-inconsistent", action="store_true", help="only report non-uniform flags; exit code stays 0 (default: non-uniform flags exit 1)")
    ap.add_argument("--strict-uniform", action="store_true", help="(deprecated no-op: non-uniform flags already fail by default; kept for compatibility)")
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
        name, value = split_name_value(spec) if "=" in spec else (spec, None)
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
            # exact match: "-flto" requires on ("1"), "-fno-lto" requires
            # explicitly off ("0"); "-flto=thin" does not satisfy "-flto".
            if actual is None or actual != value:
                missing.append(
                    {
                        "file": rec["file"],
                        "kind": "flag",
                        "expected": flag_display(name, value),
                        "actual": None if actual is None else flag_display(name, actual),
                    }
                )

    # Uniformity: absence is its own value bucket, so "one TU has -march,
    # another does not" is reported (and fails by default) as non-uniform.
    uniform_values = {}
    for key in uniform_flags:
        counter = defaultdict(list)
        for rec in selected:
            value = rec["flags"].get(key)
            display = flag_display(key, value)
            counter[display].append(rec["file"])
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
                        {"value": v, "count": len(files), "files": files[: args.max_report]}
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
        print("exit=1: required macro/flag missing or cancelled (-U) in some selected TU")
        return 1
    if inconsistent and not args.allow_inconsistent:
        print("exit=1: uniform flags are non-uniform (use --allow-inconsistent to report only)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
