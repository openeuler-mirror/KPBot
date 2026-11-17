"""Case contract checks for the kptools-openeuler-swegym-image-rebuilding skill package.

Run:  python3 -m unittest discover -s evals -v   (from the skill package root)
"""

import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]      # .../skills/kptools-openeuler-swegym-image-rebuilding
PLUGIN = PACKAGE.parents[1]                        # .../Plugins/app-tuner
CASE = PACKAGE / "cases" / "swegym-era-pin-drift"
SKILL_NAME = "kptools-openeuler-swegym-image-rebuilding"
ERA_PIN = "pyarrow==13.0.0"


class CaseContractTests(unittest.TestCase):
    def test_case_builds_from_plugin_root(self):
        text = (CASE / "case.yaml").read_text(encoding="utf-8")
        context_match = re.search(r"(?m)^  context:\s*(\S+)", text)
        dockerfile_match = re.search(r"(?m)^  dockerfile:\s*(\S+)", text)
        self.assertIsNotNone(context_match)
        self.assertIsNotNone(dockerfile_match)
        context = (CASE / context_match.group(1)).resolve()
        self.assertEqual(context, PLUGIN)
        dockerfile = context / dockerfile_match.group(1)
        self.assertTrue(dockerfile.is_file(), dockerfile)

    def test_case_yaml_declares_skill_and_metric(self):
        text = (CASE / "case.yaml").read_text(encoding="utf-8")
        self.assertIn(f"skill: {SKILL_NAME}", text)
        self.assertIn("metric: gold_ok", text)
        self.assertIn("gold_ok_with - gold_ok_without", text)
        self.assertIn("conditions: [with, without]", text)

    def test_dockerfile_copy_targets_exist(self):
        dockerfile = (CASE / "Dockerfile").read_text(encoding="utf-8")
        for line in dockerfile.splitlines():
            if not line.startswith("COPY "):
                continue
            m = re.match(r"COPY\s+(\S+)\s+", line)
            self.assertIsNotNone(m, line)
            # COPY 源路径仅允许两种：skill 包内路径（含 cases 剔除语义）与提示词
            src = PLUGIN / m.group(1)
            self.assertTrue(src.exists(), f"COPY 源不存在: {m.group(1)}")

    def test_dockerfile_fixture_has_drift_history(self):
        """fixture 必须有两笔提交（base 时代 → HEAD 漂移）且开箱自检 gold=0。"""
        dockerfile = (CASE / "Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(dockerfile.count("git commit"), 2,
                         "testbed 必须恰好两笔提交：base 时代 + 漂移")
        self.assertIn("pyarrow==13.0.0", dockerfile, "base 时代必须含精确 pin")
        self.assertIn('test "$(cat /workspace/results/gold.json)" = 0',
                      dockerfile, "镜像构建时必须自检开箱 gold=0（漂移态成立）")
        self.assertIn("sha256sum tests_official/", dockerfile,
                      "受保护文件必须固化 sha256 供 verify 防篡改复核")
        self.assertIn("rm -rf /staging/skill/cases", dockerfile,
                      "staging 的 skill 必须剔除 cases（防评测装置泄入上下文）")

    def test_prompts_asymmetry_only_skill_line(self):
        with_text = (CASE / "prompts" / "with-skill.md").read_text(encoding="utf-8")
        without_text = (CASE / "prompts" / "without-skill.md").read_text(encoding="utf-8")
        self.assertIn(SKILL_NAME, with_text, "with-skill 提示词必须提及 skill")
        self.assertNotIn(SKILL_NAME, without_text,
                         "without-skill 提示词不得提及 skill（否则变量不唯一）")
        self.assertNotIn("era-pin", without_text.lower().replace("era pin", "era-pin"),
                         "without 提示词不得泄露方法论关键词")
        # 除首行外两份提示词必须一致（唯一变量 = 是否提及 skill）
        with_rest = with_text.splitlines()[1:]
        without_rest = without_text.splitlines()[1:]
        self.assertEqual(with_rest, without_rest,
                         "提示词除 skill 首行外必须逐行一致")

    def test_verify_fail_closed(self):
        """判定必须是 fail-closed 四件套：防篡改 / 精确 pin / 亲自重跑 / 拒自述。"""
        verify = (CASE / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("sha256sum", verify, "必须复核受保护文件（防改判据作弊）")
        self.assertIn(ERA_PIN, verify, "必须检查 env/requirements.txt 的精确时代 pin")
        self.assertIn('subprocess.run(["bash", "run_gold.sh"]', verify,
                      "verify 必须亲自重跑官方验收（而非读取 agent 留下的旧结果）")
        self.assertIn("不接受 agent 自述", verify,
                      "必须显式声明拒收自述 gold 作为判据")
        self.assertIn("base_commit", verify, "必须核对报告锚点与真实 base 一致（防编造）")

    def test_run_ab_evidence_guard(self):
        run_ab = (CASE / "run_ab.sh").read_text(encoding="utf-8")
        self.assertIn("EVIDENCE_FAILED", run_ab, "必须有取证完整性守卫")
        self.assertIn("result.json 缺失或为空", run_ab)
        self.assertIn("--rm", run_ab.replace("不在白名单", "WHITELIST_NOTE"),
                      "必须显式处理 --rm 与取证的互斥（白名单排除或等价说明）")
        self.assertIn("REDACTED", run_ab, "回拷产物必须做凭证剔除")
        # 白名单自检：默认参数必须能通过自身白名单
        self.assertIn("RUN_ARGS 默认值未通过白名单", run_ab)

    def test_entrypoint_wires_skill_and_report_gate(self):
        entry = (CASE / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn(f"cp -r /staging/skill .opencode/skills/{SKILL_NAME}", entry)
        self.assertIn("opt/build_report.json not generated", entry,
                      "opencode 运行后必须存在报告产物门槛")
        self.assertIn("./verify.sh", entry)


if __name__ == "__main__":
    unittest.main()
