"""fastokens_segment_cache.py 的测试，仅依赖 Python 标准库。运行方式：

    python3 -m unittest discover -s tests -v

端到端测试依赖已安装 vLLM 的 Python 环境，运行前设置以下三个环境变量：
    E2E_VLLM_PYTHON  已安装 vLLM 的 Python 解释器
    E2E_MODEL        模型名或本地分词器路径
    E2E_OVERLAY      候选安装目录，即候选版本 fastokens 所在的目录
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from array import array
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fastokens_segment_cache.py"
_spec = importlib.util.spec_from_file_location("fastokens_segment_cache", SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class HelperTests(unittest.TestCase):
    def test_secrets_are_redacted(self):
        args = [
            "vllm",
            "serve",
            "m",
            "--tokenizer",
            "/models/tokenizer",
            "--tokenizer-mode",
            "auto",
            "--tokenizer-revision",
            "main",
            "--api-key",
            "sk-123",
            "--hf-token=abc",
            "https://u:p@host/a",
        ]
        out = " ".join(tool.redact_args(args))
        for secret in ("sk-123", "abc", "u:p"):
            self.assertNotIn(secret, out)
        self.assertIn("--api-key [已脱敏]", out)
        self.assertIn("--tokenizer /models/tokenizer", out)
        self.assertIn("--tokenizer-mode auto", out)
        self.assertIn("--tokenizer-revision main", out)

    def test_tokenizer_options_survive_both_argument_forms(self):
        for flag, value in (
            ("--tokenizer", "/models/x"),
            ("--tokenizer-mode", "auto"),
            ("--tokenizer-revision", "main"),
        ):
            for args in ([flag, value], [f"{flag}={value}"]):
                with self.subTest(args=args):
                    self.assertEqual(tool.redact_args(args), args)

    def test_credential_options_are_redacted_in_both_argument_forms(self):
        for flag in ("--api-key", "--hf-token", "--access-token", "--secret", "--password"):
            with self.subTest(flag=flag):
                self.assertEqual(tool.redact_args([flag, "sk-1"]), [flag, "[已脱敏]"])
                self.assertEqual(tool.redact_args([f"{flag}=sk-1"]), [f"{flag}=[已脱敏]"])

    def test_sessions_stay_on_one_thread_in_order(self):
        requests = [{"session": s} for s in "abcabcab"]
        groups = tool.split_by_session(requests, 2)
        self.assertEqual(sorted(i for group in groups for i in group), list(range(8)))
        for group in groups:
            self.assertEqual(group, sorted(group))
            for session in "abc":
                indices = {i for i in range(8) if requests[i]["session"] == session}
                self.assertTrue(indices <= set(group) or not indices & set(group))

    def test_generated_sessions_are_deterministic_and_grow(self):
        first = tool.generate_sessions(3, 4, seed=1, block_chars=500)
        self.assertEqual(first, tool.generate_sessions(3, 4, seed=1, block_chars=500))
        self.assertNotEqual(first, tool.generate_sessions(3, 4, seed=2, block_chars=500))
        self.assertEqual(len(first), 12)
        session = [r for r in first if r["session"] == "s0"]
        for previous, current in zip(session, session[1:]):
            prefix = current["messages"][: len(previous["messages"])]
            self.assertEqual(prefix, previous["messages"])
            self.assertGreater(len(current["messages"]), len(previous["messages"]))


class ProcessDiscoveryTests(unittest.TestCase):
    def test_recognizes_vllm_service_entry_points(self):
        accepted = [
            ["/venv/bin/vllm", "serve", "model"],
            ["/venv/bin/python", "/venv/bin/vllm", "serve", "model"],
            ["python", "-m", "vllm.entrypoints.openai.api_server", "--model", "model"],
            ["python", "-m", "vllm.entrypoints.api_server", "--model", "model"],
            ["python", "-m", "vllm", "serve", "model"],
            ["python", "/site-packages/vllm/entrypoints/openai/api_server.py"],
        ]
        for args in accepted:
            with self.subTest(args=args):
                self.assertTrue(tool.is_vllm_service_process(args))

    def test_rejects_commands_that_only_contain_vllm_text(self):
        rejected = [
            [
                "/Users/demo/Desktop/vllm&vllm-ascend/.venv/bin/python",
                "/skills/vllm-fastokens-segment-cache-migration/scripts/fastokens_segment_cache.py",
                "probe",
            ],
            ["tail", "-f", "/var/log/vllm.log"],
            ["tail", "-f", "/opt/vllm/entrypoints/openai/api_server.py"],
            ["grep", "vllm"],
            ["python", "worker.py", "--name", "vllm"],
            ["python", "worker.py", "-m", "vllm.entrypoints.openai.api_server"],
            ["vllm", "bench", "throughput"],
            ["vllm", "bench", "--dataset-name", "serve"],
        ]
        for args in rejected:
            with self.subTest(args=args):
                self.assertFalse(tool.is_vllm_service_process(args))

    def scan_fixture(self, processes, pids=None):
        def describe(pid, args):
            return {
                "pid": pid,
                "env": {} if pid < 20 else {tool.RUST_VAR: "1", tool.SWITCH_VAR: "1"},
                "fastokens_loaded": [],
            }

        with (
            mock.patch.object(tool.sys, "platform", "linux"),
            mock.patch.object(tool.os.path, "isdir", return_value=True),
            mock.patch.object(tool, "read_linux_processes", return_value=processes),
            mock.patch.object(tool, "describe_process", side_effect=describe),
        ):
            rows, note = tool.scan_processes(pids)
        self.assertIsNone(note)
        return rows

    def test_scan_excludes_unrelated_processes_from_backend_and_verdict(self):
        processes = {
            10: (["/venv/bin/vllm", "serve", "model"], 1),
            11: (["engine-core"], 10),
            12: (["worker"], 11),
            20: (["grep", "vllm"], 1),
            21: (["unrelated-child"], 20),
            30: (["python", str(SCRIPT), "_worker"], 1),
            31: (["tail", "-f", "/opt/vllm/entrypoints/openai/api_server.py"], 1),
        }
        info = verdict_info()
        info["processes"] = self.scan_fixture(processes)
        self.assertEqual([row["pid"] for row in info["processes"]], [10, 11, 12])
        backend = tool.effective_backend(info)
        self.assertIn("HF tokenizers", backend)
        self.assertNotIn("pid 20", backend)
        self.assertNotEqual(tool.probe_verdict(info, tool.MIN_VERSION)["decision"], "不支持")

    def test_explicit_pid_still_selects_only_the_requested_process(self):
        processes = {
            10: (["vllm", "serve", "model"], 1),
            11: (["engine-core"], 10),
        }
        self.assertEqual([row["pid"] for row in self.scan_fixture(processes, [10])], [10])


def make_installed_package(root: Path, editable=False):
    """在 root 下构造一个已安装的包，包含包文件、字节码缓存与安装工具写入的文件。"""
    package_dir, dist_info_dir = root / "pkgx", root / "pkgx-1.0.dist-info"
    (package_dir / "__pycache__").mkdir(parents=True)
    dist_info_dir.mkdir()
    direct_url = {"url": "file:///tmp/pkgx", "dir_info": {"editable": editable}}
    files = {
        "pkgx/__init__.py": b"VALUE = 1\n",
        "pkgx/_native.abi3.so": bytes(range(256)) * 64,
        "pkgx/__pycache__/__init__.cpython-312.pyc": b"pyc",
        "pkgx-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: pkgx\nVersion: 1.0\n",
        "pkgx-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "pkgx-1.0.dist-info/INSTALLER": b"pip\n",
        "pkgx-1.0.dist-info/uv_cache.json": b"{}",
        "pkgx-1.0.dist-info/direct_url.json": json.dumps(direct_url).encode(),
    }
    for relative, data in files.items():
        (root / relative).write_bytes(data)
    record = [f"{relative},," for relative in files]
    record += ["pkgx-1.0.dist-info/RECORD,,", "../../bin/pkgx,,"]
    (dist_info_dir / "RECORD").write_text("\n".join(record) + "\n")
    return metadata.PathDistribution(dist_info_dir)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_wheel_rebuilt_from_installed_files(self):
        dist = make_installed_package(self.tmp / "site")
        out = self.tmp / "out"
        out.mkdir()
        info = tool.wheel_from_installed("pkgx", out, dist=dist)
        self.assertEqual(Path(info["wheel"]).name, "pkgx-1.0-py3-none-any.whl")
        expected = [
            "pkgx/__init__.py",
            "pkgx/_native.abi3.so",
            "pkgx-1.0.dist-info/METADATA",
            "pkgx-1.0.dist-info/WHEEL",
            "pkgx-1.0.dist-info/RECORD",
        ]
        with zipfile.ZipFile(info["wheel"]) as archive:
            names = archive.namelist()
            self.assertEqual(sorted(names), sorted(expected))
            self.assertEqual(names[-1], "pkgx-1.0.dist-info/RECORD")
            for line in archive.read("pkgx-1.0.dist-info/RECORD").decode().splitlines():
                path, digest, size = line.split(",")
                if path.endswith("RECORD"):
                    continue
                data = archive.read(path)
                want = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                self.assertEqual(digest, f"sha256={want.decode()}")
                self.assertEqual(int(size), len(data))

    @unittest.skipUnless(importlib.util.find_spec("pip"), "当前 Python 环境中未安装 pip")
    def test_rebuilt_wheel_installs_the_same_bytes(self):
        dist = make_installed_package(self.tmp / "site")
        out = self.tmp / "out"
        out.mkdir()
        wheel = tool.wheel_from_installed("pkgx", out, dist=dist)["wheel"]
        target = self.tmp / "target"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                "--no-deps",
                "--target",
                str(target),
                wheel,
            ],
            check=True,
            capture_output=True,
        )
        for relative in ("pkgx/__init__.py", "pkgx/_native.abi3.so"):
            installed = (target / relative).read_bytes()
            self.assertEqual(installed, (self.tmp / "site" / relative).read_bytes())

    def test_editable_install_is_refused(self):
        dist = make_installed_package(self.tmp / "site", editable=True)
        info = tool.wheel_from_installed("pkgx", self.tmp, dist=dist)
        self.assertIn("可编辑", info["error"])
        self.assertNotIn("wheel", info)


def fake_run(digests, lengths, backend="fastokens", stats=None, encode_ms=1.0):
    return {
        "ok": True,
        "backend_module": backend,
        "backend_class": "Shim",
        "digests": digests,
        "lengths": lengths,
        "encode_ms": [encode_ms] * len(digests),
        "stats": stats,
        "text_sha": "t",
        "env": {},
    }


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.requests = [{"session": "s0"}, {"session": "s0"}]
        self.args = types.SimpleNamespace(
            model="m",
            sessions=str(self.tmp / "x.jsonl"),
            rounds=1,
            threads=1,
            cache_mib=64,
            overlay=None,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_ids(self, name, sequences):
        with open(self.tmp / f"{name}.ids", "wb") as f:
            for sequence in sequences:
                array("I", sequence).tofile(f)

    def summarize(self, runs):
        return tool.summarize(self.args, self.requests, runs, self.tmp, None, None)

    def test_mismatch_reports_first_different_position(self):
        self.write_ids("orig", [[1, 2, 3], [4, 5, 6, 7]])
        self.write_ids("on", [[1, 2, 3], [4, 5, 9, 7]])
        hit = {"hits": 1, "misses": 1, "hit_bytes": 5, "miss_bytes": 5}
        report = self.summarize(
            {
                "orig": [fake_run(["a", "b"], [3, 4], backend="tokenizers", encode_ms=2.0)],
                "on": [fake_run(["a", "X"], [3, 4], stats=hit)],
            }
        )
        self.assertFalse(report["correctness_pass"])
        self.assertEqual(report["parity"]["on"][0]["request"], 1)
        self.assertEqual(report["parity"]["on"][0]["first_diff"], 2)
        self.assertEqual(report["arms"]["on"]["speed_vs_orig"], 2.0)
        self.assertEqual(sorted(report["arms"]), ["on", "orig"])

    def test_cache_that_never_hits_fails(self):
        for name in ("orig", "on"):
            self.write_ids(name, [[1], [2]])
        report = self.summarize(
            {
                "orig": [fake_run(["a", "b"], [1, 1], backend="tokenizers")],
                "on": [fake_run(["a", "b"], [1, 1], stats={"hits": 0, "misses": 2})],
            }
        )
        self.assertFalse(report["correctness_pass"])
        self.assertTrue(any("命中数为 0" in p for p in report["problems"]))

    def test_candidate_that_is_not_fastokens_fails(self):
        for name in ("orig", "on"):
            self.write_ids(name, [[1], [2]])
        report = self.summarize(
            {
                "orig": [fake_run(["a", "b"], [1, 1], backend="tokenizers")],
                "on": [
                    fake_run(["a", "b"], [1, 1], backend="tokenizers", stats={"hits": 3})
                ],
            }
        )
        self.assertTrue(any("并非 fastokens" in p for p in report["problems"]))

    def test_all_good_passes(self):
        for name in ("orig", "on"):
            self.write_ids(name, [[1], [2]])
        report = self.summarize(
            {
                "orig": [fake_run(["a", "b"], [1, 1], backend="tokenizers")],
                "on": [fake_run(["a", "b"], [1, 1], stats={"hits": 3, "misses": 1})],
            }
        )
        self.assertTrue(report["correctness_pass"], report["problems"])


def verdict_info():
    return {
        "packages": {
            "vllm": {"version": "0.23.0", "module_dir": "/opt/vllm"},
            "fastokens": {"version": None},
        },
        "vllm_features": {"has_fastokens_switch": True},
        "env_current_shell": {},
        "processes": [],
    }


class MinimumVersionTests(unittest.TestCase):
    def test_version_key_reads_three_numeric_parts(self):
        self.assertEqual(tool.version_key("0.4.0rc1"), (0, 4, 0))
        self.assertIsNone(tool.version_key("unknown"))

    def test_target_below_minimum_is_unsupported(self):
        verdict = tool.probe_verdict(verdict_info(), "0.3.4")
        self.assertEqual(verdict["decision"], "不支持")
        self.assertTrue(any("不包含段缓存" in reason for reason in verdict["reasons"]))

    def test_minimum_version_is_accepted(self):
        verdict = tool.probe_verdict(verdict_info(), tool.MIN_VERSION)
        self.assertTrue(verdict["decision"].startswith(f"可切换至 fastokens=={tool.MIN_VERSION}"))

    def test_missing_target_requests_a_wheel(self):
        verdict = tool.probe_verdict(verdict_info(), None)
        self.assertTrue(verdict["decision"].startswith("缺少目标版本"))
        self.assertIn(tool.MIN_VERSION, verdict["decision"])
        self.assertIn("wheel", verdict["decision"])


class ProbeTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("vllm"), "当前 Python 环境中已安装 vLLM")
    def test_probe_without_vllm_says_unsupported(self):
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "probe", "--offline"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("不支持", done.stdout)
        self.assertIn("未找到 vLLM", done.stdout)


E2E_VARS = ("E2E_VLLM_PYTHON", "E2E_MODEL", "E2E_OVERLAY")


@unittest.skipUnless(all(os.environ.get(k) for k in E2E_VARS), "未设置端到端测试所需的环境变量")
class EndToEndTests(unittest.TestCase):
    def test_verify_passes_on_a_real_tokenizer(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            python = os.environ["E2E_VLLM_PYTHON"]
            sessions = tmp / "s.jsonl"
            subprocess.run(
                [
                    python,
                    str(SCRIPT),
                    "make-sessions",
                    "--out",
                    str(sessions),
                    "--sessions",
                    "3",
                    "--turns",
                    "4",
                    "--block-chars",
                    "2000",
                ],
                check=True,
            )
            done = subprocess.run(
                [
                    python,
                    str(SCRIPT),
                    "verify",
                    "--model",
                    os.environ["E2E_MODEL"],
                    "--overlay",
                    os.environ["E2E_OVERLAY"],
                    "--sessions",
                    str(sessions),
                    "--rounds",
                    "1",
                    "--threads",
                    "3",
                    "--cache-mib",
                    "16",
                    "--out",
                    str(tmp / "v"),
                ],
                capture_output=True,
                text=True,
                timeout=1200,
            )
            self.assertEqual(done.returncode, 0, done.stdout[-2000:] + done.stderr[-2000:])
            report = json.loads((tmp / "v" / "report.json").read_text())
            self.assertTrue(report["correctness_pass"])
            self.assertGreater(report["arms"]["on"]["stats"]["hits"], 0)
            self.assertFalse(list((tmp / "v").glob("*.ids")))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
