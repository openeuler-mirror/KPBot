#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-object-storage-deployment skill (stdlib only).

Guards the frontmatter (name/license/metadata), the object-storage-deployment
reference (source annotation + key-fact spot checks against the BoostKit Ceph
guide document version 07), the boundary table (sibling routing), table pipe
balance, the evals contract, and the PR-review fix guards (TLS/no global
sslVerify/no --no-check-certificate, chmod 755, privileged guard, approved
disk list, limited client identity, image digest check, fsid variable,
mon_host, rgw_thread_pool_size, BlueFS location, verify IP, path-one rollback,
official PG formula, NVMe unified sizing, Ubuntu cephadm version, BoostKit
source) plus the round-2 review fix guards (no always-true privileged rewrite,
registry auth + digest pin + no insecure channel, fsid collected on host shell,
netplan gateway host address, no client.admin keyring ACL grant, no
ceph-deploy rgw delete). Run via run.sh, or directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-object-storage-deployment

REFERENCES = ("object-storage-deployment.md",)


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
        self.assertIn("name: ceph-object-storage-deployment\n", self.fm[:800])

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
        text = read(SKILL_DIR / "references" / "object-storage-deployment.md")
        self.assertIn("cephadm", text)
        self.assertIn("RGW", text)
        self.assertIn("17.2.8", text)


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        text = read(SKILL_DIR / "SKILL.md")
        for sibling in ("ceph-block-storage-deployment", "ceph-file-storage-deployment",
                        "ceph-parameter-tuning"):
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


class SourceBoostKitTests(unittest.TestCase):
    """来源恢复 BoostKit 前缀（SKILL.md + references，无前缀版 0 命中）。"""

    def test_skill_source_is_boostkit(self):
        text = read(SKILL_DIR / "SKILL.md")
        normalized = " ".join(text.split())   # 归一化跨行空白
        self.assertIn("鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南",
                      normalized)
        self.assertNotIn("鲲鹏分布式存储使能套件", text)

    def test_reference_source_is_boostkit(self):
        text = read(SKILL_DIR / "references" / "object-storage-deployment.md")
        self.assertIn("鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南", text)
        self.assertNotIn("鲲鹏分布式存储使能套件", text)


class TLSGuardTests(unittest.TestCase):
    """TLS 口径：references 全局关闭校验/免校验下载字面量全 0（SKILL.md 留禁令句与命令级写法）。"""

    def setUp(self):
        self.ref = read(SKILL_DIR / "references" / "object-storage-deployment.md")
        self.skill = read(SKILL_DIR / "SKILL.md")

    def test_reference_no_global_ssl_verify(self):
        for needle in ("--global http.sslVerify", "--global https.sslVerify",
                       "sslverify=false"):
            self.assertNotIn(needle, self.ref, needle)

    def test_reference_no_no_check_certificate(self):
        self.assertNotIn("--no-check-certificate", self.ref)

    def test_reference_installs_internal_ca(self):
        self.assertIn("update-ca-trust extract", self.ref)

    def test_skill_has_tls_rule_with_forbidden_literals(self):
        self.assertIn("TLS 校验保持开启", self.skill)
        self.assertIn("git -c http.sslVerify=false", self.skill)


class ReviewFixesGuardTests(unittest.TestCase):
    """PR 447 检视意见修复守卫：逐条锁定修复后的口径。"""

    def setUp(self):
        self.ref = read(SKILL_DIR / "references" / "object-storage-deployment.md")
        self.skill = read(SKILL_DIR / "SKILL.md")

    # 意见 1（TLS + chmod）
    def test_chmod_755(self):
        self.assertNotIn("chmod 777", self.ref)
        self.assertIn("chmod 755 /usr/sbin/cephadm", self.ref)

    # 意见 2：privileged 容器护栏
    def test_privileged_guard(self):
        self.assertIn("--privileged", self.ref)
        self.assertIn("privileged 护栏", self.ref)
        self.assertIn("podman stop/rm", self.ref)
        self.assertIn("privileged 容器护栏", self.skill)

    # 意见 3：逐机核准清单，禁范围式直清
    def test_approved_disk_list(self):
        for needle in ("ls -l /dev/disk/by-id", "findmnt", "ceph-volume lvm list",
                       "DATA_DISKS", "禁止范围式直清"):
            self.assertIn(needle, self.ref, needle)
        self.assertNotIn("for i in {a..l}", self.ref)
        self.assertIn("写盘前核准清单", self.skill)

    # 意见 4：限权身份，client.admin/_admin 禁入业务客户端（含 evals 全文扫描）
    def test_limited_identity_no_admin_on_client(self):
        evals_text = read(SKILL_DIR / "evals" / "evals.json")
        self.assertNotIn("admin ceph1 ceph2 ceph3 client1", self.ref)
        self.assertNotIn("--labels _admin,rgw", self.ref)
        self.assertNotIn("--labels _admin", evals_text)
        self.assertNotIn("podman cp /etc/ceph/ceph.client.admin.keyring", self.ref)
        self.assertIn("client.<app>", self.ref)
        self.assertIn("client.rgwapp", self.ref)
        self.assertIn("身份最小化", self.skill)

    # 意见 5：基础镜像认证渠道 + 可信摘要核对
    def test_image_digest_check(self):
        self.assertIn("sha256sum", self.ref)
        self.assertIn("digest", self.ref)
        self.assertNotIn("http://repo.huaweicloud.com", self.ref)

    # 意见 6：卸载 fsid 现场变量化（宿主机 shell 采集）
    def test_fsid_variable(self):
        self.assertIn("CLUSTER_FSID", self.ref)
        self.assertIn("$(ceph fsid)", self.ref)
        self.assertIn("cephadm shell -- ceph fsid", self.ref)
        self.assertIn("tr -d '\\r'", self.ref)
        self.assertNotIn("0cb8b6e4", self.ref)

    # 意见 7：mon_host 按 ceph1~ceph3 修正
    def test_mon_host_fixed(self):
        self.assertIn("mon_host = 198.51.100.166,198.51.100.167,198.51.100.168", self.ref)
        self.assertEqual(self.ref.count("198.51.100.160,198.51.100.167"), 0)

    # 意见 9：rgw_thread_pool_size 替代 num_threads
    def test_rgw_thread_pool_size(self):
        self.assertIn("rgw_thread_pool_size = 1024", self.ref)
        self.assertNotIn("num_threads=1024", self.ref)

    # 意见 10：BlueFS 定位改函数名+代码片段优先
    def test_bluefs_located_by_function_and_snippet(self):
        self.assertIn("3108-3118", self.ref)
        self.assertIn("_log_sync", self.ref)
        self.assertNotIn("3120", self.ref)

    # 意见 11：RGW 验证 IP 修正（笔误加注保留 1 处原 IP 字样）
    def test_rgw_verify_ip(self):
        self.assertIn("198.51.100.166:10001", self.ref)
        self.assertIn("疑似笔误", self.ref)
        self.assertEqual(self.ref.count("198.51.100.164"), 1)

    # 意见 12：路径一回退小节与 SKILL.md 输出契约对应（round-2：ceph-deploy 无 rgw 删除子命令）
    def test_path_one_rollback_section(self):
        self.assertIn("## 1.9 路径一回退", self.ref)
        self.assertIn("--yes-i-really-really-mean-it", self.ref)
        self.assertNotIn("ceph-deploy rgw delete", self.ref)
        self.assertIn("无 rgw 删除子命令", self.ref)
        self.assertIn("mon_allow_pool_delete", self.ref)
        self.assertIn("§1.9", self.skill)
        self.assertIn("无 rgw delete 子命令", self.skill)

    # 意见 13：官方 PG 口径
    def test_official_pg_formula(self):
        self.assertIn("(OSD 数 × 100) / pool size", self.ref)
        self.assertIn("ceph-parameter-tuning", self.ref)
        self.assertIn("最小可运行口径", self.ref)

    # 意见 14：NVMe 统一口径 + BlueStore 版本更正
    def test_nvme_unified(self):
        self.assertIn("12*180GB", self.ref)
        self.assertIn("12*60GB", self.ref)
        for needle in ("12*20GB", "12*45GB", "3*700GB", "14.2.10"):
            self.assertNotIn(needle, self.ref, needle)
        self.assertIn("Luminous", self.ref)

    # 意见 8：Ubuntu cephadm 版本可实证口径
    def test_ubuntu_cephadm_version(self):
        self.assertIn("17.1.0-0ubuntu3", self.ref)
        self.assertIn("17.2.9-0ubuntu0.22.04", self.ref)
        self.assertNotIn("17.2.8-0ubuntu0.22.04", self.ref)


class ReviewFixesRound2GuardTests(unittest.TestCase):
    """PR 447 第二轮检视意见（7 条）修复守卫。"""

    def setUp(self):
        self.ref = read(SKILL_DIR / "references" / "object-storage-deployment.md")
        self.skill = read(SKILL_DIR / "SKILL.md")

    # 意见 1（P1）：cephadm privileged 恒真改写废弃，恢复原判断
    def test_no_privileged_always_true_rewrite(self):
        self.assertNotIn("or not self.privileged", self.ref)
        self.assertNotIn("self.privileged or", self.ref)
        self.assertIn("if self.privileged:", self.ref)
        self.assertIn("恒真", self.ref)
        self.assertIn("MON/MGR/OSD/RGW", self.ref)
        self.assertIn("privileged 场景边界", self.ref)
        self.assertIn("不得 --privileged", self.ref)

    # 意见 2（P1）：本地 registry 鉴权 + 管理网段绑定 + digest 固定
    def test_registry_auth_and_digest_pin(self):
        self.assertIn("htpasswd", self.ref)
        self.assertIn("htpasswd -Bbn deploy", self.ref)
        self.assertIn("REGISTRY_AUTH=htpasswd", self.ref)
        self.assertIn("REGISTRY_AUTH_HTPASSWD_REALM", self.ref)
        self.assertIn("REGISTRY_AUTH_HTPASSWD_PATH", self.ref)
        self.assertIn("-p 198.51.100.166:5000:5000", self.ref)
        self.assertNotIn("-p 5000:5000", self.ref)
        self.assertIn("0.0.0.0 全网卡监听", self.ref)
        self.assertIn("skopeo inspect", self.ref)
        self.assertIn('DEFAULT_IMAGE = "[IP]:5000/ceph/ceph_release@sha256:', self.ref)
        self.assertNotIn('DEFAULT_IMAGE = "[IP]:5000/ceph/ceph_release:v17.2.8"', self.ref)
        self.assertIn("本地镜像仓库必须启用鉴权", self.skill)
        self.assertIn("digest 固定", self.skill)

    # 意见 2（P1）：本地仓不保留 insecure 明文通道配置
    def test_no_insecure_registry_channel(self):
        self.assertNotIn("insecure = true", self.ref)
        self.assertNotIn("insecure-registries", self.ref)
        self.assertIn("认证 + HTTPS", self.ref)

    # 意见 4：fsid 采集必须在宿主机 shell，不在 cephadm shell 交互块内
    def test_fsid_collected_on_host_shell(self):
        self.assertIn("CLUSTER_FSID=$(cephadm shell -- ceph fsid | tr -d '\\r')", self.ref)
        blocks = re.findall(r"```[^\n]*\n(.*?)```", self.ref, re.S)
        fsid_blocks = [b for b in blocks if "CLUSTER_FSID=$(" in b]
        self.assertTrue(fsid_blocks, "fsid 采集代码块缺失")
        for block in fsid_blocks:
            for line in block.splitlines():
                if line.strip() == "cephadm shell":
                    self.fail("fsid 采集代码块内出现裸 cephadm shell 交互块（变量须在宿主机 shell 赋值）")
        self.assertIn("宿主机 shell", self.skill)

    # 意见 5：netplan 网关改同网段可用主机地址，占位项注明按现场替换
    def test_netplan_gateway_host_address(self):
        self.assertNotIn("via: 192.0.2.0", self.ref)
        self.assertIn("via: 192.0.2.1", self.ref)
        self.assertIn("占位示例，须按现场规划逐项替换", self.ref)

    # 意见 6：不对 client.admin keyring 做 ACL 放权（限权 client.<app> keyring 替代）
    def test_no_admin_keyring_acl(self):
        self.assertNotIn(
            "setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.admin.keyring", self.ref)
        self.assertIn("sudo ceph -s", self.ref)
        self.assertIn(
            "setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.<app>.keyring", self.ref)


class EvalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evals = json.loads(read(SKILL_DIR / "evals" / "evals.json"))
        cls.queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))

    def test_skill_name_and_entries(self):
        self.assertEqual(self.evals["skill_name"], "ceph-object-storage-deployment")
        self.assertEqual(self.queries["skill_name"], "ceph-object-storage-deployment")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("COS-E"), entry["id"])
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
