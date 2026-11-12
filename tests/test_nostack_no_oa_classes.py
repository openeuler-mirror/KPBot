"""四分类在无 Overlap Analysis 时的自建回退测试。"""

import importlib.util
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "Plugins/app-tuner/skills/kptools-ascend-profiling-analysis-nostack/scripts/nostack_analysis.py"
)
SPEC = importlib.util.spec_from_file_location("nostack_analysis", SCRIPT)
na = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(na)


def ev(ts, dur, name, cat, pid=1, tid=1):
    return (ts, dur, name, cat, pid, tid)


class NoOverlapAnalysisClassesTests(unittest.TestCase):
    def test_self_built_uses_device_tasks_and_hcom(self):
        events = [
            ev(1_000_000, 2_000_000, "matmul", "AI_CORE"),
            ev(4_000_000, 1_000_000, "softmax", "AI_VECTOR_CORE"),
            ev(6_000_000, 2_000_000, "hcom_allReduce", None, tid=2),
        ]
        steps = [{"id": 0, "start_ns": 0, "end_ns": 10_000_000}]

        classes = na.resolve_classes(events)
        self.assertEqual(classes[3], "self_built")

        oa, source = na.oa_classes(steps, events, classes)
        self.assertEqual(source, "self_built")
        got = oa[0]
        self.assertAlmostEqual(got["comp"], 3000.0, places=3)
        self.assertAlmostEqual(got["comm"], 2000.0, places=3)
        self.assertAlmostEqual(got["comm_no"], 2000.0, places=3)
        self.assertAlmostEqual(got["free"], 5000.0, places=3)

    def test_overlap_analysis_events_take_priority(self):
        events = [
            ev(0, 4_000_000, "Computing", "Overlap Analysis", pid=300),
            ev(4_000_000, 2_000_000, "Communication", "Overlap Analysis", pid=300),
            ev(
                4_000_000,
                2_000_000,
                "Communication(Not Overlapped)",
                "Overlap Analysis",
                pid=300,
            ),
            ev(1_000_000, 1_000_000, "matmul", "AI_CORE"),
        ]
        self.assertEqual(na.resolve_classes(events)[3], "overlap_analysis")

    def test_coordination_stream_ai_cpu_not_counted_as_computing(self):
        events = [ev(i * 1000, 500, "hcom_notify", "AI_CPU", tid=9) for i in range(100)]
        comp, comm, cno, source = na.resolve_classes(events)
        self.assertEqual(source, "self_built")
        self.assertEqual(comp, [])
        self.assertEqual(comm, [])
        self.assertEqual(cno, [])


if __name__ == "__main__":
    unittest.main()
