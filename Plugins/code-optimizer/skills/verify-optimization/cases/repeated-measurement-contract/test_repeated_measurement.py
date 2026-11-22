import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/summarize_repeats.py'
spec = importlib.util.spec_from_file_location('repeats', SCRIPT)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class RepeatedMeasurementTests(unittest.TestCase):
    def test_known_statistics(self):
        s = M.describe([1, 2, 3])
        self.assertEqual((s['mean'], s['sample_sd'], s['median'], s['mad']), (2, 1, 2, 1))
        self.assertEqual((s['min'], s['max'], s['cv']), (1, 3, .5))

    def test_single_observation_has_unknown_variability(self):
        s = M.describe([12])
        self.assertEqual(s['n'], 1)
        for key in ('sample_sd', 'mad', 'cv'):
            self.assertIsNone(s[key])
        self.assertFalse(s['variability_available'])

    def test_zero_negative_and_constant_values(self):
        self.assertIsNone(M.describe([-1, 1])['cv'])
        self.assertEqual(M.describe([-1, -3])['mean'], -2)
        self.assertEqual(M.describe([4, 4])['sample_sd'], 0)

    def test_nonfinite_and_empty_rejected(self):
        for values in ([], [True], [float('nan')], [float('inf')]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                M.describe(values)

    def test_outlier_is_retained(self):
        s = M.describe([10, 10, 100])
        self.assertEqual((s['n'], s['mean'], s['max']), (3, 40, 100))

    def test_multiple_files_dimensions_metrics_and_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a.csv', Path(tmp)/'b.tsv'
            a.write_text('group,run_id,metric,unit,value,threads\nA,1,time,ms,10,1\nA,2,time,ms,12,1\nB,1,time,ms,9,1\nC,1,time,ms,8,1\n', encoding='utf-8-sig')
            b.write_text('group\trun_id\tmetric\tunit\tvalue\tthreads\nA\t1\tipc\tratio\t2\t1\nA\t1\ttime\tms\t7\t4\n', encoding='utf-8')
            result = M.summarize([a, b], ['threads'])
            self.assertEqual(len(result['groups']), 5)
            self.assertEqual(result['groups'][0]['statistics']['mean'], 11)
            self.assertEqual(len(result['sources']), 2)
            self.assertEqual(result['groups'][0]['observations'][1]['source_record'], 2)

    def test_duplicate_runs_and_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'a.json'
            row = dict(group='A', run_id='1', metric='time', unit='ms', value=10)
            p.write_text(json.dumps([row, row]), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate independent'):
                M.summarize([p])
            p.write_text(json.dumps([row]), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate input'):
                M.summarize([p, p])

    def test_invalid_records_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'a.json'
            for value in (None, True, 'NaN', ''):
                p.write_text(json.dumps([dict(group='A', run_id=1, metric='m', unit='ms', value=value)]), encoding='utf-8')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    M.summarize([p])

    def test_dimension_errors_rejected(self):
        for dims in (['value'], ['threads', 'threads'], ['statistics'], ['observations']):
            with self.assertRaises(ValueError):
                M.summarize([], dims)

    def test_malformed_csv_cli_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, out = Path(tmp)/'bad.csv', Path(tmp)/'out.json'
            header = 'group,run_id,metric,unit,value\n'
            for body in ('A,1,time,ms,2,extra\n', 'A,1,time,ms\n', 'A,1,time,ms,"2\n'):
                p.write_text(header + body, encoding='utf-8')
                result = subprocess.run([sys.executable, '-B', '-S', str(SCRIPT), str(p), '--output', str(out)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(out.exists())

    def test_installed_script_from_unrelated_working_directory(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / 'input.json'
            p.write_text(json.dumps([dict(group='A', run_id=i, metric='time', unit='ms', value=v) for i, v in enumerate([1, 2, 3])]), encoding='utf-8')
            for layout in ('.claude', '.opencode'):
                installed = root / layout / 'skills/verify-optimization/scripts/summarize_repeats.py'
                installed.parent.mkdir(parents=True)
                shutil.copyfile(SCRIPT, installed)
                output = root / (layout + '.json')
                result = subprocess.run([sys.executable, '-B', '-S', str(installed), str(p), '--output', str(output)], cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['groups'][0]['statistics']['sample_sd'], 1)

    def test_cli_json_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, out = Path(tmp)/'a.json', Path(tmp)/'out.json'
            p.write_text(json.dumps([dict(group='A', run_id=1, metric='m', unit='ms', value=2)]), encoding='utf-8')
            cmd = [sys.executable, '-B', '-S', str(SCRIPT), str(p), '--output', str(out)]
            first = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            before = out.read_bytes()
            self.assertEqual(json.loads(before)['statistic_scope'], 'descriptive_only')
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
            self.assertEqual(out.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
