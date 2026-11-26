#!/usr/bin/env python3
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


CHECKER = Path(__file__).resolve().parents[2] / 'scripts' / 'check_shell_quality.py'


class QualityContract(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def run_check(self, *args, code=0, skip_shellcheck=True):
        command = [sys.executable, str(CHECKER)]
        if skip_shellcheck:
            command.append('--skip-shellcheck')
        result = subprocess.run(command + list(args), cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def write(self, path, data):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_healthy_deployment_not_executed_and_directory_filtering(self):
        data = b'#!/bin/bash\nprintf test > sentinel\nprintf "%s\\n" \\\n  --flag\n'
        script = self.write('deploy/nested/start.sh', data)
        self.write('deploy/other.txt', b'not a script\r\n')
        report = self.run_check('deploy', str(script))
        self.assertEqual(len(report['files']), 1)
        self.assertEqual(report['files'][0]['syntax'], 'passed')
        self.assertEqual(report['findings'], [])
        self.assertFalse((self.root / 'sentinel').exists())
        self.assertEqual(script.read_bytes(), data)

    def test_byte_defects_prevent_misleading_syntax_results(self):
        first = b'\xef\xbb\xbf#!/bin/bash\r\nech\ro\n'
        self.write('windows.sh', first)
        self.write('invalid.sh', b'#!/bin/bash\n# \xff\0\n')
        report = self.run_check('windows.sh', 'invalid.sh', code=1)
        self.assertEqual({(row['file'], row['line'], row['check']) for row in report['findings']}, {
            ('windows.sh', 1, 'bom'), ('windows.sh', 1, 'crlf'), ('windows.sh', 2, 'bare-cr'),
            ('invalid.sh', 2, 'encoding'), ('invalid.sh', 2, 'nul'),
        })
        self.assertTrue(all(row['syntax'] == 'blocked-by-format' for row in report['files']))
        self.assertEqual((self.root / 'windows.sh').read_bytes(), first)

    def test_continuation_candidate_differs_from_trailing_whitespace(self):
        self.write('start.sh', b'#!/bin/bash\necho test\necho x \\ \n --flag\necho y \\\\ \necho z\t\n')
        report = self.run_check('start.sh', code=2)
        self.assertEqual(report['files'][0]['syntax'], 'passed')
        self.assertEqual([(row['line'], row['check']) for row in report['findings']], [
            (3, 'continuation-space'), (5, 'trailing-whitespace'), (6, 'trailing-whitespace'),
        ])

    def test_final_line_checks_without_final_newline(self):
        self.write('last-line.sh', b'#!/bin/bash\necho test \\ ')
        report = self.run_check('last-line.sh', code=2)
        self.assertEqual([(row['line'], row['check']) for row in report['findings']], [
            (None, 'missing-final-newline'), (2, 'continuation-space'),
        ])

    def test_sourced_fragment_uses_explicit_interpreter(self):
        self.write('env.sh', b'export MODEL=demo')
        report = self.run_check('env.sh', code=2)
        self.assertEqual(report['errors'][0]['check'], 'interpreter')
        report = self.run_check('--shell', 'sh', 'env.sh')
        self.assertEqual(report['files'][0]['shell'], 'sh')
        self.assertEqual(report['files'][0]['syntax'], 'passed')
        self.assertEqual([row['check'] for row in report['findings']], ['missing-final-newline'])

    def test_syntax_failure_and_missing_input_have_distinct_exit_codes(self):
        self.write('broken.sh', b'#!/usr/bin/env bash\nif true; then\n  echo ok\n')
        report = self.run_check('broken.sh', code=1)
        self.assertEqual(report['files'][0]['syntax'], 'failed')
        self.assertEqual(report['findings'][0]['check'], 'syntax')
        self.assertGreaterEqual(report['findings'][0]['line'], 2)
        report = self.run_check('broken.sh', 'missing.sh', code=2)
        self.assertEqual(report['errors'][0]['file'], 'missing.sh')
        self.assertEqual(report['findings'][0]['check'], 'syntax')

    def test_unterminated_heredoc_fails_even_when_bash_n_exits_zero(self):
        self.write('heredoc.sh', b'#!/bin/bash\ncat <<EOF\nhello\n')
        report = self.run_check('heredoc.sh', code=1)
        self.assertEqual(report['files'][0]['syntax'], 'failed')
        self.assertEqual(len(report['findings']), 1)
        self.assertEqual(report['findings'][0]['line'], 2)
        self.assertEqual(report['findings'][0]['severity'], 'error')
        self.assertEqual(report['findings'][0]['check'], 'syntax')

    def test_shellcheck_reports_current_tool_state(self):
        self.write('quote.sh', b'#!/bin/sh\nprintf "%s\\n" $missing\n')
        report = self.run_check('quote.sh', skip_shellcheck=False)
        if shutil.which('shellcheck'):
            self.assertEqual(report['files'][0]['shellcheck'], 'findings')
            self.assertTrue(any(row['check'] == 'shellcheck' and 'SC2154' in row['message']
                                for row in report['findings']))
        else:
            self.assertEqual(report['files'][0]['shellcheck'], 'unavailable')
        self.assertEqual(self.run_check('quote.sh')['files'][0]['shellcheck'], 'skipped')


if __name__ == '__main__':
    unittest.main(verbosity=2)
