#!/usr/bin/env python3
"""Tier-A unit tests for the kpbot-app-tuner prescreen and io-optimization
collectors.

Strategy: run the real bash scripts with PATH-injected mock metric tools
(mpstat/vmstat/iostat/pidstat/ss/free/findmnt/numastat/perf), keyed by the
MOCK_SCENARIO env var. No real machine, no network, no side effects.

Guarded regressions:
  1. bottleneck classification per scenario (threshold drift between
     SKILL.md and detect_bottleneck.sh fails here)
  2. mpstat header layouts: sysstat >= 12 (%guest/%gnice) AND legacy
     (fixed-column parsers read %guest as %idle -> idle stuck at 0.00)
  3. collector JSON validity (bash string concatenation once produced
     trailing commas / literal backslash-n)
  4. mounts filtered to block devices (thousands of nsfs pseudo mounts on
     container hosts must not leak into the output)
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MOCKBIN = os.path.join(TESTS_DIR, "mockbin")
SKILL_DIR = os.environ.get("SKILL_DIR") or os.path.abspath(
    # tests/ -> unit-prescreen-classify -> cases -> io-optimization ->
    # subskills -> kpbot-app-tuner (5 levels; entrypoint.sh exports SKILL_DIR,
    # this fallback only serves direct `python3 -m unittest` / IDE runs)
    os.path.join(TESTS_DIR, "..", "..", "..", "..", "..")
)
DETECT = os.path.join(SKILL_DIR, "scripts", "detect_bottleneck.sh")
IO_COLLECTOR = os.path.join(SKILL_DIR, "subskills", "io-optimization", "scripts", "collect_io_evidence.sh")
MEM_COLLECTOR = os.path.join(SKILL_DIR, "subskills", "io-optimization", "scripts", "collect_memory_evidence.sh")

FAKE_PID = "4242"
DURATION = "1"


def run_script(argv, scenario, timeout=60, env_extra=None):
    env = dict(os.environ)
    env["PATH"] = MOCKBIN + os.pathsep + env.get("PATH", "")
    env["MOCK_SCENARIO"] = scenario
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash"] + argv, capture_output=True, text=True, env=env, timeout=timeout
    )
    return proc


class PrescreenClassificationTest(unittest.TestCase):
    """detect_bottleneck.sh: one classification per mock scenario."""

    # scenario -> (bottleneck_type, detailed_bottleneck_type)
    EXPECTED = {
        "idle": ("no_active_bottleneck", "unknown"),
        "cpu": ("cpu_bottleneck", "cpu"),
        "network": ("network_bottleneck", "network"),
        "disk_io": ("disk_bottleneck", "disk_io"),
        "disk_bandwidth": ("disk_bottleneck", "disk_bandwidth"),
        "memory": ("memory_capacity_bottleneck", "memory_capacity"),
        "mpstat_old_format": ("no_active_bottleneck", "unknown"),
    }

    def _run(self, scenario):
        proc = run_script([DETECT, FAKE_PID, DURATION], scenario)
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr[:500]}")
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, dict)
        return data

    def test_all_scenarios_classify_correctly(self):
        for scenario, (expected_type, expected_detailed) in self.EXPECTED.items():
            with self.subTest(scenario=scenario):
                data = self._run(scenario)
                self.assertEqual(data["bottleneck_type"], expected_type)
                self.assertEqual(data["detailed_bottleneck_type"], expected_detailed)

    def test_idle_has_no_fallback_notes(self):
        data = self._run("idle")
        # 环境类 note（running_in_container / cpu_count_from_*）在分类判定
        # 之后追加，属信息说明而非降级；容器内合法存在，不算证据缺失。
        degradations = [
            n for n in data["fallback_notes"]
            if not n.startswith("running_in_container")
            and not n.startswith("cpu_count_from_")
        ]
        self.assertEqual(degradations, [])
        self.assertEqual(data["evidence"]["iowait_pct"], 1.00)
        self.assertAlmostEqual(data["evidence"]["disk_util_pct"], 0.50)

    def test_disk_io_threshold_is_10_not_20(self):
        # SKILL.md and the script must agree: wa > 10 (not > 20).
        # Mock emits wa = 15.00: must classify as disk_io.
        data = self._run("disk_io")
        self.assertEqual(data["evidence"]["iowait_pct"], 15.00)

    def test_disk_bandwidth_threshold_is_80(self):
        data = self._run("disk_bandwidth")
        self.assertAlmostEqual(data["evidence"]["disk_util_pct"], 95.00)

    def test_network_signal_is_socket_queue_not_sar(self):
        # Script logic: sendq > 131072 -> network. No sar involved.
        data = self._run("network")
        self.assertEqual(data["evidence"]["network_total_sendq_bytes"], 262144)
        self.assertEqual(data["evidence"]["network_socket_count"], 1)

    def test_memory_capacity_uses_free_used_ratio(self):
        data = self._run("memory")
        self.assertEqual(data["evidence"]["memory_pressure_hint"], "high")

    def test_mpstat_new_format_idle_column(self):
        # sysstat >= 12 inserts %guest/%gnice before %idle: a fixed-column
        # parser reads %guest (0.00) instead of %idle (98.44).
        data = self._run("idle")
        self.assertAlmostEqual(data["evidence"]["mpstat_idle_pct"], 98.44, places=1)
        self.assertAlmostEqual(data["evidence"]["mpstat_irq_pct"], 0.04, places=2)

    def test_mpstat_legacy_format_idle_column(self):
        data = self._run("mpstat_old_format")
        self.assertAlmostEqual(data["evidence"]["mpstat_idle_pct"], 98.44, places=1)

    def test_mpstat_am_pm_locale_idle_column(self):
        # 12-hour locale headers ("11:00:00 AM CPU %usr ...") put a two-field
        # timestamp before the CPU column; parsing must shift by it.
        data = self._run("mpstat_am_pm")
        self.assertAlmostEqual(data["evidence"]["mpstat_idle_pct"], 98.44, places=1)
        self.assertAlmostEqual(data["evidence"]["mpstat_irq_pct"], 0.04, places=2)

    def test_pidstat_process_cpu_uses_cpu_column(self):
        # mock pidstat: AM header + multi-word command, %CPU = 8.00,
        # %usr = 5.00, %guest = 0.00. Fixed $(NF-6) reads %guest (0.00)
        # here and %usr (5.00) with single-word commands — never %CPU.
        data = self._run("idle")
        self.assertAlmostEqual(data["evidence"]["process_cpu_pct"], 8.00, places=2)

    def test_cpu_threshold_uses_core_aggregate(self):
        data = self._run("cpu")
        # 2000 per-core rows x ~99.5 busy > cores * 85 on any realistic host
        self.assertGreater(float(data["evidence"]["mpstat_cpu_pct"]), 190000)


class CollectorsJsonTest(unittest.TestCase):
    """io-optimization collectors: valid JSON, right fields, right filters."""

    def test_collect_io_evidence_json(self):
        proc = run_script([IO_COLLECTOR, DURATION], "disk_bandwidth")
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr[:500]}")
        data = json.loads(proc.stdout)
        self.assertTrue(data["read_only"])
        self.assertAlmostEqual(data["iowait_pct"], 1.00)
        devices = {d["name"]: d for d in data["devices"]}
        self.assertIn("nvme0n1", devices)
        self.assertAlmostEqual(devices["nvme0n1"]["util_pct"], 95.00)
        self.assertIn("avg_queue_size", devices["nvme0n1"])
        # only /dev/-backed mounts; nsfs/tmpfs must be filtered out
        targets = [m["target"] for m in data["mounts"]]
        self.assertIn("/", targets)
        self.assertNotIn("/run/netns/ns-8047", targets)
        self.assertNotIn("/dev/shm", targets)
        # vm writeback params come from real /proc (present on Linux)
        for key in ("dirty_ratio", "dirty_background_ratio", "swappiness"):
            self.assertIn(key, data["vm"])

    def test_collect_memory_evidence_json(self):
        proc = run_script([MEM_COLLECTOR, DURATION], "memory")
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr[:500]}")
        data = json.loads(proc.stdout)
        self.assertTrue(data["read_only"])
        # free mock: memory scenario (used/total > 0.9)
        self.assertGreater(float(data["mem_mb"]["used"]), 0)
        # numastat mock: nodes 0-3, system remote miss = miss/(hit+miss) = 2.61%
        nodes = [n["node"] for n in data["numa"]["nodes"]]
        self.assertEqual(nodes, [0, 1, 2, 3])
        self.assertAlmostEqual(
            float(data["numa"]["system_remote_miss_pct"]), 2.61, places=1
        )
        # perf mock: cache-miss rate ~2.20%
        self.assertAlmostEqual(float(data["cache_miss_rate_pct"]), 2.20, places=1)
        # vmstat mock: since-boot row so=5, window row so=0.60 — the window
        # average must not be diluted by the since-boot row (old NR>2: 2.80)
        self.assertAlmostEqual(float(data["swap_activity_kb_per_sec"]["so"]), 0.60, places=2)
        self.assertEqual(data["thp_mode"] in ("always", "madvise", "never"), True)


class PsiParsingTest(unittest.TestCase):
    """PSI field parsing: /proc/pressure values start after '='.

    A fixed substr($2,8) offset once dropped the first value digit
    (avg10=12.34 -> 2.34, avg10=42.00 -> 2), silently underreporting
    pressure in exactly the 1%~10% band where PSI is informative.
    PSI_DIR injection lets these cases expose the bug that a
    zero-load container (all 0.00) could not.
    """

    PSI_CONTENT = (
        "some avg10=5.00 avg60=12.34 avg300=11.00 total=1234\n"
        "full avg10=42.00 avg60=9.99 avg300=9.00 total=56\n"
    )

    def setUp(self):
        self.psi_root = tempfile.mkdtemp(prefix="psi-")

    def tearDown(self):
        shutil.rmtree(self.psi_root, ignore_errors=True)

    def _write_psi(self, resource, content):
        with open(os.path.join(self.psi_root, resource), "w") as fh:
            fh.write(content)

    def _run(self, script, resource, env_extra=None):
        env = {"PSI_DIR": self.psi_root}
        if env_extra:
            env.update(env_extra)
        proc = run_script([script, "1"], "idle", env_extra=env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        return json.loads(proc.stdout)

    def test_io_psi_values_parse_exact(self):
        self._write_psi("io", self.PSI_CONTENT)
        data = self._run(IO_COLLECTOR, "io")
        psi = data["psi_io"]
        self.assertEqual(psi["some_avg10"], 5.00)
        self.assertEqual(psi["some_avg60"], 12.34)
        self.assertEqual(psi["full_avg10"], 42.00)
        self.assertNotIn("psi_io_parse_anomaly", data["notes"])
        self.assertNotIn("psi_io_unavailable", data["notes"])

    def test_io_psi_malformed_value_marks_anomaly(self):
        self._write_psi("io", "some avg10=xx avg60=1.00 avg300=0.00 total=0\n")
        data = self._run(IO_COLLECTOR, "io")
        self.assertIsNone(data["psi_io"]["some_avg10"])
        self.assertIn("psi_io_parse_anomaly", data["notes"])

    def test_io_psi_missing_dir_marks_unavailable(self):
        data = self._run(IO_COLLECTOR, "io", env_extra={"PSI_DIR": "/nonexistent-psi"})
        self.assertIsNone(data["psi_io"]["some_avg10"])
        self.assertIn("psi_io_unavailable", data["notes"])

    def test_memory_psi_values_parse_exact(self):
        self._write_psi("memory", self.PSI_CONTENT)
        data = self._run(MEM_COLLECTOR, "memory")
        psi = data["psi_memory"]
        self.assertEqual(psi["some_avg10"], 5.00)
        self.assertEqual(psi["some_avg60"], 12.34)
        self.assertEqual(psi["full_avg10"], 42.00)
        self.assertNotIn("psi_memory_parse_anomaly", data["notes"])

    def test_memory_psi_malformed_value_marks_anomaly(self):
        self._write_psi("memory", "some avg10=5.00 avg60=zz avg300=0.00 total=0\n")
        data = self._run(MEM_COLLECTOR, "memory")
        self.assertIsNone(data["psi_memory"]["some_avg60"])
        self.assertIn("psi_memory_parse_anomaly", data["notes"])

    def test_memory_psi_missing_dir_marks_unavailable(self):
        data = self._run(
            MEM_COLLECTOR, "memory", env_extra={"PSI_DIR": "/nonexistent-psi"}
        )
        self.assertIsNone(data["psi_memory"]["some_avg10"])
        self.assertIn("psi_memory_unavailable", data["notes"])


class NpuArchiveHostCpuTest(unittest.TestCase):
    """Archived mpstat.txt parsing (parse_host_cpu) feeds the NPU
    cpu_feed_bottleneck check via host_cpu_util > 80.

    The archive is written by collect_evidence_snapshot.sh as
    `mpstat -P <set> 1 N`; on sysstat >= 12 the old fixed-column parser
    summed usr+sys+soft+steal and silently dropped %irq — under-reading
    host CPU exactly in the IRQ-heavy feed scenario, so the feed
    bottleneck could be missed. The archive header also carries a
    timestamp prefix (locale dependent) that Average rows lack.
    """

    # per core: usr 40 + sys 30 + irq 10 + soft 5 = 85 busy; 2 cores = 170.
    # Old fixed columns read 40+30+5+0 = 75 per core (150 total) -> missed %irq.
    MPSTAT_TXT = (
        "Linux 6.6.0 (mock) 11/23/2026 _aarch64_ (2 CPU)\n"
        "\n"
        "11:00:00 AM CPU %usr %nice %sys %iowait %irq %soft %steal %guest %gnice %idle\n"
        "11:00:01 AM 0 40.00 0.00 30.00 0.00 10.00 5.00 0.00 0.00 0.00 15.00\n"
        "11:00:01 AM 1 40.00 0.00 30.00 0.00 10.00 5.00 0.00 0.00 0.00 15.00\n"
        "Average: 0 40.00 0.00 30.00 0.00 10.00 5.00 0.00 0.00 0.00 15.00\n"
        "Average: 1 40.00 0.00 30.00 0.00 10.00 5.00 0.00 0.00 0.00 15.00\n"
    )

    def _run_with_evidence(self):
        evdir = tempfile.mkdtemp(prefix="npu-ev-")
        running = os.path.join(evdir, "workload_running")
        os.makedirs(running)
        with open(os.path.join(running, "mpstat.txt"), "w") as fh:
            fh.write(self.MPSTAT_TXT)
        with open(os.path.join(running, "npu_status.json"), "w") as fh:
            fh.write('{"npu_available": true}\n')
        with open(os.path.join(running, "npu_utilization.log"), "w") as fh:
            fh.write("0 50\n1 45\n")
        try:
            # argv: PID DURATION CORE_LIST EVIDENCE_DIR; idle mocks keep the
            # host-side classification at "unknown" so the NPU verdict rules
            proc = run_script([DETECT, FAKE_PID, "1", "0-1", evdir], "idle")
            self.assertEqual(proc.returncode, 0, proc.stderr[:500])
            return json.loads(proc.stdout)
        finally:
            shutil.rmtree(evdir, ignore_errors=True)

    def test_host_cpu_util_counts_irq(self):
        data = self._run_with_evidence()
        self.assertEqual(float(data["evidence"]["host_cpu_util_pct"]), 170)

    def test_npu_feed_bottleneck_verdict(self):
        data = self._run_with_evidence()
        self.assertEqual(data["evidence"]["npu_utilization_pct"], 50)
        self.assertEqual(data["evidence"]["npu_bottleneck_type"], "cpu_feed_bottleneck")
        self.assertEqual(data["bottleneck_type"], "cpu_feed_bottleneck")


class VmstatWindowSemanticsTest(unittest.TestCase):
    """vmstat window semantics: procps emits a since-boot first data row.

    Averaging from NR>2 diluted the window: disk_io's wa=15 became 8 (below
    the 10 threshold -> misclassification) and swap so=0.60 became 2.80
    (historical paging reported as current activity). With DURATION=1 the
    window is empty (since-boot row only) and must degrade to null + note.
    """

    def test_boot_only_window_degrades_to_null_with_note(self):
        proc = run_script(
            [IO_COLLECTOR, "1"], "vmstat_boot_only", timeout=60
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIsNone(data["iowait_pct"])
        self.assertIn("vmstat_since_boot_only", data["notes"])

    def test_disk_io_threshold_not_diluted_by_since_boot_row(self):
        # window wa=15, since-boot wa=1: old NR>2 average = 8 (below the
        # disk_io threshold of 10); the window-only average must stay 15.
        proc = run_script([IO_COLLECTOR, "1"], "disk_io", timeout=60)
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertAlmostEqual(data["iowait_pct"], 15.00, places=2)


class IostatHeaderFormsTest(unittest.TestCase):
    """iostat -x header forms differ across sysstat versions: "Device:"
    (colon form) and "Device" (e.g. openEuler 24.03). A header that matches
    neither must degrade with a note — an empty device list must never be
    mistaken for "no active devices" (the original $1 == "Device" rule
    silently emptied devices on colon-form hosts).

    The default mock now emits the real colon form so the tier-A suite is
    sensitive to real tool shapes instead of mirroring the implementation;
    the no-colon form is covered by its own scenario.
    """

    def _collect(self, scenario):
        proc = run_script([IO_COLLECTOR, "1"], scenario, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        return json.loads(proc.stdout)

    def test_colon_header_parsed(self):
        data = self._collect("disk_bandwidth")
        devices = {d["name"]: d for d in data["devices"]}
        self.assertIn("nvme0n1", devices)
        self.assertAlmostEqual(devices["nvme0n1"]["util_pct"], 95.00)
        self.assertNotIn("iostat_header_unparsed", data["notes"])

    def test_no_colon_header_parsed(self):
        data = self._collect("iostat_no_colon")
        devices = {d["name"]: d for d in data["devices"]}
        self.assertIn("nvme0n1", devices)
        self.assertAlmostEqual(devices["nvme0n1"]["util_pct"], 95.00)
        self.assertNotIn("iostat_header_unparsed", data["notes"])

    def test_unparseable_header_degrades_with_note(self):
        data = self._collect("iostat_bad_header")
        self.assertEqual(data["devices"], [])
        self.assertIn("iostat_header_unparsed", data["notes"])


if __name__ == "__main__":
    unittest.main()
