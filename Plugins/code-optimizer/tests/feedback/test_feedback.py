"""Logic-only fixtures: these tests do not measure target performance."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from experiment_feedback import choose_next
from experiment_record import write_record
from test_record import sample

SCRIPT = Path(__file__).parents[2] / 'skills/kpbot-code-optimizer/scripts/experiment_feedback.py'


def feedback(verdict='supported', status='completed'):
    return dict(execution_status=status, hypothesis_verdict=verdict,
                evidence_checked=True, target_checked=True)


class FeedbackTests(unittest.TestCase):
    def test_routes(self):
        for verdict, status, remaining, expected in [
            ('supported', 'completed', 0, 'DecideOptimization'),
            ('refuted', 'completed', 2, 'AnalyzeHotspot'),
            ('inconclusive', 'completed', 2, 'DesignExperiment'),
            ('inconclusive', 'completed', 0, 'stop_budget'),
            ('invalid', 'completed', 2, 'blocked_evidence'),
            ('inconclusive', 'awaiting_target_evidence', 2, 'awaiting_target_evidence'),
            ('inconclusive', 'failed', 2, 'blocked_execution'),
        ]:
            with self.subTest(verdict=verdict, status=status):
                self.assertEqual(choose_next(feedback(verdict, status), remaining), expected)
        for field in ('evidence_checked', 'target_checked'):
            f = feedback(); f[field] = False
            self.assertEqual(choose_next(f, 2), 'blocked_evidence')

    def test_strict_types(self):
        for remaining in (-1, True, 1.5, '2'):
            with self.assertRaises(ValueError):
                choose_next(feedback(), remaining)
        for field, value in [('execution_status', 'unknown'), ('hypothesis_verdict', 'unknown'),
                             ('evidence_checked', 1), ('target_checked', 'true')]:
            f = feedback(); f[field] = value
            with self.assertRaises(ValueError):
                choose_next(f, 2)
        with self.assertRaises(ValueError):
            choose_next({}, 2)
        for verdict in ('supported', 'refuted', 'invalid'):
            with self.assertRaises(ValueError):
                choose_next(feedback(verdict, 'awaiting_target_evidence'), 2)

    def cli(self, mutate=None, remaining=2):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            record = sample(root)
            record['execution_context'] = dict(target='test-target', build='test-build', workload='test-input')
            record['experiment']['exit_code'] = 0
            context = {k: copy.deepcopy(record[k]) for k in
                       ('run_id', 'point_id', 'parent_state', 'execution_context')}
            item = record['evidence'][0]
            item['source'] = 'target'
            item['provenance'] = {k: copy.deepcopy(record[k]) for k in
                                  ('parent_state', 'execution_context')}
            item['provenance']['scope'] = item['scope']
            path = root / 'experiments/e1/record.json'
            f = feedback()
            f.update({k: record[k] for k in ('run_id', 'point_id', 'experiment_id', 'hypothesis_id')})
            f.update(explanation='Logic fixture only: observation matches declared criterion.',
                     record_path='experiments/e1/record.json')
            if mutate:
                mutate(record, context, f)
            write_record(record, root)
            if context is not None:
                (root / 'current_context.json').write_text(json.dumps(context))
            (root / 'feedback.json').write_text(json.dumps(f))
            if f.pop('_remove_raw', False):
                (root / 'raw.txt').unlink()
            if f.pop('_remove_context', False):
                (root / 'current_context.json').unlink()
            result = subprocess.run([sys.executable, str(SCRIPT), '--record', str(path),
                                     '--feedback', str(root / 'feedback.json'), '--run-dir', str(root),
                                     '--remaining', str(remaining)], capture_output=True, text=True)
            return result, json.loads(result.stdout) if result.returncode == 0 else None

    def test_valid_metadata_and_recomputed_flags(self):
        result, output = self.cli(lambda r, c, f: f.update(evidence_checked=False, target_checked=False))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output['next_stage'], 'DecideOptimization')
        self.assertTrue(output['evidence_checked'])
        self.assertTrue(output['target_checked'])

    def test_invalid_inputs_never_decide(self):
        mutations = [
            lambda r, c, f: f.update(_remove_raw=True),
            lambda r, c, f: f.update(_remove_context=True),
            lambda r, c, f: r['evidence'][0].update(source='fixture'),
            lambda r, c, f: r['evidence'][0].update(kind='estimate'),
            lambda r, c, f: r['evidence'][0].pop('provenance'),
            lambda r, c, f: r['evidence'][0]['provenance'].update(scope='other'),
            lambda r, c, f: c['execution_context'].update(target='other'),
            lambda r, c, f: c['execution_context'].update(build='other'),
            lambda r, c, f: c['execution_context'].update(workload='other'),
            lambda r, c, f: c.update(parent_state='other'),
            lambda r, c, f: c.update(run_id='other'),
            lambda r, c, f: c.update(point_id='other'),
            lambda r, c, f: f.update(hypothesis_verdict='unknown'),
            lambda r, c, f: f.pop('execution_status'),
            lambda r, c, f: f.pop('explanation'),
            lambda r, c, f: f.update(record_path='raw.txt'),
            lambda r, c, f: f.update(experiment_id='other'),
            lambda r, c, f: f.update(run_id='other'),
            lambda r, c, f: f.update(point_id='other'),
            lambda r, c, f: f.update(hypothesis_id='other'),
            lambda r, c, f: r['evidence'][0]['provenance'].update(parent_state='other'),
            lambda r, c, f: r['evidence'][0]['provenance']['execution_context'].update(target='other'),
            lambda r, c, f: r['experiment'].pop('exit_code'),
            lambda r, c, f: r['experiment'].update(exit_code=True),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(case=index):
                result, output = self.cli(mutation)
                self.assertTrue(result.returncode != 0 or output['next_stage'] != 'DecideOptimization')
        result, _ = self.cli(remaining=-1)
        self.assertNotEqual(result.returncode, 0)

    def test_failed_command(self):
        result, output = self.cli(lambda r, c, f: r['experiment'].update(exit_code=1))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output['next_stage'], 'blocked_execution')

    def awaiting_cli(self, mutation=None, finalized=False):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            record = sample(root)
            record['evidence'] = []
            path = root / ('experiments/e1/record.json' if finalized else 'draft.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            f = feedback('inconclusive', 'awaiting_target_evidence')
            f.update({k: record[k] for k in ('run_id', 'point_id', 'experiment_id', 'hypothesis_id')})
            f.update(explanation='Target is unavailable.', record_path=str(path.relative_to(root)))
            if mutation:
                mutation(record, f)
            path.write_text(json.dumps(record))
            context = {k: record[k] for k in ('run_id', 'point_id', 'parent_state', 'execution_context')}
            (root / 'current_context.json').write_text(json.dumps(context))
            fp = root / 'feedback.json'
            fp.write_text(json.dumps(f))
            result = subprocess.run([sys.executable, str(SCRIPT), '--record', str(path),
                                     '--feedback', str(fp), '--run-dir', str(root), '--remaining', '2'],
                                    capture_output=True, text=True)
            return result

    def test_awaiting_draft(self):
        result = self.awaiting_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['next_stage'], 'awaiting_target_evidence')
        self.assertFalse(output['target_checked'])
        self.assertFalse(output['evidence_checked'])

    def test_awaiting_rejects_executed_or_conclusive_records(self):
        cases = [
            ('finalized location', None, True),
            ('supported', lambda r, f: f.update(hypothesis_verdict='supported'), False),
            ('exit code', lambda r, f: r['experiment'].update(exit_code=0), False),
            ('evidence', lambda r, f: r.update(evidence=[dict(
                path='raw.txt', scope='request', kind='observation', source='fixture')]), False),
        ]
        for name, mutation, finalized in cases:
            with self.subTest(case=name):
                result = self.awaiting_cli(mutation, finalized)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        result, _ = self.cli(lambda r, c, f: f.update(
            execution_status='awaiting_target_evidence', hypothesis_verdict='inconclusive'))
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_historical_reuse(self):
        def historical(r, c, f):
            r['evidence'][0].update(source='historical',
                                   reuse_explanation='Same source, target, build, input and scope verified.')
        result, output = self.cli(historical)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output['next_stage'], 'DecideOptimization')
        result, output = self.cli(lambda r, c, f: r['evidence'][0].update(source='historical'))
        self.assertEqual(output['next_stage'], 'blocked_evidence')


class VerificationRouteTests(unittest.TestCase):
    def test_candidate_routes(self):
        from experiment_feedback import route_verification
        for result, expected in [
            ({'compilation': {'ok': False}}, 'FixCode'),
            ({'functional_test': {'passed': False}}, 'FixCode'),
            ({'correctness_verdict': 'failed'}, 'FixCode'),
            ({'status': 'regression'}, 'ValidateExperiment'),
            ({'local_performance_verdict': 'passed', 'system_acceptance': 'not_run'}, 'ValidateExperiment'),
            ({'correctness_verdict': 'passed', 'system_acceptance': 'passed'}, 'Deliver'),
            ({'status': 'unverified'}, 'ValidateExperiment'),
            ({'status': 'regression', 'correctness_verdict': 'passed',
              'local_performance_verdict': 'passed', 'system_acceptance': 'failed',
              'performance': {'regression': True}}, 'ValidateExperiment'),
            ({'status': 'verified', 'compilation': {'ok': True},
              'functional_test': {'passed': True}, 'correctness_verdict': 'passed',
              'local_performance_verdict': 'not_run', 'system_acceptance': 'passed'}, 'Deliver'),
        ]:
            with self.subTest(result=result):
                self.assertEqual(route_verification(result), expected)

    def test_diagnostic_sequence(self):
        for verdict, remaining, stage in [('inconclusive', 2, 'DesignExperiment'),
                                          ('refuted', 1, 'AnalyzeHotspot'),
                                          ('supported', 0, 'DecideOptimization')]:
            self.assertEqual(choose_next(dict(execution_status='completed',
                hypothesis_verdict=verdict, evidence_checked=True, target_checked=True), remaining), stage)

    def test_rejects_conflicting_or_malformed_verification(self):
        from experiment_feedback import route_verification
        cases = [[], {'correctness_verdict': 'yes'}, {'system_acceptance': True},
                 {'local_performance_verdict': None}, {'compilation': []},
                 {'compilation': {'ok': 1}}, {'functional_test': {'passed': 'false'}},
                 {'performance': {'regression': 0}}, {'status': 'marginal'},
                 {'status': 'verified'}, {'status': 'failed', 'correctness_verdict': 'passed', 'system_acceptance': 'passed'},
                 {'status': 'unverified', 'correctness_verdict': 'passed', 'system_acceptance': 'passed'},
                 {'status': 'regression', 'system_acceptance': 'passed'},
                 {'status': 'failed', 'system_acceptance': 'passed'},
                 {'status': 'unverified', 'compilation': {'ok': False}},
                 {'status': 'regression', 'performance': {'regression': False}},
                 {'correctness_verdict': 'passed', 'compilation': {'ok': False}},
                 {'system_acceptance': 'passed', 'functional_test': {'passed': False}},
                 {'system_acceptance': 'passed', 'correctness_verdict': 'inconclusive'},
                 {'system_acceptance': 'passed', 'local_performance_verdict': 'failed'},
                 {'system_acceptance': 'passed', 'performance': {'regression': True}}]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ValueError):
                route_verification(result)

    def test_cli_modes(self):
        import contextlib
        import io
        from experiment_feedback import main
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'verify.json'
            path.write_text(json.dumps({'correctness_verdict': 'passed', 'system_acceptance': 'passed'}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['--verify-result', str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())['next_stage'], 'Deliver')
            for args in [[], ['--record', str(path)],
                         ['--verify-result', str(path), '--record', str(path)],
                         ['--verify-result', str(path), '--remaining', '0'],
                         ['--verify-result', str(path), '--feedback', str(path)],
                         ['--verify-result', str(path), '--run-dir', tmp]]:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(args)
            path.write_text(json.dumps({'status': 'failed', 'correctness_verdict': 'passed', 'system_acceptance': 'passed'}))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--verify-result', str(path)]), 1)


if __name__ == '__main__':
    unittest.main()
