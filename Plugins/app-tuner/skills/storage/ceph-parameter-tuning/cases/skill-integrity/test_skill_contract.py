#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-parameter-tuning skill (stdlib only).

Guards frontmatter, reference files (incl. the standalone-usage note in the
playbook and the dual-track ownership in the tuning-kb JSON), the kb contract
against the Kunpeng Ceph official guide,
playbook SOP facts, evals contract, and skill red-line
invariants. Run via run.sh, or directly:

    python3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-parameter-tuning


def read(path):
    return Path(path).read_text(encoding="utf-8")


def frontmatter(text):
    assert text.startswith("---\n"), "SKILL.md must start with frontmatter"
    end = text.index("\n---\n", 4)
    return text[:end]


OS_SUGGESTED = {
    "vm.swappiness": "0",
    "MTU": "9000",
    "kernel.pid_max": "4194303",
    "fs.file-max": "<MemTotal_kB>",
    "read_ahead": "8192KB",
    "io_scheduler": "HDD=deadline; SSD/NVMe=none",
    "nr_requests": "HDD=512; SSD/NVMe=1023",
}

GLOBAL_SUGGESTED = {
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
    "osd_min_pg_log_entries": "10",
    "osd_max_pg_log_entries": "10",
    "bluestore_cache_meta_ratio": "0.8",
    "bluestore_cache_kv_ratio": "0.2",
}

MON_SUGGESTED = {
    "mon_osd_full_ratio": "0.97",
    "mon_osd_nearfull_ratio": "0.95",
}


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.fm = frontmatter(read(SKILL_DIR / "SKILL.md"))

    def test_name_matches_directory(self):
        self.assertIn("name: ceph-parameter-tuning\n", self.fm[:800])

    def test_description_describes_trigger_and_role(self):
        self.assertIn("description: >", self.fm)
        self.assertIn("Ceph", self.fm)
        self.assertIn("调优", self.fm)
        self.assertTrue(("触发" in self.fm) or ("使用" in self.fm), self.fm[:500])

    def test_metadata_fields(self):
        for field in ("skill_type: tool_assisted", "category: tuning",
                      "domain: distributed_storage", 'version: "1.0.0"'):
            self.assertIn(field, self.fm, field)

    def test_license_matches_domain_convention(self):
        self.assertIn("license: MulanPSL-2.0", self.fm)
        self.assertIn("compatibility:", self.fm)


class ReferenceTests(unittest.TestCase):
    def test_reference_files_exist(self):
        self.assertTrue((SKILL_DIR / "references" / "tuning-kb.json").is_file())
        self.assertTrue((SKILL_DIR / "references" / "tuning-playbook.md").is_file())

    def test_playbook_has_standalone_note(self):
        self.assertIn("独立使用说明", read(SKILL_DIR / "references" / "tuning-playbook.md"))

    def test_kb_ownership_dual_track(self):
        ownership = json.loads(read(SKILL_DIR / "references" / "tuning-kb.json"))["ownership"]
        self.assertIn("99-os-optimization.conf", ownership)
        self.assertIn("os-optimization", ownership)
        self.assertIn("独立使用", ownership)


class TuningKbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = json.loads(read(SKILL_DIR / "references" / "tuning-kb.json"))

    @staticmethod
    def by_param(entries):
        return {e["param"]: e for e in entries}

    def test_source_cites_official_guide(self):
        source = self.kb["source"]
        self.assertIn("鲲鹏 Ceph 官方指南", source)
        self.assertIn("参数基线", source)

    def test_os_tuning_items_and_suggested_values(self):
        os_tuning = self.by_param(self.kb["os_tuning"])
        self.assertEqual(set(os_tuning), set(OS_SUGGESTED))
        for param, suggested in OS_SUGGESTED.items():
            self.assertEqual(os_tuning[param]["suggested"], suggested, param)

    def test_os_tuning_entries_have_persist_and_scope(self):
        for entry in self.kb["os_tuning"]:
            self.assertTrue(entry.get("persist"), entry["param"])
            self.assertTrue(entry.get("check_cmd"), entry["param"])

    def test_fs_file_max_site_value(self):
        entry = self.by_param(self.kb["os_tuning"])["fs.file-max"]
        self.assertEqual(entry["suggested"], "<MemTotal_kB>")
        self.assertIn("MemTotal", entry["resolve_cmd"])
        self.assertTrue(entry.get("requires_site_value"))

    def test_ceph_conf_sections(self):
        conf = self.kb["ceph_conf"]
        self.assertIn("systemctl restart ceph.target", conf["restart"])
        self.assertIn("cp /etc/ceph/ceph.conf", conf["backup"])
        for section in ("global", "mon", "client", "osd"):
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

    def test_mon_suggested_values(self):
        mon = self.by_param(self.kb["ceph_conf"]["mon"])
        for param, suggested in MON_SUGGESTED.items():
            self.assertEqual(mon[param]["suggested"], suggested, param)

    def test_mon_params_not_in_osd_section(self):
        osd = self.by_param(self.kb["ceph_conf"]["osd"])
        self.assertNotIn("mon_osd_full_ratio", osd)
        self.assertNotIn("mon_osd_nearfull_ratio", osd)

    def test_version_notes_pin_ceph_baseline(self):
        self.assertIn("250/10000", self.kb["parameter_baseline"])
        self.assertIn("0.45", self.kb["parameter_baseline"])
        osd = self.by_param(self.kb["ceph_conf"]["osd"])
        for param in ("osd_min_pg_log_entries", "osd_max_pg_log_entries",
                      "bluestore_cache_meta_ratio", "bluestore_cache_kv_ratio",
                      "bluestore_rocksdb_options"):
            self.assertIn("version_note", osd[param], param)
        debug = self.by_param(self.kb["ceph_conf"]["global"])["debug_*（全量开关）"]
        self.assertIn("debug_kinetic", debug["note"])

    def test_bluestore_cache_autotune_precondition(self):
        osd = self.by_param(self.kb["ceph_conf"]["osd"])
        for param in ("bluestore_cache_meta_ratio", "bluestore_cache_kv_ratio"):
            self.assertIn("bluestore_cache_autotune", osd[param]["precondition"], param)
        self.assertIn("bluestore_cache_autotune",
                      read(SKILL_DIR / "references" / "tuning-playbook.md"))
        self.assertIn("bluestore_cache_autotune", read(SKILL_DIR / "SKILL.md"))

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

    def test_min_size_removed(self):
        global_entries = self.by_param(self.kb["ceph_conf"]["global"])
        self.assertNotIn("osd_pool_default_min_size", global_entries)
        self.assertNotIn("osd_pool_default_min_size",
                         read(SKILL_DIR / "references" / "tuning-kb.json"))

    def test_full_nearfull_runtime_channel(self):
        mon = self.by_param(self.kb["ceph_conf"]["mon"])
        for param in ("mon_osd_full_ratio", "mon_osd_nearfull_ratio"):
            self.assertIn("runtime_channel", mon[param], param)
            self.assertIn("ceph osd set-full-ratio", mon[param]["runtime_channel"], param)
            self.assertIn("nearfull < backfillfull < full",
                          mon[param]["runtime_channel"], param)
        self.assertIn("set-full-ratio", read(SKILL_DIR / "references" / "tuning-playbook.md"))

    def test_setra_sector_units(self):
        os_tuning = self.by_param(self.kb["os_tuning"])
        read_ahead = os_tuning["read_ahead"]
        self.assertIn("--setra 16384", read_ahead["apply_cmd"])
        self.assertIn("512B 扇区计数", read_ahead["apply_cmd"])
        self.assertIn("期望 16384 扇区", read_ahead["check_cmd"])
        self.assertIn("512 字节扇区", read_ahead["note"])
        kb_text = read(SKILL_DIR / "references" / "tuning-kb.json")
        playbook = read(SKILL_DIR / "references" / "tuning-playbook.md")
        self.assertNotIn("--setra 8192", kb_text)
        self.assertNotIn("--setra 8192", playbook)
        self.assertIn("期望 16384 扇区", playbook)


class PlaybookTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_DIR / "references" / "tuning-playbook.md")

    def test_conf_backup_and_restart(self):
        self.assertIn("systemctl restart ceph.target", self.text)
        self.assertIn(".bak.<run_id>", self.text)
        self.assertIn("所有 Ceph 节点", self.text)

    def test_os_level_commands(self):
        self.assertIn("blockdev --setra 16384", self.text)
        self.assertNotIn("--setra 8192", self.text)
        self.assertIn("echo deadline >", self.text)
        self.assertIn("echo none >", self.text)
        self.assertIn("/etc/sysctl.d/99-os-optimization.conf", self.text)

    def test_balancer_commands(self):
        self.assertIn("ceph balancer mode upmap", self.text)
        self.assertIn("ceph balancer on", self.text)
        self.assertIn("ceph balancer eval", self.text)

    def test_debug_count_consistent_with_kb(self):
        self.assertIn("59 项", self.text)

    def test_standalone_note_mentions_dual_track(self):
        note_line = next(l for l in self.text.splitlines() if "独立使用说明" in l)
        self.assertIn("99-os-optimization.conf", note_line)
        self.assertIn("os-optimization", note_line)
        self.assertIn("ceph-optimization", note_line)

    def test_rollback_granularity_guard(self):
        self.assertIn("已存在+原值", self.text)
        self.assertIn("两种状态", self.text)
        self.assertIn("不得整文件重写覆盖他人条目", self.text)
        self.assertIn("键级恢复", self.text)
        self.assertIn("禁止删除整个 99-os-optimization.conf", self.text)
        self.assertIn("sysctl --system", self.text)

    def test_cephadm_config_store_rollback(self):
        self.assertIn("ceph config set", self.text)
        self.assertIn("ceph config get", self.text)
        self.assertIn("ceph config rm", self.text)
        self.assertIn("配置库", self.text)
        self.assertIn("仅还原本地 ceph.conf 不足以回退配置库变更", self.text)


class OwnershipConsistencyTests(unittest.TestCase):
    def test_playbook_ownership_mode_split(self):
        text = read(SKILL_DIR / "references" / "tuning-playbook.md")
        self.assertIn("归属边界（按模式分叉）", text)
        self.assertIn("独立使用时由本 skill 落", text)
        self.assertNotIn("内容一字未改", text)

    def test_kb_persist_ownership_no_legacy_claim(self):
        kb_text = read(SKILL_DIR / "references" / "tuning-kb.json")
        self.assertNotIn("（由 os-optimization 统一落盘）", kb_text)
        self.assertIn("独立模式本 skill 落盘", kb_text)

    def test_boundary_routes_compile_to_ceph_build(self):
        self.assertIn("ceph-build", read(SKILL_DIR / "SKILL.md"))

    def test_markdown_json_refs_exist(self):
        import re as _re
        for rel in ("SKILL.md", "references/tuning-playbook.md"):
            text = read(SKILL_DIR / rel)
            for name in set(_re.findall(r"[A-Za-z0-9_\-]+\.json", text)):
                self.assertTrue((SKILL_DIR / "references" / name).is_file(),
                                "%s 引用的 %s 不存在" % (rel, name))


class BoundaryTests(unittest.TestCase):
    def test_no_private_example_ip(self):
        for rel in ("SKILL.md", "references/tuning-playbook.md", "references/tuning-kb.json"):
            self.assertNotIn("192.168.", read(SKILL_DIR / rel), rel)
        self.assertIn("192.0.2.0/24", read(SKILL_DIR / "references" / "tuning-kb.json"))

    def test_no_numa_node_sysfs_writes(self):
        for rel in ("SKILL.md", "references/tuning-playbook.md", "references/tuning-kb.json"):
            text = read(SKILL_DIR / rel)
            self.assertIsNone(re.search(r">\s*/sys/[^\s\"'`]*numa_node", text), rel)
            self.assertNotIn("device/device/numa_node", text, msg=rel)

    def test_kb_numa_readonly_note(self):
        numa = json.loads(read(SKILL_DIR / "references" / "tuning-kb.json"))["numa_affinity"]
        self.assertIn("只读", numa["note"])

    def test_persist_ownership_file(self):
        kb_text = read(SKILL_DIR / "references" / "tuning-kb.json")
        skill_text = read(SKILL_DIR / "SKILL.md")
        playbook = read(SKILL_DIR / "references" / "tuning-playbook.md")
        self.assertIn("99-os-optimization.conf", skill_text)
        self.assertIn("99-os-optimization.conf", kb_text)
        for rel, text in (("SKILL.md", skill_text), ("playbook", playbook), ("kb", kb_text)):
            self.assertNotIn("99-ceph-tuning.conf", text, rel)


class EvalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evals = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        cls.queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))

    def test_skill_name_matches_directory(self):
        self.assertEqual(self.evals["skill_name"], "ceph-parameter-tuning")
        self.assertEqual(self.queries["skill_name"], "ceph-parameter-tuning")

    def test_evals_entries(self):
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 4)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CPT-E"), entry["id"])
            self.assertTrue(entry["prompt"].strip(), entry["id"])
            self.assertTrue(entry["expected_output"].strip(), entry["id"])
            self.assertIsInstance(entry["files"], list, entry["id"])
            self.assertTrue(entry["expectations"], entry["id"])

    def test_queries_trigger_matrix(self):
        queries = self.queries["queries"]
        self.assertGreaterEqual(len(queries), 6)
        ids = [q["id"] for q in queries]
        self.assertEqual(len(ids), len(set(ids)))
        positives = [q for q in queries if q["should_trigger"]]
        negatives = [q for q in queries if not q["should_trigger"]]
        self.assertGreaterEqual(len(positives), 3)
        self.assertGreaterEqual(len(negatives), 3)
        for q in negatives:
            self.assertIn("routes to", q["note"], q["id"])

    def test_evals_expectations_cjk_clean(self):
        for entry in self.evals["evals"]:
            fields = [entry.get("expected_output", "")] + list(entry.get("expectations", []))
            for item in entry["expectations"]:
                self.assertTrue(item.strip(), entry["id"])
                self.assertRegex(item, r"[\u4e00-\u9fff]", msg=entry["id"])
            for text in fields:
                self.assertNotIn("??", text, msg=entry["id"])
                self.assertNotIn("\ufffd", text, msg=entry["id"])

    def test_evals_expectations_respect_os_boundary(self):
        forbidden = (r"--setra \d", r">\s*/sys/", r"echo deadline >", r"echo none >", r"sysctl -w")
        for entry in self.evals["evals"]:
            fields = [entry.get("expected_output", "")] + list(entry.get("expectations", []))
            for text in fields:
                for pat in forbidden:
                    self.assertIsNone(re.search(pat, text), "%s: %s" % (entry["id"], pat))


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

    def test_confidence_not_measured(self):
        self.assertIn("恒不标", self.text)
        self.assertIn("measured", self.text)


class MarkdownTableTests(unittest.TestCase):
    def test_table_pipe_balance(self):
        lines = read(SKILL_DIR / "SKILL.md").splitlines()
        lines += read(SKILL_DIR / "references" / "tuning-playbook.md").splitlines()
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


if __name__ == "__main__":
    unittest.main()
