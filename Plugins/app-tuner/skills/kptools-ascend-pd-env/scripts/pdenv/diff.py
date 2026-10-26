"""Comparison logic.

An item that only one side reports counts as a difference ("仅 X 有") rather
than missing information, so single-sided settings such as VLLM_USE_V1 stay
visible under the "仅看差异" filter.

An item is "无法比较" instead when the data behind it was never collected on
one of the sides, so neither equality nor a one-sided difference can be
concluded: the whole collection failed, or the collection item that feeds that
whole category failed. Those rows are excluded from the difference counts.
"""
import itertools

from . import fields

SAME = '相同'
DIFFERENT = '不同'
ONLY = '仅%s有'
BOTH_MISSING = '均缺失'
ALL_SAME = '全部相同'
HAS_DIFF = '存在差异'
NOT_COMPARABLE = '无法比较'

DIFF_STATES = (DIFFERENT, HAS_DIFF)


def is_difference(status):
    return status in DIFF_STATES or status.startswith('仅')


def all_item_keys(entries):
    """Union of every (category, name), ordered by category then name."""
    keys = set()
    for e in entries:
        keys.update(e['items'])
    order = {c: i for i, c in enumerate(fields.CATEGORY_ORDER)}

    def sort_key(key):
        category, name = key
        return (order.get(category, 99), _name_key(category, name))

    return sorted(keys, key=sort_key)


def _name_key(category, name):
    if category == fields.CATEGORY_CORE:
        try:
            return (0, fields.CORE_PACKAGES.index(name), name)
        except ValueError:
            return (1, 0, name)
    return (0, 0, str(name))


def value_of(entry, key):
    """本环境这一项的取值，采不到时返回缺失标记或 FAILED。

    pair_status 和 matrix_rows 都只经过这里，两处判据因此始终一致。
    """
    if entry.get('error'):
        return fields.FAILED
    value = entry['items'].get(key, fields.NOT_SET)
    if fields.is_missing(value) and key[0] in (entry.get('unreliable_categories') or ()):
        return fields.FAILED
    return value


def pair_status(left, right):
    """Status for one item across two environments."""
    if fields.FAILED in (left, right):
        return NOT_COMPARABLE
    lm, rm = fields.is_missing(left), fields.is_missing(right)
    if lm and rm:
        return BOTH_MISSING
    if lm or rm:
        return 'left' if rm else 'right'   # caller turns this into 仅X有
    return SAME if left == right else DIFFERENT


def pair_rows(a, b, keys=None):
    """Rows for an A/B comparison, each with a resolved status."""
    rows = []
    for key in (keys if keys is not None else all_item_keys([a, b])):
        category, name = key
        left, right = value_of(a, key), value_of(b, key)
        status = pair_status(left, right)
        if status == 'left':
            status = ONLY % a['id']
        elif status == 'right':
            status = ONLY % b['id']
        rows.append({
            'category': category, 'name': name,
            'left': left, 'right': right, 'status': status,
            'perf': fields.is_perf_relevant(category, name),
            'diff': is_difference(status),
        })
    return rows


def matrix_rows(entries, keys=None):
    """Rows across all environments at once (the full table)."""
    rows = []
    for key in (keys if keys is not None else all_item_keys(entries)):
        category, name = key
        values = [value_of(e, key) for e in entries]
        present = [v for v in values if not fields.is_missing(v)]
        if fields.FAILED in values:
            status = NOT_COMPARABLE
        elif not present:
            status = BOTH_MISSING
        elif len(present) < len(values):
            status = HAS_DIFF
        elif len(set(values)) == 1:
            status = ALL_SAME
        else:
            status = HAS_DIFF
        rows.append({
            'category': category, 'name': name,
            'values': values, 'status': status,
            'perf': fields.is_perf_relevant(category, name),
            'diff': is_difference(status),
        })
    return rows


def choose_pairs(entries):
    """要产出哪些两两对比：4 个及以下全比，更多时拿第一个当基准。"""
    if len(entries) <= 4:
        return list(itertools.combinations(entries, 2))
    return [(entries[0], e) for e in entries[1:]]


def summarize_pair(a, b, rows):
    counts = {}
    for row in rows:
        if not row['diff']:
            continue
        counts[row['category']] = counts.get(row['category'], 0) + 1
    perf = [r for r in rows if r['diff'] and r['perf']]
    return {
        'left': a['id'], 'right': b['id'],
        'total_items': len(rows),
        'diff_count': sum(1 for r in rows if r['diff']),
        'perf_diff_count': len(perf),
        'diff_by_category': counts,
        'not_comparable_count': sum(1 for r in rows if r['status'] == NOT_COMPARABLE),
        'comparable': not (a.get('error') or b.get('error')),
        'note': _pair_note(a, b),
    }


def _pair_note(a, b):
    problems = []
    for e in (a, b):
        if e.get('error'):
            problems.append('%s 采集失败: %s' % (e['id'], e['error']))
        elif not e['packages_ok']:
            problems.append('%s 的包列表未采到，包差异不可信' % e['id'])
    return '; '.join(problems)
