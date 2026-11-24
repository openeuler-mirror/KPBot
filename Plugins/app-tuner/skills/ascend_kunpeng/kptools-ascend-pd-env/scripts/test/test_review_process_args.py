"""The D-history option excludes launch args that were not observed."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[2]
COMPARATOR = SKILL / 'scripts/compare.py'


class ProcessEvidence(unittest.TestCase):
    def test_skipped_process_is_not_a_configuration_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for folder, name in (('old', 'old-skipproc'), ('new', 'new')):
                subprocess.run([sys.executable, str(SKILL / 'scripts/test/make_fixture.py'),
                                str(root / folder), name], check=True, stdout=subprocess.DEVNULL)
            path = root / 'new/new/inventory.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            data['processes'][0]['args'] += ['--kv-transfer-config', 'fixture']
            path.write_text(json.dumps(data), encoding='utf-8')
            for folder, options in (('strict', ['--require-process-args']), ('generic', [])):
                subprocess.run([sys.executable, str(COMPARATOR), str(root / 'old'), str(root / 'new'),
                                '--out', str(root / folder)] + options,
                               check=True, stdout=subprocess.DEVNULL)
            strict = self.read(root / 'strict')
            generic = self.read(root / 'generic')
            self.assertTrue(strict['comparisons'][0]['comparable'])
            self.assertNotIn('启动参数', strict['comparisons'][0]['diff_by_category'])
            self.assertNotEqual(generic['comparisons'][0]['diff_by_category']['启动参数'], 0)
            rows = [r for r in strict['items'] if r['name'] == '--kv-transfer-config']
            self.assertEqual([r['status'] for r in rows], ['无法比较'])

    @staticmethod
    def read(path):
        return json.loads((path / '对比结果.json').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
