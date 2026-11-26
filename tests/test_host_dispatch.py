"""kptools-ascend-profiling-host-dispatch 的合成夹具契约测试。"""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-host-dispatch/scripts/host_dispatch.py"
)
SPEC = importlib.util.spec_from_file_location("host_dispatch", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

BASE_NS = 1_700_000_000_000_000_000
STRINGS = {0: "Enqueue@aclnnMatmul", 1: "aclnnMatmul", 2: "aclrtLaunchKernelWithHostArgs"}


def _metric(result, name):
    for m in result["metrics"]:
        if m["name"] == name:
            return m["base"]
    raise KeyError(name)


def _event(name, pid, tid, ts_ns, dur_ns, cat, corr=None):
    args = {"correlation_id": corr} if corr is not None else {}
    return {
        "ph": "X", "name": name, "pid": pid, "tid": tid,
        "ts": f"{ts_ns / 1000.0:.3f}", "dur": dur_ns / 1000.0,
        "cat": cat, "args": args,
    }


def _write_trace(root):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    events = [
        _event("ProfilerStep#0", 100, 100, BASE_NS, 5_000_000, "cpu_op"),
        _event("aten::matmul", 100, 100, BASE_NS + 1000, 5_000, "cpu_op"),
    ]
    # 两次下发：enqueue 10µs，dequeue 在其结束后 5µs，设备在其后 100µs 启动。
    for i in range(2):
        e0 = BASE_NS + 10_000 + i * 2_000_000
        events.append(_event("Enqueue@aclnnMatmul", 100, 100, e0, 10_000, "enqueue", corr=i + 1))
        events.append(_event("Dequeue@aclnnMatmul", 100, 101, e0 + 15_000, 2_000, "dequeue", corr=i + 1))
        events.append(
            _event("aclnnMatmul", 200, 1, e0 + 115_000, 100_000, "AI_CORE")
        )
        # 设备任务带 task id，便于识别
        events[-1]["args"]["task id"] = i + 1
    (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
    return out


def _write_db(out, task_offset_ns=200_000, with_dequeue=True):
    con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
    con.execute("CREATE TABLE STRING_IDS(id INTEGER, value TEXT)")
    for k, v in STRINGS.items():
        con.execute("INSERT INTO STRING_IDS VALUES(?,?)", (k, v))
    con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
    con.execute("INSERT INTO STEP_TIME VALUES(0,?,?)", (BASE_NS, BASE_NS + 5_000_000))
    con.execute("CREATE TABLE CONNECTION_IDS(id INTEGER, connectionId INTEGER)")
    con.execute("CREATE TABLE TASK(connectionId INTEGER, startNs INTEGER, globalTaskId INTEGER)")
    con.execute("CREATE TABLE COMPUTE_TASK_INFO(globalTaskId INTEGER, name TEXT)")
    con.execute(
        "CREATE TABLE PYTORCH_API(startNs INTEGER, endNs INTEGER, type INTEGER,"
        " connectionId INTEGER, name INTEGER)"
    )
    con.execute(
        "CREATE TABLE CANN_API(startNs INTEGER, endNs INTEGER, connectionId INTEGER,"
        " type INTEGER, name INTEGER)"
    )
    for i in range(2):
        op_start = BASE_NS + 10_000 + i * 2_000_000
        op_end = op_start + 10_000
        con.execute("INSERT INTO CONNECTION_IDS VALUES(?,?)", (10 + i, 500 + i))
        con.execute("INSERT INTO TASK VALUES(?,?,?)", (500 + i, op_end + task_offset_ns, 900 + i))
        con.execute("INSERT INTO COMPUTE_TASK_INFO VALUES(?,?)", (900 + i, "aclnnMatmul"))
        con.execute("INSERT INTO PYTORCH_API VALUES(?,?,?,?,?)", (op_start, op_end, 50001, 10 + i, 1))
        con.execute("INSERT INTO PYTORCH_API VALUES(?,?,?,?,?)", (op_start, op_end, 50002, 10 + i, 0))
        con.execute("INSERT INTO CANN_API VALUES(?,?,?,?,?)", (op_start, op_end, 500 + i, 10000, 2))
    con.commit()
    con.close()


class HostDispatchTests(unittest.TestCase):
    def _run(self, with_db=True, **db_kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        out = _write_trace(root)
        if with_db:
            _write_db(out, **db_kwargs)
        return mod.analyze(str(root))

    def test_native_pairing(self):
        result = self._run()
        self.assertEqual(_metric(result, "enqueue_count"), 2)
        self.assertAlmostEqual(_metric(result, "queue_wait_p50_ms"), 0.005, places=3)
        self.assertAlmostEqual(_metric(result, "dispatch_delay_p50_ms"), 0.2, places=3)
        self.assertAlmostEqual(_metric(result, "pair_rate"), 1.0, places=3)
        self.assertIn("PYTORCH_API", result["data_quality"]["sidecars"][-1])
        self.assertEqual(result["data_quality"]["confidence"], "high")

    def test_trace_only_heuristic(self):
        result = self._run(with_db=False)
        self.assertAlmostEqual(_metric(result, "dispatch_delay_p50_ms"), 0.105, places=3)
        self.assertAlmostEqual(_metric(result, "queue_wait_p50_ms"), 0.005, places=3)
        self.assertTrue(any("启发式" in s for s in result["data_quality"]["sidecars"]))
        self.assertEqual(result["data_quality"]["confidence"], "medium")

    def test_pairing_failure_degrades(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        out = _write_trace(root)
        trace = json.loads((out / "trace_view.json").read_text(encoding="utf-8"))
        for e in trace:
            if e.get("cat") == "dequeue":
                e["args"].pop("correlation_id", None)
        (out / "trace_view.json").write_text(json.dumps(trace), encoding="utf-8")
        result = mod.analyze(str(root))
        self.assertEqual(_metric(result, "queue_wait_p50_ms"), 0.0)
        self.assertTrue(result["data_quality"]["degraded"])
        self.assertIn(result["data_quality"]["confidence"], ("medium", "low"))

    def test_contract_fields(self):
        result = self._run()
        for key in ("skill", "schema_version", "mode", "inputs", "data_quality",
                    "metrics", "per_step", "summary", "notes"):
            self.assertIn(key, result)
        self.assertEqual(result["skill"], "kptools-ascend-profiling-host-dispatch")
        self.assertEqual(result["schema_version"], 1)
        for s in result["per_step"]:
            self.assertIn("overlap_ratio", s)
        self.assertIn("top_enqueue", result["per_op"])
        html = mod.render_html(result)
        self.assertIn("Host 下发", html)

    def test_per_step_present(self):
        result = self._run()
        self.assertGreaterEqual(len(result["per_step"]), 1)
        self.assertTrue(result["per_step"][0]["enqueue_count"] > 0)

    def test_launch_and_per_op_percentiles(self):
        result = self._run()
        self.assertEqual(_metric(result, "launch_count"), 2)
        self.assertAlmostEqual(_metric(result, "launch_p50_ms"), 0.01, places=3)
        self.assertTrue(result["per_op"]["top_launch"])
        for row in result["per_op"]["top_enqueue"]:
            for key in ("enqueue_p50_ms", "enqueue_p95_ms", "enqueue_p99_ms"):
                self.assertIn(key, row)

    def test_notes_and_charts(self):
        result = self._run()
        self.assertTrue(result["notes"])
        self.assertIsNotNone(result["charts"]["enqueue_hist"])
        html = mod.render_html(result)
        self.assertIn("<svg", html)
        self.assertIn("launch 耗时 Top", html)

    def test_overlap_zero_in_fixture(self):
        result = self._run()
        self.assertEqual(_metric(result, "host_device_overlap_ratio"), 0.0)
        self.assertEqual(result["per_step"][0]["overlap_ratio"], 0.0)
        self.assertAlmostEqual(_metric(result, "exposed_dispatch_ms"), 0.02, places=3)
        self.assertAlmostEqual(result["per_step"][0]["exposed_ms"], 0.02, places=3)

    def test_detect_steps_handles_zero_durations(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps, source = mod._detect_steps(
                None, [(1, 0, "x", "cpu_op", 1, 1, {})], Path(tmp)
            )
        self.assertEqual(source, "window")
        self.assertEqual(len(steps), 1)



class ReviewFixTests(unittest.TestCase):
    def _native_dir(self, root, *, step_time=True, task_end=True,
                    op_start=BASE_NS, enq_end=BASE_NS + 5_000,
                    op_end=BASE_NS + 10_000, task_start=BASE_NS + 210_000,
                    task_stop=BASE_NS + 310_000):
        out = root / "ASCEND_PROFILER_OUTPUT"
        out.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
        con.execute("CREATE TABLE STRING_IDS(id INTEGER, value TEXT)")
        for k, v in STRINGS.items():
            con.execute("INSERT INTO STRING_IDS VALUES(?,?)", (k, v))
        con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
        if step_time:
            con.execute("INSERT INTO STEP_TIME VALUES(0,?,?)", (BASE_NS, BASE_NS + 5_000_000))
        con.execute("CREATE TABLE CONNECTION_IDS(id INTEGER, connectionId INTEGER)")
        con.execute("INSERT INTO CONNECTION_IDS VALUES(?,?)", (10, 500))
        if task_end:
            con.execute(
                "CREATE TABLE TASK(connectionId INTEGER, startNs INTEGER, endNs INTEGER,"
                " globalTaskId INTEGER)")
            con.execute("INSERT INTO TASK VALUES(?,?,?,?)", (500, task_start, task_stop, 900))
        else:
            con.execute("CREATE TABLE TASK(connectionId INTEGER, startNs INTEGER, globalTaskId INTEGER)")
            con.execute("INSERT INTO TASK VALUES(?,?,?)", (500, task_start, 900))
        con.execute("CREATE TABLE COMPUTE_TASK_INFO(globalTaskId INTEGER, name TEXT)")
        con.execute("INSERT INTO COMPUTE_TASK_INFO VALUES(?,?)", (900, "aclnnMatmul"))
        con.execute(
            "CREATE TABLE PYTORCH_API(startNs INTEGER, endNs INTEGER, type INTEGER,"
            " connectionId INTEGER, name INTEGER)")
        con.execute("INSERT INTO PYTORCH_API VALUES(?,?,?,?,?)", (op_start, op_end, 50001, 10, 1))
        con.execute("INSERT INTO PYTORCH_API VALUES(?,?,?,?,?)", (op_start, enq_end, 50002, 10, 0))
        con.execute(
            "CREATE TABLE CANN_API(startNs INTEGER, endNs INTEGER, connectionId INTEGER,"
            " type INTEGER, name INTEGER)")
        con.execute("INSERT INTO CANN_API VALUES(?,?,?,?,?)", (op_start, op_end, 500, 10000, 2))
        con.commit()
        con.close()
        return out

    def test_db_only_dir_and_enqueue_based_delay(self):
        # 直接传 ASCEND_PROFILER_OUTPUT（无 trace）也能分析；延迟用实际 enqueue 结束时间。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = self._native_dir(root)
            result = mod.analyze(str(out))
            self.assertEqual(_metric(result, "enqueue_count"), 1)
            self.assertAlmostEqual(_metric(result, "dispatch_delay_p50_ms"), 0.205, places=3)
            self.assertEqual(_metric(result, "pair_rate"), 1.0)

    def test_db_only_uses_task_intervals_for_overlap(self):
        # 仅 db 时设备忙区间来自 TASK 起止，而不是恒为空。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = self._native_dir(root, op_start=BASE_NS, enq_end=BASE_NS + 5_000,
                                   op_end=BASE_NS + 10_000,
                                   task_start=BASE_NS - 1_000, task_stop=BASE_NS + 3_000)
            result = mod.analyze(str(out))
            self.assertAlmostEqual(_metric(result, "host_device_overlap_ratio"), 0.6, places=3)
            self.assertIsNotNone(_metric(result, "exposed_dispatch_ms"))
            self.assertAlmostEqual(result["per_step"][0]["overlap_ratio"], 0.6, places=3)

    def test_pairs_kept_when_step_time_empty(self):
        # STEP_TIME 为空但 CSV 给出 step 边界时，db 的精确配对不能丢。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = self._native_dir(root, step_time=False)
            (out / "kernel_details.csv").write_text(
                "Step Id,Name,Type,Duration(us),Start Time(us)\n"
                "0,aclnnMatmul,AI_CORE,100,1700000000\n", encoding="utf-8")
            result = mod.analyze(str(root))
            self.assertEqual(result["inputs"]["steps"], "kernel_details")
            self.assertEqual(_metric(result, "pair_rate"), 1.0)
            self.assertAlmostEqual(_metric(result, "dispatch_delay_p50_ms"), 0.205, places=3)

    def test_heuristic_pairing_caps_confidence_to_medium(self):
        # trace 启发式配对成功、CSV 分步时，置信度只能 medium。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            out = _write_trace(root)
            (out / "kernel_details.csv").write_text(
                "Step Id,Name,Type,Duration(us),Start Time(us)\n"
                "0,aclnnMatmul,AI_CORE,100,1700000000\n", encoding="utf-8")
            result = mod.analyze(str(root))
            self.assertTrue(any("启发式" in s for s in result["data_quality"]["sidecars"]))
            self.assertEqual(result["data_quality"]["confidence"], "medium")

    def test_dispatch_delay_basis_exported(self):
        # 信封必须给出 delay_basis 的计数与分位数，口径可核对。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = self._native_dir(root)
            result = mod.analyze(str(out))
            basis = result["dispatch_delay_basis"]
            self.assertEqual(basis["enqueue"]["count"], 1)
            self.assertEqual(basis["enqueue"]["negative"], 0)
            self.assertAlmostEqual(basis["enqueue"]["p50_ms"], 0.205, places=3)
            self.assertEqual(basis["negative_excluded"], 0)
            self.assertTrue(any("dispatch_delay_basis" in n for n in result["notes"]))

    def test_negative_delay_excluded_from_percentiles(self):
        # 设备在 op 结束前启动：回退 op 口径得到负延迟，单列计数且不混入分位数。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cap"
            out = self._native_dir(
                root, op_start=BASE_NS, enq_end=BASE_NS + 5_000,
                op_end=BASE_NS + 10_000, task_start=BASE_NS + 2_000,
                task_stop=BASE_NS + 3_000,
            )
            result = mod.analyze(str(out))
            basis = result["dispatch_delay_basis"]
            self.assertEqual(basis["op"]["count"], 1)
            self.assertEqual(basis["op"]["negative"], 1)
            self.assertEqual(basis["negative_excluded"], 1)
            self.assertEqual(_metric(result, "dispatch_delay_p50_ms"), 0.0)
            self.assertTrue(any("负" in n for n in result["notes"]))


if __name__ == "__main__":
    unittest.main()
