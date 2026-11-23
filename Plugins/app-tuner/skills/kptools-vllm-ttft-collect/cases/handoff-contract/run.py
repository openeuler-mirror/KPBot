"""Exercise the handoff CLI against the installed TTFT producer contract."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[2]
CLI = SKILL / 'scripts/handoff.py'
BASE = SKILL.parent / 'kptools-vllm-ttft/scripts/report.py'


@unittest.skipUnless(BASE.is_file(), f'install the KPBot !181 base first: {BASE}')
class Handoff(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = Path(temp.name) / 'prefill.log'
        self.receipt = Path(temp.name) / 'receipt.json'

    def cli(self, *args, status=0):
        proc = subprocess.run([sys.executable, '-B', str(CLI), *map(str, args)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, status, proc.stderr)
        return proc

    def collect(self, request, status=0):
        return self.cli('collect', '--log', self.log, '--request-id', request,
                        '--origin', 'existing_logs', status=status)

    def test_exact_request_and_digest(self):
        for request in ('chatcmpl-fixture-7', 'cmpl-fixture-7'):
            with self.subTest(request=request):
                line = f'[TTFT][BREAKDOWN] req={request} steps=1 ttft=0.5\n'
                self.log.write_text(line, encoding='utf-8')
                self.assertIn('no BREAKDOWN', self.collect('fixture', status=1).stderr)
                self.receipt.write_text(self.collect(request).stdout, encoding='utf-8')
                result = json.loads(self.cli('verify', self.receipt).stdout)
                self.assertEqual(result['request_id'], request)
                self.log.write_text(line + '# appended\n', encoding='utf-8')
                self.assertIn('no longer match', self.cli('verify', self.receipt, status=1).stderr)

    def test_prometheus_only_and_multiple_breakdowns_are_rejected(self):
        self.log.write_text('[TTFT_DEBUG][Prometheus] req=chatcmpl-a\n', encoding='utf-8')
        self.assertIn('no BREAKDOWN', self.collect('chatcmpl-a', status=1).stderr)
        self.log.write_text('[TTFT][BREAKDOWN] req=chatcmpl-a steps=1\n' * 2, encoding='utf-8')
        self.assertIn('exactly one BREAKDOWN', self.collect('chatcmpl-a', status=1).stderr)

    def test_invalid_schema_is_rejected(self):
        self.receipt.write_text('{"schema_version": 2}', encoding='utf-8')
        self.assertIn('invalid TTFT handoff schema', self.cli('verify', self.receipt, status=1).stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
