"""kptools-ascend-profiling-compare-steps 的契约测试。"""

import csv
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-steps/scripts/step_groups.py"
)
SPEC = importlib.util.spec_from_file_location("kpc_steps", SCRIPT)
steps_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(steps_mod)


def _step(sid, comp=7000, comm_no=1000, comm=1200, free=2000, stage=10000, prep=500):
    return {
        "id": sid,
        "comp": comp,
        "comm_no": comm_no,
        "comm": comm,
        "free": free,
        "stage": stage,
        "prep": prep,
    }


def _write_native(root, steps, with_analysis=True, with_step_time=True):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace_view.json").write_text("[]", encoding="utf-8")

    if with_analysis:
        con = sqlite3.connect(out / "analysis.db")
        con.execute(
            "CREATE TABLE StepTraceTime(deviceId INTEGER, step INTEGER, computing REAL,"
            " communication_not_overlapped REAL, overlapped REAL, communication REAL,"
            " free REAL, stage REAL, preparing REAL)"
        )
        for s in steps:
            con.execute(
                "INSERT INTO StepTraceTime VALUES(0,?,?,?,0,?,?,?,?)",
                (
                    s["id"],
                    s["comp"],
                    s["comm_no"],
                    s["comm"],
                    s["free"],
                    s["stage"],
                    s["prep"],
                ),
            )
        con.commit()
        con.close()

    if with_step_time:
        con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
        con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
        for s in steps:
            start = s["id"] * 10_000_000
            con.execute(
                "INSERT INTO STEP_TIME VALUES(?,?,?)",
                (s["id"], start, start + int((s["stage"] + s.get("prep", 0)) * 1000)),
            )
        con.commit()
        con.close()


def _write_csv(root, steps):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "step_trace_time.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["Device_id", "Step", "Computing", "Communication(Not Overlapped)",
             "Communication", "Free", "Stage", "Preparing"]
        )
        for s in steps:
            writer.writerow(
                [0, s["id"], s["comp"], s["comm_no"], s["comm"], s["free"],
                 s["stage"], s["prep"]]
            )


def _sample_steps():
    base = [_step(i) for i in range(3)]
    cand = [_step(i, comp=8000, free=1000) for i in range(3, 5)]
    cand.append(_step(5, comp=8000, free=3000, stage=12000))
    return base + cand


def _run(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + [str(a) for a in args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class GroupCompareTests(unittest.TestCase):
    def test_two_group_medians_and_verdicts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, _sample_steps())
            cap = steps_mod.load_capture(root)
            base_ids, cand_ids, notes = steps_mod.select_groups(
                cap, [0, 1, 2], [3, 4, 5]
            )
            result = steps_mod.compare_groups(cap, base_ids, cand_ids)
            result["notes"] = list(dict.fromkeys(result["notes"] + notes))

            self.assertEqual(result["mode"], "single")
            self.assertEqual(result["data_quality"]["confidence"], "high")
            self.assertEqual(steps_mod.validate(result), [])

            m = {x["name"]: x for x in result["metrics"]}
            self.assertAlmostEqual(m["group_step_time_ms"]["base"], 10.5, places=3)
            self.assertAlmostEqual(m["group_step_time_ms"]["cand"], 10.5, places=3)
            self.assertEqual(m["group_step_time_ms"]["verdict"], "noise")

            self.assertAlmostEqual(m["group_computing_ms"]["base"], 7.0, places=3)
            self.assertAlmostEqual(m["group_computing_ms"]["cand"], 8.0, places=3)
            self.assertEqual(m["group_computing_ms"]["verdict"], "regressed")

            self.assertAlmostEqual(m["group_free_ms"]["base"], 2.0, places=3)
            self.assertAlmostEqual(m["group_free_ms"]["cand"], 1.0, places=3)
            # 3 对中 2 对改善、1 对恶化，方向一致性 0.667 < 0.75 → info
            self.assertEqual(m["group_free_ms"]["verdict"], "info")

            self.assertEqual(len(result["per_step"]), 3)
            self.assertEqual(result["per_step"][-1]["step"], 2)
            self.assertEqual(result["per_step"][-1]["cand_step"], 5)
            self.assertAlmostEqual(result["per_step"][-1]["delta"], -2.0, places=3)
            self.assertTrue(result["per_step"][-1].get("worst"))
            self.assertIn("最大劣化", result["summary"])

    def test_single_step_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, _sample_steps())
            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = _run(
                [root, "--out-html", out_html, "--json", out_json,
                 "--base-step", "0", "--cand-step", "3"]
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["mode"], "single")
            self.assertEqual(len(env["per_step"]), 1)
            self.assertEqual(env["per_step"][0]["step"], 0)
            self.assertEqual(env["per_step"][0]["cand_step"], 3)
            html = out_html.read_text(encoding="utf-8")
            self.assertIn("噪声带说明", html)
            self.assertIn("逐 step 配对", html)

    def test_group_size_mismatch_uses_intersection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, _sample_steps())
            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = _run(
                [root, "--out-html", out_html, "--json", out_json,
                 "--base-steps", "0,1,2", "--cand-steps", "5"]
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(len(env["per_step"]), 1)
            self.assertTrue(
                any("只比较前 1 对" in n for n in env["notes"]), env["notes"]
            )

    def test_skip_warmup_drops_leading_steps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, _sample_steps())
            env_path = Path(d) / "r.json"
            run = _run(
                [root, "--out-html", Path(d) / "r.html", "--json", env_path,
                 "--base-steps", "0,1,2", "--cand-steps", "3,4,5",
                 "--skip-warmup", "1"]
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(env_path.read_text(encoding="utf-8"))
            self.assertEqual(len(env["per_step"]), 2)
            self.assertEqual(env["per_step"][0]["step"], 1)
            self.assertEqual(env["per_step"][0]["cand_step"], 4)
            self.assertTrue(any("--skip-warmup" in n for n in env["notes"]))


class DegradeTests(unittest.TestCase):
    def test_no_four_class_uses_step_time_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(
                root,
                [_step(i) for i in range(4)],
                with_analysis=False,
                with_step_time=True,
            )
            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = _run(
                [root, "--out-html", out_html, "--json", out_json,
                 "--base-steps", "0,1", "--cand-steps", "2,3"]
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(env["data_quality"]["degraded"])
            m = {x["name"]: x for x in env["metrics"]}
            self.assertAlmostEqual(m["group_step_time_ms"]["base"], 10.5, places=3)
            self.assertAlmostEqual(m["group_computing_ms"]["base"], 0.0, places=3)
            self.assertAlmostEqual(m["group_free_ms"]["base"], 0.0, places=3)
            self.assertAlmostEqual(m["group_preparing_ms"]["base"], 0.0, places=3)
            for name in ("group_computing_ms", "group_communication_ms",
                         "group_free_ms", "group_preparing_ms"):
                self.assertEqual(m[name]["verdict"], "info", name)

    def test_no_step_data_empty_metrics_rc0(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            (root / "ASCEND_PROFILER_OUTPUT").mkdir(parents=True)
            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = _run([root, "--out-html", out_html, "--json", out_json])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["metrics"], [])
            self.assertEqual(env["per_step"], [])
            self.assertEqual(env["summary"], "无可用 step 数据")
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertIn("无可用 step 数据", out_html.read_text(encoding="utf-8"))

    def test_missing_step_id_rc2_with_available_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, _sample_steps())
            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = _run(
                [root, "--out-html", out_html, "--json", out_json,
                 "--base-steps", "99", "--cand-steps", "0"]
            )
            self.assertEqual(run.returncode, 2)
            combined = run.stdout + run.stderr
            self.assertIn("可用 step id", combined)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["metrics"], [])
            self.assertEqual(env["summary"], "step 选择失败")
            self.assertTrue(out_html.exists())

    def test_unreadable_input_rc3(self):
        with tempfile.TemporaryDirectory() as d:
            out_html = Path(d) / "err.html"
            out_json = Path(d) / "err.json"
            run = _run(
                [Path(d) / "nope", "--out-html", out_html, "--json", out_json]
            )
            self.assertEqual(run.returncode, 3)
            self.assertIn("Step 组对比失败", out_html.read_text(encoding="utf-8"))
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["metrics"], [])
            self.assertTrue(env["data_quality"]["degraded"])


class CsvTests(unittest.TestCase):
    def test_csv_fallback_with_parenthesized_columns(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_csv(root, _sample_steps())
            cap = steps_mod.load_capture(root)
            self.assertEqual(cap["step_source"], "step_trace_time.csv")
            self.assertTrue(cap["four_class"])
            self.assertEqual(cap["confidence"], "high")
            base_ids, cand_ids, _ = steps_mod.select_groups(cap, [0, 1, 2], [3, 4, 5])
            result = steps_mod.compare_groups(cap, base_ids, cand_ids)
            m = {x["name"]: x for x in result["metrics"]}
            self.assertAlmostEqual(m["group_computing_ms"]["base"], 7.0, places=3)
            self.assertAlmostEqual(m["group_computing_ms"]["cand"], 8.0, places=3)


class StepTimeRegressionTests(unittest.TestCase):
    def test_step_time_includes_preparing_without_step_time_db(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, [_step(i) for i in range(4)], with_step_time=False)
            cap = steps_mod.load_capture(root)
            self.assertAlmostEqual(cap["steps"][0]["step_time"], 10.5, places=3)
            result = steps_mod.compare_groups(cap, [0, 1], [2, 3])
            m = {x["name"]: x for x in result["metrics"]}
            self.assertIn("stage+preparing", m["group_step_time_ms"]["evidence"])

    def test_step_time_prefers_step_time_span(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = root / "ASCEND_PROFILER_OUTPUT"
            out.mkdir(parents=True)
            (out / "trace_view.json").write_text("[]", encoding="utf-8")
            con = sqlite3.connect(out / "analysis.db")
            con.execute(
                "CREATE TABLE StepTraceTime(deviceId INTEGER, step INTEGER, computing REAL,"
                " communication_not_overlapped REAL, communication REAL, free REAL,"
                " stage REAL, preparing REAL)"
            )
            for sid in range(2):
                con.execute("INSERT INTO StepTraceTime VALUES(0,?,?,?,?,?,?,?)",
                            (sid, 7000, 1000, 1200, 1500, 10000, 500))
            con.commit()
            con.close()
            con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
            con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
            for sid in range(2):
                con.execute("INSERT INTO STEP_TIME VALUES(?,?,?)",
                            (sid, sid * 30_000_000, sid * 30_000_000 + 20_000_000))
            con.commit()
            con.close()
            cap = steps_mod.load_capture(root)
            self.assertAlmostEqual(cap["steps"][0]["step_time"], 20.0, places=3)
            self.assertTrue(any("差异 >2%" in n for n in cap["notes"]))

    def test_multi_device_sums_components_for_additivity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = root / "ASCEND_PROFILER_OUTPUT"
            out.mkdir(parents=True)
            (out / "trace_view.json").write_text("[]", encoding="utf-8")
            con = sqlite3.connect(out / "analysis.db")
            con.execute(
                "CREATE TABLE StepTraceTime(deviceId INTEGER, step INTEGER, computing REAL,"
                " communication_not_overlapped REAL, communication REAL, free REAL,"
                " stage REAL, preparing REAL)"
            )
            for dev in (0, 1):
                for sid in range(2):
                    con.execute("INSERT INTO StepTraceTime VALUES(?,?,?,?,?,?,?,?)",
                                (dev, sid, 600, 200, 300, 500, 2000, 200))
            con.commit()
            con.close()
            cap = steps_mod.load_capture(root)
            s0 = cap["steps"][0]
            self.assertAlmostEqual(s0["step_time"], 2.2, places=3)  # max(stage)+max(prep)
            self.assertAlmostEqual(s0["comp"], 1.2, places=3)       # sum(600+600)
            self.assertAlmostEqual(s0["comm_no"], 0.4, places=3)    # sum(200+200)
            self.assertAlmostEqual(s0["free"], 1.0, places=3)       # Σ free(500+500)
            self.assertTrue(any("Device_id" in n for n in cap["notes"]))

    def test_invalid_step_text_rc2_with_report(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, _sample_steps())
            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = _run([root, "--out-html", out_html, "--json", out_json,
                        "--base-steps", "a", "--cand-steps", "0"])
            self.assertEqual(run.returncode, 2)
            self.assertIn("必须是整数", run.stderr)
            self.assertTrue(out_html.exists())
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["summary"], "step 选择失败")

    def test_no_degradation_note_when_no_worst(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, [_step(i, stage=20000) for i in (0, 1)]
                          + [_step(i, stage=10000) for i in (2, 3)])
            cap = steps_mod.load_capture(root)
            result = steps_mod.compare_groups(cap, [0, 1], [2, 3])
            self.assertEqual(result["worst"], {})
            self.assertIn("未出现候选劣化", steps_mod.render_html(result))

    def test_default_no_json_file_written(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "cap"
            _write_native(root, _sample_steps())
            run = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)], cwd=str(d),
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertFalse((d / "steps.json").exists())
            self.assertTrue((d / "profiling_steps_report.html").exists())


class ReviewFixTests(unittest.TestCase):
    def test_values_and_verdict_use_common_paired_samples(self):
        """基线 1 步 10ms、候选 3 步 9/100/100ms：只比首对，展示与判定都用配对样本。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, [
                _step(0, stage=10000, prep=0),
                _step(1, stage=9000, prep=0),
                _step(2, stage=100000, prep=0),
                _step(3, stage=100000, prep=0),
            ])
            cap = steps_mod.load_capture(root)
            result = steps_mod.compare_groups(cap, [0], [1, 2, 3])
            m = {x["name"]: x for x in result["metrics"]}
            self.assertAlmostEqual(m["group_step_time_ms"]["base"], 10.0, places=3)
            self.assertAlmostEqual(m["group_step_time_ms"]["cand"], 9.0, places=3)
            self.assertAlmostEqual(m["group_step_time_ms"]["delta"], 1.0, places=3)
            self.assertEqual(m["group_step_time_ms"]["verdict"], "improved")

    def test_step_time_only_ids_kept_and_comparable(self):
        """STEP_TIME 有 0/1/2、侧车只有 0/1 时，step 2 仍保留并可做步耗时比较。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = root / "ASCEND_PROFILER_OUTPUT"
            out.mkdir(parents=True)
            (out / "trace_view.json").write_text("[]", encoding="utf-8")
            con = sqlite3.connect(out / "analysis.db")
            con.execute(
                "CREATE TABLE StepTraceTime(deviceId INTEGER, step INTEGER, computing REAL,"
                " communication_not_overlapped REAL, communication REAL, free REAL,"
                " stage REAL, preparing REAL)"
            )
            for sid in (0, 1):
                con.execute(
                    "INSERT INTO StepTraceTime VALUES(0,?,?,?,?,?,?,?)",
                    (sid, 7000, 1000, 1200, 2000, 10000, 500),
                )
            con.commit()
            con.close()
            con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
            con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
            for sid, end in ((0, 10_500_000), (1, 20_500_000), (2, 30_000_000)):
                start = sid * 10_000_000
                con.execute("INSERT INTO STEP_TIME VALUES(?,?,?)", (sid, start, end))
            con.commit()
            con.close()

            cap = steps_mod.load_capture(root)
            self.assertEqual([s["id"] for s in cap["steps"]], [0, 1, 2])
            self.assertFalse(cap["steps"][2]["sidecar"])
            base_ids, cand_ids, _ = steps_mod.select_groups(cap, [0], [2])
            result = steps_mod.compare_groups(cap, base_ids, cand_ids)
            m = {x["name"]: x for x in result["metrics"]}
            # step 2 仅 STEP_TIME 跨度 end-start：30ms − 20ms = 10ms
            self.assertAlmostEqual(m["group_step_time_ms"]["cand"], 10.0, places=3)
            self.assertNotEqual(m["group_step_time_ms"]["verdict"], "info")
            self.assertEqual(m["group_computing_ms"]["verdict"], "info")

    def test_missing_preparing_marks_only_that_metric_unavailable(self):
        """侧车缺 Preparing 时不得把补出的 0ms 当实测，也不得因此把其它指标全判 info。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = root / "ASCEND_PROFILER_OUTPUT"
            out.mkdir(parents=True)
            (out / "trace_view.json").write_text("[]", encoding="utf-8")
            con = sqlite3.connect(out / "analysis.db")
            con.execute(
                "CREATE TABLE StepTraceTime(deviceId INTEGER, step INTEGER, computing REAL,"
                " communication_not_overlapped REAL, communication REAL, free REAL, stage REAL)"
            )
            for sid in range(4):
                con.execute(
                    "INSERT INTO StepTraceTime VALUES(0,?,?,?,?,?,?)",
                    (sid, 7000 if sid < 2 else 8000, 1000, 1200, 2000, 10000),
                )
            con.commit()
            con.close()

            cap = steps_mod.load_capture(root)
            self.assertFalse(cap["four_class"])
            self.assertFalse(cap["available"]["prep"])
            self.assertEqual(cap["confidence"], "low")
            result = steps_mod.compare_groups(cap, [0, 1], [2, 3])
            m = {x["name"]: x for x in result["metrics"]}
            self.assertEqual(m["group_preparing_ms"]["verdict"], "info")
            self.assertIn("不可用", m["group_preparing_ms"]["evidence"])
            self.assertEqual(m["group_computing_ms"]["verdict"], "regressed")

    def test_noise_band_uses_relative_floor(self):
        """1000ms 对 995ms、单步样本：相对下限 10ms 应生效，判 noise 且噪声带=10。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_native(root, [
                _step(0, stage=1_000_000, prep=0, comp=7000, free=2000),
                _step(1, stage=995_000, prep=0, comp=7000, free=2000),
            ])
            cap = steps_mod.load_capture(root)
            result = steps_mod.compare_groups(cap, [0], [1])
            m = {x["name"]: x for x in result["metrics"]}
            self.assertAlmostEqual(m["group_step_time_ms"]["noise_band"], 10.0, places=3)
            self.assertEqual(m["group_step_time_ms"]["verdict"], "noise")


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_references_and_evals(self):
        import re

        skill_dir = (
            ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-steps"
        )
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm, "SKILL.md 缺少 frontmatter")
        name = re.search(r"^name:\s*(\S+)", fm.group(1), re.MULTILINE)
        self.assertEqual(name.group(1), skill_dir.name)
        self.assertTrue((skill_dir / "assets/report.css").is_file())
        self.assertTrue((skill_dir / "references/step-group-methodology.md").is_file())

        evals = json.loads((skill_dir / "evals/eval_queries.json").read_text(encoding="utf-8"))
        queries = evals["queries"]
        self.assertGreaterEqual(len(queries), 10)
        self.assertGreaterEqual(sum(1 for q in queries if q["should_trigger"]), 6)
        self.assertGreaterEqual(sum(1 for q in queries if not q["should_trigger"]), 4)


if __name__ == "__main__":
    unittest.main()
