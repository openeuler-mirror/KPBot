"""kptools-ascend-profiling-compare-free 的契约测试（合成时间轴）。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-free"
SCRIPT = SKILL / "scripts/free_breakdown.py"

SPEC = importlib.util.spec_from_file_location("free_mod", SCRIPT)
fbm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fbm)


def ev(name, ts, dur, cat, tid=10):
    return {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": tid}


def write_trace(root, events):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def mixed_trace(host_busy_dur=1000):
    evs = [ev("ProfilerStep#0", 0, 10000, "cpu_op")]
    for a, b, name, cat, tid in (
        (0, 5000, "MatMul", "AI_CORE", 100),
        (6000, 500, "MatMul", "AI_CORE", 100),
        (7000, 500, "MatMul", "AI_CORE", 100),
        (8000, 500, "MatMul", "AI_CORE", 100),
        (9200, 800, "hcom_allReduce_", "AI_CORE", 101),
    ):
        evs.append(ev(name, a, b, cat, tid))
    evs.append(ev("aten::mm", 5000, host_busy_dur, "python_function"))
    evs.append(ev("aclrtSynchronizeDevice", 6500, 500, "cann"))
    evs.append(ev("aclnnMatmul", 7500, 50, "enqueue"))
    return evs


def load(path):
    notes, deg = [], []
    return fbm.load_capture(path, notes, deg), notes, deg


def mrow(env, name):
    return next(m for m in env["metrics"] if m["name"] == name)


class ClassificationTests(unittest.TestCase):
    def test_mixed_classes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_trace(root, mixed_trace())
            cap, notes, deg = load(root)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertEqual(fbm.validate(env), [])
            self.assertEqual(env["mode"], "single")
            self.assertAlmostEqual(mrow(env, "free_total_ms")["base"], 2.7)
            self.assertAlmostEqual(mrow(env, "free_host_busy_ms")["base"], 1.0)
            self.assertAlmostEqual(mrow(env, "free_sync_wait_ms")["base"], 0.5)
            self.assertAlmostEqual(mrow(env, "free_dispatch_gap_ms")["base"], 0.5)
            self.assertAlmostEqual(mrow(env, "free_comm_align_ms")["base"], 0.7)
            self.assertAlmostEqual(mrow(env, "free_unknown_ms")["base"], 0.0)
            self.assertEqual(env["data_quality"]["confidence"], "high")

    def test_memcpy_gap(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            evs = [ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                   ev("MatMul", 0, 5000, "AI_CORE"),
                   ev("MatMul", 6000, 4000, "AI_CORE"),
                   ev("Memcpy H2D", 6000, 100, "Memcpy")]
            write_trace(root, evs)
            cap, notes, deg = load(root)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertAlmostEqual(mrow(env, "free_memcpy_gap_ms")["base"], 1.0)

    def test_unknown_warning(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_trace(root, [ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                               ev("MatMul", 0, 7000, "AI_CORE")])
            cap, notes, deg = load(root)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertGreater(mrow(env, "free_unknown_pct")["base"], 30)
            self.assertTrue(any("unknown 占比" in x for x in env["data_quality"]["degraded"]))

    def test_pair_compare(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, mixed_trace(host_busy_dur=1000))
            write_trace(cand, mixed_trace(host_busy_dur=300))
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fbm.compare(b, c, 0.05)
            env = fbm.envelope(b, c, result, [bn, cn], [bd, cd])
            self.assertEqual(env["mode"], "pair")
            self.assertAlmostEqual(mrow(env, "free_host_busy_ms")["base"], 1.0)
            # 覆盖不足 50% 时整个窗口归 unknown（窗口级归因，不做部分切分）
            self.assertAlmostEqual(mrow(env, "free_host_busy_ms")["cand"], 0.0)
            self.assertAlmostEqual(mrow(env, "free_unknown_ms")["cand"], 1.0)
            self.assertEqual(len(env["per_step"]), 1)

    def test_single_mode_has_no_cand(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_trace(root, mixed_trace())
            cap, notes, deg = load(root)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertEqual(env["mode"], "single")
            self.assertEqual(fbm.validate(env), [])
            for m in env["metrics"]:
                self.assertIsNone(m["cand"])
                self.assertIsNone(m["delta"])
                self.assertIsNone(m["delta_pct"])
                self.assertEqual(m["verdict"], "info")
            row = env["per_step"][0]
            self.assertIsNone(row["cand"])
            self.assertIsNone(row["delta"])
            self.assertNotIn("cand_step", row)
            self.assertIn("Free 2.700ms", env["summary"])


class ReviewFixTests(unittest.TestCase):
    def test_communication_category_is_device_busy_and_adjacent(self):
        """cat=communication（非 hcom、无 Task Type）也是设备忙碌，参与相邻归因。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, [
                ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                ev("MatMul", 0, 1000, "AI_CORE"),
                ev("Collective", 3000, 1000, "communication"),
            ])
            cap, notes, deg = load(root)
            self.assertEqual(len(cap["kernels"]), 2)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertAlmostEqual(mrow(env, "free_total_ms")["base"], 8.0)
            self.assertAlmostEqual(mrow(env, "free_comm_align_ms")["base"], 8.0)
            self.assertAlmostEqual(mrow(env, "free_unknown_ms")["base"], 0.0)

    def test_whole_window_covers_host_preparation_without_step(self):
        """无 ProfilerStep 时整窗覆盖 host 准备阶段，而不是只分析首末 kernel 之间。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, [
                ev("aten::mm", 0, 2000, "python_function"),
                ev("MatMul", 2000, 1000, "AI_CORE"),
            ])
            cap, notes, deg = load(root)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertAlmostEqual(mrow(env, "free_total_ms")["base"], 2.0)
            self.assertAlmostEqual(mrow(env, "free_host_busy_ms")["base"], 2.0)

    def test_per_step_carries_classification_composition(self):
        """per_step 必须带每步子类构成，而不只是 total。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, mixed_trace())
            cap, notes, deg = load(root)
            result = fbm.compare(cap, None, 0.05)
            row = result["per_step"][0]
            for c in fbm.CLASSES:
                self.assertIn(f"base_{c}", row)
            self.assertGreater(row["base_host_busy"], 0.0)

    def test_interval_index_cover_matches_bruteforce(self):
        ivs = [(0, 5), (3, 9), (20, 25)]
        idx = fbm.IntervalIndex(ivs)
        for a, b in ((0, 10), (4, 22), (6, 8), (9, 20), (-5, 50)):
            self.assertAlmostEqual(idx.cover(a, b), fbm.cover(ivs, a, b))
        self.assertTrue(idx.any_overlap(4, 6))
        self.assertFalse(idx.any_overlap(10, 19))


class ValidatorTests(unittest.TestCase):
    def _env(self):
        return {"skill": "x", "schema_version": 1, "mode": "single", "inputs": {},
                "data_quality": {"sidecars": [], "degraded": [], "confidence": "high"},
                "metrics": [], "per_step": [], "summary": "s", "notes": []}

    def test_empty_metrics_rejected(self):
        self.assertTrue(fbm.validate(self._env()))

    def test_bad_schema_version_rejected(self):
        env = self._env()
        env["metrics"] = [{"name": "free_total_ms", "unit": "ms", "base": 1, "cand": None,
                           "delta": None, "delta_pct": None, "verdict": "info",
                           "noise_band": 0.0, "evidence": "e"}]
        env["schema_version"] = 2
        self.assertTrue(fbm.validate(env))


class DegradeTests(unittest.TestCase):
    def test_no_step_is_low_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_trace(root, [ev("MatMul", 0, 1000, "AI_CORE")])
            cap, notes, deg = load(root)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertEqual(len(env["per_step"]), 1)

    def test_unreadable_input(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "err.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(d / "nope"),
                                  "--out-html", str(out)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 3)
            self.assertIn("Free 分析失败", out.read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_cli_json_and_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, mixed_trace())
            write_trace(cand, mixed_trace(host_busy_dur=600))
            out_json, out_html = d / "r.json", d / "r.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(base), str(cand),
                                  "--json", str(out_json), "--out-html", str(out_html)],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(fbm.validate(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare-free")
            self.assertIn("Free 构成对比", out_html.read_text(encoding="utf-8"))


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_and_evals(self):
        import re

        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm)
        self.assertIn(f"name: {SKILL.name}", fm.group(1))
        evals = json.loads((SKILL / "evals/eval_queries.json").read_text(encoding="utf-8"))
        qs = evals["queries"]
        self.assertGreaterEqual(len(qs), 10)
        self.assertGreaterEqual(sum(1 for q in qs if q["should_trigger"]), 6)
        self.assertGreaterEqual(sum(1 for q in qs if not q["should_trigger"]), 4)
        self.assertTrue((SKILL / "assets/report.css").is_file())


class RealSchemaTests(unittest.TestCase):
    """真实 CANN trace：Task Type、cat=enqueue、Event::synchronize。"""

    def test_real_schema_classes(self):
        def eva(name, ts, dur, cat=None, args=None):
            e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 10}
            if args:
                e["args"] = args
            return e

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, [
                eva("ProfilerStep#0", 0, 10000),
                eva("MatMul", 0, 5000, None, {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
                eva("MatMul", 6000, 3000, None, {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
                eva("aten::mm", 5000, 1000, "python_function"),
                eva("Enqueue@acl_memcpy_host_to_device", 9450, 50, "enqueue"),
            ])
            cap, notes, deg = load(root)
            self.assertGreater(len(cap["kernels"]), 0)
            result = fbm.compare(cap, None, 0.05)
            env = fbm.envelope(cap, None, result, [notes, []], [deg, []])
            self.assertAlmostEqual(mrow(env, "free_host_busy_ms")["base"], 1.0)
            self.assertAlmostEqual(mrow(env, "free_dispatch_gap_ms")["base"], 1.0)
            self.assertLess(mrow(env, "free_unknown_pct")["base"], 30)


if __name__ == "__main__":
    unittest.main()
