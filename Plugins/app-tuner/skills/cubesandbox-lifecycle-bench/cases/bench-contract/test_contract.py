#!/usr/bin/env python3
"""Tier-A contracts: no CubeSandbox service, root, network or host changes needed."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[2]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_cubesandbox as lib  # noqa: E402
import cubesandbox_bench as cli  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def formal_args(output_root):
    return types.SimpleNamespace(
        command="run",
        repo=Path("/mock/CubeSandbox"),
        toolbox=Path("/mock/toolbox"),
        api_url="http://127.0.0.1:3000",
        template_id="tpl-confirmed",
        python="python3",
        numa_node=0,
        tap_target=500,
        tap_evidence="",
        cubelet_wait=1800,
        busy_threshold=10.0,
        prepare=False,
        yes=True,
        exclusive_confirmed=True,
        approved_plan_hash="",
        output_root=Path(output_root),
    )


class PlanContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = lib.load_plan(cli.DEFAULT_PLAN)

    def test_default_plan_covers_every_blog_section(self):
        self.assertEqual(list(lib.select_sections(self.plan, "all")), list(lib.SECTION_ORDER))
        self.assertEqual(len(self.plan["template_create"]["cases"]), 3)
        self.assertEqual(len(self.plan["dirty_page"]["cases"]), 8)
        self.assertEqual(
            [case["dirty_mb"] for case in self.plan["dirty_page"]["cases"]],
            [0, 10, 50, 100, 200, 500, 800, 1024],
        )
        self.assertEqual(sum(c["requests"] for c in self.plan["density"]["batches"]), 20)

    def test_section_aliases_are_canonical_and_ordered(self):
        self.assertEqual(
            lib.select_sections(self.plan, "4.6,create,dirty-page,4.5"),
            ["template_create", "dirty_page", "clone", "pause_resume"],
        )
        with self.assertRaises(lib.BenchError):
            lib.select_sections(self.plan, "not-a-test")

    def test_plan_hash_changes_with_parameters(self):
        changed = copy.deepcopy(self.plan)
        changed["snapshot"]["cases"][0]["rounds"] += 1
        self.assertNotEqual(lib.plan_digest(self.plan), lib.plan_digest(changed))

    def test_unknown_field_and_unsafe_density_are_rejected(self):
        changed = copy.deepcopy(self.plan)
        changed["snapshot"]["cases"][0]["concurency"] = 9
        with self.assertRaises(lib.BenchError):
            lib.validate_plan(changed)
        changed = copy.deepcopy(self.plan)
        changed["density"]["max_live"] = 10
        with self.assertRaises(lib.BenchError):
            lib.validate_plan(changed)

    def test_clone_concurrency_cannot_exceed_total(self):
        changed = copy.deepcopy(self.plan)
        changed["clone"]["cases"][0]["concurrency"] = 2
        with self.assertRaises(lib.BenchError):
            lib.validate_plan(changed)

    def test_non_finite_numbers_and_large_density_batch_are_rejected(self):
        for value in (float("nan"), float("inf")):
            changed = copy.deepcopy(self.plan)
            changed["snapshot"]["settle_secs"] = value
            with self.assertRaises(lib.BenchError):
                lib.validate_plan(changed)
        changed = copy.deepcopy(self.plan)
        changed["density"]["batches"][-1]["requests"] = 11
        with self.assertRaises(lib.BenchError):
            lib.validate_plan(changed)


class CommandContractTest(unittest.TestCase):
    def setUp(self):
        self.plan = lib.load_plan(cli.DEFAULT_PLAN)
        self.args = types.SimpleNamespace(
            repo=Path("/mock/CubeSandbox"),
            python="python3",
        )

    def test_every_selected_job_uses_official_tools(self):
        jobs = cli.make_jobs(
            self.args,
            self.plan,
            list(lib.SECTION_ORDER),
            Path("/mock/results"),
        )
        self.assertEqual(len(jobs), 29)
        self.assertEqual(
            sum(job["section"] == "template_create" for job in jobs), 3
        )
        self.assertEqual(sum(job["section"] == "density" for job in jobs), 4)
        self.assertTrue(all(job["keep_alive"] for job in jobs if job["section"] == "density"))
        for name, script in lib.PYTHON_SCRIPTS.items():
            self.assertTrue(any(script in job["command"][1] for job in jobs if job["section"] == name))

    def test_template_create_rounds_and_json_paths(self):
        plan = copy.deepcopy(self.plan)
        plan["template_create"]["cases"][0]["rounds"] = 3
        jobs = cli.make_jobs(self.args, plan, ["template_create"], Path("/mock/results"))
        self.assertEqual(len(jobs), 5)
        self.assertEqual(
            [Path(job["json"]).name for job in jobs[:3]],
            ["c1_n20_r1.json", "c1_n20_r2.json", "c1_n20_r3.json"],
        )
        for job in jobs:
            self.assertIn("--no-tui", job["command"])
            self.assertIn("-w", job["command"])

    def test_proxy_variables_are_removed_from_child_environment(self):
        with mock.patch.dict(os.environ, {
            "http_proxy": "http://user:secret@proxy.invalid",
            "HTTPS_PROXY": "http://proxy.invalid",
            "ALL_PROXY": "http://proxy.invalid",
            "NO_PROXY": "localhost",
            "E2B_API_KEY": "not-printed",
        }, clear=False):
            env = lib.sanitized_environment("tpl-test", "http://127.0.0.1:3000/")
        for key in lib.PROXY_VARIABLES + ("NO_PROXY", "no_proxy"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CUBE_API_URL"], "http://127.0.0.1:3000")
        self.assertEqual(env["E2B_API_URL"], "http://127.0.0.1:3000")
        self.assertEqual(env["CUBE_TEMPLATE_ID"], "tpl-test")
        with mock.patch.dict(os.environ, {
            "CUBE_API_KEY": "cube-key",
            "E2B_API_KEY": "different-e2b-key",
        }, clear=False):
            env = lib.sanitized_environment()
        self.assertEqual(env["CUBE_API_KEY"], "cube-key")
        self.assertEqual(env["E2B_API_KEY"], "cube-key")

    def test_real_run_requires_approval_before_touching_host(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "cubesandbox_bench.py"),
                "run",
                "--sections",
                "create",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--yes", result.stderr)


class ApprovalAndApiContractTest(unittest.TestCase):
    def setUp(self):
        self.plan = lib.load_plan(cli.DEFAULT_PLAN)

    def test_approval_hash_binds_scope_and_side_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = formal_args(temporary)
            sections = ["template_create"]
            baseline = cli.approval_digest(args, self.plan, sections)
            mutations = (
                ("sections", lambda: cli.approval_digest(args, self.plan, ["snapshot"])),
                ("prepare", lambda: cli.approval_digest(args, self.plan, sections)),
            )
            self.assertNotEqual(baseline, mutations[0][1]())
            args.prepare = True
            self.assertNotEqual(baseline, cli.approval_digest(args, self.plan, sections))
            args.prepare = False
            for attribute, value in (
                ("numa_node", 1),
                ("tap_target", 100),
                ("api_url", "http://192.0.2.1:3000"),
                ("template_id", "tpl-other"),
                ("repo", Path("/other/CubeSandbox")),
                ("toolbox", Path("/other/toolbox")),
                ("tap_evidence", "a" * 64),
            ):
                original = getattr(args, attribute)
                setattr(args, attribute, value)
                self.assertNotEqual(baseline, cli.approval_digest(args, self.plan, sections))
                setattr(args, attribute, original)

    def test_prepare_only_hash_cannot_authorize_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = formal_args(temporary)
            sections = ["snapshot"]
            run_hash = cli.approval_digest(args, self.plan, sections)
            args.command = "prepare"
            prepare_hash = cli.approval_digest(args, self.plan, sections)
            self.assertNotEqual(run_hash, prepare_hash)
            args.approved_plan_hash = prepare_hash
            args.command = "run"
            with self.assertRaises(lib.BenchError):
                cli.require_confirmation(args, self.plan, sections)

    def test_api_list_is_fail_closed(self):
        api = lib.CubeAPI("http://127.0.0.1:3000", "key")
        invalid_payloads = (
            {"error": "backend unavailable"},
            {"sandboxes": [{"state": "running"}]},
            {"sandboxes": [], "next_token": "page-2"},
            {"items": [{"id": "sb-1"}], "total": 2},
            {"data": ["not-an-object"]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with mock.patch.object(api, "request", return_value=FakeResponse(payload)):
                    with self.assertRaises(lib.BenchError):
                        api.list_ids()
        valid = {"sandboxes": [{"sandboxID": "sb-1"}, {"sandboxId": "sb-2"}], "total": 2}
        with mock.patch.object(api, "request", return_value=FakeResponse(valid)):
            self.assertEqual(api.list_ids(), {"sb-1", "sb-2"})

    def test_host_lock_rejects_second_mutating_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "bench.lock"
            with lib.host_lock(lock):
                with self.assertRaises(lib.BenchError):
                    with lib.host_lock(lock):
                        pass


class NumaAndTapContractTest(unittest.TestCase):
    def test_numa_rewrite_is_idempotent_and_only_touches_launches(self):
        original = """#!/bin/bash
ensure_executable \"${CUBEMASTER_BIN}\"
exec \"${CUBEMASTER_BIN}\"
"""
        updated, changed = cli.rewrite_numa_launches(original, "CUBEMASTER_BIN", 0)
        self.assertEqual(changed, 1)
        self.assertIn('exec numactl -N 0 --localalloc "${CUBEMASTER_BIN}"', updated)
        self.assertIn('ensure_executable "${CUBEMASTER_BIN}"', updated)
        second, changed_again = cli.rewrite_numa_launches(updated, "CUBEMASTER_BIN", 0)
        self.assertEqual(changed_again, 0)
        self.assertEqual(second, updated)

    def test_cubelet_two_launch_branches_are_rebound(self):
        original = """if [[ -n \"${CUBE_SANDBOX_NODE_IP:-}\" ]]; then
  (
    exec numactl -N 3 --localalloc \"${CUBELET_BIN}\" --config x
  ) &
else
  numactl -N 2 --localalloc \"${CUBELET_BIN}\" --config x &
fi
"""
        updated, changed = cli.rewrite_numa_launches(original, "CUBELET_BIN", 0)
        self.assertEqual(changed, 2)
        self.assertEqual(updated.count("numactl -N 0 --localalloc"), 2)
        self.assertNotIn("numactl -N 3", updated)
        self.assertNotIn("numactl -N 2", updated)

    def test_mixed_bound_and_unbound_launches_are_not_false_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "cubelet-start.sh"
            script.write_text(
                'exec numactl -N 0 --localalloc "${CUBELET_BIN}"\n'
                '"${CUBELET_BIN}" --config x\n'
                'ensure_executable "${CUBELET_BIN}"\n',
                encoding="utf-8",
            )
            self.assertEqual(lib.script_bindings(script, "CUBELET_BIN"), [0, -1])

    def test_cpu_list_parser(self):
        self.assertEqual(lib.parse_cpu_set("0-3,8,10-11"), {0, 1, 2, 3, 8, 10, 11})

    def test_tap_regex_is_anchored_to_interface_name(self):
        match = lib.TAP_NAME_RE.match("256: 192.168.0.225: <BROADCAST> link/ether aa")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "192.168.0.225")
        self.assertIsNone(lib.TAP_NAME_RE.match("2: enp1s0: alias 192.168.0.225"))
        self.assertIsNone(lib.TAP_NAME_RE.match("2: z192.168.0.225: <BROADCAST>"))

    def test_tap_list_requires_kernel_type_and_cube_cidr(self):
        output = (
            "1: 192.168.0.225: <UP> link/ether aa tun type tap persist on\n"
            "2: 192.168.0.226: <UP> link/ether bb\n"
            "3: 192.168.64.1: <UP> link/ether cc tun type tap persist on\n"
        )
        with mock.patch.object(
            lib, "capture", return_value=subprocess.CompletedProcess([], 0, stdout=output)
        ):
            interfaces = lib.list_ip_interfaces()
            self.assertEqual([item["verified_tap"] for item in interfaces], [True, False, True])
            network = lib.ipaddress.ip_network("192.168.0.0/18")
            self.assertEqual(lib.list_taps(network=network), [(1, "192.168.0.225")])

    def test_tap_evidence_binds_exact_ifindex_name_set(self):
        taps = [(3, "192.168.0.3"), (1, "192.168.0.1"), (2, "192.168.0.2")]
        candidates = lib.tap_cleanup_candidates(taps, 1)
        self.assertEqual(candidates, [
            {"ifindex": 2, "name": "192.168.0.2"},
            {"ifindex": 3, "name": "192.168.0.3"},
        ])
        digest = lib.tap_evidence_digest(taps, 1)
        self.assertEqual(digest, lib.tap_evidence_digest(list(reversed(taps)), 1))
        changed = [(3, "192.168.0.3"), (1, "192.168.0.1"), (9, "192.168.0.2")]
        self.assertNotEqual(digest, lib.tap_evidence_digest(changed, 1))

    def test_cubelet_tap_config_is_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            toolbox = Path(temporary)
            config = toolbox / "Cubelet/config/config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                '[plugins."io.cubelet.internal.v1.network"]\n'
                'tap_init_num = 500\ncidr = "192.168.0.0/18"\n',
                encoding="utf-8",
            )
            target, network = lib.cubelet_network_config(toolbox)
            self.assertEqual(target, 500)
            self.assertEqual(str(network), "192.168.0.0/18")

    def test_prepare_patch_writes_backup_without_real_systemd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "cubemaster-start.sh"
            backup = root / "backups"
            script.write_text('exec "${CUBEMASTER_BIN}"\n', encoding="utf-8")
            script.chmod(0o750)
            self.assertTrue(cli.patch_numa_script(script, "CUBEMASTER_BIN", 1, backup))
            self.assertIn("numactl -N 1 --localalloc", script.read_text(encoding="utf-8"))
            self.assertEqual(script.stat().st_mode & 0o777, 0o750)
            self.assertEqual(len(list(backup.glob("*.bak"))), 1)
            self.assertFalse(cli.patch_numa_script(script, "CUBEMASTER_BIN", 1, backup))

    def test_capture_timeout_is_a_controlled_failure(self):
        result = lib.capture(
            [sys.executable, "-c", "import time; time.sleep(2)"], timeout=1
        )
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stdout)


class ResultSafetyContractTest(unittest.TestCase):
    @staticmethod
    def _write_report(directory, payload, name="report.json"):
        path = Path(directory) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_cube_json_create_only_report_has_no_delete_ms(self):
        # Authoritative in-repo schema: raw is {seq, create_ms[, error]}.
        # bench-protocol analyzer doc + fixtures + timeline-profile fixture
        # all carry no per-request delete_ms; create-only must therefore pass.
        payload = {
            "config": {"total": 2, "concurrency": 1, "mode": "create-only"},
            "summary": {
                "successful": 2,
                "errors": 0,
                "success_rate": 1.0,
                "total_time_s": 0.2,
                "throughput_qps": 10,
            },
            "create": {"count": 2, "avg": 10, "p95": 12, "p99": 12},
            "raw": [
                {"seq": 1, "create_ms": 10},
                {"seq": 2, "create_ms": 11},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_report(temporary, payload)
            metrics, problems = cli.cube_json_metrics(
                path,
                expected_requests=2,
                expected_concurrency=1,
                expected_mode="create-only",
            )
            self.assertEqual(problems, [])
            self.assertEqual(metrics["successful"], 2)

    def test_cube_json_requires_complete_successful_schema(self):
        # create-delete runs do measure delete latency per request, so the
        # same authoritative schema is extended with delete_ms there.
        payload = {
            "config": {"total": 2, "mode": "create-delete"},
            "summary": {
                "successful": 2,
                "errors": 0,
                "success_rate": 1.0,
                "total_time_s": 0.2,
                "throughput_qps": 10,
            },
            "create": {"count": 2, "avg": 10, "p95": 12, "p99": 12},
            "raw": [
                {"seq": 1, "create_ms": 10, "delete_ms": 2},
                {"seq": 2, "create_ms": 11, "delete_ms": 2},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_report(temporary, payload)
            metrics, problems = cli.cube_json_metrics(
                path, expected_requests=2, expected_mode="create-delete"
            )
            self.assertEqual(problems, [])
            self.assertEqual(metrics["successful"], 2)
            _, scenario_problems = cli.cube_json_metrics(
                path, expected_requests=2, expected_concurrency=1,
                expected_mode="create-delete",
            )
            self.assertTrue(scenario_problems)
            # create-delete tier with delete_ms dropped from raw must fail.
            stripped = copy.deepcopy(payload)
            for entry in stripped["raw"]:
                entry.pop("delete_ms")
            _, stripped_problems = cli.cube_json_metrics(
                self._write_report(temporary, stripped, "stripped.json"),
                expected_requests=2,
                expected_mode="create-delete",
            )
            self.assertTrue(stripped_problems)
            for mutate in (
                lambda value: value["summary"].update(errors=None),
                lambda value: value["summary"].update(success_rate=None),
                lambda value: value.update(raw=[]),
                lambda value: value["create"].pop("avg"),
                lambda value: value.update(raw=[
                    {"seq": 1, "create_ms": 10, "delete_ms": 2, "error": "boom"},
                    {"seq": 2, "create_ms": 11, "delete_ms": 2, "error": "boom"},
                ]),
            ):
                changed = copy.deepcopy(payload)
                mutate(changed)
                _, problems = cli.cube_json_metrics(
                    self._write_report(temporary, changed),
                    expected_requests=2,
                    expected_mode="create-delete",
                )
                self.assertTrue(problems)

    def test_interrupted_job_is_recorded_and_cleaned(self):
        plan = lib.load_plan(cli.DEFAULT_PLAN)
        sections = ["snapshot"]
        with tempfile.TemporaryDirectory() as temporary:
            args = formal_args(temporary)
            args.approved_plan_hash = cli.approval_digest(args, plan, sections)
            api = mock.Mock()
            api.list_ids.return_value = set()
            api.cleanup_difference.return_value = []
            with mock.patch.object(cli, "CubeAPI", return_value=api), \
                 mock.patch.object(cli, "run_preflight", return_value=cli.Checks()), \
                 mock.patch.object(cli, "repo_commit", return_value="mock-commit"), \
                 mock.patch.object(cli, "make_jobs", return_value=[]), \
                 mock.patch.object(cli, "stream_command", side_effect=KeyboardInterrupt()):
                with self.assertRaises(lib.BenchError):
                    cli.run_benchmark(args, plan, sections)
            api.cleanup_difference.assert_called_once_with(set())
            result_files = list(Path(temporary).glob("*/results.json"))
            self.assertEqual(len(result_files), 1)
            result = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["records"]), 1)
            self.assertEqual(result["records"][0]["return_code"], 130)
            self.assertIn("用户中断", result["records"][0]["problems"][0])

    def test_interrupt_during_cleanup_still_persists_job_record(self):
        plan = lib.load_plan(cli.DEFAULT_PLAN)
        sections = ["snapshot"]
        with tempfile.TemporaryDirectory() as temporary:
            args = formal_args(temporary)
            args.approved_plan_hash = cli.approval_digest(args, plan, sections)
            api = mock.Mock()
            api.list_ids.return_value = set()
            api.cleanup_difference.side_effect = KeyboardInterrupt()

            def smoke_stream(command, cwd, env, log_path):
                log_path.write_text("SMOKE_OK\n", encoding="utf-8")
                return 0, 0.01

            with mock.patch.object(cli, "CubeAPI", return_value=api), \
                 mock.patch.object(cli, "run_preflight", return_value=cli.Checks()), \
                 mock.patch.object(cli, "repo_commit", return_value="mock-commit"), \
                 mock.patch.object(cli, "make_jobs", return_value=[]), \
                 mock.patch.object(cli, "stream_command", side_effect=smoke_stream):
                with self.assertRaises(lib.BenchError):
                    cli.run_benchmark(args, plan, sections)
            result_files = list(Path(temporary).glob("*/results.json"))
            result = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertEqual(len(result["records"]), 1)
            self.assertEqual(result["records"][0]["return_code"], 130)
            self.assertTrue(any("差集清理" in p for p in result["records"][0]["problems"]))

    def test_density_baseline_failure_never_arms_empty_baseline_cleanup(self):
        plan = lib.load_plan(cli.DEFAULT_PLAN)
        sections = ["density"]
        with tempfile.TemporaryDirectory() as temporary:
            args = formal_args(temporary)
            args.approved_plan_hash = cli.approval_digest(args, plan, sections)
            api = mock.Mock()
            api.list_ids.side_effect = [set(), set(), lib.BenchError("temporary API failure")]
            api.cleanup_difference.return_value = []

            def smoke_stream(command, cwd, env, log_path):
                log_path.write_text("SMOKE_OK\n", encoding="utf-8")
                return 0, 0.01

            with mock.patch.object(cli, "CubeAPI", return_value=api), \
                 mock.patch.object(cli, "run_preflight", return_value=cli.Checks()), \
                 mock.patch.object(cli, "repo_commit", return_value="mock-commit"), \
                 mock.patch.object(cli, "stream_command", side_effect=smoke_stream), \
                 mock.patch.object(cli.time, "sleep", return_value=None):
                with self.assertRaises(lib.BenchError):
                    cli.run_benchmark(args, plan, sections)
            # Only smoke cleanup is allowed; density baseline was never armed.
            self.assertEqual(api.cleanup_difference.call_count, 1)
            result_files = list(Path(temporary).glob("*/results.json"))
            result = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertIsNone(result["density_cleanup"])

    def test_interrupt_during_density_sampling_persists_record_and_cleans(self):
        plan = lib.load_plan(cli.DEFAULT_PLAN)
        sections = ["density"]
        with tempfile.TemporaryDirectory() as temporary:
            args = formal_args(temporary)
            args.approved_plan_hash = cli.approval_digest(args, plan, sections)
            api = mock.Mock()
            api.list_ids.return_value = set()
            api.cleanup_difference.return_value = []

            def fake_stream(command, cwd, env, log_path):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("SMOKE_OK\n", encoding="utf-8")
                return 0, 0.01

            with mock.patch.object(cli, "CubeAPI", return_value=api), \
                 mock.patch.object(cli, "run_preflight", return_value=cli.Checks()), \
                 mock.patch.object(cli, "repo_commit", return_value="mock-commit"), \
                 mock.patch.object(cli, "stream_command", side_effect=fake_stream), \
                 mock.patch.object(cli, "cube_json_metrics", return_value=({"successful": 1}, [])), \
                 mock.patch.object(cli.time, "sleep", side_effect=[None, KeyboardInterrupt()]):
                with self.assertRaises(lib.BenchError):
                    cli.run_benchmark(args, plan, sections)
            result_files = list(Path(temporary).glob("*/results.json"))
            result = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertEqual(len(result["records"]), 2)
            self.assertEqual(result["records"][-1]["return_code"], 130)
            self.assertTrue(any("density 采样" in p for p in result["records"][-1]["problems"]))
            self.assertEqual(api.cleanup_difference.call_count, 2)
            self.assertEqual(result["density_cleanup"], [])

    def test_density_drains_before_snapshot_and_waits_use_baseline(self):
        plan = lib.load_plan(cli.DEFAULT_PLAN)
        sections = ["density", "snapshot"]
        with tempfile.TemporaryDirectory() as temporary:
            args = formal_args(temporary)
            args.approved_plan_hash = cli.approval_digest(args, plan, sections)

            class FakeAPI:
                def __init__(self):
                    self.current = set()
                    self.events = []
                    self.sequence = 0

                def list_ids(self):
                    return set(self.current)

                def cleanup_difference(self, baseline):
                    created = self.current - baseline
                    self.events.append(("cleanup", len(created)))
                    self.current -= created
                    return [
                        {"sandbox_id": sandbox_id, "ok": True, "message": "deleted"}
                        for sandbox_id in sorted(created)
                    ]

            api = FakeAPI()

            def fake_stream(command, cwd, env, log_path):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if command[0].endswith("cube-bench"):
                    count = int(command[command.index("-n") + 1])
                    concurrency = int(command[command.index("-c") + 1])
                    mode = command[command.index("-m") + 1]
                    report_path = Path(command[command.index("-o") + 1])
                    created = set()
                    for _ in range(count):
                        api.sequence += 1
                        created.add(f"sb-density-{api.sequence}")
                    api.current.update(created)
                    api.events.append(("density", len(api.current)))
                    # Real authoritative create-only report: raw has no delete_ms.
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(
                        json.dumps(
                            {
                                "config": {
                                    "template": "tpl-confirmed",
                                    "concurrency": concurrency,
                                    "total": count,
                                    "mode": mode,
                                },
                                "summary": {
                                    "successful": count,
                                    "errors": 0,
                                    "success_rate": 1.0,
                                    "total_time_s": 0.5,
                                    "throughput_qps": count / 0.5,
                                },
                                "create": {
                                    "count": count,
                                    "avg": 50,
                                    "p95": 60,
                                    "p99": 60,
                                },
                                "raw": [
                                    {"seq": index, "create_ms": 50}
                                    for index in range(1, count + 1)
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    log_path.write_text("density batch done\n", encoding="utf-8")
                elif command[1] == "-c":
                    log_path.write_text("SMOKE_OK\n", encoding="utf-8")
                else:
                    api.sequence += 1
                    api.current.add(f"sb-snapshot-{api.sequence}")
                    api.events.append(("snapshot", len(api.current)))
                    log_path.write_text("snapshot batch done\n", encoding="utf-8")
                return 0, 0.01

            with mock.patch.object(cli, "CubeAPI", return_value=api), \
                 mock.patch.object(cli, "run_preflight", return_value=cli.Checks()), \
                 mock.patch.object(cli, "repo_commit", return_value="mock-commit"), \
                 mock.patch.object(cli, "stream_command", side_effect=fake_stream), \
                 mock.patch.object(cli.time, "sleep", return_value=None):
                # No mock on cube_json_metrics: density batches are judged by
                # the real report parser against create-only reports.
                self.assertEqual(cli.run_benchmark(args, plan, sections), 0)

            event_names = [name for name, _ in api.events]
            self.assertLess(api.events.index(("cleanup", 20)), event_names.index("snapshot"))
            self.assertEqual(api.current, set())
            result_files = list(Path(temporary).glob("*/results.json"))
            result = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["density_cleanup"]), 20)
            self.assertEqual(len(result["records"]), 8)
            # Every density record went through the real parser with no problems.
            batch_requests = {"batch1": 1, "batch2": 4, "batch3": 5, "batch4": 10}
            for record in result["records"]:
                if record["section"] == "density":
                    self.assertEqual(record["problems"], [])
                    expected = batch_requests[record["case"]]
                    self.assertEqual(record["metrics"]["successful"], expected)
            # Raw per-request entries persisted on disk carry no delete_ms.
            density_reports = list(Path(temporary).glob("*/density/batch*.json"))
            self.assertEqual(len(density_reports), 4)
            for report_path in density_reports:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                for entry in report["raw"]:
                    self.assertNotIn("delete_ms", entry)

        # A non-empty baseline is valid; waiting must not require global zero.
        waiting_api = mock.Mock()
        waiting_api.list_ids.return_value = {"sb-existing"}
        cli.wait_api_baseline(waiting_api, {"sb-existing"}, timeout=1)

    def test_create_analyzer_nonzero_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "template-create").mkdir()
            args = types.SimpleNamespace(python="python3")
            with mock.patch.object(
                cli, "capture", return_value=subprocess.CompletedProcess([], 3, stdout="bad report")
            ):
                with self.assertRaises(lib.BenchError):
                    cli.analyze_create_reports(args, {}, run_dir)


class CliContractTest(unittest.TestCase):
    def test_dry_run_expands_without_repo_or_approval(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "cubesandbox_bench.py"),
                "run",
                "--dry-run",
                "--repo",
                "/path/that/need/not/exist",
                "--sections",
                "4.2,clone",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY-RUN", result.stdout)
        self.assertEqual(result.stdout.count("bench_snapshot_dirty.py"), 8)
        self.assertEqual(result.stdout.count("bench_clone_concurrency.py"), 3)
        self.assertNotIn("SMOKE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
