#!/usr/bin/env python3
"""Check source-only agent findings; never build or execute the input sources."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys


HERE = Path(__file__).resolve().parent


def fixture_name(path, source_root=None):
    """Resolve to one of these actual fixtures, never a same-named substitute."""
    if not isinstance(path, str) or not path:
        return None
    supplied = Path(path)
    if supplied.is_absolute() or supplied.anchor:
        candidates = [supplied]
    else:
        candidates = [HERE / supplied]
        if isinstance(source_root, str) and source_root:
            candidates.append(Path(source_root) / supplied)
    allowed = {(HERE / name).resolve(): name for name in ("fixture.rs", "fixture.c")}
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if resolved in allowed:
            return allowed[resolved]
    return None


def evidence_matches(source, symbol, start, end):
    """Allow exact references inside the simple functions in these fixtures.

    This is a fixture checker, not a general C/Rust parser. Mask comments and
    string literals while retaining line positions before finding brace scopes.
    """
    span = "\n".join(source.splitlines()[start - 1:end])
    if re.search(r"\b" + re.escape(symbol) + r"\b", span):
        return True
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: re.sub(r"[^\n]", " ", match.group()), source)
    definition = re.compile(r"\b" + re.escape(symbol) +
                            r"\s*\([^;{}]*\)[^;{}]*\{")
    for match in definition.finditer(masked):
        depth = 1
        position = match.end()
        while depth and position < len(masked):
            depth += (masked[position] == "{") - (masked[position] == "}")
            position += 1
        if depth:
            continue
        first_line = masked.count("\n", 0, match.start()) + 1
        last_line = masked.count("\n", 0, position - 1) + 1
        if first_line <= start <= end <= last_line:
            return True
    return False


def validate(report):
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    require(isinstance(report, dict), "report must be a JSON object")
    if not isinstance(report, dict):
        return errors
    require(report.get("schema_version") == "prefetch-policy/v1", "wrong schema_version")
    require(report.get("status") in ("ok", "partial"),
            "source-only analysis may be complete (ok) or partial")
    target = report.get("target", {})
    target_fields = {"source_root", "scope", "architectures", "commit", "dirty", "build_context"}
    require(isinstance(target, dict) and target_fields.issubset(target),
            "target must include source_root, scope, architectures, commit, dirty, build_context")
    if not isinstance(target, dict):
        return errors
    for key in ("source_root", "scope"):
        require(isinstance(target.get(key), str) and bool(target[key].strip()),
                "target." + key + " must describe the supplied source scope")
    require(target.get("commit") is None or isinstance(target["commit"], str),
            "target.commit must be a string or null")
    require(target.get("dirty") is None or type(target["dirty"]) is bool,
            "target.dirty must be a boolean or null")
    require(target.get("build_context") is None or isinstance(target["build_context"], (str, dict)),
            "target.build_context must be a string, object or null")
    require("artifacts" in report and report["artifacts"] == [],
            "source-only case requires artifacts: []")
    source_root = target.get("source_root") if isinstance(target, dict) else None
    architectures = target.get("architectures", []) if isinstance(target, dict) else []
    require(isinstance(architectures, list) and
            {"x86_64", "aarch64"}.issubset(architectures), "missing target architectures")
    require(isinstance(report.get("limitations"), list) and len(report["limitations"]) > 0,
            "limitations must describe the source-only evidence scope")
    require(isinstance(report.get("required_evidence"), list),
            "required_evidence must be a list; a completed source analysis may use []")
    findings = report.get("findings", [])
    require(isinstance(findings, list) and len(findings) > 0, "findings must be nonempty")
    if not isinstance(findings, list):
        return errors

    source_lines = {name: (HERE / name).read_text(encoding="utf-8").splitlines()
                    for name in ("fixture.rs", "fixture.c")}
    sources = report.get("source_files")
    require(isinstance(sources, list), "source_files must be a list of fixture paths and digests")
    covered_sources = set()
    for source in sources if isinstance(sources, list) else []:
        name = fixture_name(source.get("path"), source_root) if isinstance(source, dict) else None
        require(name is not None, "source_files path does not resolve to an actual fixture")
        if name:
            covered_sources.add(name)
            require(source.get("sha256") == hashlib.sha256((HERE / name).read_bytes()).hexdigest(),
                    "source_files SHA-256 differs from the actual fixture")
    require(covered_sources == {"fixture.rs", "fixture.c"},
            "source_files must identify and hash both fixtures")
    for index, finding in enumerate(findings):
        label = "finding " + str(index)
        require(isinstance(finding, dict), label + " must be an object")
        if not isinstance(finding, dict):
            continue
        require(finding.get("binary_state") == "unavailable", label + ": no binary was supplied")
        require(finding.get("runtime_state") == "unverified", label + ": no runtime evidence was supplied")
        require(isinstance(finding.get("id"), str) and bool(finding["id"]), label + ": missing id")
        if finding.get("source_state") == "noop":
            distance = finding.get("distance")
            require(isinstance(distance, dict) and
                    {"kind", "value", "unit"}.issubset(distance) and
                    distance["kind"] == "unknown" and distance["value"] is None and
                    distance["unit"] == "unknown",
                    label + ": no-op distance must be kind=unknown, value=null, unit=unknown")
        evidence = finding.get("evidence", [])
        require(isinstance(evidence, list) and len(evidence) > 0, label + ": missing evidence")
        if not isinstance(evidence, list):
            continue
        for ref in evidence:
            if not isinstance(ref, dict):
                errors.append(label + ": malformed evidence")
                continue
            name = fixture_name(ref.get("path", ""), source_root)
            require(name is not None, label + ": evidence path does not resolve to an actual fixture")
            lines = ref.get("lines")
            valid_range = (name in source_lines and isinstance(lines, list) and
                           len(lines) == 2 and all(type(x) is int for x in lines) and
                           1 <= lines[0] <= lines[1] <= len(source_lines[name]))
            require(valid_range, label + ": invalid fixture line range")
            require(isinstance(ref.get("symbol"), str) and bool(ref["symbol"]),
                    label + ": missing evidence symbol")
            if valid_range and isinstance(ref.get("symbol"), str):
                require(evidence_matches("\n".join(source_lines[name]), ref["symbol"],
                                         lines[0], lines[1]),
                        label + ": evidence span is outside the cited symbol scope")
        # These fixed fixture locations cover the helper call and subsequent
        # data access; prose describing their meaning still needs manual review.
        callers = {
            "prefetch_l1": ("fixture.rs", "harvest_probe", (34, 36)),
            "prefetch_l2": ("fixture.rs", "harvest_probe", (29, 36)),
            "local_read_hint": ("fixture.c", "read_one", (15, 16)),
            "write_hint": ("fixture.c", "write_one", (20, 21)),
        }
        symbols = {ref.get("symbol") for ref in evidence if isinstance(ref, dict)}
        for helper in symbols.intersection(callers):
            name, caller, locations = callers[helper]
            ranges = [ref["lines"] for ref in evidence if isinstance(ref, dict) and
                      ref.get("symbol") == caller and
                      fixture_name(ref.get("path"), source_root) == name and
                      isinstance(ref.get("lines"), list) and len(ref["lines"]) == 2 and
                      all(type(line) is int for line in ref["lines"])]
            require(all(any(start <= line <= end for start, end in ranges) for line in locations),
                    label + ": missing " + caller + " call/consume evidence for " + helper)

    def matching(symbol, architecture=None):
        return [item for item in findings if isinstance(item, dict) and
                (architecture is None or item.get("architecture") == architecture) and
                isinstance(item.get("evidence"), list) and
                any(isinstance(ref, dict) and ref.get("symbol") == symbol
                    for ref in item["evidence"])]

    def find_one(symbol, architecture, predicate, message):
        matches = matching(symbol, architecture)
        require(any(predicate(item) for item in matches), message)

    def reject_conflicts(symbol, architecture, predicate, message):
        # These fixtures have one call site per helper. A correct row cannot
        # hide another row that asserts an incompatible policy for that helper.
        require(all(predicate(item) for item in matching(symbol, architecture)), message)

    find_one("prefetch_l2", "x86_64",
             lambda x: x.get("source_state") == "implemented" and
             x.get("level_hint") == "L2" and x.get("access") == "read" and
             x.get("distance", {}).get("kind") == "phase" and
             x.get("distance", {}).get("value") is None,
             "x86_64 L2 harvest-phase read hint is missing")
    find_one("prefetch_l2", "aarch64", lambda x: x.get("source_state") == "noop",
             "aarch64 prefetch_l2 must be reported as source no-op")
    reject_conflicts("prefetch_l2", "aarch64",
                     lambda x: x.get("source_state") in ("noop", "unknown"),
                     "conflicting aarch64 prefetch_l2 source state")
    reject_conflicts("prefetch_l2", "x86_64",
                     lambda x: x.get("source_state") in ("implemented", "unknown") and
                     x.get("level_hint") in ("L2", "unknown") and
                     x.get("access") in ("read", "unknown") and
                     x.get("distance", {}).get("value") is None,
                     "conflicting x86_64 prefetch_l2 policy")
    for architecture in ("x86_64", "aarch64"):
        find_one("prefetch_l1", architecture,
                 lambda x: x.get("source_state") == "implemented" and
                 x.get("level_hint") == "L1" and x.get("access") == "read" and
                 x.get("distance", {}).get("kind") == "fixed" and
                 x.get("distance", {}).get("value") == 16 and
                 x.get("distance", {}).get("unit") in ("entries", "items") and
                 any(ref.get("symbol") == "harvest_probe" for ref in x["evidence"]),
                 architecture + ": L1 must cite caller and report 16 entries/items")
        reject_conflicts("prefetch_l1", architecture,
                         lambda x: x.get("source_state") in ("implemented", "unknown") and
                         x.get("level_hint") in ("L1", "unknown") and
                         x.get("access") in ("read", "unknown") and
                         (x.get("distance", {}).get("kind") == "unknown" or
                          (x.get("distance", {}).get("kind") == "fixed" and
                           x.get("distance", {}).get("value") == 16 and
                           x.get("distance", {}).get("unit") in ("entries", "items"))),
                         architecture + ": conflicting L1 policy or distance")
        find_one("local_read_hint", architecture,
                 lambda x: x.get("source_state") == "implemented" and
                 x.get("level_hint") == "unknown" and x.get("access") == "read",
                 architecture + ": builtin read hint needs explicit coverage, cache level unknown")
    reject_conflicts("local_read_hint", None,
                     lambda x: x.get("level_hint") == "unknown" and
                     x.get("access") in ("read", "unknown"),
                     "conflicting builtin locality policy")
    find_one("write_hint", "aarch64",
             lambda x: x.get("source_state") == "implemented" and
             x.get("level_hint") == "L1" and x.get("access") == "write",
             "AArch64 PSTL1KEEP must be identified as a write hint")
    reject_conflicts("write_hint", "aarch64",
                     lambda x: x.get("source_state") in ("implemented", "unknown") and
                     x.get("level_hint") in ("L1", "unknown") and
                     x.get("access") in ("write", "unknown"),
                     "conflicting AArch64 write_hint policy")
    find_one("write_hint", "x86_64", lambda x: x.get("source_state") == "noop",
             "x86_64 write_hint must be reported as source no-op")
    reject_conflicts("write_hint", "x86_64",
                     lambda x: x.get("source_state") in ("noop", "unknown"),
                     "conflicting x86_64 write_hint source state")
    return errors


def validate_markdown(report, markdown):
    """Check presence and finding IDs only; prose equivalence is a manual gate."""
    if not isinstance(markdown, str) or not markdown.strip():
        return ["Markdown report is absent or empty"]
    errors = []
    for finding in report.get("findings", []):
        identity = finding.get("id") if isinstance(finding, dict) else None
        if isinstance(identity, str) and identity and not re.search(
                r"(?<![\w-])" + re.escape(identity) + r"(?![\w-])", markdown):
            errors.append("Markdown report is missing finding ID: " + identity)
    return errors


def self_test():
    # Synthetic reports exercise the verifier, not an LLM or real hardware.
    def evidence(path, symbol, start, end):
        return {"path": path, "symbol": symbol, "lines": [start, end]}

    def finding(symbol, arch, state, level, access, distance, refs):
        result = {"id": symbol + "-" + arch, "architecture": arch,
                  "source_state": state, "level_hint": level, "access": access,
                  "binary_state": "unavailable", "runtime_state": "unverified",
                  "distance": distance, "evidence": refs}
        if state == "noop":
            result["notes"] = "No effective prefetch in this branch; distance is not applicable."
        return result

    report = {"schema_version": "prefetch-policy/v1", "status": "partial",
              "target": {"architectures": ["x86_64", "aarch64"], "source_root": str(HERE),
                         "scope": "harvest_probe, read_one, write_one; source only",
                         "commit": None, "dirty": None, "build_context": None},
              "artifacts": [],
              "source_files": [{"path": name,
                                "sha256": hashlib.sha256((HERE / name).read_bytes()).hexdigest()}
                               for name in ("fixture.rs", "fixture.c")],
              "limitations": ["Only source was supplied."],
              "required_evidence": ["Matching build and runtime evidence."], "findings": []}
    for arch in ("x86_64", "aarch64"):
        report["findings"].append(finding("prefetch_l1", arch, "implemented", "L1", "read",
            {"value": 16, "unit": "entries", "kind": "fixed"},
            [evidence("fixture.rs", "prefetch_l1", 11, 17),
             evidence("fixture.rs", "harvest_probe", 27, 39)]))
        report["findings"].append(finding("prefetch_l2", arch,
            "implemented" if arch == "x86_64" else "noop", "L2", "read",
            ({"value": None, "unit": "phase", "kind": "phase"} if arch == "x86_64" else
             {"value": None, "unit": "unknown", "kind": "unknown"}),
            [evidence("fixture.rs", "prefetch_l2", 20, 25),
             evidence("fixture.rs", "harvest_probe", 27, 39)]))
    report["findings"].append(finding("local_read_hint", "x86_64", "implemented", "unknown", "read",
        {"value": None, "unit": "unknown", "kind": "unknown"},
        [evidence("fixture.c", "local_read_hint", 2, 4),
         evidence("fixture.c", "read_one", 14, 17)]))
    report["findings"].append(finding("write_hint", "aarch64", "implemented", "L1", "write",
        {"value": None, "unit": "unknown", "kind": "unknown"},
        [evidence("fixture.c", "write_hint", 6, 12),
         evidence("fixture.c", "write_one", 19, 22)]))
    report["findings"].append(finding("local_read_hint", "aarch64", "implemented", "unknown", "read",
        {"value": None, "unit": "unknown", "kind": "unknown"},
        [evidence("fixture.c", "local_read_hint", 2, 4),
         evidence("fixture.c", "read_one", 14, 17)]))
    report["findings"].append(finding("write_hint", "x86_64", "noop", "unknown", "write",
        {"value": None, "unit": "unknown", "kind": "unknown"},
        [evidence("fixture.c", "write_hint", 6, 12),
         evidence("fixture.c", "write_one", 19, 22)]))
    failures = validate(report)
    equivalent = copy.deepcopy(report)
    equivalent["status"] = "ok"
    equivalent["required_evidence"] = []
    for index in (0, 2):
        equivalent["findings"][index]["distance"]["unit"] = "items"
    failures.extend(validate(equivalent))
    precise = copy.deepcopy(equivalent)
    for index in (0, 2):
        precise["findings"][index]["evidence"][1]["lines"] = [32, 36]
    failures.extend(validate(precise))
    for absolute in (False, True):
        located = copy.deepcopy(report)
        located["target"]["source_root"] = str(HERE.parent)
        for item in located["findings"]:
            for ref in item["evidence"]:
                ref["path"] = str(HERE / ref["path"]) if absolute else "source-only/" + ref["path"]
        failures.extend(validate(located))
    mutants = [
        ("wrong-distance-unit", 0, "distance", {"value": 16, "unit": "bytes", "kind": "fixed"}),
        ("arm-noop-misread", 3, "source_state", "implemented"),
        ("locality-as-level", 4, "level_hint", "L2"),
        ("store-as-load", 5, "access", "read"),
        ("unseen-binary", 0, "binary_state", "verified"),
        ("unseen-runtime", 0, "runtime_state", "observed"),
        ("bad-evidence-range", 0, "evidence",
         [evidence("fixture.rs", "prefetch_l1", 500, 600)]),
        ("wrong-symbol-scope", 0, "evidence",
         [evidence("fixture.rs", "prefetch_l1", 11, 17),
          evidence("fixture.rs", "harvest_probe", 21, 24)]),
        ("fixed-phase-distance", 1, "distance", {"value": 64, "unit": "items", "kind": "phase"}),
        ("fake-fixture-path", 0, "evidence",
         [evidence("/not/exist/fixture.rs", "prefetch_l1", 11, 17),
          evidence("fixture.rs", "harvest_probe", 32, 36)]),
    ]
    noop_mutants = [
        ("noop-phase-distance", 3, "distance",
         {"value": None, "unit": "phase", "kind": "phase"}),
        ("noop-fixed-distance", 3, "distance",
         {"value": 16, "unit": "items", "kind": "fixed"}),
        ("noop-nonnull-distance", 3, "distance",
         {"value": 0, "unit": "unknown", "kind": "unknown"}),
        ("noop-distance-unit", 3, "distance",
         {"value": None, "unit": "items", "kind": "unknown"}),
        ("noop-missing-distance-value", 3, "distance",
         {"unit": "unknown", "kind": "unknown"}),
        ("x86-noop-phase-distance", 7, "distance",
         {"value": None, "unit": "phase", "kind": "phase"}),
    ]
    rejected = []
    for name, index, key, value in mutants + noop_mutants:
        mutant = copy.deepcopy(report)
        mutant["findings"][index][key] = value
        if validate(mutant):
            rejected.append(name)
        else:
            failures.append("accepted invalid report: " + name)
    for name, index, key, value in mutants[:4] + noop_mutants[:1]:
        mutant = copy.deepcopy(report)
        conflicting = copy.deepcopy(mutant["findings"][index])
        conflicting["id"] += "-conflicting"
        conflicting[key] = value
        mutant["findings"].append(conflicting)
        if validate(mutant):
            rejected.append("conflicting-" + name)
        else:
            failures.append("accepted conflicting report: " + name)
    for index in (4, 6, 7):
        mutant = copy.deepcopy(report)
        omitted = mutant["findings"].pop(index)["id"]
        if validate(mutant):
            rejected.append("missing-" + omitted)
        else:
            failures.append("accepted omitted policy: " + omitted)
    mutant = copy.deepcopy(report)
    mutant["source_files"][0]["sha256"] = "0" * 64
    if validate(mutant):
        rejected.append("incorrect-source-digest")
    else:
        failures.append("accepted incorrect source digest")
    for index in range(len(report["findings"])):
        mutant = copy.deepcopy(report)
        mutant["findings"][index]["evidence"].pop()
        name = "missing-caller-" + mutant["findings"][index]["id"]
        if validate(mutant):
            rejected.append(name)
        else:
            failures.append("accepted " + name)
    for section, keys in ((None, ("artifacts", "source_files")),
                          ("target", tuple(report["target"]))):
        for key in keys:
            mutant = copy.deepcopy(report)
            del (mutant if section is None else mutant[section])[key]
            name = "missing-" + ((section + ".") if section else "") + key
            if validate(mutant):
                rejected.append(name)
            else:
                failures.append("accepted " + name)
    mutant = copy.deepcopy(report)
    mutant["source_files"].pop()
    if validate(mutant):
        rejected.append("missing-one-source-digest")
    else:
        failures.append("accepted missing fixture digest")
    markdown = "\n".join(item["id"] for item in report["findings"])
    failures.extend(validate_markdown(report, markdown))
    for name, bad_markdown in (("missing-markdown", ""),
                               ("missing-markdown-id", markdown.split("\n", 1)[1])):
        if validate_markdown(report, bad_markdown):
            rejected.append(name)
        else:
            failures.append("accepted " + name)
    return {"success": not failures, "mode": "verifier-self-test",
            "positive_reports": 5, "markdown_positive_checks": 1,
            "negative_reports_rejected": rejected, "errors": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path)
    parser.add_argument("--markdown", type=Path,
                        help="Defaults to PREFETCH-POLICY.md beside the JSON report")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = self_test()
    elif args.report:
        try:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            errors = validate(report)
            markdown_path = args.markdown or args.report.with_name("PREFETCH-POLICY.md")
            if isinstance(report, dict):
                errors.extend(validate_markdown(report, markdown_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors = [str(exc)]
        result = {"success": not errors, "mode": "agent-report", "errors": errors}
    else:
        parser.error("provide a report or --self-test")
    result.update({"check_scope": "automated_structure_and_fixture_checks_only",
                   "requires_manual_review": True})
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
