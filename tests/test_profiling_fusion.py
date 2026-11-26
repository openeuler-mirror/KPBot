"""kptools-ascend-profiling-compare-fusion 的契约测试（合成 kernel 序列）。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-fusion"
SCRIPT = SKILL / "scripts/fusion_diff.py"

SPEC = importlib.util.spec_from_file_location("fusion_mod", SCRIPT)
fus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fus)


def write_csv(root, rows):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "kernel_details.csv", "w", encoding="utf-8") as f:
        f.write("Step Id,Name,Type,Duration(us),Start Time(us)\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return out


def write_trace(root, events):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
    return out


def ev(name, ts, dur, cat="AI_CORE"):
    return {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 2}


def load(path, fused=()):
    notes, deg = [], []
    return fus.load_capture(path, set(fused), notes, deg), notes, deg


def mrow(env, name):
    return next(m for m in env["metrics"] if m["name"] == name)


class MergeEvidenceTests(unittest.TestCase):
    def test_three_to_one_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "MatMul", "AI_CORE", 100, 0),
                             (0, "BiasAdd", "AI_VECTOR_CORE", 100, 105),
                             (0, "Relu", "AI_VECTOR_CORE", 100, 210)])
            write_csv(cand, [(0, "MatMul_BiasAdd_Relu", "MIX_AIC", 280, 0)])

            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)

            self.assertEqual(fus.validate(env), [])
            self.assertEqual(mrow(env, "fusion_merge_confirmed")["cand"], 1)
            self.assertEqual(mrow(env, "fusion_merge_suspected")["cand"], 0)
            self.assertEqual(mrow(env, "fusion_op_count")["base"], 3)
            self.assertEqual(mrow(env, "fusion_op_count")["cand"], 1)
            self.assertAlmostEqual(mrow(env, "fusion_kernel_ms")["base"], 0.3)
            self.assertAlmostEqual(mrow(env, "fusion_kernel_ms")["cand"], 0.28)
            self.assertAlmostEqual(mrow(env, "fusion_attribution_kernel_ms")["delta"], 0.02)
            self.assertIn("confirmed 1", env["summary"])

    def test_overlap_without_name_relation_is_suspected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "Alpha", "AI_CORE", 100, 0),
                             (0, "Beta", "AI_CORE", 100, 110)])
            write_csv(cand, [(0, "Gamma", "MIX_AIC", 190, 0)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(mrow(env, "fusion_merge_suspected")["cand"], 1)
            self.assertEqual(mrow(env, "fusion_merge_confirmed")["cand"], 0)

    def test_cross_capture_clock_offset_is_aligned(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_trace(base, [
                {"ph": "X", "name": "ProfilerStep#0", "pid": 1, "tid": 1,
                 "ts": 0, "dur": 10000, "cat": "cpu_op"},
                ev("Alpha", 0, 100), ev("Beta", 110, 100),
            ])
            write_trace(cand, [
                {"ph": "X", "name": "ProfilerStep#0", "pid": 1, "tid": 1,
                 "ts": 5000, "dur": 10000, "cat": "cpu_op"},
                ev("Gamma", 5000, 190, "MIX_AIC"),
            ])
            b, bn, bd = load(base, fused={"Gamma"})
            c, cn, cd = load(cand, fused={"Gamma"})
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(mrow(env, "fusion_merge_confirmed")["cand"], 1)

    def test_non_overlapping_is_unproven(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "Alpha", "AI_CORE", 100, 0),
                             (0, "Beta", "AI_CORE", 100, 110)])
            write_csv(cand, [(0, "Gamma", "MIX_AIC", 190, 9000)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(mrow(env, "fusion_merge_unproven")["cand"], 1)
            self.assertIn("无法证明融合", env["summary"])

    def test_fused_ops_file_upgrades_suspected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "Alpha", "AI_CORE", 100, 0),
                             (0, "Beta", "AI_CORE", 100, 110)])
            write_csv(cand, [(0, "Gamma", "MIX_AIC", 190, 0)])
            b, bn, bd = load(base, fused={"Gamma"})
            c, cn, cd = load(cand, fused={"Gamma"})
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(mrow(env, "fusion_merge_confirmed")["cand"], 1)
            self.assertIn("--fused-ops", " ".join(env["notes"]) + " ".join(
                d["reason"] for d in result["details"]))


class NoFusionTests(unittest.TestCase):
    def test_identical_sequences_report_unproven(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            rows = [(0, "MatMul", "AI_CORE", 100, 0), (0, "Add", "AI_VECTOR_CORE", 50, 110)]
            write_csv(base, rows)
            write_csv(cand, rows)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(mrow(env, "fusion_merge_confirmed")["cand"], 0)
            self.assertEqual(mrow(env, "fusion_merge_suspected")["cand"], 0)
            self.assertEqual(mrow(env, "fusion_merge_unproven")["cand"], 0)
            self.assertIn("无法证明融合", env["summary"])

    def test_split_not_counted_as_fusion_gain(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "Fused", "MIX_AIC", 300, 0)])
            write_csv(cand, [(0, "OpA", "AI_CORE", 100, 0),
                             (0, "OpB", "AI_CORE", 100, 110),
                             (0, "OpC", "AI_CORE", 100, 220)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(result["splits"], 1)
            self.assertIn("拆分", env["summary"])
            self.assertTrue(any("拆分" in n for n in env["notes"]))


class DegradeTests(unittest.TestCase):
    def test_trace_only_and_no_step(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_trace(root, [ev("MatMul", 100, 1000), ev("Add", 1200, 500)])
            cap, notes, deg = load(root)
            self.assertEqual(cap["source"], "trace_view.json")
            self.assertTrue(any("ProfilerStep" in x for x in deg))
            env = fus.envelope(cap, cap, fus.compare_captures(cap, cap, 50.0),
                               notes, notes, deg, deg)
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertEqual(len(env["per_step"]), 1)

    def test_unreadable_input_emits_error_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "err.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(d / "nope"), str(d / "none"),
                                  "--out-html", str(out)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 3)
            self.assertIn("融合对比失败", out.read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_cli_json_and_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "MatMul", "AI_CORE", 100, 0),
                             (0, "BiasAdd", "AI_CORE", 100, 110),
                             (0, "Relu", "AI_CORE", 100, 220)])
            write_csv(cand, [(0, "MatMul_BiasAdd_Relu", "MIX_AIC", 250, 0)])
            out_json, out_html = d / "r.json", d / "r.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(base), str(cand),
                                  "--json", str(out_json), "--out-html", str(out_html)],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(fus.validate(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare-fusion")
            html = out_html.read_text(encoding="utf-8")
            self.assertIn("合并/拆分证据", html)
            self.assertIn("unsupported", html)


class NoiseAndContractTests(unittest.TestCase):
    def _three_to_one(self, d):
        base, cand = d / "base", d / "cand"
        write_csv(base, [(0, "MatMul", "AI_CORE", 100, 0),
                         (0, "BiasAdd", "AI_VECTOR_CORE", 100, 105),
                         (0, "Relu", "AI_VECTOR_CORE", 100, 210)])
        write_csv(cand, [(0, "MatMul_BiasAdd_Relu", "MIX_AIC", 280, 0)])
        return base, cand

    def test_min_delta_floor_applied(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = self._three_to_one(d)
            b, _, _ = load(base)
            c, _, _ = load(cand)
            default = {m["name"]: m for m in fus.compare_captures(b, c, 50.0)["metrics"]}
            self.assertEqual(default["fusion_kernel_ms"]["verdict"], "noise")  # 0.02ms ≤ 0.05 下限
            fine = {m["name"]: m for m in
                    fus.compare_captures(b, c, 50.0, min_delta_ms=0.001)["metrics"]}
            self.assertEqual(fine["fusion_kernel_ms"]["verdict"], "improved")

    def test_cli_accepts_min_delta(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = self._three_to_one(d)
            run = subprocess.run(
                [sys.executable, str(SCRIPT), str(base), str(cand), "--min-delta-ms", "0.05",
                 "--json", str(d / "r.json"), "--out-html", str(d / "r.html")],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("unrecognized", run.stderr)

    def test_csv_without_step_id_is_low_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "a" / "ASCEND_PROFILER_OUTPUT"
            out.mkdir(parents=True)
            (out / "kernel_details.csv").write_text(
                "Name,Type,Duration(us),Start Time(us)\nMatMul,AI_CORE,100,0\n",
                encoding="utf-8")
            cap, notes, deg = load(d / "a")
            env = fus.envelope(cap, cap, fus.compare_captures(cap, cap, 50.0),
                               notes, notes, deg, deg)
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(any("Step Id" in x for x in env["data_quality"]["degraded"]))

    def test_validate_rejects_empty_metrics_and_bad_schema(self):
        env = {"skill": "x", "schema_version": 1, "mode": "pair", "inputs": {},
               "data_quality": {"sidecars": [], "degraded": [], "confidence": "high"},
               "metrics": [], "per_step": [], "summary": "s", "notes": []}
        self.assertTrue(fus.validate(env))
        env["schema_version"] = 2
        env["metrics"] = [{"name": "fusion_kernel_ms", "unit": "ms", "base": 1, "cand": 1,
                           "delta": 0, "delta_pct": 0, "verdict": "noise",
                           "noise_band": 0, "evidence": "e"}]
        self.assertTrue(fus.validate(env))


class ReviewFixTests(unittest.TestCase):
    def test_eval_eq07_typo_fixed(self):
        data = json.loads((SKILL / "evals/eval_queries.json").read_text(encoding="utf-8"))
        eq07 = next(q for q in data["queries"] if q["id"] == "EQ-07")
        self.assertEqual(eq07["prompt"], "这两份 profiling 优化前后步耗时差多少")

    def test_norm_name_keeps_body_digits_and_known_suffix(self):
        self.assertNotEqual(fus.norm_name("Conv2D"), fus.norm_name("Conv3D"))
        self.assertEqual(fus.norm_name("MatMul_v2"), fus.norm_name("matmulV2"))
        self.assertEqual(fus.norm_name("hcom_x_98_1"), fus.norm_name("hcom_x_293_1"))
        self.assertNotEqual(fus.norm_name("MatMul"), fus.norm_name("MatMulV3"))
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "Conv2D", "AI_CORE", 100, 0)])
            write_csv(cand, [(0, "Conv3D", "AI_CORE", 100, 0)])
            b, _, _ = load(base)
            c, _, _ = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            self.assertTrue(any(x["old"] == ["Conv2D"] for x in result["details"]))

    def test_too_long_sequence_forces_unproven(self):
        # > MAX_SEQ 时放弃 LCS，整序列缺口必须全部 unproven，不能凑成 confirmed/suspected。
        n = fus.MAX_SEQ + 1
        base_rows = [{"name": f"op{i}", "start": i * 10.0, "dur": 5.0} for i in range(n)]
        cand_rows = [{"name": "FusedAll", "start": 0.0, "dur": float(n * 10)}]
        gaps = fus.analyse_step(base_rows, cand_rows, 50.0, set())
        self.assertTrue(gaps)
        self.assertTrue(all(g["level"] == "unproven" for g in gaps))
        self.assertTrue(any("序列过长" in (g.get("reason") or "") for g in gaps))

    def test_html_none_rendered_as_dash(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "MatMul", "AI_CORE", 100, 0),
                             (0, "BiasAdd", "AI_CORE", 100, 105)])
            write_csv(cand, [(0, "MatMul_BiasAdd", "MIX_AIC", 190, 0)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            html = fus.render_html(env, result, SKILL / "assets/report.css")
            self.assertNotIn("<td>None</td>", html)
            self.assertIn("—", html)

    def test_missing_start_time_keeps_intervals_unverifiable(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "MatMul", "AI_CORE", 100, ""),
                             (0, "BiasAdd", "AI_CORE", 100, "")])
            write_csv(cand, [(0, "MatMul_BiasAdd", "MIX_AIC", 190, "")])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(mrow(env, "fusion_merge_confirmed")["cand"], 0)
            self.assertEqual(mrow(env, "fusion_merge_unproven")["cand"], 1)
            self.assertTrue(any("缺 Start Time" in x for x in env["data_quality"]["degraded"]
                                + env["notes"]))
            self.assertEqual(mrow(env, "fusion_op_count")["base"], 2)

    def test_trace_outside_step_events_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, [
                {"ph": "X", "name": "ProfilerStep#10", "pid": 1, "tid": 1,
                 "ts": 0, "dur": 10000, "cat": "cpu_op"},
                ev("Inside", 1000, 100),
                ev("Warmup", 20000, 100),
            ])
            cap, notes, _ = load(root)
            self.assertEqual(cap["step_ids"], [10])
            self.assertEqual(len(cap["steps"][10]), 1)
            self.assertNotIn(0, cap["steps"])
            self.assertTrue(any("窗口之外" in x for x in notes))

    def test_directional_metrics_and_noise_use_paired_steps(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            rows = [(0, "MatMul", "AI_CORE", 100, 0),
                    (0, "Add", "AI_CORE", 100, 105),
                    (0, "Relu", "AI_CORE", 100, 210)]
            write_csv(base, [(sid,) + r[1:] for sid in (0, 1) for r in rows])
            cand_rows = [(sid,) + r[1:] for sid in (0, 1) for r in rows]
            cand_rows.append((2, "MatMul", "AI_CORE", 10, 0))
            write_csv(cand, cand_rows)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            m = mrow(env, "fusion_kernel_ms")
            self.assertAlmostEqual(m["delta"], 0.0, places=4)
            self.assertEqual(m["verdict"], "noise")
            self.assertTrue(any("未配对 step" in x and "cand=[2]" in x for x in env["notes"]))
            self.assertLessEqual(len(env["per_step"]), 2)


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_and_evals(self):
        import re

        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm)
        self.assertIn(f"name: {SKILL.name}", fm.group(1))
        self.assertRegex(fm.group(1), r"description:\s*\S")
        evals = json.loads((SKILL / "evals/eval_queries.json").read_text(encoding="utf-8"))
        qs = evals["queries"]
        self.assertGreaterEqual(len(qs), 10)
        self.assertGreaterEqual(sum(1 for q in qs if q["should_trigger"]), 6)
        self.assertGreaterEqual(sum(1 for q in qs if not q["should_trigger"]), 4)
        self.assertTrue((SKILL / "assets/report.css").is_file())


class RealSchemaTests(unittest.TestCase):
    """真实数据里同名算子带数字实例后缀，不应被误判为合并/拆分。"""

    def test_numeric_suffix_alignment_no_fake_split(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "Op_1", "AI_CORE", 100, 0),
                             (0, "Op_2", "AI_CORE", 100, 105)])
            write_csv(cand, [(0, "Op_9", "AI_CORE", 200, 0)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = fus.compare_captures(b, c, 50.0)
            env = fus.envelope(b, c, result, bn, cn, bd, cd)
            self.assertEqual(result["splits"], 0)
            self.assertEqual(mrow(env, "fusion_merge_confirmed")["cand"], 0)
            self.assertEqual(mrow(env, "fusion_merge_suspected")["cand"], 0)


class RealSchemaTraceTests(unittest.TestCase):
    """纯 trace 路径：设备任务在 args["Task Type"]（cat=None）。"""

    def test_trace_task_type_kernels(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_trace(root, [
                {"ph": "X", "name": "ProfilerStep#0", "ts": 0, "dur": 10000, "cat": None, "pid": 1, "tid": 2},
                {"ph": "X", "name": "MatMul", "ts": 100, "dur": 1000, "cat": None, "pid": 1, "tid": 2,
                 "args": {"Task Type": "AI_CORE", "Physic Stream Id": 100}},
                {"ph": "X", "name": "Relu", "ts": 1200, "dur": 500, "cat": None, "pid": 1, "tid": 2,
                 "args": {"Task Type": "AI_VECTOR_CORE", "Physic Stream Id": 100}},
            ])
            cap, notes, deg = load(root)
            self.assertEqual(cap["source"], "trace_view.json")
            total = sum(len(v) for v in cap["steps"].values())
            self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
