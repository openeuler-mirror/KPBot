---
name: batch-drive-optimize-pipeline
description: Batch-drive real Claude Code sessions through the KunpengAccelerationLibOptimization /kpbot-code-optimizer workflow. Use when the user wants unattended batch evaluation of optimization skills across many C/C++ libraries, operators, functions, or test cases, with original repos left untouched, worker Claude launched per target, all interactions auto-answered, and detailed reports/transcripts/patches collected. Includes a real-Claude self-test that creates a virtual C project and fails if Claude exits early, stalls, needs manual input, or does not reach a final optimization result.
platform: claude-only
---

# Batch Drive Optimize Pipeline

## Purpose

Run `/kpbot-code-optimizer` repeatedly through real worker Claude sessions, using `drive-claude-optimize-pipeline` as the single-target interaction model.

This skill is an outer batch evaluator. It does not optimize directly and does not bypass `/kpbot-code-optimizer`. For every target, it creates an isolated temporary copy, launches worker Claude, sends `/kpbot-code-optimizer`, auto-answers prompts, waits for the final pipeline result, and collects reports.

## Hard Rules

- Original repositories must not be edited, committed, stashed, or cleaned.
- All source changes happen only in per-target working copies under the output directory.
- Worker Claude may commit/stash inside those temporary copies because the existing pipeline assumes that behavior.
- The batch driver must continue to the next target after any single-target failure.
- A target is failed if worker Claude cannot start, stalls past idle timeout, repeats the same prompt too many times, exits before final result, or requires manual input.
- The real-Claude self-test must not use a fake Claude. Missing Claude is a test failure.

## Slash Command Contract

`/batch-drive-optimize-pipeline` is a deterministic launcher by default, not an interactive run-mode wizard.

- Do not present a "Self-test first / Run full batch / Run specific targets" menu unless the user explicitly passes `--interactive`.
- With no arguments, immediately run the full batch using `batch_optimize_manifest.yaml` in the current working directory.
- With no explicit output path, write to `_batch_optimize_results_YYYYMMDD_HHMMSS` beside the manifest.
- Use `CLAUDE_BIN`, or `claude` on `PATH`, unless the user explicitly supplies `--claude-bin`.
- Only run the real-Claude self-test when the user explicitly passes `--self-test` or `--self-test-real-claude`.
- For invalid arguments, missing default manifest, unknown target ids, or missing Claude binary, fail fast with a concrete error instead of asking follow-up questions.

Default slash-command mapping:

```bash
python3 .opencode/skills/batch-drive-optimize-pipeline/scripts/batch_drive_optimize.py \
  --manifest batch_optimize_manifest.yaml \
  --non-interactive
```

Target subset mapping:

```bash
python3 .opencode/skills/batch-drive-optimize-pipeline/scripts/batch_drive_optimize.py \
  --manifest batch_optimize_manifest.yaml \
  --targets faiss,isa_l,x264 \
  --non-interactive
```

Self-test mapping:

```bash
python3 .opencode/skills/batch-drive-optimize-pipeline/scripts/batch_drive_optimize.py \
  --self-test \
  --non-interactive
```

## Quick Start

For a real self-test that creates and optimizes a virtual C project:

```bash
python3 .opencode/skills/batch-drive-optimize-pipeline/scripts/batch_drive_optimize.py \
  --self-test \
  --claude-bin /absolute/path/to/claude
```

For batch evaluation:

```bash
python3 .opencode/skills/batch-drive-optimize-pipeline/scripts/batch_drive_optimize.py \
  --manifest targets.yaml \
  --claude-bin /absolute/path/to/claude
```

`--claude-bin` may be omitted when `CLAUDE_BIN` or `claude` on `PATH` is valid.
`--out` may be omitted; the script then creates a timestamped output directory beside the manifest, or under `/tmp` for self-test.

## Manifest

Use JSON or a simple YAML file. The minimal target only needs `project_path`; all function, test, benchmark, and hotspot fields are optional overrides.

Sparse auto-discovery target:

```yaml
run:
  max_parallel: 1
  timeout_minutes: 180
  idle_timeout_minutes: 10
  resume_attempts: 2
  transcript_tail_lines: 120

targets:
  - id: auto_project
    project_path: /absolute/path/to/project
    optimization_goal: ARM64/NEON throughput
```

Fully pinned target:

```yaml
run:
  max_parallel: 1
  timeout_minutes: 180
  idle_timeout_minutes: 10
  resume_attempts: 2
  keep_workdirs: on_failure

targets:
  - id: vec_add
    project_path: /absolute/path/to/project
    mode: function
    code_path: src/vec_add.c
    function_name: vec_add
    test_method: make test && make bench
    benchmark_command: make bench
    optimization_goal: ARM64/NEON throughput
```

The first version intentionally runs serially (`max_parallel: 1`) so benchmark resources and Claude sessions do not interfere with each other.

## Managed Batch Workflow

The default driver is a managed outer workflow. It keeps `/kpbot-code-optimizer` as the only optimizer, but the batch layer owns target isolation, Claude worker attempts, resume prompts, completion gates, and final artifact collection. Estimated-token context limits are disabled by default; set `context_soft_limit_tokens`, `context_hard_limit_tokens`, or `resume_prompt_max_tokens` only when an explicit bounded run is needed.

- Use `--legacy-driver` only when the previous long-session behavior is explicitly needed for comparison.
- Each target gets an independent temporary workdir, state files, attempt log, usage summary, timing summary, completion gate, report, final summary, and patch.
- Resume prompts must be bounded structured summaries: completed stages, known artifacts, missing gate fields, last termination reason, and a small transcript tail only.
- If explicit positive token thresholds are configured, `context_soft_limit_tokens` requests a checkpoint rollover and `context_hard_limit_tokens` stops the worker before continuing. With the default `0` values, the batch driver does not impose estimated-token limits and only reacts if the Claude UI itself reports context exhaustion.
- Monitoring must use the bounded monitor command or read `status_snapshot.json`, `targets/<id>/target_state.json`, `targets/<id>/usage.json`, and `targets/<id>/timing.json` before considering transcript tails.

## Auto Discovery

Before launching worker Claude, the batch driver performs read-only answer-bank discovery on the original project. This mirrors `drive-claude-optimize-pipeline` and is only used to help `/kpbot-code-optimizer` answer GatherContext prompts; it does not replace profiling, decomposition, hotspot analysis, optimization, verification, or reporting.

Discovery collects:

- Project structure and build files such as `Makefile`, `CMakeLists.txt`, `meson.build`, `build.ninja`, `pyproject.toml`, `Cargo.toml`, and `go.mod`.
- Correctness and benchmark command candidates from Make targets, CTest listings, Meson/Pytest/Cargo/Go signals, executable test files, and benchmark-like paths.
- Static hotspot/function candidates from C/C++/assembly source files, loop density, performance-like names, SIMD/assembly signals, and references from tests/benchmarks.
- Existing profiling artifacts such as flamegraphs, perf files, profile outputs, benchmark logs, and bench reports.

Inference rules:

- Explicit manifest fields always win.
- If `code_path` and `function_name` are supplied, choose `函数优化`.
- If no explicit function is supplied, recommend `用例优化` by default so `/kpbot-code-optimizer` runs its normal auto-mode profiling path through `DecomposeTasks`.
- Static hotspot candidates are retained only as unmeasured hints in the answer bank; they must not switch a sparse target into `函数优化`.
- Pass the discovered test/benchmark/hotspot candidates to the worker and let `/kpbot-code-optimizer` decompose and confirm the true hotspot.
- The original project is only inspected with read-only probes; all edits, builds, tests, benchmarks, commits, and reports happen inside the temporary copy through worker Claude.

## Output Contract

The output directory contains:

```text
  summary.md
  summary.json
  ability_evidence.json
  ability_evidence.md
  status_snapshot.json
  targets/<id>/
  answer_bank.txt
  discovery.md
  discovery.json
  target_state.json
  attempts.jsonl
  usage.json
  timing.json
  completion_gate.json
  patch_manifest.json
  target_report.md
  target_result.json
  ability_evidence.json
  ability_evidence.md
  final.patch
  final_summary.md
  internal_git_log.txt
  transcript.md
  transcript_clean.md
  raw_terminal.log
  optimization_reports/
workdirs/<id>/
```

`target_report.md` must include optimization content, workflow/stage evidence, interaction summary, final status, blocker if any, and artifact paths.
`final.patch` is a clean source/config patch. It is assembled from an allowlist of tracked source, header, assembly, and build-system/config files, and excludes build outputs, perf data, compressed/test payloads, binaries, `.opencode/**`, helper files, and copied optimization report artifacts from committed, working-tree, and untracked changes.
`patch_manifest.json` records every included and excluded path plus `binary_detected`, `large_file_detected`, `deleted_test_sources_detected`, and `patch_hygiene_passed`. If hygiene fails, the target quality status is `artifact_error` and must not count as successful.
`final_summary.md` prefers the worker pipeline's formal final report from `optimization_reports/**/final_summary.md`, `FINAL_SUMMARY.md`, `final_report.md`, or `FINAL_REPORT.md`; if unavailable, it falls back to a transcript excerpt around a trusted final marker.
Completion detection must also accept structured markers such as `.batch_optimize_result.json`, `claude_log/final_result.json`, `TARGET_COMPLETE.json`, `BATCH_SUMMARY.md`, `SESSION_COMPLETE`, `BATCH_END`, and `SESSION_END` when they report a complete status.
`completion_gate.json` is the structured success gate and records `pipeline_reached_final_report`, `required_stages_seen`, `patch_collected`, `functional_verified`, `performance_measured`, `artifact_consistent`, `patch_hygiene_passed`, and `evidence_sources`.
`target_result.json` separates `run_status`, `pipeline_status`, and `quality_status`: `run_status` is the batch-driver outcome, `pipeline_status` says whether `/kpbot-code-optimizer` reached a final report, and `quality_status` / legacy `status` are the strict result gate (`applied_verified`, `complete_no_optimization`, `applied_unverified`, `baseline_blocked`, `pipeline_incomplete`, `driver_failed`, `artifact_error`, `report_inconsistent`, etc.).
`optimization_reports/run_*/stages/*.json`, `optimization_reports/run_*/points/*.json`, and `optimization_reports/run_*/batch_result.json` are strong evidence. Markdown summaries and transcript text are fallback evidence only and cannot by themselves satisfy strict stage gates.
`status_snapshot.json` includes a bounded `human_summary` with one-line per-target progress, active target, phase label, elapsed/idle time, task counts, recent stage digest, optimization-point digest, auto-reply counts, and artifact paths. It must never include transcript bodies.
`usage.json` records per-target and per-attempt token estimates: worker input/output, transcript tokens, driver prompt tokens, peak context estimate, method, and whether the numbers are exact. Treat these as estimates unless `is_exact=true`.
`timing.json` records per-target and per-attempt wall time, active worker time, idle seconds, driver overhead, attempt count, start/end timestamps, and termination reasons.
`ability_evidence.json` / `ability_evidence.md` records structured optimization evidence for the target and whole batch after collection finishes. It does not calculate numeric scores or grades. It records status counts, evidence booleans, extracted speedup when present, trace status, missing or weak stages, and risk flags such as driver failure, incomplete pipeline, baseline blockage, artifact inconsistency, unverified patches, missing performance measurement, no code change, or source repository mutation.

## Monitoring Contract

Monitor agents must use bounded status artifacts and must not read full transcript files into model context.

Preferred live status command:

```bash
out=$(ls -dt _batch_optimize_results_* | head -n 1)
python3 .opencode/skills/batch-drive-optimize-pipeline/scripts/batch_drive_optimize.py --monitor --out "$out"
```

This command refreshes and prints only `status_snapshot.json`-level data. It does not launch Claude, does not parse the manifest, and does not read transcript bodies.

Preferred live status source when reading files directly:

```bash
cat _batch_optimize_results_*/status_snapshot.json
```

Per-target bounded status sources:

```bash
python3 -m json.tool targets/<id>/target_state.json
python3 -m json.tool targets/<id>/usage.json
python3 -m json.tool targets/<id>/timing.json
python3 -m json.tool targets/<id>/completion_gate.json
```

Fallback for the active target:

```bash
python3 -m json.tool targets/<id>/progress.json
```

If logs are needed, read only small tails, for example:

```bash
tail -n 120 targets/<id>/claude_log/transcript_clean.md
```

Never summarize or paste full `events.jsonl`, `transcript.md`, `transcript_clean.md`, or `raw_terminal.log`; these files can grow to MB scale and overflow model/API input limits. `status_snapshot.json` intentionally contains only phase, status, file sizes, timestamps, and artifact paths, not transcript bodies.

If a monitor session reports `Range of input length should be [1, 202745]`, treat that as a monitor-context overflow. Stop or restart only the monitor context, not the worker process, and resume observation through `--monitor` or `status_snapshot.json`.

## Interaction Policy

The automatic worker replies follow the single-target drive skill:

- Startup: send `/kpbot-code-optimizer` without changing Claude's default effort.
- Mode: choose `自动模式 (auto)` when prompted.
- GatherContext: answer from the manifest plus the generated answer bank.
- Sandbox/permissions: choose the option that keeps the current session moving, usually `配置 permissions`.
- Stage confirmation: choose `同意，继续下一步` unless the output contradicts manifest paths or commands.
- AnalyzeHotspot supplements: do not invent extra optimization points; accept Claude-discovered points.
- Caller-context analysis: skip unless explicitly requested in the manifest.
- Round continuation: choose `自动继续` or the strongest available continue/retry/resume option.
- Auto-reply loops are not progress. Repeated `nudge_continue` is capped; auto-reply echoes such as `继续` or the forced finalization prompt must not refresh semantic-progress timers.
- Early exit: relaunch the same temporary copy with the resume prompt until `resume_attempts` is exhausted.

## Bundled Scripts

- `scripts/batch_drive_optimize.py`: CLI entrypoint, monitor entrypoint, project discovery helpers, worker launch adapter, and real self-test fixture.
- `scripts/config_loader.py`: manifest/default-output/Claude binary resolution, argument validation, target filtering, and run-context assembly.
- `scripts/pipeline.py`: top-level batch pipeline orchestration, scheduler wiring, summary emission, and final exit-code decision.
- `scripts/auto_claude_logged.py`: PTY runner main loop, auto replies, idle timeout, hard timeout, final-result detection. It must launch workers through the restricted `child_env()` allowlist, not raw `os.environ`, so secrets and delegated auth sockets are not inherited by the child process.
- `scripts/config.py`: auto-runner CLI defaults.
- `scripts/log_handler.py`: transcript cleaning, event logging, and `Recorder`.
- `scripts/session_manager.py`: auto-runner session metadata and running-state initialization.
- `scripts/data_loader.py`: JSON/YAML manifest loading.
- `scripts/task_scheduler.py`: target id normalization, target selection/listing, serial target scheduling, and target-local driver-failure artifact recording.
- `scripts/stage_executor.py`: per-target attempt/resume controller and timeout-aware worker invocation loop.
- `scripts/pipeline_core.py`: batch run configuration merge helpers.
- `scripts/result_collector.py`: result collection adapter used by the batch pipeline.
- `scripts/batch_runtime.py`: shared bounded file IO and retry helpers used by orchestration code.
- `scripts/safe_env.py`: child-process environment allowlist and sensitive-key filtering.
- `scripts/create_virtual_project.py`: creates the real self-test C project with `vec_add`, correctness test, benchmark, Makefile, and baseline git commit.
- `scripts/collect_result.py`: extracts reports, transcripts, patch, git log, final-result evidence, and JSON/Markdown summaries.
