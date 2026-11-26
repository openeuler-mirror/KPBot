"""kptools-ascend-profiling-compare-opstats 的契约测试（程序化合成夹具）。"""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-opstats"
SCRIPT = SKILL / "scripts/opstats.py"

SPEC = importlib.util.spec_from_file_location("ops_mod", SCRIPT)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


def write_csv(root, rows, with_shape=True):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    header = "Step Id,Name,Type,Duration(us)" + (",Shape" if with_shape else "") + "\n"
    with open(out / "kernel_details.csv", "w", encoding="utf-8") as f:
        f.write(header)
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return out


def write_csv_no_step(root, rows):
    """kernel_details.csv 不含 Step Id 列。"""
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "kernel_details.csv", "w", encoding="utf-8") as f:
        f.write("Name,Type,Duration(us),Shape\n")
        for r in rows:
            f.write(",".join(str(x) for x in r[1:]) + "\n")
    return out


def write_csv_empty_step(root, rows):
    """Step Id 列存在但值为空。"""
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "kernel_details.csv", "w", encoding="utf-8") as f:
        f.write("Step Id,Name,Type,Duration(us),Shape\n")
        for r in rows:
            f.write("," + ",".join(str(x) for x in r[1:]) + "\n")
    return out


def write_csv_io_shape(root, rows):
    """真实 kernel_details.csv 形态：同时有 Input Shapes / Output Shapes。"""
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    header = "Step Id,Name,Type,Duration(us),Input Shapes,Output Shapes\n"
    with open(out / "kernel_details.csv", "w", encoding="utf-8") as f:
        f.write(header)
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return out


def write_db(root, rows, with_step_time=True, name="ascend_pytorch_profiler_0.db"):
    """按真实 torch_npu db schema 造夹具：TASK/COMPUTE_TASK_INFO/STRING_IDS/STEP_TIME。

    rows: (step, name, taskType, dur_us, in_shape, out_shape)
    """
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out / name)
    con.execute("CREATE TABLE TASK(startNs INTEGER, endNs INTEGER, globalTaskId INTEGER,"
                " taskType INTEGER)")
    con.execute("CREATE TABLE COMPUTE_TASK_INFO(name INTEGER, globalTaskId INTEGER,"
                " taskType INTEGER, inputShapes INTEGER, outputShapes INTEGER)")
    con.execute("CREATE TABLE STRING_IDS(id INTEGER, value TEXT)")
    if with_step_time:
        con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
    strings = {}

    def sid(value):
        if value in (None, ""):
            return -1
        if value not in strings:
            strings[value] = len(strings)
        return strings[value]

    steps = sorted({r[0] for r in rows})
    span = 1_000_000
    if with_step_time:
        for i, step_id in enumerate(steps):
            con.execute("INSERT INTO STEP_TIME VALUES(?,?,?)",
                        (step_id, i * span, (i + 1) * span))
    for idx, (step_id, name, typ, dur_us, in_shape, out_shape) in enumerate(rows):
        base_ns = 100_000 + (steps.index(step_id) * span if with_step_time else 0)
        con.execute("INSERT INTO TASK VALUES(?,?,?,?)",
                    (base_ns, base_ns + int(dur_us * 1000), idx, sid(typ)))
        con.execute("INSERT INTO COMPUTE_TASK_INFO VALUES(?,?,?,?,?)",
                    (sid(name), idx, sid(typ), sid(in_shape), sid(out_shape)))
    for value, i in strings.items():
        con.execute("INSERT INTO STRING_IDS VALUES(?,?)", (i, value))
    con.commit()
    con.close()


def write_trace(root, events):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def ev(name, ts, dur, cat):
    return {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 2}


def run(path, **kw):
    notes, deg = [], []
    cap = ops.load_capture(path, notes, deg)
    return cap, notes, deg


def metric_by_name(env, name):
    return next(m for m in env["metrics"] if m["name"] == name)


class MainPathTests(unittest.TestCase):
    def test_metrics_from_kernel_csv(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [
                (0, "MatMul", "AI_CORE", 1000, "16,16"),
                (0, "Add", "AI_VECTOR_CORE", 500, "16,16"),
                (1, "MatMul", "AI_CORE", 1000, "16,16"),
                (1, "Add", "AI_VECTOR_CORE", 500, "16,16"),
            ])
            write_csv(cand, [
                (0, "MatMul", "AI_CORE", 800, "16,16"),
                (0, "Add", "AI_VECTOR_CORE", 400, "16,16"),
                (1, "MatMul", "AI_CORE", 800, "16,16"),
                (1, "Add", "AI_VECTOR_CORE", 400, "16,16"),
            ])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])

            self.assertEqual(ops.validate(env), [])
            kind = metric_by_name(env, "op_kind_count")
            self.assertEqual((kind["base"], kind["cand"], kind["verdict"]), (2, 2, "info"))

            call = metric_by_name(env, "op_call_count_per_step")
            self.assertAlmostEqual(call["base"], 2.0)
            self.assertAlmostEqual(call["cand"], 2.0)

            tm = metric_by_name(env, "op_time_ms_per_step")
            self.assertAlmostEqual(tm["base"], 1.5)
            self.assertAlmostEqual(tm["cand"], 1.2)
            self.assertEqual(tm["verdict"], "improved")

            p50 = metric_by_name(env, "op_p50_ms")
            self.assertAlmostEqual(p50["base"], 0.75)
            self.assertAlmostEqual(p50["cand"], 0.6)

            self.assertEqual(env["data_quality"]["confidence"], "high")
            self.assertEqual(env["data_quality"]["sidecars"], ["kernel_details.csv"])
            self.assertIn("每步算子耗时", env["summary"])
            self.assertEqual(len(env["per_step"]), 2)

    def test_shape_drift_and_unparsed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [
                (0, "MatMul", "AI_CORE", 1000, "16,16"),
                (0, "Add", "AI_VECTOR_CORE", 500, ""),
            ])
            write_csv(cand, [
                (0, "MatMul", "AI_CORE", 1000, "32,32"),
                (0, "Add", "AI_VECTOR_CORE", 500, ""),
            ])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])

            changed = metric_by_name(env, "op_shape_changed_count")
            self.assertEqual(changed["cand"], 1)
            unparsed = metric_by_name(env, "op_shape_unparsed_count")
            self.assertEqual((unparsed["base"], unparsed["cand"]), (1, 1))

    def test_new_and_gone_ops(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "OpA", "AI_CORE", 100, "1"),
                             (0, "OpB", "AI_CORE", 100, "1")])
            write_csv(cand, [(0, "OpA", "AI_CORE", 100, "1"),
                             (0, "OpC", "AI_CORE", 100, "1")])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])

            self.assertEqual(metric_by_name(env, "op_new_count")["cand"], 1)
            self.assertEqual(metric_by_name(env, "op_gone_count")["cand"], 1)
            self.assertEqual(result["new_ops"], ["OpC"])
            self.assertEqual(result["gone_ops"], ["OpB"])

    def _raw_cap(self, names):
        return {
            "rows": [
                {"step": 0, "name": n, "type": "COMMUNICATION", "dur": 10.0,
                 "shape": "", "shape_known": False}
                for n in names
            ],
            "source": "raw",
            "path": "raw",
        }

    def test_suffix_variants_not_counted_as_new(self):
        # 直连 compare（绕过读取层归一化），模拟“base 1 个实例、cand 多实例”。
        cases = [
            (["hcom_allGather__503_98_1"],
             ["hcom_allGather__503_293_1", "hcom_allGather__503_555_1"]),
            (["hcom_x_98_1"], ["hcom_x_293_1", "hcom_x_555_1"]),
        ]
        for base_names, cand_names in cases:
            result = ops.compare(
                self._raw_cap(base_names), self._raw_cap(cand_names),
                0.05, 0.0, 0.6,
            )
            self.assertEqual(result["new_ops"], [])
            self.assertEqual(result["gone_ops"], [])
            self.assertEqual(len(result["base_ops"]), 1)
            self.assertEqual(len(result["cand_ops"]), 1)

    def test_output_shape_drift_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv_io_shape(base, [(0, "OpA", "AI_CORE", 100, '"1,1"', '"2"')])
            write_csv_io_shape(cand, [(0, "OpA", "AI_CORE", 100, '"1,1"', '"3"')])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])
            self.assertEqual(metric_by_name(env, "op_shape_changed_count")["cand"], 1)

    def test_percentile_noise_floor(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(i, "OpA", "AI_CORE", 1000, "1") for i in range(4)])
            write_csv(cand, [(i, "OpA", "AI_CORE", 1000.001, "1") for i in range(4)])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            env = ops.envelope(b, c, ops.compare(b, c, 0.05, 0.0, 0.6),
                               bn, cn, bd, cd, b["source"], c["source"])
            for name in ("op_p50_ms", "op_p95_ms", "op_p99_ms"):
                m = metric_by_name(env, name)
                self.assertGreaterEqual(m["noise_band"], 0.05)
                self.assertEqual(m["verdict"], "noise")

    def test_summary_mentions_new_and_gone_when_time_is_noise(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(i, "AlphaKernel", "AI_CORE", 1000, "1") for i in range(4)])
            write_csv(cand, [(i, "BetaKernel", "AI_CORE", 1000, "1") for i in range(4)])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            env = ops.envelope(b, c, ops.compare(b, c, 0.05, 0.0, 0.6),
                               bn, cn, bd, cd, b["source"], c["source"])
            self.assertIn("新增 1", env["summary"])
            self.assertIn("消失 1", env["summary"])

    def test_rename_suspected_not_counted_as_new_gone(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "MatMul_1", "AI_CORE", 100, "1")])
            write_csv(cand, [(0, "MatMulV2", "AI_CORE", 100, "1")])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])

            self.assertEqual(metric_by_name(env, "op_new_count")["cand"], 0)
            self.assertEqual(metric_by_name(env, "op_gone_count")["cand"], 0)
            self.assertEqual(len(result["renames"]), 1)
            self.assertTrue(any("疑似改名" in n for n in env["notes"]))


class DegradeTests(unittest.TestCase):
    def test_no_shape_column_is_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_csv(root, [(0, "OpA", "AI_CORE", 100)], with_shape=False)
            _, _, deg = run(root)
            self.assertTrue(any("无 shape 列" in x for x in deg))

    def test_db_real_schema_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_db(base, [(0, "OpA", "AI_CORE", 100.0, '"1"', '"2"')])
            write_db(cand, [(0, "OpA", "AI_CORE", 80.0, '"1"', '"2"')])
            b, bn, bd = run(base)
            self.assertEqual(b["source"], "ascend_pytorch_profiler_0.db")
            self.assertEqual(b["rows"][0]["name"], "OpA")
            self.assertEqual(b["rows"][0]["dur"], 100.0)
            self.assertTrue(b["rows"][0]["shape_known"])
            self.assertTrue(any("COMPUTE_TASK_INFO" in x for x in bd))
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])
            self.assertEqual(env["data_quality"]["confidence"], "medium")
            self.assertEqual(ops.validate(env), [])

    def test_db_without_step_time_is_low_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_db(base, [(0, "OpA", "AI_CORE", 100.0, "", "")], with_step_time=False)
            write_db(cand, [(0, "OpA", "AI_CORE", 100.0, "", "")], with_step_time=False)
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(any("STEP_TIME" in x for x in env["data_quality"]["degraded"]))

    def test_trace_only_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            for root in (base, cand):
                write_trace(root, [
                    ev("ProfilerStep#0", 0, 5000, "cpu_op"),
                    ev("MatMul", 100, 1000, "AI_CORE"),
                    ev("Add", 1200, 500, "AI_VECTOR_CORE"),
                ])
            b, bn, bd = run(base)
            self.assertEqual(b["source"], "trace_view.json")
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])
            self.assertEqual(env["data_quality"]["confidence"], "medium")
            self.assertTrue(env["data_quality"]["degraded"])
            self.assertEqual(ops.validate(env), [])
            self.assertEqual(len(env["per_step"]), 1)

    def test_unreadable_input_emits_error_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "err.html"
            run_p = subprocess.run(
                [sys.executable, str(SCRIPT), str(d / "nope"), str(d / "none"),
                 "--out-html", str(out)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(run_p.returncode, 3)
            self.assertTrue(out.exists())
            self.assertIn("算子对比失败", out.read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_cli_json_and_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "OpA", "AI_CORE", 1000, "1,1")])
            write_csv(cand, [(0, "OpA", "AI_CORE", 800, "1,1")])
            out_json, out_html = d / "r.json", d / "r.html"
            run_p = subprocess.run(
                [sys.executable, str(SCRIPT), str(base), str(cand),
                 "--json", str(out_json), "--out-html", str(out_html)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run_p.returncode, 0, run_p.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(ops.validate(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare-opstats")
            html = out_html.read_text(encoding="utf-8")
            self.assertIn("指标对比", html)
            self.assertIn("契约置信度", html)

    def test_cli_rejects_same_output_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "OpA", "AI_CORE", 1000, "1,1")])
            write_csv(cand, [(0, "OpA", "AI_CORE", 800, "1,1")])
            same = d / "same.html"
            run_p = subprocess.run(
                [sys.executable, str(SCRIPT), str(base), str(cand),
                 "--json", str(same), "--out-html", str(same)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(run_p.returncode, 0)
            self.assertFalse(same.exists())

    def test_cli_rejects_overwriting_input_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "OpA", "AI_CORE", 1000, "1,1")])
            write_csv(cand, [(0, "OpA", "AI_CORE", 800, "1,1")])
            target = base / "ASCEND_PROFILER_OUTPUT" / "kernel_details.csv"
            before = target.read_bytes()
            run_p = subprocess.run(
                [sys.executable, str(SCRIPT), str(base), str(cand),
                 "--out-html", str(target)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(run_p.returncode, 0)
            self.assertEqual(target.read_bytes(), before)


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_and_evals(self):
        import re

        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm)
        self.assertIn(f"name: {SKILL.name}", fm.group(1))
        self.assertRegex(fm.group(1), r"description:\s*\S")

        evals = json.loads((SKILL / "evals/eval_queries.json").read_text(encoding="utf-8"))
        self.assertEqual(evals["skill_name"], SKILL.name)
        qs = evals["queries"]
        self.assertGreaterEqual(len(qs), 10)
        self.assertGreaterEqual(sum(1 for q in qs if q["should_trigger"]), 6)
        self.assertGreaterEqual(sum(1 for q in qs if not q["should_trigger"]), 4)
        self.assertTrue((SKILL / "assets/report.css").is_file())


class RealSchemaTests(unittest.TestCase):
    """真实数据里 hcom 算子只差数字实例后缀，不算改名/新增/消失。"""

    def test_numeric_suffix_is_not_rename(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "hcom_allGather__503_98_1", "AI_CORE", 100, "1")])
            write_csv(cand, [(0, "hcom_allGather__503_293_1", "AI_CORE", 100, "1")])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])
            self.assertEqual(metric_by_name(env, "op_new_count")["cand"], 0)
            self.assertEqual(metric_by_name(env, "op_gone_count")["cand"], 0)
            self.assertTrue(any("实例后缀" in n for n in env["notes"]))

    def test_multi_instance_suffix_not_counted_as_new(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "hcom_allGather__503_98_1", "AI_CORE", 100, "1")])
            write_csv(cand, [(0, "hcom_allGather__503_293_1", "AI_CORE", 100, "1"),
                             (0, "hcom_allGather__503_555_1", "AI_CORE", 100, "1")])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            self.assertEqual(result["new_ops"], [])
            self.assertEqual(result["gone_ops"], [])
            self.assertEqual(len(result["cand_ops"]), 1)
            self.assertEqual(len(result["base_ops"]), 1)

    def test_instance_suffix_collapsed_in_op_kind(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            rows = [
                (0, "hcom_allGather__503_98_1", "AI_CORE", 100, "1"),
                (0, "hcom_allGather__503_293_1", "AI_CORE", 100, "1"),
                (0, "hcom_allReduce__503_98_1", "AI_CORE", 100, "1"),
                (0, "hcom_allReduce__503_293_1", "AI_CORE", 100, "1"),
            ]
            write_csv(base, rows)
            write_csv(cand, rows)
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            result = ops.compare(b, c, 0.05, 0.0, 0.6)
            env = ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])
            self.assertEqual(metric_by_name(env, "op_kind_count")["base"], 2)
            self.assertEqual(metric_by_name(env, "op_kind_count")["cand"], 2)


class StepIdDegradeTests(unittest.TestCase):
    def _env(self, base, cand):
        b, bn, bd = run(base)
        c, cn, cd = run(cand)
        result = ops.compare(b, c, 0.05, 0.0, 0.6)
        return bd, ops.envelope(b, c, result, bn, cn, bd, cd, b["source"], c["source"])

    def test_csv_without_step_id_degrades_to_low(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv_no_step(base, [(0, "MatMulV2", "AI_CORE", 100, "1")])
            write_csv_no_step(cand, [(0, "MatMulV2", "AI_CORE", 100, "1")])
            bd, env = self._env(base, cand)
            self.assertTrue(any("整窗" in x for x in bd))
            self.assertEqual(env["data_quality"]["confidence"], "low")

    def test_csv_empty_step_ids_degrades_to_low(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv_empty_step(base, [(0, "MatMulV2", "AI_CORE", 100, "1")])
            write_csv_empty_step(cand, [(0, "MatMulV2", "AI_CORE", 100, "1")])
            bd, env = self._env(base, cand)
            self.assertTrue(any("全为空" in x for x in bd))
            self.assertEqual(env["data_quality"]["confidence"], "low")

    def test_many_invalid_step_ids_degrade_to_low(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            rows = [("x", "OpA", "AI_CORE", 100, "1"),
                    ("y", "OpB", "AI_CORE", 100, "1"),
                    (2, "OpC", "AI_CORE", 100, "1")]
            write_csv(base, rows)
            write_csv(cand, rows)
            bd, env = self._env(base, cand)
            self.assertTrue(any("Step Id 无法解析" in x for x in bd))
            self.assertEqual(env["data_quality"]["confidence"], "low")


class RealSchemaTraceTests(unittest.TestCase):
    """纯 trace 路径：设备任务在 args["Task Type"]（cat=None）。"""

    def test_trace_task_type_kernels(self):
        def ev(name, ts, dur, cat=None, args=None):
            e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 2}
            if args:
                e["args"] = args
            return e

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            (root / "ASCEND_PROFILER_OUTPUT").mkdir(parents=True)
            (root / "ASCEND_PROFILER_OUTPUT" / "trace_view.json").write_text(json.dumps([
                ev("ProfilerStep#0", 0, 10000),
                ev("MatMul", 100, 1000, None,
                   {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
            ]), encoding="utf-8")
            cap, _, _ = run(root)
            self.assertEqual(cap["source"], "trace_view.json")
            self.assertEqual(len(cap["rows"]), 1)

    @staticmethod
    def _ev(name, ts, dur, cat=None, args=None):
        e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 2}
        if args:
            e["args"] = args
        return e

    def _write(self, root, events):
        out = root / "ASCEND_PROFILER_OUTPUT"
        out.mkdir(parents=True, exist_ok=True)
        (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")

    def test_trace_excludes_dma_sqe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            self._write(root, [
                self._ev("ProfilerStep#0", 0, 10000),
                self._ev("MatMul", 100, 1000, None, {"Task Type": "AI_CORE"}),
                self._ev("MemcpySdma", 2000, 500, None, {"Task Type": "SDMA_SQE"}),
                self._ev("MemcpyPcie", 3000, 500, None, {"Task Type": "PCIE_DMA_SQE"}),
            ])
            cap, _, _ = run(root)
            self.assertEqual([r["name"] for r in cap["rows"]], ["MatMul"])

    def test_trace_events_outside_step_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            self._write(root, [
                self._ev("ProfilerStep#0", 0, 1000),
                self._ev("InStep", 100, 100, None, {"Task Type": "AI_CORE"}),
                self._ev("Warmup", 2000, 100, None, {"Task Type": "AI_CORE"}),
            ])
            cap, notes, _ = run(root)
            self.assertEqual([r["name"] for r in cap["rows"]], ["InStep"])
            self.assertTrue(any("不在 ProfilerStep 窗口" in n for n in notes))

    def test_cross_source_comparison_is_caveated(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "OpA", "AI_CORE", 1000, "1")])
            self._write(cand, [
                self._ev("ProfilerStep#0", 0, 10000),
                self._ev("OpA", 100, 1000, None, {"Task Type": "AI_CORE"}),
            ])
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            env = ops.envelope(b, c, ops.compare(b, c, 0.05, 0.0, 0.6),
                               bn, cn, bd, cd, b["source"], c["source"])
            self.assertTrue(any("数据源不同" in x for x in env["data_quality"]["degraded"]))
            self.assertEqual(env["data_quality"]["confidence"], "medium")


class CsvFallbackTests(unittest.TestCase):
    def test_bad_csv_reports_parse_failure_not_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_db(root, [(0, "OpA", "AI_CORE", 100, "", "")])
            out = root / "ASCEND_PROFILER_OUTPUT"
            (out / "kernel_details.csv").write_text("foo,bar\n1,2\n", encoding="utf-8")
            cap, _, deg = run(root)
            self.assertEqual(cap["source"], "ascend_pytorch_profiler_0.db")
            self.assertTrue(any("解析失败" in x for x in deg))
            self.assertFalse(any("缺失" in x for x in deg))


class TraceRobustnessTests(unittest.TestCase):
    def _write_trace(self, root, payload):
        out = root / "ASCEND_PROFILER_OUTPUT"
        out.mkdir(parents=True, exist_ok=True)
        (out / "trace_view.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    def test_dict_wrapped_trace_supported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            self._write_trace(root, {"traceEvents": [
                ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                ev("MatMul", 100, 1000, "AI_CORE"),
            ]})
            cap, _, _ = run(root)
            self.assertEqual(cap["source"], "trace_view.json")
            self.assertEqual([r["name"] for r in cap["rows"]], ["MatMul"])

    def test_oversized_trace_truncates_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            text = json.dumps([
                ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                ev("MatMul", 100, 1000, "AI_CORE"),
            ])
            self._write_trace(root, text)
            old = (ops.MAX_TRACE_BYTES, ops.TRACE_TRUNCATE_BYTES)
            ops.MAX_TRACE_BYTES, ops.TRACE_TRUNCATE_BYTES = 1, len(text)
            try:
                cap, _, deg = run(root)
            finally:
                ops.MAX_TRACE_BYTES, ops.TRACE_TRUNCATE_BYTES = old
            self.assertTrue(any("过大" in x for x in deg))
            self.assertEqual([r["name"] for r in cap["rows"]], ["MatMul"])

    def test_hcom_kept_with_non_ai_task_type(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            self._write_trace(root, [
                ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                {"ph": "X", "name": "hcom_allReduce__503_98_1", "ts": 100, "dur": 1000,
                 "pid": 1, "tid": 2, "args": {"Task Type": "COMM_SQE"}},
            ])
            cap, _, _ = run(root)
            self.assertEqual([r["name"] for r in cap["rows"]], ["hcom_allReduce"])


class MultiDbTests(unittest.TestCase):
    def test_prefers_db_with_most_steps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_db(root, [(0, "OpA", "AI_CORE", 100, "", "")],
                     name="ascend_pytorch_profiler_0.db")
            write_db(root, [(0, "OpB", "AI_CORE", 100, "", ""),
                            (1, "OpC", "AI_CORE", 100, "", "")],
                     name="ascend_pytorch_profiler_1.db")
            cap, notes, deg = run(root)
            self.assertEqual(cap["source"], "ascend_pytorch_profiler_1.db")
            self.assertEqual({r["name"] for r in cap["rows"]}, {"OpB", "OpC"})
            self.assertTrue(any("发现 2 个" in n for n in notes))
            self.assertTrue(any("发现 2 个" in x for x in deg))

    def test_same_db_kind_different_file_not_cross_source(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_db(base, [(0, "OpA", "AI_CORE", 100, "", "")],
                     name="ascend_pytorch_profiler_0.db")
            write_db(cand, [(0, "OpA", "AI_CORE", 100, "", "")],
                     name="ascend_pytorch_profiler_1.db")
            b, bn, bd = run(base)
            c, cn, cd = run(cand)
            env = ops.envelope(b, c, ops.compare(b, c, 0.05, 0.0, 0.6),
                               bn, cn, bd, cd, b["source"], c["source"])
            self.assertNotEqual(b["source"], c["source"])
            self.assertFalse(any("数据源不同" in x for x in env["data_quality"]["degraded"]))


class DeterminismTests(unittest.TestCase):
    def test_identical_input_produces_identical_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            rows_b = [(0, f"Op{i}", "AI_CORE", 100 + i, f'"1"', f'"2"') for i in range(8)]
            rows_c = [(0, f"Op{i}", "AI_CORE", 90 + i, f'"1"', f'"3"') for i in range(8)]
            write_csv_io_shape(base, rows_b)
            write_csv_io_shape(cand, rows_c)
            outs = []
            for seed in ("1", "2"):
                out = d / f"r{seed}.html"
                env = dict(**__import__("os").environ, PYTHONHASHSEED=seed)
                run_p = subprocess.run(
                    [sys.executable, str(SCRIPT), str(base), str(cand),
                     "--out-html", str(out)],
                    capture_output=True, text=True, timeout=120, env=env,
                )
                self.assertEqual(run_p.returncode, 0, run_p.stderr)
                outs.append(out.read_text(encoding="utf-8"))
            self.assertEqual(outs[0], outs[1])


class CoverageAndStepFillTests(unittest.TestCase):
    def test_explicit_db_path_recorded_in_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            write_db(root, [(0, "OpA", "AI_CORE", 100, "", "")])
            db = root / "ASCEND_PROFILER_OUTPUT" / "ascend_pytorch_profiler_0.db"
            cap = ops.load_capture(db, [], [])
            self.assertTrue(cap["path"].endswith("ascend_pytorch_profiler_0.db"))

    def test_suffix_variant_exemption_limited_to_hcom(self):
        # FastGeluV2 → FastGeluV3 是真实版本换代，不能当“仅实例后缀不同”豁免。
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "FastGeluV2", "AI_CORE", 1000, "1,1")])
            write_csv(cand, [(0, "FastGeluV3", "AI_CORE", 1000, "1,1")])
            b = ops.load_capture(base, [], [])
            c = ops.load_capture(cand, [], [])
            res = ops.compare(b, c, 0.05, 0.0, 0.6)
            self.assertTrue(res["renames"] or res["new_ops"])
            self.assertFalse(any("仅数字实例后缀" in n for n in res["notes"]))

    def test_known_steps_zero_fill(self):
        # trace 有 2 个 ProfilerStep，但只有 step0 有算子：step1 仍要计入“每步”分母。
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "ASCEND_PROFILER_OUTPUT"
            out.mkdir(parents=True)
            events = [
                {"ph": "X", "name": "ProfilerStep#0", "pid": 100, "tid": 100,
                 "ts": "0", "dur": "10000", "cat": "cpu_op"},
                {"ph": "X", "name": "MatMul", "pid": 200, "tid": 1,
                 "ts": "1000", "dur": "5000", "cat": "AI_CORE", "args": {"task id": 1}},
                {"ph": "X", "name": "ProfilerStep#1", "pid": 100, "tid": 100,
                 "ts": "10000", "dur": "10000", "cat": "cpu_op"},
            ]
            (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
            cap = ops.load_capture(out, [], [])
            agg = ops.aggregate(cap)
            self.assertEqual(agg["steps"], [0, 1])
            self.assertEqual(agg["per_step"][1]["count"], 0)

    def test_task_coverage_mismatch_marks_info(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            write_csv(base, [(0, "hcom_allReduce", "AI_CORE", 1000, "1,1"),
                             (0, "MatMul", "AI_CORE", 1000, "1,1")])
            write_csv(cand, [(0, "MatMul", "AI_CORE", 800, "1,1")])
            b = ops.load_capture(base, [], [])
            c = ops.load_capture(cand, [], [])
            res = ops.compare(b, c, 0.05, 0.0, 0.6)
            m = {x["name"]: x for x in res["metrics"]}
            self.assertEqual(m["op_time_ms_per_step"]["verdict"], "info")
            self.assertTrue(any("任务类型不同" in n for n in res["notes"]))


if __name__ == "__main__":
    unittest.main()
