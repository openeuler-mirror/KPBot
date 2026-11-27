#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-block-expansion skill (stdlib only).

Guards the frontmatter (name/license/metadata), the block-expansion reference
(source annotation + key-fact spot checks against the Ceph guide document
version 07), the boundary table (sibling routing), the single-path contract
(no file/object expansion section titles or exclusive command facts), the
review-fix guards (rbd image function call consistency, output contract
matching reference capability, NTP server IP, BoostKit source title), table
pipe balance, and the evals contract. Round-2 review-fix guards cover: config
push (no ceph-deploy admin), OSD by-id fail-fast creation, minimized
firewall/SELinux scope with recovery, official PG formula (no pool_count),
pg_num rollback promise removal, sibling PR annotations, passwordless SSH
baseline (StrictHostKeyChecking=yes + ssh-keyscan prefill), expansion-baseline
independence and incremental mon_host append. Round-3 review-fix guards cover:
rich-rule source coverage for both the Public and the Cluster subnets, and the
accept-new literal ban (OpenSSH 7.4p1 in the OS matrix does not support it).
Run via run.sh, or directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-block-expansion

REFERENCES = ("block-expansion.md",)


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
        self.assertIn("name: ceph-block-expansion\n", self.fm[:800])

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
        text = read(SKILL_DIR / "references" / "block-expansion.md")
        self.assertIn("鲲鹏", text)
        self.assertIn("块存储扩容", text)
        self.assertIn("mon_initial_members", text)
        self.assertIn("ceph-deploy mon create", text)
        self.assertIn("pg_num", text)
        self.assertIn("rbd create", text)
        self.assertIn("ceph osd df", text)


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        text = read(SKILL_DIR / "SKILL.md")
        for sibling in ("ceph-block-storage-deployment", "ceph-file-expansion",
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
    """单路径守卫：本 skill 只覆盖块存储扩容路径。"""

    def _skill_texts(self):
        texts = [read(SKILL_DIR / "SKILL.md")]
        for name in REFERENCES:
            texts.append(read(SKILL_DIR / "references" / name))
        return texts

    def test_no_other_path_titles_or_commands(self):
        # 不得出现文件/对象存储扩容章节标题，也不得出现两路径专属命令事实
        # （rgw/mds/mount 具体命令）；边界表中的路由行文字不受限。
        for text in self._skill_texts():
            for line in text.splitlines():
                if line.startswith("#"):
                    self.assertNotIn("文件存储扩容", line, line[:80])
                    self.assertNotIn("对象存储扩容", line, line[:80])
            for fact in ("ceph-deploy rgw", "rgw_frontends", "civetweb",
                         "ceph-deploy mds create", "ceph fs ls",
                         "mount -t ceph", "umount",
                         "ceph.client.admin.keyring"):
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
        self.assertEqual(self.evals["skill_name"], "ceph-block-expansion")
        self.assertEqual(self.queries["skill_name"], "ceph-block-expansion")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CBX-E"), entry["id"])
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
    """检视意见闭环守卫：createimage 调用一致、契约与参考对齐、NTP 与来源口径。"""

    def test_rbd_image_function_name_consistent(self):
        # 块设备创建函数定义与调用必须同名（历史上误写单数形式，5 个 rbd 块设备不会创建）
        text = read(SKILL_DIR / "references" / "block-expansion.md")
        self.assertIn("createimages()", text)
        self.assertRegex(text, r"(?m)^\s*createimages\s*$")
        self.assertIsNone(re.search(r"(?m)^\s*createimage\s*$", text),
                          "块设备创建函数调用与定义名不一致")
        self.assertIn("rbd create image", text)  # 函数体确实创建块设备

    def test_output_contract_matches_reference_capability(self):
        # 输出契约不得承诺参考无对应步骤的内容（OSD/RGW 下线不在本 skill 范围）
        text = read(SKILL_DIR / "SKILL.md")
        self.assertNotIn("OSD 下线", text)
        self.assertNotIn("RGW 下线", text)
        self.assertIn("拆除、下线动作不在本 skill 范围", text)

    def test_ntp_server_points_to_ceph1_public_ip(self):
        # NTP 服务端为 ceph1，hosts 规划 ceph1 Public IP = 198.51.100.156（RFC 5737）
        text = read(SKILL_DIR / "references" / "block-expansion.md")
        self.assertIn("server 198.51.100.156", text)
        self.assertIn("ntpdate 198.51.100.156", text)
        self.assertNotIn("198.51.100.166", text)

    def test_source_title_is_boostkit_suite(self):
        title = "鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南"
        old_title = "鲲鹏分布式存储使能套件 Ceph 编译&部署&调优&测试指南"
        for name in ("SKILL.md", "references/block-expansion.md"):
            text = read(SKILL_DIR / name)
            self.assertIn(title, text, name)
            self.assertNotIn(old_title, text, name)


class Round2FixTests(unittest.TestCase):
    """第二轮检视意见闭环守卫：config push、OSD by-id、防火墙/SELinux 收敛、
    PG 官方公式、pg_num 不可还原承诺、姊妹 PR 标注、免密安全基线、扩容基线独立
    与 mon_host 增量式修改。"""

    def _skill_text(self):
        return read(SKILL_DIR / "SKILL.md")

    def _ref_text(self):
        return read(SKILL_DIR / "references" / "block-expansion.md")

    def test_config_push_replaces_admin(self):
        # ceph.conf 同步场景改用 config push（仅同步配置不下发 client.admin keyring）
        for text in (self._skill_text(), self._ref_text()):
            self.assertNotIn("ceph-deploy --overwrite-conf admin", text)
        ref = self._ref_text()
        self.assertIn("ceph-deploy --overwrite-conf config push ceph1 ceph2 ceph3 ceph4 ceph5", ref)
        self.assertIn("config push 仅同步配置", ref)
        self.assertIn("获准的管理主机", ref)

    def test_osd_create_uses_verified_by_id_disks(self):
        # 禁止按盘符循环盲建 OSD：逐机核对设备标识、仅对确认数据盘执行、遇错即停、写盘 by-id
        ref = self._ref_text()
        self.assertNotIn("{a..l}", ref)
        self.assertNotIn("/dev/sd", ref)
        self.assertIn("/dev/disk/by-id/", ref)
        for probe in ("lsblk", "df -l", "findmnt", "遇错即停"):
            self.assertIn(probe, ref, probe)
        self.assertIn("按盘符循环盲建", self._skill_text())

    def test_firewall_scope_minimized(self):
        # 防火墙不再全局停用：新增节点停用或放行 Ceph 端口，现有节点只加 rich rule，
        # 执行前授权确认并记录变更窗口
        ref = self._ref_text()
        self.assertNotIn("在所有 Ceph 节点和 Client 节点依次执行", ref)
        for port in ("6789", "3300", "6800-7568"):
            self.assertIn(port, ref, port)
        self.assertIn("--add-rich-rule", ref)
        self.assertIn("授权确认", ref)
        self.assertIn("变更窗口", ref)
        self.assertIn("ceph1~ceph3", ref)

    def test_selinux_scope_narrowed_with_recovery(self):
        # SELinux permissive 仅新增节点 + 授权确认；恢复项含 setenforce 1 / enforcing / firewalld
        ref = self._ref_text()
        self.assertNotIn("在所有主客户机节点执行", ref)
        self.assertIn("仅在新增节点", ref)
        self.assertIn("cp -n /etc/selinux/config", ref)
        self.assertIn("setenforce 1", ref)
        self.assertIn("SELINUX=enforcing", ref)
        self.assertIn("授权确认", ref)
        skill = self._skill_text()
        self.assertIn("setenforce 1", skill)
        self.assertIn("SELINUX=enforcing", skill)
        self.assertIn("systemctl enable --now firewalld", skill)

    def test_pg_formula_official_form(self):
        # 官方口径公式（EC 为 K+M、向上取整 2 的整数次幂、多池按占比分摊），无 pool_count 除法
        for text in (self._skill_text(), self._ref_text()):
            self.assertIsNone(re.search(r"/\s*pool_count", text))
            self.assertIn("向上取整到最近的 2 的整数次幂", text)
            self.assertIn("EC 为", text)
            self.assertIn("多池按各池数据占比分摊", text)
        # 修正说明注明与源文档口径差异（源指南额外按 pool 数均分作除法）
        self.assertIn("官方口径", self._skill_text())
        ref = self._ref_text()
        self.assertIn("官方口径", ref)
        self.assertIn("与官方口径不一致", ref)

    def test_pg_num_rollback_promise_removed(self):
        # Ceph 12.2.11 拒绝调低 pg_num：移除"用同款命令改回原值"承诺，改为核准目标 PG 数
        for text in (self._skill_text(), self._ref_text()):
            self.assertNotIn("改回原值", text)
            self.assertNotIn("还原原值", text)
        skill = self._skill_text()
        self.assertIn("拒绝调低 pg_num", skill)
        self.assertIn("核准目标 PG 数", skill)
        self.assertIn("拒绝调低 pg_num", self._ref_text())
        self.assertIn("mon destroy", self._ref_text())  # 有据回退项保留

    def test_unmerged_siblings_annotated_with_pr(self):
        # 未合入姊妹按「名字 + 同批 PR 编号」标注，禁止裸路由
        skill = self._skill_text()
        self.assertNotIn("同批姊妹 PR，同批交付", skill)
        self.assertIn("同批 PR #446，尚未合入", skill)
        self.assertIn("同批 PR #453，尚未合入", skill)
        self.assertIn("同批 PR #454，尚未合入", skill)
        self.assertIn("合入前该场景按通用流程处理并在产出中标注「未覆盖」", skill)
        queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))
        mapping = {"ceph-block-storage-deployment": "#446",
                   "ceph-file-expansion": "#453",
                   "ceph-object-expansion": "#454"}
        annotated = 0
        for q in queries["queries"]:
            if q["should_trigger"]:
                continue
            self.assertIn("routes to", q["note"], q["id"])
            for name, pr in mapping.items():
                if name in q["note"]:
                    self.assertIn("同批 PR %s，尚未合入" % pr, q["note"], q["id"])
                    annotated += 1
        self.assertEqual(annotated, 3, "三个未合入姊妹的负例均须带 PR 标注")

    def test_ssh_key_security_baseline(self):
        # 免密安全基线：口令私钥 + agent、StrictHostKeyChecking=yes + ssh-keyscan 预填
        # 已知主机（OpenSSH 7.4p1 不支持 accept-new，字面量禁入）、from= 来源限定、
        # ssh-keygen -R、撤销收尾；禁用写法（StrictHostKeyChecking=no /
        # UserKnownHostsFile）不得出现在命令块中（正文说明文字中的禁止性提及不受限）
        ref = self._ref_text()
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
        skill = self._skill_text()
        self.assertIn("authorized_keys", skill)

    def test_passwordless_scope_matches_commands(self):
        # 免密说明与命令作用域匹配：仅补齐 ceph1/client1 对 ceph4/ceph5 的免密
        ref = self._ref_text()
        self.assertIn("为 ceph1 与 client1 补齐对扩容节点 ceph4/ceph5 的免密", ref)
        self.assertNotIn("包括 ceph1 本身", ref)
        self.assertNotIn("包括 client1 本身", ref)
        self.assertNotIn("对所有主/客户机节点", ref)

    def test_expansion_baseline_independent_from_deployment(self):
        # 扩容基线独立声明：12.2.11/.156~158+192/197/198 与部署姊妹 #446 的 14.2.8 不通用
        ref = self._ref_text()
        self.assertIn("扩容基线独立", ref)
        self.assertIn("12.2.11", ref)
        self.assertIn("rpm-luminous", ref)
        self.assertIn("14.2.8", ref)
        self.assertIn("不得跨用", ref)
        self.assertIn("#446", ref)

    def test_mon_host_edit_is_incremental(self):
        # mon_initial_members/mon_host 改增量式追加，不依赖固定 before-state 示例值
        ref = self._ref_text()
        self.assertIn("在现有 mon_initial_members 末尾追加", ref)
        self.assertIn("在现有 mon_host 末尾追加", ref)
        self.assertNotIn("将 mon_host=198.51.100.156,198.51.100.157,"
                         "198.51.100.158 修改为", ref)
        self.assertNotIn("将 mon_initial_members=ceph1,ceph2,ceph3 修改为", ref)


class Round3FixTests(unittest.TestCase):
    """第三轮检视意见闭环守卫：rich rule 双网段源与 StrictHostKeyChecking 兼容性。"""

    def _ref_text(self):
        return read(SKILL_DIR / "references" / "block-expansion.md")

    def test_rich_rule_covers_public_and_cluster_sources(self):
        # 存量节点 rich rule 必须同时放行新节点的 Public 与 Cluster 地址源：
        # OSD peering/回填走 cluster_network，漏 Cluster 段会把 3300/6800-7568 上来自
        # Cluster 地址的连接拦掉，与扩容完成判据矛盾
        ref = self._ref_text()
        blocks = [b for b in re.findall(r"```bash\n(.*?)```", ref, re.S)
                  if "--add-rich-rule" in b]
        self.assertTrue(blocks, "rich rule 示例块缺失")
        rich = "\n".join(blocks)
        for source in ("198.51.100.197", "198.51.100.198",
                       "203.0.113.197", "203.0.113.198"):
            self.assertIn('source address="%s/32"' % source, rich, source)
        self.assertIn("漏 Cluster 段", ref)


if __name__ == "__main__":
    unittest.main()
