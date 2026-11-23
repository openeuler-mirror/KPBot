#!/usr/bin/env python3
"""Tier-A unit tests for the uadk-accelerator-optimization skill (stdlib only).

Guards frontmatter, reference files, orchestrator registration, the
algorithm-map JSON contract against the UADK quick-start doc, playbook SOP
facts, and skill red-line invariants. Run via run.sh, or directly:

    python3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../subskills/uadk-accelerator-optimization
TUNER = SKILL_DIR.parents[1]         # .../skills/kpbot-app-tuner
REPO = SKILL_DIR.parents[5]          # subskills->tuner->skills->app-tuner->Plugins->root

MAIN_SKILL = TUNER / "SKILL.md"
CANDIDATE = TUNER / "references" / "candidate-skill-list.md"
ROUTING = TUNER / "references" / "knowledge-technique-routing.md"
CHANGELOG = REPO / "CHANGELOG.md"


def read(path):
    return Path(path).read_text(encoding="utf-8")


UADK_ALGS = {
    "MD5", "SM3", "SHA (SHA1/SHA256/SHA512)", "AES-CBC/ECB/CTR", "SM4-ECB/CBC",
    "DES/3DES (des-ede3)", "AEAD (AES-GCM 等)", "HMAC", "RSA", "DH",
    "gzip/zlib (deflate/inflate)",
}
FAMILY_DEVICE = {
    "digest": "SEC", "cipher": "SEC", "aead": "SEC", "mac": "SEC",
    "asymmetric": "HPRE", "compression": "ZIP",
}


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_DIR / "SKILL.md")

    def test_name_matches_directory(self):
        self.assertTrue(self.text.startswith("---\n"))
        self.assertIn("name: uadk-accelerator-optimization\n", self.text[:800])

    def test_description_describes_trigger_and_role(self):
        head = self.text[:2500]
        self.assertIn("UADK", head)
        self.assertIn("触发", head)
        self.assertIn("卸载评估", head)


class ReferenceTests(unittest.TestCase):
    def test_reference_files_exist(self):
        self.assertTrue((SKILL_DIR / "references" / "uadk-algorithm-map.json").is_file())
        self.assertTrue((SKILL_DIR / "references" / "uadk-playbook.md").is_file())

    def test_cross_skill_sampler_scripts_exist(self):
        scripts = SKILL_DIR.parent / "performance-library-selection" / "scripts"
        self.assertTrue((scripts / "run_and_profile.sh").is_file())

    def test_skill_md_declares_sampler_reuse(self):
        self.assertIn("run_and_profile.sh", read(SKILL_DIR / "SKILL.md"))


class RegistrationTests(unittest.TestCase):
    def test_catalog_row_in_main_skill(self):
        self.assertIn(
            "| UADK 硬件加速 skill | `subskills/uadk-accelerator-optimization/SKILL.md` |",
            read(MAIN_SKILL),
        )

    def test_step7_candidate_rule(self):
        text = read(MAIN_SKILL)
        self.assertIn("uadk-accelerator-optimization", text)
        self.assertIn("≥0.5%", text)

    def test_candidate_signal_row_and_threshold(self):
        text = read(CANDIDATE)
        self.assertIn("密码学/压缩算法热点", text)
        self.assertIn("uadk-accelerator-optimization", text)
        self.assertIn(">= 0.5%", text)

    def test_routing_rows(self):
        self.assertGreaterEqual(read(ROUTING).count("uadk-accelerator-optimization"), 2)

    def test_changelog_entry(self):
        self.assertIn("uadk-accelerator-optimization", read(CHANGELOG))


class AlgorithmMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = json.loads(read(SKILL_DIR / "references" / "uadk-algorithm-map.json"))
        cls.algs = cls.kb["algorithms"]

    def test_schema_and_threshold(self):
        self.assertEqual(self.kb["schema_version"], "1.0")
        self.assertEqual(self.kb["matching_rules"]["hotspot_pct_threshold_pct"], 0.5)

    def test_devices(self):
        devices = self.kb["devices"]
        self.assertEqual(set(devices), {"SEC", "HPRE", "ZIP"})
        self.assertEqual(devices["SEC"]["driver"], "hisi_sec2")
        self.assertEqual(devices["HPRE"]["driver"], "hisi_hpre")
        self.assertEqual(devices["ZIP"]["driver"], "hisi_zip")
        self.assertEqual(devices["SEC"]["pf_q_num_default"], 256)
        self.assertEqual(devices["HPRE"]["pf_q_num_default"], 64)
        self.assertEqual(devices["ZIP"]["pf_q_num_default"], 64)

    def test_algorithms_cover_uadk_doc(self):
        names = {a["algorithm"] for a in self.algs}
        self.assertEqual(len(self.algs), 11)
        self.assertEqual(names, UADK_ALGS)

    def test_family_device_consistency(self):
        for alg in self.algs:
            self.assertEqual(FAMILY_DEVICE[alg["family"]], alg["device"], alg["algorithm"])

    def test_patterns_are_compilable_regex(self):
        for alg in self.algs:
            for pat in alg["hotspot_patterns"] + alg["exclude_patterns"]:
                try:
                    re.compile(pat)
                except re.error:
                    self.fail(f"invalid regex in {alg['algorithm']}: {pat!r}")

    def test_compression_excludes_checksum_only_hotspots(self):
        comp = next(a for a in self.algs if a["family"] == "compression")
        for pat in ("crc32", "adler32"):
            self.assertIn(pat, comp["exclude_patterns"])

    def test_each_algorithm_has_tool_and_engine_entry(self):
        for alg in self.algs:
            self.assertTrue(alg.get("uadk_tool_alg"))
            self.assertTrue(alg.get("engine_entry"))


class PlaybookTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_DIR / "references" / "uadk-playbook.md")

    def test_driver_load_order(self):
        self.assertIn("modprobe uacce", self.text)
        self.assertIn("modprobe hisi_qm", self.text)
        for mod in ("hisi_sec2", "hisi_hpre", "hisi_zip"):
            self.assertRegex(self.text, r"modprobe %s\s+uacce_mode=1" % mod)

    def test_ctx_env_format(self):
        self.assertIn("WD_CIPHER_CTX_NUM", self.text)
        self.assertIn("sync:2@0,async:4@0", self.text)
        self.assertIn("ctx_mode:ctx_num@node", self.text)

    def test_benchmark_tools(self):
        self.assertIn("uadk_tool benchmark", self.text)
        self.assertIn("openssl speed", self.text)
        self.assertIn("--mode sva", self.text)
        self.assertIn("--pktlen", self.text)

    def test_install_paths(self):
        self.assertIn("yum install libwd", self.text)
        self.assertIn("yum install uadk_engine", self.text)


class ApplicabilityTests(unittest.TestCase):
    def test_performance_predicatble_layer_declared(self):
        text = read(SKILL_DIR / "SKILL.md")
        self.assertIn("性能适用性预判", text)
        self.assertIn("三层", text)
        self.assertIn("uadk_tool benchmark", text)
        self.assertIn("pktlen", text)


class RedlineTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_DIR / "SKILL.md")

    def test_readonly_gate_declared(self):
        self.assertIn("只读", self.text)

    def test_conclusion_tri_state(self):
        for kw in ("recommended", "not_applicable", "blocked"):
            self.assertIn(kw, self.text)

    def test_mutual_exclusion_declared(self):
        self.assertIn("mutually_exclusive_with", self.text)
        self.assertIn("performance-library-selection", self.text)

    def test_confidence_never_measured(self):
        self.assertIn("恒不标", self.text)
        self.assertIn("measured", self.text)


class MarkdownTableTests(unittest.TestCase):
    def test_table_pipe_balance(self):
        lines = read(SKILL_DIR / "SKILL.md").splitlines()
        lines += read(SKILL_DIR / "references" / "uadk-playbook.md").splitlines()
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
        self.assertEqual(self.evals["skill_name"], "uadk-accelerator-optimization")
        self.assertEqual(self.queries["skill_name"], "uadk-accelerator-optimization")

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

if __name__ == "__main__":
    unittest.main()
