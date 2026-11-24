"""Check comparison evidence when a cgroup probe or one inventory is missing."""
import json
import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / 'scripts'))
from pdenv import diff, fields
from make_fixture import inventory


def entry(name, payload, error=None):
    return fields.build_entry(name, {'meta': {}, 'source': name},
                              {'container': name, 'inventory': payload, 'docker': {}, 'error': error})


class CgroupEvidence(unittest.TestCase):
    def test_failed_or_unstarted_cgroup_is_not_drift(self):
        newer = inventory()
        newer['cgroup'] = {'cpu.max': '200000 100000'}
        new = entry('new', newer)
        for key, value in (('failed', {'cgroup': 'timeout'}), ('never_started', ['cgroup'])):
            with self.subTest(status=key):
                older = inventory()
                older['collection_status']['completed'].remove('cgroup')
                older['collection_status'][key] = value
                old = entry('old', older)
                rows = diff.pair_rows(old, new)
                quota = next(row for row in rows if row['name'] == 'cgroup: cpu.max')
                self.assertEqual(quota['status'], diff.NOT_COMPARABLE)
                self.assertFalse(quota['diff'])
                self.assertEqual(diff.summarize_pair(old, new, rows)['not_comparable_count'], 1)

    def test_unparseable_inventory_is_not_a_zero_diff_match(self):
        try:
            json.loads('{')
        except ValueError as error:
            old = entry('old', None, str(error))
        new = entry('new', inventory())
        pair = diff.summarize_pair(old, new, diff.pair_rows(old, new))
        self.assertFalse(pair['comparable'])
        self.assertEqual(pair['diff_count'], 0)
        self.assertIn(old['error'], pair['note'])


if __name__ == '__main__':
    unittest.main()
