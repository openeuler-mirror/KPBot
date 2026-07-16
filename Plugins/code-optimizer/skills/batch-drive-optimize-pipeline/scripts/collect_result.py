#!/usr/bin/env python3
"""Collect one batch-drive target result into JSON and Markdown artifacts."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\_-])"
)

FINAL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"鲲鹏性能优化流水线完成",
        r"总轮次(数)?",
        r"累计(优化|吞吐)?提升",
        r"累计优化效果",
        r"持续进化入口",
        r"进化入口",
        r"final_summary\.md",
        r"FINAL_SUMMARY\.md",
        r"Pipeline\s+Complete",
        r"Final\s+Verdict",
        r"Generated\s+by\s+KunpengAccelerationLibOptimization",
        r"No optimization possible",
        r"No code changes required",
        r"practical maximum",
        r"batch_result\.json",
        r"complete_no_optimization",
        r"SESSION_COMPLETE",
        r"BATCH_END",
        r"SESSION_END",
        r"TARGET_COMPLETE",
        r"BATCH_SUMMARY",
        r"pipeline_status.{0,20}(complete|completed)",
        r"target_status.{0,20}(complete|completed)",
        r"已达最大轮次",
        r"所有轮次.{0,20}完成",
        r"优化流程.{0,20}(完成|结束)",
        r"final completed result",
        r"closing report.{0,40}(complete|completed)",
        r"最终(输出|结果|报告).{0,40}(完成|如下)",
    ]
]

NUMERIC_TOKEN_PATTERN = r"[-+]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][-+]?\d+)?"
NUMERIC_TOKEN_RE = re.compile(NUMERIC_TOKEN_PATTERN)
SIMPLE_NUMERIC_RE = re.compile(
    rf"^\s*(?:[A-Za-z_][A-Za-z0-9_ ./-]*[:=]\s*)?({NUMERIC_TOKEN_PATTERN})\s*(?:x|%|percent)?\s*$",
    re.I,
)
PERFORMANCE_SIGNAL_RE = re.compile(
    r"性能(测试|提升|改进|下降|回归)|performance[._ -]?(improvement|regression|delta)|"
    rf"吞吐量(提升|下降|回归)|speedup|加速比|{NUMERIC_TOKEN_PATTERN}%",
    re.I,
)

PATCH_EXCLUDE_PATHS = [
    ":(exclude)build/**",
    ":(exclude)cmake-build-*/**",
    ":(exclude).claude/**",
    ":(exclude)**/*.o",
    ":(exclude)**/*.a",
    ":(exclude)**/*.so",
    ":(exclude)**/*.dylib",
    ":(exclude)**/*.dll",
    ":(exclude)**/*.exe",
    ":(exclude)**/*.out",
    ":(exclude)**/*.data",
    ":(exclude)**/*.gz",
    ":(exclude)**/*.log",
    ":(exclude)**/*.dSYM/**",
    ":(exclude)perf*",
    ":(exclude)test_*",
    ":(exclude)optimization_reports/**",
    ":(exclude).batch_optimize_answer_bank.md",
    ":(exclude)CLAUDE.md",
]

PATCH_EXCLUDE_GLOBS = [
    "build/**",
    "cmake-build-*/*",
    ".claude/**",
    "optimization_reports/**",
    "**/*.o",
    "**/*.a",
    "**/*.so",
    "**/*.dylib",
    "**/*.dll",
    "**/*.exe",
    "**/*.out",
    "**/*.data",
    "**/*.gz",
    "**/*.log",
    "**/*.dSYM/**",
    "perf*",
    "test_*",
    ".batch_optimize_answer_bank.md",
    "CLAUDE.md",
]

SOURCE_PATCH_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".s",
    ".S",
    ".cmake",
    ".mk",
    ".am",
    ".ac",
}
SOURCE_PATCH_NAMES = {
    "BUILD",
    "BUILD.bazel",
    "CMakeLists.txt",
    "GNUmakefile",
    "Makefile",
    "Makefile.in",
    "MODULE.bazel",
    "WORKSPACE",
    "configure",
    "configure.ac",
    "makefile",
    "meson.build",
    "meson_options.txt",
}
FINAL_REPORT_NAMES = (
    "final_summary.md",
    "FINAL_SUMMARY.md",
    "final_report.md",
    "FINAL_REPORT.md",
    "optimization_summary.md",
)
FINAL_REPORT_NAME_SET = {name.lower() for name in FINAL_REPORT_NAMES}
COMPLETE_STATUSES = {
    "complete",
    "completed",
    "complete_no_optimization",
    "analysis_complete",
    "success",
    "succeeded",
    "ok",
    "done",
}
PREP_STAGES = ("GatherContext", "ParseIntent", "PrepareProject", "DecomposeTasks", "AnalyzeTestcase", "AnalyzeHotspot")
OPT_STAGES = ("DecideOptimization", "ApplyOptimization", "AdversarialReview", "VerifyOptimization")
SUCCESS_STATUSES = {"applied_verified", "analysis_only", "complete_no_optimization"}
GIT_COMMAND_TIMEOUT_SECONDS = 60
TOKENISH_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff]", re.UNICODE)
LARGE_PATCH_FILE_BYTES = 1_000_000


class ContractValidator:
    COMPLETION_GATE_BOOL_FIELDS = {
        "pipeline_reached_final_report",
        "required_stages_seen",
        "patch_collected",
        "functional_verified",
        "performance_measured",
        "artifact_consistent",
        "patch_hygiene_passed",
    }

    @staticmethod
    def expect_dict(payload: Any, context: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f"{context}: expected dict, got {type(payload).__name__}")
        return payload

    @classmethod
    def validate_completion_gate(cls, payload: Any) -> dict[str, Any]:
        data = cls.expect_dict(payload, "completion_gate")
        missing = sorted(field for field in cls.COMPLETION_GATE_BOOL_FIELDS if field not in data)
        if missing:
            raise ValueError(f"completion_gate: missing required field(s): {', '.join(missing)}")
        wrong_types = sorted(
            field for field in cls.COMPLETION_GATE_BOOL_FIELDS if not isinstance(data.get(field), bool)
        )
        if wrong_types:
            raise ValueError(f"completion_gate: expected bool field(s): {', '.join(wrong_types)}")
        if not isinstance(data.get("evidence_sources"), dict):
            raise ValueError("completion_gate: expected dict field: evidence_sources")
        return data


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def completion_status_for_result(
    runner_result: dict[str, Any] | None,
    reached_final: bool,
    strict_status: str,
) -> str:
    runner_status = str((runner_result or {}).get("status") or "")
    if reached_final:
        return "completed"
    if runner_status and runner_status != "completed":
        return runner_status
    if strict_status in SUCCESS_STATUSES:
        return "completed"
    if strict_status:
        return "incomplete"
    return "unknown"


def run_text(
    cmd: list[str],
    cwd: Path,
    check: bool = False,
    timeout_seconds: int = GIT_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        proc = subprocess.CompletedProcess(cmd, 124, stdout=output or f"command timed out after {timeout_seconds} seconds\n")
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")
    return proc.returncode, proc.stdout


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def read_json(path: Path, *, strict: bool = False, contract_name: str = "json") -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        if strict:
            raise ValueError(f"{contract_name}: invalid JSON at {path}: {exc}") from exc
        return {}
    if strict:
        return ContractValidator.expect_dict(loaded, contract_name)
    return loaded if isinstance(loaded, dict) else {}


def read_claude_output_events(path: Path) -> str:
    if not path.exists():
        return ""
    chunks: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "claude_output":
            chunks.append(str(record.get("text", "")))
    return "\n".join(chunks)


def clean_terminal_text(text: str) -> str:
    text = re.sub(
        r"\x1b\[([0-9]*)C",
        lambda match: " " * min(int(match.group(1) or "1"), 80),
        text,
    )
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.replace("\xa0", " ").strip()
        if not line:
            continue
        if len(line) == 1 and line in "✳✢✶✻✽·":
            continue
        if re.fullmatch(r"[─│┌┐└┘├┤┬┴┼╭╮╰╯━┏┓┗┛╋╂\-_= ]+", line):
            continue
        lines.append(re.sub(r"[ \t]{2,}", " ", line))
    return "\n".join(lines)


def fallback_token_count(text: str) -> int:
    return sum(1 for _ in TOKENISH_RE.finditer(text))


def transcript_token_stats(text: str, source: str) -> dict[str, Any]:
    cleaned = clean_terminal_text(text)
    method = "heuristic_word_cjk_punct_estimate"
    token_count: int
    try:
        import tiktoken  # type: ignore

        encoding = tiktoken.get_encoding("cl100k_base")
        token_count = len(encoding.encode(cleaned, disallowed_special=()))
        method = "tiktoken_cl100k_base_estimate"
    except ImportError:
        token_count = fallback_token_count(cleaned)
    return {
        "token_count": token_count,
        "method": method,
        "is_estimate": True,
        "source": source,
        "text_chars": len(cleaned),
        "text_bytes": len(cleaned.encode("utf-8")),
    }


def read_auto_interactions(path: Path) -> list[str]:
    if not path.exists():
        return []
    interactions: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "auto_to_claude":
            interactions.append(str(record.get("text", "")).strip())
    return interactions


def final_excerpt(text: str, max_chars: int = 3600) -> str:
    clean = clean_terminal_text(text)
    positions: list[int] = []
    for pattern in FINAL_PATTERNS:
        match = pattern.search(clean)
        if match:
            positions.append(match.start())
    if not positions:
        return clean[-max_chars:]
    start = max(0, min(positions) - 800)
    return clean[start : start + max_chars]


def copy_if_exists(src: Path, dst: Path) -> None:
    try:
        if src.is_dir():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except FileNotFoundError:
        return


def latest_report_dir(workdir: Path) -> Path | None:
    reports = workdir / "optimization_reports"
    if not reports.exists():
        return None
    candidates = [p for p in reports.iterdir() if p.is_dir() and p.name.startswith("run_")]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.stat().st_mtime)[-1]


def final_found(text: str) -> bool:
    return any(pattern.search(text) for pattern in FINAL_PATTERNS)


def find_final_report(workdir: Path) -> tuple[Path | None, Path | None]:
    reports = workdir / "optimization_reports"
    if not reports.exists():
        return None, None
    candidates = [path for path in reports.rglob("*") if path.is_file() and path.name.lower() in FINAL_REPORT_NAME_SET]
    candidates = [
        path
        for path in candidates
        if path.parent.name.startswith("run_") or path.parent == reports or "final" in path.name.lower()
    ]
    if not candidates:
        return latest_report_dir(workdir), None
    report_path = sorted(candidates, key=lambda path: path.stat().st_mtime)[-1]
    return report_path.parent, report_path


def marker_json_complete(path: Path) -> bool:
    data = read_json(path)
    if not data:
        return False
    for key in ("pipeline_status", "target_status", "status", "result", "batch_status"):
        value = str(data.get(key, "")).strip().lower()
        if value in COMPLETE_STATUSES or value.startswith("complete"):
            return True
    return False


def completion_markers(workdir: Path, target_out: Path, log_dir: Path) -> list[str]:
    marker_paths = [
        workdir / ".batch_optimize_result.json",
        log_dir / "final_result.json",
        target_out / "TARGET_COMPLETE.json",
        target_out / ".batch_optimize_result.json",
        target_out.parent.parent / "batch_summary.json",
    ]
    found: list[str] = []
    for path in marker_paths:
        if path.exists() and marker_json_complete(path):
            found.append(str(path))
    reports_root = workdir / "optimization_reports"
    if reports_root.exists():
        for path in reports_root.rglob("*"):
            if not path.is_file():
                continue
            lowered = path.name.lower()
            if lowered == "batch_result.json" and marker_json_complete(path):
                found.append(str(path))
            elif lowered == "optimization_summary.md" and final_found(read_text(path)):
                found.append(str(path))
    text_markers = [workdir / ".batch_optimize_result.md", target_out.parent.parent / "BATCH_SUMMARY.md"]
    for path in text_markers:
        if path.exists() and final_found(read_text(path)):
            found.append(str(path))
    return found


def is_patch_excluded(rel_path: str) -> bool:
    rel_path = rel_path.strip("/")
    rel_no_slash = rel_path.rstrip("/")
    return any(
        fnmatch.fnmatch(rel_path, pattern)
        or fnmatch.fnmatch(rel_no_slash, pattern)
        or (pattern.endswith("/**") and rel_no_slash == pattern[:-3])
        for pattern in PATCH_EXCLUDE_GLOBS
    )


def is_source_patch_candidate(rel_path: str) -> bool:
    path = Path(rel_path)
    return path.name in SOURCE_PATCH_NAMES or path.suffix in SOURCE_PATCH_SUFFIXES


def parse_name_status_z(output: str) -> list[dict[str, str]]:
    parts = [part for part in output.split("\0") if part]
    entries: list[dict[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        code = status[0]
        if code in {"R", "C"} and i + 1 < len(parts):
            old_path = parts[i]
            new_path = parts[i + 1]
            i += 2
        elif i < len(parts):
            old_path = parts[i]
            new_path = old_path
            i += 1
        else:
            break
        entries.append({"status": status, "code": code, "old_path": old_path, "path": new_path})
    return entries


def path_is_deleted_test_source(entry: dict[str, str]) -> bool:
    if entry.get("code") != "D":
        return False
    path = entry.get("old_path") or entry.get("path") or ""
    return (
        is_source_patch_candidate(path)
        and (
            path.startswith("test/")
            or path.startswith("tests/")
            or "/test/" in path
            or "/tests/" in path
            or Path(path).name.startswith("test_")
        )
    )


def file_size(workdir: Path, rel_path: str) -> int:
    path = workdir / rel_path
    if not path.exists() or not path.is_file():
        return 0
    return path.stat().st_size


def looks_binary(workdir: Path, rel_path: str) -> bool:
    path = workdir / rel_path
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            sample = handle.read(8192)
    except OSError:
        return False
    return b"\0" in sample


def patch_decision(workdir: Path, entry: dict[str, str]) -> tuple[bool, list[str]]:
    rel_path = (entry.get("path") or "").strip()
    old_path = (entry.get("old_path") or rel_path).strip()
    reasons: list[str] = []
    if not rel_path:
        reasons.append("empty_path")
    if is_patch_excluded(rel_path) or is_patch_excluded(old_path):
        reasons.append("excluded_path")
    if not is_source_patch_candidate(rel_path) and not is_source_patch_candidate(old_path):
        reasons.append("not_source_or_build_config")
    if path_is_deleted_test_source(entry):
        reasons.append("deleted_test_source")
    if looks_binary(workdir, rel_path):
        reasons.append("binary_file")
    size = file_size(workdir, rel_path)
    if size > LARGE_PATCH_FILE_BYTES:
        reasons.append("large_file")
    return not reasons, reasons


def diff_for_entry(workdir: Path, revspec: str, entry: dict[str, str]) -> str:
    code = entry.get("code")
    paths = [entry.get("path") or ""]
    if code in {"R", "C"} and entry.get("old_path") and entry.get("old_path") != entry.get("path"):
        paths.insert(0, entry["old_path"])
    cmd = ["git", "diff", "--binary", revspec, "--", *paths]
    _, patch = run_text(cmd, workdir)
    return patch


def tracked_patch_for_range(workdir: Path, revspec: str, scope: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    _, output = run_text(["git", "diff", "--name-status", "-z", revspec, "--", "."], workdir)
    patches: list[str] = []
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for entry in parse_name_status_z(output):
        include, reasons = patch_decision(workdir, entry)
        item = {
            "path": entry.get("path"),
            "old_path": entry.get("old_path"),
            "status": entry.get("status"),
            "scope": scope,
        }
        if include:
            patch = diff_for_entry(workdir, revspec, entry)
            if patch.strip():
                patches.append(patch)
                included.append(item)
        else:
            excluded.append({**item, "reasons": reasons})
    return "\n".join(part.strip() for part in patches if part.strip()), included, excluded


def git_base(workdir: Path) -> str:
    base = "batch_baseline"
    code, _ = run_text(["git", "rev-parse", "--verify", base], workdir)
    if code != 0:
        code, first = run_text(["git", "rev-list", "--max-parents=0", "HEAD"], workdir)
        base = first.strip().splitlines()[0] if code == 0 and first.strip() else "HEAD"
    return base


def untracked_source_patch(workdir: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    _, output = run_text(["git", "ls-files", "--others", "--exclude-standard", "--", "."], workdir)
    patches: list[str] = []
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for rel_path in output.splitlines():
        rel_path = rel_path.strip()
        entry = {"status": "??", "code": "?", "old_path": rel_path, "path": rel_path}
        include, reasons = patch_decision(workdir, entry)
        item = {"path": rel_path, "old_path": rel_path, "status": "??", "scope": "untracked"}
        if not include:
            excluded.append({**item, "reasons": reasons})
            continue
        _, patch = run_text(["git", "diff", "--no-index", "--binary", "--", "/dev/null", rel_path], workdir)
        if patch.strip():
            patches.append(patch)
            included.append(item)
    return "\n".join(part.strip() for part in patches if part.strip()), included, excluded


def write_patch(workdir: Path, out_path: Path) -> dict[str, Any]:
    base = git_base(workdir)
    committed_patch, committed_included, committed_excluded = tracked_patch_for_range(workdir, f"{base}..HEAD", "committed")
    working_patch, working_included, working_excluded = tracked_patch_for_range(workdir, "HEAD", "working_tree")
    untracked_patch, untracked_included, untracked_excluded = untracked_source_patch(workdir)
    final_patch = "\n".join(part.strip() for part in [committed_patch, working_patch, untracked_patch] if part.strip())

    (out_path.parent / "committed.patch").write_text(committed_patch, encoding="utf-8")
    (out_path.parent / "working_tree.patch").write_text(
        "\n".join(part.strip() for part in [working_patch, untracked_patch] if part.strip()) + ("\n" if working_patch.strip() or untracked_patch.strip() else ""),
        encoding="utf-8",
    )
    out_path.write_text(final_patch + ("\n" if final_patch else ""), encoding="utf-8")
    included = committed_included + working_included + untracked_included
    excluded = committed_excluded + working_excluded + untracked_excluded

    def hygiene_failure(item: dict[str, Any], reason: str) -> bool:
        reasons = set(item.get("reasons", []))
        if reason == "deleted_test_source":
            return reason in reasons
        # Generated outputs such as perf.data and optimization_reports are
        # intentionally excluded from final.patch. They should be visible in
        # the manifest, but they should not make a clean source patch fail.
        if "excluded_path" in reasons or "not_source_or_build_config" in reasons:
            return False
        return reason in reasons

    binary_detected = any(hygiene_failure(item, "binary_file") for item in excluded)
    large_file_detected = any(hygiene_failure(item, "large_file") for item in excluded)
    excluded_binary_detected = any("binary_file" in item.get("reasons", []) for item in excluded)
    excluded_large_file_detected = any("large_file" in item.get("reasons", []) for item in excluded)
    deleted_test_sources = [
        item.get("old_path") or item.get("path")
        for item in excluded
        if hygiene_failure(item, "deleted_test_source")
    ]
    manifest = {
        "base": base,
        "included": included,
        "excluded": excluded,
        "binary_detected": binary_detected,
        "large_file_detected": large_file_detected,
        "excluded_binary_detected": excluded_binary_detected,
        "excluded_large_file_detected": excluded_large_file_detected,
        "deleted_test_sources_detected": deleted_test_sources,
        "patch_hygiene_passed": not (binary_detected or large_file_detected or deleted_test_sources),
    }
    (out_path.parent / "patch_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "base": base,
        "committed_patch_bytes": len(committed_patch.encode("utf-8")),
        "working_tree_patch_bytes": len(working_patch.encode("utf-8")) + len(untracked_patch.encode("utf-8")),
        "final_patch_bytes": len((final_patch + ("\n" if final_patch else "")).encode("utf-8")),
        "included_files": [str(item.get("path")) for item in included],
        "excluded_files": excluded,
        "untracked_source_files": [str(item.get("path")) for item in untracked_included],
        "patch_manifest": str(out_path.parent / "patch_manifest.json"),
        "patch_hygiene_passed": manifest["patch_hygiene_passed"],
        "binary_detected": binary_detected,
        "large_file_detected": large_file_detected,
        "excluded_binary_detected": excluded_binary_detected,
        "excluded_large_file_detected": excluded_large_file_detected,
        "deleted_test_sources_detected": deleted_test_sources,
    }


def read_event_text_and_agents(path: Path) -> tuple[str, list[str]]:
    if not path.exists():
        return "", []
    chunks: list[str] = []
    agents: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") not in {"claude_output", "batch_driver"}:
            continue
        text = clean_terminal_text(str(record.get("text", "")))
        chunks.append(text)
        agents.extend(re.findall(r"Agent\(([^)\n\r]{1,120})\)", text))
    return "\n".join(chunks), agents


def stage_patterns(stage: str) -> list[re.Pattern[str]]:
    variants = {
        "GatherContext": [r"GatherContext", r"gather-context", r"收集优化目标信息"],
        "ParseIntent": [r"ParseIntent", r"parse-intent", r"解析用户优化意图"],
        "PrepareProject": [r"PrepareProject", r"prepare-project", r"准备项目环境", r"准备阶段"],
        "DecomposeTasks": [r"DecomposeTasks", r"decompose-tasks", r"分解优化任务"],
        "AnalyzeTestcase": [r"AnalyzeTestcase", r"analyze-testcase", r"测试用例分析"],
        "AnalyzeHotspot": [r"AnalyzeHotspot", r"analyze-hotspot", r"热点分析"],
        "DecideOptimization": [r"DecideOptimization", r"decide-optimization", r"策略确认"],
        "ApplyOptimization": [r"ApplyOptimization", r"apply-optimization", r"应用优化"],
        "AdversarialReview": [r"AdversarialReview", r"adversarial-review", r"对抗"],
        "VerifyOptimization": [r"VerifyOptimization", r"verify-optimization", r"验证优化", r"验证结果"],
    }
    return [re.compile(pattern, re.IGNORECASE) for pattern in variants[stage]]


def iter_report_json(reports_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not reports_root.exists():
        return []
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(reports_root.rglob("*.json")):
        data = read_json(path)
        if data:
            loaded.append((path, data))
    return loaded


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def json_status(data: dict[str, Any]) -> str:
    for key in ("quality_status", "status", "target_status", "result", "batch_status", "pipeline_status"):
        value = str(data.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def number_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if len(NUMERIC_TOKEN_RE.findall(value)) != 1:
            return None
        match = SIMPLE_NUMERIC_RE.match(value)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def nested_values(data: Any, key_names: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in key_names:
                found.append(value)
            found.extend(nested_values(value, key_names))
    elif isinstance(data, list):
        for item in data:
            found.extend(nested_values(item, key_names))
    return found


def truthy_pass_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return any(truthy_pass_value(item) for item in value.values())
    if isinstance(value, list):
        return any(truthy_pass_value(item) for item in value)
    text = str(value).strip().lower()
    return text in {"pass", "passed", "success", "succeeded", "ok", "true", "verified"} or "passed" in text


def performance_value_present(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            if any(token in key_lower for token in ("speedup", "improvement", "optimized", "baseline", "cycles", "time")):
                if number_value(item) is not None or isinstance(item, dict):
                    return True
            if performance_value_present(item):
                return True
    elif isinstance(value, list):
        return any(performance_value_present(item) for item in value)
    else:
        return number_value(value) is not None
    return False


def collect_structured_evidence(target_out: Path) -> dict[str, Any]:
    reports_root = target_out / "optimization_reports"
    json_files = iter_report_json(reports_root)
    stage_hits: dict[str, list[str]] = {stage: [] for stage in [*PREP_STAGES, *OPT_STAGES]}
    verify_files: list[str] = []
    batch_result_files: list[str] = []
    baseline_blocked_files: list[str] = []
    functional_verified = False
    performance_measured = False
    no_effect_claim = False
    baseline_blocked = False
    applied_count: int | None = None
    verified_count: int | None = None
    clean_patch_files: list[str] = []
    performance_summary: Any = None

    def rel(path: Path) -> str:
        try:
            return str(path.relative_to(reports_root))
        except ValueError:
            return str(path)

    for path, data in json_files:
        rel_path = rel(path)
        lowered = rel_path.lower()
        payload = compact_json(data).lower()
        status = json_status(data)

        if "batch_result.json" in lowered:
            batch_result_files.append(rel_path)
            for stage in [*PREP_STAGES, *OPT_STAGES]:
                if status in COMPLETE_STATUSES or status.startswith("complete") or data.get("pipeline_status") in COMPLETE_STATUSES:
                    stage_hits[stage].append(rel_path)
            applied = number_value(data.get("applied_count") or data.get("total_optimizations_applied"))
            verified = number_value(data.get("verified_count") or data.get("total_optimizations_verified"))
            if applied is not None:
                applied_count = int(applied)
            if verified is not None:
                verified_count = int(verified)
            if isinstance(data.get("clean_patch_files"), list):
                clean_patch_files = [str(item) for item in data.get("clean_patch_files") or []]
            if "complete_no_optimization" in status:
                no_effect_claim = True
            if "baseline_blocked" in status:
                baseline_blocked = True
            if data.get("performance_summary") is not None:
                performance_summary = data.get("performance_summary")

        if "baseline_blocked" in lowered or "baseline_blocked" in status or "baseline_blocked" in payload:
            baseline_blocked = True
            baseline_blocked_files.append(rel_path)

        for stage in [*PREP_STAGES, *OPT_STAGES]:
            if f"stages/{stage.lower()}.json" in lowered or f"stage_{stage.lower()}" in lowered:
                stage_hits[stage].append(rel_path)
            elif any(pattern.search(rel_path) for pattern in stage_patterns(stage)):
                stage_hits[stage].append(rel_path)

        if "hotspot_analysis" in lowered:
            stage_hits["AnalyzeHotspot"].append(rel_path)
        if "adversarial_review" in lowered:
            stage_hits["AdversarialReview"].append(rel_path)
        if "verification" in lowered or "verify" in lowered:
            stage_hits["VerifyOptimization"].append(rel_path)
            verify_files.append(rel_path)
        if any(token in lowered for token in ("compiler_flag", "memory_access", "apply_result", "optimization_result")):
            stage_hits["ApplyOptimization"].append(rel_path)

        if any(truthy_pass_value(value) for value in nested_values(data, {"functional_test", "functional_test_passed", "functional_verified", "passed"})):
            functional_verified = True
        if any(performance_value_present(value) for value in nested_values(data, {"performance", "performance_summary", "speedup", "improvement_percent"})):
            performance_measured = True
        if "complete_no_optimization" in status or "no optimization possible" in payload or "no code changes required" in payload:
            no_effect_claim = True

    stage_hits = {stage: sorted(set(paths)) for stage, paths in stage_hits.items()}
    return {
        "stage_hits": stage_hits,
        "verify_files": sorted(set(verify_files)),
        "batch_result_files": sorted(set(batch_result_files)),
        "baseline_blocked_files": sorted(set(baseline_blocked_files)),
        "functional_verified": functional_verified,
        "performance_measured": performance_measured,
        "no_effect_claim": no_effect_claim,
        "baseline_blocked": baseline_blocked,
        "applied_count": applied_count,
        "verified_count": verified_count,
        "clean_patch_files": clean_patch_files,
        "performance_summary": performance_summary,
        "json_files": [rel(path) for path, _ in json_files],
    }


def collect_stage_evidence(target_out: Path, transcript_text: str, structured: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    reports_root = target_out / "optimization_reports"
    report_files = sorted(reports_root.rglob("*.md")) if reports_root.exists() else []
    report_texts = {path: read_text(path) for path in report_files}
    evidence: dict[str, dict[str, Any]] = {}
    combined_report_text = "\n".join(report_texts.values())
    structured_hits = structured.get("stage_hits") if isinstance(structured.get("stage_hits"), dict) else {}
    for stage in [*PREP_STAGES, *OPT_STAGES]:
        patterns = stage_patterns(stage)
        json_hits = list(structured_hits.get(stage) or [])
        artifact_hits = [
            str(path.relative_to(reports_root))
            for path in report_files
            if any(pattern.search(str(path.relative_to(reports_root))) for pattern in patterns)
        ]
        report_hits = [
            str(path.relative_to(reports_root))
            for path, text in report_texts.items()
            if any(pattern.search(text) for pattern in patterns)
        ]
        transcript_hit = any(pattern.search(transcript_text) for pattern in patterns)
        present = bool(json_hits or artifact_hits or report_hits or transcript_hit)
        source = "missing"
        if json_hits:
            source = "structured"
        elif artifact_hits:
            source = "artifact"
        elif report_hits:
            source = "report"
        elif transcript_hit:
            source = "transcript"
        evidence[stage] = {
            "present": present,
            "source": source,
            "structured": json_hits[:8],
            "artifacts": artifact_hits[:8],
            "reports": report_hits[:8],
        }
    return evidence, combined_report_text


def parse_quality_signals(
    text: str,
    status_text: str,
    patch_info: dict[str, Any],
    structured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    structured = structured or {}

    def status_path(line: str) -> str:
        if " -> " in line:
            return line.rsplit(" -> ", 1)[-1].strip()
        return line[3:].strip() if len(line) > 3 else line.strip()

    source_status_lines = [
        line
        for line in status_text.splitlines()
        if line.strip()
        and not is_patch_excluded(status_path(line))
        and is_source_patch_candidate(status_path(line).rstrip("/"))
    ]
    source_modified = bool(source_status_lines)
    baseline_blocked = bool(
        re.search(
            r"baseline\.build_ok\s*=\s*false|baseline[^\n]{0,80}(构建失败|build failed)|"
            r"No rule to make target|Could NOT find [A-Z0-9_+-]+|missing:\s*[A-Z0-9_+-]+|"
            r"CMake Error[^\n]{0,120}(missing|not found|could not find)|"
            r"(dependency|依赖)[^\n]{0,80}(missing|not found|缺失|不可用)",
            text,
            re.I,
        )
    )
    functional_pass = bool(
        re.search(
            r"functional_test[\"']?\s*:\s*[\"']?(pass|passed|success|ok|true)|"
            r"functional[ _-]?test[^\n]{0,80}(pass|passed|success|verified|ok)|"
            r"correctness[^\n]{0,80}(pass|passed|success|verified|ok)|"
            r"(功能测试|功能验证|正确性|测试)[^\n]{0,80}(pass|passed|通过|成功|✓|✔|✅)|"
            r"(all tests passed|100% tests passed|tests passed|ctest[^\n]{0,80}passed)",
            text,
            re.I,
        )
    )
    perf_measured = bool(PERFORMANCE_SIGNAL_RE.search(text))
    no_effect_claim = bool(
        re.search(
            r"应用优化数\s*[|:：]\s*0|应用\s*[|:：]\s*0|无代码变更|未执行代码变更|"
            r"complete_no_optimization|No optimization possible|No code changes required|"
            r"无有效优化|未产生有效优化",
            text,
            re.I,
        )
    )
    baseline_blocked = baseline_blocked or bool(structured.get("baseline_blocked"))
    functional_pass = functional_pass or bool(structured.get("functional_verified"))
    perf_measured = perf_measured or bool(structured.get("performance_measured"))
    no_effect_claim = no_effect_claim or bool(structured.get("no_effect_claim"))
    patch_hygiene_passed = bool(patch_info.get("patch_hygiene_passed", True))
    patch_hygiene_errors = []
    if patch_info.get("binary_detected"):
        patch_hygiene_errors.append("binary file detected")
    if patch_info.get("large_file_detected"):
        patch_hygiene_errors.append("large file detected")
    if patch_info.get("deleted_test_sources_detected"):
        patch_hygiene_errors.append("deleted test source detected")
    return {
        "source_modified": source_modified,
        "source_status_lines": source_status_lines,
        "baseline_blocked": baseline_blocked,
        "functional_test_pass": functional_pass,
        "performance_measured": perf_measured,
        "no_effect_claim": no_effect_claim,
        "patch_nonempty": int(patch_info.get("final_patch_bytes") or 0) > 0,
        "patch_hygiene_passed": patch_hygiene_passed,
        "patch_hygiene_errors": patch_hygiene_errors,
        "applied_count": structured.get("applied_count"),
        "verified_count": structured.get("verified_count"),
    }


def grade_trace(
    runner_result: dict[str, Any] | None,
    reached_final: bool,
    stage_evidence: dict[str, dict[str, Any]],
    quality: dict[str, Any],
    patch_info: dict[str, Any],
    agents: list[str],
    trusted_completion: bool = False,
) -> dict[str, Any]:
    missing_prep = [stage for stage in PREP_STAGES if not stage_evidence[stage]["present"]]
    weak_prep = [stage for stage in PREP_STAGES if stage_evidence[stage]["source"] == "transcript"]
    missing_opt = [stage for stage in OPT_STAGES if not stage_evidence[stage]["present"]]
    weak_opt = [stage for stage in OPT_STAGES if stage_evidence[stage]["source"] == "transcript"]
    patch_nonempty = bool(quality["patch_nonempty"])
    source_modified = bool(quality["source_modified"])
    runner_status = (runner_result or {}).get("status")
    events: list[dict[str, str]] = []

    def add(kind: str, severity: str, message: str) -> None:
        events.append({"kind": kind, "severity": severity, "message": message})

    late_driver_stop = runner_status != "completed" and trusted_completion
    if runner_status != "completed" and not late_driver_stop:
        add("driver_failed", "error", (runner_result or {}).get("error") or "worker runner did not complete")
    elif late_driver_stop:
        add("driver_late_stop", "warning", (runner_result or {}).get("error") or "runner stopped after trusted completion marker")
    if not reached_final:
        add("missing_final_report", "error", "no formal final report or trusted final marker was found")
    if missing_prep:
        add("pipeline_incomplete", "error", "missing preparation or analysis stages: " + ", ".join(missing_prep))
    if weak_prep:
        add("weak_stage_evidence", "error", "preparation or analysis stages only appear in transcript: " + ", ".join(weak_prep))
    if quality["baseline_blocked"]:
        add("baseline_blocked", "error", "baseline build or setup failed")
    if not quality.get("patch_hygiene_passed", True):
        add("artifact_error", "error", "patch hygiene failed: " + ", ".join(quality.get("patch_hygiene_errors") or ["unknown artifact issue"]))
    if source_modified and not patch_nonempty:
        add("artifact_error", "error", "workdir has source changes but final.patch is empty")
    if patch_nonempty and quality["no_effect_claim"]:
        add("report_inconsistent", "error", "patch is non-empty but report claims no applied changes")
    if patch_nonempty and missing_opt:
        add("applied_unverified", "error", "patch exists but optimization chain is incomplete: " + ", ".join(missing_opt))
    if patch_nonempty and weak_opt:
        add("applied_unverified", "error", "patch exists but optimization stages only appear in transcript: " + ", ".join(weak_opt))
    if patch_nonempty and not quality["functional_test_pass"]:
        add("applied_unverified", "error", "patch exists but functional test pass evidence is missing")
    if patch_nonempty and not quality["performance_measured"]:
        add("applied_unverified", "warning", "patch exists but measured performance evidence is missing")

    status = "analysis_only"
    if runner_status != "completed" and not late_driver_stop:
        status = "driver_failed"
    elif quality["baseline_blocked"]:
        status = "baseline_blocked"
    elif not quality.get("patch_hygiene_passed", True):
        status = "artifact_error"
    elif source_modified and not patch_nonempty:
        status = "artifact_error"
    elif patch_nonempty and quality["no_effect_claim"]:
        status = "report_inconsistent"
    elif missing_prep or weak_prep or not reached_final:
        status = "pipeline_incomplete"
    elif patch_nonempty and not missing_opt and not weak_opt and quality["functional_test_pass"] and quality["performance_measured"]:
        status = "applied_verified"
    elif patch_nonempty:
        status = "applied_unverified"
    elif quality["no_effect_claim"]:
        status = "complete_no_optimization"

    return {
        "status": status,
        "events": events,
        "stage_evidence": stage_evidence,
        "missing_preparation_stages": missing_prep,
        "weak_preparation_stages": weak_prep,
        "missing_optimization_stages": missing_opt if patch_nonempty else [],
        "weak_optimization_stages": weak_opt if patch_nonempty else [],
        "subagents": agents,
        "quality": quality,
        "patch": patch_info,
        "trusted_completion": trusted_completion,
    }


def build_completion_gate(
    reached_final: bool,
    trace_grade: dict[str, Any],
    quality: dict[str, Any],
    patch_info: dict[str, Any],
    structured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    structured = structured or {}
    patch_nonempty = int(patch_info.get("final_patch_bytes") or 0) > 0
    missing_prep = trace_grade.get("missing_preparation_stages") or []
    weak_prep = trace_grade.get("weak_preparation_stages") or []
    missing_opt = trace_grade.get("missing_optimization_stages") or []
    weak_opt = trace_grade.get("weak_optimization_stages") or []
    required_stages_seen = not missing_prep and not weak_prep
    if patch_nonempty:
        required_stages_seen = required_stages_seen and not missing_opt and not weak_opt
    artifact_consistent = not (
        (quality.get("source_modified") and not patch_nonempty)
        or (patch_nonempty and quality.get("no_effect_claim"))
        or not quality.get("patch_hygiene_passed", True)
    )
    evidence_sources = {
        "structured_json": structured.get("json_files") or [],
        "batch_result": structured.get("batch_result_files") or [],
        "baseline_blocked": structured.get("baseline_blocked_files") or [],
        "verification": structured.get("verify_files") or [],
        "stage_sources": {
            stage: evidence.get("source")
            for stage, evidence in (trace_grade.get("stage_evidence") or {}).items()
        },
    }
    return {
        "pipeline_reached_final_report": bool(reached_final),
        "required_stages_seen": bool(required_stages_seen),
        "patch_collected": bool(patch_nonempty),
        "functional_verified": bool(quality.get("functional_test_pass")),
        "performance_measured": bool(quality.get("performance_measured")),
        "artifact_consistent": bool(artifact_consistent),
        "patch_hygiene_passed": bool(quality.get("patch_hygiene_passed", True)),
        "evidence_sources": evidence_sources,
        "status": trace_grade.get("status"),
    }


def collect_target_result(
    target_id: str,
    workdir: Path,
    target_out: Path,
    log_dir: Path | None = None,
    runner_result: dict[str, Any] | None = None,
    source_project: Path | None = None,
) -> dict[str, Any]:
    workdir = workdir.expanduser().resolve()
    target_out = target_out.expanduser().resolve()
    if not workdir.exists():
        raise FileNotFoundError(f"workdir not found: {workdir}")
    if not workdir.is_dir():
        raise NotADirectoryError(f"workdir is not a directory: {workdir}")
    if runner_result is not None:
        ContractValidator.expect_dict(runner_result, "runner_result")
    target_out.mkdir(parents=True, exist_ok=True)
    previous_result = read_json(target_out / "target_result.json", strict=True, contract_name="target_result")

    if log_dir is None:
        log_dir = target_out / "claude_log"
    log_dir = log_dir.expanduser().resolve()

    for name in ["transcript.md", "transcript_clean.md", "raw_terminal.log", "events.jsonl", "session_meta.json"]:
        copy_if_exists(log_dir / name, target_out / name)

    reports_root = workdir / "optimization_reports"
    copy_if_exists(reports_root, target_out / "optimization_reports")
    target_reports_root = target_out / "optimization_reports"

    patch_info = write_patch(workdir, target_out / "final.patch")
    base = str(patch_info["base"])
    _, git_log = run_text(["git", "log", "--oneline", "--decorate", f"{base}..HEAD"], workdir)
    (target_out / "internal_git_log.txt").write_text(git_log, encoding="utf-8")
    _, status_text = run_text(["git", "status", "--short"], workdir)
    source_status_current = None
    if source_project and (source_project / ".git").exists():
        _, source_status_current = run_text(["git", "status", "--short"], source_project)

    event_text, agents = read_event_text_and_agents(target_out / "events.jsonl")
    transcript_source = "events.jsonl"
    transcript_text = event_text or read_claude_output_events(target_out / "events.jsonl")
    if not transcript_text:
        transcript_source = "transcript files"
        transcript_text = read_text(target_out / "transcript_clean.md") + "\n" + read_text(target_out / "transcript.md")
    usage = transcript_token_stats(transcript_text, transcript_source)
    (target_out / "transcript_tokens.json").write_text(
        json.dumps(usage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    interactions = read_auto_interactions(target_out / "events.jsonl")
    report_dir, final_report_path = find_final_report(workdir)
    report_final_summary = read_text(final_report_path) if final_report_path else ""
    report_text = ""
    if report_dir:
        for path in sorted(report_dir.rglob("*.md")):
            report_text += "\n" + read_text(path)
    final_text_excerpt = report_final_summary.strip() or final_excerpt(transcript_text)
    (target_out / "final_summary.md").write_text(final_text_excerpt.strip() + "\n", encoding="utf-8")

    transcript_final_marker = final_found(transcript_text)
    trusted_completion_markers = completion_markers(workdir, target_out, log_dir)
    trusted_completion = bool(report_final_summary.strip() or trusted_completion_markers or final_found(report_text))
    reached_final = trusted_completion
    structured = collect_structured_evidence(target_out)
    stage_evidence, combined_report_text = collect_stage_evidence(target_out, transcript_text, structured)
    quality = parse_quality_signals("\n".join([transcript_text, report_text, combined_report_text]), status_text, patch_info, structured)
    trace_grade = grade_trace(
        runner_result,
        reached_final,
        stage_evidence,
        quality,
        patch_info,
        agents,
        trusted_completion=trusted_completion,
    )
    (target_out / "trace_grade.json").write_text(
        json.dumps(trace_grade, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    completion_gate = ContractValidator.validate_completion_gate(
        build_completion_gate(reached_final, trace_grade, quality, patch_info, structured)
    )
    (target_out / "completion_gate.json").write_text(
        json.dumps(completion_gate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    status = str(trace_grade["status"])
    blocker = None
    if status not in SUCCESS_STATUSES:
        event_messages = [event["message"] for event in trace_grade["events"] if event["severity"] == "error"]
        blocker = "; ".join(event_messages) or (runner_result or {}).get("error") or "pipeline did not satisfy strict result gates"

    completion_status = completion_status_for_result(runner_result, reached_final, status)
    run_status = "completed" if completion_status == "completed" else "failed"
    pipeline_status = "completed" if reached_final else "incomplete"

    result: dict[str, Any] = {
        "target_id": target_id,
        "status": status,
        "quality_status": status,
        "run_status": run_status,
        "pipeline_status": pipeline_status,
        "completion_status": completion_status,
        "legacy_runner_status": (runner_result or {}).get("status"),
        "blocker": blocker,
        "reached_final_summary": reached_final,
        "transcript_final_marker": transcript_final_marker,
        "collected_at": utc_now(),
        "source_project": str(source_project.resolve()) if source_project else None,
        "workdir": str(workdir),
        "target_out": str(target_out),
        "log_dir": str(log_dir),
        "optimization_reports": str(target_reports_root) if target_reports_root.exists() else None,
        "transcript_clean": str(target_out / "transcript_clean.md") if (target_out / "transcript_clean.md").exists() else None,
        "transcript_token_stats": usage,
        "transcript_token_stats_path": str(target_out / "transcript_tokens.json"),
        "patch": str(target_out / "final.patch"),
        "committed_patch": str(target_out / "committed.patch"),
        "working_tree_patch": str(target_out / "working_tree.patch"),
        "patch_info": patch_info,
        "patch_manifest": str(target_out / "patch_manifest.json"),
        "patch_excluded_pathspecs": PATCH_EXCLUDE_PATHS,
        "final_summary_excerpt": str(target_out / "final_summary.md"),
        "formal_final_report": str(final_report_path) if final_report_path else None,
        "trusted_completion_markers": trusted_completion_markers,
        "structured_evidence": structured,
        "completion_gate": completion_gate,
        "completion_gate_path": str(target_out / "completion_gate.json"),
        "trace_grade": str(target_out / "trace_grade.json"),
        "internal_git_log": str(target_out / "internal_git_log.txt"),
        "internal_commits": [line for line in git_log.splitlines() if line.strip()],
        "workdir_status_short": status_text,
        "runner": runner_result or {},
        "auto_interactions": interactions,
    }
    for key in ["source_status_before", "source_status_after", "source_unchanged"]:
        if key in previous_result:
            result[key] = previous_result[key]
    if source_status_current is not None:
        result["source_status_current"] = source_status_current
        if "source_unchanged" not in result:
            result["source_unchanged"] = source_status_current == ""
    (target_out / "target_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = [
        f"# Target Report: {target_id}",
        "",
        f"- Status: `{status}`",
        f"- Run status: `{run_status}`",
        f"- Pipeline status: `{pipeline_status}`",
        f"- Completion status: `{completion_status}`",
        f"- Quality status: `{status}`",
        f"- Legacy runner status: `{(runner_result or {}).get('status')}`",
        f"- Reached final summary: `{reached_final}`",
        f"- Transcript final marker: `{transcript_final_marker}`",
        f"- Source project: `{result['source_project']}`",
        f"- Temporary workdir: `{workdir}`",
        f"- Claude log: `{log_dir}`",
        f"- Transcript tokens: `{usage['token_count']:,}` (method: `{usage['method']}`, source: `{usage['source']}`)",
        f"- Patch: `{target_out / 'final.patch'}`",
        f"- Committed patch: `{target_out / 'committed.patch'}`",
        f"- Working tree patch: `{target_out / 'working_tree.patch'}`",
        f"- Patch manifest: `{target_out / 'patch_manifest.json'}`",
        f"- Final summary excerpt: `{target_out / 'final_summary.md'}`",
        f"- Formal final report: `{final_report_path or ''}`",
        f"- Trusted completion markers: `{', '.join(trusted_completion_markers) or ''}`",
        f"- Trace grade: `{target_out / 'trace_grade.json'}`",
        f"- Completion gate: `{target_out / 'completion_gate.json'}`",
        f"- Internal git log: `{target_out / 'internal_git_log.txt'}`",
    ]
    if "source_unchanged" in result:
        report.append(f"- Source unchanged: `{result['source_unchanged']}`")
    if blocker:
        report.extend(["", "## Blocker", "", blocker])
    report.extend(["", "## Internal Commits", ""])
    report.append(git_log.strip() or "No internal commits after baseline.")
    report.extend(["", "## Interaction And Workflow Evidence", ""])
    report.append(f"- Transcript: `{target_out / 'transcript_clean.md'}`")
    report.append(f"- Auto replies sent: `{len(interactions)}`")
    if interactions:
        report.append(f"- Auto reply sequence: `{' | '.join(interactions[:20])}`")
        if len(interactions) > 20:
            report.append(f"- Auto reply sequence truncated: `{len(interactions) - 20}` more")
    runner_status = (runner_result or {}).get("status")
    report.append(f"- Runner status: `{runner_status}`")
    report.append(f"- Final marker seen: `{(runner_result or {}).get('final_marker_seen')}`")
    report.append(f"- Missing preparation stages: `{', '.join(trace_grade['missing_preparation_stages']) or 'none'}`")
    report.append(f"- Weak preparation stages: `{', '.join(trace_grade['weak_preparation_stages']) or 'none'}`")
    report.append(f"- Missing optimization stages: `{', '.join(trace_grade['missing_optimization_stages']) or 'none'}`")
    report.append(f"- Weak optimization stages: `{', '.join(trace_grade['weak_optimization_stages']) or 'none'}`")
    report.append(f"- Structured JSON evidence files: `{len(structured.get('json_files') or [])}`")
    report.extend(["", "## Completion Gate", ""])
    for key in [
        "pipeline_reached_final_report",
        "required_stages_seen",
        "patch_collected",
        "functional_verified",
        "performance_measured",
        "artifact_consistent",
        "patch_hygiene_passed",
    ]:
        report.append(f"- {key}: `{completion_gate[key]}`")
    report.append("- Patch excludes common build outputs, helper files, and copied optimization report artifacts.")
    report.append(f"- Final patch bytes: `{patch_info['final_patch_bytes']}`")
    if report_dir:
        report.append(f"- Optimization reports copied from `{report_dir}`")
    else:
        report.append("- No `optimization_reports/run_*` directory was found.")
    if trace_grade["events"]:
        report.extend(["", "## Strict Gate Events", ""])
        for event in trace_grade["events"]:
            report.append(f"- `{event['severity']}` `{event['kind']}`: {event['message']}")
    if final_text_excerpt.strip():
        report.extend(["", "## Final Summary Evidence", "", "```text", final_text_excerpt.strip(), "```"])
    report.extend(["", "## Workdir Status", "", "```text", status_text.rstrip(), "```", ""])
    (target_out / "target_report.md").write_text("\n".join(report), encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--target-out", required=True)
    parser.add_argument("--log-dir")
    parser.add_argument("--source-project")
    parser.add_argument("--runner-result-json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner_result = json.loads(args.runner_result_json) if args.runner_result_json else None
    collect_target_result(
        target_id=args.target_id,
        workdir=Path(args.workdir),
        target_out=Path(args.target_out),
        log_dir=Path(args.log_dir) if args.log_dir else None,
        runner_result=runner_result,
        source_project=Path(args.source_project) if args.source_project else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
