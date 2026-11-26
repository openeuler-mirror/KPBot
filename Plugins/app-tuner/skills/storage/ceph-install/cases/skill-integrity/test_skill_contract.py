#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-install skill (stdlib only).

Guards the frontmatter (name/license/metadata), the installation-guide reference
(source annotation + key-fact spot checks against the Ceph guide document
version 07), the boundary table (sibling routing), table pipe balance, and the
evals contract. Run via run.sh, or directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-install

REFERENCES = ("installation-guide.md",)


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
        self.assertIn("name: ceph-install\n", self.fm[:800])

    def test_license_and_metadata_fields(self):
        self.assertIn("license: MulanPSL-2.0", self.fm)
        self.assertIn("compatibility:", self.fm)
        for field in ("skill_type: tool_assisted", "category: install",
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
        text = read(SKILL_DIR / "references" / "installation-guide.md")
        self.assertIn("16.2.7", text)
        self.assertIn("openEuler-22.03-LTS-SP4", text)


class SafetyGuardTests(unittest.TestCase):
    """Review-fix guards: TLS/GPG verification kept on, first repo backup
    preserved, transaction-scoped rollback, ISO checksum before mount."""

    def setUp(self):
        self.guide = read(SKILL_DIR / "references" / "installation-guide.md")
        self.skill = read(SKILL_DIR / "SKILL.md")

    def test_reference_tls_verification_preserved(self):
        # TLS 校验保持开启（同已合入 ceph-build 铁律 5 口径）
        self.assertNotIn("sslverify=false", self.guide)
        self.assertNotIn("--global http.sslVerify", self.guide)
        self.assertNotIn("--no-check-certificate", self.guide)
        self.assertIn("update-ca-trust", self.guide)

    def test_reference_gpg_keys_not_fetched_over_http(self):
        # RPM 签名公钥不走明文 HTTP 导入：gpgkey 一律 HTTPS/file://，
        # 优先系统预装密钥（rpm -q gpg-pubkey）并核对指纹（gpg --show-keys）
        self.assertNotIn("gpgkey=http://", self.guide)
        self.assertIn("rpm -q gpg-pubkey", self.guide)
        self.assertIn("gpg --show-keys", self.guide)
        self.assertIn("rpm --import", self.guide)

    def test_reference_iso_checksum_and_local_repo_gpg_on(self):
        # ISO 挂载前先官方 sha256 摘要核对；local.repo 保持 gpgcheck=1 并引用镜像内 GPG key
        self.assertIn("sha256sum", self.guide)
        self.assertNotIn("gpgcheck=0", self.guide)
        self.assertIn("gpgkey=file:///media/", self.guide)
        self.assertIn("find /media -name 'RPM-GPG-KEY*'", self.guide)

    def test_reference_backup_keeps_first_copy(self):
        # repo 备份保留首次原件：cp -n 不覆盖已有 .bak，回退恢复首次原件
        self.assertIn("cp -n /etc/yum.repos.d/openEuler.repo", self.guide)
        self.assertIn("cp -p /etc/yum.repos.d/openEuler.repo.bak /etc/yum.repos.d/openEuler.repo",
                      self.guide)

    def test_reference_rollback_scoped_to_this_transaction(self):
        # 回退只撤销本次安装事务：安装前 ceph 包快照 + yum history undo，不得无条件整批卸载
        self.assertIn("rpm -qa | grep -E '^ceph'", self.guide)
        self.assertIn("yum history undo", self.guide)
        self.assertNotIn("yum remove ceph", self.guide)
        self.assertIn("不得无条件卸载全部 Ceph 软件包", self.skill)

    def test_reference_local_repo_backup_before_write(self):
        # §1.1 写 local.repo 前已有该文件先 cp -n 备份原件，不得直接覆盖不备份
        self.assertIn("cp -n /etc/yum.repos.d/local.repo", self.guide)

    def test_reference_rollback_cleans_local_source(self):
        # 本地源回退补齐环境清理：本次新建 local.repo 直接删除、已存在时恢复 cp -n 备份原件、
        # umount /media（必要时 rmdir 挂载点），末尾 makecache+repolist 核对启用源清单
        rollback = self.guide[self.guide.index("回退（只撤销本次安装事务"):]
        self.assertIn("rm -f /etc/yum.repos.d/local.repo", rollback)
        self.assertIn("cp -p /etc/yum.repos.d/local.repo.bak /etc/yum.repos.d/local.repo",
                      rollback)
        self.assertIn("umount /media", rollback)
        self.assertIn("yum clean all && yum makecache && yum repolist", rollback)

    def test_skill_output_contract_covers_local_source_rollback(self):
        # SKILL.md 输出契约与指南回退段同步：含 local.repo 清理、umount /media、
        # makecache/repolist 核对启用源清单与变更前一致
        contract = self.skill[self.skill.index("## 输出契约"):]
        self.assertIn("local.repo", contract)
        self.assertIn("umount /media", contract)
        self.assertIn("yum clean all && yum makecache && yum repolist", contract)


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        text = read(SKILL_DIR / "SKILL.md")
        for sibling in ("ceph-build", "ceph-ansible-deployment",
                        "ceph-block-storage-deployment"):
            self.assertIn(sibling, text, sibling)

    def test_guide_expansion_routes_by_storage_type_no_phantom_skill(self):
        # 集群扩容按存储类型路由到三个扩容姊妹 skill，不得引用幻影扩容 skill 名
        # （幻影名由拼接构造，避免守卫源码本身出现该字面量）
        phantom = "ceph-" + "expansion"
        guide = read(SKILL_DIR / "references" / "installation-guide.md")
        self.assertNotIn(phantom, guide)
        self.assertNotIn(phantom, read(SKILL_DIR / "SKILL.md"))
        for sibling in ("ceph-block-expansion", "ceph-file-expansion",
                        "ceph-object-expansion"):
            self.assertIn(sibling, guide, sibling)
        self.assertIn("#452", guide)
        self.assertIn("#453", guide)
        self.assertIn("#454", guide)

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


class EvalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evals = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        cls.queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))

    def test_skill_name_and_entries(self):
        self.assertEqual(self.evals["skill_name"], "ceph-install")
        self.assertEqual(self.queries["skill_name"], "ceph-install")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CI-E"), entry["id"])
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
