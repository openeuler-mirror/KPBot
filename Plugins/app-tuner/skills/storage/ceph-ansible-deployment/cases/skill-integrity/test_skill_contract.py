#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-ansible-deployment skill (stdlib only).

Guards the frontmatter (name/license/metadata), the ansible-deployment and
auto-deployment references (source annotation + key-fact spot checks against
the Ceph guide document version 07), the boundary table (storage-type sibling
routing), table pipe balance, review-fix safety guards (TLS/Git version check,
no client.admin distribution, keyring exported via `ceph auth get -o` with
0600 perms and no print-key stdout export, proxy temp env file,
site.yml/all.yml YAML shape, offline repo dir unification with relative-path
zip packing, plural infrastructure-playbooks dir, https-only rpm downloads
with signature checks and gpgcheck=1/repo_gpgcheck=1 local repos,
ceph_conf_overrides structure), and the evals contract. Run via run.sh, or
directly:

    py -3 test_skill_contract.py
"""

import json
import re
import unittest
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]      # .../skills/storage/ceph-ansible-deployment

REFERENCES = ("ansible-deployment.md",
    "auto-deployment.md",)


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
        self.assertIn("name: ceph-ansible-deployment\n", self.fm[:800])

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
        ansible = read(SKILL_DIR / "references" / "ansible-deployment.md")
        self.assertIn("ceph-ansible", ansible)
        self.assertIn("group_vars", ansible)
        auto = read(SKILL_DIR / "references" / "auto-deployment.md")
        self.assertIn("ceph_input", auto)
        self.assertIn("input_check.sh", auto)
        self.assertIn("install_all.sh", auto)


class SafetyGuardTests(unittest.TestCase):
    """Review-fix guards: TLS kept on, scoped keyring, proxy temp env, YAML
    shapes, offline repo dir unification, ceph_conf_overrides structure."""

    def setUp(self):
        self.ansible = read(SKILL_DIR / "references" / "ansible-deployment.md")
        self.auto = read(SKILL_DIR / "references" / "auto-deployment.md")

    def test_tls_verification_preserved_and_clone_version_checked(self):
        # TLS 校验保持开启（同已合入 ceph-build 铁律 5 口径）；克隆后核对版本并写入台账
        self.assertNotIn("sslverify=false", self.ansible)
        self.assertNotIn("--global http.sslVerify", self.ansible)
        self.assertNotIn("--no-check-certificate", self.ansible)
        self.assertIn("update-ca-trust", self.ansible)
        self.assertIn("git -c http.sslVerify=false", self.ansible)
        self.assertIn("git log -1 --oneline", self.ansible)

    def test_no_client_admin_keyring_to_business_clients(self):
        # 业务客户端不下发 client.admin，改用按实际用途限权的 client.<app> 身份并只分发该 keyring；
        # 导出用 ceph auth get -o 直写完整 INI keyring 文件，不经 stdout 明文输出
        self.assertNotIn("ceph.client.admin.keyring", self.auto)
        self.assertIn("不得将 client.admin", self.auto)
        self.assertIn("ceph auth get-or-create client.<app> mon 'allow r' osd 'allow rw pool=<pool>'",
                      self.auto)
        self.assertIn("ceph auth get client.<app> -o /etc/ceph/ceph.client.<app>.keyring",
                      self.auto)
        self.assertNotIn("auth print_key", self.auto)
        self.assertNotIn("print-key", self.auto)

    def test_app_keyring_perm_600_and_no_stdout_export(self):
        # 凭据 keyring 不得以 0644 等宽松权限留置/分发：服务端与客户端均 chmod 600，
        # 不得用 print-key/print_key 重定向写法产出裸 key（凭据不落日志，见 SKILL.md 铁律 6）
        self.assertGreaterEqual(
            self.auto.count("chmod 600 /etc/ceph/ceph.client.<app>.keyring"), 2)
        self.assertNotIn("> /etc/ceph/ceph.client.<app>.keyring", self.auto)
        self.assertIn("ls -l /etc/ceph/ceph.client.<app>.keyring", self.auto)

    def test_proxy_credentials_not_in_etc_environment(self):
        # 代理凭据只进安装账号家目录 600 权限临时 env 文件，不落 /etc/environment，安装后清理
        self.assertNotIn("在 /etc/environment 添加", self.ansible)
        self.assertNotIn("https_proxy= http", self.ansible)
        self.assertIn("不得写入 /etc/environment", self.ansible)
        self.assertIn("chmod 600 /root/.ceph_install_proxy.env", self.ansible)
        self.assertIn("rm -f /root/.ceph_install_proxy.env", self.ansible)

    def test_site_yml_hosts_group_indentation_and_syntax_check(self):
        # site.yml 组名必须缩进到 - hosts: 之下；部署前用文中 inventory 做 --syntax-check
        self.assertIn("- hosts:\n  - mons\n  - osds", self.ansible)
        self.assertNotIn("- hosts:\n- mons", self.ansible)
        self.assertIn("ansible-playbook -i /home/ceph-ansible/hosts "
                      "/home/ceph-ansible/site.yml --syntax-check", self.ansible)

    def test_centos_rgw_create_pools_nesting(self):
        # CentOS all.yml：池键与 systemd 参数恢复嵌套层级，pg_num/size 为各自池的子键；附 yaml 解析核对
        self.assertIn("rgw_create_pools:\n  defaults.rgw.buckets.data:\n    pg_num: 8", self.ansible)
        self.assertNotIn("rgw_create_pools:\ndefaults.rgw.buckets.data:", self.ansible)
        self.assertIn("ceph_rgw_systemd_overrides:\n  Service:\n    PrivateDevices: False",
                      self.ansible)
        self.assertIn("yaml.safe_load", self.ansible)

    def test_expansion_inventory_uses_hosts(self):
        # 扩容段 inventory 统一用 hosts（与前一步新增节点时的清单一致），不得写作 host
        self.assertNotIn("-i host add-osd.yml", self.ansible)
        self.assertEqual(self.ansible.count("ansible-playbook -i hosts add-osd.yml"), 2)

    def test_expansion_playbook_dir_plural(self):
        # 扩容 playbook 目录名全文唯一为复数 infrastructure-playbooks/（add-mon 与 add-osd/purge 一致）
        self.assertIn("cp ./infrastructure-playbooks/add-mon.yml ./add-mon.yml", self.ansible)
        self.assertIn("ansible-playbook -i hosts add-mon.yml", self.ansible)
        self.assertNotIn("infrastructure-playbook/", self.ansible)

    def test_local_source_zip_packs_relative_path(self):
        # §9 打包在 /home 下用相对路径 zip -r local_source.zip local_source，
        # 不得带绝对路径 /home/local_source（保证解包目录与 §1.6 baseurl 一致）；
        # §1.6 方法二解包后用 ls /home/local_source 回显核对解包目标目录
        self.assertNotIn("zip -r local_source.zip /home/local_source", self.ansible)
        self.assertEqual(self.ansible.count("zip -r local_source.zip local_source"), 2)
        s = self.ansible.index("方法二（离线）")
        e = self.ansible.index("### 1.7")
        region = self.ansible[s:e]
        self.assertIn("unzip local_source.zip", region)
        self.assertIn("ls /home/local_source", region)

    def test_no_plaintext_http_rpm_and_local_repo_gpgcheck(self):
        # 供应链：rpm 一律 https 下载并 rpm -K 签名校验（GPG key 导入 /etc/pki/rpm-gpg/）；
        # 本地 repo 一律 gpgcheck=1 + repo_gpgcheck=1（共 5 个：§1.6 三个 CentOS + openEuler 一个
        # + §1.7 local-rpm），不得保留 gpgcheck=0；SKILL.md 铁律 7 补供应链红线
        self.assertNotIn("http://mirrors.tools.huawei.com", self.ansible)
        self.assertIn("wget https://mirrors.tools.huawei.com", self.ansible)
        self.assertIn("rpm --import", self.ansible)
        self.assertIn("rpm -K", self.ansible)
        self.assertNotIn("gpgcheck=0", self.ansible)
        # 5 个本地 repo ini 块（§1.6 三个 CentOS + openEuler 一个 + §1.7 local-rpm）均带 gpgkey 与验签配置
        self.assertEqual(self.ansible.count("gpgkey=file:///etc/pki/"), 5)
        self.assertGreaterEqual(self.ansible.count("repo_gpgcheck=1"), 5)
        skill = read(SKILL_DIR / "SKILL.md")
        self.assertIn("禁止明文 http:// 下载", skill)
        self.assertIn("gpgcheck=0 的本地 repo", skill)

    def test_offline_repo_dirs_unified(self):
        # 离线制包/解包统一到 /home/local_source，repo 写入 /etc/yum.repos.d/，断网节点刷缓存验证
        self.assertNotIn("/home/yum.repo.d", self.ansible)
        s = self.ansible.index("方法二（离线）")
        e = self.ansible.index("### 1.7")
        region = self.ansible[s:e]
        self.assertIn("local_source.zip", region)
        self.assertIn("/etc/yum.repos.d", region)
        self.assertIn("file:///home/local_source/ceph", region)
        self.assertIn("yum clean all && yum makecache", region)
        s9 = self.ansible.index("## 9. repo 源压缩包制作")
        self.assertIn("/home/local_source", self.ansible[s9:])

    def test_ceph_conf_overrides_structure(self):
        # mon_max_pg_per_osd 在 ceph_conf_overrides.global 下、mon 段在 ceph_conf_overrides.mon 下；
        # 注明用 cat 生成文件 / ceph config show 核对
        self.assertIn("    mon_max_pg_per_osd: 3000", self.ansible)
        self.assertNotIn("\nmon_max_pg_per_osd: 3000", self.ansible)
        self.assertIn("  mon:\n    mon_allow_pool_create: true", self.ansible)
        self.assertIn("cat /etc/ceph/ceph.conf", self.ansible)
        self.assertIn("ceph config show", self.ansible)


class BoundaryTests(unittest.TestCase):
    def test_boundary_routes_to_siblings(self):
        text = read(SKILL_DIR / "SKILL.md")
        for sibling in ("ceph-block-storage-deployment", "ceph-block-expansion",
                        "ceph-file-expansion", "ceph-object-expansion",
                        "ceph-parameter-tuning"):
            self.assertIn(sibling, text, sibling)
        # 幻影 skill 名 ceph-expansion 不得再出现（非 ansible 扩容按存储类型路由到三个
        # expansion 姊妹 skill，类型不确定时按集群实际存储接口在三者中选择）
        self.assertNotIn("ceph-expansion", text)

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
        self.assertEqual(self.evals["skill_name"], "ceph-ansible-deployment")
        self.assertEqual(self.queries["skill_name"], "ceph-ansible-deployment")
        evals = self.evals["evals"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertLessEqual(len(evals), 3)
        ids = [e["id"] for e in evals]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in evals:
            self.assertTrue(entry["id"].startswith("CAD-E"), entry["id"])
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
