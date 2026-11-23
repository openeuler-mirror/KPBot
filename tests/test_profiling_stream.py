"""kptools-ascend-profiling-stream 的契约测试（合成夹具，不依赖真实 trace）。"""

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-stream"
STREAM = SKILL_DIR / "scripts/stream.py"
SPEC = importlib.util.spec_from_file_location("kps_stream", STREAM)
st = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(st)


def _ev(name, pid, tid, ts_us, dur_us, cat="AI_CORE", args=None):
    e = {"ph": "X", "name": name, "pid": pid, "tid": tid,
         "ts": str(ts_us), "dur": dur_us, "cat": cat}
    if args:
        e["args"] = args
    return e


def _write(root, events):
    root.mkdir(parents=True, exist_ok=True)
    (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def _overlap_trace(root, scale=1.0):
    """两条流 [0,6] 与 [4,10]（µs），窗口 10µs：各 60% 忙碌，平均并发 1.2，峰值 2。"""
    _write(root, [
        _ev("ProfilerStep#0", 100, 100, 0, 10, "cpu_op"),
        _ev("matmul", 200, 1, 0, 6 * scale),
        _ev("matmul", 200, 2, 4, 6 * scale),
    ])


def _single_stream_trace(root):
    _write(root, [
        _ev("ProfilerStep#0", 100, 100, 0, 5, "cpu_op"),
        _ev("matmul", 200, 1, 0, 5),
    ])


def _metrics(cap):
    return cap["metrics"]


class StreamMetricTests(unittest.TestCase):
    def test_two_streams_partial_overlap_exact(self):
        with tempfile.TemporaryDirectory() as d:
            _overlap_trace(Path(d))
            cap = st.analyze_capture(Path(d))
            m = _metrics(cap)
            self.assertEqual(m["stream_count"], 2)
            self.assertAlmostEqual(m["stream_busy_pct"], 60.0, places=4)
            self.assertAlmostEqual(m["stream_busy_max_pct"], 60.0, places=4)
            self.assertAlmostEqual(m["stream_concurrency_avg"], 1.2, places=4)
            self.assertEqual(m["stream_concurrency_max"], 2)
            self.assertAlmostEqual(m["stream_concurrency_per_step"], 1.2, places=4)
            self.assertEqual(cap["steps"][0]["concurrency_avg"], 1.2)
            self.assertEqual(cap["segments"], [(0.0, 4000.0, 1), (4000.0, 6000.0, 2),
                                               (6000.0, 10000.0, 1)])

    def test_single_stream(self):
        with tempfile.TemporaryDirectory() as d:
            _single_stream_trace(Path(d))
            cap = st.analyze_capture(Path(d))
            m = _metrics(cap)
            self.assertEqual(m["stream_count"], 1)
            self.assertAlmostEqual(m["stream_busy_pct"], 100.0, places=4)
            self.assertAlmostEqual(m["stream_concurrency_avg"], 1.0, places=4)
            self.assertEqual(m["stream_concurrency_max"], 1)
            self.assertAlmostEqual(m["stream_concurrency_per_step"], 1.0, places=4)

    def test_stream_id_prefers_args(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("matmul", 200, 9, 0, 5, args={"streamId": "11"}),
                _ev("matmul", 200, 9, 0, 5, args={"stream_id": "12"}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(_metrics(cap)["stream_count"], 2)
            self.assertEqual({s["stream"] for s in cap["streams"]}, {"11", "12"})


class DependencyTests(unittest.TestCase):
    def test_cross_stream_dependency_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("ProfilerStep#0", 100, 100, 0, 10, "cpu_op"),
                _ev("matmul", 200, 1, 0, 6),
                _ev("matmul", 200, 2, 4, 6),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"Event ID": "7", "streamId": "1"}),
                _ev("Event Wait", 100, 100, 3, 0.1, "Event Wait",
                    {"Event ID": "7", "streamId": "2"}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(_metrics(cap)["stream_cross_dep_count"], 1)
            self.assertEqual(cap["deps"]["pairing_rate"], 100.0)
            self.assertTrue(any("配对率" in n for n in cap["notes"]))

    def test_same_stream_record_wait_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("matmul", 200, 1, 0, 5),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"eventId": "3", "streamId": "1"}),
                _ev("Event Wait", 100, 100, 2, 0.1, "Event Wait",
                    {"eventId": "3", "streamId": "1"}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(_metrics(cap)["stream_cross_dep_count"], 0)

    def test_missing_event_id_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("ProfilerStep#0", 100, 100, 0, 5, "cpu_op"),
                _ev("matmul", 200, 1, 0, 5),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record"),
                _ev("Event Wait", 100, 100, 2, 0.1, "Event Wait"),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(_metrics(cap)["stream_cross_dep_count"], 0)
            self.assertTrue(any("缺少 event id" in x for x in cap["degraded"]))
            self.assertEqual(cap["confidence"], "medium")

    def test_no_event_events_is_not_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            _single_stream_trace(Path(d))
            cap = st.analyze_capture(Path(d))
            self.assertEqual(_metrics(cap)["stream_cross_dep_count"], 0)
            self.assertFalse(any("event id" in x for x in cap["degraded"]))
            self.assertTrue(any("未发现 Event" in n for n in cap["notes"]))


class DegradeTests(unittest.TestCase):
    def test_missing_stream_id_degrades_to_single_stream(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("matmul", 200, None, 0, 6),
                _ev("matmul", 200, None, 4, 6),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(_metrics(cap)["stream_count"], 1)
            self.assertTrue(any("无法区分 stream" in x for x in cap["degraded"]))
            self.assertEqual(cap["confidence"], "low")

    def test_no_device_events_produces_empty_result(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _write(d, [_ev("aten::matmul", 100, 100, 0, 5, "cpu_op")])
            out_html, out_json = d / "r.html", d / "r.json"
            run = subprocess.run(
                [sys.executable, str(STREAM), str(d), "--out-html", str(out_html),
                 "--json", str(out_json)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["mode"], "single")
            self.assertIn("未发现设备 kernel", env["summary"])
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(any("未发现设备 kernel" in x for x in env["data_quality"]["degraded"]))
            self.assertTrue(out_html.exists())

    def test_invalid_input_writes_error_page_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out_html, out_json = d / "err.html", d / "err.json"
            run = subprocess.run(
                [sys.executable, str(STREAM), str(d / "nope"),
                 "--out-html", str(out_html), "--json", str(out_json)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(run.returncode, 3)
            self.assertIn("stream 分析失败", out_html.read_text(encoding="utf-8"))
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertIn("分析失败", env["summary"])
            self.assertEqual(env["data_quality"]["confidence"], "low")


class ModeAndReportTests(unittest.TestCase):
    def test_single_mode_has_no_delta(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _overlap_trace(d)
            out_html, out_json = d / "r.html", d / "r.json"
            run = subprocess.run(
                [sys.executable, str(STREAM), str(d), "--out-html", str(out_html),
                 "--json", str(out_json)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["mode"], "single")
            self.assertEqual(st.validate(env), [])
            for m in env["metrics"]:
                self.assertIsNone(m["cand"])
                self.assertIsNone(m["delta"])
                self.assertEqual(m["verdict"], "info")
            self.assertEqual(len(env["per_step"]), 1)
            self.assertIn("单份画像", env["summary"])
            html = out_html.read_text(encoding="utf-8")
            self.assertIn("<svg", html)
            self.assertIn("每流忙碌占比", html)
            self.assertIn("跨流依赖清单", html)

    def test_pair_mode_delta_and_topology(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _overlap_trace(d / "base")
            _write(d / "cand", [
                _ev("ProfilerStep#0", 100, 100, 0, 10, "cpu_op"),
                _ev("matmul", 200, 1, 0, 10),
            ])
            out_html, out_json = d / "r.html", d / "r.json"
            run = subprocess.run(
                [sys.executable, str(STREAM), str(d / "base"), str(d / "cand"),
                 "--out-html", str(out_html), "--json", str(out_json)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["mode"], "pair")
            self.assertEqual(st.validate(env), [])
            by_name = {m["name"]: m for m in env["metrics"]}
            self.assertEqual(by_name["stream_count"]["base"], 2)
            self.assertEqual(by_name["stream_count"]["cand"], 1)
            self.assertAlmostEqual(by_name["stream_concurrency_avg"]["delta"], 0.2,
                                   places=4)
            self.assertIn(by_name["stream_concurrency_avg"]["verdict"],
                          ("noise", "info"))
            html = out_html.read_text(encoding="utf-8")
            self.assertIn("流拓扑变化", html)
            self.assertIn("消失", html)
            self.assertIn("逐 step 并发度对比", html)

    def test_deterministic_output(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _overlap_trace(d / "a")
            _overlap_trace(d / "b")
            runs = []
            for i in range(2):
                js = d / f"r{i}.json"
                subprocess.run(
                    [sys.executable, str(STREAM), str(d / "a"), str(d / "b"),
                     "--out-html", str(d / f"r{i}.html"), "--json", str(js)],
                    capture_output=True, text=True, timeout=60, check=True,
                )
                runs.append(json.loads(js.read_text(encoding="utf-8")))
            self.assertEqual(runs[0], runs[1])

    def test_a1_evidence_and_units(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _overlap_trace(d)
            env = st.run(d)
            names = [m["name"] for m in env["metrics"]]
            self.assertEqual(names, [
                "stream_count", "stream_busy_pct", "stream_busy_max_pct",
                "stream_concurrency_avg", "stream_concurrency_max",
                "stream_concurrency_per_step", "stream_cross_dep_count",
            ])
            units = {m["name"]: m["unit"] for m in env["metrics"]}
            self.assertEqual(units["stream_busy_pct"], "pct")
            self.assertEqual(units["stream_concurrency_avg"], "count")
            for m in env["metrics"]:
                self.assertTrue(m["evidence"])
                self.assertTrue(m["name"].startswith("stream_"))


class RegressionTests(unittest.TestCase):
    def test_copy_tasks_are_not_stream_kernels(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("ProfilerStep#0", 1, 1, 0, 10, "cpu_op"),
                _ev("MatMul", 1, 100, 0, 6, None,
                    {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
                _ev("SDMA_SQE", 1, 47, 0, 2, None,
                    {"Task Type": "SDMA_SQE", "Physic Stream Id": 47}),
                _ev("MEMCPY_ASYNC", 1, 48, 1, 2, None,
                    {"Task Type": "PCIE_DMA_SQE", "Physic Stream Id": 48}),
                _ev("Memcpy", 1, 49, 2, 2, None,
                    {"task type": "Memcpy", "Physic Stream Id": 49}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(cap["metrics"]["stream_count"], 1)
            self.assertEqual(len(cap["kernels"]), 1)
            self.assertEqual(cap["streams"][0]["stream"], "100")

    def test_event_id_prefers_notify_id(self):
        self.assertEqual(st._event_id({"args": {"id": 1, "notify_id": 7}}), "7")
        self.assertEqual(st._event_id({"args": {"Event ID": 9}}), "9")

    def test_low_pairing_rate_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            events = [
                _ev("ProfilerStep#0", 1, 1, 0, 100, "cpu_op"),
                _ev("MatMul", 1, 1, 0, 10, None,
                    {"Task Type": "AI_CORE", "Physic Stream Id": 1}),
            ]
            for i in range(4):
                events.append(_ev("Notify_Wait", 1, 2, 20 + i, 0.1, None,
                                  {"task type": "Notify_Wait", "notify_id": f"w{i}",
                                   "stream id": 2}))
            _write(Path(d), events)
            cap = st.analyze_capture(Path(d))
            self.assertEqual(cap["metrics"]["stream_cross_dep_count"], 0)
            self.assertTrue(any("配对率偏低" in n for n in cap["notes"]))

    def test_pair_notes_are_tagged_per_side(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _overlap_trace(d / "base")
            _overlap_trace(d / "cand")
            env = st.build_envelope(st.analyze_capture(d / "base"),
                                    st.analyze_capture(d / "cand"), 0.05)
            self.assertTrue(any(n.startswith("base: ") for n in env["notes"]))
            self.assertTrue(any(n.startswith("cand: ") for n in env["notes"]))

    def test_default_does_not_write_json(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _overlap_trace(d / "cap")
            run = subprocess.run(
                [sys.executable, str(STREAM), str(d / "cap")], cwd=str(d),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertFalse((d / "stream_result.json").exists())
            self.assertTrue((d / "stream_report.html").exists())


class ReviewFixTests(unittest.TestCase):
    def test_pairing_rate_counts_same_stream_pairs(self):
        """配对率度量 id 匹配质量：全同流也应 100%，跨流计数单列。"""
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("matmul", 200, 1, 0, 5),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"eventId": "1", "streamId": "1"}),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"eventId": "2", "streamId": "1"}),
                _ev("Event Wait", 100, 100, 3, 0.1, "Event Wait",
                    {"eventId": "1", "streamId": "1"}),
                _ev("Event Wait", 100, 100, 4, 0.1, "Event Wait",
                    {"eventId": "2", "streamId": "1"}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(cap["metrics"]["stream_cross_dep_count"], 0)
            self.assertEqual(cap["deps"]["pairing_rate"], 100.0)
            self.assertFalse(any("配对率偏低" in n for n in cap["notes"]))

    def test_pairing_rate_mixed_cross_stream(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("matmul", 200, 1, 0, 5),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"eventId": "1", "streamId": "1"}),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"eventId": "2", "streamId": "1"}),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"eventId": "3", "streamId": "1"}),
                _ev("Event Wait", 100, 100, 3, 0.1, "Event Wait",
                    {"eventId": "1", "streamId": "1"}),
                _ev("Event Wait", 100, 100, 3, 0.1, "Event Wait",
                    {"eventId": "2", "streamId": "2"}),
                _ev("Event Wait", 100, 100, 3, 0.1, "Event Wait",
                    {"eventId": "3", "streamId": "2"}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(cap["metrics"]["stream_cross_dep_count"], 2)
            self.assertEqual(cap["deps"]["pairing_rate"], 100.0)

    def test_stream_arg_priority_is_order_independent(self):
        a = {"args": {"streamId": "A", "Physic Stream Id": "B"}, "tid": 9}
        b = {"args": {"Physic Stream Id": "B", "streamId": "A"}, "tid": 9}
        self.assertEqual(st._stream_of(a), "B")
        self.assertEqual(st._stream_of(b), "B")
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("matmul", 200, 1, 0, 5, args={"streamId": "A", "Physic Stream Id": "B"}),
                _ev("matmul", 200, 2, 0, 5,
                    args={"Physic Stream Id": "B", "streamId": "A"}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(cap["metrics"]["stream_count"], 1)
            self.assertEqual(cap["streams"][0]["stream"], "B")

    def test_pair_per_step_uses_real_step_ids(self):
        with tempfile.TemporaryDirectory() as d:
            base, cand = Path(d) / "base", Path(d) / "cand"
            _write(base, [
                _ev("ProfilerStep#10", 100, 100, 0, 10, "cpu_op"),
                _ev("matmul", 200, 1, 0, 5),
            ])
            _write(cand, [
                _ev("ProfilerStep#11", 100, 100, 0, 10, "cpu_op"),
                _ev("matmul", 200, 1, 0, 5),
            ])
            env = st.run(base, cand)
            rows = {p["step"]: p for p in env["per_step"]}
            self.assertEqual(sorted(rows), [10, 11])
            self.assertIsNotNone(rows[10]["base"])
            self.assertIsNone(rows[10]["cand"])
            self.assertIsNone(rows[10]["delta"])
            self.assertIsNone(rows[11]["base"])
            self.assertIsNotNone(rows[11]["cand"])

    def test_dependency_uses_nearest_prior_record_by_id(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("matmul", 200, 1, 0, 5),
                _ev("Event Record", 100, 100, 1, 0.1, "Event Record",
                    {"eventId": "5", "streamId": "1"}),
                _ev("Event Record", 100, 100, 5, 0.1, "Event Record",
                    {"eventId": "5", "streamId": "2"}),
                _ev("Event Wait", 100, 100, 6, 0.1, "Event Wait",
                    {"eventId": "5", "streamId": "3"}),
            ])
            cap = st.analyze_capture(Path(d))
            self.assertEqual(len(cap["deps"]["pairs"]), 1)
            pair = cap["deps"]["pairs"][0]
            self.assertEqual(pair["record_stream"], "2")
            self.assertEqual(pair["record_ts"], 5000.0)
            self.assertEqual(cap["deps"]["pairing_rate"], 100.0)


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_links_and_assets(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm, "SKILL.md 缺少 frontmatter")
        name = re.search(r"^name:\s*(\S+)", fm.group(1), re.MULTILINE)
        self.assertIsNotNone(name)
        self.assertEqual(name.group(1), SKILL_DIR.name)
        self.assertIn("bubble", fm.group(1))
        self.assertIn("kptools-ascend-profiling-compare", fm.group(1))
        links = re.findall(r"\]\((references/[^)]+)\)", text)
        self.assertGreaterEqual(len(links), 1)
        for target in links:
            self.assertTrue((SKILL_DIR / target).is_file(), f"引用不存在: {target}")
        self.assertTrue((SKILL_DIR / "assets/report.css").is_file())

    def test_eval_queries_cover_positive_and_negative(self):
        data = json.loads((SKILL_DIR / "evals/eval_queries.json").read_text(encoding="utf-8"))
        queries = data["queries"]
        self.assertGreaterEqual(len(queries), 10)
        pos = [q for q in queries if q["should_trigger"]]
        neg = [q for q in queries if not q["should_trigger"]]
        self.assertGreaterEqual(len(pos), 6)
        self.assertGreaterEqual(len(neg), 4)
        for q in neg:
            self.assertTrue(q.get("note"), f"负例缺路由说明: {q['id']}")


class RealSchemaTests(unittest.TestCase):
    """真实 CANN trace：cat=None + args["Task Type"] / "Physic Stream Id"。"""

    def test_task_type_physic_stream_and_notify(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), [
                _ev("ProfilerStep#0", 1, 1, 0, 10, None),
                _ev("MatMul", 1, 100, 0, 6, None,
                    {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
                _ev("Add", 1, 101, 4, 6, None,
                    {"Task Type": "AI_VECTOR_CORE", "Physic Stream Id": 101}),
                _ev("Notify_Record", 1, 10, 1, 0.1, None,
                    {"task type": "Notify_Record", "notify_id": "7", "stream id": 200}),
                _ev("Notify_Wait", 1, 11, 3, 0.1, None,
                    {"task type": "Notify_Wait", "notify_id": "7", "stream id": 201}),
            ])
            cap = st.analyze_capture(Path(d))
            m = _metrics(cap)
            self.assertEqual(m["stream_count"], 2)
            self.assertEqual(m["stream_concurrency_max"], 2)
            self.assertEqual(m["stream_cross_dep_count"], 1)


if __name__ == "__main__":
    unittest.main()
