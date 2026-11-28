#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-block-storage-deployment skill (stdlib only).

Guards the frontmatter (name/license/metadata), the block-storage-deployment
reference (source annotation + key-fact spot checks against the Ceph guide
document version 07), the boundary table (sibling routing marked with the
same-batch PR numbers for not-yet-merged sisters), table pipe balance,
review-fix safety guards (TLS kept on, per-machine approved disk list, scoped
client.<rbd-app> identity, staged health verification, exclusive OSD branches,
mon_host correction, §6.2 partition pointer with single-NVMe layout, ro-status
read-only keyring ACL instead of admin keyring, OS-matrix pip/pip3 & yum
commands, run_id backups instead of fixed .bak/mv overwrite, minimized SSH
trust domain, pinned ceph-deploy clone with rev-parse check and no
verification-skipping clone inside code blocks), and the evals contract.
Run via run.sh, or directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-block-storage-deployment

REFERENCES = ("block-storage-deployment.md",)


def read(path):
    return Path(path).read_text(encoding="utf-8")


def frontmatter(text):
    assert text.startswith("---\n"), "SKILL.md must start with frontmatter"
    end = text.index("\n---\n", 4)
    return text[:end]


def code_blocks(text):
    """Return [(lang, body), ...] of fenced code blocks in a markdown text."""
    return [(m.group(1), m.group(2))
            for m in re.finditer(r"```([A-Za-z0-9_-]*)\n(.*?)```", text, re.S)]


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.fm = frontmatter(read(SKILL_DIR / "SKILL.md"))

    def test_name_matches_directory(self):
        self.assertIn("name: ceph-block-storage-deployment\n", self.fm[:800])

    def test_license_and_metadata_fields(self):
        self.assertIn("license: MulanPSL-2.0", self.fm)
        self.assertIn("compatibility:", self.fm)
        for field in ("skill_type: tool_assisted", "category: deployment",
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
        text = read(SKILL_DIR / "references" / "block-storage-deployment.md")
        self.assertIn("ceph-deploy", text)
        self.assertIn("mon_host", text)


class SafetyGuardTests(unittest.TestCase):
    """Review-fix guards: TLS kept on, per-machine approved disk list instead of
    range zap, scoped client.<rbd-app> identity, staged health verification,
    exclusive OSD layout branches, mon_host matches the MON node plan."""

    def setUp(self):
        self.guide = read(SKILL_DIR / "references" / "block-storage-deployment.md")
        self.skill = read(SKILL_DIR / "SKILL.md")

    def test_tls_verification_preserved(self):
        # TLS 校验保持开启（同已合入 ceph-build 铁律 5 口径）；git 仅命令级绕过、CA 装信任库
        self.assertNotIn("sslverify=false", self.guide)
        self.assertNotIn("--global http.sslVerify", self.guide)
        self.assertNotIn("--no-check-certificate", self.guide)
        self.assertIn("update-ca-trust", self.guide)
        self.assertIn("git -c http.sslVerify=false", self.guide)
        self.assertIn("TLS 校验保持开启", self.skill)

    def test_no_range_zap_approved_disk_list(self):
        # 禁止范围式直清（sda~sdl），改逐机核准稳定设备 ID 清单并排除根盘/挂载盘/现役 OSD
        self.assertNotIn("{a..l}", self.guide)
        self.assertNotIn("/dev/sd${i}", self.guide)
        self.assertIn("禁止范围式直清", self.guide)
        self.assertIn("/dev/disk/by-id", self.guide)
        self.assertIn("lsblk -o NAME,SIZE,TYPE,MOUNTPOINT", self.guide)
        self.assertIn("df -l /", self.guide)
        self.assertIn("findmnt", self.guide)
        self.assertIn("ceph-volume lvm list", self.guide)
        self.assertIn("approved_disks", self.guide)

    def test_no_client_admin_keyring_to_business_clients(self):
        # 业务客户端不下发 client.admin，改用限权身份 client.<rbd-app> 并只分发该 keyring
        self.assertNotIn("admin ceph1 ceph2 ceph3 client1 client2 client3", self.guide)
        self.assertIn("不得将 client.admin", self.guide)
        self.assertIn("ceph auth get-or-create client.<rbd-app> mon 'allow r' osd 'allow rw pool=<pool>'",
                      self.guide)
        self.assertIn("--name client.<rbd-app> --keyring /etc/ceph/ceph.client.<rbd-app>.keyring",
                      self.guide)
        self.assertIn("client.rbd-app", self.guide)

    def test_staged_health_verification(self):
        # 分阶段验收：MON 后只查 quorum、MGR 后查 mgr active、OSD 后查 osd up/in、最后整体 HEALTH_OK
        self.assertIn("ceph mon stat", self.guide)
        self.assertIn("ceph quorum_status", self.guide)
        self.assertIn("ceph mgr stat", self.guide)
        self.assertIn("ceph osd stat", self.guide)
        self.assertIn("分阶段验收", self.guide)
        self.assertIn("HEALTH_OK", self.guide)

    def test_osd_layout_branches_mutually_exclusive(self):
        # 普通 OSD 创建与 DB/WAL 外置创建互斥，不得按 A 建完再执行 B 的 zap（会清掉刚建的 OSD）
        self.assertIn("分支 A（普通", self.guide)
        self.assertIn("分支 B（外置 DB/WAL", self.guide)
        self.assertIn("互斥", self.guide)
        self.assertIn("不得先按分支 A 建完 OSD 再执行分支 B 的 zap", self.guide)

    def test_mon_host_matches_mon_plan(self):
        # mon_host 按 MON 节点 ceph1~ceph3 的 Public 地址生成：不得混入 client1 的 .160、不得漏 ceph3
        self.assertIn("mon_host = 198.51.100.166,198.51.100.167,198.51.100.168", self.guide)
        self.assertNotIn("198.51.100.160,198.51.100.167", self.guide)
        self.assertIn("核对说明", self.guide)

    def test_partition_pointer_and_single_nvme_layout(self):
        # §3.3 步骤2 指针指向 §6.2（partition.sh，单盘 12 WAL + 12 DB），不得指向 §5.2；
        # NVMe 布局统一单块 NVMe（nvme0n1p1 WAL / nvme0n1p2 DB，与 §0 一致），无 nvme1n1
        self.assertNotIn("参见 §5.2", self.guide)
        self.assertIn("快速划分多个 WAL 和 DB 分区参见 §6.2", self.guide)
        self.assertIn("多 OSD 场景必须改用 §6.2 的 partition.sh", self.guide)
        self.assertIn("单盘 12 个 WAL", self.guide)
        self.assertIn("/dev/nvme0n1p1", self.guide)
        self.assertIn("/dev/nvme0n1p2", self.guide)
        self.assertIn("单 NVMe，与 §0 规划一致", self.guide)
        self.assertNotIn("nvme1n1", self.guide)

    def test_keyring_acl_scoped_to_readonly_identity(self):
        # §7.2 按已合入 ceph-build §4.2 口径：setfacl 只对只读身份 client.ro-status keyring 授 ACL，
        # 不得对 client.admin keyring 授 ACL；排障结束回收（setfacl -x + ceph auth rm）
        self.assertNotIn("setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.admin.keyring",
                         self.guide)
        self.assertIn("不得对 client.admin keyring 授 ACL", self.guide)
        self.assertIn("client.ro-status", self.guide)
        self.assertIn("setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.ro-status.keyring",
                      self.guide)
        self.assertIn("setfacl -x u:ceph-adm", self.guide)
        self.assertIn("ceph auth rm client.ro-status", self.guide)

    def test_os_matrix_package_commands(self):
        # OS 矩阵为 CentOS 7.6 / openEuler（无 Ubuntu）：yum 安装 acl，pip/pip3 按矩阵选择，
        # 无 apt install、无 pip2（上游 Ubuntu 示例的偏离在文档中标注）
        self.assertNotIn("apt install", self.guide)
        self.assertNotIn("pip2", self.guide)
        self.assertIn("sudo yum install acl -y", self.guide)
        self.assertIn("pip install xxx", self.guide)
        self.assertIn("pip3 install xxx", self.guide)
        self.assertIn("偏离标注", self.guide)

    def test_no_fixed_backup_names_or_mv_overwrite(self):
        # 备份一律 cp -n + .bak.<run_id>（不覆盖已有文件），不 mv 覆盖固定 .bak 裸名；
        # SKILL.md 回退清单含 ntp.conf 还原动作
        self.assertIn("cp -n", self.guide)
        self.assertIn(".bak.<run_id>", self.guide)
        self.assertIsNone(re.search(r"openEuler\.repo\.bak(?!\.)", self.guide),
                          "fixed openEuler.repo.bak bare name found")
        self.assertIsNone(re.search(r"ntp\.conf\.bak(?!\.)", self.guide),
                          "fixed ntp.conf.bak bare name found")
        self.assertNotIn("mv /etc/ntp.conf", self.guide)
        self.assertNotIn("mv /etc/yum.repos.d/openEuler.repo ", self.guide)
        self.assertIn("ntp.conf", read(SKILL_DIR / "SKILL.md"))

    def test_passwordless_ssh_scope_minimized(self):
        # 免密信任域最小化：仅 ceph1 → 服务端 {1..3}，ssh-copy-id 循环不覆盖 client 节点；
        # SKILL.md 铁律与输出契约含 SSH 信任域收敛项与撤销台账
        self.assertNotIn("ssh-copy-id client", self.guide)
        self.assertIn("for i in {1..3}; do ssh-copy-id ceph$i; done", self.guide)
        self.assertIn("免密信任域最小化", self.guide)
        self.assertIn("SSH 信任域最小化", self.skill)
        self.assertIn("撤销", self.skill)
        self.assertIn("SSH 免密发放范围收敛记录", self.skill)

    def test_ceph_deploy_pinned_clone_and_revparse(self):
        # ceph-deploy 固定 v2.1.0 浅克隆 + 安装前 rev-parse 核对（不匹配即中止）；
        # 免校验 clone 不得出现在任何代码块内（公开 github 地址走系统信任库 CA）
        self.assertIn("--branch v2.1.0 --depth 1", self.guide)
        self.assertIn("git -C /root/ceph-deploy rev-parse HEAD", self.guide)
        self.assertIn("不匹配即中止", self.guide)
        for lang, body in code_blocks(self.guide):
            self.assertNotIn("http.sslVerify=false", body,
                             "verification-skipping clone inside a %s code block" % lang)


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        # 姊妹 skill 已合入：路由仍带「同批 PR 编号 + 已合入」标注，并保留依赖缺失时
        # 标注「未覆盖」的降级口径
        text = read(SKILL_DIR / "SKILL.md")
        for sibling, pr in (("ceph-ansible-deployment", "同批 PR #445"),
                            ("ceph-object-storage-deployment", "同批 PR #447"),
                            ("ceph-file-storage-deployment", "同批 PR #448")):
            self.assertIn(sibling, text, sibling)
            self.assertIn(pr, text, sibling)
        self.assertIn("已合入", text)
        self.assertIn("未覆盖", text)
        # python 缺失降级口径：先补齐解释器，矩阵匹配且用户接受才转交，否则 blocked
        self.assertIn("缺失时先补齐解释器", text)
        self.assertIn("仅当 OS 为其矩阵内", text)
        self.assertIn("否则 ceph-deploy 路径标 blocked", text)
        self.assertIn("不得以转交替代补齐依赖", text)

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
        self.assertEqual(self.evals["skill_name"], "ceph-block-storage-deployment")
        self.assertEqual(self.queries["skill_name"], "ceph-block-storage-deployment")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CBD-E"), entry["id"])
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
