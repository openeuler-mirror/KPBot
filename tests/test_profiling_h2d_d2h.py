"""kptools-ascend-profiling-compare-h2d-d2h 的契约测试（合成事件序列）。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-h2d-d2h"
SCRIPT = SKILL / "scripts/memcpy_diff.py"

SPEC = importlib.util.spec_from_file_location("memcpy_mod", SCRIPT)
mem = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mem)


def ev(name, ts, dur, cat="Memcpy", args=None):
    e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 2}
    if args:
        e["args"] = args
    return e


def write_trace(root, events):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace_view.json").write_text(json.dumps(events), encoding="utf-8")
    return out


def write_csv(root, rows):
    out = root / "ASCEND_PROFILER_OUTPUT"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "kernel_details.csv", "w", encoding="utf-8") as f:
        f.write("Step Id,Name,Type,Duration(us),Start Time(us)\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return out


def load(path):
    notes, deg = [], []
    return mem.load_capture(path, notes, deg), notes, deg


def mrow(env, name):
    return next(m for m in env["metrics"] if m["name"] == name)


def trio(root, copies, kernels=None, syncs=None):
    events = [ev("ProfilerStep#0", 0, 10000, cat="cpu_op")]
    for (name, ts, dur, args) in copies:
        a = dict(args or {})
        a.setdefault("task type", "Memcpy")  # 设备拷贝任务：与真实 Memcpy 事件同形
        events.append(ev(name, ts, dur, cat=None, args=a))
    for (ts, dur) in (kernels or []):
        events.append(ev("MatMul", ts, dur, cat="AI_CORE"))
    for (name, ts, dur) in (syncs or []):
        events.append(ev(name, ts, dur, cat="cann"))
    write_trace(root, events)


class MainPathTests(unittest.TestCase):
    def test_direction_bytes_bandwidth_overlap_sync(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            copies = [
                ("Memcpy H2D", 100, 10, {"size": 1000}),
                ("Memcpy D2H", 200, 20, {"size": 2000}),
                ("Memcpy D2D", 300, 5, {"size": 500}),
            ]
            trio(base, copies, kernels=[(0, 1000)],
                 syncs=[("aclrtSynchronizeDeviceWithTimeout", 200, 20)])
            trio(cand, copies, kernels=[(0, 1000)],
                 syncs=[("aclrtSynchronizeDeviceWithTimeout", 200, 20)])

            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = mem.compare(b, c, 0, 0, 1.0)
            env = mem.envelope(b, c, result, bn, cn, bd, cd)

            self.assertEqual(mem.validate(env), [])
            self.assertEqual(mrow(env, "memcpy_count")["base"], 3)
            self.assertEqual(mrow(env, "memcpy_bytes")["base"], 3500)
            for k in ("memcpy_h2d_count", "memcpy_d2h_count", "memcpy_d2d_count"):
                self.assertEqual(mrow(env, k)["base"], 1)
            self.assertAlmostEqual(mrow(env, "memcpy_overlap_pct")["base"], 100.0)
            self.assertAlmostEqual(mrow(env, "memcpy_sync_pct")["base"], 20 / 35 * 100, places=2)
            self.assertGreater(mrow(env, "memcpy_bandwidth_gbps")["base"], 0)
            self.assertEqual(mrow(env, "memcpy_bandwidth_gbps")["verdict"], "info")
            self.assertEqual(env["data_quality"]["confidence"], "high")

    def test_direction_from_cann_enum_and_name(self):
        # CANN aclrtMemcpyKind: 1=H2D, 2=D2H, 3=D2D。
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            trio(root, [
                ("Memcpy", 100, 10, {"kind": 1, "size": 100}),
                ("Memcpy", 200, 10, {"kind": 2, "size": 100}),
                ("Memcpy", 300, 10, {"kind": 3, "size": 100}),
                ("Memcpy D2D", 400, 10, {"size": 100}),
            ], kernels=[(0, 500)])
            cap, _, _ = load(root)
            self.assertEqual([x["direction"] for x in cap["copies"]],
                             ["h2d", "d2h", "d2d", "d2d"])

    def test_unknown_direction_counted(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            trio(root, [("TransData", 100, 10, {"size": 100})], kernels=[(0, 500)])
            cap, _, _ = load(root)
            st = mem.stats(cap)
            self.assertEqual(st["unknown_direction"], 1)
            self.assertEqual(st["directions"]["h2d"], 0)

    def test_memset_assumed_h2d(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            trio(root, [("Memset", 100, 10, {"size": 64})], kernels=[(0, 500)])
            cap, _, _ = load(root)
            self.assertEqual(cap["copies"][0]["direction"], "h2d")


class DegradeTests(unittest.TestCase):
    def test_no_size_keeps_count_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            trio(base, [("Memcpy H2D", 100, 10, {})], kernels=[(0, 500)])
            trio(cand, [("Memcpy H2D", 100, 10, {})], kernels=[(0, 500)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = mem.compare(b, c, 0, 0, 1.0)
            env = mem.envelope(b, c, result, bn, cn, bd, cd)

            self.assertEqual(mrow(env, "memcpy_count")["base"], 1)
            self.assertEqual(mrow(env, "memcpy_bytes")["base"], 0)
            self.assertEqual(mrow(env, "memcpy_size_unknown_count")["base"], 1)
            self.assertEqual(mrow(env, "memcpy_bandwidth_gbps")["base"], 0)

    def test_no_copy_events_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            trio(base, [], kernels=[(0, 500)])
            trio(cand, [], kernels=[(0, 500)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            result = mem.compare(b, c, 0, 0, 1.0)
            env = mem.envelope(b, c, result, bn, cn, bd, cd)

            self.assertEqual(mrow(env, "memcpy_count")["base"], 0)
            self.assertIn("未发现拷贝事件", env["summary"])
            self.assertEqual(env["data_quality"]["confidence"], "medium")

    def test_no_kernel_marks_overlap_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            trio(root, [("Memcpy H2D", 100, 10, {"size": 100})])
            _, _, deg = load(root)
            self.assertTrue(any("kernel" in x for x in deg))

    def test_csv_only_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_csv(root, [(0, "TransData", "MIX_AIC", 20, 1700000000000.0),
                             (0, "MatMul", "AI_CORE", 100, 1700000000100.0)])
            cap, notes, deg = load(root)
            self.assertEqual(cap["source"], "kernel_details.csv")
            self.assertEqual(len(cap["copies"]), 1)
            self.assertTrue(any("kernel_details.csv" in x for x in deg))

    def test_unreadable_input_emits_error_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "err.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(d / "nope"), str(d / "none"),
                                  "--out-html", str(out)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 3)
            self.assertTrue(out.exists())
            self.assertIn("拷贝对比失败", out.read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_cli_json_and_html(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            trio(base, [("Memcpy H2D", 100, 10, {"size": 1000})], kernels=[(0, 500)])
            trio(cand, [("Memcpy H2D", 100, 10, {"size": 500})], kernels=[(0, 500)])
            out_json, out_html = d / "r.json", d / "r.html"
            run = subprocess.run([sys.executable, str(SCRIPT), str(base), str(cand),
                                  "--json", str(out_json), "--out-html", str(out_html)],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(mem.validate(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare-h2d-d2h")
            html = out_html.read_text(encoding="utf-8")
            self.assertIn("方向分布", html)
            self.assertIn("契约置信度", html)


class LayerAndDirectionRegressionTests(unittest.TestCase):
    def _ev(self, name, ts, dur, cat=None, args=None):
        e = {"ph": "X", "name": name, "ts": ts, "dur": dur, "cat": cat, "pid": 1, "tid": 2}
        if args:
            e["args"] = args
        return e

    def _write(self, root, events):
        write_trace(root, events)

    def test_size_byte_key_is_parsed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            self._write(root, [
                self._ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                self._ev("Memcpy", 100, 10, None,
                         {"task type": "Memcpy", "size(Byte)": 16384,
                          "transport type": "SDMA", "link type": "ON_CHIP"}),
            ])
            cap, _, _ = load(root)
            st = mem.stats(cap)
            self.assertEqual(st["bytes"], 16384)
            self.assertEqual(st["size_unknown"], 0)
            self.assertGreater(st["bandwidth_gbps"], 0)
            self.assertEqual(st["directions"]["d2d"], 1)

    def test_numeric_in_name_does_not_set_direction(self):
        self.assertIsNone(mem._dir_of("<built-in method copy_ of Tensor object at 0x585010e0>", {}))
        self.assertIsNone(mem._dir_of("/vllm-workspace/vllm/v1/utils.py(138): copy_to_gpu", {}))

    def test_host_layers_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            self._write(root, [
                self._ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                self._ev("acl_memcpy_host_to_device", 100, 10, "cpu_op"),
                self._ev("Enqueue@acl_memcpy_host_to_device", 110, 5, "enqueue",
                         {"correlation_id": 7}),
                self._ev("Dequeue@acl_memcpy_host_to_device", 115, 5, "dequeue",
                         {"correlation_id": 7}),
                self._ev("AscendCL@aclrtMemcpyAsync", 120, 5, None, {"connection_id": 9}),
                self._ev("/x/utils.py(1): copy_to_gpu", 130, 5, "python_function"),
                self._ev("MEMCPY_ASYNC", 140, 5, None,
                         {"Task Type": "PCIE_DMA_SQE", "connection_id": 9}),
            ])
            cap, notes, _ = load(root)
            self.assertEqual([c["name"] for c in cap["copies"]], ["MEMCPY_ASYNC"])
            self.assertTrue(any("host 侧拷贝方向参考" in n for n in notes))

    def test_empty_capture_reports_zero_instead_of_error(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            for root in (base, cand):
                self._write(root, [self._ev("ProfilerStep#0", 0, 10000, "cpu_op")])
            out_html, out_json = d / "r.html", d / "r.json"
            run = subprocess.run(
                [sys.executable, str(SCRIPT), str(base), str(cand),
                 "--out-html", str(out_html), "--json", str(out_json)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(mrow(env, "memcpy_count")["base"], 0)
            self.assertEqual(env["summary"], "两侧均未发现拷贝事件")
            self.assertEqual(env["data_quality"]["confidence"], "medium")

    def test_one_sided_empty_capture_is_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base, cand = d / "base", d / "cand"
            self._write(base, [self._ev("ProfilerStep#0", 0, 10000, "cpu_op")])
            self._write(cand, [
                self._ev("ProfilerStep#0", 0, 10000, "cpu_op"),
                self._ev("MEMCPY_ASYNC", 100, 10, None,
                         {"Task Type": "PCIE_DMA_SQE", "size(Byte)": 1024}),
            ])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = mem.envelope(b, c, mem.compare(b, c, 0, 0, 1.0), bn, cn, bd, cd)
            self.assertEqual(env["data_quality"]["confidence"], "medium")
            self.assertTrue(any("未发现拷贝事件" in x for x in env["data_quality"]["degraded"]))


class ReviewFixTests(unittest.TestCase):
    def test_memset_onchip_is_d2d_and_heuristics_disclosed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            trio(root, [
                ("Memset", 100, 10, {"transport type": "SDMA", "link type": "ON_CHIP",
                                     "size": 64}),
                ("Memset", 200, 10, {"size": 64}),
            ], kernels=[(0, 500)])
            cap, _, _ = load(root)
            self.assertEqual(cap["copies"][0]["direction"], "d2d")
            self.assertEqual(cap["copies"][0]["dir_source"], "onchip-sdma-assumption")
            self.assertEqual(cap["copies"][1]["direction"], "h2d")
            env = mem.envelope(cap, cap, mem.compare(cap, cap, 0, 0, 1.0), [], [], [], [])
            joined = " ".join(env["notes"])
            self.assertIn("Memset 事件按 host 发起假设归 H2D", joined)
            self.assertIn("ON_CHIP/SDMA 事件按设备内搬运假设归 D2D", joined)

    def test_bytes_verdict_skipped_when_size_coverage_differs(self):
        with tempfile.TemporaryDirectory() as d:
            base, cand = Path(d) / "base", Path(d) / "cand"
            trio(base, [("Memcpy H2D", 100, 10, {"size": 4096})], kernels=[(0, 500)])
            trio(cand, [("Memcpy H2D", 100, 10, {})], kernels=[(0, 500)])
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = mem.envelope(b, c, mem.compare(b, c, 0, 0, 1.0), bn, cn, bd, cd)
            m = mrow(env, "memcpy_bytes")
            self.assertEqual(m["base"], 4096)
            self.assertEqual(m["cand"], 0)
            self.assertEqual(m["verdict"], "info")
            self.assertIn("已知大小事件", m["evidence"])

    def test_count_verdict_uses_common_steps(self):
        def step_trace(path, n_steps):
            events = []
            for sid in range(n_steps):
                t = sid * 10000
                events.append(ev(f"ProfilerStep#{sid}", t, 10000, cat="cpu_op"))
                events.append(ev("Memcpy H2D", t + 100, 10, cat=None,
                                 args={"Task Type": "Memcpy", "size": 100}))
            write_trace(path, events)

        with tempfile.TemporaryDirectory() as d:
            base, cand = Path(d) / "base", Path(d) / "cand"
            step_trace(base, 10)
            step_trace(cand, 5)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = mem.envelope(b, c, mem.compare(b, c, 0, 0, 1.0), bn, cn, bd, cd)
            m = mrow(env, "memcpy_count")
            self.assertEqual(m["base"], 10)
            self.assertEqual(m["cand"], 5)
            self.assertEqual(m["verdict"], "noise")
            self.assertEqual(len(env["per_step"]), 10)
            missing = [p for p in env["per_step"] if p["cand"] is None]
            self.assertEqual([p["step"] for p in missing], [5, 6, 7, 8, 9])

    def test_per_step_keeps_real_profiler_step_ids(self):
        def one(path, step_id):
            write_trace(path, [
                ev(f"ProfilerStep#{step_id}", 0, 10000, cat="cpu_op"),
                ev("Memcpy H2D", 100, 10, cat=None,
                   args={"Task Type": "Memcpy", "size": 100}),
            ])

        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "base"
            cand = Path(d) / "cand"
            one(base, 10)
            one(cand, 11)
            b, bn, bd = load(base)
            c, cn, cd = load(cand)
            env = mem.envelope(b, c, mem.compare(b, c, 0, 0, 1.0), bn, cn, bd, cd)
            steps = {p["step"]: p for p in env["per_step"]}
            self.assertEqual(sorted(steps), [10, 11])
            self.assertEqual(steps[10]["base"], 1)
            self.assertIsNone(steps[10]["cand"])
            self.assertIsNone(steps[11]["base"])
            self.assertEqual(steps[11]["cand"], 1)

    def test_api_count_note_is_not_called_async_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a"
            trio(root, [("Memcpy H2D", 100, 10, {"size": 100})], kernels=[(0, 500)])
            out = root / "ASCEND_PROFILER_OUTPUT"
            (out / "api_statistic.csv").write_text(
                "API Name,Count\naclrtMemcpy,2\n", encoding="utf-8")
            cap, notes, _ = load(root)
            joined = " ".join(notes)
            self.assertIn("aclrtMemcpyAsync", joined)
            self.assertNotIn("异步 API 与设备事件非一一对应", joined)


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
    """真实 CANN trace：设备任务在 Task Type，拷贝事件名带 host_to_device。"""

    def test_task_type_kernel_and_direction_from_name(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "a"
            write_trace(root, [
                ev("ProfilerStep#0", 0, 10000, cat=None),
                ev("MatMul", 0, 1000, cat=None, args={"Task Type": "AI_CORE"}),
                ev("acl_memcpy_host_to_device", 100, 50, cat="cpu_op"),
            ])
            cap, _, _ = load(root)
            st = mem.stats(cap)
            self.assertEqual(st["count"], 1)
            self.assertEqual(st["directions"]["h2d"], 1)
            self.assertAlmostEqual(st["overlap_pct"], 100.0)

    def test_dsa_sqe_not_counted_as_compute_kernel(self):
        self.assertNotIn("DSA_SQE", mem.AI_TASK_TYPES)


if __name__ == "__main__":
    unittest.main()
