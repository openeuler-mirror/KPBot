#!/usr/bin/env python3
"""Regression tests for batch-drive optimize pipeline result handling."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import time
import json
import os
import re
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
TEST_SUBPROCESS_TIMEOUT_SECONDS = 60

import batch_drive_optimize as batch_drive_module  # noqa: E402
from batch_drive_optimize import (  # noqa: E402
    build_answer_bank,
    copy_project,
    discover_project,
    discover_hotspot_candidates,
    ensure_workdir_pipeline_skills,
    filter_manifest_targets,
    install_answer_bank_in_workdir,
    invoke_auto_claude,
    monitor_status_snapshot,
    parse_args,
    render_batch_progress,
    render_monitor_snapshot,
    resolve_claude_bin,
    resolve_default_out,
    resolve_manifest_path,
    validate_args,
    truncate_to_token_budget,
    write_status_snapshot,
    write_summary,
)
from auto_claude_logged import (  # noqa: E402
    answer_for,
    completion_marker_exists,
    context_ui_limit_reached,
    child_env,
    is_meaningful_worker_output,
    max_repeats_for_key,
    render_progress_summary,
)
from ability_evidence import number_value, performance_speedup, summarize_target_evidence  # noqa: E402
from batch_runtime import RetryConfig, retry_call  # noqa: E402
from config_loader import BatchRunContext, load_batch_context  # noqa: E402
from data_loader import load_manifest  # noqa: E402
from pipeline import BatchPipeline  # noqa: E402
from pipeline_core import merge_cli_run_config  # noqa: E402
from stage_executor import StageExecutor, StageExecutorConfig  # noqa: E402
from task_scheduler import TaskScheduler  # noqa: E402
from collect_result import (  # noqa: E402
    copy_if_exists,
    collect_target_result,
    grade_trace,
    number_value as collect_number_value,
    parse_quality_signals,
    transcript_token_stats,
)
from create_virtual_project import prepare_output_dir  # noqa: E402


def run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=TEST_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")
    return proc.stdout


def init_repo(path: Path) -> None:
    run(["git", "init"], path)
    run(["git", "config", "user.email", "test@example.invalid"], path)
    run(["git", "config", "user.name", "Batch Test"], path)


class BatchDriveFrameworkTests(unittest.TestCase):
    def test_spinner_output_is_not_meaningful_worker_progress(self) -> None:
        spinner = (
            "almost done thinking with medium effort) * 1 * 2 * 3 * 4 * 5 * 6 "
            "esc to interrupt · ctrl+t · ↓ 14.3k tokens"
        )
        self.assertFalse(is_meaningful_worker_output(spinner))
        self.assertFalse(is_meaningful_worker_output("✶ * 1 * 2 * 3 * 4 * 5"))

    def test_stage_and_tool_output_are_meaningful_worker_progress(self) -> None:
        self.assertTrue(is_meaningful_worker_output("优化轮次循环 SubTask#1:foo 代码优化 In Progress"))
        self.assertTrue(is_meaningful_worker_output("cmake --build build && ctest --test-dir build passed"))

    def test_auto_reply_echo_is_not_meaningful_worker_progress(self) -> None:
        self.assertFalse(is_meaningful_worker_output("继续\n"))
        self.assertFalse(
            is_meaningful_worker_output(
                "继续；如果流水线已经完成，请写入 optimization_reports/run_*/FINAL_SUMMARY.md，"
                "同时输出 SESSION_COMPLETE / BATCH_END / SESSION_END 后退出。\n"
            )
        )

    def test_claude_context_ui_limit_is_detected(self) -> None:
        self.assertTrue(context_ui_limit_reached("⏵⏵ bypass permissions on 100%contextused"))
        self.assertTrue(context_ui_limit_reached("status: 97% context used"))
        self.assertTrue(context_ui_limit_reached("status: 2% context left"))
        self.assertFalse(context_ui_limit_reached("status: 80% context used"))

    def test_default_manifest_and_output_are_resolved_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "batch_optimize_manifest.yaml"
            manifest.write_text(
                "targets:\n"
                "  - id: faiss\n"
                "    project_path: /tmp/faiss\n",
                encoding="utf-8",
            )

            args = parse_args(["--default-manifest", str(manifest)])
            validate_args(args)
            manifest_path = resolve_manifest_path(args.manifest, args.default_manifest)
            out = resolve_default_out(manifest_path, self_test=False, now=datetime(2026, 6, 1, 1, 2, 3))

            self.assertEqual(manifest_path, manifest.resolve())
            self.assertEqual(out, manifest.resolve().parent / "_batch_optimize_results_20260601_010203")

    def test_filter_targets_rejects_unknown_ids(self) -> None:
        targets = [
            {"id": "faiss", "project_path": "/tmp/faiss"},
            {"id": "isa_l", "project_path": "/tmp/isa_l"},
            {"id": "x264", "project_path": "/tmp/x264"},
        ]

        selected = filter_manifest_targets(targets, "faiss,x264")
        self.assertEqual([item["id"] for item in selected], ["faiss", "x264"])

        with self.assertRaises(SystemExit) as raised:
            filter_manifest_targets(targets, "faiss,missing")
        self.assertIn("unknown target id(s): missing", str(raised.exception))
        self.assertIn("available targets: faiss, isa_l, x264", str(raised.exception))

    def test_data_loader_parses_simple_yaml_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "batch.yaml"
            manifest.write_text(
                "run:\n"
                "  timeout_minutes: 7\n"
                "targets:\n"
                "  - id: demo\n"
                "    project_path: /tmp/demo\n",
                encoding="utf-8",
            )

            loaded = load_manifest(manifest)

            self.assertEqual(loaded["run"]["timeout_minutes"], 7)
            self.assertEqual(loaded["targets"][0]["id"], "demo")

    def test_pipeline_core_merges_cli_run_config(self) -> None:
        args = parse_args(
            [
                "--self-test",
                "--timeout-minutes",
                "9",
                "--idle-timeout-minutes",
                "2",
                "--context-soft-limit-tokens",
                "1000",
            ]
        )

        run_cfg = merge_cli_run_config({"run": {"timeout_minutes": 1, "max_parallel": 1}}, args)

        self.assertEqual(run_cfg["timeout_minutes"], 9)
        self.assertEqual(run_cfg["idle_timeout_minutes"], 2)
        self.assertEqual(run_cfg["context_soft_limit_tokens"], 1000)
        self.assertEqual(run_cfg["max_parallel"], 1)

    def test_config_loader_builds_run_context_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            out = root / "out"
            manifest.write_text(
                "run:\n"
                "  timeout_minutes: 3\n"
                "targets:\n"
                "  - id: demo\n"
                "    project_path: /tmp/demo\n"
                "  - id: skip_me\n"
                "    project_path: /tmp/skip\n",
                encoding="utf-8",
            )
            args = parse_args(
                [
                    "--manifest",
                    str(manifest),
                    "--targets",
                    "demo",
                    "--out",
                    str(out),
                    "--claude-bin",
                    sys.executable,
                    "--timeout-minutes",
                    "11",
                ]
            )

            context = load_batch_context(
                args,
                make_self_test_manifest=lambda _: self.fail("manifest batch must not create self-test fixture"),
            )

            self.assertEqual(context.manifest_path, manifest.resolve())
            self.assertEqual(context.out, out.resolve())
            self.assertEqual(context.claude_bin, str(Path(sys.executable).resolve()))
            self.assertEqual(context.run_cfg["timeout_minutes"], 11)
            self.assertEqual([target["id"] for target in context.targets], ["demo"])
            self.assertTrue(out.exists())

    def test_batch_pipeline_wires_scheduler_summary_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            context = BatchRunContext(
                manifest={"targets": [{"id": "demo", "project_path": "/tmp/demo"}]},
                manifest_path=None,
                out=out,
                claude_bin=sys.executable,
                run_cfg={"max_parallel": 1},
                targets=[{"id": "demo", "project_path": "/tmp/demo"}],
            )
            summary_calls: list[list[dict[str, object]]] = []
            progress_calls: list[str | None] = []

            def fake_run_target(
                target: dict[str, object],
                target_out: Path,
                run_cfg: dict[str, object],
                claude_bin: str,
                legacy_driver: bool = False,
                progress_callback=None,
            ) -> dict[str, object]:
                self.assertEqual(target_out, out)
                self.assertEqual(run_cfg["max_parallel"], 1)
                self.assertEqual(claude_bin, sys.executable)
                self.assertFalse(legacy_driver)
                if progress_callback:
                    progress_callback()
                return {"target_id": str(target["id"]), "status": "applied_verified"}

            def fake_summary(summary_out: Path, results: list[dict[str, object]]) -> None:
                self.assertEqual(summary_out, out)
                summary_calls.append(results)

            def fake_progress(progress_out: Path, targets: list[dict[str, object]], active_id: str | None) -> None:
                self.assertEqual(progress_out, out)
                self.assertEqual(len(targets), 1)
                progress_calls.append(active_id)

            exit_code = BatchPipeline(
                context=context,
                legacy_driver=False,
                managed_driver_version="managed-test",
                run_target=fake_run_target,
                write_summary=fake_summary,
                progress_renderer=fake_progress,
                now=lambda: "2026-06-16T00:00:00+00:00",
            ).run()

            self.assertEqual(exit_code, 0)
            self.assertEqual(summary_calls[0][0]["status"], "applied_verified")
            self.assertEqual(progress_calls, ["demo", None])

    def test_self_test_conflicts_with_manifest_batch_options(self) -> None:
        args = parse_args(["--self-test", "--manifest", "targets.yaml"])
        with self.assertRaises(SystemExit) as raised:
            validate_args(args)
        self.assertIn("--self-test cannot be combined", str(raised.exception))

    def test_monitor_requires_output_and_skips_manifest_options(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            validate_args(parse_args(["--monitor"]))
        self.assertIn("--monitor requires --out", str(raised.exception))

        args = parse_args(["--monitor", "--out", "/tmp/batch-out"])
        validate_args(args)

        with self.assertRaises(SystemExit) as combined:
            validate_args(parse_args(["--monitor", "--out", "/tmp/batch-out", "--manifest", "targets.yaml"]))
        self.assertIn("--monitor cannot be combined", str(combined.exception))

    def test_list_targets_does_not_create_output_or_require_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "targets:\n"
                "  - id: faiss\n"
                "    project_path: /tmp/faiss\n"
                "  - id: isa_l\n"
                "    project_path: /tmp/isa_l\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "batch_drive_optimize.py"),
                    "--default-manifest",
                    str(manifest),
                    "--list-targets",
                ],
                cwd=str(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=TEST_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("faiss\t/tmp/faiss", proc.stdout)
            self.assertIn("isa_l\t/tmp/isa_l", proc.stdout)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["manifest.yaml"])

    def test_monitor_command_reads_output_without_manifest_or_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "_batch_optimize_results_20260611_010203"
            target_out = out / "targets" / "kernel"
            log_dir = target_out / "claude_log"
            log_dir.mkdir(parents=True)
            (target_out / "progress.json").write_text(
                '{"status":"running","phase":"hotspot_analysis","elapsed_seconds":30,"idle_seconds":1}\n',
                encoding="utf-8",
            )
            (target_out / "target_state.json").write_text('{"run_status":"running"}\n', encoding="utf-8")
            (log_dir / "transcript_clean.md").write_text("SECRET_TRANSCRIPT_BODY\n" * 1000, encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "batch_drive_optimize.py"),
                    "--monitor",
                    "--out",
                    str(out),
                ],
                cwd=str(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=TEST_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("Bounded batch monitor snapshot", proc.stdout)
            self.assertIn("kernel", proc.stdout)
            self.assertIn("热点分析", proc.stdout)
            self.assertNotIn("SECRET_TRANSCRIPT_BODY", proc.stdout)

    def test_missing_claude_fails_fast(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            resolve_claude_bin("/definitely/missing/claude")
        self.assertIn("claude binary not found", str(raised.exception))

    def test_skill_contract_documents_non_interactive_slash_default(self) -> None:
        text = (SCRIPT_DIR.parent / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("/batch-drive-optimize-pipeline` is a deterministic launcher", text)
        self.assertIn("batch_optimize_manifest.yaml", text)
        self.assertNotIn("/data/", text)
        self.assertIn("--interactive", text)
        self.assertIn("--self-test", text)
        self.assertIn("--non-interactive", text)
        self.assertIn(".claude/skills/batch-drive-optimize-pipeline/scripts/batch_drive_optimize.py", text)
        self.assertNotIn("python3 scripts/batch_drive_optimize.py", text)

    def test_install_answer_bank_force_adds_ignored_claude_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            (repo / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
            (repo / "src.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)

            install_answer_bank_in_workdir(repo, "Answer bank:\n- recommended_mode: testcase\n")

            tracked = run(["git", "ls-files"], repo)
            self.assertIn("CLAUDE.md", tracked)
            self.assertIn(".batch_optimize_answer_bank.md", tracked)
            self.assertIn(".claude/settings.local.json", tracked)
            settings = (repo / ".claude" / "settings.local.json").read_text(encoding="utf-8")
            self.assertIn('"defaultMode": "bypassPermissions"', settings)
            self.assertIn('"Bash(sha256sum:*)"', settings)
            self.assertNotIn("Bash sha256sum:*)", settings)

    def test_install_answer_bank_normalizes_existing_bad_permission_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            settings_dir = repo / ".claude"
            settings_dir.mkdir()
            (settings_dir / "settings.local.json").write_text(
                '{"permissions":{"allow":["Bash sha256sum:*)","Bash realpath:*)"]}}\n',
                encoding="utf-8",
            )
            (repo / "src.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)

            install_answer_bank_in_workdir(repo, "Answer bank:\n- recommended_mode: testcase\n")

            settings = (settings_dir / "settings.local.json").read_text(encoding="utf-8")
            self.assertIn('"Bash(sha256sum:*)"', settings)
            self.assertIn('"Bash(realpath:*)"', settings)
            self.assertNotIn("Bash sha256sum:*)", settings)
            self.assertNotIn("Bash realpath:*)", settings)

    def test_workdir_skills_are_installed_from_manifest_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_root = root / "projects"
            source = manifest_root / "rapidjson"
            workdir = root / "out" / "workdirs" / "rapidjson"
            shared_skill = manifest_root / ".claude" / "skills" / "kpbot-code-optimizer" / "SKILL.md"
            source.mkdir(parents=True)
            shared_skill.parent.mkdir(parents=True)
            shared_skill.write_text("---\nname: kpbot-code-optimizer\n---\n", encoding="utf-8")
            (source / "reader.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

            copy_project(source, workdir)
            installed_from = ensure_workdir_pipeline_skills(workdir, source)
            install_answer_bank_in_workdir(workdir, "Answer bank:\n- recommended_mode: testcase\n")

            self.assertEqual(installed_from, (manifest_root / ".claude" / "skills").resolve())
            self.assertTrue((workdir / ".claude" / "skills" / "kpbot-code-optimizer" / "SKILL.md").exists())
            self.assertNotIn(".claude/skills", run(["git", "status", "--short"], workdir))

    def test_workdir_skills_replace_stale_project_copy_with_manifest_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_root = root / "projects"
            source = manifest_root / "isa-l_crypto"
            workdir = root / "out" / "workdirs" / "isa_l_crypto"
            shared_skill = manifest_root / ".claude" / "skills" / "kpbot-code-optimizer" / "SKILL.md"
            stale_skill = source / ".claude" / "skills" / "kpbot-code-optimizer" / "SKILL.md"
            shared_skill.parent.mkdir(parents=True)
            stale_skill.parent.mkdir(parents=True)
            shared_skill.write_text("root-synced\n", encoding="utf-8")
            stale_skill.write_text("stale-project-copy\n", encoding="utf-8")
            (source / "sha.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

            copy_project(source, workdir)
            ensure_workdir_pipeline_skills(workdir, source)

            installed = (workdir / ".claude" / "skills" / "kpbot-code-optimizer" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(installed, "root-synced\n")

    def test_workdir_skills_install_when_destination_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_root = root / "projects"
            source = manifest_root / "zlib"
            workdir = root / "out" / "workdirs" / "zlib"
            shared_skill = manifest_root / ".claude" / "skills" / "kpbot-code-optimizer" / "SKILL.md"
            source.mkdir(parents=True)
            shared_skill.parent.mkdir(parents=True)
            shared_skill.write_text("---\nname: kpbot-code-optimizer\n---\n", encoding="utf-8")
            workdir.mkdir(parents=True)

            installed_from = ensure_workdir_pipeline_skills(workdir, source)

            self.assertEqual(installed_from, (manifest_root / ".claude" / "skills").resolve())
            self.assertTrue((workdir / ".claude" / "skills" / "kpbot-code-optimizer" / "SKILL.md").exists())

    def test_run_probe_executes_argv_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            code, output = batch_drive_module.run_probe(
                [sys.executable, "-c", "import sys; print(sys.argv[1])", "safe; echo injected"],
                root,
            )

            self.assertEqual(code, 0)
            self.assertEqual(output.strip(), "safe; echo injected")
            self.assertNotIn("injected\ninjected", output)

    def test_run_streamed_times_out_hung_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, output = batch_drive_module.run_streamed(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                cwd=Path(tmp),
                progress_interval_seconds=10,
                timeout_seconds=1,
            )

            self.assertEqual(code, 124)
            self.assertIn("timed out", output)

    def test_run_streamed_times_out_partial_line_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            code, output = batch_drive_module.run_streamed(
                [
                    sys.executable,
                    "-c",
                    "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(5)",
                ],
                cwd=Path(tmp),
                progress_interval_seconds=10,
                timeout_seconds=1,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(code, 124)
            self.assertIn("partial", output)
            self.assertIn("timed out", output)
            self.assertLess(elapsed, 2.5)

    def test_list_project_files_prunes_skipped_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / ".git").mkdir()
            (root / "node_modules").mkdir()
            (root / "src" / "__pycache__").mkdir()
            (root / "src" / "kernel.c").write_text("int kernel(void) { return 0; }\n", encoding="utf-8")
            (root / "src" / "skip.tmp").write_text("tmp\n", encoding="utf-8")
            (root / ".git" / "config").write_text("ignored\n", encoding="utf-8")
            (root / "node_modules" / "dep.c").write_text("ignored\n", encoding="utf-8")
            (root / "src" / "__pycache__" / "cache.pyc").write_text("ignored\n", encoding="utf-8")

            files = {path.relative_to(root).as_posix() for path in batch_drive_module.list_project_files(root)}

            self.assertEqual(files, {"src/kernel.c"})

    def test_read_limited_reads_only_requested_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.txt"
            path.write_text("abcdef" * 10000, encoding="utf-8")

            self.assertEqual(batch_drive_module.read_limited(path, max_bytes=7), "abcdefa")
            self.assertEqual(batch_drive_module.read_limited(path, max_bytes=0), "")
            self.assertEqual(batch_drive_module.read_limited(path, max_bytes=-1), "")

    def test_retry_call_retries_transient_failures(self) -> None:
        attempts = {"count": 0}

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TimeoutError("transient")
            return "ok"

        result = retry_call(
            flaky,
            retry_exceptions=(TimeoutError,),
            config=RetryConfig(attempts=2, initial_delay_seconds=0, max_delay_seconds=0),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 2)

    def test_child_env_filters_sensitive_values(self) -> None:
        old_env = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(
                {
                    "PATH": "/usr/bin",
                    "HOME": "/tmp/home",
                    "SSH_AUTH_SOCK": "/tmp/agent.sock",
                    "API_KEY": "secret",
                    "MY_TOKEN": "secret",
                    "CUSTOM_FLAG": "drop-me",
                }
            )
            env = child_env("claude")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual(env["CLAUDE_BIN"], "claude")
        self.assertIn("PATH", env)
        self.assertIn("HOME", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("API_KEY", env)
        self.assertNotIn("MY_TOKEN", env)
        self.assertNotIn("CUSTOM_FLAG", env)

    def test_auto_driver_source_has_no_hardcoded_secret_assignments(self) -> None:
        source_paths = [
            SCRIPT_DIR / "auto_claude_logged.py",
            SCRIPT_DIR / "config.py",
            SCRIPT_DIR / "safe_env.py",
        ]
        assignment_re = re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b\s*=\s*['\"][^'\"]+['\"]"
        )
        for path in source_paths:
            self.assertIsNone(assignment_re.search(path.read_text(encoding="utf-8")))

    def test_auto_driver_responsibilities_are_split_into_modules(self) -> None:
        expected = [
            "config.py",
            "log_handler.py",
            "session_manager.py",
            "safe_env.py",
        ]

        for name in expected:
            self.assertTrue((SCRIPT_DIR / name).exists(), name)

    def test_batch_driver_responsibilities_are_split_into_modules(self) -> None:
        expected = [
            "data_loader.py",
            "task_scheduler.py",
            "pipeline_core.py",
            "result_collector.py",
            "batch_runtime.py",
            "stage_executor.py",
        ]

        for name in expected:
            self.assertTrue((SCRIPT_DIR / name).exists(), name)

    def test_stage_executor_runs_resume_attempts_with_injected_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "workdir"
            target_out = root / "target"
            workdir.mkdir()
            target_out.mkdir()
            answer_bank = target_out / "answer_bank.txt"
            companion_bank = workdir / ".batch_optimize_answer_bank.md"
            answer_bank.write_text("initial\n", encoding="utf-8")
            companion_bank.write_text("initial\n", encoding="utf-8")
            attempts_path = target_out / "attempts.jsonl"
            worker_attempts: list[int] = []
            states: list[dict[str, object]] = []

            def invoke_worker(**kwargs: object) -> dict[str, object]:
                attempt_index = int(kwargs["attempt_index"])
                worker_attempts.append(attempt_index)
                if attempt_index == 0:
                    return {"status": "failed", "final_marker_seen": False, "error": "timeout"}
                return {"status": "completed", "final_marker_seen": True}

            def attempt_record(
                attempt: int,
                started_at: str,
                duration_seconds: int,
                runner_result: dict[str, object],
            ) -> dict[str, object]:
                return {
                    "attempt": attempt,
                    "started_at": started_at,
                    "duration_seconds": duration_seconds,
                    "status": runner_result.get("status"),
                }

            def build_resume_prompt(**kwargs: object) -> str:
                return f"\nRESUME target={kwargs['target_id']} status={kwargs['runner_result']['status']}\n"

            def write_target_state(path: Path, data: dict[str, object]) -> None:
                states.append(dict(data))
                path.mkdir(parents=True, exist_ok=True)
                (path / "target_state.json").write_text(json.dumps(data) + "\n", encoding="utf-8")

            def append_jsonl(path: Path, data: dict[str, object]) -> None:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(data) + "\n")

            executor = StageExecutor(
                invoke_worker=invoke_worker,
                attempt_record=attempt_record,
                build_resume_prompt=build_resume_prompt,
                write_target_state=write_target_state,
                append_jsonl=append_jsonl,
                utc_now=lambda: "2026-06-16T00:00:00+00:00",
            )
            result, attempts = executor.run(
                StageExecutorConfig(
                    target_id="kernel",
                    workdir=workdir,
                    target_out=target_out,
                    answer_bank_file=answer_bank,
                    claude_bin="claude",
                    timeout_minutes=1,
                    idle_timeout_minutes=1,
                    progress_interval_seconds=0,
                    resume_attempts=1,
                    context_soft_limit_tokens=0,
                    context_hard_limit_tokens=0,
                    resume_prompt_max_tokens=100,
                    transcript_tail_lines=20,
                    legacy_driver=False,
                ),
                attempts_path,
            )

            self.assertEqual(worker_attempts, [0, 1])
            self.assertEqual(result["status"], "completed")
            self.assertEqual([attempt["status"] for attempt in attempts], ["failed", "completed"])
            self.assertIn("RESUME target=kernel status=failed", answer_bank.read_text(encoding="utf-8"))
            self.assertIn("RESUME target=kernel status=failed", companion_bank.read_text(encoding="utf-8"))
            self.assertTrue(any(state.get("run_status") == "resuming" for state in states))

    def test_task_scheduler_records_driver_failure_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            targets = [
                {"id": "bad_target", "project_path": "/tmp/bad"},
                {"id": "good_target", "project_path": "/tmp/good"},
            ]
            progress_calls: list[str | None] = []

            def render_progress(render_out: Path, render_targets: list[dict[str, object]], active_id: str | None) -> None:
                self.assertEqual(render_out, out)
                self.assertEqual(len(render_targets), 2)
                progress_calls.append(active_id)

            def run_one(target: dict[str, object], progress_callback) -> dict[str, object]:
                progress_callback()
                if target["id"] == "bad_target":
                    raise RuntimeError("driver boom")
                return {
                    "target_id": str(target["id"]),
                    "status": "applied_verified",
                    "completion_status": "completed",
                }

            scheduler = TaskScheduler(
                targets=targets,
                out=out,
                run_cfg={"max_parallel": 1},
                legacy_driver=False,
                managed_driver_version="managed-test",
                progress_renderer=render_progress,
                now=lambda: "2026-06-16T00:00:00+00:00",
                failure_exceptions=(RuntimeError,),
            )

            results = scheduler.run_serial(run_one)

            self.assertEqual([result["target_id"] for result in results], ["bad_target", "good_target"])
            self.assertEqual(results[0]["status"], "driver_failed")
            self.assertEqual(results[1]["status"], "applied_verified")
            self.assertIn("bad_target", progress_calls)
            self.assertIn("good_target", progress_calls)
            self.assertIn(None, progress_calls)
            target_out = out / "targets" / "bad_target"
            for name in [
                "usage.json",
                "timing.json",
                "completion_gate.json",
                "target_result.json",
                "target_state.json",
                "target_report.md",
            ]:
                self.assertTrue((target_out / name).exists(), name)
            failed_result = json.loads((target_out / "target_result.json").read_text(encoding="utf-8"))
            self.assertEqual(failed_result["completion_gate"]["status"], "driver_failed")
            self.assertEqual(failed_result["blocker"], "driver boom")

    def test_special_case_skill_documents_pipeline_contract_compatibility(self) -> None:
        text = (SCRIPT_DIR.parent.parent / "special-case-optimization" / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("## Compatibility", text)
        self.assertIn("kpbot-code-optimizer/apply-optimization v1 contract", text)
        for field in [
            "special_case_result.success",
            "original_code",
            "optimized_code",
            "fallback_preserved",
            "rewrite_kind",
            "validation_focus",
            "error_message",
        ]:
            self.assertIn(field, text)

    def test_copy_if_exists_merges_directory_without_predelete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dst = root / "dst"
            src.mkdir()
            dst.mkdir()
            (src / "fresh.txt").write_text("fresh\n", encoding="utf-8")
            (dst / "fresh.txt").write_text("old\n", encoding="utf-8")
            (dst / "kept.txt").write_text("kept\n", encoding="utf-8")

            copy_if_exists(src, dst)

            self.assertEqual((dst / "fresh.txt").read_text(encoding="utf-8"), "fresh\n")
            self.assertEqual((dst / "kept.txt").read_text(encoding="utf-8"), "kept\n")

    def test_copy_if_exists_ignores_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            copy_if_exists(root / "missing.txt", root / "dst" / "missing.txt")

            self.assertFalse((root / "dst" / "missing.txt").exists())

    def test_prepare_output_dir_uses_exception_driven_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "project"
            out.mkdir()
            marker = out / "old.txt"
            marker.write_text("old\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                prepare_output_dir(out, force=False)
            self.assertTrue(marker.exists())

            prepared = prepare_output_dir(out, force=True)

            self.assertEqual(prepared, out.resolve())
            self.assertTrue(out.exists())
            self.assertFalse(marker.exists())

    def test_worker_command_disables_token_limits_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "workdir"
            target_out = root / "target"
            workdir.mkdir()
            captured: list[list[str]] = []
            original = batch_drive_module.run_streamed

            def fake_run_streamed(
                cmd: list[str],
                cwd: Path | None = None,
                progress_callback=None,
                progress_interval_seconds: float = 30.0,
                timeout_seconds: int | None = None,
            ) -> tuple[int, str]:
                captured.append(cmd)
                if progress_callback:
                    progress_callback()
                log_dir = Path(cmd[cmd.index("--log-dir") + 1])
                log_dir.mkdir(parents=True)
                (log_dir / "auto_result.json").write_text(
                    '{"status":"completed","final_marker_seen":true}\n',
                    encoding="utf-8",
                )
                return 0, ""

            batch_drive_module.run_streamed = fake_run_streamed
            try:
                invoke_auto_claude(
                    workdir=workdir,
                    target_out=target_out,
                    answer_bank_file=target_out / "answer_bank.txt",
                    claude_bin="claude",
                    timeout_minutes=1,
                    idle_timeout_minutes=1,
                    progress_interval_seconds=30,
                )
            finally:
                batch_drive_module.run_streamed = original

            self.assertTrue(captured)
            self.assertNotIn("--allow-transcript-final-marker", captured[0])
            self.assertNotIn("--context-soft-limit-tokens", captured[0])
            self.assertNotIn("--context-hard-limit-tokens", captured[0])
            self.assertIn("--transcript-tail-lines", captured[0])

    def test_worker_command_passes_explicit_token_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "workdir"
            target_out = root / "target"
            workdir.mkdir()
            captured: list[list[str]] = []
            original = batch_drive_module.run_streamed

            def fake_run_streamed(
                cmd: list[str],
                cwd: Path | None = None,
                progress_callback=None,
                progress_interval_seconds: float = 30.0,
                timeout_seconds: int | None = None,
            ) -> tuple[int, str]:
                captured.append(cmd)
                log_dir = Path(cmd[cmd.index("--log-dir") + 1])
                log_dir.mkdir(parents=True)
                (log_dir / "auto_result.json").write_text(
                    '{"status":"completed","final_marker_seen":true}\n',
                    encoding="utf-8",
                )
                return 0, ""

            batch_drive_module.run_streamed = fake_run_streamed
            try:
                invoke_auto_claude(
                    workdir=workdir,
                    target_out=target_out,
                    answer_bank_file=target_out / "answer_bank.txt",
                    claude_bin="claude",
                    timeout_minutes=1,
                    idle_timeout_minutes=1,
                    progress_interval_seconds=30,
                    context_soft_limit_tokens=160000,
                    context_hard_limit_tokens=220000,
                )
            finally:
                batch_drive_module.run_streamed = original

            self.assertTrue(captured)
            self.assertIn("--context-soft-limit-tokens", captured[0])
            self.assertIn("--context-hard-limit-tokens", captured[0])

    def test_invoke_auto_claude_marks_stale_running_result_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "workdir"
            target_out = root / "target"
            workdir.mkdir()
            original = batch_drive_module.run_streamed

            def fake_run_streamed(
                cmd: list[str],
                cwd: Path | None = None,
                progress_callback=None,
                progress_interval_seconds: float = 30.0,
                timeout_seconds: int | None = None,
            ) -> tuple[int, str]:
                log_dir = Path(cmd[cmd.index("--log-dir") + 1])
                log_dir.mkdir(parents=True)
                (log_dir / "auto_result.json").write_text(
                    '{"status":"running","error":null,"exit_code":null}\n',
                    encoding="utf-8",
                )
                return 143, "terminated"

            batch_drive_module.run_streamed = fake_run_streamed
            try:
                result = invoke_auto_claude(
                    workdir=workdir,
                    target_out=target_out,
                    answer_bank_file=target_out / "answer_bank.txt",
                    claude_bin="claude",
                    timeout_minutes=1,
                    idle_timeout_minutes=1,
                    progress_interval_seconds=30,
                )
            finally:
                batch_drive_module.run_streamed = original

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exit_code"], 143)
            self.assertIn("auto runner exited with code 143", result["error"])

    def test_resume_prompt_token_budget_zero_means_unbounded(self) -> None:
        text = "word " * 5000
        self.assertEqual(truncate_to_token_budget(text, 0), text)

    def test_render_batch_progress_includes_visual_overview_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            targets = [
                {"id": "isa_l_crypto", "project_path": "/tmp/isa-l_crypto"},
                {"id": "sleef", "project_path": "/tmp/sleef"},
            ]
            done_out = out / "targets" / "isa_l_crypto"
            running_out = out / "targets" / "sleef"
            done_out.mkdir(parents=True)
            running_out.mkdir(parents=True)
            (done_out / "target_result.json").write_text(
                '{"status":"applied_verified","transcript_token_stats":{"token_count":12345,"is_estimate":true},'
                '"patch_info":{"final_patch_bytes":387},"internal_commits":["abc fix"]}\n',
                encoding="utf-8",
            )
            (done_out / "transcript_clean.md").write_text("line1\nline2\n", encoding="utf-8")
            (running_out / "progress.json").write_text(
                '{"status":"running","phase":"project_preparation","elapsed_seconds":373,'
                '"idle_seconds":12,"reply_counts":{"accept_stage":2},'
                '"task_counts":{"total":16,"done":5,"in_progress":7,"pending":4},'
                '"stages":[{"stage":"GatherContext","status":"done"},{"stage":"PrepareProject","status":"running"}],'
                '"optimization_points":["opt1 vectorization - Pending"],'
                '"message":"building baseline"}\n',
                encoding="utf-8",
            )
            (running_out / "transcript_clean.md").write_text("准备项目环境\n中文 token sample\n", encoding="utf-8")

            overview = render_batch_progress(out, targets, active_target_id="sleef")

            self.assertIn("进度总览", overview)
            self.assertIn("Tokens", overview)
            self.assertIn("任务", overview)
            self.assertIn("耗时/空闲", overview)
            self.assertIn("自动回复", overview)
            self.assertNotIn("Transcript", overview)
            self.assertIn("isa_l_crypto", overview)
            self.assertIn("✅ 完成", overview)
            self.assertIn("sleef", overview)
            self.assertIn("⏳ 进行中", overview)
            self.assertIn("5/16 done", overview)
            self.assertIn("6m 13s / 12s", overview)
            self.assertIn("accept_stage=2", overview)
            self.assertIn("当前运行目标", overview)
            self.assertIn("opt1 vectorization - Pending", overview)
            self.assertIn("building baseline", overview)
            self.assertIn("12,345 估", overview)
            self.assertIn("Token 消耗估算", overview)
            self.assertIn("Patch 387 bytes", overview)

    def test_render_batch_progress_shows_completed_with_quality_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target_out = out / "targets" / "kernel"
            target_out.mkdir(parents=True)
            (target_out / "target_result.json").write_text(
                '{"status":"pipeline_incomplete","quality_status":"pipeline_incomplete",'
                '"completion_status":"completed","reached_final_summary":true}\n',
                encoding="utf-8",
            )

            overview = render_batch_progress(out, [{"id": "kernel", "project_path": "/tmp/kernel"}])

            self.assertIn("⚠ 完成待审", overview)
            self.assertIn("全流程完成（质量门控未通过）", overview)

    def test_status_snapshot_avoids_transcript_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target_out = out / "targets" / "running"
            log_dir = target_out / "claude_log"
            log_dir.mkdir(parents=True)
            (target_out / "progress.json").write_text(
                '{"status":"running","phase":"hotspot_analysis","updated_at":"now","elapsed_seconds":42,'
                '"idle_seconds":0,"reply_counts":{"recap_continue":1},'
                '"task_counts":{"total":3,"done":1,"in_progress":1,"pending":1},'
                '"stages":[{"stage":"GatherContext","status":"done"},{"stage":"AnalyzeHotspot","status":"running"}],'
                '"optimization_points":["opt1 prefetch - Pending"],'
                '"message":"SECRET_SUMMARY_MARKER"}\n',
                encoding="utf-8",
            )
            (log_dir / "auto_result.json").write_text('{"status":"running","error":null}\n', encoding="utf-8")
            (log_dir / "transcript_clean.md").write_text("SECRET_TRANSCRIPT_BODY\n" * 1000, encoding="utf-8")

            snapshot = write_status_snapshot(out, [{"id": "running", "project_path": "/tmp/running"}], "running")
            serialized = (out / "status_snapshot.json").read_text(encoding="utf-8")

            self.assertEqual(snapshot["targets"][0]["progress"]["phase"], "hotspot_analysis")
            self.assertIn("transcript_clean.md", snapshot["targets"][0]["artifacts"])
            self.assertIn("human_summary", snapshot)
            self.assertIn("热点分析", snapshot["targets"][0]["human_summary"]["line"])
            self.assertIn("1/3 done", snapshot["targets"][0]["human_summary"]["tasks"])
            self.assertIn("recap_continue=1", snapshot["targets"][0]["human_summary"]["auto_replies"])
            self.assertIn("opt1 prefetch - Pending", snapshot["targets"][0]["human_summary"]["optimization_points"])
            self.assertIn("SECRET_SUMMARY_MARKER", serialized)
            self.assertNotIn("SECRET_TRANSCRIPT_BODY", serialized)
            self.assertLess(len(serialized), 10000)

    def test_monitor_status_snapshot_omits_transcript_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target_out = out / "targets" / "running"
            log_dir = target_out / "claude_log"
            log_dir.mkdir(parents=True)
            (target_out / "progress.json").write_text(
                '{"status":"running","phase":"optimization_point","updated_at":"now",'
                '"elapsed_seconds":75,"idle_seconds":3,"message":"bounded status only"}\n',
                encoding="utf-8",
            )
            (target_out / "target_state.json").write_text('{"run_status":"running"}\n', encoding="utf-8")
            (log_dir / "auto_result.json").write_text('{"status":"running"}\n', encoding="utf-8")
            (log_dir / "transcript_clean.md").write_text("SECRET_TRANSCRIPT_BODY\n" * 2000, encoding="utf-8")

            snapshot = monitor_status_snapshot(out)
            rendered = render_monitor_snapshot(snapshot)
            serialized = json.dumps(snapshot, ensure_ascii=False)

            self.assertEqual(snapshot["active_target_id"], "running")
            self.assertIn("optimization_point", serialized)
            self.assertIn("transcript_clean.md", serialized)
            self.assertIn("Bounded batch monitor snapshot", rendered)
            self.assertIn("Transcript bodies intentionally omitted", rendered)
            self.assertNotIn("SECRET_TRANSCRIPT_BODY", serialized)
            self.assertNotIn("SECRET_TRANSCRIPT_BODY", rendered)

    def test_monitor_does_not_show_failed_progress_as_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target_out = out / "targets" / "failed_target"
            target_out.mkdir(parents=True)
            (out / "status_snapshot.json").write_text('{"active_target_id":"failed_target"}\n', encoding="utf-8")
            (target_out / "progress.json").write_text(
                '{"status":"failed","phase":"verification","elapsed_seconds":153,"idle_seconds":0,'
                '"message":"worker Claude ended without final-result marker"}\n',
                encoding="utf-8",
            )
            (target_out / "target_state.json").write_text('{"run_status":"running"}\n', encoding="utf-8")

            snapshot = monitor_status_snapshot(out)
            target = snapshot["targets"][0]

            self.assertIsNone(snapshot["active_target_id"])
            self.assertFalse(target["active"])
            self.assertEqual(target["status_label"], "❌ 失败")
            self.assertEqual(target["quality_status"], "failed")
            self.assertEqual(target["completion_status"], "failed")

    def test_render_batch_progress_does_not_read_transcript_body_for_live_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target_out = out / "targets" / "running"
            log_dir = target_out / "claude_log"
            log_dir.mkdir(parents=True)
            (target_out / "progress.json").write_text(
                '{"status":"running","phase":"hotspot_analysis","elapsed_seconds":9,"idle_seconds":0}\n',
                encoding="utf-8",
            )
            (log_dir / "transcript_clean.md").write_text("SECRET_TRANSCRIPT_BODY\n" * 1000, encoding="utf-8")
            original = batch_drive_module.count_lines_and_tokens

            def fail_if_full_transcript_is_read(path: Path) -> tuple[int | None, int | None]:
                raise AssertionError(f"unexpected full transcript read: {path}")

            batch_drive_module.count_lines_and_tokens = fail_if_full_transcript_is_read
            try:
                overview = render_batch_progress(out, [{"id": "running", "project_path": "/tmp/running"}], "running")
            finally:
                batch_drive_module.count_lines_and_tokens = original

            self.assertIn("热点分析", overview)
            self.assertIn("Tokens", overview)
            self.assertNotIn("SECRET_TRANSCRIPT_BODY", overview)

    def test_sparse_discovery_defaults_to_testcase_even_with_static_hotspot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "bench").mkdir()
            (repo / "src" / "kernels.c").write_text(
                "void sgemm_kernel(float *a, float *b, float *c, int n) {\n"
                "  for (int i = 0; i < n; ++i) {\n"
                "    for (int j = 0; j < n; ++j) {\n"
                "      c[i] += a[i] * b[j];\n"
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (repo / "bench" / "bench_kernel.c").write_text(
                "extern void sgemm_kernel(float*, float*, float*, int);\n"
                "int main(void) { return 0; }\n",
                encoding="utf-8",
            )
            (repo / "Makefile").write_text(
                "test:\n\ttrue\n"
                "bench:\n\ttrue\n",
                encoding="utf-8",
            )

            discovery = discover_project(repo, {})
            answer_bank = build_answer_bank({}, repo, discovery)

            self.assertEqual(discovery["recommended_mode"], "testcase")
            self.assertEqual(discovery["recommended_code_path"], "")
            self.assertEqual(discovery["recommended_function_name"], "")
            self.assertEqual(discovery["hotspot_candidates"][0]["function_name"], "sgemm_kernel")
            self.assertIn("选择：用例优化", answer_bank)
            self.assertNotIn("选择：函数优化", answer_bank)
            self.assertIn("must_confirm_with_runtime_profiling=true", answer_bank)

    def test_explicit_function_path_is_preserved_in_answer_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "kernels.c").write_text(
                "void vec_add(float *a, float *b, float *c, int n) {\n"
                "  for (int i = 0; i < n; ++i) c[i] = a[i] + b[i];\n"
                "}\n",
                encoding="utf-8",
            )
            target = {
                "mode": "function",
                "code_path": "src/kernels.c",
                "function_name": "vec_add",
                "test_method": "make test",
            }

            discovery = discover_project(repo, target)
            answer_bank = build_answer_bank(target, repo, discovery)

            self.assertEqual(discovery["recommended_mode"], "function")
            self.assertEqual(discovery["recommended_code_path"], "src/kernels.c")
            self.assertEqual(discovery["recommended_function_name"], "vec_add")
            self.assertIn("选择：函数优化", answer_bank)
            self.assertIn(f"源码路径：{(repo / 'src' / 'kernels.c').resolve()}", answer_bank)
            self.assertIn("函数名：vec_add", answer_bank)

    def test_collect_result_captures_working_tree_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "kernel.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            (repo / "kernel.c").write_text("int f(void) { return 2; }\n", encoding="utf-8")
            report_dir = repo / "optimization_reports" / "run_1"
            report_dir.mkdir(parents=True)
            (report_dir / "FINAL_SUMMARY.md").write_text("# Formal Summary\n\n功能测试: pass\n性能测试: +1%\n", encoding="utf-8")
            log_dir = out / "claude_log"
            log_dir.mkdir(parents=True)
            (log_dir / "events.jsonl").write_text(
                '{"event":"claude_output","text":"Pipeline Complete\\nTOKEN sample 中文\\n"}\n',
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="kernel",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )

            self.assertIn("return 2", (out / "final.patch").read_text(encoding="utf-8"))
            self.assertIn("return 2", (out / "working_tree.patch").read_text(encoding="utf-8"))
            self.assertGreater(result["patch_info"]["final_patch_bytes"], 0)
            self.assertEqual((out / "final_summary.md").read_text(encoding="utf-8").splitlines()[0], "# Formal Summary")
            self.assertGreater(result["transcript_token_stats"]["token_count"], 0)
            self.assertTrue((out / "transcript_tokens.json").exists())
            self.assertTrue((out / "completion_gate.json").exists())
            self.assertTrue(result["completion_gate"]["patch_collected"])

    def test_collect_result_separates_completion_from_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "kernel.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            reports = repo / "optimization_reports"
            reports.mkdir()
            (reports / "FINAL_SUMMARY.md").write_text("# Final\n\nPipeline Complete\n", encoding="utf-8")

            result = collect_target_result(
                target_id="kernel",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )

            self.assertEqual(result["completion_status"], "completed")
            self.assertEqual(result["quality_status"], result["status"])
            self.assertEqual(result["status"], "pipeline_incomplete")
            self.assertTrue(result["reached_final_summary"])
            self.assertFalse(result["completion_gate"]["required_stages_seen"])

    def test_collect_result_marks_patch_without_verification_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "kernel.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            (repo / "kernel.c").write_text("int f(void) { return 2; }\n", encoding="utf-8")
            reports = repo / "optimization_reports" / "run_1"
            reports.mkdir(parents=True)
            (reports / "FINAL_SUMMARY.md").write_text(
                "# Final\n\n"
                "GatherContext ParseIntent PrepareProject DecomposeTasks AnalyzeTestcase AnalyzeHotspot\n"
                "DecideOptimization ApplyOptimization AdversarialReview VerifyOptimization\n"
                "Pipeline Complete\n",
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="kernel",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )

            self.assertEqual(result["status"], "applied_unverified")
            self.assertFalse(result["completion_gate"]["functional_verified"])
            self.assertFalse(result["completion_gate"]["performance_measured"])

    def test_collect_result_marks_structured_verified_clean_patch_successful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "configure").write_text("CFLAGS=-O3\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            (repo / "configure").write_text("CFLAGS=-O3 -march=armv8-a+crc\n", encoding="utf-8")

            report_dir = repo / "optimization_reports" / "run_1"
            stages = report_dir / "stages"
            points = report_dir / "points"
            stages.mkdir(parents=True)
            points.mkdir()
            for stage in [
                "GatherContext",
                "ParseIntent",
                "PrepareProject",
                "DecomposeTasks",
                "AnalyzeTestcase",
                "AnalyzeHotspot",
                "DecideOptimization",
                "ApplyOptimization",
                "AdversarialReview",
                "VerifyOptimization",
            ]:
                (stages / f"{stage.lower()}.json").write_text('{"status":"ok"}\n', encoding="utf-8")
            (points / "round1_crc32_verify.json").write_text(
                '{"status":"verified","functional_test":{"passed":true},'
                '"performance":{"speedup":1.19,"improvement_percent":15.85}}\n',
                encoding="utf-8",
            )
            (report_dir / "batch_result.json").write_text(
                '{"status":"complete","pipeline_status":"completed","applied_count":1,'
                '"verified_count":1,"clean_patch_files":["configure"],'
                '"verification":{"functional_test_passed":true},'
                '"performance_summary":{"speedup":1.19}}\n',
                encoding="utf-8",
            )
            (repo / "perf.data").write_bytes(b"\0" + (b"x" * 1_000_001))

            result = collect_target_result(
                target_id="zlib_crc32",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )

            self.assertEqual(result["status"], "applied_verified")
            self.assertTrue(result["completion_gate"]["required_stages_seen"])
            self.assertTrue(result["completion_gate"]["functional_verified"])
            self.assertTrue(result["completion_gate"]["performance_measured"])
            self.assertTrue(result["completion_gate"]["patch_hygiene_passed"])
            self.assertFalse(result["patch_info"]["binary_detected"])
            self.assertTrue(result["patch_info"]["excluded_binary_detected"])
            self.assertTrue(result["patch_info"]["excluded_large_file_detected"])
            self.assertIn("configure", (out / "final.patch").read_text(encoding="utf-8"))
            self.assertNotIn("perf.data", (out / "final.patch").read_text(encoding="utf-8"))

    def test_collect_result_marks_no_optimization_with_patch_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "deflate.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            (repo / "deflate.c").write_text("int f(void) { return 2; }\n", encoding="utf-8")
            report_dir = repo / "optimization_reports" / "run_1"
            report_dir.mkdir(parents=True)
            (report_dir / "batch_result.json").write_text(
                '{"status":"complete_no_optimization","pipeline_status":"completed",'
                '"applied_count":0,"verified_count":0}\n',
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="zlib_deflate_fast",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )

            self.assertEqual(result["status"], "report_inconsistent")
            self.assertFalse(result["completion_gate"]["artifact_consistent"])

    def test_collect_result_marks_dirty_generated_patch_artifact_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "test").mkdir()
            (repo / "test" / "example.c").write_text("int test_example(void) { return 0; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            (repo / "test" / "example.c").unlink()
            (repo / "gun_test").write_bytes(b"\0binary")
            (repo / "binary_payload.c").write_bytes(b"\0binary")
            (repo / "test_payload.c").write_text("a" * (1_000_001), encoding="utf-8")
            report_dir = repo / "optimization_reports" / "run_1"
            report_dir.mkdir(parents=True)
            (report_dir / "batch_result.json").write_text(
                '{"status":"complete","pipeline_status":"completed","applied_count":1,'
                '"verified_count":1,"verification":{"functional_test_passed":true},'
                '"performance_summary":{"speedup":1.05}}\n',
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="zlib_inflate_fast",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )
            manifest = (out / "patch_manifest.json").read_text(encoding="utf-8")

            self.assertEqual(result["status"], "artifact_error")
            self.assertFalse(result["completion_gate"]["patch_hygiene_passed"])
            self.assertIn("deleted_test_source", manifest)
            self.assertIn("binary_file", manifest)
            self.assertIn("large_file", manifest)

    def test_collect_result_marks_baseline_dependency_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "kernel.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            report_dir = repo / "optimization_reports" / "run_1"
            report_dir.mkdir(parents=True)
            (report_dir / "baseline_blocked.json").write_text(
                '{"status":"baseline_blocked","blocked_stage":"PrepareProject",'
                '"error":"CMake Error: Could NOT find BLAS (missing: BLAS_LIBRARIES)"}\n',
                encoding="utf-8",
            )
            (report_dir / "batch_result.json").write_text(
                '{"status":"baseline_blocked","pipeline_status":"completed",'
                '"blocked_reason":"Could NOT find BLAS"}\n',
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="faiss_distance_aarch64",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )

            self.assertEqual(result["status"], "baseline_blocked")
            self.assertFalse(result["completion_gate"]["patch_collected"])
            self.assertIn("baseline build or setup failed", result["blocker"])

    def test_transcript_token_stats_counts_tokens_not_lines(self) -> None:
        stats = transcript_token_stats("hello world\n中文 token\n", "events.jsonl")

        self.assertGreaterEqual(stats["token_count"], 4)
        self.assertEqual(stats["source"], "events.jsonl")
        self.assertTrue(stats["is_estimate"])
        self.assertNotIn("line_count", stats)

    def test_batch_summary_aggregates_transcript_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target_out = out / "targets" / "kernel"
            target_out.mkdir(parents=True)
            results = [
                {
                    "target_id": "kernel",
                    "status": "analysis_only",
                    "run_status": "completed",
                    "pipeline_status": "completed",
                    "quality_status": "analysis_only",
                    "target_out": str(target_out),
                    "reached_final_summary": True,
                    "completion_gate": {
                        "pipeline_reached_final_report": True,
                        "required_stages_seen": True,
                        "patch_collected": False,
                        "functional_verified": False,
                        "performance_measured": False,
                        "artifact_consistent": True,
                        "patch_hygiene_passed": True,
                    },
                    "transcript_token_stats": {"token_count": 42, "method": "heuristic_word_cjk_punct_estimate"},
                    "usage": {"transcript_tokens_estimate": 42, "is_exact": False},
                    "timing": {"wall_time_seconds": 61, "attempt_count": 2},
                }
            ]
            (target_out / "final_summary.md").write_text("No optimization possible.\n", encoding="utf-8")

            write_summary(out, results)

            summary_json = (out / "summary.json").read_text(encoding="utf-8")
            summary_md = (out / "summary.md").read_text(encoding="utf-8")
            summary_data = json.loads(summary_json)
            self.assertIn('"transcript_tokens_total": 42', summary_json)
            self.assertIn('"wall_time_seconds_total": 61', summary_json)
            self.assertIn('"ability_evidence"', summary_json)
            self.assertNotIn("ability_" + "score", summary_data)
            self.assertTrue((out / "ability_evidence.json").exists())
            self.assertTrue((target_out / "ability_evidence.json").exists())
            self.assertIn("Transcript tokens total: `42`", summary_md)
            self.assertIn("Wall time total: `1m 1s`", summary_md)
            self.assertIn("Ability evidence", summary_md)
            self.assertNotIn("Ability " + "score", summary_md)
            self.assertIn("| `kernel` | `completed` | `completed` | `analysis_only` |", summary_md)
            self.assertIn("| `none` | `42` | `1m 1s` | `2` | No optimization possible", summary_md)

    def test_ability_evidence_strong_verified_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_out = Path(tmp)
            trace = target_out / "trace_grade.json"
            trace.write_text('{"status":"applied_verified","events":[]}\n', encoding="utf-8")
            result = {
                "target_id": "kernel",
                "status": "applied_verified",
                "quality_status": "applied_verified",
                "target_out": str(target_out),
                "trace_grade": str(trace),
                "reached_final_summary": True,
                "source_unchanged": True,
                "patch_info": {"final_patch_bytes": 512},
                "completion_gate": {
                    "pipeline_reached_final_report": True,
                    "required_stages_seen": True,
                    "patch_collected": True,
                    "functional_verified": True,
                    "performance_measured": True,
                    "artifact_consistent": True,
                    "patch_hygiene_passed": True,
                },
                "structured_evidence": {"performance_summary": {"speedup": 1.19}},
                "usage": {"context_window_peak_estimate": 12000},
                "timing": {"attempt_count": 1},
            }

            report = summarize_target_evidence(result)

            self.assertEqual(report["status"], "applied_verified")
            self.assertEqual(report["evidence"]["speedup"], 1.19)
            self.assertTrue(report["evidence"]["functional_verified"])
            self.assertEqual(report["risk_flags"], [])
            self.assertNotIn("score", report)
            self.assertNotIn("grade_label", report)

    def test_ability_evidence_number_value_ignores_composite_text(self) -> None:
        self.assertIsNone(number_value("10 tests, 3 failed"))
        self.assertEqual(number_value("1.05x"), 1.05)
        self.assertEqual(number_value("-15%"), -15.0)
        self.assertEqual(number_value("speedup=1.2e+1x"), 12.0)
        self.assertEqual(number_value(".5%"), 0.5)

    def test_collect_result_number_value_ignores_composite_text(self) -> None:
        self.assertIsNone(collect_number_value("10 tests, 3 failed"))
        self.assertEqual(collect_number_value("1.05x"), 1.05)
        self.assertEqual(collect_number_value("-15%"), -15.0)
        self.assertEqual(collect_number_value("speedup=1.2e+1x"), 12.0)
        self.assertEqual(collect_number_value(".5%"), 0.5)

    def test_ability_evidence_extracts_performance_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_out = Path(tmp)
            (target_out / "final_summary.md").write_text("性能下降: -1.5e1%\n", encoding="utf-8")

            self.assertAlmostEqual(performance_speedup({}, target_out), 0.85)

        with tempfile.TemporaryDirectory() as tmp:
            target_out = Path(tmp)
            (target_out / "final_summary.md").write_text("speedup: 0.85x\n", encoding="utf-8")

            self.assertAlmostEqual(performance_speedup({}, target_out), 0.85)

    def test_ability_evidence_flags_unverified_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_out = Path(tmp)
            trace = target_out / "trace_grade.json"
            trace.write_text('{"status":"applied_unverified","events":[]}\n', encoding="utf-8")
            result = {
                "target_id": "kernel",
                "status": "applied_unverified",
                "quality_status": "applied_unverified",
                "target_out": str(target_out),
                "trace_grade": str(trace),
                "reached_final_summary": True,
                "source_unchanged": True,
                "patch_info": {"final_patch_bytes": 512},
                "completion_gate": {
                    "pipeline_reached_final_report": True,
                    "required_stages_seen": True,
                    "patch_collected": True,
                    "functional_verified": False,
                    "performance_measured": False,
                    "artifact_consistent": True,
                    "patch_hygiene_passed": True,
                },
                "usage": {"context_window_peak_estimate": 12000},
                "timing": {"attempt_count": 1},
            }

            report = summarize_target_evidence(result)

            self.assertEqual(report["status"], "applied_unverified")
            self.assertNotIn("score", report)
            self.assertIn("applied_unverified", report["risk_flags"])
            self.assertIn("patch_without_functional_verification", report["risk_flags"])
            self.assertIn("patch_without_performance_measurement", report["risk_flags"])

    def test_auto_reply_uses_answer_bank_for_test_and_exec_prompts(self) -> None:
        answer_bank = (
            "Answer bank:\n"
            "- recommended_test_method: cd build && ctest --output-on-failure\n"
            "- recommended_benchmark_command: make bench\n"
        )

        test_answer = answer_for("Enter to select\n测试方法\n❯", answer_bank, {})
        exec_answer = answer_for("Enter to select\n执行方法\nctest\n❯", answer_bank, {})

        self.assertEqual(test_answer, ("test_method", "cd build && ctest --output-on-failure\n"))
        self.assertEqual(exec_answer, ("exec_method", "make bench\n"))

    def test_auto_reply_starts_pipeline_without_changing_effort(self) -> None:
        answer = answer_for("What would you like to do?\n❯ ", "", {})

        self.assertEqual(answer, ("start_pipeline", "/kpbot-code-optimizer\n"))

    def test_auto_reply_answers_chinese_target_type_prompt(self) -> None:
        answer_bank = (
            "Answer bank:\n"
            "- project_root: /tmp/blake3\n"
            "- selection_block:\n"
            "选择：用例优化\n"
            "项目路径：/tmp/blake3\n"
        )
        screen = (
            "Thought for 7s, read 1 file\n"
            "请选择优化目标类型：\n"
            "| 选项 | 说明 |\n"
            "│ 函数优化 │ 优化指定 C/C++ 函数的性能 │\n"
            "│ 用例优化 │ 以测试用例为驱动进行性能优化 │\n"
            "请回复您的选择：\n"
            "- 函数优化 或 function\n"
            "- 用例 testcase\n"
            "❯ "
        )

        answer = answer_for(screen, answer_bank, {"start_pipeline": 1})

        self.assertEqual(answer, ("target_type", "用例优化\n"))

    def test_auto_reply_handles_claude_settings_warning(self) -> None:
        screen = (
            'SettingsWarning: .claude/settings.local.json\n'
            'Invalid permission rule "Bash sha256sum:*)" was skipped: Mismatched parentheses\n'
            "❯ 1. Continue\n"
            "  2. Fix with Claude\n"
            "  3. Exit and fix manually\n"
            "Enter to confirm · Esc to cancel\n"
        )

        answer = answer_for(screen, "", {"start_pipeline": 1})

        self.assertEqual(answer, ("settings_warning_continue", "\n"))

    def test_auto_reply_ignores_stale_continue_prompt_while_worker_is_busy(self) -> None:
        stale_prompt = (
            'SettingsWarning: .claude/settings.local.json\n'
            'Invalid permission rule "Bash sha256sum:*)" was skipped: Mismatched parentheses\n'
            "❯ 1. Continue\n"
            "  2. Fix with Claude\n"
            "  3. Exit and fix manually\n"
            "Enter to confirm · Esc to cancel\n"
        )
        busy_output = (
            "\n".join(f"worker output line {idx}" for idx in range(220))
            + "\nThought for 18s, searched for 2 patterns, read 1 file\n"
            + "◼ └ 架构分析\n"
            + "Bash(pwd && ls -la)\n"
        )

        answer = answer_for(
            stale_prompt + busy_output,
            "",
            {"start_pipeline": 1, "settings_warning_continue": 1},
        )

        self.assertIsNone(answer)

    def test_auto_reply_handles_claude_recap_pause(self) -> None:
        screen = (
            "Churned for 1m 38s\n\n"
            "※ recap: 已完成 GatherContext 和 PrepareProject，下一步继续 DecomposeTasks。\n"
            "❯ "
        )

        answer = answer_for(screen, "", {"start_pipeline": 1})

        self.assertEqual(answer, ("recap_continue", "继续\n"))

    def test_recap_continue_is_not_counted_as_nudge_continue(self) -> None:
        screen = (
            "Churned for 2m 04s\n"
            "※ recap: 当前停在第 1 轮优化点循环。\n"
            "❯ "
        )

        answer = answer_for(screen, "", {"start_pipeline": 1, "nudge_continue": 20})

        self.assertEqual(answer, ("recap_continue", "继续\n"))
        self.assertEqual(max_repeats_for_key("recap_continue", 3), 10)
        self.assertEqual(max_repeats_for_key("nudge_continue", 3), 6)

    def test_auto_reply_ignores_old_recap_while_worker_is_busy(self) -> None:
        screen = (
            "Churned for 1m 38s\n"
            "※ recap: 继续执行热点分析。\n"
            "Thought for 18s, searched for 2 patterns, read 1 file\n"
            "Bash(pwd && ls -la)\n"
            "◼ └ 热点分析\n"
        )

        answer = answer_for(screen, "", {"start_pipeline": 1, "recap_continue": 1})

        self.assertIsNone(answer)

    def test_auto_runner_detects_json_completion_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            log_dir = root / "target" / "claude_log"
            project.mkdir()
            log_dir.mkdir(parents=True)
            (project / ".batch_optimize_result.json").write_text(
                '{"pipeline_status":"complete"}\n',
                encoding="utf-8",
            )

            self.assertTrue(completion_marker_exists(project, log_dir))

    def test_auto_runner_detects_batch_result_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            log_dir = root / "target" / "claude_log"
            report_dir = project / "optimization_reports" / "run_1"
            report_dir.mkdir(parents=True)
            log_dir.mkdir(parents=True)
            (report_dir / "batch_result.json").write_text(
                '{"status":"complete_no_optimization","final":{"patches_applied":0}}\n',
                encoding="utf-8",
            )

            self.assertTrue(completion_marker_exists(project, log_dir))

    def test_progress_summary_renders_stage_table_and_points(self) -> None:
        summary = render_progress_summary(
            project_path=Path("/tmp/blake3"),
            log_dir=Path("/tmp/out/targets/blake3/claude_log"),
            phase="optimization_point",
            started=0,
            last_output=0,
            reply_counts={"nudge_continue": 2},
            final_seen=False,
            buffer=(
                "✔ 收集优化目标信息\n"
                "✔ 准备项目环境\n"
                "◼ 优化轮次循环\n"
                "◼ 第 1 轮优化\n"
                "◼ SubTask #1: blake3_hash4_neon\n"
                "1. SVE vectorization (priority 1)\n"
                "Status: SKIPPED\n"
                "Skip Reason: Kunpeng architecture constraint: NEON and SVE share vector compute resources.\n"
                "2. Round function unroll (priority 2)\n"
                "16 tasks (5 done, 7 in progress, 4 pending)\n"
            ),
        )

        self.assertIn("blake3 Target Progress", summary)
        self.assertIn("收集优化目标信息", summary)
        self.assertIn("In Progress", summary)
        self.assertIn("SVE vectorization - SKIPPED", summary)
        self.assertIn("Round function unroll", summary)
        self.assertIn("16 tasks (5 done, 7 in progress, 4 pending)", summary)
        self.assertIn("Progress snapshot: phase=optimization_point", summary)
        self.assertIn("Progress snapshot: stages=", summary)
        self.assertIn("Progress snapshot: points=", summary)

    def test_collect_result_trusts_root_final_summary_despite_late_driver_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "kernel.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            (repo / "kernel.c").write_text("int f(void) { return 2; }\n", encoding="utf-8")
            reports = repo / "optimization_reports"
            reports.mkdir()
            (reports / "FINAL_SUMMARY.md").write_text(
                "# Final\n\n"
                "GatherContext ParseIntent PrepareProject DecomposeTasks AnalyzeTestcase AnalyzeHotspot\n"
                "DecideOptimization ApplyOptimization AdversarialReview VerifyOptimization\n"
                "功能测试通过，性能测试 +12%。\n",
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="kernel",
                workdir=repo,
                target_out=out,
                runner_result={
                    "status": "failed",
                    "error": "prompt/action repeated too many times: nudge_continue",
                    "final_marker_seen": False,
                },
            )
            trace = (out / "trace_grade.json").read_text(encoding="utf-8")

            self.assertEqual(result["status"], "applied_verified")
            self.assertIn("driver_late_stop", trace)
            self.assertNotIn("driver_failed", trace)
            self.assertEqual(Path(result["formal_final_report"]).resolve(), (reports / "FINAL_SUMMARY.md").resolve())

    def test_collect_result_trusts_optimization_summary_and_batch_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "kernel.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run(["git", "add", "-A"], repo)
            run(["git", "commit", "-m", "baseline"], repo)
            run(["git", "tag", "batch_baseline"], repo)
            report_dir = repo / "optimization_reports" / "run_1"
            report_dir.mkdir(parents=True)
            (report_dir / "optimization_summary.md").write_text(
                "# Summary\n\n"
                "GatherContext ParseIntent PrepareProject DecomposeTasks AnalyzeTestcase AnalyzeHotspot\n"
                "DecideOptimization ApplyOptimization AdversarialReview VerifyOptimization\n"
                "Pipeline Complete\n\n"
                "Final Verdict: No optimization possible.\n",
                encoding="utf-8",
            )
            (report_dir / "batch_result.json").write_text(
                '{"status":"complete_no_optimization","verification":{"functional_test_passed":true}}\n',
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="kernel",
                workdir=repo,
                target_out=out,
                runner_result={"status": "failed", "error": "late stop", "final_marker_seen": False},
            )

            self.assertEqual(result["status"], "complete_no_optimization")
            self.assertEqual(result["completion_gate"]["pipeline_reached_final_report"], True)
            self.assertTrue((out / "completion_gate.json").exists())
            self.assertEqual(
                Path(result["formal_final_report"]).resolve(),
                (report_dir / "optimization_summary.md").resolve(),
            )

    def test_quality_signals_ignore_build_and_helper_outputs_as_source_changes(self) -> None:
        quality = parse_quality_signals(
            "功能测试通过，性能测试 +1%",
            " M .batch_optimize_answer_bank.md\n?? optimization_reports/\n?? build/perf.data\n M perf_ec.data\n",
            {"final_patch_bytes": 0},
        )

        self.assertFalse(quality["source_modified"])
        self.assertTrue(quality["functional_test_pass"])

    def test_quality_signals_accept_performance_regression_evidence(self) -> None:
        quality = parse_quality_signals(
            "功能测试通过，性能下降: -15%",
            "",
            {"final_patch_bytes": 1},
        )

        self.assertTrue(quality["functional_test_pass"])
        self.assertTrue(quality["performance_measured"])

    def test_disappearing_tmp_files_do_not_break_static_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "kernel.c"
            tmp_file = repo / "build" / "Testing" / "Temporary" / "LastTest.log.tmp"
            tmp_file.parent.mkdir(parents=True)
            source.write_text("void hot(float *x) { for (int i = 0; i < 8; ++i) x[i] += 1; }\n", encoding="utf-8")
            tmp_file.write_text("transient\n", encoding="utf-8")
            files = [source, tmp_file]
            tmp_file.unlink()

            candidates = discover_hotspot_candidates(repo, files)

            self.assertEqual(candidates[0]["function_name"], "hot")

    def test_static_discovery_skips_pathological_long_intrinsic_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            include = repo / "include"
            src = repo / "src"
            include.mkdir()
            src.mkdir()
            long_args = ", ".join(f"uint8_t a{i}" for i in range(64))
            (include / "simd.h").write_text(
                f"inline __m512i _mm512_set_epi8({long_args}) {{ return _mm512_setzero_si512(); }}\n",
                encoding="utf-8",
            )
            (src / "kernel.c").write_text(
                "void hash_kernel(float *x, int n) {\n"
                "  for (int i = 0; i < n; ++i) x[i] += 1;\n"
                "}\n",
                encoding="utf-8",
            )

            files = [include / "simd.h", src / "kernel.c"]
            start = time.monotonic()
            candidates = discover_hotspot_candidates(repo, files)

            self.assertLess(time.monotonic() - start, 1.0)
            self.assertEqual(candidates[0]["function_name"], "hash_kernel")

    def test_trace_gate_rejects_transcript_only_preparation_evidence(self) -> None:
        stages = {
            stage: {"present": True, "source": "artifact", "artifacts": [f"{stage}.md"], "reports": []}
            for stage in [
                "GatherContext",
                "ParseIntent",
                "PrepareProject",
                "DecomposeTasks",
                "AnalyzeTestcase",
                "AnalyzeHotspot",
                "DecideOptimization",
                "ApplyOptimization",
                "AdversarialReview",
                "VerifyOptimization",
            ]
        }
        stages["PrepareProject"]["source"] = "transcript"
        grade = grade_trace(
            {"status": "completed"},
            True,
            stages,
            {
                "source_modified": False,
                "baseline_blocked": False,
                "functional_test_pass": False,
                "performance_measured": False,
                "no_effect_claim": False,
                "patch_nonempty": False,
            },
            {"final_patch_bytes": 0},
            [],
        )
        self.assertEqual(grade["status"], "pipeline_incomplete")
        self.assertIn("PrepareProject", grade["weak_preparation_stages"])


if __name__ == "__main__":
    unittest.main()
