#!/usr/bin/env python3
"""Tier-A unit tests for the ceph-build skill (stdlib only).

Guards the frontmatter (name/license/metadata), the build-guide reference
(source annotation + key-command spot checks against the Kunpeng Ceph official guide), the boundary table (sibling routing), table pipe
balance, and the evals contract. Round-2 review-fix guards cover the
line-anchored ceph-install routing assertions (boundary-table row,
Dependencies gcc/cmake row, description negative route); round-3 covers
the deployment-axis anchors (boundary-table cluster-deployment row
routing to ceph-ansible-deployment / ceph-block-storage-deployment,
same-batch PRs #445/#446); round-4 covers the three-face deployment-axis
anchors (frontmatter description routing phrase, eval_queries NQ-02 note,
build-guide handover paragraph); round-5 covers the NQ-01 note anchor
(ceph-install axis), the SP4-scoped fallback condition (16.2.7 only ships
in the SP4 system-image source), and the ansible-leg matrix annotations
(boundary row + build-guide handover). Run via run.sh, or directly:

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
        for sibling in ("ceph-parameter-tuning", "ceph-install",
                        "ceph-ansible-deployment",
                        "ceph-block-storage-deployment"):
            self.assertIn(sibling, text, sibling)
        # 本断言的 ceph-install 存在性以 #444 同批交付为前提；合并次序约束：
        # 本 PR（#469）不得先于 #444/#445/#446 合入，否则 master 上 ceph-build 将
        # 指向未合入 skill（GitCode mergeable 无次序门禁，需人工保证顺序）。
        self.assertNotIn("ceph-optimization", text, "ceph-optimization")


class RoutingAnchorTests(unittest.TestCase):
    """检视意见闭环守卫（PR 469 round-2/round-3）：ceph-install 路由按行锚定，
    替代全文子串弱断言（assertIn("ceph-install") 会把任意正文提及误当路由）；
    round-3 补部署轴锚定（集群部署行内含两个姊妹 skill 名与同批 PR 标注）；
    round-4 补部署轴三面锚定（description / eval_queries NQ-02 note /
    build-guide 交接段），任一面静默回退单独红；round-5 补 NQ-01 note 锚定
    （ceph-install 轴三要素）、降级列 OS 条件收敛为 openEuler 22.03 LTS SP4
    （16.2.7 仅集成于 SP4 系统镜像源）与 ansible 腿矩阵括注（边界表行 +
    build-guide 交接段）。"""

    def _lines(self):
        return read(SKILL_DIR / "SKILL.md").splitlines()

    def _frontmatter(self):
        return frontmatter(read(SKILL_DIR / "SKILL.md"))

    def test_boundary_row_anchors_ceph_install(self):
        # 边界表行断言：含「| Ceph repo 安装（16.2.7」前缀的行内含 skill ceph-install
        rows = [line for line in self._lines()
                if line.strip().startswith("| Ceph repo 安装（16.2.7")]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("skill `ceph-install`", rows[0], rows[0])
        self.assertIn("同批 PR #444，同批交付", rows[0], rows[0])

    def test_boundary_row_anchors_deployment_siblings(self):
        # 边界表集群部署行断言：含「| 集群部署（MON/MGR/OSD 初建」前缀的行内含
        # 两个姊妹 skill 名与同批 PR #445/#446 标注（与 ceph-install 轴同款锚定），
        # 且 ceph-ansible 路径括注其矩阵（14.2.22/CentOS 7.6/20.03，不覆盖本
        # skill 的 14.2.8 产物与 22.03 路径）
        rows = [line for line in self._lines()
                if line.strip().startswith("| 集群部署（MON/MGR/OSD 初建")]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("skill `ceph-ansible-deployment`", rows[0], rows[0])
        self.assertIn("`ceph-block-storage-deployment`", rows[0], rows[0])
        self.assertIn("同批 PR #445/#446，同批交付", rows[0], rows[0])
        self.assertIn("含 ceph-ansible 路径（其矩阵为 14.2.22/CentOS 7.6/20.03）",
                      rows[0], rows[0])

    def test_description_anchors_deployment_siblings(self):
        # description 面断言：frontmatter description 的集群部署路由片段含两个姊妹
        # skill 名与同批 PR #445/#446 标注（description 为折叠块，跨行文本按空白
        # 归一后匹配），且不含「当前仓库无对应 skill」回退文案
        fm = self._frontmatter()
        self.assertIn(
            "skill `ceph-ansible-deployment` / `ceph-block-storage-deployment`"
            "（同批 PR #445/#446，同批交付）",
            " ".join(fm.split()))
        self.assertNotIn("当前仓库无对应 skill", fm)

    def test_eval_queries_nq02_note_anchors_deployment_siblings(self):
        # evals 面断言：eval_queries.json 中 id==NQ-02 项的 note 含两个姊妹 skill
        # 名与同批 PR #445/#446 标注（任一要素缺失即单独红）
        queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))["queries"]
        rows = [q for q in queries if q["id"] == "NQ-02"]
        self.assertEqual(len(rows), 1, rows)
        note = rows[0]["note"]
        self.assertIn("ceph-ansible-deployment", note, note)
        self.assertIn("ceph-block-storage-deployment", note, note)
        self.assertIn("同批 PR #445/#446", note, note)

    def test_eval_queries_nq01_note_anchors_ceph_install(self):
        # evals 面断言：eval_queries.json 中 id==NQ-01 项的 note 含 ceph-install、
        # 16.2.7 repo 安装与同批 PR #444 标注（与 NQ-02 部署轴锚定同款，
        # 任一要素缺失即单独红）
        queries = json.loads(read(SKILL_DIR / "evals" / "eval_queries.json"))["queries"]
        rows = [q for q in queries if q["id"] == "NQ-01"]
        self.assertEqual(len(rows), 1, rows)
        note = rows[0]["note"]
        self.assertIn("ceph-install", note, note)
        self.assertIn("16.2.7 repo 安装", note, note)
        self.assertIn("同批 PR #444", note, note)

    def test_build_guide_handover_anchors_deployment_siblings(self):
        # build-guide 面断言：安装验证后的边界交接段含两姊妹承接句（各腿括注其
        # 矩阵与主承接腿标注，跨行文本按空白归一后匹配），且全文不含
        # 「当前仓库无对应 skill」回退文案
        guide = read(SKILL_DIR / "references" / "build-guide.md")
        norm = " ".join(guide.split())
        self.assertIn(
            "MON/MGR/OSD 部署由姊妹 skill 承接—— "
            "`ceph-ansible-deployment`（其矩阵为 Ceph 14.2.22、CentOS 7.6/openEuler 20.03，"
            "不覆盖本 skill 的 14.2.8 产物与 22.03 路径）或 "
            "`ceph-block-storage-deployment`（Ceph 14.2.8/16.2.7、",
            norm)
        self.assertIn(
            "OS 含 openEuler 22.03；本 skill 14.2.8 自编译产物——尤其 22.03 编译路径——的部署承接",
            norm)
        self.assertIn(
            "以该 skill 为主）（同批 PR #445/#446，同批交付）；"
            "不得在本 skill 流程内混跑部署动作",
            norm)
        self.assertNotIn("当前仓库无对应 skill", guide)

    def test_dependencies_row_anchors_ceph_install(self):
        # Dependencies gcc/cmake 行断言：该行含 ceph-install，且降级列保留 blocked
        # 并改为条件化转交（14.2.8 自编译 RPM 无法由 16.2.7 安装交付；仅当 OS 为
        # openEuler 22.03 LTS SP4——ceph-install 矩阵，16.2.7 仅集成于 SP4 系统镜像
        # 源——且用户明确接受改用 16.2.7 repo 安装时转交）
        rows = [line for line in self._lines()
                if line.lstrip().startswith("|") and "devtoolset-8-gcc-c++" in line
                and "cmake" in line]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("ceph-install", rows[0], rows[0])
        self.assertIn("blocked", rows[0], rows[0])
        self.assertIn("14.2.8 自编译 RPM 无法由 16.2.7 安装交付", rows[0], rows[0])
        self.assertIn("仅当 OS 为 openEuler 22.03 LTS SP4（ceph-install 矩阵，"
                      "16.2.7 仅集成于 SP4 系统镜像源）", rows[0], rows[0])
        self.assertIn("且用户明确接受改用 16.2.7 repo 安装时", rows[0], rows[0])
        self.assertIn("转交 skill `ceph-install`", rows[0], rows[0])
        self.assertIn("同批 PR #444", rows[0], rows[0])

    def test_description_negative_route_updated(self):
        # description 负例断言：description 不再含「当前仓库无对应 skill」与「规划中」
        # （repo 安装已互指 ceph-install），且含 ceph-install 互指（OS 条件收敛为
        # openEuler 22.03 LTS SP4——ceph-install 矩阵，16.2.7 仅集成于 SP4 系统镜像
        # 源；description 为折叠块，跨行文本按空白归一后匹配）与同批 PR 标注
        fm = self._frontmatter()
        self.assertNotIn("当前仓库无对应 skill", fm)
        self.assertNotIn("规划中", fm)
        self.assertIn("ceph-install", fm)
        self.assertIn(
            "16.2.7 repo 安装（openEuler 22.03 LTS SP4，ceph-install 矩阵，"
            "16.2.7 仅集成于 SP4 系统镜像源）→ skill `ceph-install`",
            " ".join(fm.split()))
        self.assertIn("同批 PR #444，同批交付", fm)

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
