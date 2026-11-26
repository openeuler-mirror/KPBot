"""Tests for worker_profiles and the opencode adaptation in auto_claude_logged."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import auto_claude_logged as acl  # noqa: E402
from worker_profiles import get_profile  # noqa: E402


class WorkerProfileTests(unittest.TestCase):
    def test_claude_default(self):
        p = get_profile("claude")
        self.assertEqual(p.command, ["claude"])
        self.assertEqual(p.exit_cmd, b"/exit\r")
        self.assertTrue(p.ready_re.search("❯ What would you like to do?"))

    def test_opencode_command_and_ready(self):
        p = get_profile("opencode")
        self.assertEqual(p.command, ["opencode", "--auto"])
        self.assertTrue(p.ready_re.search("Ask anything"))
        self.assertTrue(p.busy_re.search("esc to interrupt"))

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):
            get_profile("cursor")

    def test_claude_bin_override(self):
        p = get_profile("claude", "/custom/claude")
        self.assertEqual(p.command, ["/custom/claude"])

    def test_opencode_ignores_claude_bin(self):
        p = get_profile("opencode", "/custom/claude")
        self.assertEqual(p.command, ["opencode", "--auto"])

    def test_get_profile_preserves_start_pipeline_cmd(self):
        # Regression: get_profile must carry over start_pipeline_cmd, or the
        # opencode pty path would fall back to the claude slash command.
        self.assertEqual(
            get_profile("opencode").start_pipeline_cmd,
            "请运行 kpbot-code-optimizer 技能，按自动模式执行完整优化流水线",
        )
        self.assertEqual(get_profile("claude").start_pipeline_cmd, "/kpbot-code-optimizer")


class AnswerForWorkerTests(unittest.TestCase):
    def test_claude_ready_prompt_sends_start(self):
        buf = "claude v2 stuff\n❯ What would you like to do?\n"
        ans = acl.answer_for(buf, "", {}, worker=get_profile("claude"))
        self.assertIsNotNone(ans)
        self.assertEqual(ans[0], "start_pipeline")

    def test_opencode_ready_when_idle(self):
        # opencode idle: placeholder visible, no busy indicators
        buf = "Ask anything  ·  \"What is the tech stack of this project?\"\n/tmp/proj:master"
        ans = acl.answer_for(buf, "", {}, worker=get_profile("opencode"))
        self.assertIsNotNone(ans)
        self.assertEqual(ans[0], "start_pipeline")

    def test_opencode_busy_blocks_ready_trigger(self):
        # busy: input box repaints but worker is processing
        buf = "Ask anything\nesc to interrupt\nWorking on it..."
        ans = acl.answer_for(buf, "", {}, worker=get_profile("opencode"))
        self.assertNotEqual(ans and ans[0], "start_pipeline")

    def test_default_worker_is_claude_compatible(self):
        # worker=None keeps legacy behaviour
        buf = "❯ What would you like to do?\n"
        ans = acl.answer_for(buf, "", {})
        self.assertEqual(ans[0], "start_pipeline")


if __name__ == "__main__":
    unittest.main()
