"""Exercise collection-to-report handoff without a running service."""

import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


SKILLS = Path(__file__).resolve().parents[3]
HANDOFF = Path(os.environ.get('TTFT_HANDOFF', SKILLS / 'kptools-vllm-ttft-collect/scripts/handoff.py')).resolve()
TTFT = Path(os.environ.get('TTFT_CLI', SKILLS / 'kptools-vllm-ttft/scripts/ttft.py')).resolve()
MISSING = [str(path) for path in (HANDOFF, TTFT, TTFT.with_name('report.py')) if not path.is_file()]
LOG = (
    '[TTFT][QUEUE] req=selected initial_queue_valid=true reason=available '
    'preemptions=0 queued_events=1 scheduled_events=1 queue_wait_s=0.1\n'
    '[TTFT_DEBUG][EngineCore] execution_id=e0 request_ids=selected '
    'prefill_request_ids=selected num_total=1 num_tokens=32\n'
    '[TTFT_DEBUG][AscendStoreSave] req_id=selected execution_id=e0 task_id=t0 '
    'tp_rank=0 pid=123 keys=2 bytes=1048576 put=0.125\n'
    '[TTFT][BREAKDOWN] req=selected steps=1 ttft=1 forward=0.2 save_kv=0.3\n'
)


@unittest.skipIf(MISSING, 'missing TTFT dependencies: ' + ', '.join(MISSING))
class HandoffContract(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / 'prefill.log'
        self.log.write_bytes(LOG.encode())
        self.receipt = self.root / 'receipt.json'

    def cli(self, script, *args, status=0):
        result = subprocess.run([sys.executable, '-B', str(script), *map(str, args)],
                                capture_output=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        return result

    def collect(self, request_id='selected', status=0, log=None):
        return self.cli(HANDOFF, 'collect', '--log', self.log if log is None else log,
                        '--request-id', request_id,
                        '--origin', 'existing_logs', status=status)

    def test_existing_logs_snapshot_generates_both_artifacts(self):
        collection_snapshot = self.root / 'collection-snapshot.log'
        collection_snapshot.write_bytes(self.log.read_bytes())
        receipt = self.collect(log=collection_snapshot).stdout
        self.receipt.write_text(receipt, encoding='utf-8')
        with self.log.open('ab') as source:
            source.write(b'[TTFT][BREAKDOWN] req=other steps=1 ttft=2\n')
        snapshot = self.root / 'report-snapshot.log'
        snapshot.write_bytes(collection_snapshot.read_bytes())
        result = self.cli(HANDOFF, 'verify', self.receipt, '--log', snapshot)
        evidence = json.loads(result.stdout)
        self.assertEqual(Path(evidence['prefill_log']), snapshot.resolve())
        self.assertEqual(self.receipt.read_text(encoding='utf-8'), receipt)
        self.assertEqual(evidence['origin'], 'existing_logs')
        html = self.root / 'report.html'
        self.cli(TTFT, 'check', snapshot)
        self.cli(TTFT, 'report', snapshot, '--req', evidence['request_id'],
                 '--mode', 'prefill', '-o', html)
        rendered = html.read_text(encoding='utf-8')
        self.assertIn('selected', rendered)
        self.assertIn('0.125', rendered)

    def test_missing_request_and_changed_log_are_rejected(self):
        self.assertIn('no BREAKDOWN', self.collect('select', status=1).stderr)
        self.receipt.write_text(self.collect().stdout, encoding='utf-8')
        self.log.write_bytes(LOG.encode() + b'# appended\n')
        result = self.cli(HANDOFF, 'verify', self.receipt, '--log', self.log, status=1)
        self.assertIn('no longer match', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_multiple_breakdowns_require_a_single_request_snapshot(self):
        with self.log.open('ab') as log:
            log.write(b'[TTFT][BREAKDOWN] req=other steps=1 ttft=2\n')
        self.assertIn('exactly one BREAKDOWN', self.collect(status=1).stderr)

    def test_future_schema_is_rejected(self):
        data = json.loads(self.collect().stdout)
        data['schema_version'] = 2
        self.receipt.write_text(json.dumps(data), encoding='utf-8')
        result = self.cli(HANDOFF, 'verify', self.receipt, '--log', self.log, status=1)
        self.assertIn('invalid TTFT handoff schema', result.stderr)


if __name__ == '__main__':
    result = unittest.main(verbosity=2, exit=False).result
    success = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    output = Path(__file__).resolve().parent / 'results' / 'result.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({
        'success': success,
        'tests_run': result.testsRun,
        'skipped': len(result.skipped),
        'failures': len(result.failures),
        'errors': len(result.errors),
    }, indent=2) + '\n', encoding='utf-8')
    sys.exit(0 if success else 1)
