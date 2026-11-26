"""kptools-ascend-profiling-compare-stack 的契约测试（合成小 trace，真实调用 build_index）。"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-stack"
SCRIPT = SKILL_DIR / "scripts/stack_diff.py"
SPEC = importlib.util.spec_from_file_location("kps_stack", SCRIPT)
sd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sd)

_CACHE_TMP = tempfile.TemporaryDirectory()
sd.CACHE_DIR = Path(_CACHE_TMP.name)


def tearDownModule():
    _CACHE_TMP.cleanup()


def _env():
    return {**os.environ, "KPBOT_STACK_CACHE_DIR": str(sd.CACHE_DIR)}


def _e(name, pid, tid, ts_us, dur_us, cat, args=None):
    e = {"ph": "X", "name": name, "pid": pid, "tid": tid,
         "ts": str(ts_us), "dur": dur_us, "cat": cat}
    if args:
        e["args"] = args
    return e


def _device(ts_us=50_000, dur_us=10_000):
    return _e("matmul", 200, 1, ts_us, dur_us, "AI_CORE")


def _write(root, events):
    root.mkdir(parents=True, exist_ok=True)
    (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def _profile_trace(root):
    """base：root→(a→leaf, b, c(345))。self: leaf 10 / a 30 / b 30 / c 30 / root 0 = 100ms。"""
    _write(root, [
        _e("ProfilerStep#0", 100, 100, 0, 100_000, "cpu_op"),
        _e("root.py(1): forward", 100, 100, 0, 100_000, "python_function"),
        _e("a.py(2): compute", 100, 100, 0, 40_000, "python_function"),
        _e("leaf.py(3): kernel", 100, 100, 10_000, 10_000, "python_function"),
        _e("b.py(4): gone", 100, 100, 40_000, 30_000, "python_function"),
        _e("c.py(345): run", 100, 100, 70_000, 30_000, "python_function"),
        _device(),
    ])


def _cand_trace(root):
    """cand：root→(a→leaf, new, c(340))。self: leaf 10 / a 25 / new 20 / c 30 / root 5 = 90ms。"""
    _write(root, [
        _e("ProfilerStep#0", 100, 100, 0, 90_000, "cpu_op"),
        _e("root.py(1): forward", 100, 100, 0, 90_000, "python_function"),
        _e("a.py(2): compute", 100, 100, 0, 35_000, "python_function"),
        _e("leaf.py(3): kernel", 100, 100, 10_000, 10_000, "python_function"),
        _e("new.py(7): added", 100, 100, 40_000, 20_000, "python_function"),
        _e("c.py(340): run", 100, 100, 60_000, 30_000, "python_function"),
        _device(),
    ])


def _nostack_trace(root):
    _write(root, [
        _e("ProfilerStep#0", 100, 100, 0, 100_000, "cpu_op"),
        _device(),
    ])


class DiffTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        _profile_trace(self.d / "base")
        _cand_trace(self.d / "cand")

    def tearDown(self):
        self._td.cleanup()

    def test_diff_known_synthetic_stack(self):
        result = sd.run(self.d / "base", self.d / "cand")
        self.assertTrue(result["comparable"])
        self.assertEqual(sd.validate(sd.public_result(result)), [])
        m = {x["name"]: x for x in result["metrics"]}

        self.assertEqual(m["stack_func_count"]["base"], 5)
        self.assertEqual(m["stack_func_count"]["cand"], 5)
        self.assertAlmostEqual(m["stack_total_self_ms"]["base"], 100.0, places=3)
        self.assertAlmostEqual(m["stack_total_self_ms"]["cand"], 90.0, places=3)
        self.assertAlmostEqual(m["stack_total_self_ms"]["delta"], 10.0, places=3)
        self.assertEqual(m["stack_new_funcs"]["cand"], 1)
        self.assertEqual(m["stack_gone_funcs"]["cand"], 1)
        self.assertEqual(m["stack_changed_funcs"]["cand"], 2)
        self.assertIn("b.py", m["stack_top_self_ms"]["evidence"])
        self.assertAlmostEqual(m["stack_top_self_ms"]["base"], 30.0, places=3)
        self.assertAlmostEqual(m["stack_top_self_ms"]["cand"], 0.0, places=3)

        details = result["_details"]
        self.assertEqual([r["display"] for r in details["new"]], ["new.py(7): added"])
        self.assertEqual([r["display"] for r in details["gone"]], ["b.py(4): gone"])
        self.assertEqual([r["display"] for r in details["increased"]],
                         ["root.py(1): forward"])
        self.assertEqual([r["display"] for r in details["decreased"]],
                         ["a.py(2): compute"])

        self.assertEqual(result["per_step"][0]["base"], 100.0)
        self.assertEqual(result["per_step"][0]["cand"], 90.0)
        self.assertEqual(result["data_quality"]["confidence"], "medium")

    def test_line_number_drift_is_merged(self):
        result = sd.run(self.d / "base", self.d / "cand")
        cand_names = set(result["_cand"]["functions"])
        self.assertIn("c.py(*): run", cand_names)
        self.assertNotIn("c.py(340): run", cand_names)
        m = {x["name"]: x for x in result["metrics"]}
        self.assertEqual(m["stack_gone_funcs"]["cand"], 1)  # 只有 b.py
        self.assertEqual(m["stack_new_funcs"]["cand"], 1)   # 只有 new.py

    def test_strict_line_splits_drift(self):
        result = sd.run(self.d / "base", self.d / "cand", strict=True)
        m = {x["name"]: x for x in result["metrics"]}
        self.assertEqual(m["stack_new_funcs"]["cand"], 2)   # new.py + c.py(340)
        self.assertEqual(m["stack_gone_funcs"]["cand"], 2)  # b.py + c.py(345)
        self.assertIn("--strict-line", " ".join(result["notes"]))

    def test_html_contains_required_sections(self):
        out = self.d / "r.html"
        run = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.d / "base"), str(self.d / "cand"),
             "--out-html", str(out), "--json", str(self.d / "r.json")],
            capture_output=True, text=True, timeout=120, env=_env(),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        html = out.read_text(encoding="utf-8")
        for needle in ("新增 / 消失函数", "self 时间变化", "关键调用路径变化示例",
                       "归一化/折叠/self 口径", "c.py(345): run", "c.py(340): run"):
            self.assertIn(needle, html)

    def test_deterministic(self):
        a = sd.run(self.d / "base", self.d / "cand")
        b = sd.run(self.d / "base", self.d / "cand")
        self.assertEqual(sd.public_result(a), sd.public_result(b))

    def test_top_self_noise_is_ms_and_verdict(self):
        """变化最大函数逐 step self 有波动时，噪声带须为 ms 量级且 verdict 不能恒为 noise。"""

        def two_step(root, selfs):
            _write(root, [
                _e("matmul", 200, 1, 10_000, 5_000, "AI_CORE"),
                _e("ProfilerStep#0", 100, 1, 0, 200_000, "cpu_op"),
                _e("hot.py(1): run", 100, 1, 0, selfs[0], "python_function"),
                _e("ProfilerStep#1", 100, 1, 200_000, 200_000, "cpu_op"),
                _e("hot.py(1): run", 100, 1, 200_000, selfs[1], "python_function"),
            ])

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            two_step(d / "base", (10_000, 20_000))  # 10ms / 20ms（MAD 5ms）
            two_step(d / "cand", (5_000, 5_000))    # 5ms / 5ms
            result = sd.run(d / "base", d / "cand")
            self.assertTrue(result["comparable"])
            m = {x["name"]: x for x in result["metrics"]}
            top = m["stack_top_self_ms"]
            self.assertAlmostEqual(top["delta"], 20.0, places=3)
            self.assertAlmostEqual(top["noise_band"], 5.0, places=3)
            self.assertLess(top["noise_band"], 100.0)  # ms 量级，未误用 ns
            self.assertEqual(top["verdict"], "improved")


class StepSplitAndRankingTests(unittest.TestCase):
    def test_cross_step_self_is_split_by_intersection(self):
        """独占区间跨 step 边界时按交集分摊，而不是整体归到起始步。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root / "base", [
                _device(60_000, 10_000),
                _e("ProfilerStep#0", 100, 1, 0, 10_000, "cpu_op"),
                _e("ProfilerStep#1", 100, 1, 10_000, 10_000, "cpu_op"),
                _e("span.py(1): f", 100, 1, 8_000, 4_000, "python_function"),
            ])
            _write(root / "cand", [
                _device(60_000, 10_000),
                _e("ProfilerStep#0", 100, 1, 0, 10_000, "cpu_op"),
                _e("ProfilerStep#1", 100, 1, 10_000, 10_000, "cpu_op"),
                _e("span.py(1): f", 100, 1, 8_000, 4_000, "python_function"),
            ])
            result = sd.run(root / "base", root / "cand")
            self.assertEqual(len(result["per_step"]), 2)
            self.assertAlmostEqual(result["per_step"][0]["base"], 2.0, places=4)
            self.assertAlmostEqual(result["per_step"][1]["base"], 2.0, places=4)
            self.assertAlmostEqual(
                result["_base"]["functions"]["span.py(*): f"]["self"] / 1e6, 4.0, places=4
            )

    def test_top_excludes_unresolved_from_ranking(self):
        """unresolved 混合组保留计数/耗时，但不参与「变化最大函数」排名。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def trace(path, unknown_us):
                _write(path, [
                    _device(70_000, 10_000),
                    _e("ProfilerStep#0", 100, 1, 0, 200_000, "cpu_op"),
                    _e("weird/name", 100, 1, 0, unknown_us, "python_function"),
                    _e("real.py(1): f", 100, 1, 100_000, 10_000, "python_function"),
                ])

            trace(root / "base", 100_000)
            trace(root / "cand", 1_000)
            result = sd.run(root / "base", root / "cand")
            m = {x["name"]: x for x in result["metrics"]}
            evidence = m["stack_top_self_ms"]["evidence"]
            self.assertIn("real.py", evidence)
            self.assertNotIn("unresolved", evidence)
            self.assertIn("unresolved", result["_base"]["functions"])

    def test_top_verdict_uses_paired_steps(self):
        """两侧采样步数不同时，单函数判定用配对样本，不因全量差异误判 regressed。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def trace(path, steps):
                events = [_device(step * 200_000, 10_000) for step in range(steps)]
                for step in range(steps):
                    events.append(_e(f"ProfilerStep#{step}", 100, 1, step * 200_000, 100_000, "cpu_op"))
                    events.append(_e("hot.py(1): run", 100, 1, step * 200_000, 1_000, "python_function"))
                _write(path, events)

            trace(root / "base", 1)
            trace(root / "cand", 4)
            result = sd.run(root / "base", root / "cand")
            m = {x["name"]: x for x in result["metrics"]}
            self.assertEqual(m["stack_top_self_ms"]["verdict"], "noise")
            self.assertEqual(m["stack_total_self_ms"]["verdict"], "noise")


class DegradeTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        _profile_trace(self.d / "base")
        _cand_trace(self.d / "cand")
        _nostack_trace(self.d / "nostack")

    def tearDown(self):
        self._td.cleanup()

    def test_one_side_without_stack_is_incomparable(self):
        result = sd.run(self.d / "base", self.d / "nostack")
        self.assertFalse(result["comparable"])
        self.assertIn("无法比较调用栈", result["summary"])
        self.assertEqual(result["data_quality"]["confidence"], "low")
        self.assertTrue(any("python_function" in x for x in result["data_quality"]["degraded"]))
        m = {x["name"]: x for x in result["metrics"]}
        self.assertEqual(m["stack_func_count"]["base"], 5)
        self.assertIsNone(m["stack_func_count"]["cand"])
        self.assertIsNone(m["stack_func_count"]["delta"])
        out = self.d / "n.html"
        run = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.d / "base"), str(self.d / "nostack"),
             "--out-html", str(out), "--json", str(self.d / "n.json")],
            capture_output=True, text=True, timeout=120, env=_env(),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("不可比原因", out.read_text(encoding="utf-8"))

    def test_build_index_missing_degrades(self):
        with unittest.mock.patch.object(sd, "find_build_index", lambda *a, **k: None):
            profile = sd.load_profile(self.d / "base")
        self.assertEqual(profile["status"], "no_index")
        self.assertIn("缺少", profile["reason"])
        self.assertTrue(profile["degraded"])
        self.assertEqual(profile["confidence"], "low")

    def test_invalid_input_returns_3(self):
        run = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.d / "nope"), str(self.d / "cand"),
             "--out-html", str(self.d / "e.html"), "--json", str(self.d / "e.json")],
            capture_output=True, text=True, timeout=60, env=_env(),
        )
        self.assertEqual(run.returncode, 3)
        self.assertIn("调用栈 diff 失败", (self.d / "e.html").read_text(encoding="utf-8"))
        env = json.loads((self.d / "e.json").read_text(encoding="utf-8"))
        self.assertEqual(env["data_quality"]["confidence"], "low")
        self.assertIn("失败", env["summary"])


class LookupAndContractTests(unittest.TestCase):
    def test_find_build_index_both_layouts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            layouts = (
                "kptools-ascend-profiling-analysis/scripts/build_index.py",
                "ascend_kunpeng/kptools-ascend-profiling-analysis/scripts/build_index.py",
                "a/b/kptools-ascend-profiling-analysis/scripts/build_index.py",
            )
            for i, rel in enumerate(layouts):
                skills = root / f"s{i}"
                target = skills / rel
                target.parent.mkdir(parents=True)
                target.write_text("", encoding="utf-8")
                self.assertEqual(sd.find_build_index(skills), target)

            empty = root / "empty_skills"
            empty.mkdir()
            self.assertIsNone(sd.find_build_index(empty))

    def test_repo_layout_is_found(self):
        found = sd.find_build_index()
        self.assertIsNotNone(found)
        self.assertTrue(found.is_file())
        self.assertEqual(found.name, "build_index.py")

    def test_name_normalization_rules(self):
        self.assertEqual(sd.normalize_name("worker_base.py(345): run"),
                         "worker_base.py(*): run")
        self.assertEqual(sd.normalize_name("/a/b/c.py(12): f"), "c.py(*): f")
        self.assertEqual(sd.normalize_name("vllm/worker/c.py(12): f"), "c.py(*): f")
        self.assertEqual(sd.normalize_name("foo (/a/b.py:12)"), "foo")
        self.assertEqual(sd.normalize_name("keep.py(9): f", strict=True),
                         "keep.py(9): f")

    def test_a1_contract(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _profile_trace(d / "base")
            _cand_trace(d / "cand")
            result = sd.run(d / "base", d / "cand")
            env = sd.public_result(result)
            self.assertEqual(sd.validate(env), [])
            names = [m["name"] for m in env["metrics"]]
            self.assertEqual(names, [
                "stack_func_count", "stack_total_self_ms", "stack_top_self_ms",
                "stack_new_funcs", "stack_gone_funcs", "stack_changed_funcs",
            ])
            for m in env["metrics"]:
                self.assertTrue(m["evidence"])
            for name in ("stack_new_funcs", "stack_gone_funcs", "stack_changed_funcs"):
                m = next(x for x in env["metrics"] if x["name"] == name)
                self.assertEqual(m["base"], 0)
                self.assertEqual(m["verdict"], "info")
            self.assertEqual(env["mode"], "pair")
            self.assertTrue(any("集合差异" in n for n in env["notes"]))

    def test_skill_integrity(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm, "SKILL.md 缺少 frontmatter")
        name = re.search(r"^name:\s*(\S+)", fm.group(1), re.MULTILINE)
        self.assertIsNotNone(name)
        self.assertEqual(name.group(1), SKILL_DIR.name)
        self.assertIn("kptools-ascend-profiling-analysis", fm.group(1))
        self.assertIn("opstats", fm.group(1))
        links = re.findall(r"\]\((references/[^)]+)\)", text)
        self.assertGreaterEqual(len(links), 1)
        for target in links:
            self.assertTrue((SKILL_DIR / target).is_file(), f"引用不存在: {target}")
        self.assertTrue((SKILL_DIR / "assets/report.css").is_file())

    def test_eval_queries(self):
        data = json.loads((SKILL_DIR / "evals/eval_queries.json").read_text(encoding="utf-8"))
        queries = data["queries"]
        self.assertGreaterEqual(len(queries), 10)
        pos = [q for q in queries if q["should_trigger"]]
        neg = [q for q in queries if not q["should_trigger"]]
        self.assertGreaterEqual(len(pos), 6)
        self.assertGreaterEqual(len(neg), 4)
        for q in neg:
            self.assertTrue(q.get("note"), f"负例缺路由说明: {q['id']}")


if __name__ == "__main__":
    unittest.main()
