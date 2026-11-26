"""kptools-ascend-profiling-compare-enqueue 的契约测试（合成 trace）。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-enqueue"
SCRIPT = SKILL / "scripts/enqueue.py"

SPEC = importlib.util.spec_from_file_location("enq_mod", SCRIPT)
enq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(enq)


def ev(name, ts, dur, cat):
    return {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 10}


def write_trace(root, events):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def two_steps(host_dur=100, gap=50, kernel_dur=500):
    events = []
    for s in range(2):
        base = s * 10000.0
        events.append(ev(f"ProfilerStep#{s}", base, 10000, "cpu_op"))
        for k in range(2):
            h_start = base + 1000 + k * 1000
            events.append(ev("Enqueue@aclnnMatmul", h_start, host_dur, "enqueue"))
            events.append(ev("MatMul", h_start + host_dur + gap, kernel_dur, "AI_CORE"))
    return events


def load(path):
    notes, deg = [], []
    return enq.load_capture(path, notes, deg), notes, deg


def mrow(env, name):
    return next(m for m in env["metrics"] if m["name"] == name)


class MainPathTests(unittest.TestCase):
    def test_enqueue_and_latency(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps())
            write_trace(cand, two_steps(host_dur=40, gap=20))
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = enq.compare(b, c, 0.05, 5.0)
            env = enq.envelope(b, c, result, [bn, cn], [bd, cd])

            self.assertEqual(enq.validate(env), [])
            self.assertAlmostEqual(mrow(env, "enqueue_p50_ms")["base"], 0.1)
            self.assertAlmostEqual(mrow(env, "enqueue_p50_ms")["cand"], 0.04)
            self.assertAlmostEqual(mrow(env, "enqueue_to_start_p50_ms")["base"], 0.05)
            self.assertAlmostEqual(mrow(env, "enqueue_to_start_p50_ms")["cand"], 0.02)
            self.assertAlmostEqual(mrow(env, "enqueue_total_per_step_ms")["base"], 0.2)
            self.assertEqual(mrow(env, "enqueue_pair_rate_pct")["base"], 100.0)
            self.assertEqual(mrow(env, "enqueue_p50_ms")["verdict"], "improved")
            self.assertEqual(env["data_quality"]["confidence"], "high")
            self.assertEqual(len(env["per_step"]), 2)

    def test_no_host_api_is_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_trace(root, [ev("MatMul", 1000, 500, "AI_CORE")])
            cap, notes, deg = load(root)
            self.assertTrue(any("host API" in x for x in deg))
            self.assertEqual(cap["hosts"], [])


class DegradeTests(unittest.TestCase):
    def test_no_kernel_zero_pair_rate(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, [ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                               ev("aclnnMatmul", 1000, 100, "CANN_API")])
            write_trace(cand, [ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                               ev("aclnnMatmul", 1000, 100, "CANN_API")])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = enq.compare(b, c, 0.05, 5.0)
            env = enq.envelope(b, c, result, [bn, cn], [bd, cd])
            self.assertEqual(mrow(env, "enqueue_pair_rate_pct")["base"], 0.0)
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(any("kernel" in x for x in env["data_quality"]["degraded"]))

    def test_no_profiler_step_is_low_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            events = [ev("aclnnMatmul", 1000, 100, "CANN_API"),
                      ev("MatMul", 1150, 500, "AI_CORE")]
            write_trace(base, events)
            write_trace(cand, events)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = enq.envelope(b, c, enq.compare(b, c, 0.05, 5.0), [bn, cn], [bd, cd])
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertEqual(len(env["per_step"]), 1)

    def test_unreadable_input_emits_error_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "err.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(d / "nope"), str(d / "none"),
                                  "--out-html", str(out)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 3)
            self.assertIn("下发对比失败", out.read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_cli_json_and_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps())
            write_trace(cand, two_steps(host_dur=50, gap=10))
            out_json, out_html = d / "r.json", d / "r.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(base), str(cand),
                                  "--json", str(out_json), "--out-html", str(out_html)],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(enq.validate(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare-enqueue")
            self.assertIn("下发指标", out_html.read_text(encoding="utf-8"))


class RegressionTests(unittest.TestCase):
    def _ev(self, name, ts, dur, cat=None, args=None):
        e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 1}
        if args:
            e["args"] = args
        return e

    def _write(self, root, events):
        write_trace(root, events)

    def test_fallback_excludes_blocking_sync(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            self._write(root, [
                self._ev("ProfilerStep#0", 0, 100000, "cpu_op"),
                self._ev("aclnnMatmul", 1000, 100, "CANN_API"),
                self._ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 20000, 30000, "cann"),
                self._ev("MatMul", 1200, 500, None, {"Task Type": "AI_CORE"}),
            ])
            cap, notes, deg = load(root)
            self.assertEqual([h["name"] for h in cap["hosts"]], ["aclnnMatmul"])
            self.assertTrue(any("启发式" in x for x in deg))

    def test_fallback_makes_confidence_medium(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            for root in (base, cand):
                self._write(root, [
                    self._ev("ProfilerStep#0", 0, 100000, "cpu_op"),
                    self._ev("aclnnMatmul", 1000, 100, "CANN_API"),
                    self._ev("MatMul", 1200, 500, None, {"Task Type": "AI_CORE"}),
                ])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = enq.envelope(b, c, enq.compare(b, c, 0.05, 5.0), [bn, cn], [bd, cd])
            self.assertEqual(env["data_quality"]["confidence"], "medium")

    def test_noise_floor_for_percentiles_and_overlap(self):
        def build(host_dur):
            events = []
            for s in range(2):
                b = s * 10000.0
                events.append(self._ev(f"ProfilerStep#{s}", b, 10000, "cpu_op"))
                events.append(self._ev("Enqueue@aclnnMatmul", b + 1000, host_dur, "enqueue"))
                # kernel 起点固定，host 结束点的小抖动才能体现在下发→启动延迟上
                events.append(self._ev("MatMul", b + 5000, 500, None,
                                       {"Task Type": "AI_CORE"}))
            return events

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, build(100))
            write_trace(cand, build(100.0001))
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = enq.envelope(b, c, enq.compare(b, c, 0.05, 5.0), [bn, cn], [bd, cd])
            for name in ("enqueue_p50_ms", "enqueue_p95_ms",
                         "enqueue_to_start_p50_ms", "enqueue_to_start_p95_ms",
                         "dispatch_overlap_pct"):
                m = mrow(env, name)
                self.assertGreater(m["noise_band"], 0.0, name)
                self.assertEqual(m["verdict"], "noise", name)

    def test_step_denominator_uses_profiler_steps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            events = [self._ev(f"ProfilerStep#{i}", i * 10000, 10000, "cpu_op") for i in range(4)]
            events += [self._ev("Enqueue@aclnnMatmul", 1000, 100, "enqueue"),
                       self._ev("MatMul", 1200, 500, None, {"Task Type": "AI_CORE"})]
            self._write(root, events)
            cap, _, _ = load(root)
            self.assertEqual(cap["steps"], [0, 1, 2, 3])
            st = enq.stats(cap, *enq.pair_enqueues(cap, 5.0))
            self.assertDictEqual(st["per_step"], {0: 0.1, 1: 0.0, 2: 0.0, 3: 0.0})
            self.assertAlmostEqual(st["enqueue_total_per_step_ms"], 0.025)

    def test_per_step_aligns_by_step_id_when_some_steps_have_no_enqueue(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            def build(skip):
                events = [self._ev(f"ProfilerStep#{i}", i * 10000, 10000, "cpu_op") for i in range(4)]
                for i in range(4):
                    if i == skip:
                        continue
                    events += [self._ev("Enqueue@aclnnMatmul", i * 10000 + 1000, 100, "enqueue"),
                               self._ev("MatMul", i * 10000 + 1200, 500, None,
                                        {"Task Type": "AI_CORE"})]
                return events
            self._write(base, build(skip=1))
            self._write(cand, build(skip=None))
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = enq.compare(b, c, 0.05, 5.0)
            env = enq.envelope(b, c, result, [bn, cn], [bd, cd])
            self.assertEqual([p["step"] for p in result["per_step"]], [0, 1, 2, 3])
            self.assertEqual([p["cand_step"] for p in result["per_step"]], [0, 1, 2, 3])
            self.assertAlmostEqual(result["base_stats"]["enqueue_total_per_step_ms"], 0.075)

    def test_notes_are_tagged_per_side(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps())
            write_trace(cand, two_steps())
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = enq.envelope(b, c, enq.compare(b, c, 0.05, 5.0), [bn, cn], [bd, cd])
            self.assertTrue(any(n.startswith("base: host API") for n in env["notes"]))
            self.assertTrue(any(n.startswith("cand: host API") for n in env["notes"]))

    def test_low_pair_rate_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            events = [self._ev("ProfilerStep#0", 0, 100000, "cpu_op"),
                      self._ev("Enqueue@aclnnMatmul", 1000, 100, "enqueue"),
                      self._ev("MatMul", 90000, 500, None, {"Task Type": "AI_CORE"})]
            for root in (base, cand):
                self._write(root, events)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = enq.envelope(b, c, enq.compare(b, c, 0.05, 5.0), [bn, cn], [bd, cd])
            self.assertTrue(any("配对率" in n for n in env["notes"]))
            self.assertEqual(env["data_quality"]["confidence"], "low")


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
    """真实 CANN trace：cat=enqueue + 设备任务在 args["Task Type"]。"""

    def test_enqueue_cat_and_task_type(self):
        def eva(name, ts, dur, cat=None, args=None):
            e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 10}
            if args:
                e["args"] = args
            return e

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, [
                eva("ProfilerStep#0", 0, 10000),
                eva("Enqueue@acl_memcpy_host_to_device", 1000, 100, "enqueue", {"correlation_id": 7}),
                eva("Enqueue@aclnnMatmul", 1200, 100, "enqueue", {"correlation_id": 8}),
                eva("MatMul", 1150, 500, None, {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
                eva("MatMul", 1350, 500, None, {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
            ])
            cap, notes, deg = load(root)
            self.assertEqual(len(cap["hosts"]), 2)
            result = enq.compare(cap, cap, 0.05, 5.0)
            env = enq.envelope(cap, cap, result, [notes, notes], [deg, deg])
            self.assertAlmostEqual(mrow(env, "enqueue_pair_rate_pct")["base"], 100.0)
            self.assertAlmostEqual(mrow(env, "enqueue_total_per_step_ms")["base"], 0.2)

    def test_real_profiler_step_ids_not_positional(self):
        # 真实 trace 常从 ProfilerStep#1 开始；step 全集必须是真实编号，不能是 0..N-1。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            events = []
            for sid in (1, 2):
                base = (sid - 1) * 10000.0
                events.append(ev(f"ProfilerStep#{sid}", base, 10000, "cpu_op"))
                events.append(ev("Enqueue@aclnnMatmul", base + 1000, 100, "enqueue"))
                events.append(ev("MatMul", base + 1200, 500, "AI_CORE"))
            write_trace(root, events)
            cap, _notes, _deg = load(root)
            self.assertEqual(cap["steps"], [1, 2])

    def test_output_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps())
            write_trace(cand, two_steps(host_dur=40))
            sentinel = d / "sentinel.md"
            sentinel.write_text("keep", encoding="utf-8")
            link = d / "out.html"
            link.symlink_to(sentinel)
            run = subprocess.run(
                [sys.executable, str(SCRIPT), str(base), str(cand),
                 "--out-html", str(link), "--overwrite"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_existing_output_requires_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, two_steps())
            write_trace(cand, two_steps(host_dur=40))
            out = d / "out.html"
            out.write_text("old", encoding="utf-8")
            run = subprocess.run(
                [sys.executable, str(SCRIPT), str(base), str(cand), "--out-html", str(out)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(out.read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
