"""kptools-ascend-profiling-compare-dequeue 的契约测试（合成 trace）。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-dequeue"
SCRIPT = SKILL / "scripts/dequeue.py"

SPEC = importlib.util.spec_from_file_location("deq_mod", SCRIPT)
deq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deq)


def ev(name, ts, dur, cat):
    return {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 10}


def write_trace(root, events):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def two_steps(wait=200, host_dur=100, kernel_dur=500):
    events = []
    for s in range(2):
        base = s * 10000.0
        events.append(ev(f"ProfilerStep#{s}", base, 10000, "cpu_op"))
        for k in range(2):
            h_start = base + 1000 + k * 1000
            events.append(ev("aclnnMatmul", h_start, host_dur, "CANN_API"))
            events.append(ev("MatMul", h_start + host_dur + wait, kernel_dur, "AI_CORE"))
    return events


def load(path):
    notes, deg = [], []
    return deq.load_capture(path, notes, deg), notes, deg


def mrow(env, name):
    return next(m for m in env["metrics"] if m["name"] == name)


class MainPathTests(unittest.TestCase):
    def test_wait_distribution(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps(wait=200))
            write_trace(cand, two_steps(wait=100))
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = deq.compare(b, c, 0.05, 5.0)
            env = deq.envelope(b, c, result, [bn, cn], [bd, cd])

            self.assertEqual(deq.validate(env), [])
            self.assertAlmostEqual(mrow(env, "dequeue_p50_ms")["base"], 0.2)
            self.assertAlmostEqual(mrow(env, "dequeue_p50_ms")["cand"], 0.1)
            self.assertAlmostEqual(mrow(env, "dequeue_total_per_step_ms")["base"], 0.4)
            self.assertEqual(mrow(env, "dequeue_p50_ms")["verdict"], "improved")
            self.assertEqual(mrow(env, "dequeue_pair_rate_pct")["base"], 100.0)
            # 等待 400us vs 设备忙碌 1000us → 28.57%
            self.assertAlmostEqual(mrow(env, "dequeue_queue_wait_share_pct")["base"],
                                   0.4 / 1.4 * 100, places=1)
            # 样例配对样本 < 30，按新口径最高 medium（高置信需足够配对样本）。
            self.assertEqual(env["data_quality"]["confidence"], "medium")

    def test_no_wait_zero(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps(wait=0))
            write_trace(cand, two_steps(wait=0))
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = deq.envelope(b, c, deq.compare(b, c, 0.05, 5.0), [bn, cn], [bd, cd])
            self.assertEqual(mrow(env, "dequeue_p50_ms")["base"], 0.0)
            self.assertEqual(mrow(env, "dequeue_p50_ms")["verdict"], "noise")


class DegradeTests(unittest.TestCase):
    def test_no_kernel_low_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            events = [ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                      ev("aclnnMatmul", 1000, 100, "CANN_API")]
            write_trace(base, events)
            write_trace(cand, events)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = deq.envelope(b, c, deq.compare(b, c, 0.05, 5.0), [bn, cn], [bd, cd])
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(any("kernel" in x for x in env["data_quality"]["degraded"]))

    def test_unreadable_input(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "err.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(d / "nope"), str(d / "none"),
                                  "--out-html", str(out)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 3)
            self.assertIn("Dequeue 对比失败", out.read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_cli_json_and_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps(wait=200))
            write_trace(cand, two_steps(wait=120))
            out_json, out_html = d / "r.json", d / "r.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(base), str(cand),
                                  "--json", str(out_json), "--out-html", str(out_html)],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(deq.validate(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare-dequeue")
            self.assertIn("排队等待指标", out_html.read_text(encoding="utf-8"))


class ComparabilityTests(unittest.TestCase):
    def test_mixed_pairing_mode_flagged_low(self):
        def eva(name, ts, dur, cat, args=None):
            e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 10}
            if args:
                e["args"] = args
            return e

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            exact, heur = d / "exact", d / "heur"
            write_trace(exact, [
                eva("ProfilerStep#0", 0, 10000, None),
                eva("Enqueue@a", 1000, 100, "enqueue", {"correlation_id": 1}),
                eva("Dequeue@a", 1250, 10, "dequeue", {"correlation_id": 1}),
                eva("MatMul", 1500, 500, None, {"Task Type": "AI_CORE"}),
            ])
            write_trace(heur, [
                eva("ProfilerStep#0", 0, 10000, None),
                eva("Enqueue@a", 1000, 100, "enqueue", {"correlation_id": 1}),
                eva("MatMul", 1300, 500, None, {"Task Type": "AI_CORE"}),
            ])
            e, en, ed = load(exact)
            h, hn, hd = load(heur)
            result = deq.compare(e, h, 0.05, 5.0)
            env = deq.envelope(e, h, result, [en, hn], [ed, hd])
            self.assertEqual(result["base_mode"], "exact")
            self.assertEqual(result["cand_mode"], "heuristic")
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(any("配对口径不一致" in x for x in env["data_quality"]["degraded"]))
            self.assertTrue(any("等待口径" in n for n in env["notes"]))
            self.assertEqual(deq.validate(env), [])
            # 口径不一致时总量/占比也不可比，必须 info，不能把缺失侧按 0 判改善。
            for name in ("dequeue_total_per_step_ms", "dequeue_queue_wait_share_pct"):
                self.assertEqual(mrow(env, name)["verdict"], "info")

    def test_step_count_mismatch_note(self):
        one_step = [
            ev("ProfilerStep#0", 0, 10000, "cpu_op"),
            ev("aclnnMatmul", 1000, 100, "CANN_API"),
            ev("MatMul", 1300, 500, "AI_CORE"),
        ]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "b", d / "c"
            write_trace(base, two_steps())
            write_trace(cand, one_step)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = deq.compare(b, c, 0.05, 5.0)
            env = deq.envelope(b, c, result, [bn, cn], [bd, cd])
            self.assertIsNotNone(result["step_note"])
            self.assertTrue(any("step 数不一致" in n for n in env["notes"]))
            self.assertIn("step id 交集", result["step_note"])
            self.assertEqual(deq.validate(env), [])


class ValidatorTests(unittest.TestCase):
    def _env(self):
        return {"skill": "x", "schema_version": 1, "mode": "pair", "inputs": {},
                "data_quality": {"sidecars": [], "degraded": [], "confidence": "high"},
                "metrics": [], "per_step": [], "summary": "s", "notes": []}

    def test_empty_metrics_rejected(self):
        self.assertTrue(deq.validate(self._env()))

    def test_bad_schema_version_rejected(self):
        env = self._env()
        env["metrics"] = [{"name": "a", "unit": "ms", "base": 1, "cand": 1,
                           "delta": 0, "delta_pct": 0, "verdict": "noise",
                           "noise_band": 0, "evidence": "e"}]
        env["schema_version"] = 2
        self.assertTrue(deq.validate(env))


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
    """真实 CANN trace：enqueue/dequeue + correlation_id 精确配对。"""

    def test_correlation_pairing(self):
        def eva(name, ts, dur, cat, args=None):
            e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 10}
            if args:
                e["args"] = args
            return e

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, [
                eva("ProfilerStep#0", 0, 10000, None),
                eva("Enqueue@a", 1000, 100, "enqueue", {"correlation_id": 7}),
                eva("Dequeue@a", 1250, 10, "dequeue", {"correlation_id": 7}),
                eva("Enqueue@b", 2000, 100, "enqueue", {"correlation_id": 8}),
                eva("Dequeue@b", 2100, 10, "dequeue", {"correlation_id": 8}),
                eva("MatMul", 1300, 500, None, {"Task Type": "AI_CORE"}),
            ])
            cap, notes, deg = load(root)
            result = deq.compare(cap, cap, 0.05, 5.0)
            env = deq.envelope(cap, cap, result, [notes, notes], [deg, deg])
            self.assertAlmostEqual(mrow(env, "dequeue_pair_rate_pct")["base"], 100.0)
            self.assertAlmostEqual(mrow(env, "dequeue_p50_ms")["base"], 0.075, places=4)
            self.assertAlmostEqual(mrow(env, "dequeue_total_per_step_ms")["base"], 0.15, places=4)

    def test_copy_kernel_types_have_source(self):
        self.assertIn("SDMA_SQE", deq.COPY_KERNEL_TYPES)
        self.assertNotIn("WRITE_VALUE_SQE", deq.COPY_KERNEL_TYPES)
        self.assertNotIn("MEM_WRITE_VALUE", deq.COPY_KERNEL_TYPES)


if __name__ == "__main__":
    unittest.main()
