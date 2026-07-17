#!/usr/bin/env python3
"""Validate the kpbot-app-tuner skill structure and common regressions."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


FORBIDDEN_CORE_PATTERNS = tuple(
    "".join(parts)
    for parts in (
        ("server", "-cpu", "-optimization"),
        ("Server", " CPU", " Optimization"),
        ("7a", "-summary"),
        ("analysis", "-checklist", "-7a"),
        ("7B", "-execution"),
        ("bios", "-os", "-optimization"),
    )
)

REQUIRED_SUBSKILLS = (
    "accelerator-optimization",
    "application-config-optimization",
    "bios-optimization",
    "compiler-optimization",
    "cpu-affinity-optimization",
    "database-workload-analysis",
    "hardware-upgrade-analysis",
    "io-memory-network-bottleneck-analysis",
    "network-optimization",
    "os-optimization",
    "other-optimization",
    "performance-library-selection",
)


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError(f"{path}: unterminated YAML frontmatter")
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{path}: invalid frontmatter line: {line}")
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"')
    return result


def check_frontmatter(skill_dir: Path, errors: list[str]) -> None:
    metadata = parse_frontmatter(skill_dir / "SKILL.md")
    expected_name = skill_dir.name
    actual_name = metadata.get("name")
    if actual_name != expected_name:
        errors.append(f"SKILL.md name={actual_name!r} does not match folder {expected_name!r}")
    if not metadata.get("description"):
        errors.append("SKILL.md description is required")

    for entry in (".claude", ".opencode", ".agents"):
        entry_path = skill_dir.parents[1] / entry / "skills" / expected_name / "SKILL.md"
        if not entry_path.exists():
            errors.append(f"missing platform entry: {entry_path}")
            continue
        entry_meta = parse_frontmatter(entry_path)
        if entry_meta.get("name") != expected_name:
            errors.append(f"{entry_path}: name must be {expected_name!r}")


def iter_core_text_files(repo_root: Path, skill_dir: Path) -> list[Path]:
    candidates = [
        repo_root / "README.md",
        repo_root / "CLAUDE.md",
        repo_root / "docs",
        skill_dir,
        repo_root / ".claude" / "skills" / skill_dir.name,
        repo_root / ".opencode" / "skills" / skill_dir.name,
        repo_root / ".agents" / "skills" / skill_dir.name,
    ]
    files: list[Path] = []
    for candidate in candidates:
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            files.extend(
                path
                for path in candidate.rglob("*")
                if path.is_file()
                and "examples/mysql-test" not in path.as_posix()
                and path.suffix in {".md", ".yaml", ".yml", ".py", ".sh", ".html"}
            )
    return sorted(set(files))


def check_forbidden_patterns(files: list[Path], errors: list[str]) -> None:
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in FORBIDDEN_CORE_PATTERNS:
            if pattern in text:
                errors.append(f"{path}: forbidden legacy pattern {pattern!r}")


def check_backtick_refs(skill_dir: Path, errors: list[str]) -> None:
    for path in [skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md"))]:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for ref in re.findall(r"`((?:references|scripts|subskills)/[^`<>]+)`", text):
            if "<" in ref or ">" in ref:
                continue
            if not (skill_dir / ref).exists():
                errors.append(f"{path}: missing referenced resource `{ref}`")


def check_high_risk_scripts(skill_dir: Path, errors: list[str]) -> None:
    optimize_network = skill_dir / "scripts" / "optimize_network.sh"
    text = optimize_network.read_text(encoding="utf-8", errors="ignore")
    for token in ("--execute", "--approved-change-id", "Dry-run"):
        if token not in text:
            errors.append(f"{optimize_network}: missing high-risk gate token {token}")

    apply_action = skill_dir / "scripts" / "apply_optimization_action.sh"
    text = apply_action.read_text(encoding="utf-8", errors="ignore")
    for token in ("--execute", "--approved-change-id"):
        if token not in text:
            errors.append(f"{apply_action}: missing execution approval token {token}")


def require_tokens(path: Path, tokens: tuple[str, ...], errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    for token in tokens:
        if token not in text:
            errors.append(f"{path}: missing required gate token {token!r}")


def check_current_run_gates(skill_dir: Path, errors: list[str]) -> None:
    scripts_dir = skill_dir / "scripts"
    require_tokens(
        scripts_dir / "collect_evidence_snapshot.sh",
        (
            "--current-run-id",
            "--current-run-started-at",
            "current_evidence_status",
            "target_identity",
            "performance_signal_summary.json",
            "hotspot_dso_rank",
            "L1-icache-load-misses",
            "LLC-load-misses",
        ),
        errors,
    )
    require_tokens(
        scripts_dir / "create_subagent_tasks.py",
        (
            "evidence current_run_id mismatch",
            "current evidence status must be 'current'",
            "snapshot_time is earlier than current_run_started_at",
            "target identity JSON does not match snapshot metadata",
            "candidate_skill_list",
            "third_party_library_hotspot",
            "network_hotspot_high",
            "l1_icache_miss_high",
            "context_switch_high_and_l3_miss_high",
            "coverage_after_candidate_list",
        ),
        errors,
    )
    require_tokens(
        scripts_dir / "merge_subagent_results.py",
        (
            "missing_current_run_id_in_candidate_manifest",
            "candidate_manifest_current_evidence_status_not_current",
            "candidate_skill_list",
            "current_run_id_mismatch",
            "current_evidence_status_mismatch",
        ),
        errors,
    )
    require_tokens(
        scripts_dir / "create_execution_task.py",
        (
            "candidate_pool is missing current_run_id",
            "candidate_pool current_evidence_status must be 'current'",
            "candidate_pool has unresolved gate_errors",
            "snapshot_time is earlier than current_run_started_at",
            "skill iteration stop gate",
            "--per-skill-state",
        ),
        errors,
    )
    require_tokens(
        scripts_dir / "generate_report.py",
        (
            "current_run_identity",
            "evidence_freshness_check",
            "Current conclusions require matching current_run_id",
        ),
        errors,
    )


def check_subskill_contracts(skill_dir: Path, errors: list[str], warnings: list[str]) -> None:
    subskills_dir = skill_dir / "subskills"
    existing = {path.name for path in subskills_dir.iterdir() if path.is_dir()}
    missing = sorted(set(REQUIRED_SUBSKILLS) - existing)
    if missing:
        errors.append(f"missing required subskill directories: {','.join(missing)}")

    unexpected = sorted(existing - set(REQUIRED_SUBSKILLS))
    for name in unexpected:
        warnings.append(f"unexpected subskill directory, verify routing contract before use: {name}")

    for name in sorted(existing & set(REQUIRED_SUBSKILLS)):
        skill_file = subskills_dir / name / "SKILL.md"
        if not skill_file.exists():
            errors.append(f"{subskills_dir / name}: missing SKILL.md")
            continue
        try:
            metadata = parse_frontmatter(skill_file)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if metadata.get("name") != name:
            errors.append(f"{skill_file}: name must match directory {name!r}")
        if not metadata.get("description"):
            errors.append(f"{skill_file}: description is required")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", nargs="?", default="skills/kpbot-app-tuner")
    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    repo_root = skill_dir.parents[1]
    errors: list[str] = []
    warnings: list[str] = []

    if not skill_dir.exists():
        errors.append(f"skill directory does not exist: {skill_dir}")
    else:
        try:
            check_frontmatter(skill_dir, errors)
        except ValueError as exc:
            errors.append(str(exc))
        files = iter_core_text_files(repo_root, skill_dir)
        check_forbidden_patterns(files, errors)
        check_backtick_refs(skill_dir, errors)
        check_high_risk_scripts(skill_dir, errors)
        check_current_run_gates(skill_dir, errors)
        check_subskill_contracts(skill_dir, errors, warnings)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)

    if errors:
        return 1
    print("skill quality validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
