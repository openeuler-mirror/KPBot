"""Unit tests for auto_opencode_http (no real HTTP or opencode processes).

Covers the review findings on the opencode driver: completion detection
(_pipeline_complete), auto-answer selection (_auto_answer), and shutdown
(_shutdown) behave as designed without side effects on real services.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import auto_opencode_http as aoh  # noqa: E402


def make_session(workdir: Path) -> aoh.OpenCodeHttpSession:
    session = aoh.OpenCodeHttpSession.__new__(aoh.OpenCodeHttpSession)
    session.workdir = workdir
    session.log_dir = workdir / "logs"
    session.log_dir.mkdir(parents=True, exist_ok=True)
    session.deadline = time.time() + 60
    session.idle_timeout_seconds = 0
    session.port = 0
    session.password = "unused"
    session.proc = None
    session.session_id = "ses_test"
    session.events = []
    session.transcript_lines = []
    session.status = "running"
    session.error = None
    session.reply_counts = {}
    session.last_text = ""
    return session


class PipelineCompleteTests(unittest.TestCase):
    def test_batch_result_complete_and_verified(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            run_dir = workdir / "optimization_reports" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "batch_result.json").write_text(
                json.dumps({"pipeline_status": "complete", "quality_status": "verified"})
            )
            session = make_session(workdir)
            self.assertTrue(session._pipeline_complete())

    def test_batch_result_incomplete_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            run_dir = workdir / "optimization_reports" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "batch_result.json").write_text(
                json.dumps({"pipeline_status": "running", "quality_status": "unknown"})
            )
            session = make_session(workdir)
            self.assertFalse(session._pipeline_complete())

    def test_batch_result_malformed_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            run_dir = workdir / "optimization_reports" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "batch_result.json").write_text("{not json")
            session = make_session(workdir)
            self.assertFalse(session._pipeline_complete())

    def test_final_marker_in_last_message(self):
        with tempfile.TemporaryDirectory() as td:
            session = make_session(Path(td))
            session.last_text = "all done.\nSESSION_COMPLETE"
            self.assertTrue(session._pipeline_complete())

    def test_marker_mention_in_prose_does_not_complete(self):
        """P2 regression: a worker *mentioning* a marker literal inside prose
        ("将输出 SESSION_COMPLETE 标记") must not be mistaken for completion."""
        with tempfile.TemporaryDirectory() as td:
            session = make_session(Path(td))
            for text in (
                "将输出 SESSION_COMPLETE 标记",
                "BATCH_END 是收尾标志，收到后请停止",
                "after SESSION_END the driver stops",
                "all done, SESSION_COMPLETE",
                "TODO: print SESSION_COMPLETE",
            ):
                session.last_text = text
                self.assertFalse(
                    session._pipeline_complete(),
                    f"prose mention falsely completed: {text!r}",
                )

    def test_nothing_present(self):
        with tempfile.TemporaryDirectory() as td:
            session = make_session(Path(td))
            self.assertFalse(session._pipeline_complete())


class AutoAnswerTests(unittest.TestCase):
    def test_default_auto_reply_without_answer_bank(self):
        with tempfile.TemporaryDirectory() as td:
            session = make_session(Path(td))
            session._prompt = mock.MagicMock()
            session._auto_answer("nudge")
            sent = session._prompt.call_args[0][0]
            self.assertIn("自动", sent)
            self.assertEqual(session.reply_counts.get("auto_answer"), 1)

    def test_answer_bank_line_used_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            (workdir / aoh.ANSWER_BANK_FILE).write_text("选择：用例优化 自动模式\n")
            session = make_session(workdir)
            session._prompt = mock.MagicMock()
            session._auto_answer("stall")
            sent = session._prompt.call_args[0][0]
            self.assertIn("自动", sent)
            self.assertNotIn("请继续执行并完成全部阶段", sent)


class ShutdownTests(unittest.TestCase):
    def test_shutdown_without_process_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            session = make_session(Path(td))
            session.proc = None
            session.session_id = None
            session._shutdown()  # must not raise

    def test_shutdown_terminates_running_process(self):
        with tempfile.TemporaryDirectory() as td:
            session = make_session(Path(td))
            session.session_id = None  # skip interrupt API call
            session.proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
            time.sleep(0.1)
            session._shutdown()
            self.assertIsNotNone(session.proc.poll())  # terminated


if __name__ == "__main__":
    unittest.main()
