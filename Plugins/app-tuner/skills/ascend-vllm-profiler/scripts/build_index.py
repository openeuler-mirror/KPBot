#!/usr/bin/env python3
"""解析 torch_npu trace_view.json → sqlite 索引.

用法: python3 build_index.py <trace_view.json> [-o out.db]

索引内容:
  events   全部 X 事件 (双键序格式, ts/dur 以 ns 整数存储)
  classes  分类区间 kind ∈ computing/communication/free (优先 CANN Overlap Analysis, 否则自建)
  steps    ProfilerStep# 边界
  comm_ops communication.json 侧车: 通信算子耗时/等待/带宽明细 (无该文件则跳过)
  api_stat api_statistic.csv 侧车: host/device API 聚合 (无该文件则跳过)
  meta     ui_zero(自动=全事件最早 ts, 即查看器零点; calibrate 手动覆盖后重建继承) 等;
           coord_streams = HCCL 协调流 tid 列表 (其 AI_CPU 任务属通信机制, 四分类里不算 Computing)
"""
import argparse, csv, json, os, re, sqlite3, sys, time

RE_A = re.compile(
    r'\{"ph": "X", "name": "([^"]+)", "pid": (\d+), "tid": (\d+), "ts": "([0-9.]+)", "dur": ([0-9.]+), "cat": "([^"]+)"')
RE_B = re.compile(
    r'\{"name": "([^"]+)", "pid": (\d+), "tid": (\d+), "ts": "([0-9.]+)"(?:, "dur": ([0-9.]+))?, "ph": "X"')
RE_TT = re.compile(r'"Task Type": "([^"]+)"')
RE_TS = re.compile(r'"ts": "([0-9.]+)"')

AI_TYPES = {'AI_CORE', 'AI_VECTOR_CORE', 'MIX_AIC', 'MIX_AIV', 'AI_CPU', 'DSA_SQE'}
OA_NAMES = {'Computing', 'Communication', 'Communication(Not Overlapped)', 'Free'}


def to_ns(s):
    if '.' in s:
        a, b = s.split('.', 1)
        return int(a) * 1000 + int((b + '000')[:3])
    return int(s) * 1000


def union(ivs):
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def complement(ivs, a, b):
    out, prev = [], a
    for s, e in ivs:
        if s > prev:
            out.append((prev, s))
        prev = max(prev, e)
    if prev < b:
        out.append((prev, b))
    return out


def load_comm_ops(trace_path):
    path = os.path.join(os.path.dirname(trace_path), 'communication.json')
    if not os.path.exists(path):
        return None
    data = json.load(open(path), parse_float=str)
    rows = []
    for step_name, grp in data.items():
        for kind in ('collective', 'p2p'):
            for name, info in (grp.get(kind) or {}).items():
                ti = info.get('Communication Time Info', {})
                ts_us = ti.get('Start Timestamp(us)')

                def num(key, default=0.0):
                    v = ti.get(key)
                    if v is None:
                        return default
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return default
                start_ns = int(str(ts_us).split('.')[0]) * 1000 if ts_us else 0
                rows.append((step_name, kind, name[:200], start_ns,
                             num('Elapse Time(ms)'), num('Transit Time(ms)'),
                             num('Wait Time(ms)'), num('Synchronization Time(ms)'),
                             num('Idle Time(ms)')))
    return rows


def load_api_stat(trace_path):
    path = os.path.join(os.path.dirname(trace_path), 'api_statistic.csv')
    if not os.path.exists(path):
        return None
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append((r['Device_id'], r['Level'], r['API Name'],
                             float(r['Time(us)']), int(r['Count']),
                             float(r['Avg(us)']), float(r['Min(us)']), float(r['Max(us)'])))
            except (KeyError, ValueError):
                continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('trace')
    ap.add_argument('-o', '--out', default=None)
    ap.add_argument('--ui-zero-ns', type=int, default=None,
                    help='UI 工具零点 (绝对ns)。不填则用最早事件 ts, 可后续用 query.py calibrate 校准')
    args = ap.parse_args()
    out = args.out or os.path.join(
        os.path.expanduser('~/.cache/ascend_prof_skill'),
        re.sub(r'[^A-Za-z0-9]', '_', os.path.abspath(args.trace)) + '.db')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    old_zero, old_src = None, None
    if os.path.exists(out):
        try:
            old = sqlite3.connect(out)
            r = old.execute("SELECT value FROM meta WHERE key='ui_zero'").fetchone()
            old_zero = int(r[0]) if r else None
            r2 = old.execute("SELECT value FROM meta WHERE key='ui_zero_source'").fetchone()
            old_src = r2[0] if r2 else None
            old.close()
        except Exception:
            pass
        os.remove(out)

    t0 = time.time()
    print(f'读取 {args.trace} ...')
    data = open(args.trace).read()
    print(f'  {len(data)/1e6:.0f}MB, {time.time()-t0:.1f}s')

    events = []
    for m in RE_A.finditer(data):
        events.append((to_ns(m.group(4)), to_ns(m.group(5)), m.group(1), m.group(6),
                       int(m.group(2)), int(m.group(3))))
    n_a = len(events)
    print(f'  格式A(ph-first): {n_a}')
    for m in RE_B.finditer(data):
        tail = data[m.end():m.end() + 300]
        tt = RE_TT.search(tail)
        events.append((to_ns(m.group(4)), to_ns(m.group(5)) if m.group(5) else 0,
                       m.group(1), tt.group(1) if tt else None,
                       int(m.group(2)), int(m.group(3))))
    print(f'  格式B(name-first): {len(events)-n_a}, 合计 {len(events)}, {time.time()-t0:.1f}s')

    steps = []
    for m in RE_A.finditer(data):
        if m.group(1).startswith('ProfilerStep#'):
            steps.append((int(m.group(1).split('#')[1]), to_ns(m.group(4)), to_ns(m.group(4)) + to_ns(m.group(5))))
    steps = sorted(set(steps))

    # 自动校准: chrome trace 查看器零点 = 全部事件(含 metadata)最早 ts,
    # 比最早 X 事件早几十 ms (8B 实测 75ms, 27B 实测几十 ms)
    auto_zero = min(to_ns(m.group(1)) for m in RE_TS.finditer(data))
    if args.ui_zero_ns is not None:
        ui_zero, src_zero, zero_src = args.ui_zero_ns, '显式指定', 'manual'
    elif old_zero is not None and old_src == 'manual':
        ui_zero, src_zero, zero_src = old_zero, '继承原db手动校准值', 'manual'
    else:
        ui_zero, src_zero, zero_src = auto_zero, '自动: 全事件最早ts(=查看器零点)', 'auto_min_ts'
    print(f'  UI 零点: {ui_zero} ns (来源: {src_zero})')

    ai_by_stream = {}
    comm = []
    py_pids, dev_pids, cann_pids, oa_pids = {}, {}, {}, {}
    for ts, dur, name, tt, pid, tid in events:
        if tt == 'python_function':
            py_pids[pid] = py_pids.get(pid, 0) + 1
        if dur:
            if tt in AI_TYPES:
                ai_by_stream.setdefault(tid, []).append((ts, ts + dur, tt))
                dev_pids[pid] = dev_pids.get(pid, 0) + 1
            elif name.startswith('hcom'):
                comm.append((ts, ts + dur))
        if name.startswith('AscendCL@'):
            cann_pids[pid] = cann_pids.get(pid, 0) + 1
        if name in OA_NAMES:
            oa_pids[pid] = oa_pids.get(pid, 0) + 1
    coord = sorted(tid for tid, ivs in ai_by_stream.items()
                   if len(ivs) >= 100 and all(t == 'AI_CPU' for _, _, t in ivs))

    oa = {'computing': [], 'communication': [], 'free': [], 'comm_not_overlapped': []}
    kind_map = {'Computing': 'computing', 'Communication': 'communication',
                'Communication(Not Overlapped)': 'comm_not_overlapped', 'Free': 'free'}
    for ts, dur, name, tt, pid, tid in events:
        if name in kind_map and dur:
            oa[kind_map[name]].append((ts, ts + dur))
    if oa['computing']:
        src = 'overlap_analysis'
        classes = [(k, s, e) for k, ivs in oa.items() for s, e in union(ivs)]
        print(f'  分类源: CANN Overlap Analysis ({sum(len(v) for v in oa.values())} 区间)')
    else:
        src = 'self_built'
        K = union([(s, e) for tid, ivs in ai_by_stream.items() if tid not in coord for s, e, _ in ivs])
        C = union(comm)
        busy = union(K + C)
        lo, hi = busy[0][0], busy[-1][1]
        classes = [('computing', s, e) for s, e in K] + [('communication', s, e) for s, e in C] + \
                  [('free', s, e) for s, e in complement(busy, lo, hi)]
        print(f'  分类源: 自建重建 (协调stream={coord})')

    con = sqlite3.connect(out)
    con.executescript('''
    CREATE TABLE events(ts_ns INTEGER, dur_ns INTEGER, name TEXT, cat TEXT, pid INTEGER, tid INTEGER);
    CREATE TABLE classes(kind TEXT, start_ns INTEGER, end_ns INTEGER);
    CREATE TABLE steps(id INTEGER PRIMARY KEY, start_ns INTEGER, end_ns INTEGER);
    CREATE TABLE comm_ops(step_name TEXT, kind TEXT, name TEXT, start_ns INTEGER,
        elapse_ms REAL, transit_ms REAL, wait_ms REAL, sync_ms REAL, idle_ms REAL);
    CREATE TABLE api_stat(device TEXT, level TEXT, api TEXT, time_us REAL,
        cnt INTEGER, avg_us REAL, min_us REAL, max_us REAL);
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    ''')
    con.executemany('INSERT INTO events VALUES(?,?,?,?,?,?)', events)
    con.executemany('INSERT INTO classes VALUES(?,?,?)', classes)
    con.executemany('INSERT INTO steps VALUES(?,?,?)', steps)
    con.executemany('INSERT INTO meta VALUES(?,?)', [
        ('trace', os.path.abspath(args.trace)), ('ui_zero', str(ui_zero)),
        ('ui_zero_source', zero_src),
        ('classification_source', src), ('n_events', str(len(events))),
        ('coord_streams', json.dumps(coord)),
        ('has_stack', '1' if any(e[3] == 'python_function' for e in events) else '0'),
        ('python_pid', str(max(py_pids, key=py_pids.get)) if py_pids else ''),
        ('device_pid', str(max(dev_pids, key=dev_pids.get)) if dev_pids else ''),
        ('cann_pid', str(max(cann_pids, key=cann_pids.get)) if cann_pids else ''),
        ('oa_pid', str(max(oa_pids, key=oa_pids.get)) if oa_pids else '')])
    con.commit()
    con.execute('CREATE INDEX idx_ev_name ON events(name)')
    con.execute('CREATE INDEX idx_ev_ts ON events(ts_ns)')
    con.execute('CREATE INDEX idx_cls_kind ON classes(kind, start_ns)')

    comm_rows = load_comm_ops(args.trace)
    if comm_rows:
        con.executemany('INSERT INTO comm_ops VALUES(?,?,?,?,?,?,?,?,?)', comm_rows)
        print(f'  communication.json: {len(comm_rows)} 个通信算子')
    else:
        print('  communication.json: 未找到, 跳过')
    api_rows = load_api_stat(args.trace)
    if api_rows:
        con.executemany('INSERT INTO api_stat VALUES(?,?,?,?,?,?,?,?)', api_rows)
        print(f'  api_statistic.csv: {len(api_rows)} 条 API 聚合')
    else:
        print('  api_statistic.csv: 未找到, 跳过')
    con.commit()
    print(f'索引完成: {out}  (耗时 {time.time()-t0:.1f}s)')


if __name__ == '__main__':
    main()
