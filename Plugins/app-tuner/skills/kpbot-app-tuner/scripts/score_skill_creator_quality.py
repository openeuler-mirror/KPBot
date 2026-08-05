#!/usr/bin/env python3
"""Score this skill against a local skill-creator-aligned rubric."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


REQUIRED_SUBSKILLS = {
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
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    data: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"')
    return data


def line_count(path: Path) -> int:
    return len(read(path).splitlines())


def add(points: list[dict], name: str, earned: int, total: int, evidence: str) -> None:
    points.append({"name": name, "earned": earned, "total": total, "evidence": evidence})


def score(skill_dir: Path) -> dict:
    points: list[dict] = []
    skill_md = skill_dir / "SKILL.md"
    skill_text = read(skill_md) if skill_md.exists() else ""
    meta = frontmatter(skill_text)

    metadata_score = 0
    metadata_score += 5 if meta.get("name") == skill_dir.name else 0
    metadata_score += 5 if meta.get("description") else 0
    metadata_score += 3 if len(meta.get("description", "")) <= 1024 else 0
    metadata_score += 2 if re.fullmatch(r"[a-z0-9-]+", meta.get("name", "")) else 0
    add(points, "metadata_and_frontmatter", metadata_score, 15, "SKILL.md has valid discovery metadata")

    main_lines = line_count(skill_md) if skill_md.exists() else 9999
    subskill_files = sorted((skill_dir / "subskills").glob("*/SKILL.md"))
    subskill_line_counts = [line_count(path) for path in subskill_files]
    progressive = 0
    progressive += 6 if main_lines <= 500 else 0
    progressive += 6 if subskill_line_counts and max(subskill_line_counts) <= 500 else 0
    progressive += 4 if (skill_dir / "references").is_dir() else 0
    progressive += 4 if (skill_dir / "subskills" / "network-optimization" / "references").is_dir() and (skill_dir / "subskills" / "compiler-optimization" / "references").is_dir() else 0
    add(points, "progressive_disclosure", progressive, 20, f"main_lines={main_lines}, max_subskill_lines={max(subskill_line_counts or [0])}")

    required_refs = [
        "environment-diagnosis.md",
        "candidate-skill-list.md",
        "knowledge-technique-routing.md",
        "subagent-orchestration.md",
        "iteration-execution.md",
        "report-schema.md",
    ]
    existing_refs = {path.name for path in (skill_dir / "references").glob("*.md")}
    ref_score = round(15 * (len(set(required_refs) & existing_refs) / len(required_refs)))
    add(points, "reference_navigation", ref_score, 15, f"required_refs_present={sorted(set(required_refs) & existing_refs)}")

    all_text = "\n".join(read(path) for path in [skill_md, *subskill_files, *sorted((skill_dir / "references").glob("*.md"))])
    safety_tokens = [
        "approved_execute",
        "current_run_id",
        "current_evidence_status",
        "service_health_status",
        "historical_records_status",
        "rollback",
        "perf_pmu_status",
        "candidate_skill_list",
    ]
    safety_score = round(15 * (sum(1 for token in safety_tokens if token in all_text) / len(safety_tokens)))
    add(points, "safety_and_gate_integrity", safety_score, 15, "checks execution approval, evidence freshness, health, history, rollback, PMU")

    subskill_dirs = {path.parent.name for path in subskill_files}
    subskill_score = round(15 * (len(REQUIRED_SUBSKILLS & subskill_dirs) / len(REQUIRED_SUBSKILLS)))
    action_contract_count = sum(1 for path in subskill_files if "candidate_actions" in read(path) and ("rollback" in read(path) or "rollback_or_reversal" in read(path)))
    if action_contract_count == len(subskill_files):
        subskill_score = min(15, subskill_score + 2)
    add(points, "subskill_coverage_and_contracts", subskill_score, 15, f"subskills={len(subskill_dirs)}, action_contracts={action_contract_count}/{len(subskill_files)}")

    knowledge_tokens = [
        "Network Parameter Tuning",
        "Architecture Flags",
        "PGO",
        "LTO",
        "CRC and LSE",
        "jemalloc",
        "tcmalloc",
        "NUMA",
        "PMU",
        "L6 Microarchitecture",
    ]
    knowledge_score = round(10 * (sum(1 for token in knowledge_tokens if token in all_text) / len(knowledge_tokens)))
    add(points, "knowledge_base_coverage", knowledge_score, 10, "maps knowledge L1-L6 techniques and cases to subskills")

    validation_files = [
        "validate_skill_quality.py",
        "score_skill_creator_quality.py",
        "diagnose_environment.py",
        "create_subagent_tasks.py",
        "merge_subagent_results.py",
        "generate_report.py",
    ]
    script_names = {path.name for path in (skill_dir / "scripts").glob("*")}
    validation_score = round(10 * (len(set(validation_files) & script_names) / len(validation_files)))
    add(points, "deterministic_validation_assets", validation_score, 10, f"validation_scripts_present={sorted(set(validation_files) & script_names)}")

    score_total = sum(item["earned"] for item in points)
    return {
        "score": score_total,
        "max_score": sum(item["total"] for item in points),
        "passed_90_threshold": score_total >= 90,
        "rubric": points,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", nargs="?", default="skills/kpbot-app-tuner")
    parser.add_argument("--min-score", type=int, default=90)
    args = parser.parse_args()

    result = score(Path(args.skill_dir).resolve())
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["score"] >= args.min_score else 1


if __name__ == "__main__":
    raise SystemExit(main())
