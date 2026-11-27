#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-file-storage-deployment skill (stdlib only).

Guards the frontmatter (name/license/metadata), the file-storage-deployment
reference (source annotation + key-fact spot checks against the Ceph guide
document version 07), the boundary table (sibling routing), table pipe balance,
review-fix safety guards (TLS kept on, per-machine approved disk list, scoped
client.<cfs-app> identity without key printing, exclusive OSD branches,
mon_host correction, lsblk -f compatibility, rollback section, official PG
formula, yum acl), round-2 review fixes (by-id-only write commands, pool-delete
danger toggle scoped to §6.3 with temp-enable + reset, pool pg 1024/128 matching
the formula, ro-status read-only identity for the acl troubleshooting instead of
an admin-keyring ACL, pinned ceph-deploy tag with rev-parse check and venv
install), and the evals contract. Run via run.sh, or directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-file-storage-deployment

REFERENCES = ("file-storage-deployment.md",)


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
        self.assertIn("name: ceph-file-storage-deployment\n", self.fm[:800])

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
        text = read(SKILL_DIR / "references" / "file-storage-deployment.md")
        self.assertIn("MDS", text)
        self.assertIn("挂载", text)


class SafetyGuardTests(unittest.TestCase):
    """Review-fix guards: TLS kept on, per-machine approved disk list instead of
    range zap, scoped client.<cfs-app> identity with no key printing, exclusive
    OSD layout branches, mon_host matches the MON node plan, lsblk -f
    compatibility, rollback section, official PG formula, yum acl."""

    def setUp(self):
        self.guide = read(SKILL_DIR / "references" / "file-storage-deployment.md")
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
        self.assertIn("lsblk -f", self.guide)
        self.assertIn("df -l /", self.guide)
        self.assertIn("findmnt", self.guide)
        self.assertIn("ceph-volume lvm list", self.guide)
        self.assertIn("approved_disks", self.guide)

    def test_lsblk_f_portable(self):
        # lsblk 拓扑查询改为三 OS 通用 lsblk -f，附偏离标注（CentOS 7.6.1810 util-linux
        # 2.23.2 / openEuler 20.03 SP1 util-linux 2.35.2 不支持/需参数）
        self.assertNotIn("lsblk -T", self.guide)
        self.assertIn("lsblk -f", self.guide)
        self.assertIn("util-linux 2.23.2", self.guide)
        self.assertIn("2.35.2", self.guide)

    def test_no_client_admin_keyring_and_no_key_printing(self):
        # CephFS 客户端不下发 client.admin；限权身份 client.<cfs-app>，keyring 落盘 600，
        # 密钥不打印在命令行（secret= 禁用，从 keyring 文件读取）
        self.assertNotIn("admin ceph1 ceph2 ceph3 client1 client2 client3", self.guide)
        self.assertNotIn("cat /etc/ceph/ceph.client.admin.keyring", self.guide)
        self.assertNotIn("secret=", self.guide)
        self.assertNotIn("name=admin", self.guide)
        self.assertIn("不得将 client.admin", self.guide)
        self.assertIn("ceph auth get-or-create client.<cfs-app> mon 'allow r' mds 'allow rw' osd 'allow rw pool=<cfs 数据池>'",
                      self.guide)
        self.assertIn("umask 077; ceph auth print_key client.cfs-app > /etc/ceph/ceph.client.cfs-app.keyring",
                      self.guide)
        self.assertIn("chmod 600 /etc/ceph/ceph.client.cfs-app.keyring", self.guide)
        self.assertIn("secretfile=/etc/ceph/ceph.client.cfs-app.keyring", self.guide)
        self.assertIn("read -s", self.guide)

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

    def test_rollback_section_complete(self):
        # 回退小节：fs rm（先 umount + mds fail）、pool delete（删池前临时开启
        # mon_allow_pool_delete、删除后立即复位，不预置）、mon destroy、客户端 umount
        # 与 fstab 清理，与 SKILL.md 输出契约一一对应
        self.assertIn("## 6. 回退与清理", self.guide)
        self.assertIn("umount /mnt/cephfs", self.guide)
        self.assertIn("/etc/fstab", self.guide)
        self.assertIn("ceph mds fail", self.guide)
        self.assertIn("ceph fs rm cephfs --yes-i-really-mean-it", self.guide)
        self.assertIn("ceph osd pool delete fs_data fs_data --yes-i-really-mean-it", self.guide)
        self.assertIn("ceph osd pool delete fs_metadata fs_metadata --yes-i-really-mean-it", self.guide)
        self.assertNotIn("mon_allow_pool_delete = true", self.guide)
        self.assertIn("ceph config set mon mon_allow_pool_delete true", self.guide)
        self.assertIn("ceph config set mon mon_allow_pool_delete false", self.guide)
        self.assertIn("ceph-deploy mon destroy", self.guide)
        self.assertIn("§6「回退与清理」", self.skill)

    def test_nvme_partition_comment_consistent(self):
        # NVMe 注释与命令及 §4.4 用法一致：WAL_NVME/DB_NVME（核准清单 by-id 变量）=
        # 60GB WAL / 180GB DB（演示两块盘）
        self.assertIn('"$WAL_NVME" 划分 1 个 60GB WAL 分区、"$DB_NVME" 划分 1 个 180GB DB 分区',
                      self.guide)
        self.assertIn("生产建议同盘划出两个分区", self.guide)
        self.assertNotIn("划分出 1 个 60GB 分区和 1 个 180GB 分区", self.guide)

    def test_pg_formula_official_wording(self):
        # PG 公式改官方原文口径：单池基线公式 Total PGs = (OSD 数 × 100) / 副本数（EC 为 K+M），
        # 多池按数据量占比分摊、各向上取整 2 的整数次幂；注明源指南口径与数值结论等价
        self.assertIn("Total PGs = (OSD 数 × 100) / 副本数", self.guide)
        self.assertIn("单池基线", self.guide)
        self.assertIn("向上取整 2 的整数次幂", self.guide)
        self.assertIn("数值结论等价", self.guide)
        self.assertIn("1200", self.guide)

    def test_acl_installed_with_yum(self):
        # 排障示例 RHEL 化：apt → yum，提示符对齐集群规划节点，附偏离标注
        self.assertIn("sudo yum install -y acl", self.guide)
        self.assertNotIn("apt install", self.guide)
        self.assertIn("上游 Ubuntu 排障示例", self.guide)
        self.assertIn("root@ceph1", self.guide)
        self.assertNotIn("ceph-adm@mon", self.guide)

    def test_write_commands_use_by_id_paths(self):
        # 写盘命令块内不得出现盘符写盘路径（/dev/sdX、/dev/nvmeXnY[pZ]）：一律取 §3.4
        # 核准清单 /dev/disk/by-id/ 变量形式（行内含 by-id/approved 声明方可豁免；
        # 注释行中的源文档回显示意豁免）
        device_re = re.compile(r"/dev/sd[a-z]|/dev/nvme\d+n\d+p?\d*")
        for block in re.findall(r"```[^\n]*\n(.*?)```", self.guide, re.S):
            for line in block.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "by-id" in s or "approved" in s:
                    continue
                m = device_re.search(s)
                self.assertIsNone(m, "device-letter write path in command block: %s" % s)
        self.assertIn("APPROVED_DISK=$(grep", self.guide)
        self.assertIn('parted "$WAL_NVME" mklabel gpt', self.guide)
        self.assertIn("仅为示意", self.guide)

    def test_pool_delete_toggle_scoped(self):
        # 危险开关 mon_allow_pool_delete 不在基础 ceph.conf 预置（§3.2 已删 [mon] 段），
        # §6.3 删池前临时开启、删除完成后立即复位（危险开关用后即复位）
        self.assertNotIn("[mon]", self.guide)
        self.assertNotIn("mon_allow_pool_delete = true", self.guide)
        self.assertNotIn("mon_allow_pool_delete=true", self.guide)
        self.assertIn("ceph config set mon mon_allow_pool_delete true", self.guide)
        self.assertIn("ceph config set mon mon_allow_pool_delete false", self.guide)
        self.assertIn("危险开关用后即复位", self.guide)
        self.assertIn("危险开关用后即复位", self.skill)
        self.assertNotIn("mon_allow_pool_delete=true", self.skill)

    def test_pool_create_pg_matches_formula(self):
        # 建池 pg_num/pgp_num 与公式说明一致（§0 示例集群 36 OSD 三副本 → fs_data 1024、
        # fs_metadata 128），并注明现场部署按实际 OSD 数与副本数重算、不得照抄示例值
        self.assertIn("ceph osd pool create fs_data 1024 1024", self.guide)
        self.assertIn("ceph osd pool create fs_metadata 128 128", self.guide)
        self.assertNotIn("fs_data 32 32", self.guide)
        self.assertNotIn("fs_metadata 8 8", self.guide)
        self.assertIn("按实际 OSD 数与副本数重新计算", self.guide)
        self.assertIn("不得照抄", self.guide)

    def test_ro_status_identity_for_acl(self):
        # §8.2 排障改用一次性只读身份 client.ro-status（管理员上下文签发，setfacl 对象为
        # 该身份 keyring），不再对 client.admin keyring 授持久 ACL；补 ACL 与身份回收段
        self.assertNotIn("setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.admin.keyring", self.guide)
        self.assertIn("ceph auth get-or-create client.ro-status mon 'allow r' --name client.admin"
                      " --keyring /etc/ceph/ceph.client.admin.keyring"
                      " -o /etc/ceph/ceph.client.ro-status.keyring", self.guide)
        self.assertIn("setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.ro-status.keyring", self.guide)
        self.assertIn("setfacl -x u:ceph-adm", self.guide)
        self.assertIn("ceph auth rm client.ro-status", self.guide)
        self.assertIn("client.ro-status", self.skill)

    def test_ceph_deploy_pinned_and_venv_install(self):
        # ceph-deploy 源码安装供应链加固：固定 v2.1.0 tag 浅克隆、rev-parse HEAD 摘要与
        # 台账比对（不一致中止）、隔离 venv 安装（root 直装系统 Python 移除）；公开源
        # github.com 不得免校验 clone（命令级绕过仅限内网自签名镜像源）
        self.assertIn("git clone --branch v2.1.0 --depth 1 https://github.com/ceph/ceph-deploy.git",
                      self.guide)
        self.assertIn("git rev-parse HEAD", self.guide)
        self.assertIn("python3 -m venv /root/ceph-deploy-venv", self.guide)
        self.assertIn("/root/ceph-deploy-venv/bin/pip install /root/ceph-deploy", self.guide)
        self.assertNotIn("git -c http.sslVerify=false clone", self.guide)
        self.assertNotIn("setup.py install", self.guide)
        self.assertNotIn("setup.py install", self.skill)
        for block in re.findall(r"```[^\n]*\n(.*?)```", self.guide, re.S):
            for line in block.splitlines():
                if "sslVerify" in line or "no-check-certificate" in line:
                    self.assertNotIn("github.com", line,
                                     "no-verify clone against public source: %s" % line.strip())


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        text = read(SKILL_DIR / "SKILL.md")
        for sibling in ("ceph-object-storage-deployment", "ceph-block-storage-deployment",
                        "ceph-ansible-deployment"):
            self.assertIn(sibling, text, sibling)
        # python3 缺失降级口径：先补齐解释器，矩阵匹配且用户接受才转交，否则 blocked
        self.assertIn("缺失时先补齐 python3 解释器", text)
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
        self.assertEqual(self.evals["skill_name"], "ceph-file-storage-deployment")
        self.assertEqual(self.queries["skill_name"], "ceph-file-storage-deployment")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CFS-E"), entry["id"])
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
