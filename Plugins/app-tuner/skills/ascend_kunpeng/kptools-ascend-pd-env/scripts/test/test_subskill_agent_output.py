#!/usr/bin/env python3
"""Exercise the agent harness without calling a model."""
import io
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_subskill_agent as harness


class OutputTest(unittest.TestCase):
    def test_completed_items_ignore_blank_lines_and_other_events(self):
        stream = '\n{"type":"thread.started"}\n{"type":"item.completed","item":{"type":"agent_message"}}'
        self.assertEqual(harness.completed_items(stream), [{"type": "agent_message"}])

    def test_malformed_output_becomes_a_case_failure(self):
        for line in ('cli banner', '[]', '{"type":"item.completed"}'):
            with self.subTest(line=line), self.assertRaisesRegex(RuntimeError, 'line 2'):
                harness.completed_items('{"type":"thread.started"}\n' + line)

    def test_main_continues_after_bad_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = pathlib.Path(tmp) / 'skill'
            for name in ('collect-environment', 'analyze-environment', 'compare-p-over-time',
                         'compare-d-over-time', 'compare-p-and-d', harness.SUMMARY):
                entry = skill / 'subskills' / name / 'SKILL.md'
                entry.parent.mkdir(parents=True)
                entry.touch()

            def collect_role(work):
                harness.completed_items('not JSON')

            def collect_only(work):
                pass

            output = io.StringIO()
            with patch.object(harness, 'SKILL', skill), patch.dict('os.environ', CODEX_BIN=sys.executable), \
                    patch.object(harness, 'collect_role', collect_role), \
                    patch.object(harness, 'collect_only', collect_only), \
                    patch.object(sys, 'argv', ['test_subskill_agent.py', 'collect_role', 'collect_only']), \
                    redirect_stdout(output):
                self.assertEqual(harness.main(), 1)
            self.assertIn('FAIL collect_role:', output.getvalue())
            self.assertIn('PASS collect_only', output.getvalue())


if __name__ == '__main__':
    unittest.main()
