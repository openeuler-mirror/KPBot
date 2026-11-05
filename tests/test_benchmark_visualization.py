"""Behavioral tests for generic grouping, statistics and chart artifacts."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'Plugins/code-optimizer/skills/benchmark-visualization/scripts/visualize.py'
SPEC = importlib.util.spec_from_file_location('visualize', SCRIPT)
V = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V)


class VisualizationTests(unittest.TestCase):
    def records(self):
        return [dict(machine=m, version=v, size=x, run=r, time=x * (r + 1), ipc=r / 2)
                for m in ('arm-a', 'arm-b') for v in ('v1', 'v2', 'v3', 'v4')
                for x in (1, 4, 16) for r in range(3)]

    def metrics(self):
        return {'time': dict(name='time', column='time', unit='ms', sample_level='independent_run')}

    def chart(self, kind='line'):
        return dict(id='test', type=kind, metric='time', x='size', x_type='numeric',
                    series='version', facet='machine', aggregate='median', spread='minmax', unit_id='run')

    def test_arbitrary_groups_and_facets(self):
        c = self.chart()
        points = V.prepare(self.records(), c, self.metrics())
        rows = V.summarize(points, c)
        self.assertEqual(len(rows), 24)
        self.assertTrue(all(r['n'] == 3 for r in rows))
        self.assertEqual(rows[0]['center'], 2)
        self.assertEqual((rows[0]['low'], rows[0]['high']), (1, 3))

    def test_nested_samples_are_not_independent_runs(self):
        data = self.records()
        data.append(data[0].copy())
        with self.assertRaisesRegex(ValueError, 'experimental unit'):
            V.prepare(data, self.chart(), self.metrics())

    def test_summary_aggregation_requires_explicit_choice(self):
        metrics = self.metrics()
        metrics['time']['sample_level'] = 'summary'
        with self.assertRaisesRegex(ValueError, 'aggregate_summary'):
            V.prepare(self.records(), self.chart(), metrics)

    def test_statistics_and_invalid_values(self):
        self.assertEqual(V.aggregate([10, 12, 14, 16, 1000], 'mad'), 2)
        self.assertEqual(V.aggregate([0, 10], 'p90'), 9)
        self.assertEqual(V.aggregate([-2, 0, 2], 'mean'), 0)
        for value in (True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                V.number(value)
        with self.assertRaises(ValueError):
            V.aggregate([1], 'std')

    def test_duplicate_x_needs_explicit_reduction(self):
        c = self.chart()
        c.pop('aggregate')
        c.pop('spread')
        with self.assertRaisesRegex(ValueError, 'repeated x'):
            V.summarize(V.prepare(self.records(), c, self.metrics()), c)

    def test_long_table_filter_and_unit_conversion(self):
        c = dict(id='custom', type='scatter', metric='time', x='version',
                 series=['machine', 'version'], filter={'kind': 'latency'})
        metrics = self.metrics()
        metrics['time'].update(column='value', scale=1000)
        records = [dict(machine='a', version='b', kind='latency', value=.002),
                   dict(machine='a', version='b', kind='throughput', value=400)]
        points = V.prepare(records, c, metrics)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['value'], 2)
        self.assertEqual(points[0]['series'], ('a', 'b'))

    def test_missing_matrix_cell_is_not_filled_with_zero(self):
        data = self.records()
        data = [r for r in data if not (r['version'] == 'v4' and r['size'] == 16)]
        c = self.chart('heatmap')
        summary = V.summarize(V.prepare(data, c, self.metrics()), c)
        self.assertEqual(len(summary), 22)
        self.assertFalse(any(r['series'] == ('v4',) and r['x'] == 16 for r in summary))

    def test_portable_skill_in_both_install_layouts(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            folder = Path(tmp)
            for platform in ('.claude', '.opencode'):
                installed = folder / platform / 'skills' / 'benchmark-visualization'
                shutil.copytree(SCRIPT.parents[1], installed)
                source = installed / 'assets/examples/chart-spec.json'
                spec = json.loads(source.read_text())
                spec['charts'] = [spec['charts'][0]]
                source.write_text(json.dumps(spec), encoding='utf-8')
                result = subprocess.run([sys.executable, '-B', str(installed / 'scripts/visualize.py'),
                                         '--spec', str(source), '--output-dir', str(folder / platform / 'result')],
                                        cwd=folder, capture_output=True, text=True, timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_vector_adapter_preserves_custom_fields(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            source = Path(tmp) / 'vector.json'
            source.write_text(json.dumps({'performance_field': 'cycles', 'datasets': {
                'custom': {'baseline': [{'cycles': 9}], 'candidate': [{'cycles': 8}]}}}))
            records = V.load_records(source, {'format': 'vector'})
            self.assertEqual(records[1]['cycles'], 8)
            self.assertEqual(records[1]['side'], 'candidate')

    def test_all_chart_types_and_custom_axis(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            folder = Path(tmp)
            (folder / 'data.json').write_text(json.dumps(self.records()), encoding='utf-8')
            charts = []
            for kind in ('scatter', 'line', 'point', 'bar', 'box', 'hist', 'ecdf', 'heatmap'):
                c = self.chart(kind)
                c['id'] = kind
                if kind in ('box', 'hist', 'ecdf'):
                    c.pop('aggregate')
                    c.pop('spread')
                if kind == 'line':
                    c['ylim'] = [0, 100]
                charts.append(c)
            spec = dict(input=dict(path='data.json', format='json'),
                        mapping=dict(metrics=list(self.metrics().values())), charts=charts,
                        output=dict(formats=['svg', 'png']))
            path = folder / 'spec.json'
            path.write_text(json.dumps(spec), encoding='utf-8')
            output = V.run(path, output_dir=folder / 'output')
            result = json.loads((output / 'summary.json').read_text())
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(len(list((output / 'figures').glob('*'))), 18)
            self.assertIn('100', (output / 'figures/line.svg').read_text())
            self.assertIn('v4', (output / 'figures/heatmap.svg').read_text())
            self.assertEqual(len(list((output / 'data').glob('*.csv'))), 9)
            original = (output / 'figures/line.svg').read_bytes()
            V.run(path, output_dir=output)
            self.assertEqual(original, (output / 'figures/line.svg').read_bytes())
            self.assertEqual(len(list(output.glob('*summary.json'))), 2)

    def test_six_revision_scenarios(self):
        """Simulate multi-file inputs, repetitions, labels and same-directory edits."""
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            folder = Path(tmp)
            full = 'extremely_long_test_case_name_with_machine_configuration_and_payload_size'
            for name, values in [('a.csv', [10, 15, 20]), ('b.txt', [11, 17, 24])]:
                (folder / name).write_text('case,run,time\n' + ''.join(f'{full},{i},{v}\n' for i,v in enumerate(values)))
            spec = dict(inputs=[dict(path='a.csv', format='csv', dimensions={'version':'A'}),
                                dict(path='b.txt', format='csv', dimensions={'version':'B'})],
                        mapping=dict(metrics=list(self.metrics().values())),
                        charts=[dict(id='comparison', type='bar', metric='time', x='case',
                                     series='version', aggregate='mean', unit_id='run')],
                        output=dict(formats=['svg'], run_name='v1'))
            path = folder / 'spec.json'
            path.write_text(json.dumps(spec))
            out = V.run(path, output_dir=folder/'plots')
            report = json.loads((out/'v1-summary.json').read_text())
            chart = report['charts'][0]
            self.assertEqual(chart['label_map'][full], 'A')
            self.assertEqual(chart['statistics'][0]['low'], 10)
            self.assertEqual(chart['statistics'][0]['high'], 20)
            self.assertEqual(chart['statistics'][0]['n'], 3)
            self.assertEqual(chart['selected_records'], 6)
            self.assertIn(full, (out/'v1-index.md').read_text())
            self.assertIn('user review is required', (out/'v1-index.md').read_text())
            first = (out/'figures/v1-comparison.svg').read_bytes()
            spec['output']['run_name']='v2'
            spec['charts'][0]['ylim']=[0,100]
            path.write_text(json.dumps(spec))
            V.run(path, output_dir=out)
            self.assertEqual(first, (out/'figures/v1-comparison.svg').read_bytes())
            self.assertTrue((out/'figures/v2-comparison.svg').exists())
            self.assertEqual((out/'data/v1-comparison.csv').read_bytes(), (out/'data/v2-comparison.csv').read_bytes())
            provenance=json.loads((out/'v2-provenance.json').read_text())
            self.assertEqual(len(provenance['inputs']),2)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                V.run(path, output_dir=out)

    def test_preparation_and_non_multimodal_instructions(self):
        skill = (SCRIPT.parents[1]/'SKILL.md').read_text(encoding='utf-8')
        preparation = (SCRIPT.parents[1]/'references/data-preparation.md').read_text(encoding='utf-8')
        self.assertLess(skill.index('先盘点并整理数据'), skill.index('保存配置'))
        self.assertIn('不具备该能力时跳过图像读取',skill)
        self.assertIn('不按行号假定配对',preparation)
        self.assertIn('不在 reshape 时只剩一个平均数',preparation)


if __name__ == '__main__':
    unittest.main()
