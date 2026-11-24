#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-build skill (stdlib only).

Guards the frontmatter (name/license/metadata), the build-guide reference
(source annotation + key-command spot checks against the Kunpeng Ceph official guide), the boundary table (sibling routing), table pipe
balance, and the evals contract. Run via run.sh,
or directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-build

REFERENCES = ("build-guide.md",)


def read(path):
    return Path(path).read_text(encoding="utf-8")


def frontmatter(text):
    assert text.startswith("---\n"), "SKILL.md must start with frontmatter"
    end = text.index("\n---\n", 4)
    return text[:end]


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.fm = frontmatter(read(SKILL_DIR / "SKILL.md"))

    def test_name_matches_directory(self):
        self.assertIn("name: ceph-build\n", self.fm[:800])

    def test_license_and_metadata_fields(self):
        self.assertIn("license: MulanPSL-2.0", self.fm)
        self.assertIn("compatibility:", self.fm)
        for field in ("skill_type: tool_assisted", "category: build",
                      "domain: distributed_storage", 'version: "1.0.0"'):
            self.assertIn(field, self.fm, field)

    def test_description_describes_trigger_and_role(self):
        self.assertIn("description: >", self.fm)
        self.assertIn("Ceph", self.fm)
        self.assertTrue(("触发" in self.fm) or ("使用" in self.fm), self.fm[:500])


class ReferenceTests(unittest.TestCase):
    def test_reference_files_exist(self):
        for name in REFERENCES:
            self.assertTrue((SKILL_DIR / "references" / name).is_file(), name)

    def test_reference_has_source_annotation(self):
        for name in REFERENCES:
            text = read(SKILL_DIR / "references" / name)
            self.assertIn("来源", text, name)
            self.assertIn("鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南", text, name)
            self.assertIn("文档版本 07", text, name)

    def test_reference_key_facts(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("openEuler 22.03", text)
        self.assertIn("rpmbuild -ba ceph.spec", text)

    def test_tls_verification_preserved(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertNotIn("sslverify=false", text)
        self.assertIn("update-ca-trust", text)
        self.assertNotIn("--global http.sslVerify", text)
        self.assertIn("git -c http.sslVerify=false", text)

    def test_liboath_installed_after_local_source(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        s = text.index("### 1.2 openEuler 20.03")
        e = text.index("### 1.3 openEuler 22.03")
        self.assertNotIn("liboath", text[s:e])

    def test_isal_upgrade_on_all_paths(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        s = text.index("### 2.3 openEuler 22.03")
        e = text.index("## 3.")
        self.assertIn("git clone https://github.com/intel/isa-l.git", text[s:e])
        self.assertIn("log -1 --oneline", text)
        self.assertIn("isa-l 升级三路径必做", read(SKILL_DIR / "SKILL.md"))

    def test_tar_bz2_format(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertNotIn("tar -zcvf ceph-14.2.8.tar.bz2", text)
        self.assertEqual(text.count("tar -jcvf ceph-14.2.8.tar.bz2"), 3)
        self.assertEqual(text.count("bzip2 -t ceph-14.2.8.tar.bz2"), 3)

    def test_source_tarball_acquisition(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("download.ceph.com/tarballs/ceph-14.2.8.tar.gz", text)
        self.assertIn("sha256sum", text)

    def test_centos_python36_epel_naming(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        s = text.index("### 1.1 CentOS 7.6")
        e = text.index("### 1.2 openEuler 20.03")
        chunk = text[s:e]
        for bad in ("python3-Cython", "python3-setuptools", "python3-devel", "python3-pip"):
            self.assertNotIn(bad, chunk, bad)
        self.assertIn("python36-setuptools", chunk)
        self.assertIn("python36-devel", chunk)
        self.assertIn("python36-pip", chunk)
        self.assertEqual(chunk.count("python36-Cython"), 1)

    def test_22_03_patch_paths_absolute(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        s = text.index("### 2.3 openEuler 22.03")
        e = text.index("## 3.")
        chunk = text[s:e]
        self.assertIn("-O /home/22.03_adaptation.patch", chunk)
        self.assertIn("cd /home/ceph-14.2.8\npatch -p1 --dry-run < ../22.03_adaptation.patch", chunk)
        self.assertNotIn("cd ceph-14.2.8/", chunk)
        self.assertIn("patch -p1 < ../22.03_adaptation.patch", chunk)


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        text = read(SKILL_DIR / "SKILL.md")
        for sibling in ("ceph-parameter-tuning",):
            self.assertIn(sibling, text, sibling)
        self.assertNotIn("ceph-optimization", text, "ceph-optimization")
        for name in ("ceph-install", "ceph-ansible-deployment",
                     "ceph-block-storage-deployment"):
            self.assertNotIn(name, text, name)

    def test_table_pipe_balance(self):
        lines = read(SKILL_DIR / "SKILL.md").splitlines()
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


class RollbackAndAcceptanceTests(unittest.TestCase):
    def test_repo_backup_steps(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("cp -n /etc/yum.repos.d/local.repo{,.bak.<run_id>}", guide)
        self.assertIn("回退时直接删除", guide)
        self.assertNotIn("openEuler.repo.bak", guide)
        skill = read(SKILL_DIR / "SKILL.md")
        self.assertNotIn("openEuler.repo.bak", skill)
        self.assertIn(".bak.<run_id>", skill)

    def test_dependency_check_uses_rpm_q(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("rpm -q python-sphinx", guide)
        self.assertNotIn("yum repolist", guide)

    def test_step4_os_split_python_cython(self):
        text = read(SKILL_DIR / "references" / "build-guide.md")
        s = text.index("## 3.")
        e = text.index("## 4.")
        chunk = text[s:e]
        self.assertIn("python3-Cython", chunk)
        self.assertIn("python36-Cython", chunk)
        self.assertGreaterEqual(chunk.count("python36-Cython"), 2)

    def test_acceptance_package_consistency(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn('rpm -q ceph --qf "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH} %{VENDOR}\\n"', guide)
        self.assertIn("rpm -q ceph", read(SKILL_DIR / "SKILL.md"))

    def test_local_ceph_repo_enabled_key(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("enabled=1", guide)
        self.assertNotIn("enable=1\n", guide)

    def test_readonly_cephx_identity(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("client.ro-status", guide)
        self.assertIn("mon 'allow r'", guide)
        self.assertNotIn("setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.admin.keyring", guide)
        self.assertIn("--name client.admin --keyring /etc/ceph/ceph.client.admin.keyring", guide)
        self.assertIn("setfacl -x u:ceph-adm", guide)
        self.assertIn("ceph auth rm client.ro-status", guide)
        self.assertIn("pip3 install", guide)
        self.assertNotIn("pip2 install", guide)
        self.assertNotIn("sudo apt install", guide)

    def test_system_user_creation(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("groupadd --system ceph", guide)
        self.assertNotIn("echo 'ceph:x:167:'", guide)

    def test_no_dangling_iron_law_reference(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertNotIn("铁律 7", guide)
        self.assertIn("铁律 5", guide)

    def test_oath_toolkit_sources_ready(self):
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        self.assertIn("ls -l 0001-oath-toolkit-2.6.5-lockfile.patch", guide)
        self.assertIn("download.savannah.gnu.org/releases/oath-toolkit/oath-toolkit-2.6.5.tar.gz", guide)
        self.assertNotIn("ftp.gnu.org/gnu/oath-toolkit", guide)
        self.assertIn("sha256sum /home/oath-toolkit/oath-toolkit-2.6.5.tar.gz", guide)


class EvalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evals = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        cls.queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))

    def test_skill_name_and_entries(self):
        self.assertEqual(self.evals["skill_name"], "ceph-build")
        self.assertEqual(self.queries["skill_name"], "ceph-build")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CB-E"), entry["id"])
            self.assertTrue(entry["prompt"].strip(), entry["id"])
            self.assertTrue(entry["expected_output"].strip(), entry["id"])
            self.assertIsInstance(entry["files"], list, entry["id"])
            self.assertTrue(entry["expectations"], entry["id"])

    def test_queries_trigger_matrix(self):
        queries = self.queries["queries"]
        self.assertGreaterEqual(len(queries), 4)
        self.assertLessEqual(len(queries), 6)
        ids = [q["id"] for q in queries]
        self.assertEqual(len(ids), len(set(ids)))
        positives = [q for q in queries if q["should_trigger"]]
        negatives = [q for q in queries if not q["should_trigger"]]
        self.assertGreaterEqual(len(positives), 2)
        self.assertGreaterEqual(len(negatives), 2)
        for q in negatives:
            self.assertIn("routes to", q["note"], q["id"])

    def test_evals_expectations_cjk_clean(self):
        for entry in self.evals["evals"]:
            for item in entry["expectations"]:
                self.assertTrue(item.strip(), entry["id"])
                self.assertRegex(item, r"[\u4e00-\u9fff]", msg=entry["id"])
            for text in [entry.get("expected_output", "")] + list(entry.get("expectations", [])):
                self.assertNotIn("?" * 2, text, msg=entry["id"])
                self.assertNotIn("\ufffd", text, msg=entry["id"])

    def test_evals_expectations_no_command_injection(self):
        # expectations 为声明式断言，不得内嵌可执行命令（命令注入模式禁入）
        forbidden = (r"--setra \d", r">\s*/sys/", r"sysctl -w")
        for entry in self.evals["evals"]:
            for text in [entry.get("expected_output", "")] + list(entry.get("expectations", [])):
                for pat in forbidden:
                    self.assertIsNone(re.search(pat, text),
                                      "%s: %s" % (entry["id"], pat))


if __name__ == "__main__":
    unittest.main()
