"""Exercise mapped folders and paths that escape a selected object."""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / 'scripts'))
from pdenv import bundle, fields


class BundlePaths(unittest.TestCase):
    def test_legacy_mapping_and_package_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            src = SKILL / 'subskills/analyze-environment/evals/fixtures/legacy'
            data = bundle.load_bundle('old-pd', src, root / 'unpack')
            p, d = [fields.build_entry(c['container'], data, c) for c in data['containers']]
            self.assertEqual(p['items'][fields.CATEGORY_ARGS, '--max-num-seqs'], '256')
            self.assertEqual(d['items'][fields.CATEGORY_ARGS, '--max-num-seqs'], '64')
            self.assertEqual(p['items'][fields.CATEGORY_CORE, 'vllm'], '0.9.1')
            self.assertEqual(p['items'][fields.CATEGORY_CORE, 'torch-npu'], fields.NOT_REPORTED)

    def test_reject_escaping_mapping_or_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            src = SKILL / 'subskills/analyze-environment/evals/fixtures/legacy'
            with (src / 'collection_meta.json').open(encoding='utf-8') as stream:
                meta = json.load(stream)
            for kind in ('mapping', 'folder_link', 'file_link'):
                with self.subTest(kind=kind):
                    dest = root / kind
                    shutil.copytree(str(src), str(dest))
                    record = dict(meta, folders=dict(meta['folders']))
                    folder = dest / 'archive-01'
                    if kind == 'mapping':
                        record['folders']['stream-p'] = '../outside'
                    elif kind == 'folder_link':
                        shutil.rmtree(str(folder))
                        folder.symlink_to(src / 'archive-01', target_is_directory=True)
                    else:
                        inv = folder / 'inventory.json'
                        inv.unlink()
                        inv.symlink_to(dest / 'archive-02/inventory.json')
                    (dest / 'collection_meta.json').write_text(json.dumps(record), encoding='utf-8')
                    data = bundle.load_bundle('bad', dest, root / 'unpack')
                    p, d = data['containers']
                    self.assertIsNone(p['inventory'])
                    self.assertTrue(p['error'])
                    self.assertIsNotNone(d['inventory'])
                    self.assertIsNone(d['error'])


if __name__ == '__main__':
    unittest.main()
