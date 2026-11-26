"""kptools-ascend-profiling-compare-bubble 的契约测试。"""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUBBLE = (
    ROOT
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-bubble/scripts/bubble.py"
)
SPEC = importlib.util.spec_from_file_location("kpc_bubble", BUBBLE)
bub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bub)


def _ev(name, ts_us, dur_us, cat=None, pid=200, tid=1):
    e = {
        "ph": "X",
        "name": name,
        "pid": pid,
        "tid": tid,
        "ts": ts_us,
        "dur": dur_us,
    }
    if cat:
        e["cat"] = cat
    return e


def _write_trace(root, events):
    root.mkdir(parents=True, exist_ok=True)
    (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def _write_native(root, steps, events):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    _write_trace(out, events)

    con = sqlite3.connect(out / "analysis.db")
    con.execute(
        "CREATE TABLE StepTraceTime(deviceId INTEGER, step INTEGER, computing REAL,"
        " communication_not_overlapped REAL, communication REAL, free REAL, stage REAL)"
    )
    for s in steps:
        con.execute(
            "INSERT INTO StepTraceTime VALUES(0,?,?,0,?,?,?)",
            (
                s["id"],
                s["comp"],
                s["comm"],
                s["free"],
                s["stage"],
            ),
        )
    con.commit()
    con.close()

    con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
    con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
    for s in steps:
        con.execute(
            "INSERT INTO STEP_TIME VALUES(?,?,?)",
            (s["id"], int(s["start_us"] * 1000), int(s["end_us"] * 1000)),
        )
    con.commit()
    con.close()

    info = {
        "torch_npu_version": "2.10.0.post4",
        "cann_version": "9.1.0",
        "rank_id": 0,
        "config": {"common_config": {"with_stack": False}},
    }
    (root / "profiler_info_0.json").write_text(json.dumps(info), encoding="utf-8")


def _two_bubble_trace():
    return [
        _ev("matmul", 0, 1000, "AI_CORE"),
        _ev("matmul", 3000, 1000, "AI_CORE"),
        _ev("matmul", 4500, 4500, "AI_CORE"),
        _ev("matmul", 10500, 1000, "AI_CORE"),
    ]


def _three_class_trace():
    return [
        _ev("hcom_allReduce_", 0, 1000, "COMMUNICATION"),
        _ev("matmul", 3000, 1000, "AI_CORE"),
        _ev("matmul", 6000, 1000, "AI_CORE"),
        _ev("matmul", 9000, 1000, "AI_CORE"),
        _ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 11000, 500, "cann", pid=100, tid=100),
        _ev("matmul", 12000, 1000, "AI_CORE"),
    ]


class FallbackParserTests(unittest.TestCase):
    def test_key_order_quoted_dur_and_task_type_isolation(self):
        text = (
            '['
            '{"name":"aten::matmul","pid":100,"tid":100,"ts":"1000","dur":"10","ph":"X"},'
            '{"ph": "X","name":"kernel","pid":200,"tid":1,"ts":"9000","dur":"1000",'
            '"args":{"Task Type":"AI_CORE"}},'
            '{"name":"kernel2","pid":200,"tid":2,"ts":"12000","dur":2000,'
            '"ph":"X","cat":"AI_CORE"}'
            ']'
        )
        events, meta = bub._parse_events_fallback(text)
        self.assertEqual(len(events), 3)
        self.assertEqual(meta["markers"], 3)
        self.assertEqual(meta["parsed"], 3)
        host = events[0]
        self.assertEqual(host[2], "aten::matmul")
        self.assertEqual(host[1], 10_000)          # "dur": "10" µs -> ns
        self.assertNotEqual(host[3], "AI_CORE")    # 不串染下一事件的 Task Type
        self.assertEqual(events[1][3], "AI_CORE")

    def test_read_events_force_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "trace_view.json"
            p.write_text(
                '[{"name":"aten::matmul","pid":100,"tid":100,"ts":"1000",'
                '"dur":"10","ph":"X"},'
                '{"ph":"X","name":"k","pid":200,"tid":1,"ts":"9000","dur":"1000",'
                '"args":{"Task Type":"AI_CORE"}}]',
                encoding="utf-8",
            )
            events, meta = bub._read_events(p, force_fallback=True)
            self.assertEqual(len(events), 2)
            self.assertTrue(meta["fallback"])
            self.assertEqual(events[1][3], "AI_CORE")


class MultiRankTests(unittest.TestCase):
    def _write_rank(self, root, rank, bubble_us):
        out = root / f"rank{rank}" / "ASCEND_PROFILER_OUTPUT"
        out.mkdir(parents=True, exist_ok=True)
        events = [
            _ev("ProfilerStep#0", 0, 60_000, "cpu_op"),
            _ev("matmul", 0, 1000, "AI_CORE"),
            _ev("matmul", 1000 + bubble_us, 1000, "AI_CORE"),
        ]
        (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
        (out / "profiler_info_0.json").write_text(
            json.dumps({"torch_npu_version": "2.10.0", "cann_version": "9.1.0",
                        "rank_id": rank}),
            encoding="utf-8",
        )

    def test_multi_rank_is_deterministic_and_noted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "capture"
            # 故意先创建 rank1，验证选择不依赖 rglob/文件系统顺序。
            self._write_rank(root, 1, 50_000)
            self._write_rank(root, 0, 1_000)
            cap = bub.load_capture(root)
            self.assertEqual(cap["selected_rank"], 0)
            self.assertEqual(cap["rank"], 0)
            self.assertEqual(cap["available_ranks"], [0, 1])
            self.assertTrue(any("仅分析 rank=0" in n for n in cap["notes"]))
            self.assertTrue(any("仅分析 rank=0" in x for x in cap["degradations"]))
            # rank0 的气泡约 1ms，rank1 的约 50ms；确认用的是 rank0。
            bubbles, _info = bub.detect_bubbles(cap)
            self.assertAlmostEqual(bubbles[0]["dur_ms"], 1.0, places=1)

            result = bub.compare_captures(cap, cap)
            env = bub.to_json_envelope(result)
            self.assertEqual(env["base"]["selected_rank"], 0)
            self.assertEqual(env["base"]["available_ranks"], [0, 1])
            self.assertEqual(result["data_quality"]["confidence"], "medium")
            self.assertTrue(any("仅分析 rank=0" in x for x in result["data_quality"]["degraded"]))

    def test_explicit_rank_and_missing_rank(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "capture"
            self._write_rank(root, 0, 1_000)
            self._write_rank(root, 1, 50_000)
            cap = bub.load_capture(root, rank=1)
            self.assertEqual(cap["selected_rank"], 1)
            self.assertEqual(cap["rank"], 1)
            with self.assertRaises(ValueError):
                bub.load_capture(root, rank=3)


class DetectTests(unittest.TestCase):
    def test_hundred_k_event_scale_is_fast(self):
        n = 100_000
        busy = [(i * 2_000_000, i * 2_000_000 + 1_000_000) for i in range(n)]
        cap = {
            "busy": busy,
            "window": (0, busy[-1][1]),
            "busy_items": [(s, e, "matmul", "AI_CORE") for s, e in busy],
            "busy_comm": [],
            "sync_events": [],
            "steps": [
                {"id": i, "start_ns": i * 20_000_000, "end_ns": (i + 1) * 20_000_000}
                for i in range(10_000)
            ],
        }
        started = time.monotonic()
        bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=0, adjacent_ms=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(len(bubbles), n - 1)
        self.assertLess(elapsed, 5.0, f"10 万事件检测耗时 {elapsed:.2f}s")

        started = time.monotonic()
        series, scale = bub._series_for(cap, bubbles, "total")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(scale, 10_000)
        self.assertEqual(len(series), 10_000)

    def test_two_bubble_positions_and_durations(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(root, _two_bubble_trace())
            cap = bub.load_capture(root)
            bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=1.0, adjacent_ms=1.0)

            self.assertEqual(len(bubbles), 2)
            self.assertAlmostEqual(bubbles[0]["start_ns"] / 1000.0, 1000.0, places=3)
            self.assertAlmostEqual(bubbles[0]["end_ns"] / 1000.0, 3000.0, places=3)
            self.assertAlmostEqual(bubbles[0]["dur_ms"], 2.0, places=4)
            self.assertAlmostEqual(bubbles[1]["start_ns"] / 1000.0, 9000.0, places=3)
            self.assertAlmostEqual(bubbles[1]["end_ns"] / 1000.0, 10500.0, places=3)
            self.assertAlmostEqual(bubbles[1]["dur_ms"], 1.5, places=4)
            # 0.5ms 的窗口低于 --min-gap-ms，不应召回
            self.assertFalse(
                any(abs(b["dur_ms"] - 0.5) < 1e-6 for b in bubbles)
            )

    def test_three_classification_cases(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(root, _three_class_trace())
            cap = bub.load_capture(root)
            bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=1.0, adjacent_ms=1.0)

            by_start = {round(b["start_ns"] / 1000.0): b["cls"] for b in bubbles}
            self.assertEqual(by_start.get(1000), "comm_wait")
            self.assertEqual(by_start.get(4000), "compute")
            self.assertEqual(by_start.get(7000), "compute")
            self.assertEqual(by_start.get(10000), "sync")

            result = bub.compare_captures(cap, min_gap_ms=1.0, adjacent_ms=1.0)
            m = {x["name"]: x for x in result["metrics"]}
            self.assertAlmostEqual(m["bubble_total_ms"]["base"], 8.0, places=4)
            self.assertAlmostEqual(m["bubble_comm_wait_ms"]["base"], 2.0, places=4)
            self.assertAlmostEqual(m["bubble_compute_ms"]["base"], 4.0, places=4)
            self.assertAlmostEqual(m["bubble_sync_ms"]["base"], 2.0, places=4)

    def test_no_step_bounds_gives_low_confidence_and_empty_per_step(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(root, _two_bubble_trace())
            result = bub.compare_captures(bub.load_capture(root))
            self.assertEqual(result["data_quality"]["confidence"], "low")
            self.assertEqual(result["per_step"], [])
            self.assertIn("bubble_total_ms", {m["name"] for m in result["metrics"]})
            self.assertEqual(bub.validate(result), [])

    def test_comm_cat_without_hcom_prefix_is_busy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(
                root,
                [
                    _ev("matmul", 0, 1000, "AI_CORE"),
                    _ev("allreduce_1", 1900, 200, "COMMUNICATION"),
                    _ev("matmul", 3000, 1000, "AI_CORE"),
                ],
            )
            cap = bub.load_capture(root)
            bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=1.0, adjacent_ms=1.0)
            self.assertEqual(bubbles, [], "通信 kernel 应计入忙碌区间，不得产生假气泡")

    def test_copy_engines_are_busy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(
                root,
                [
                    _ev("matmul", 0, 1000, "AI_CORE"),
                    _ev("SDMA_SQE", 1900, 100, "SDMA_SQE"),
                    _ev("PCIE_DMA_SQE", 2050, 100, "PCIE_DMA_SQE"),
                    _ev("matmul", 3000, 1000, "AI_CORE"),
                ],
            )
            cap = bub.load_capture(root)
            bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=1.0, adjacent_ms=1.0)
            self.assertEqual(bubbles, [], "拷贝引擎任务应计入忙碌区间，不得产生假气泡")

    def test_max_bubble_ms_filters_long_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(
                root,
                [
                    _ev("matmul", 0, 1000, "AI_CORE"),
                    _ev("matmul", 3000, 2000, "AI_CORE"),
                    _ev("matmul", 10000, 1000, "AI_CORE"),
                ],
            )
            cap = bub.load_capture(root)
            bubbles, info = bub.detect_bubbles(
                cap, min_gap_ms=1.0, adjacent_ms=1.0, max_bubble_ms=3.0
            )
            self.assertEqual(len(bubbles), 1)
            self.assertAlmostEqual(bubbles[0]["dur_ms"], 2.0, places=4)
            self.assertEqual(info["filtered_long"], 1)


class DegradeTests(unittest.TestCase):
    def test_overlap_analysis_communication_is_comm_wait(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(
                root,
                [
                    _ev("Communication", 0, 1000, "Overlap Analysis"),
                    _ev("Computing", 3000, 1000, "Overlap Analysis"),
                ],
            )
            cap = bub.load_capture(root)
            self.assertEqual(cap["busy_source"], "trace_view.json:overlap_analysis")
            bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=1.0, adjacent_ms=1.0)
            self.assertEqual(len(bubbles), 1)
            self.assertAlmostEqual(bubbles[0]["dur_ms"], 2.0, places=4)
            self.assertEqual(bubbles[0]["cls"], "comm_wait")

            result = bub.compare_captures(cap)
            evidence = {m["name"]: m["evidence"] for m in result["metrics"]}
            self.assertIn("Overlap Analysis", evidence["bubble_total_ms"])
            self.assertNotIn("AI_TASK_TYPES", evidence["bubble_total_ms"])

    def test_overlap_analysis_without_cat_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(
                root,
                [
                    _ev("Communication", 0, 1000),
                    _ev("Computing", 3000, 1000),
                ],
            )
            cap = bub.load_capture(root)
            self.assertEqual(cap["busy_source"], "trace_view.json:overlap_analysis")
            bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=1.0, adjacent_ms=1.0)
            self.assertEqual(len(bubbles), 1)
            self.assertEqual(bubbles[0]["cls"], "comm_wait")

    def test_step_trace_time_approximation_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            steps = [
                {"id": 0, "start_us": 0, "end_us": 10000, "comp": 4000, "comm": 0,
                 "free": 6000, "stage": 10000},
                {"id": 1, "start_us": 10000, "end_us": 20000, "comp": 4000, "comm": 1000,
                 "free": 5000, "stage": 10000},
            ]
            _write_native(
                root,
                steps,
                [_ev("aten::matmul", 0, 10, "cpu_op", pid=100, tid=100)],
            )
            cap = bub.load_capture(root)
            self.assertEqual(cap["busy_source"], "analysis.db:StepTraceTime(近似)")
            self.assertTrue(any("近似" in x for x in cap["degradations"]))
            bubbles, _info = bub.detect_bubbles(cap, min_gap_ms=1.0, adjacent_ms=1.0)
            self.assertEqual(len(bubbles), 1)
            self.assertAlmostEqual(bubbles[0]["dur_ms"], 6.0, places=3)
            self.assertEqual(bubbles[0]["cls"], "compute")

    def test_no_device_events_returns_empty_bubbles_without_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(
                root,
                [
                    _ev("aten::matmul", 0, 10, "cpu_op", pid=100, tid=100),
                    _ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 100, 50, "cann",
                        pid=100, tid=100),
                ],
            )
            cap = bub.load_capture(root)
            bubbles, _info = bub.detect_bubbles(cap)
            self.assertEqual(bubbles, [])
            self.assertTrue(any("无 device 事件" in x for x in cap["degradations"]))

            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = subprocess.run(
                [sys.executable, str(BUBBLE), str(root), "--out-html", str(out_html),
                 "--json", str(out_json)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["mode"], "single")
            self.assertTrue(env["data_quality"]["degraded"])
            self.assertTrue(env["metrics"])
            self.assertEqual(env["metrics"][0]["base"], 0.0)
            self.assertIn("未检出气泡", out_html.read_text(encoding="utf-8"))

    def test_unreadable_input_writes_error_html_and_json_rc3(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out_html = d / "err.html"
            out_json = d / "err.json"
            run = subprocess.run(
                [sys.executable, str(BUBBLE), str(d / "nope"), str(d / "none"),
                 "--out-html", str(out_html), "--json", str(out_json)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 3)
            self.assertTrue(out_html.exists())
            self.assertIn("设备气泡检测失败", out_html.read_text(encoding="utf-8"))
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["metrics"], [])
            self.assertTrue(env["data_quality"]["degraded"])

    def test_rejects_overwriting_input_trace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            _write_trace(root, _two_bubble_trace())
            target = root / "trace_view.json"
            before = target.read_bytes()
            run = subprocess.run(
                [sys.executable, str(BUBBLE), str(root), "--out-html", str(target),
                 "--json", str(Path(d) / "r.json")],
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(target.read_bytes(), before)

    def test_single_mode_has_no_delta(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            steps = [
                {"id": 0, "start_us": 0, "end_us": 10000, "comp": 5000, "comm": 0,
                 "free": 5000, "stage": 10000},
                {"id": 1, "start_us": 10000, "end_us": 20000, "comp": 5000, "comm": 0,
                 "free": 5000, "stage": 10000},
            ]
            events = _two_bubble_trace() + [_ev("matmul", 21000, 1000, "AI_CORE")]
            _write_native(root, steps, events)
            out_html = Path(d) / "r.html"
            out_json = Path(d) / "r.json"
            run = subprocess.run(
                [sys.executable, str(BUBBLE), str(root), "--out-html", str(out_html),
                 "--json", str(out_json)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["mode"], "single")
            self.assertEqual(env["data_quality"]["confidence"], "high")
            for m in env["metrics"]:
                self.assertIsNone(m["cand"])
                self.assertIsNone(m["delta"])
                self.assertEqual(m["verdict"], "info")
            self.assertEqual(len(env["per_step"]), 2)
            self.assertTrue(all(p["cand"] is None for p in env["per_step"]))
            self.assertTrue(out_html.exists())


class PairTests(unittest.TestCase):
    def _large_cap(self, n=100_000, steps=5_000):
        busy = [(i * 2_000_000, i * 2_000_000 + 1_000_000) for i in range(n)]
        return {
            "label": "large",
            "path": "/tmp/large",
            "kind": "trace_view",
            "busy_source": "trace_view.json:device_events",
            "step_source": "STEP_TIME",
            "window": (0, busy[-1][1]),
            "busy": busy,
            "busy_items": [(s, e, "matmul", "AI_CORE") for s, e in busy],
            "busy_comm": [],
            "sync_events": [],
            "steps": [
                {"id": i, "start_ns": i * 40_000_000, "end_ns": (i + 1) * 40_000_000}
                for i in range(steps)
            ],
            "sidecars": ["trace_view.json"],
            "notes": [],
            "degradations": [],
            "version": "2.10.0",
            "cann_version": "9.1.0",
            "rank": 0,
        }

    def test_json_and_html_output_are_bounded(self):
        cap_b = self._large_cap()
        cap_c = self._large_cap()
        result = bub.compare_captures(cap_b, cap_c)
        self.assertNotIn("busy", result["base"])
        env = bub.to_json_envelope(result)
        raw = json.dumps(env)
        self.assertLess(len(raw), 2_000_000)
        self.assertNotIn("busy_items", raw)
        self.assertNotIn('"bubbles"', raw)

        html = bub.render_html(result, cap_b, cap_c)
        self.assertLess(len(html), 2_000_000)
        self.assertLessEqual(html.count('class="tl-seg'), 2_000)


    def test_count_metric_has_unit_floor(self):
        steps = [{"id": i, "start_ns": i, "end_ns": i + 1} for i in range(3)]
        base_bubbles = [{"step": i, "dur_ms": 1.0} for i in range(3)]
        cand_bubbles = [
            {"step": i, "dur_ms": 1.0}
            for i in range(3)
            for _ in range(2)
        ]
        m = bub._build_metric(
            "bubble_count", "count", "count",
            {"steps": steps}, base_bubbles, {"steps": steps}, cand_bubbles,
            False, 0.05, 1.0, "bubbles.json:每个空闲窗口计 1",
        )
        self.assertEqual(m["verdict"], "noise")
        self.assertEqual(m["noise_band"], 1.0)
        self.assertIn("count noise floor=1.0", m["evidence"])

    def test_verdict_does_not_depend_on_step_count(self):
        def metric_for(n):
            steps = [{"id": i, "start_ns": i, "end_ns": i + 1} for i in range(n)]
            base_bubbles, cand_bubbles = [], []
            for i in range(n):
                # 每步稳定改善 1ms，但步间方差很大（10/20 交替）。
                base_bubbles.append({"step": i, "dur_ms": 10.0 if i % 2 == 0 else 20.0})
                cand_bubbles.append({"step": i, "dur_ms": 9.0 if i % 2 == 0 else 19.0})
            cap_b = {"steps": steps}
            cap_c = {"steps": steps}
            return bub._build_metric(
                "bubble_total_ms", "ms", "total",
                cap_b, base_bubbles, cap_c, cand_bubbles, False, 0.05, 1.0, "test",
            )

        two = metric_for(2)
        twenty = metric_for(20)
        self.assertEqual(two["verdict"], "improved")
        self.assertEqual(twenty["verdict"], "improved")
        self.assertEqual(two["verdict"], twenty["verdict"])
        self.assertAlmostEqual(two["paired_delta"], 1.0, places=6)
        self.assertAlmostEqual(twenty["paired_delta"], 1.0, places=6)

    def test_pair_metrics_and_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "base"
            cand = d / "cand"
            steps = [
                {"id": i, "start_us": i * 10000, "end_us": (i + 1) * 10000,
                 "comp": 6000, "comm": 0, "free": 4000, "stage": 10000}
                for i in range(3)
            ]
            base_events = []
            cand_events = []
            for i in range(3):
                t = i * 10000
                base_events.append(_ev("matmul", t, 3000, "AI_CORE"))
                base_events.append(_ev("matmul", t + 5000, 5000, "AI_CORE"))
                cand_events.append(_ev("matmul", t, 4000, "AI_CORE"))
                cand_events.append(_ev("matmul", t + 5000, 5000, "AI_CORE"))
            _write_native(base, steps, base_events)
            _write_native(cand, steps, cand_events)

            result = bub.compare_captures(
                bub.load_capture(base), bub.load_capture(cand)
            )
            self.assertEqual(result["mode"], "pair")
            self.assertEqual(result["data_quality"]["confidence"], "high")
            self.assertEqual(bub.validate(result), [])

            m = {x["name"]: x for x in result["metrics"]}
            # pair 模式下 metrics 为 per-step 中位数（每步 1 个气泡、2ms/1ms）
            self.assertEqual(m["bubble_count"]["base"], 1.0)
            self.assertEqual(m["bubble_count"]["cand"], 1.0)
            self.assertAlmostEqual(m["bubble_total_ms"]["base"], 2.0, places=3)
            self.assertAlmostEqual(m["bubble_total_ms"]["cand"], 1.0, places=3)
            self.assertGreater(m["bubble_total_ms"]["delta"], 0)
            self.assertEqual(m["bubble_total_ms"]["verdict"], "improved")
            self.assertEqual(len(result["per_step"]), 3)
            self.assertAlmostEqual(result["per_step"][0]["base"], 2.0, places=3)
            self.assertAlmostEqual(result["per_step"][0]["cand"], 1.0, places=3)

            out_html = d / "r.html"
            out_json = d / "r.json"
            run = subprocess.run(
                [sys.executable, str(BUBBLE), str(base), str(cand),
                 "--out-html", str(out_html), "--json", str(out_json)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            html = out_html.read_text(encoding="utf-8")
            for needle in ("气泡时间轴", "Top 10 最长气泡窗口", "分类时长", "降级说明"):
                self.assertIn(needle, html)
            self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["mode"], "pair")


class PerStepMismatchTests(unittest.TestCase):
    def test_step_count_mismatch_adds_note(self):
        def steps(n):
            return [
                {"id": i, "start_us": i * 10000, "end_us": (i + 1) * 10000,
                 "comp": 6000, "comm": 0, "free": 4000, "stage": 10000}
                for i in range(n)
            ]

        def events(n):
            evs = []
            for i in range(n):
                t = i * 10000
                evs.append(_ev("matmul", t, 3000, "AI_CORE"))
                evs.append(_ev("matmul", t + 5000, 5000, "AI_CORE"))
            return evs

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            _write_native(base, steps(3), events(3))
            _write_native(cand, steps(2), events(2))
            result = bub.compare_captures(bub.load_capture(base), bub.load_capture(cand))
            self.assertEqual(len(result["per_step"]), 2)
            self.assertTrue(any("step 数不一致" in n for n in result["notes"]))
            self.assertEqual(bub.validate(result), [])


class ValidatorTests(unittest.TestCase):
    def _env(self, **kw):
        env = {
            "skill": "x", "schema_version": 1, "mode": "single", "inputs": {},
            "data_quality": {"sidecars": [], "degraded": [], "confidence": "low"},
            "metrics": [], "per_step": [], "summary": "s", "notes": [],
        }
        env.update(kw)
        return env

    def test_empty_metrics_rejected(self):
        self.assertTrue(bub.validate(self._env()))

    def test_non_list_sidecars_rejected(self):
        env = self._env()
        env["data_quality"]["sidecars"] = "not-a-list"
        self.assertTrue(bub.validate(env))


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_references_and_evals(self):
        import re

        skill_dir = (
            ROOT
            / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-bubble"
        )
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm, "SKILL.md 缺少 frontmatter")
        name = re.search(r"^name:\s*(\S+)", fm.group(1), re.MULTILINE)
        self.assertEqual(name.group(1), skill_dir.name)
        self.assertTrue((skill_dir / "assets/report.css").is_file())
        self.assertTrue((skill_dir / "references/bubble-definition.md").is_file())

        evals = json.loads((skill_dir / "evals/eval_queries.json").read_text(encoding="utf-8"))
        queries = evals["queries"]
        self.assertGreaterEqual(len(queries), 10)
        self.assertGreaterEqual(sum(1 for q in queries if q["should_trigger"]), 6)
        self.assertGreaterEqual(sum(1 for q in queries if not q["should_trigger"]), 4)


class RealSchemaTests(unittest.TestCase):
    """真实 CANN trace：cat=None，任务类型在 args["Task Type"]。"""

    def test_task_type_kernel_and_default_gap(self):
        def ev(name, ts, dur, cat=None, args=None):
            e = {"ph": "X", "name": name, "pid": 200, "tid": 1, "ts": ts, "dur": dur}
            if cat:
                e["cat"] = cat
            if args:
                e["args"] = args
            return e

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            _write_trace(root, [
                ev("ProfilerStep#0", 0, 10000),
                ev("MatMul", 0, 4000, None, {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
                ev("MatMul", 4010, 5990, None, {"Task Type": "AI_CORE", "Physic Stream Id": 100}),
            ])
            cap = bub.load_capture(root)
            bubbles, info = bub.detect_bubbles(cap)
            total = sum(b["dur_ms"] for b in bubbles)
            # 10µs 间隙在默认阈值（0.01ms）下应被计入，而不是把整步当气泡
            self.assertGreaterEqual(len(bubbles), 1)
            self.assertLess(total, 1.0)
            self.assertAlmostEqual(total, 0.01, places=3)


class NoStepPairTests(unittest.TestCase):
    def _cap(self):
        return {"steps": [], "busy_source": "trace_view.json:device_events"}

    def _bubbles(self, n):
        return [{"step": 0, "dur_ms": 1.0, "cls": "unknown"} for _ in range(n)]

    def test_count_without_steps_reports_true_totals(self):
        # pair 模式无 step 边界时 count 不能恒为 1，应取气泡总数。
        m = bub._build_metric(
            "bubble_count", "count", "count", self._cap(), self._bubbles(3),
            self._cap(), self._bubbles(7), False, 0.05, 1.0, "bubbles.json:count",
        )
        self.assertEqual(m["base"], 3.0)
        self.assertEqual(m["cand"], 7.0)
        self.assertEqual(m["delta"], -4.0)
        self.assertNotEqual(m["verdict"], "noise")

    def test_total_without_steps_uses_sum(self):
        m = bub._build_metric(
            "bubble_total_ms", "ms", "total", self._cap(), self._bubbles(3),
            self._cap(), self._bubbles(7), False, 0.05, 1.0, "bubbles.json:sum(dur_ms)",
        )
        self.assertEqual(m["base"], 3.0)
        self.assertEqual(m["cand"], 7.0)


if __name__ == "__main__":
    unittest.main()
