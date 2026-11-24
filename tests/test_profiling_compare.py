"""kptools-ascend-profiling-compare 的契约测试。"""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPARE = (
    ROOT
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare/scripts/compare.py"
)
SPEC = importlib.util.spec_from_file_location("kpc_compare", COMPARE)
cmp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cmp)


def _load_core():
    spec = importlib.util.spec_from_file_location(
        "kpc_core_t", COMPARE.parent / "kpc_core.py"
    )
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    return core


def _write_comm_db(root, values):
    """values: {step_id: (elapse, wait, transit, sync)}，单位 ms。"""
    con = sqlite3.connect(root / "ASCEND_PROFILER_OUTPUT" / "analysis.db")
    con.execute(
        "CREATE TABLE CommAnalyzerTime(hccl_op_name TEXT, group_name TEXT,"
        " start_timestamp REAL, elapse_time REAL, transit_time REAL, wait_time REAL,"
        " synchronization_time REAL, idle_time REAL, step TEXT, type TEXT)"
    )
    for sid, (e, w, t, sy) in values.items():
        con.execute(
            "INSERT INTO CommAnalyzerTime VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"hcom_allReduce__{sid}", "g", 0, e, t, w, sy, 0, f"step{sid}", "collective"),
        )
    con.commit()
    con.close()


def _write_native(root, steps, kernels, comm=None, api=None, with_stack=None):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out / "analysis.db")
    con.execute(
        "CREATE TABLE StepTraceTime(deviceId INTEGER, step INTEGER, computing REAL,"
        "communication_not_overlapped REAL, overlapped REAL, communication REAL,"
        "free REAL, stage REAL, bubble REAL,"
        "communication_not_overlapped_and_exclude_receive REAL, preparing REAL)"
    )
    for s in steps:
        con.execute(
            "INSERT INTO StepTraceTime VALUES(0,?,?,?,0,?,?,?,0,?,?)",
            (
                s["id"],
                s["comp"],
                s["comm_no"],
                s["comm"],
                s["free"],
                s["stage"],
                s["comm_no"],
                s["prep"],
            ),
        )
    con.commit()
    con.close()

    con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
    con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
    for s in steps:
        start = 1_700_000_000_000_000_000 + s["id"] * 20_000_000
        # 真实采集里 STEP_TIME 跨度 = stage + preparing
        span = int((s["stage"] + s.get("prep", 0.0)) * 1000)
        con.execute("INSERT INTO STEP_TIME VALUES(?,?,?)", (s["id"], start, start + span))
    con.commit()
    con.close()

    with open(out / "kernel_details.csv", "w", encoding="utf-8") as f:
        f.write("Step Id,Name,Type,Duration(us),Start Time(us)\n")
        for sid, name, dur, typ in kernels:
            f.write(f"{sid},{name},{typ},{dur},1700000000000000.0\n")

    if comm:
        (out / "communication.json").write_text(json.dumps(comm), encoding="utf-8")
    if api:
        with open(out / "api_statistic.csv", "w", encoding="utf-8") as f:
            f.write("Device_id,Level,API Name,Time(us),Count,Avg(us),Min(us),Max(us)\n")
            for name, t, c in api:
                f.write(f"host,acl,{name},{t},{c},{t / max(c, 1)},0,0\n")

    info = {
        "torch_npu_version": "2.10.0.post4",
        "cann_version": "9.1.0",
        "rank_id": 0,
        "config": {"common_config": {"with_stack": with_stack}},
    }
    (root / "profiler_info_0.json").write_text(json.dumps(info), encoding="utf-8")


def _add_device(root, steps, device_id=1):
    out = root / "ASCEND_PROFILER_OUTPUT"
    con = sqlite3.connect(out / "analysis.db")
    for s in steps:
        con.execute(
            "INSERT INTO StepTraceTime VALUES(?,?,?,?,0,?,?,?,0,?,?)",
            (
                device_id,
                s["id"],
                s["comp"],
                s["comm_no"],
                s["comm"],
                s["free"],
                s["stage"],
                s["comm_no"],
                s["prep"],
            ),
        )
    con.commit()
    con.close()


def _steps_for(scale, n=2):
    out = []
    for i in range(n):
        stage = (10000 + 2000 * i) * scale
        comp = (6000 + 1000 * i) * scale
        comm_no = (1000 + 200 * i) * scale
        out.append(
            {
                "id": i,
                "comp": comp,
                "comm_no": comm_no,
                "comm": (1200 + 200 * i) * scale,
                "free": stage - comp - comm_no,
                "stage": stage,
                "prep": (500 + 100 * i) * scale,
            }
        )
    return out


def _ev(name, pid, tid, ts_us, dur_us, cat=None, args=None):
    e = {"ph": "X", "name": name, "pid": pid, "tid": tid,
         "ts": f"{ts_us:.3f}", "dur": dur_us}
    if cat:
        e["cat"] = cat
    if args:
        e["args"] = args
    return e


def _write_trace(root, events):
    root.mkdir(parents=True, exist_ok=True)
    (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")


def _write_trace_from_steps(root, steps):
    """按 native 夹具的 step 口径造一份纯 trace（ProfilerStep + Overlap Analysis）。"""
    events = []
    for s in steps:
        ts0 = s["id"] * 20_000.0  # µs，与 _write_native 的 STEP_TIME 起点一致
        span = s["stage"] + s["prep"]
        events.append(_ev(f"ProfilerStep#{s['id']}", 100, 100, ts0, span, "cpu_op"))
        cur = ts0 + s["prep"]
        for name, dur in (
            ("Computing", s["comp"]),
            ("Communication(Not Overlapped)", s["comm_no"]),
            ("Communication", s["comm"]),
        ):
            if dur > 0:
                events.append(_ev(name, 300, 300, cur, dur, "Overlap Analysis"))
            if name != "Communication":
                cur += dur
        free = span - s["prep"] - s["comp"] - s["comm_no"]
        if free > 0:
            events.append(_ev("Free", 300, 300, cur, free, "Overlap Analysis"))
    _write_trace(root, events)


def _profiled_trace(root, scale=1):
    step = 10_000.0
    out = []
    for i in range(2):
        base = i * step
        out.append(_ev(f"ProfilerStep#{i}", 100, 100, base, step, "cpu_op"))
        out.append(_ev("aten::matmul", 100, 100, base + 500, 10, "cpu_op"))
        out.append(_ev("Computing", 300, 300, base + 600, 4000 * scale,
                       "Overlap Analysis"))
        out.append(_ev("matmul", 200, 1, base + 700, 1000 * scale, "AI_CORE"))
    _write_trace(root, out)


def _comm_trace(root):
    out = []
    for i in range(3):
        base = i * 20_000.0
        out.append(_ev("aten::matmul", 100, 100, base + 200, 10, "cpu_op"))
        out.append(_ev("matmul", 200, 1, base + 600, 4000, "AI_CORE"))
        out.append(_ev("hcom_allReduce_", 200, 2, base + 5000, 3000, "COMMUNICATION"))
        out.append(_ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 300, 300,
                       base + 15_000, 100, "cann"))
    _write_trace(root, out)


def _sync_trace(root):
    out = [
        _ev("Event::synchronize", 100, 100, 1000, 5, "cpu_op"),
        _ev("aten::matmul", 100, 100, 5000, 10, "cpu_op"),
        _ev("matmul", 200, 1, 6000, 1000, "AI_CORE"),
        _ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 300, 300, 20000, 100, "cann"),
        _ev("aten::matmul", 100, 100, 21000, 10, "cpu_op"),
        _ev("matmul", 200, 1, 25000, 1000, "AI_CORE"),
        _ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 300, 300, 40000, 100, "cann"),
        _ev("AscendCL@aclrtSynchronizeDeviceWithTimeout", 300, 300, 40050, 10, "cann"),
    ]
    _write_trace(root, out)


def _single_event_trace(root):
    _write_trace(root, [
        _ev("aten::matmul", 100, 100, 5000, 10, "cpu_op"),
        _ev("matmul", 200, 1, 6000, 1000, "AI_CORE"),
    ])


class NativeCompareTests(unittest.TestCase):
    def test_aligned_compare_reports_improvement(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "base"
            cand = d / "cand"
            kernels_b = [(0, "matmul", 5000, "AI_CORE"), (1, "matmul", 5000, "AI_CORE")]
            kernels_c = [(0, "matmul", 4000, "AI_CORE"), (1, "matmul", 4000, "AI_CORE")]
            _write_native(base, _steps_for(1), kernels_b,
                          comm={"step0": {"collective": {}, "p2p": {}},
                                "step1": {"collective": {}, "p2p": {}}},
                          api=[("aclnnMatmul", 1000, 2)], with_stack=True)
            _write_native(cand, _steps_for(0.8), kernels_c,
                          comm={"step0": {"collective": {}, "p2p": {}},
                                "step1": {"collective": {}, "p2p": {}}},
                          api=[("aclnnMatmul", 800, 2)], with_stack=True)

            result = cmp.compare_captures(cmp.load_capture(base), cmp.load_capture(cand))
            self.assertEqual(result["mode"], "aligned")
            self.assertEqual(result["summary"]["pair_count"], 2)
            self.assertGreater(result["summary"]["dur"]["delta_ms"], 0)
            self.assertIn("候选更快", result["summary"]["overall_verdict"])
            self.assertTrue(result["kernels"]["improved"])

            out = d / "report.html"
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(base), str(cand), "--out-html", str(out)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            html = out.read_text(encoding="utf-8")
            self.assertIn("候选更快", html)
            self.assertIn("matmul", html)

    def test_identical_captures_are_within_noise(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            kernels = [(0, "matmul", 5000, "AI_CORE"), (1, "matmul", 5000, "AI_CORE")]
            for root in (a, b):
                _write_native(root, _steps_for(1), kernels, with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertIn("未观察到实质变化", result["summary"]["overall_verdict"])

    def test_comm_analyzer_time_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            steps = _steps_for(1)
            for root in (a, b):
                _write_native(root, steps, [], with_stack=True)
            _write_comm_db(a, {0: (10.0, 4.0, 1.0, 2.0), 1: (12.0, 5.0, 1.5, 2.5)})
            _write_comm_db(b, {0: (20.0, 8.0, 2.0, 4.0), 1: (24.0, 10.0, 3.0, 5.0)})
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertIsNotNone(result["comm"])
            self.assertAlmostEqual(result["comm"]["elapse"]["base_ms"], 11.0, places=3)
            self.assertAlmostEqual(result["comm"]["elapse"]["cand_ms"], 22.0, places=3)
            self.assertAlmostEqual(result["comm"]["wait"]["delta_ms"], -4.5, places=3)
            self.assertIn("analysis.db:CommAnalyzerTime", result["base"]["sidecars"])
            self.assertIn("analysis.db:CommAnalyzerTime", result["cand"]["sidecars"])

    def test_arbitrary_step_pair(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1), [(0, "matmul", 5000, "AI_CORE")], with_stack=True)
            _write_native(b, _steps_for(1), [(0, "matmul", 5000, "AI_CORE")], with_stack=True)
            result = cmp.compare_captures(
                cmp.load_capture(a), cmp.load_capture(b), base_step=1, cand_step=0
            )
            self.assertEqual(result["mode"], "single")
            self.assertEqual(result["pairs"][0]["base_id"], 1)
            self.assertEqual(result["pairs"][0]["cand_id"], 0)
            # 仍是两份采集，§7 信封契约下 mode 必须是 pair（single 只用于单份采集分析）
            self.assertEqual(cmp.to_envelope(result)["mode"], "pair")

    def test_whole_window_rejects_explicit_steps(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1, 3), [], with_stack=True)
            _single_event_trace(b)
            base_cap, cand_cap = cmp.load_capture(a), cmp.load_capture(b)
            self.assertTrue(cand_cap["whole_window"])
            with self.assertRaises(ValueError):
                cmp.compare_captures(base_cap, cand_cap, base_step=1, cand_step=0)
            with self.assertRaises(ValueError):
                cmp.compare_captures(base_cap, cand_cap, base_steps=[0], cand_steps=[0])

    def test_multi_device_keeps_components_additive(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            steps = _steps_for(1)
            for root in (a, b):
                _write_native(root, steps, [], with_stack=True)
                _add_device(root, steps, device_id=1)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertTrue(result["summary"]["components_comparable"])
            for pair in result["pairs"]:
                total = sum(pair["base"][k] for k in ("comp", "comm_no", "free", "prep"))
                self.assertAlmostEqual(total, pair["base_dur_ms"], places=2)
            for pct in result["summary"]["bottleneck"]["base_pcts"].values():
                self.assertLessEqual(pct, 100.0)
            self.assertTrue(
                any("Device_id" in c and "主卡" in c for c in result["caveats"])
            )

    def test_non_additive_components_are_downgraded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            steps = _steps_for(1)
            _write_native(a, steps, [], with_stack=True)
            _write_native(b, steps, [], with_stack=True)
            con = sqlite3.connect(a / "ASCEND_PROFILER_OUTPUT" / "analysis.db")
            con.execute("UPDATE StepTraceTime SET free = free + stage")
            con.commit()
            con.close()
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertFalse(result["summary"]["components_comparable"])
            self.assertTrue(any("可加性" in c for c in result["caveats"]))
            for key in ("comp", "comm_no", "free", "prep"):
                self.assertEqual(result["summary"]["components"][key]["verdict"], "info")

    def test_group_mode_keeps_selected_steps(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1, 4), [], with_stack=True)
            _write_native(b, _steps_for(1, 4), [], with_stack=True)
            result = cmp.compare_captures(
                cmp.load_capture(a), cmp.load_capture(b),
                base_steps=[0, 1], cand_steps=[2, 3],
            )
            self.assertEqual(result["mode"], "group")
            self.assertEqual([p["base_id"] for p in result["pairs"]], [0, 1])
            self.assertEqual([p["cand_id"] for p in result["pairs"]], [2, 3])

    def test_group_step_count_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1, 4), [], with_stack=True)
            _write_native(b, _steps_for(1, 2), [], with_stack=True)
            with self.assertRaises(ValueError):
                cmp.compare_captures(
                    cmp.load_capture(a), cmp.load_capture(b),
                    base_steps=[0, 1, 2, 3], cand_steps=[0, 1],
                )

    def test_skip_warmup_drops_leading_steps(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1, 4), [], with_stack=True)
            _write_native(b, _steps_for(1, 4), [], with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b), skip_warmup=2)
            self.assertEqual([p["base_id"] for p in result["pairs"]], [2, 3])
            self.assertTrue(any("warmup" in x for x in result["caveats"]))

    def test_skip_warmup_with_explicit_steps_raises(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1, 2), [], with_stack=True)
            _write_native(b, _steps_for(1, 2), [], with_stack=True)
            with self.assertRaises(ValueError):
                cmp.compare_captures(
                    cmp.load_capture(a), cmp.load_capture(b),
                    base_step=0, cand_step=0, skip_warmup=1,
                )

    def test_one_sided_comm_is_caveated(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            comm = {"step0": {"collective": {"allreduce": {"Communication Time Info": {
                "Elapse Time(ms)": 10, "Wait Time(ms)": 4}}}, "p2p": {}}}
            _write_native(a, _steps_for(1), [], comm=comm, with_stack=True)
            _write_native(b, _steps_for(1), [], with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertTrue(any("仅一侧提供通信数据" in x for x in result["caveats"]))

    def test_large_preparing_becomes_dominant(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            steps = [{"id": 0, "comp": 48000, "comm_no": 2000, "comm": 2500,
                      "free": 4000, "stage": 54000, "prep": 335000}]
            _write_native(a, steps, [], with_stack=True)
            _write_native(b, steps, [], with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertEqual(result["summary"]["bottleneck"]["base"], "Preparing")
            self.assertIn("Preparing", result["summary"]["bottleneck"]["base_pcts"])

    def test_comm_total_op_not_double_counted(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            comm = {
                "step0": {
                    "collective": {
                        "Total Op Info": {
                            "Communication Time Info": {
                                "Elapse Time(ms)": 10,
                                "Wait Time(ms)": 4,
                                "Transit Time(ms)": 6,
                                "Synchronization Time(ms)": 2,
                            }
                        },
                        "allreduce": {
                            "Communication Time Info": {
                                "Elapse Time(ms)": 10,
                                "Wait Time(ms)": 4,
                                "Transit Time(ms)": 6,
                            }
                        },
                    },
                    "p2p": {},
                },
                "step1": {
                    "collective": {
                        "allreduce": {
                            "Communication Time Info": {
                                "Elapse Time(ms)": 5,
                                "Wait Time(ms)": 1,
                            }
                        }
                    },
                    "p2p": {},
                },
            }
            _write_native(root, _steps_for(1), [], comm=comm, with_stack=True)
            cap = cmp.load_capture(root)
            self.assertEqual(cap["comm"][0]["elapse"], 10)
            self.assertEqual(cap["comm"][0]["wait"], 4)
            self.assertEqual(cap["comm"][0]["transit"], 6)
            self.assertEqual(cap["comm"][0]["sync"], 2)
            self.assertEqual(cap["comm"][1]["elapse"], 5)
            self.assertEqual(cap["comm"][1]["wait"], 1)

    def test_api_normalized_by_capture_steps_in_single_mode(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            kernels = [(0, "matmul", 5000, "AI_CORE")]
            _write_native(a, _steps_for(1), kernels,
                          api=[("aclnnMatmul", 1000, 2)], with_stack=True)
            _write_native(b, _steps_for(1), kernels,
                          api=[("aclnnMatmul", 800, 2)], with_stack=True)
            result = cmp.compare_captures(
                cmp.load_capture(a), cmp.load_capture(b), base_step=0, cand_step=1
            )
            row = next(r for r in result["api"] if r["name"] == "aclnnMatmul")
            self.assertAlmostEqual(row["base_ms_per_step"], 0.5, places=3)
            self.assertAlmostEqual(row["cand_ms_per_step"], 0.4, places=3)
            self.assertTrue(any("Host API" in x for x in result["caveats"]))


class TraceOnlyTests(unittest.TestCase):
    def test_profiler_step_trace_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _profiled_trace(a)
            _profiled_trace(b)
            out = d / "report.html"
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(a), str(b), "--out-html", str(out)],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(out.exists())
            self.assertIn("步耗时", out.read_text(encoding="utf-8"))

    def test_no_profiler_step_without_stack_trace_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _sync_trace(a)
            _sync_trace(b)
            cap = cmp.load_capture(a)
            self.assertGreaterEqual(len(cap["steps"]), 2)
            self.assertFalse(cap["has_stack"])
            out = d / "report.html"
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(a), str(b), "--out-html", str(out)],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(out.exists())

    def test_no_stack_self_built_has_comm_not_overlapped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a = d / "a"
            _comm_trace(a)
            cap = cmp.load_capture(a)
            self.assertGreater(len(cap["steps"]), 0)
            self.assertGreater(cap["steps"][0]["comm_no"], 0)
            self.assertAlmostEqual(
                cap["steps"][0]["comm_no"], cap["steps"][0]["comm"], places=3
            )

    def test_no_steps_degrades_to_whole_window(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _single_event_trace(a)
            _single_event_trace(b)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertEqual(result["mode"], "whole_window")
            self.assertTrue(any("整窗" in x for x in result["caveats"]))

    def test_whole_window_one_sided_comm_is_caveated(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            comm = {"step0": {"collective": {"allreduce": {"Communication Time Info": {
                "Elapse Time(ms)": 10, "Wait Time(ms)": 4}}}, "p2p": {}}}
            _write_native(base, _steps_for(1), [], comm=comm, with_stack=True)
            _single_event_trace(cand)
            result = cmp.compare_captures(cmp.load_capture(base), cmp.load_capture(cand))
            self.assertEqual(result["mode"], "whole_window")
            self.assertTrue(result["base"]["has_comm"])
            self.assertFalse(result["cand"]["has_comm"])
            self.assertIsNotNone(result["comm"])
            self.assertTrue(
                any("仅一侧提供通信数据" in c for c in result["caveats"])
            )

    def test_whole_window_without_comm_has_no_comm_diff(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _single_event_trace(a)
            _single_event_trace(b)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertEqual(result["mode"], "whole_window")
            self.assertIsNone(result["comm"])
            self.assertFalse(any("仅一侧提供通信数据" in c for c in result["caveats"]))

    def test_invalid_input_still_produces_error_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "error.html"
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(d / "nope"), str(d / "none"),
                 "--out-html", str(out)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(run.returncode, 3)
            self.assertTrue(out.exists())
            self.assertIn("对比失败", out.read_text(encoding="utf-8"))


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_and_reference_links(self):
        import re

        skill_dir = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare"
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm, "SKILL.md 缺少 frontmatter")
        name = re.search(r"^name:\s*(\S+)", fm.group(1), re.MULTILINE)
        self.assertIsNotNone(name)
        self.assertEqual(name.group(1), skill_dir.name)
        self.assertTrue(re.search(r"^description:\s*\S", fm.group(1), re.MULTILINE | re.DOTALL))

        links = re.findall(r"\]\((references/[^)]+)\)", text)
        self.assertGreaterEqual(len(links), 1)
        for target in links:
            self.assertTrue((skill_dir / target).is_file(), f"引用不存在: {target}")
        self.assertTrue((skill_dir / "assets/report.css").is_file())


class EnvelopeContractTests(unittest.TestCase):
    """§7 统一 JSON 信封契约（hub 依赖）。"""

    def _native_pair(self, d, scale=1.0, n=2):
        base, cand = d / "base", d / "cand"
        _write_native(base, _steps_for(1, n), [(0, "matmul", 5000, "AI_CORE")], with_stack=True)
        _write_native(cand, _steps_for(scale, n), [(0, "matmul", 4000, "AI_CORE")],
                      with_stack=True)
        return base, cand

    def test_envelope_matches_schema_v1(self):
        with tempfile.TemporaryDirectory() as d:
            base, cand = self._native_pair(Path(d), 0.8)
            result = cmp.compare_captures(cmp.load_capture(base), cmp.load_capture(cand))
            env = cmp.to_envelope(result)

            self.assertEqual(cmp.validate(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare")
            self.assertEqual(env["schema_version"], 1)
            self.assertEqual(env["mode"], "pair")
            self.assertEqual(
                [m["name"] for m in env["metrics"]],
                ["step_time_ms", "computing_ms", "communication_not_overlapped_ms",
                 "free_ms", "preparing_ms"],
            )
            for m in env["metrics"]:
                self.assertIn(m["verdict"], ("improved", "regressed", "noise", "info"))
                self.assertTrue(m["evidence"])
                self.assertIsInstance(m["noise_band"], float)
                self.assertIn("paired_delta", m)
            self.assertEqual(len(env["per_step"]), result["summary"]["pair_count"])
            self.assertIsInstance(env["summary"], str)
            self.assertTrue(any("噪声判定" in n for n in env["notes"]))

    def test_validate_rejects_inconsistent_fields(self):
        with tempfile.TemporaryDirectory() as d:
            base, cand = self._native_pair(Path(d), 0.8)
            env = cmp.to_envelope(cmp.compare_captures(cmp.load_capture(base),
                                                       cmp.load_capture(cand)))
            bad = json.loads(json.dumps(env))
            bad["metrics"][0]["delta"] = bad["metrics"][0]["delta"] + 1.0
            self.assertTrue(cmp.validate(bad))
            bad2 = json.loads(json.dumps(env))
            bad2["metrics"][0]["sign_consistency"] = 2.0
            self.assertTrue(cmp.validate(bad2))
            bad3 = json.loads(json.dumps(env))
            bad3["inputs"]["cand"] = None
            self.assertTrue(cmp.validate(bad3))

    def test_envelope_validate_cli(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = self._native_pair(d, 0.8)
            out_json = d / "result.json"
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(base), str(cand),
                 "--json", str(out_json)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            cli = subprocess.run(
                [sys.executable, str(COMPARE.parent / "envelope.py"), str(out_json)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            env["metrics"][0]["delta"] += 1.0
            out_json.write_text(json.dumps(env), encoding="utf-8")
            cli = subprocess.run(
                [sys.executable, str(COMPARE.parent / "envelope.py"), str(out_json)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(cli.returncode, 1)

    def test_sidecars_and_confidence_native(self):
        with tempfile.TemporaryDirectory() as d:
            base, cand = self._native_pair(Path(d), n=3)
            env = cmp.to_envelope(cmp.compare_captures(cmp.load_capture(base),
                                                       cmp.load_capture(cand)))
            sidecars = env["data_quality"]["sidecars"]
            self.assertIn("analysis.db", sidecars)
            self.assertIn("profiler_info_0.json", sidecars)
            self.assertEqual(env["data_quality"]["confidence"], "high")
            self.assertEqual(env["data_quality"]["degraded"], [])

    def test_trace_only_envelope_is_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "a", d / "b"
            _profiled_trace(base)
            _profiled_trace(cand)
            env = cmp.to_envelope(cmp.compare_captures(cmp.load_capture(base),
                                                       cmp.load_capture(cand)))
            self.assertEqual(cmp.validate(env), [])
            self.assertIn("trace_view.json", env["data_quality"]["sidecars"])
            self.assertFalse(
                [x for x in env["data_quality"]["degraded"] if "索引不可用" in x],
                "build_index 应可用，出现索引降级说明依赖脚本路径失效",
            )
            self.assertTrue(env["data_quality"]["degraded"])
            self.assertEqual(env["data_quality"]["confidence"], "medium")

    def test_whole_window_confidence_is_low(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "a", d / "b"
            _single_event_trace(base)
            _single_event_trace(cand)
            env = cmp.to_envelope(cmp.compare_captures(cmp.load_capture(base),
                                                       cmp.load_capture(cand)))
            self.assertEqual(env["mode"], "pair")
            self.assertEqual(env["data_quality"]["confidence"], "low")
            self.assertTrue(any("整窗" in x for x in env["data_quality"]["degraded"]))

    def test_cli_json_writes_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = self._native_pair(d, 0.8)
            out_json = d / "result.json"
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(base), str(cand),
                 "--json", str(out_json), "--out-html", str(d / "r.html")],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(cmp.validate(env), [])
            self.assertTrue(env["metrics"])
            self.assertIn("improved", {m["verdict"] for m in env["metrics"]})

    def test_dependency_scripts_resolve_in_any_layout(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("kpc_core_layout", COMPARE.parent / "kpc_core.py")
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        for skill, rel in (
            ("kptools-ascend-profiling-analysis", "scripts/build_index.py"),
            ("kptools-ascend-profiling-analysis-nostack", "scripts/detect_steps.py"),
        ):
            path = core._skill_script(skill, rel)
            self.assertTrue(path.is_file(), f"{skill}/{rel} 未解析到（当前布局={path}）")

    def test_core_importable_standalone(self):
        import importlib.util

        core_path = COMPARE.parent / "kpc_core.py"
        self.assertTrue(core_path.is_file())
        spec = importlib.util.spec_from_file_location("kpc_core_standalone", core_path)
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        self.assertTrue(hasattr(core, "load_capture"))
        self.assertTrue(hasattr(core, "_metric_block"))


class RealWorldRegressions(unittest.TestCase):
    """真实采集暴露的问题：步耗时口径、跨路径一致性、双峰噪声、算子名漂移。"""

    @staticmethod
    def _step(sid, dur_us, prep=1000.0):
        comp = dur_us * 0.6
        comm_no = dur_us * 0.1
        return {
            "id": sid,
            "comp": comp,
            "comm_no": comm_no,
            "comm": comm_no * 1.1,
            "free": dur_us - comp - comm_no,
            "stage": dur_us - prep,
            "prep": prep,
        }

    def test_step_time_includes_preparing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            _write_native(
                root,
                [{"id": 0, "comp": 6000, "comm_no": 1000, "comm": 1200,
                  "free": 3000, "stage": 10000, "prep": 1370}],
                [],
                with_stack=True,
            )
            cap = cmp.load_capture(root)
            self.assertAlmostEqual(cap["steps"][0]["dur"], 11370.0, places=1)

    def test_large_preparing_is_not_clamped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            _write_native(
                root,
                [{"id": 0, "comp": 48000, "comm_no": 2000, "comm": 2500,
                  "free": 4000, "stage": 54000, "prep": 335000}],
                [],
                with_stack=True,
            )
            cap = cmp.load_capture(root)
            self.assertAlmostEqual(cap["steps"][0]["dur"], 389000.0, places=1)
            self.assertAlmostEqual(cap["steps"][0]["prep"], 335000.0, places=1)

    def test_uniform_improvement_detected_with_three_steps(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1.0, 3), [], with_stack=True)
            _write_native(b, _steps_for(0.85, 3), [], with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertEqual(result["summary"]["dur"]["verdict"], "改善")

    def test_heterogeneous_steps_are_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            base = [self._step(i, d_) for i, d_ in enumerate([534_000, 559_000, 105_000, 561_000])]
            cand = [self._step(i, d_) for i, d_ in enumerate([3_334_000, 555_000, 3_345_000, 249_000])]
            _write_native(a, base, [], with_stack=True)
            _write_native(b, cand, [], with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            dur = result["summary"]["dur"]
            self.assertTrue(dur["heterogeneous"])
            self.assertEqual(dur["verdict"], "方向不一致")
            self.assertIn("方向不一致", result["summary"]["overall_verdict"])
            env = cmp.to_envelope(result)
            dur_metric = next(m for m in env["metrics"] if m["name"] == "step_time_ms")
            self.assertEqual(dur_metric["verdict"], "info")
            self.assertEqual(env["data_quality"]["confidence"], "low")

    def test_hcom_kernel_names_are_normalized(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1.0), [(0, "hcom_allReduce__503_0_1", 500, "COMMUNICATION")],
                          with_stack=True)
            _write_native(b, _steps_for(1.0), [(0, "hcom_allReduce__503_99_1", 400, "COMMUNICATION")],
                          with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            ranked = result["kernels"]["improved"] + result["kernels"]["regressed"]
            self.assertIn("hcom_allReduce", [r["name"] for r in ranked])
            self.assertFalse(result["kernels"]["one_sided"])

    def test_one_sided_kernels_are_listed_separately(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1.0), [(0, "OnlyBaseKernel", 500, "AI_CORE")], with_stack=True)
            _write_native(b, _steps_for(1.0), [(0, "OnlyCandKernel", 400, "AI_CORE")], with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            ranked = result["kernels"]["improved"] + result["kernels"]["regressed"]
            self.assertNotIn("OnlyBaseKernel", [r["name"] for r in ranked])
            self.assertEqual(
                {r["name"] for r in result["kernels"]["one_sided"]},
                {"OnlyBaseKernel", "OnlyCandKernel"},
            )

    def test_native_and_trace_paths_agree_on_step_time(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, trace_dir = d / "a", d / "a_trace"
            steps = _steps_for(1.0, 3)
            _write_native(a, steps, [], with_stack=True)
            _write_trace_from_steps(trace_dir, steps)
            base, cand = cmp.load_capture(a), cmp.load_capture(trace_dir)
            self.assertEqual(len(base["steps"]), len(cand["steps"]))
            for sb, sc in zip(base["steps"], cand["steps"]):
                self.assertAlmostEqual(sb["dur"] / sc["dur"], 1.0, delta=0.02)
            result = cmp.compare_captures(base, cand)
            self.assertEqual(result["summary"]["dur"]["verdict"], "在噪声内")
            self.assertFalse(result["summary"]["components_comparable"])
            self.assertTrue(any("跨族" in x for x in result["caveats"]))


class DegradationTests(unittest.TestCase):
    def _native_pair(self, d, scale=1.0, n=2):
        return EnvelopeContractTests._native_pair(self, d, scale, n)

    """缺依赖、缺四分类、无忙碌区间时的降级行为。"""

    def test_no_busy_intervals_does_not_fabricate_preparing(self):
        core = _load_core()
        m = core._metrics_from_classes({}, (0, 0, 1_000_000))
        self.assertEqual(m["prep"], 0.0)
        self.assertAlmostEqual(m["free"], 1000.0, places=3)

    def test_missing_nostack_dependency_degrades(self):
        core = _load_core()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            _profiled_trace(root)
            original = core._nostack_module
            core._nostack_module = lambda: (_ for _ in ()).throw(
                FileNotFoundError("缺少依赖脚本 detect_steps.py")
            )
            try:
                cap = core.load_capture(root)
            finally:
                core._nostack_module = original
            self.assertTrue(any("nostack" in x for x in cap["degradations"]))
            self.assertGreaterEqual(len(cap["steps"]), 1)

    def test_classes_unavailable_suppresses_component_verdicts(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1), [], with_stack=True)
            _write_native(b, _steps_for(0.8), [], with_stack=True)
            base, cand = cmp.load_capture(a), cmp.load_capture(b)
            base["class_source"] = "none"
            cand["class_source"] = "none"
            result = cmp.compare_captures(base, cand)
            self.assertFalse(result["summary"]["components_comparable"])
            for key in ("comp", "comm_no", "free", "prep"):
                self.assertEqual(result["summary"]["components"][key]["verdict"], "info")
            self.assertTrue(any("无法获得设备任务分类" in x for x in result["caveats"]))
            self.assertIn("四分类不可用", result["summary"]["bottleneck"]["text"])

    def test_negative_skip_warmup_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            base, cand = self._native_pair(Path(d), 0.8)
            with self.assertRaises(ValueError):
                cmp.compare_captures(cmp.load_capture(base), cmp.load_capture(cand),
                                     skip_warmup=-2)

    def test_one_sided_api_is_caveated_and_not_zeroed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = d / "a", d / "b"
            _write_native(a, _steps_for(1), [], api=[("aclnnMatmul", 1000, 2)], with_stack=True)
            _write_native(b, _steps_for(0.8), [], with_stack=True)
            result = cmp.compare_captures(cmp.load_capture(a), cmp.load_capture(b))
            self.assertTrue(any("仅一侧提供 Host API" in c for c in result["caveats"]))
            self.assertTrue(all(r["delta_ms"] is None for r in result["api"]))

    def test_validate_non_numeric_sign_consistency_returns_error(self):
        with tempfile.TemporaryDirectory() as d:
            base, cand = self._native_pair(Path(d), 0.8)
            result = cmp.compare_captures(cmp.load_capture(base), cmp.load_capture(cand))
            env = cmp.to_envelope(result)
            env["metrics"][0]["sign_consistency"] = "abc"
            errs = cmp.validate(env)
            self.assertTrue(any("sign_consistency" in e for e in errs))

    def test_api_diff_marks_incomparable_when_denominators_differ(self):
        base = {"api": {"x": {"time": 1000, "count": 1}}}
        cand = {"api": {"x": {"time": 2000, "count": 1}}}
        rows = cmp._api_diff(base, cand, 4, 1, comparable=False)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["comparable"])
        self.assertIsNone(rows[0]["delta_ms"])

    def test_output_path_protection_rejects_same_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = self._native_pair(d, 0.8)
            same = d / "same.html"
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(base), str(cand),
                 "--json", str(same), "--out-html", str(same)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(same.exists())

    def test_output_path_protection_rejects_input_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = self._native_pair(d, 0.8)
            target = base / "ASCEND_PROFILER_OUTPUT" / "analysis.db"
            before = target.read_bytes()
            run = subprocess.run(
                [sys.executable, str(COMPARE), str(base), str(cand),
                 "--out-html", str(target)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
