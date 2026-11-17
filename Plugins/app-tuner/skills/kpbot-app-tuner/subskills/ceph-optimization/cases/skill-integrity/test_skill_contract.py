#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-optimization skill (stdlib only).

Guards frontmatter, reference files, orchestrator registration, the
tuning-kb JSON contract against BoostKit Ceph guide 9.3.3/9.3.4, playbook
SOP facts, and skill red-line invariants. Run via run.sh, or directly:

    python3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../subskills/ceph-optimization
TUNER = SKILL_DIR.parents[1]         # .../skills/kpbot-app-tuner
REPO = SKILL_DIR.parents[5]          # subskills->tuner->skills->app-tuner->Plugins->root

MAIN_SKILL = TUNER / "SKILL.md"
CANDIDATE = TUNER / "references" / "candidate-skill-list.md"
ROUTING = TUNER / "references" / "knowledge-technique-routing.md"
CHANGELOG = REPO / "CHANGELOG.md"


def read(path):
    return Path(path).read_text(encoding="utf-8")


OS_SUGGESTED = {
    "vm.swappiness": "0",
    "MTU": "9000",
    "kernel.pid_max": "4194303",
    "fs.file-max": "$(cat /proc/meminfo | grep MemTotal | awk '{print $2}')",
    "read_ahead": "8192KB",
    "io_scheduler": "HDD=deadline; SSD/NVMe=none",
    "nr_requests": "HDD=512; SSD/NVMe=1023",
}

GLOBAL_SUGGESTED = {
    "osd_pool_default_min_size": "1",
    "osd_pool_default_size": "3",
    "mon_max_pg_per_osd": "3000",
    "mon_max_pool_pg_num": "300000",
    "throttler_perf_counter": "False",
    "ms_dispatch_throttle_bytes": "2097152000",
    "ms_bind_before_connect": "True",
}

OSD_SUGGESTED = {
    "osd_max_write_size": "256",
    "osd_client_message_size_cap": "1073741824",
    "osd_map_cache_size": "1024",
    "mon_osd_full_ratio": "0.97",
    "mon_osd_nearfull_ratio": "0.95",
    "osd_min_pg_log_entries": "10",
    "osd_max_pg_log_entries": "10",
    "bluestore_cache_meta_ratio": "0.8",
    "bluestore_cache_kv_ratio": "0.2",
}


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_DIR / "SKILL.md")

    def test_name_matches_directory(self):
        self.assertTrue(self.text.startswith("---\n"))
        self.assertIn("name: ceph-optimization\n", self.text[:800])

    def test_description_describes_trigger_and_role(self):
        head = self.text[:2500]
        self.assertIn("Ceph", head)
        self.assertIn("触发", head)
        self.assertIn("调优体检", head)


class ReferenceTests(unittest.TestCase):
    def test_reference_files_exist(self):
        self.assertTrue((SKILL_DIR / "references" / "ceph-tuning-kb.json").is_file())
        self.assertTrue((SKILL_DIR / "references" / "ceph-tuning-playbook.md").is_file())


class RegistrationTests(unittest.TestCase):
    def test_catalog_row_in_main_skill(self):
        self.assertIn(
            "| Ceph 存储调优 skill | `subskills/ceph-optimization/SKILL.md` |",
            read(MAIN_SKILL),
        )

    def test_step7_candidate_rule(self):
        text = read(MAIN_SKILL)
        self.assertIn("ceph-optimization", text)
        self.assertIn("RBD/RGW/CephFS", text)

    def test_candidate_signal_row(self):
        text = read(CANDIDATE)
        self.assertIn("Ceph 存储场景", text)
        self.assertIn("ceph-optimization", text)

    def test_routing_rows(self):
        self.assertGreaterEqual(read(ROUTING).count("ceph-optimization"), 2)

    def test_changelog_entry(self):
        self.assertIn("ceph-optimization", read(CHANGELOG))


class TuningKbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = json.loads(read(SKILL_DIR / "references" / "ceph-tuning-kb.json"))

    @staticmethod
    def by_param(entries):
        return {e["param"]: e for e in entries}

    def test_source_cites_sections(self):
        source = self.kb["source"]
        self.assertIn("9.3.3", source)
        self.assertIn("9.3.4", source)

    def test_os_tuning_items_and_suggested_values(self):
        os_tuning = self.by_param(self.kb["os_tuning"])
        self.assertEqual(set(os_tuning), set(OS_SUGGESTED))
        for param, suggested in OS_SUGGESTED.items():
            self.assertEqual(os_tuning[param]["suggested"], suggested, param)

    def test_os_tuning_entries_have_persist_and_scope(self):
        for entry in self.kb["os_tuning"]:
            self.assertTrue(entry.get("persist"), entry["param"])
            self.assertTrue(entry.get("check_cmd"), entry["param"])

    def test_ceph_conf_sections(self):
        conf = self.kb["ceph_conf"]
        self.assertIn("systemctl restart ceph.target", conf["restart"])
        self.assertIn("cp /etc/ceph/ceph.conf", conf["backup"])
        for section in ("global", "client", "osd"):
            self.assertTrue(conf[section])

    def test_global_suggested_values(self):
        global_entries = self.by_param(self.kb["ceph_conf"]["global"])
        for param, suggested in GLOBAL_SUGGESTED.items():
            self.assertEqual(global_entries[param]["suggested"], suggested, param)

    def test_debug_switch_list_is_complete(self):
        debug = self.by_param(self.kb["ceph_conf"]["global"])["debug_*（全量开关）"]
        self.assertEqual(len(debug["list"]), 59)
        for name in debug["list"]:
            self.assertTrue(name.startswith("debug_"), name)
        self.assertIn("debug_bluestore", debug["list"])
        self.assertIn("debug_eventtrace", debug["list"])

    def test_client_rbd_cache(self):
        client = self.by_param(self.kb["ceph_conf"]["client"])
        self.assertEqual(client["rbd_cache"]["suggested"], "False")

    def test_osd_suggested_values(self):
        osd = self.by_param(self.kb["ceph_conf"]["osd"])
        for param, suggested in OSD_SUGGESTED.items():
            self.assertEqual(osd[param]["suggested"], suggested, param)

    def test_rocksdb_options_key_facts(self):
        osd = self.by_param(self.kb["ceph_conf"]["osd"])
        suggested = osd["bluestore_rocksdb_options"]["suggested"]
        for fact in ("max_write_buffer_number=64", "level0_stop_writes_trigger=256",
                     "max_background_compactions=16", "compaction_threads=8"):
            self.assertIn(fact, suggested)

    def test_pg_distribution(self):
        pg = self.kb["pg_distribution"]
        self.assertIn("* 100", pg["pg_num_formula"])
        self.assertIn("2 的整数次幂", pg["pg_num_formula"])
        self.assertEqual(pg["balancer_mode"]["suggested"], "upmap")
        self.assertIn("ceph balancer on", pg["balancer_mode"]["cmd"])

    def test_osd_binding(self):
        self.assertEqual(self.kb["osd_binding"]["param"], "osd_numa_node")


class PlaybookTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_DIR / "references" / "ceph-tuning-playbook.md")

    def test_conf_backup_and_restart(self):
        self.assertIn("systemctl restart ceph.target", self.text)
        self.assertIn(".bak.<run_id>", self.text)
        self.assertIn("所有 Ceph 节点", self.text)

    def test_os_level_commands(self):
        self.assertIn("blockdev --setra 8192", self.text)
        self.assertIn("echo deadline >", self.text)
        self.assertIn("echo none >", self.text)
        self.assertIn("/etc/sysctl.d/99-os-optimization.conf", self.text)
        self.assertNotIn("99-ceph-tuning.conf", self.text)

    def test_balancer_commands(self):
        self.assertIn("ceph balancer mode upmap", self.text)
        self.assertIn("ceph balancer on", self.text)
        self.assertIn("ceph balancer eval", self.text)

    def test_debug_count_consistent_with_kb(self):
        self.assertIn("59 项", self.text)


class OSBoundaryTests(unittest.TestCase):
    def test_os_layer_evidence_handoff_declared(self):
        text = read(SKILL_DIR / "SKILL.md")
        self.assertIn("os_tuning_findings", text)
        self.assertIn("os-optimization", text)
        self.assertIn("99-os-optimization.conf", text)
        self.assertNotIn("99-ceph-tuning.conf", text)

    def test_no_numa_node_sysfs_writes(self):
        for rel in ("SKILL.md", "references/ceph-tuning-playbook.md", "references/ceph-tuning-kb.json"):
            text = read(SKILL_DIR / rel)
            self.assertIsNone(re.search(r">\s*/sys/[^\s\"'`]*numa_node", text), rel)
            self.assertNotIn("device/device/numa_node", text, msg=rel)

    def test_numa_handoff_declared(self):
        pb = read(SKILL_DIR / "references" / "ceph-tuning-playbook.md")
        self.assertIn("cpu-affinity-optimization", pb)
        self.assertIn("/sys/block/nvme*/device/numa_node", pb)
        self.assertIn("hardware-upgrade-analysis", pb)


class RedlineTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_DIR / "SKILL.md")

    def test_readonly_gate_declared(self):
        self.assertIn("只读", self.text)

    def test_restart_window_gate(self):
        self.assertIn("requires_restart_window", self.text)

    def test_multinode_contract(self):
        self.assertIn("逐节点", self.text)

    def test_conclusion_tri_state(self):
        for kw in ("recommended", "already_tuned", "blocked"):
            self.assertIn(kw, self.text)


class MarkdownTableTests(unittest.TestCase):
    def test_table_pipe_balance(self):
        lines = read(SKILL_DIR / "SKILL.md").splitlines()
        lines += read(SKILL_DIR / "references" / "ceph-tuning-playbook.md").splitlines()
        cols = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                n = len(re.findall(r"(?<!\\)\|", stripped))
                if cols is None:
                    cols = n
                else:
                    self.assertEqual(n, cols, f"unbalanced table row: {stripped[:80]}")
            else:
                cols = None


class EvalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evals = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        cls.queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))

    def test_skill_name_matches_directory(self):
        self.assertEqual(self.evals["skill_name"], "ceph-optimization")
        self.assertEqual(self.queries["skill_name"], "ceph-optimization")

    def test_evals_entries(self):
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 4)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["prompt"].strip(), entry["id"])
            self.assertTrue(entry["expected_output"].strip(), entry["id"])

    def test_queries_trigger_matrix(self):
        queries = self.queries["queries"]
        self.assertGreaterEqual(len(queries), 6)
        ids = [q["id"] for q in queries]
        self.assertEqual(len(ids), len(set(ids)))
        positives = [q for q in queries if q["should_trigger"]]
        negatives = [q for q in queries if not q["should_trigger"]]
        self.assertTrue(positives)
        self.assertTrue(negatives)
        for q in negatives:
            self.assertIn("routes to", q["note"], q["id"])


    def test_evals_expectations_present(self):
        for entry in self.evals["evals"]:
            exps = entry.get("expectations")
            self.assertTrue(exps, entry["id"])
            for item in exps:
                self.assertTrue(item.strip(), entry["id"])
                self.assertRegex(item, r"[\u4e00-\u9fff]", msg=entry["id"])
                self.assertNotIn("??", item, msg=entry["id"])
                self.assertNotIn("\ufffd", item, msg=entry["id"])

    def test_evals_expectations_respect_os_boundary(self):
        forbidden = (r"--setra \d", r">\s*/sys/", r"echo deadline >", r"echo none >", r"sysctl -w")
        for entry in self.evals["evals"]:
            fields = [entry.get("expected_output", "")] + list(entry.get("expectations", []))
            for text in fields:
                for pat in forbidden:
                    self.assertIsNone(re.search(pat, text), "%s: %s" % (entry["id"], pat))

if __name__ == "__main__":
    unittest.main()
