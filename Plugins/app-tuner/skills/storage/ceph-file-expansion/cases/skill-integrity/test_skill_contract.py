#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-file-expansion skill (stdlib only).

Guards the frontmatter (name/license/metadata), the file-expansion reference
(source annotation + key-fact spot checks against the Ceph guide document
version 07), the boundary table (sibling routing), the single-path contract
(no block/object expansion section titles or exclusive command facts), the
review-fix guards (umount spelling, stat -f mount verification, NTP server
IP, BoostKit source title), the round-2 review-fix guards (device-verified
OSD creation, firewall convergence, config push, secretfile remount, https
yum repo with pinned package, official PG formula, max_mds/fs status, step
renumbering, env rollback, authorization gating), and the round-3 review-fix
guards (mount secretfile pointing at a pure key file per the 12.2.11
mount.ceph semantics, unmerged sibling skills annotated with same-batch PR
numbers, SELinux scope narrowed to the new nodes with recovery, passwordless
SSH baseline ported from the block-expansion round-3 wording). Run via
run.sh, or directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-file-expansion

REFERENCES = ("file-expansion.md",)


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
        self.assertIn("name: ceph-file-expansion\n", self.fm[:800])

    def test_license_and_metadata_fields(self):
        self.assertIn("license: MulanPSL-2.0", self.fm)
        self.assertIn("compatibility:", self.fm)
        for field in ("skill_type: tool_assisted", "category: expansion",
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
        text = read(SKILL_DIR / "references" / "file-expansion.md")
        self.assertIn("鲲鹏", text)
        self.assertIn("文件存储扩容", text)
        self.assertIn("mon_initial_members", text)
        self.assertIn("ceph-deploy mon create", text)
        self.assertIn("ceph-deploy mds create", text)
        self.assertIn("mount -t ceph", text)
        self.assertIn("fs_metadata", text)
        self.assertIn("ceph osd df", text)


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        text = read(SKILL_DIR / "SKILL.md")
        for sibling in ("ceph-file-storage-deployment", "ceph-block-expansion",
                        "ceph-object-expansion", "ceph-parameter-tuning",
                        "ceph-build"):
            self.assertIn(sibling, text, sibling)

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


class SinglePathTests(unittest.TestCase):
    """单路径守卫：本 skill 只覆盖文件存储扩容路径。"""

    def _skill_texts(self):
        texts = [read(SKILL_DIR / "SKILL.md")]
        for name in REFERENCES:
            texts.append(read(SKILL_DIR / "references" / name))
        return texts

    def test_no_other_path_titles_or_commands(self):
        # 不得出现块/对象存储扩容章节标题，也不得出现两路径专属命令事实
        # （rgw/rbd 具体命令）；边界表中的路由行文字不受限。
        for text in self._skill_texts():
            for line in text.splitlines():
                if line.startswith("#"):
                    self.assertNotIn("块存储扩容", line, line[:80])
                    self.assertNotIn("对象存储扩容", line, line[:80])
            for fact in ("ceph-deploy rgw", "rgw_frontends", "civetweb",
                         "rbd create", "rbd ls", "rbd map"):
                self.assertNotIn(fact, text, fact)

    def test_no_banned_literals(self):
        # 示例 IP 仅允许 RFC 5737 文档保留地址（192.0.2./198.51.100./203.0.113.），
        # 禁止私网/内网示例字面量；来源套件名须为 BoostKit 版官方全称（见 ReviewFixTests）。
        banned = ("192.168" + ".", "172.19" + ".", "90.90" + ".")
        for text in self._skill_texts():
            for literal in banned:
                self.assertNotIn(literal, text, literal)


class EvalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evals = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        cls.queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))

    def test_skill_name_and_entries(self):
        self.assertEqual(self.evals["skill_name"], "ceph-file-expansion")
        self.assertEqual(self.queries["skill_name"], "ceph-file-expansion")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CFX-E"), entry["id"])
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


class ReviewFixTests(unittest.TestCase):
    """检视意见闭环守卫：umount 拼写、stat -f 验证落地、NTP 与来源口径。"""

    def test_umount_spelling_in_remount_step(self):
        # 客户端重新挂载步骤的卸载命令必须为正确拼写的 umount（历史上误写 umoun）
        text = read(SKILL_DIR / "references" / "file-expansion.md")
        self.assertIn("umount -t ceph", text)
        self.assertNotIn("umoun -t", text)

    def test_mount_verification_present(self):
        # SKILL.md 输出契约承诺 stat -f 验证，参考验证节必须有对应步骤
        text = read(SKILL_DIR / "references" / "file-expansion.md")
        self.assertIn("stat -f /mnt/cephfs", text)
        self.assertIn("df -h /mnt/cephfs", text)
        skill = read(SKILL_DIR / "SKILL.md")
        self.assertIn("stat -f", skill)

    def test_ntp_server_points_to_ceph1_public_ip(self):
        # NTP 服务端为 ceph1，hosts 规划 ceph1 Public IP = 198.51.100.156（RFC 5737）
        text = read(SKILL_DIR / "references" / "file-expansion.md")
        self.assertIn("server 198.51.100.156", text)
        self.assertIn("ntpdate 198.51.100.156", text)
        self.assertNotIn("198.51.100.166", text)

    def test_source_title_is_boostkit_suite(self):
        title = "鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南"
        old_title = "鲲鹏分布式存储使能套件 Ceph 编译&部署&调优&测试指南"
        for name in ("SKILL.md", "references/file-expansion.md"):
            text = read(SKILL_DIR / name)
            self.assertIn(title, text, name)
            self.assertNotIn(old_title, text, name)


class Round2ReviewFixTests(unittest.TestCase):
    """第二轮检视意见闭环守卫（PR 453 round-2，11 条）。"""

    def _ref(self):
        return read(SKILL_DIR / "references" / "file-expansion.md")

    def _skill(self):
        return read(SKILL_DIR / "SKILL.md")

    def test_osd_create_requires_device_verification(self):
        # 修复1：建 OSD 前逐机核对设备标识，仅确认数据盘执行、遇错即停、写 by-id
        ref = self._ref()
        self.assertIn("lsblk", ref)
        self.assertIn("/dev/disk/by-id", ref)
        self.assertIn("findmnt", ref)
        self.assertIn("df -l /", ref)
        self.assertIn("遇错即停", ref)
        self.assertIn("确认的数据盘", ref)
        self.assertNotIn("for i in {a..l}", ref)
        self.assertNotIn("/dev/sd", ref)
        skill = self._skill()
        self.assertIn("设备标识", skill)
        self.assertIn("by-id", skill)

    def test_firewall_scope_converged_to_new_nodes(self):
        # 修复2：防火墙收敛到新增节点（端口规则示例），现有节点仅 rich rule，需授权
        ref = self._ref()
        self.assertNotIn("在所有 Ceph 节点和 Client 节点", ref)
        self.assertIn("--add-port=6789/tcp", ref)
        self.assertIn("--add-port=3300/tcp", ref)
        self.assertIn("--add-port=6800-7568/tcp", ref)
        self.assertIn("--add-rich-rule", ref)
        self.assertIn("授权", ref)
        self.assertIn("变更窗口", ref)

    def test_pgnum_not_lowerable_and_values_collected(self):
        # 修复3/8：pg_num 不可调低、移除还原承诺、变更前采集原值入台账
        ref = self._ref()
        self.assertIn("ceph osd pool get fs_metadata pg_num", ref)
        self.assertIn("ceph osd pool get fs_data pg_num", ref)
        self.assertIn("不支持调低 pg_num", ref)
        self.assertIn("核准", ref)
        skill = self._skill()
        self.assertIn("pg_num/pgp_num", skill)
        self.assertIn("size", skill)
        self.assertNotIn("pgnum/pgpnum 还原", skill)

    def test_no_key_echo_or_inline_secret_mount(self):
        # 修复4/6/10：不回显密钥、不内联 secret、受限身份 + secretfile（600）
        pattern_cat = re.compile(r"cat .*keyring")
        pattern_secret = re.compile(r"secret=[A-Za-z0-9+/]")
        for name in ("SKILL.md", "references/file-expansion.md", "evals/evals.json"):
            text = read(SKILL_DIR / name)
            self.assertIsNone(pattern_cat.search(text), name)
            self.assertIsNone(pattern_secret.search(text), name)
            self.assertNotIn("name=admin", text, name)
        ref = self._ref()
        self.assertIn("client.fs-expansion", ref)
        self.assertIn("secretfile=", ref)
        self.assertIn("chmod 600", ref)
        self.assertIn("key= 行", ref)
        skill = self._skill()
        self.assertIn("argv", skill)
        self.assertIn("600", skill)

    def test_config_push_replaces_admin_push(self):
        # 修复5：仅同步配置用 config push，管理密钥限定管理主机
        ref = self._ref()
        self.assertIn("ceph-deploy --overwrite-conf config push", ref)
        self.assertNotIn("ceph-deploy --overwrite-conf admin", ref)
        self.assertIn("管理密钥只保留在管理主机", ref)

    def test_repo_https_gpgcheck_no_srpms_no_wildcard_install(self):
        # 修复9：yum 源 https、repo_gpgcheck=1、删源码仓段、钉死精确包名 + rpm -K
        ref = self._ref()
        self.assertIn("baseurl=https://download.ceph.com", ref)
        self.assertIn("repo_gpgcheck=1", ref)
        self.assertIn("rpm --import", ref)
        self.assertIn("rpm -q gpg-pubkey", ref)
        self.assertIn("rpm -K", ref)
        self.assertIn("ceph-12.2.11-<release>", ref)
        self.assertNotIn("http://download.ceph.com", ref)
        self.assertNotIn("[ceph-source]", ref)
        self.assertNotIn("SRPMS", ref)
        self.assertNotIn("yum -y install ceph*", ref)
        self.assertNotIn("ceph-debuginfo", ref)

    def test_pg_formula_official_no_pool_count(self):
        # 修复11：PG 公式官方口径（向上取整 2 的整数次幂、多池占比分摊）
        ref = self._ref()
        self.assertIn("(OSD 总数 × 100) / 最大副本数", ref)
        self.assertIn("2 的整数次幂", ref)
        self.assertIn("占比分摊", ref)
        self.assertNotIn("pool_count", ref)
        skill = self._skill()
        self.assertIn("2 的整数次幂", skill)
        self.assertIn("占比分摊", skill)
        self.assertNotIn("pool_count", skill)

    def test_mds_expansion_max_mds_and_fs_status(self):
        # 修复12：MDS 扩容口径——max_mds、新 MDS 默认 standby、ceph fs status 验证
        ref = self._ref()
        self.assertIn("ceph fs set", ref)
        self.assertIn("max_mds", ref)
        self.assertIn("standby", ref)
        self.assertIn("ceph fs status", ref)
        self.assertIn("ceph mds stat", ref)
        self.assertIn("实验特性", ref)
        skill = self._skill()
        self.assertIn("ceph fs status", skill)
        queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))
        cq02 = [q for q in queries["queries"] if q["id"] == "CQ-02"][0]
        self.assertIn("max_mds", cq02["note"])

    def test_remount_step_numbering_no_conflict(self):
        # 修复13：§3.2 步骤在本节内从步骤1 重新起号，消除双「步骤4」歧义
        ref = self._ref()
        self.assertNotIn("查看客户端访问密钥", ref)
        remount = ref.split("### 3.2", 1)[1].split("### 3.3", 1)[0]
        self.assertIn("# 步骤1", remount)
        self.assertNotIn("步骤4", remount)
        self.assertNotIn("步骤5", remount)

    def test_rollback_covers_env_restoration(self):
        # 修复7：输出契约回退步骤补环境类还原项
        skill = self._skill()
        self.assertIn("systemctl enable --now firewalld", skill)
        self.assertIn("setenforce enforcing", skill)
        self.assertIn("/etc/selinux/config", skill)
        self.assertIn("/etc/hosts", skill)
        self.assertIn("ntp.conf", skill)
        self.assertIn("ceph.repo", skill)
        self.assertIn("epel", skill)
        self.assertIn("卸载新增 Ceph 包", skill)

    def test_firewall_selinux_actions_require_authorization(self):
        # 修复2/7：降防护动作须授权、风险标注、Dependencies 标注 blocked
        skill = self._skill()
        self.assertIn("需用户授权的降防护动作", skill)
        self.assertIn("blocked", skill)
        ref = self._ref()
        self.assertGreaterEqual(ref.count("受控内网"), 2)
        self.assertIn("限变更窗口", ref)


class Round3ReviewFixTests(unittest.TestCase):
    """第三轮检视意见闭环守卫（PR 453 round-3，4 条）：secretfile 纯 key 文件、
    姊妹 PR 标注、SELinux 范围收敛、免密安全基线。"""

    def _ref(self):
        return read(SKILL_DIR / "references" / "file-expansion.md")

    def _skill(self):
        return read(SKILL_DIR / "SKILL.md")

    def test_secretfile_uses_pure_key_file(self):
        # 修复1：12.2.11 的 mount.ceph secretfile 只取文件首行纯 base64 key，keyring 文件
        # 首行是段头，整 keyring 文件作 secretfile 会报 secret is not valid base64；
        # 挂载改指向从 keyring 提取的 0600 纯 key 文件（含 ssh 提取与 scp 传输步骤），
        # keyring 留在管理主机供 ceph -s --name 受限身份验证
        ref = self._ref()
        self.assertIn("secretfile=/root/.ceph-fs-key", ref)
        self.assertIn('ssh ceph1 "grep -oP', ref)
        self.assertIn("scp /root/.ceph-fs-key client1:/root/.ceph-fs-key", ref)
        self.assertIn("chmod 600 /root/.ceph-fs-key", ref)
        self.assertIn("secret is not valid base64", ref)
        self.assertIn("ceph -s --name", ref)
        self.assertIsNone(re.search(r"secretfile=[^,\s]*keyring", ref),
                          "mount secretfile 不得指向 keyring 文件")
        skill = self._skill()
        self.assertIn("纯 key 文件", skill)
        self.assertIn("ceph -s --name", skill)
        self.assertIsNone(re.search(r"secretfile=[^,\s]*keyring", skill))

    def test_unmerged_siblings_annotated_with_pr(self):
        # 同批姊妹已合入：仍按「名字 + 同批 PR 编号」标注（已合入态），禁止裸路由
        # 「同批姊妹 PR，同批交付」，也不得残留合入前的降级措辞
        skill = self._skill()
        self.assertNotIn("同批姊妹 PR，同批交付", skill)
        self.assertIn("同批 PR #448，已合入", skill)
        self.assertIn("同批 PR #452，已合入", skill)
        self.assertIn("同批 PR #454，已合入", skill)
        self.assertNotIn("合入前该场景按通用流程处理并在产出中标注「未覆盖」", skill)
        queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))
        mapping = {"ceph-file-storage-deployment": "#448",
                   "ceph-block-expansion": "#452",
                   "ceph-object-expansion": "#454"}
        annotated = 0
        for q in queries["queries"]:
            if q["should_trigger"]:
                continue
            self.assertIn("routes to", q["note"], q["id"])
            for name, pr in mapping.items():
                if name in q["note"]:
                    self.assertIn("同批 PR %s，已合入" % pr, q["note"], q["id"])
                    annotated += 1
        self.assertEqual(annotated, 3, "三个姊妹负例均须带 PR 标注")

    def test_selinux_scope_narrowed_with_recovery(self):
        # 修复3：SELinux permissive 仅新增节点 ceph4/ceph5 执行 + 授权确认 + cp -n 备份；
        # 收尾恢复含 setenforce 1 与 /etc/selinux/config 恢复 SELINUX=enforcing
        ref = self._ref()
        self.assertNotIn("在所有主客户机节点执行", ref)
        self.assertIn("仅在新增节点", ref)
        self.assertIn("cp -n /etc/selinux/config", ref)
        self.assertIn("setenforce 1", ref)
        self.assertIn("SELINUX=enforcing", ref)
        self.assertIn("授权确认", ref)
        skill = self._skill()
        self.assertIn("setenforce enforcing", skill)
        self.assertIn("/etc/selinux/config", skill)

    def test_ssh_key_security_baseline(self):
        # 修复4：免密安全基线（对齐 #452 round-3 口径）——口令私钥 + agent、
        # StrictHostKeyChecking=yes + ssh-keyscan 预填已知主机（OpenSSH 7.4p1 不支持
        # accept-new，字面量禁入）、from= 来源限定、ssh-keygen -R、撤销收尾；无 rsa
        # 裸回车形态；禁用写法（StrictHostKeyChecking=no / UserKnownHostsFile）不得
        # 出现在命令块中（正文说明文字中的禁止性提及不受限）
        ref = self._ref()
        self.assertNotIn("ssh-keygen -t rsa", ref)
        self.assertNotIn("按回车用默认配置", ref)
        self.assertIn("ssh-keygen -t ed25519 -a 100", ref)
        self.assertIn("rsa -b 4096", ref)
        self.assertIn("-o StrictHostKeyChecking=yes", ref)
        self.assertIn("ssh-keyscan", ref)
        self.assertNotIn("accept-new", ref)
        self.assertIn("ssh-keygen -R", ref)
        self.assertIn('from="198.51.100.0/24"', ref)
        self.assertIn("passphrase", ref)
        self.assertIn("ssh-agent", ref)
        for block in re.findall(r"```bash\n(.*?)```", ref, re.S):
            self.assertNotIn("StrictHostKeyChecking=no", block)
            self.assertNotIn("UserKnownHostsFile", block)
        skill = self._skill()
        self.assertIn("authorized_keys", skill)

    def test_passwordless_scope_matches_commands(self):
        # 修复4：免密说明与命令作用域匹配——仅补齐 ceph1/client1 对扩容节点 ceph4/ceph5
        # 的免密，既有节点沿用部署期配置
        ref = self._ref()
        self.assertIn("为 ceph1 与 client1 补齐对扩容节点 ceph4/ceph5 的免密", ref)
        self.assertIn("沿用部署期配置", ref)
        self.assertNotIn("包括 ceph1 本身", ref)
        self.assertNotIn("包括 client1 本身", ref)
        self.assertNotIn("对所有主/客户机节点", ref)


if __name__ == "__main__":
    unittest.main()
