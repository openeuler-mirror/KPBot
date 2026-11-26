#!/usr/bin/env python3
"""Parameterized Tier-A contract for one focused CubeSandbox skill."""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest import mock

COMMON_DIR = Path(__file__).resolve().parents[2]
SCRIPTS = COMMON_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cubesandbox_bench as bench  # noqa: E402
import cubesandbox_skill as focused  # noqa: E402
import lib_cubesandbox as lib  # noqa: E402

EXPECTED_JOBS = {
    "template_create": 3,
    "density": 4,
    "snapshot": 3,
    "dirty_page": 8,
    "snapshot_create": 3,
    "rollback": 3,
    "clone": 3,
    "pause_resume": 2,
}
HASH_RE = re.compile(r"APPROVAL_SHA256：([0-9a-f]{64})")
PLAN_HASH_RE = re.compile(r"PLAN_SHA256：([0-9a-f]{64})")


class Contract:
    def __init__(self, skill_dir: Path, results_dir: Path):
        self.skill_dir = skill_dir.resolve()
        self.skill = self.skill_dir.name
        self.results_dir = results_dir.resolve()
        self.entry = self.skill_dir / "scripts/run.sh"
        self.checks = []

    def record(self, name: str, condition: bool, detail=""):
        if not condition:
            raise AssertionError(f"{name}: {detail or 'assertion failed'}")
        self.checks.append(name)
        print(f"ok: {name}")

    def run_entry(self, args, cwd=None):
        return subprocess.run(
            [str(self.entry), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            cwd=cwd,
        )

    def static_contract(self):
        document = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.record(
            "independent metadata and executable entry",
            f"name: {self.skill}" in document
            and "## 唯一职责与输入" in document
            and "## 输出与成功判据" in document
            and "## 受阻与边界" in document
            and self.entry.is_file(),
        )
        self.record(
            "shared executor and case are present",
            (SCRIPTS / "cubesandbox_bench.py").is_file()
            and (SCRIPTS / "lib_cubesandbox.py").is_file()
            and (COMMON_DIR / "assets/default-plan.json").is_file()
            and (self.skill_dir / "cases/skill-contract/case.yaml").is_file(),
        )

    def scenario(self, section: str):
        base = [
            "--repo", "/mock/CubeSandbox", "--template-id", "tpl-confirmed",
            "--numa-node", "0",
        ]
        preview = self.run_entry([*base, "--dry-run"])
        output = preview.stdout + preview.stderr
        self.record("fixed-section dry-run succeeds", preview.returncode == 0, output)
        job_count = len(re.findall(r"^\[[^]]+ / [^]]+\]", preview.stdout, re.MULTILINE))
        self.record(
            "all and only this section's default jobs expand",
            job_count == EXPECTED_JOBS[section]
            and re.search(r'"sections": \[\s+"' + re.escape(section) + r'"\s+\],', preview.stdout)
            and all(f"[{other} /" not in preview.stdout for other in EXPECTED_JOBS if other != section)
            and "DRY-RUN" in preview.stdout,
            preview.stdout,
        )
        shown = self.run_entry(["--show-plan", *base, "--operation", "run"])
        dry_hash = HASH_RE.search(preview.stdout)
        show_hash = HASH_RE.search(shown.stdout)
        formal_return = None
        formal_calls = 0
        if dry_hash and show_hash and dry_hash.group(1) == show_hash.group(1):
            with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
                with mock.patch.object(
                    bench, "host_lock", return_value=contextlib.nullcontext()
                ), mock.patch.object(
                    bench, "run_benchmark", return_value=0
                ) as run_mock:
                    formal_return = focused.main([
                        self.skill, *base, "--operation=run", "--output-root", temporary,
                        "--yes", "--exclusive-confirmed",
                        "--approved-plan-hash", show_hash.group(1),
                    ])
                formal_calls = run_mock.call_count
        self.record(
            "show-plan run hash authorizes the same focused formal run",
            shown.returncode == 0 and dry_hash and show_hash
            and dry_hash.group(1) == show_hash.group(1)
            and '"operation": "run"' in shown.stdout
            and formal_return == 0 and formal_calls == 1,
            shown.stdout + shown.stderr,
        )

        conflicts = []
        conflict_output = []
        for operation in (("--operation", "prepare"), ("--operation=prepare",)):
            with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
                conflict = self.run_entry(
                    ["--show-plan", *base, *operation], cwd=temporary
                )
                conflicts.append(
                    conflict.returncode == 2
                    and "固定 --operation run" in conflict.stderr
                    and "APPROVAL_SHA256" not in conflict.stdout
                    and not any(Path(temporary).iterdir())
                )
                conflict_output.append(conflict.stdout + conflict.stderr)
        self.record(
            "scenario prepare operation is rejected before any output",
            all(conflicts),
            "\n".join(conflict_output),
        )

        prepare_args = [*base, "--prepare", "--tap-evidence", "a" * 64]
        prepared_preview = self.run_entry([*prepare_args, "--dry-run"])
        prepared_shown = self.run_entry(["--show-plan", *prepare_args])
        prepared_dry_hash = HASH_RE.search(prepared_preview.stdout)
        prepared_show_hash = HASH_RE.search(prepared_shown.stdout)
        self.record(
            "prepare flag remains a run-before-prepare approval scope",
            prepared_preview.returncode == 0 and prepared_shown.returncode == 0
            and '"operation": "run"' in prepared_shown.stdout
            and '"prepare": true' in prepared_shown.stdout
            and prepared_dry_hash and prepared_show_hash
            and prepared_dry_hash.group(1) == prepared_show_hash.group(1),
            prepared_preview.stdout + prepared_shown.stdout + prepared_shown.stderr,
        )

        with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
            custom_path = Path(temporary) / "plan.json"
            default = lib.load_plan(bench.DEFAULT_PLAN)
            section_plan = copy.deepcopy(default[section])
            section_plan["settle_secs"] += 1
            custom = {
                "schema_version": 1,
                "name": f"{self.skill}-custom",
                section: section_plan,
            }
            custom_path.write_text(json.dumps(custom), encoding="utf-8")
            changed = self.run_entry([*base, "--plan", str(custom_path), "--dry-run"])
            old_digest = PLAN_HASH_RE.search(preview.stdout)
            new_digest = PLAN_HASH_RE.search(changed.stdout)
            self.record(
                "single-section custom plan and supported parameters are accepted",
                changed.returncode == 0 and custom["name"] in changed.stdout
                and old_digest and new_digest
                and old_digest.group(1) != new_digest.group(1)
                and len(re.findall(r"^\[[^]]+ / [^]]+\]", changed.stdout, re.MULTILINE))
                    == EXPECTED_JOBS[section],
                changed.stdout + changed.stderr,
            )

        escaped = self.run_entry([*base, "--sections", "all", "--dry-run"])
        self.record(
            "cross-section override is rejected before command expansion",
            escaped.returncode == 2 and "固定 section=" in escaped.stderr
            and "DRY-RUN" not in escaped.stdout,
            escaped.stdout + escaped.stderr,
        )
        unapproved = self.run_entry(base)
        self.record(
            "formal run still requires confirmation before host access",
            unapproved.returncode == 2 and "--yes" in unapproved.stderr,
            unapproved.stdout + unapproved.stderr,
        )

    def preflight(self):
        base = [
            self.skill, "--repo", "/mock/CubeSandbox",
            "--template-id", "tpl-confirmed", "--numa-node", "0",
        ]
        with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
            output = Path(temporary) / "preflight.json"
            checks = bench.Checks()
            checks.pass_("mock.read_only", "ok")
            stdout = io.StringIO()
            with mock.patch.object(bench, "resolve_template", return_value="tpl-confirmed"), \
                 mock.patch.object(bench, "run_preflight", return_value=checks), \
                 contextlib.redirect_stdout(stdout):
                return_code = focused.main([*base, "--output", str(output)])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.record(
                "mocked read-only check writes successful preflight.json",
                return_code == 0 and payload["ok"] is True
                and payload["checks"][0]["name"] == "mock.read_only"
                and json.loads(stdout.getvalue())["ok"] is True,
            )

            failed_output = Path(temporary) / "failed-preflight.json"
            failed = bench.Checks()
            failed.fail("mock.blocked", "environment not ready")
            with mock.patch.object(bench, "resolve_template", return_value="tpl-confirmed"), \
                 mock.patch.object(bench, "run_preflight", return_value=failed), \
                 contextlib.redirect_stdout(io.StringIO()):
                return_code = focused.main([*base, "--output", str(failed_output)])
            payload = json.loads(failed_output.read_text(encoding="utf-8"))
            self.record(
                "failed check remains blocked and persists evidence",
                return_code == 1 and payload["ok"] is False and payload["failures"] == 1,
            )

            stale_output = Path(temporary) / "stale-preflight.json"
            stale_output.write_text(
                '{"ok": true, "checks": [], "sentinel": "stale"}\n', encoding="utf-8"
            )
            api = mock.Mock()
            api.health.return_value = (False, "mock offline")
            api.list_ids.return_value = set()
            stderr = io.StringIO()
            with mock.patch.object(bench, "is_root", return_value=True), \
                 mock.patch.object(bench, "python_imports", return_value=(False, "mock sdk")), \
                 mock.patch.object(bench, "service_is_active", return_value=False), \
                 mock.patch.object(bench, "CubeAPI", return_value=api), \
                 mock.patch.object(bench, "process_inventory", return_value=[]), \
                 mock.patch.object(bench, "available_memory_mb", return_value=8192), \
                 mock.patch.object(bench, "numa_topology", return_value={}), \
                 mock.patch.object(
                     bench, "cubelet_network_config",
                     side_effect=lib.BenchError("mock config absent"),
                 ), \
                 mock.patch.object(
                     bench.subprocess, "run",
                     side_effect=FileNotFoundError(
                         2, "No such file or directory", "cubemastercli"
                     ),
                 ), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(stderr):
                return_code = focused.main([
                    *base, "--sections", "pause_resume", "--output", str(stale_output),
                ])
            payload = json.loads(stale_output.read_text(encoding="utf-8"))
            template_checks = [
                item for item in payload["checks"] if item["name"] == "template.ready"
            ]
            self.record(
                "missing cubemastercli overwrites stale report with template.ready FAIL",
                return_code == 1 and payload["ok"] is False
                and "sentinel" not in payload and len(template_checks) == 1
                and template_checks[0]["status"] == "FAIL"
                and "缺少命令 cubemastercli" in template_checks[0]["detail"]
                and "Traceback" not in stderr.getvalue(),
                stderr.getvalue(),
            )

            misuse_dir = Path(temporary) / "misuse"
            misuse_dir.mkdir()
            misuse = self.run_entry(["--dry-run"], cwd=misuse_dir)
            self.record(
                "preflight rejects run-only options without touching the host",
                misuse.returncode == 2 and not (misuse_dir / "preflight.json").exists(),
                misuse.stdout + misuse.stderr,
            )
        rewritten = focused.focused_argv(self.skill, [])
        self.record(
            "default output is a standalone preflight.json",
            rewritten[:2] == ["check", "--json"]
            and rewritten[-1].endswith("preflight.json"),
        )

    def prepare(self):
        evidence = "a" * 64
        base = [
            "--repo", "/mock/CubeSandbox", "--template-id", "tpl-confirmed",
            "--numa-node", "0", "--tap-evidence", evidence,
        ]
        shown = self.run_entry(["--show-plan", *base])
        approval = HASH_RE.search(shown.stdout)
        self.record(
            "prepare-only show-plan succeeds without host access",
            shown.returncode == 0 and approval
            and '"operation": "prepare"' in shown.stdout,
            shown.stdout + shown.stderr,
        )
        if not approval:
            raise AssertionError("missing prepare approval hash")

        with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
            formal = [
                self.skill, *base, "--output-root", temporary,
                "--yes", "--exclusive-confirmed",
                "--approved-plan-hash", approval.group(1),
            ]
            checks = bench.Checks()
            checks.pass_("mock.ready", "ok")
            readiness = {"ok": True, "tap_count": 500, "numa_affinity_ok": True}
            with mock.patch.object(bench, "resolve_template", return_value="tpl-confirmed"), \
                 mock.patch.object(bench, "host_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(
                     bench, "_prepare_environment_impl", return_value={"readiness": readiness}
                 ), \
                 mock.patch.object(bench, "run_preflight", return_value=checks), \
                 contextlib.redirect_stdout(io.StringIO()):
                return_code = focused.main(formal)
            run_dirs = [path for path in Path(temporary).iterdir() if path.is_dir()]
            journal = json.loads((run_dirs[0] / "prepare.json").read_text(encoding="utf-8"))
            preflight = json.loads((run_dirs[0] / "preflight.json").read_text(encoding="utf-8"))
            self.record(
                "mocked prepare completes journal, readiness and post-check",
                return_code == 0 and len(run_dirs) == 1
                and journal["status"] == "completed"
                and journal["result"]["readiness"] == readiness
                and preflight["ok"] is True,
            )

        def fail_after_action(args, env, run_dir, actions):
            bench.journal_action(run_dir, actions, "mock_action", {"blocked": True})
            raise lib.BenchError("mock blocked")

        with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
            formal = [
                self.skill, *base, "--output-root", temporary,
                "--yes", "--exclusive-confirmed",
                "--approved-plan-hash", approval.group(1),
            ]
            with mock.patch.object(bench, "resolve_template", return_value="tpl-confirmed"), \
                 mock.patch.object(bench, "host_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(
                     bench, "_prepare_environment_impl", side_effect=fail_after_action,
                 ), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assert_raises(lib.BenchError):
                    focused.main(formal)
            run_dirs = [path for path in Path(temporary).iterdir() if path.is_dir()]
            journal = json.loads((run_dirs[0] / "prepare.json").read_text(encoding="utf-8"))
            self.record(
                "blocked prepare persists failed journal and never starts a benchmark",
                len(run_dirs) == 1 and journal["status"] == "failed"
                and "mock blocked" in journal["error"]
                and journal["actions"]
                and journal["actions"][0]["action"] == "mock_action"
                and not (run_dirs[0] / "results.json").exists(),
            )

        with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
            unapproved = self.run_entry([*base, "--output-root", temporary])
            self.record(
                "missing confirmation fails before creating a prepare directory",
                unapproved.returncode == 2 and "--yes" in unapproved.stderr
                and not any(Path(temporary).iterdir()),
                unapproved.stdout + unapproved.stderr,
            )
        with tempfile.TemporaryDirectory(dir=self.results_dir) as temporary:
            mismatched = self.run_entry([
                *base, "--output-root", temporary, "--yes", "--exclusive-confirmed",
                "--approved-plan-hash", "0" * 64,
            ])
            self.record(
                "approval mismatch fails before creating a prepare directory",
                mismatched.returncode == 2 and "批准哈希不匹配" in mismatched.stderr
                and not any(Path(temporary).iterdir()),
                mismatched.stdout + mismatched.stderr,
            )
        escaped = self.run_entry(["--show-plan", *base, "--operation", "run"])
        self.record(
            "prepare entry cannot be redirected to run",
            escaped.returncode == 2 and "固定 --operation prepare" in escaped.stderr,
            escaped.stdout + escaped.stderr,
        )

    @staticmethod
    def assert_raises(error):
        return _AssertRaises(error)

    def run(self):
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.static_contract()
        if self.skill not in focused.SKILL_OPERATIONS:
            raise AssertionError(f"unregistered focused skill: {self.skill}")
        operation = focused.SKILL_OPERATIONS[self.skill]
        if operation == "run":
            self.scenario(focused.SKILL_SECTIONS[self.skill])
        elif operation == "check":
            self.preflight()
        else:
            self.prepare()
        return {
            "success": True,
            "skill": self.skill,
            "operation": operation,
            "checks": len(self.checks),
            "check_names": self.checks,
            "requires_live_service": False,
        }


class _AssertRaises:
    def __init__(self, expected):
        self.expected = expected

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            raise AssertionError(f"expected {self.expected.__name__}")
        return issubclass(exc_type, self.expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    args = parser.parse_args()
    contract = Contract(args.skill_dir, args.results_dir)
    try:
        result = contract.run()
    except Exception as exc:
        contract.results_dir.mkdir(parents=True, exist_ok=True)
        lib.write_json(contract.results_dir / "result.json", {
            "success": False,
            "skill": contract.skill,
            "error": f"{type(exc).__name__}: {exc}",
            "checks": len(contract.checks),
            "requires_live_service": False,
        })
        raise
    lib.write_json(contract.results_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
