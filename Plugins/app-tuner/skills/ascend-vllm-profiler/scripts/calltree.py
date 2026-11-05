#!/usr/bin/env python3
"""调用树重建: 按校准规则折叠机械层, 输出层级树.

用法:
  calltree.py <db> <UI起ms> <UI止ms> [--tid 176] [--cat python_function|cpu_op] [--min-dur-us 0] [--out file]
折叠规则: module._call_impl / torch._ops.__call__ / <built-in method / _dispatcher
         / decorate_context / profiler wrapper
无栈数据: --cat cpu_op 重建算子层树 (折叠规则对 cpu_op 名天然不生效)
"""
import sqlite3, sys

COLLAPSE_SUBSTR = (
    ': _call_impl',
    'torch/_ops.py',
    '<built-in method',
    '_record_function_enter_new',
    '_record_function_exit',
    'profiler_interface.py',
    ': decorate_context',
    '_dispatcher',
    'logging/__init__.py',
    'logger.py',
    '): cast',
    ': req_ids',
    ': num_reqs',
    'autograd/profiler.py',
    '_has_torch_function',
    'torch/_tensor.py',
    '_get_device_index',
)


def collapsed(name):
    return any(p in name for p in COLLAPSE_SUBSTR)


def short(name):
    n = name.replace('/vllm-workspace/vllm-ascend/', 'va/').replace('/vllm-workspace/vllm/', 'vllm/')
    parts = n.split('/')
    return '/'.join(parts[-2:]) if len(parts) > 2 else n


def main():
    db_path, a_ms, b_ms = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    tid, min_us, out, cat = None, 0.0, None, 'python_function'
    argv = sys.argv[4:]
    i = 0
    while i < len(argv):
        if argv[i] == '--tid': tid = int(argv[i + 1]); i += 2
        elif argv[i] == '--cat': cat = argv[i + 1]; i += 2
        elif argv[i] == '--min-dur-us': min_us = float(argv[i + 1]); i += 2
        elif argv[i] == '--out': out = argv[i + 1]; i += 2
        else: i += 1
    db = sqlite3.connect(db_path)
    uiz = int(db.execute("SELECT value FROM meta WHERE key='ui_zero'").fetchone()[0])
    m = dict(db.execute("SELECT key, value FROM meta"))
    py_pid = int(m.get('python_pid') or 176)
    if tid is None:
        r = db.execute("SELECT tid FROM events WHERE pid=? AND cat=? "
                       'GROUP BY tid ORDER BY COUNT(*) DESC LIMIT 1', (py_pid, cat)).fetchone()
        tid = r[0] if r else 176
    a, b = uiz + int(a_ms * 1e6), uiz + int(b_ms * 1e6)
    evs = [(ts, ts + d, n) for ts, d, n in db.execute(
        'SELECT ts_ns, dur_ns, name FROM events WHERE pid=? AND tid=? AND cat=? '
        'AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=? ORDER BY ts_ns', (py_pid, tid, cat, a, b))]
    roots, stack = [], []
    for s, e, n in evs:
        node = [n, s, e, []]
        while stack and stack[-1][2] <= s:
            stack.pop()
        (stack[-1][3] if stack else roots).append(node)
        stack.append(node)
    lines = []

    def emit(node, depth):
        n, s, e, ch = node
        if collapsed(n):
            for c in ch:
                emit(c, depth)
            return
        dur = e - s
        if dur / 1e3 >= min_us:
            lines.append(f"{'  ' * depth}{(s - uiz) / 1e6:9.3f}ms {dur / 1e6:8.3f}ms  {short(n)}")
            d2 = depth + 1
        else:
            d2 = depth
        for c in ch:
            emit(c, d2)

    for r in roots:
        emit(r, 0)
    text = '\n'.join(lines)
    if out:
        open(out, 'w').write(text + '\n')
        print(f'共 {len(lines)} 行 → {out}')
    else:
        print(text)


if __name__ == '__main__':
    main()
