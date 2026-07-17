---
name: drive-claude-optimize-pipeline
description: Drive Claude Code through the KunpengAccelerationLibOptimization /kpbot-code-optimizer workflow as a strict interaction proxy. Use when the user wants Codex or another Claude agent to enter, spawn, or supervise a Claude terminal session, especially when the user gives only a project or incomplete optimization context. Fill missing answers for /kpbot-code-optimizer prompts by read-only discovery of project structure, candidate functions, test cases, and test/benchmark tools; keep the worker Claude as the sole workflow executor; prefer a separate worker Claude when Claude is the outer proxy; always choose continuation over stopping until Claude clearly reaches the final result; and save the full outer-agent/Claude transcript.
---

# Drive Claude Optimize Pipeline

## Purpose

Operate as a narrow interaction proxy for Claude Code running `/kpbot-code-optimizer`. The worker Claude owns the optimization workflow. The outer proxy agent supplies answers to the worker Claude's interactive prompts, keeps the session alive, and saves the transcript.

When the user does not specify functions, test cases, test commands, or benchmark tools, the outer proxy performs read-only discovery only to build an answer bank for `/kpbot-code-optimizer` prompts. Discovery supports the main pipeline; it does not replace GatherContext, DecomposeTasks, AnalyzeHotspot, profiling, verification, or any optimization stage.

The outer proxy may be Codex or another Claude agent. When Claude is acting as the outer proxy instead of the worker, it may open a child Claude session in a subagent or child terminal and drive that child Claude with the same rules Codex would use.

This skill is deliberately narrower than an autonomous optimization operator: do not edit the target project, do not modify pipeline skills, do not run independent optimization commands, and do not override the worker Claude's stage decisions except by answering its own questions.

## Operating Contract

1. Start from the user's target project directory, or `cd` to the absolute project path the user gives.
2. Build an answer bank from explicit user context. If key fields are missing, run Answer Bank Discovery from the project directory to find candidate functions, test cases, test suites, benchmark tools, commands, and confidence notes.
3. Choose the execution topology:
   - If the active agent is the same Claude session that should execute the optimization, run the workflow directly in that session.
   - If the active agent is Codex or a Claude proxy supervising another Claude, launch a worker Claude Code session from the project directory. A Claude proxy must first try to use an available subagent, child terminal, or equivalent independent interactive session for this worker Claude.
4. In the worker Claude session, send `/kpbot-code-optimizer` without changing Claude's default effort.
5. Reply to the worker Claude only when it asks for input or exits before completing the workflow.
6. Use the user's explicit context first, enriched by the answer bank. If a required answer is still missing, inspect the project with read-only commands and infer the best candidate answer.
7. Do not change any workflow, skill, source file, build config, git state, dependency, Claude setting, or reasoning effort yourself, except the user-authorized permissions setup described below.
8. Let the worker Claude perform all edits, builds, tests, profiling, verification, stash/commit operations, and report writing.
9. Continue until the worker Claude clearly reaches the final pipeline summary or final completed result, or until the user explicitly stops the run.
10. Save all worker-Claude output and every outer-agent-to-Claude reply after the run.

## Continuation Priority

Treat continuation as the highest-priority interaction rule. If Claude presents any interactive choice that includes continuing, retrying, proceeding, resuming, going to the next stage, going to the next round, auto-continuing, or terminating/stopping, always choose the option that keeps `/kpbot-code-optimizer` moving forward. This applies even when the prompt is phrased as `继续 or 终止`, `continue or stop`, `是否继续`, `是否进入下一轮`, `是否重试`, or similar wording.

Do not voluntarily terminate, stop, pause, or summarize early. Do not treat a warning, failed attempt, skipped optimization point, regression, or blocker-like message as terminal if Claude offers any continue/retry/resume option. Stop only when Claude clearly reports the final pipeline summary/final result has been produced, or when the user explicitly tells the outer proxy to stop.

## Startup Sequence

After the worker Claude session opens, start the optimization workflow without changing Claude's default reasoning effort:

```text
/kpbot-code-optimizer
```

Do not change the reasoning effort by default for this proxy workflow unless the user explicitly requests a specific effort.

## Transcript Logging

Prefer the bundled wrapper so every terminal byte and every reply is recorded. Resolve the script path relative to this skill directory:

```bash
python3 scripts/run_claude_logged.py --project-path <project_path> -- claude
```

If the command is omitted, the wrapper defaults to `claude`:

```bash
python3 scripts/run_claude_logged.py --project-path <project_path>
```

The wrapper resolves `claude` using `CLAUDE_BIN`, the current `PATH`, common user install locations under `$HOME` such as `~/.local/bin`, and prefix environment variables such as `HOMEBREW_PREFIX`. This handles Codex or noninteractive shell sessions whose `PATH` does not match the user's interactive Terminal.

The wrapper stores logs outside the target project by default:

```text
${CODEX_HOME:-~/.codex}/claude-optimize-pipeline-logs/<project>-<timestamp>/
```

It writes:

- `session_meta.json`: command, project path, start/end time, exit code.
- `events.jsonl`: timestamped `claude_output` and `codex_to_claude` chunks.
- `transcript.md`: raw Markdown transcript containing terminal chunks.
- `transcript_clean.md`: cleaned Markdown transcript with ANSI/control redraw noise removed.
- `raw_terminal.log`: raw terminal stream plus input markers.

These transcript files are intentionally complete and are not redacted. Do not paste API keys, passwords, tokens, private certificates, or other secrets into the Claude session. If sensitive content is accidentally captured, treat the log directory as sensitive, remove or protect it immediately, and rotate the exposed credential.

If the wrapper cannot be used, manually maintain equivalent transcript files in the same external log root. In a Claude-proxy-to-Claude-worker setup, the log labels may remain `codex_to_claude` for wrapper compatibility, but the content must still represent the outer proxy's actual replies. Do not place transcript files inside the target project unless the user explicitly asks for that.

## Agent Topology and Claude Compatibility

This skill must remain portable when installed under either Codex or Claude skill roots. Do not hard-code machine-specific skill paths in examples or instructions. Use paths relative to the skill directory, especially `scripts/run_claude_logged.py`, for bundled resources.

Use these role names consistently:

- `outer proxy`: the agent reading this skill and supervising the interaction. It may be Codex or Claude.
- `worker Claude`: the Claude Code session that runs `/kpbot-code-optimizer` and owns all optimization work.

If the active agent is already the Claude Code session that the user explicitly intends to use as the worker, run `/kpbot-code-optimizer` directly and follow the continuation rules above.

If the active agent is Claude and the user invoked this skill to supervise Claude work, prefer Claude-proxy-to-worker-Claude topology. The outer Claude must first attempt to launch a nested worker Claude through a subagent, child terminal, or equivalent independent interactive mechanism, then behave like Codex as a proxy: gather only missing answer-bank context read-only, answer the worker Claude tersely, prefer continuation, keep the worker Claude as the only agent that edits or tests the target project, and save a transcript for both sides of the interaction. Do not silently solve the optimization inside the current Claude session just because direct execution is possible. Fall back to direct execution only when the user explicitly asks the active Claude to be the worker or the environment cannot provide an independent child Claude session; state that fallback limitation in the final report.

## Answer Bank Discovery and Context Collection

Before launching Claude, construct an answer bank from explicit user input. Run read-only discovery only for missing or ambiguous fields. This lets the user invoke the skill with just a project path while still giving `/kpbot-code-optimizer` useful candidate answers.

Explicit fields to capture when present:

- `project_path`: absolute target project directory.
- `project_structure`: source directories, build files, important scripts, existing build directories, and language mix.
- `build_system`: CMake, Make, Ninja, Meson, Bazel, Python, Rust, Go, custom scripts, or unknown.
- `test_tools`: discovered correctness and benchmark frameworks/tools.
- `test_command_candidates`: likely correctness-test commands, not yet executed by the outer proxy.
- `benchmark_command_candidates`: likely performance-test commands, not yet executed by the outer proxy.
- `hotspot_candidates`: likely hot functions/files/benchmarks and why they look hot.
- `profiling_artifacts`: existing flamegraphs, perf reports, benchmark logs, profiler output, or prior optimization reports found in the tree.
- `optimization_mode`: `function` or `testcase`.
- `code_path`: absolute source file path for function optimization.
- `function_name`: function to optimize.
- `test_cases`: selected correctness/performance cases.
- `test_method`: exact command Claude should use for the selected cases.
- `build_command`: build command if known.
- `benchmark_command`: benchmark command if different from `test_method`.
- `optimization_goal`: throughput, latency, memory bandwidth, cache behavior, OpenBLAS gap, or user-specific goal.
- `target_arch`: Kunpeng, ARMv8, AArch64, NEON, SVE, SME, or host ARM64.
- `round_policy`: continue one round at a time or auto-continue; do not stop after the first verified improvement unless the user explicitly stops the run.
- `commit_policy`: whether Claude may commit verified changes.
- `instruction_query_validation`: whether the user's main goal is to validate `arm_query.py` / ARM intrinsic instruction lookup integration.
- `answer_bank_confidence`: high/medium/low confidence and the reason, especially when hotspots are static candidates rather than measured profiling results.

If `instruction_query_validation` is true, include this requirement whenever Claude asks for goal, constraints, or whether to continue:

```text
本轮主要验证 ARM 指令查询接入。涉及 NEON/SVE/SVE2 intrinsic、inline asm 或汇编指令事实时，请使用当前 pipeline skill repo 内统一入口 `<pipeline_root>/skills/arm-instructions-query/scripts/arm_query.py ... --json`，不要使用 `query.py --json`；将 evidence 写入阶段报告。若环境无法构建，请继续只读分析并完成查询 evidence 验证。
```

When `project_structure` or `build_system` is missing, use read-only project-structure probes such as:

```bash
pwd
git rev-parse --show-toplevel
git status --porcelain
rg --files
find . -maxdepth 3 \( -name CMakeLists.txt -o -name Makefile -o -name meson.build -o -name build.ninja \)
find . -maxdepth 4 -type f \( -name pyproject.toml -o -name setup.py -o -name pytest.ini -o -name tox.ini -o -name package.json -o -name Cargo.toml -o -name go.mod -o -name WORKSPACE -o -name MODULE.bazel \)
find . -maxdepth 3 -type d \( -name src -o -name include -o -name test -o -name tests -o -name bench -o -name benchmark -o -name benchmarks -o -name build \)
```

When test cases, test suites, test commands, or benchmark commands are missing, discover candidates with static or list-only probes:

```bash
find . -maxdepth 4 -type f \( -perm -111 -o -name 'test_*' -o -name '*_test' -o -name 'benchmark_*' -o -name '*bench*' -o -name '*.sh' \)
rg -n "add_test|enable_testing|ctest|gtest|gmock|Catch2|doctest|TEST\\(|TEST_F\\(|BENCHMARK\\(|benchmark|pytest|unittest|cargo test|cargo bench|go test|meson test|ninja test|make test|perf|flamegraph" .
cd build && ctest -N
cd build && ninja -t targets all
make -n test
cd build && meson test --list
```

Use command-listing probes only when the corresponding tool/build directory exists. If a listing command would build, install, execute tests, mutate files, or is not supported by that tool, skip it and record the static evidence instead.

When the function target or hotspot target is missing, discover candidate functions/files before answering `/kpbot-code-optimizer`. Prefer measured evidence from existing artifacts; otherwise produce static candidates and mark them as unmeasured:

```bash
find . -maxdepth 5 -type f \( -name '*perf*' -o -name '*profile*' -o -name '*flame*' -o -name '*.prof' -o -name 'perf.data' -o -name 'gmon.out' -o -name '*benchmark*.log' -o -name '*bench*.txt' \)
rg -n "hotspot|profile|flamegraph|GFLOPS|GB/s|ns/op|cycles|throughput|latency|slow|bottleneck" .
rg -n "sgemm|gemm|matmul|conv|fft|stencil|sort|hash|compress|crypto|memcpy|memmove|for \\(|while \\(|#pragma omp|neon|sve|sme|aarch64|arm64|SIMD" .
```

Summarize the answer bank as a compact packet:

```text
Answer bank:
- project_root:
- structure/build_system:
- test_tools:
- test_command_candidates:
- benchmark_command_candidates:
- hotspot_candidates:
- profiling_artifacts:
- confidence:
```

Do not run builds, tests, benchmarks, profilers, package installs, formatters, code generators, git mutations, or cleanup commands during answer-bank discovery. If the user gave an exact test, benchmark, or profiling command, record it as a candidate for the worker Claude instead of running it yourself. The worker Claude should confirm or measure true dynamic hotspots inside `/kpbot-code-optimizer`.

## Inference Rules

Use these defaults when Claude asks and the user did not specify the answer:

- Optimization mode:
  - If `code_path` and `function_name` are known, choose `函数优化`.
  - If the answer bank found a high-confidence measured hotspot with a source path and function name, choose `函数优化` and provide the evidence.
  - If benchmark/test entry points are clearer than a single function, choose `用例优化`.
  - If no safe function target is known or hotspots are only static candidates, choose `用例优化`, provide the answer bank, and let `/kpbot-code-optimizer` confirm and decompose hotspots.
- Project path:
  - Use the target directory's git root when available.
  - Otherwise use the current working directory.
- Test case selection:
  - Start from `test_tools`, `test_command_candidates`, and `benchmark_command_candidates` discovered for the answer bank.
  - Prefer performance benchmarks named with `bench`, `benchmark`, `perf`, `speed`, `gemm`, `sgemm`, `matmul`.
  - Include at least one correctness test when available.
  - Avoid examples, docs, install checks, or unrelated integration tests unless no better case exists.
- Test command:
  - Prefer the user's exact command.
  - Else prefer `cd build && ctest -R "<selected1>|<selected2>" --output-on-failure` when `ctest -N` listed matching tests.
  - Else choose the most direct benchmark/test executable command Claude offers.
- Sandbox or permissions setup:
  - When Claude shows the sandbox precheck prompt and offers:
    1. `开启 sandbox（推荐）`
    2. `配置 permissions`
    choose option 2, `配置 permissions`, every time.
  - If Claude asks to edit `.claude/settings*.json` for the permissions whitelist, allow Claude to do it. This user-authorized setting change is part of the proxy workflow.
  - Do not choose option 1 (`开启 sandbox`) unless the user explicitly overrides this rule.
  - If labels differ, choose the option that adds current-session command permissions or a common Bash whitelist, not the option that enables sandbox and requires restarting the session.
- Stage confirmation:
  - Choose "同意，继续下一步" for ordinary completed stages.
  - Choose "不同意，需要重做" only when Claude's summary contradicts explicit user input or lacks a required path/command.
- AnalyzeHotspot supplement:
  - Do not invent extra optimization points.
  - If the user explicitly supplied extra ideas, provide them exactly as user-supplemented points.
  - Otherwise choose the option that accepts Claude's discovered points.
- Round continuation:
  - Continue until the final summary, final completed result, or maximum configured rounds.
  - If Claude offers auto-continue, choose auto-continue.
  - If Claude offers continue versus stop/terminate, choose continue.
  - If Claude offers retry/resume versus stop/terminate after a failed, skipped, or regressed step, choose retry/resume/continue.
- Early exit:
  - If Claude is still interactive but seems to stop before Phase 4, reply `继续`.
  - If the Claude process exits before final summary, relaunch Claude in the same project and send a concise continuation request:

```text
继续上一次 /kpbot-code-optimizer 工作流。请从已有 optimization_reports 和当前仓库状态恢复，完成剩余阶段直到最终汇总。
```

## Claude Reply Discipline

Replies to the worker Claude must be terse and contain only what Claude needs. Do not include commentary meant for the user. Do not explain the outer proxy's reasoning unless Claude asks for rationale.

When the worker Claude asks for project context, optimization mode, test cases, benchmark commands, or hotspot targets, answer from the answer bank first. Provide summarized candidates and confidence, not raw command dumps.

Good replies:

```text
Answer bank:
- project_root: /absolute/path/to/project
- structure/build_system: CMake project with src/, include/, tests/, build/
- test_tools: ctest listed unit and benchmark targets
- test_command_candidates: cd build && ctest -R "unit|correctness" --output-on-failure
- benchmark_command_candidates: cd build && ctest -R "bench|perf" --output-on-failure
- hotspot_candidates: src/gemm.c:sgemm_kernel, static candidate from benchmark names and GEMM patterns; unmeasured
- confidence: medium; worker Claude should confirm with profiling in /kpbot-code-optimizer
选择：用例优化
```

```text
选择：用例优化
项目路径：/absolute/path/to/project
```

```text
同意，继续下一步。
```

```text
继续下一轮 profiling。
```

```text
继续。请完成完整 /kpbot-code-optimizer 工作流，直到最终汇总。
```

Bad replies:

```text
我觉得你可能应该先检查一下，因为这个项目看起来...
```

```text
我自己跑了一下 benchmark，结果是...
```

```text
我修改了你的配置，现在继续。
```

## Completion

After the worker Claude finishes, ensure the transcript files exist and include both sides of the interaction. In the user-facing final response, report only:

- Whether Claude reached the final pipeline summary or where it stopped.
- Transcript directory path.
- Any true blocker Claude reported.

Do not summarize or second-guess optimization results unless the user asks for analysis. Claude's own reports are the source of truth for the workflow outcome.
