"""kptools-ascend-profiling-step-split 的合成夹具契约测试。"""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-step-split/scripts/split_steps.py"
)
SPEC = importlib.util.spec_from_file_location("step_split", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def _ev(name, pid, tid, ts_us, dur_us, cat="cpu_op", args=None):
    out = {
        "ph": "X",
        "name": name,
        "pid": pid,
        "tid": tid,
        "ts": f"{ts_us:.3f}",
        "dur": dur_us,
    }
    if cat:
        out["cat"] = cat
    if args:
        out["args"] = args
    return out


def _build_trace(root, periods, kernels_per_step=3, internal_idle_ms=0.0,
                 with_step_marker=True, multi_stream=False,
                 extra_gap_ms=0.0, extra_gap_step=-1):
    """periods: 每步周期（ms）；步内最后留 1ms 空档。返回权威边界（µs）。"""
    events = []
    authority = []
    t_us = 1000.0
    for idx, period_ms in enumerate(periods):
        authority.append(t_us)
        if with_step_marker:
            events.append(_ev(f"ProfilerStep#{idx}", 100, 100, t_us, period_ms * 1000, "cpu_op"))
        if idx == extra_gap_step and extra_gap_ms > 0:
            t_us += extra_gap_ms * 1000
        events.append(_ev("aten::empty", 100, 100, t_us, 5, "cpu_op"))
        events.append(
            _ev("Enqueue@aclnnMatmul", 100, 100, t_us + 10, 8, "enqueue",
                {"correlation_id": 1000 + idx})
        )
        active_end = t_us + (period_ms - 1.0) * 1000
        usable = active_end - (t_us + 20)
        if internal_idle_ms > 0 and idx == 1:
            usable -= internal_idle_ms * 1000
        segment = usable / kernels_per_step
        cur = t_us + 20
        for k in range(kernels_per_step):
            stream = 7 if (multi_stream and k % 2 == 0) else 8
            events.append(
                _ev(
                    f"Kernel_{k}",
                    stream,
                    1,
                    cur,
                    max(1.0, segment * 0.98),
                    "AI_CORE",
                    {"task id": idx * 100 + k, "stream id": stream},
                )
            )
            cur += segment
            if internal_idle_ms > 0 and idx == 1 and k == 0:
                cur += internal_idle_ms * 1000
        t_us += period_ms * 1000
    root.mkdir(parents=True, exist_ok=True)
    (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
    return [int(a) for a in authority]


def _write_step_time(root, authority):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out / "ascend_pytorch_profiler_0.db")
    con.execute("CREATE TABLE STEP_TIME(id INTEGER, startNs INTEGER, endNs INTEGER)")
    for i, start_us in enumerate(authority):
        con.execute(
            "INSERT INTO STEP_TIME VALUES(?,?,?)",
            (i, int(start_us * 1000), int((start_us + 1000) * 1000)),
        )
    con.commit()
    con.close()
    src = root / "trace_view.json"
    dst = out / "trace_view.json"
    dst.write_bytes(src.read_bytes())
    return dst


class StepSplitTests(unittest.TestCase):
    def _run(self, periods, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        authority = _build_trace(root, periods, **kwargs)
        trace = root / "trace_view.json"
        return mod.analyze(str(trace), tolerance_ms=3.0), authority, root

    def test_stable_periodic(self):
        result, authority, _ = self._run([20, 20, 20, 20, 20, 20])
        check = result["self_check"]
        self.assertIsNotNone(check)
        self.assertGreaterEqual(check["f1"], 0.9)
        self.assertIn(result["data_quality"]["confidence"], ("high", "medium"))
        self.assertEqual(len(result["per_step"]), 6)

    def test_jitter_periodic(self):
        result, _, _ = self._run([20, 18, 22, 19, 21, 20, 23])
        self.assertGreaterEqual(result["self_check"]["f1"], 0.9)

    def test_long_idle_between_steps(self):
        result, _, _ = self._run([20, 20, 20, 20], extra_gap_ms=30.0, extra_gap_step=1)
        self.assertGreaterEqual(result["self_check"]["f1"], 0.9)

    def test_internal_idle_limitation(self):
        # 已知限制：step 内部的长空闲会被当成候选边界，F1 略降但不失败。
        result, _, _ = self._run([20, 20, 20, 20, 20], internal_idle_ms=6.0)
        self.assertGreaterEqual(result["self_check"]["f1"], 0.85)

    def test_multi_stream(self):
        result, _, _ = self._run([15, 15, 15, 15], multi_stream=True)
        self.assertGreaterEqual(result["self_check"]["f1"], 0.9)

    def test_no_marker_no_sidecar(self):
        result, _, _ = self._run([20, 20, 20, 20], with_step_marker=False)
        self.assertIsNone(result["self_check"])
        self.assertEqual(len(result["per_step"]), 4)
        self.assertTrue(all(abs(s["dur_ms"] - 20.0) < 1.0 for s in result["per_step"][1:]))
        self.assertEqual(result["mode"], "single")
        self.assertEqual(result["schema_version"], 1)

    def test_profiler_step_not_used_for_recognition(self):
        def run_with_marker(with_marker):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            root = Path(tmp.name)
            events = []
            t = 1000.0
            for i in range(8):
                if with_marker:
                    events.append(_ev(f"ProfilerStep#{i}", 100, 100, t, 20000, "cpu_op"))
                events.append(_ev("aten::add", 100, 100, t, 2000, "cpu_op"))
                events.append(
                    _ev("Enqueue@x", 100, 100, t + 2100, 10, "enqueue",
                        {"correlation_id": i})
                )
                events.append(
                    _ev("Enqueue@y", 100, 100, t + 2200, 10, "enqueue",
                        {"correlation_id": 100 + i})
                )
                t += 20000
            (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
            return mod.analyze(str(root / "trace_view.json"))

        without = run_with_marker(False)
        with_marker = run_with_marker(True)
        self.assertEqual(len(without["per_step"]), len(with_marker["per_step"]))
        self.assertGreaterEqual(len(without["per_step"]), 3)
        self.assertNotEqual(without["data_quality"]["confidence"], "high")
        self.assertNotEqual(with_marker["data_quality"]["confidence"], "high")

    def test_sync_only_fallback(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        events = []
        t = 1000.0
        period = 20000.0
        for i in range(6):
            events.append(_ev(f"ProfilerStep#{i}", 100, 100, t, period, "cpu_op"))
            events.append(
                _ev("Enqueue@x", 100, 100, t + 20, 8, "enqueue", {"correlation_id": i})
            )
            k0 = t + 50
            seg = (period - 60) / 4
            for k in range(4):
                events.append(
                    _ev(f"K{k}", 8, 1, k0 + k * seg, seg, "AI_CORE",
                        {"task id": i * 10 + k})
                )
            events.append(_ev("SynchronizeStream", 100, 100, t + period - 5, 5, "cpu_op"))
            t += period
        (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
        result = mod.analyze(str(root / "trace_view.json"), tolerance_ms=3.0)
        self.assertGreaterEqual(result["self_check"]["f1"], 0.9)
        self.assertGreaterEqual(len(result["per_step"]), 3)
        self.assertTrue(any("sync 兜底" in d for d in result["data_quality"]["degraded"]))

    def test_first_step_confidence_zero(self):
        result, _, _ = self._run([20, 20, 20, 20])
        self.assertEqual(result["per_step"][0]["confidence"], 0.0)
        self.assertTrue(result["per_step"][0]["uncertain"])
        self.assertTrue(all(s["confidence"] > 0 for s in result["per_step"][1:]))

    def test_host_only_confidence_downgraded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _build_trace(root, [20, 20, 20, 20], with_step_marker=False)
        events = json.loads((root / "trace_view.json").read_text(encoding="utf-8"))
        events = [e for e in events if int(e["pid"]) == 100]
        (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
        result = mod.analyze(str(root / "trace_view.json"))
        self.assertIn(result["data_quality"]["confidence"], ("low", "medium"))
        self.assertTrue(any("host" in d for d in result["data_quality"]["degraded"]))

    def test_authority_mode(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        authority = _build_trace(root, [20, 20, 20], with_step_marker=False)
        trace = _write_step_time(root, authority)
        result = mod.analyze(str(trace), force_authority=True)
        self.assertTrue(all("authority" in s["signals"] for s in result["per_step"]))
        self.assertIsNone(result["self_check"])

    def test_contract_fields(self):
        result, _, _ = self._run([20, 20, 20])
        for key in ("skill", "schema_version", "mode", "inputs", "data_quality",
                    "metrics", "per_step", "summary", "notes"):
            self.assertIn(key, result)
        for m in result["metrics"]:
            for key in ("name", "unit", "base", "verdict", "evidence"):
                self.assertIn(key, m)
        for s in result["per_step"]:
            for key in ("step", "start_ms", "end_ms", "dur_ms", "confidence", "signals"):
                self.assertIn(key, s)

    def test_threshold_fallback(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _build_trace(root, [20, 20, 20, 20])
        result = mod.analyze(str(root / "trace_view.json"), min_step_ms=50)
        self.assertGreater(len(result["per_step"]), 1)
        self.assertTrue(any("回退" in d for d in result["data_quality"]["degraded"]))

    def test_host_only_fallback(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        events = []
        t = 1000.0
        for _ in range(4):
            for k in range(4):
                events.append(_ev("aten::add", 100, 100, t + k * 1000, 900, "cpu_op"))
            t += 5000
        (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
        result = mod.analyze(str(root / "trace_view.json"))
        self.assertTrue(result["data_quality"]["degraded"])
        self.assertGreaterEqual(len(result["per_step"]), 1)



class ReviewFixTests(unittest.TestCase):
    def test_device_episodes_preserve_gaps_at_step_threshold(self):
        # 相邻迭代空档都是 2ms；合并阈值（中位数×2=4ms）不得吞掉达到切步阈值的空档。
        events = []
        for i in range(3):
            start = i * 3_000_000
            events.append((start, 1_000_000, f"K{i}", "AI_CORE", 8, 1, {}))
        episodes, gaps = mod.device_episodes(events, host_pid=100,
                                             preserve_gap_ns=int(0.5e6))
        self.assertEqual(len(episodes), 3)
        self.assertEqual([g[2] for g in gaps], [2_000_000, 2_000_000])
        merged, merged_gaps = mod.device_episodes(events, host_pid=100)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged_gaps, [])

    def test_authority_mode_starts_at_first_authority(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            events = [_ev("aten::empty", 100, 100, 0, 100, "cpu_op")]
            for i in range(3):
                t = 5000 + i * 10000
                events.append(_ev(f"K{i}", 8, 1, t, 5000, "AI_CORE",
                                  {"task id": i}))
            (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
            authority_us = [5000, 15000, 25000]
            trace = _write_step_time(root, authority_us)
            result = mod.analyze(str(trace), force_authority=True)
            self.assertAlmostEqual(result["per_step"][0]["start_ms"], 5.0, places=3)
            self.assertNotAlmostEqual(result["per_step"][0]["start_ms"], 0.0, places=3)
            self.assertEqual(len(result["per_step"]), 3)

    def test_self_check_keeps_boundaries_within_tolerance(self):
        # 权威起点 0/1/2/3ms、容差 2ms：只剔除与窗口起点重合的 0，保留 1/2/3 参与匹配。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            events = [_ev("aten::empty", 100, 100, 0, 100, "cpu_op")]
            for i in range(1, 4):
                events.append(_ev(f"K{i}", 8, 1, i * 1000, 500, "AI_CORE",
                                  {"task id": i}))
            (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
            trace = _write_step_time(root, [0, 1000, 2000, 3000])
            captured = {}

            def fake_check(ours, truth, tol):
                captured["truth"] = list(truth)
                return {"authority_count": len(truth), "our_count": len(ours),
                        "matched": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0,
                        "tolerance_ms": tol / 1e6, "max_abs_err_ms": None,
                        "mean_abs_err_ms": None}

            with unittest.mock.patch.object(mod, "self_check", side_effect=fake_check):
                mod.analyze(str(trace), tolerance_ms=2.0)
            self.assertEqual(captured["truth"], [1_000_000, 2_000_000, 3_000_000])

    def test_authority_start_equals_trace_origin(self):
        # ProfilerStep#0 即 trace 首个事件时，不能因 lo < t 严格比较丢掉第一个权威 step。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            events = []
            for i in range(3):
                t = i * 10_000
                events.append(_ev(f"ProfilerStep#{i}", 100, 100, t, 9000, "cpu_op"))
                events.append(_ev(f"K{i}", 8, 1, t + 100, 5000, "AI_CORE",
                                  {"task id": i}))
            (root / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
            trace = _write_step_time(root, [0, 10_000, 20_000])
            result = mod.analyze(str(trace), force_authority=True)
            self.assertEqual(len(result["per_step"]), 3)
            self.assertAlmostEqual(result["per_step"][0]["start_ms"], 0.0, places=3)

    def test_pure_device_trace_keeps_device_tasks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            events = [_ev("K%d" % i, 8, 1, i * 1000, 500, "AI_CORE", {"task id": i})
                      for i in range(40)]
            trace = root / "trace_view.json"
            trace.write_text(json.dumps(events), encoding="utf-8")
            _events, host_pid, _z = mod.load_trace(trace)
            self.assertIsNone(host_pid)
            result = mod.analyze(str(trace))
            self.assertFalse(any("未识别到设备任务" in x
                                 for x in result["data_quality"]["degraded"]))

    def test_output_guard_resolves_directory_input(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            events = [_ev("ProfilerStep#0", 100, 100, 0, 10000, "cpu_op"),
                      _ev("K", 8, 1, 100, 5000, "AI_CORE", {"task id": 1})]
            trace = root / "trace_view.json"
            trace.write_text(json.dumps(events), encoding="utf-8")
            before = trace.read_bytes()
            rc = mod.main([str(root), "--json", str(trace)])
            self.assertNotEqual(rc, 0)
            self.assertEqual(trace.read_bytes(), before)

    def test_output_path_guard_rejects_input_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            events = [_ev("ProfilerStep#0", 100, 100, 0, 10000, "cpu_op"),
                      _ev("K", 8, 1, 100, 5000, "AI_CORE", {"task id": 1})]
            trace = root / "trace_view.json"
            trace.write_text(json.dumps(events), encoding="utf-8")
            before = trace.read_bytes()
            rc = mod.main([str(trace), "--out-html", str(trace)])
            self.assertNotEqual(rc, 0)
            self.assertEqual(trace.read_bytes(), before)

    def test_host_signal_uses_gap_start_reference(self):
        # host 信号与 idle 信号统一取空档起点，不能取恢复点（否则同一物理边界被标两次）。
        events = []
        for k in range(3):
            base = k * 10_000_000
            events.append((base, 1_000_000, "aten::a", "cpu_op", 100, 1, {}))
            events.append((base + 6_000_000, 1000, "Enqueue@x", "enqueue", 100, 1, {}))
            events.append((base + 6_100_000, 1000, "Enqueue@y", "enqueue", 100, 1, {}))
        items = mod.host_signal(events, 100, 0, 30_000_000, int(0.5e6))
        ts = [it["t"] for it in items]
        self.assertIn(1_000_000, ts)
        self.assertNotIn(6_000_000, ts)
        self.assertEqual(len(ts), 3)


if __name__ == "__main__":
    unittest.main()
