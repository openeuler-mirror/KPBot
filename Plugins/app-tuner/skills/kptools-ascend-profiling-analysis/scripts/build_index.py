#!/usr/bin/env python3
"""解析 torch_npu trace_view.json → sqlite 索引.

用法: python3 build_index.py <trace_view.json> [-o out.db]

索引内容:
  events   全部 X 事件 (双键序格式, ts/dur 以 ns 整数存储)
  classes  分类区间 kind ∈ computing/communication/free (优先 CANN Overlap Analysis, 否则自建)
  steps    ProfilerStep# 边界; 缺失时按 python 主线程最外层周期容器推断
           (meta.step_source = profiler_step | anchor:<函数名> | none)
  comm_ops communication.json 侧车: 通信算子耗时/等待/带宽明细 (无该文件则跳过)
  api_stat api_detail.csv(优先)/api_statistic.csv 侧车: host/device API 聚合 (无则跳过, meta.api_source 记来源)
  meta     ui_zero(自动=全事件最早 ts, 即查看器零点; calibrate 手动覆盖后重建继承) 等;
           coord_streams = HCCL 协调流 tid 列表 (其 AI_CPU 任务属通信机制, 四分类里不算 Computing)
"""
import argparse, bisect, csv, json, os, re, sqlite3, sys, time

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

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
    data = json.load(open(path, encoding='utf-8'), parse_float=str)
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
    base = os.path.dirname(trace_path)
    for name in ('api_detail.csv', 'api_statistic.csv'):
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        rows = []
        with open(path, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                try:
                    api = r.get('API Name') or r.get('Name') or r.get('api')
                    if not api:
                        continue
                    avg = float(r.get('Avg(us)') or r.get('Avg') or 0)
                    cnt = int(float(r.get('Count') or r.get('cnt') or 0))
                    rows.append((r.get('Device_id', ''), r.get('Level', ''), api[:200],
                                 float(r.get('Time(us)', 0) or 0), cnt, avg,
                                 float(r.get('Min(us)', 0) or 0), float(r.get('Max(us)', 0) or 0)))
                except (TypeError, ValueError):
                    continue
        if rows:
            return rows, name
    return None, None


def load_step_time(trace_path):
    path = os.path.join(os.path.dirname(trace_path), 'ascend_pytorch_profiler_0.db')
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        rows = con.execute('SELECT id, startNs, endNs FROM STEP_TIME ORDER BY id').fetchall()
        con.close()
    except Exception:
        return None
    return [(int(i), int(s), int(e)) for i, s, e in rows if e > s] or None


def detect_steps(events, py_pid):
    """无 ProfilerStep#N 时按 python 主线程最外层周期容器推断 step 边界.

    候选: 主线程 python_function 中实例数>=2 且自身互不重叠的函数;
    从中位时长最大者向下找第一个"包含其他候选且不被更大候选包含"的容器
    (即最外层循环体, 如 execute_model / training_step).
    窗口语义对齐 ProfilerStep: step_i = [实例i.start, 实例i+1.start),
    末步 end = 主线程末个 python 事件 end (含步尾收尾).
    分组键去掉行号只留 file:func: 同一函数不同调用路径行号不同
    (27B 实测 step0 走 worker_base.py(345) 其余走 (340), 不归并会把 4 步裂成 3+1).
    """
    tids = {}
    for ts, dur, name, tt, pid, tid in events:
        if pid == py_pid and tt == 'python_function' and dur > 0:
            tids[tid] = tids.get(tid, 0) + 1
    if not tids:
        return [], ''
    main_tid = max(tids, key=tids.get)
    by_name, reps, last_end = {}, {}, 0
    for ts, dur, name, tt, pid, tid in events:
        if pid == py_pid and tid == main_tid and tt == 'python_function' and dur > 0:
            key = re.sub(r'\(\d+\): ', ': ', name)
            by_name.setdefault(key, []).append((ts, ts + dur))
            if dur > reps.get(key, (0, ''))[0]:
                reps[key] = (dur, name)
            if ts + dur > last_end:
                last_end = ts + dur
    cands = []
    for name, insts in by_name.items():
        if len(insts) < 2:
            continue
        insts.sort()
        if any(insts[i + 1][0] < insts[i][1] for i in range(len(insts) - 1)):
            continue
        cands.append((name, insts))
    if not cands:
        return [], ''

    for name, insts in cands:
        if 'profiler.py' in name and name.endswith(': step'):
            first_py = min(s for ivs in by_name.values() for s, _ in ivs)
            bounds = [first_py] + [s for s, _ in insts]
            windows = [(i + 1, bounds[i], bounds[i + 1]) for i in range(len(insts))]
            trail = last_end - insts[-1][1]
            if trail > 0.05 * (bounds[-1] - bounds[-2]):
                windows.append((len(insts) + 1, bounds[-1], last_end))
            else:
                windows[-1] = (windows[-1][0], windows[-1][1], max(insts[-1][1], last_end))
            return windows, reps.get(name, (0, name))[1]

    def med(insts):
        ds = sorted(e - s for s, e in insts)
        return ds[len(ds) // 2]

    cands.sort(key=lambda c: -med(c[1]))
    cands = cands[:256]
    cmap = dict(cands)
    cstarts = {n: [s for s, _ in insts] for n, insts in cmap.items()}

    def contains(a_n, b_n):
        a_s = cmap[a_n][:64]
        bs, binst = cstarts[b_n], cmap[b_n]
        hits = 0
        for s, e in a_s:
            i = bisect.bisect_left(bs, s)
            if i < len(bs) and binst[i][1] <= e:
                hits += 1
        return hits >= max(1, len(a_s) // 2)

    def contained_by(a_n, b_n):
        a_s = cmap[a_n][:64]
        bs, binst = cstarts[b_n], cmap[b_n]
        hits = 0
        for s, e in a_s:
            i = bisect.bisect_right(bs, s) - 1
            if i >= 0 and binst[i][1] >= e:
                hits += 1
        return hits >= max(1, len(a_s) // 2)

    names = [n for n, _ in cands]
    chosen = ''
    for idx, n in enumerate(names):
        if not any(contains(n, m) for m in names[idx + 1:]):
            continue
        if any(contained_by(n, m) for m in names[:idx]):
            continue
        chosen = n
        break
    if not chosen:
        chosen = names[0]
    insts = cmap[chosen]
    windows = []
    for i, (s, e) in enumerate(insts):
        we = insts[i + 1][0] if i + 1 < len(insts) else max(e, last_end)
        windows.append((i + 1, s, max(we, e)))
    return windows, reps.get(chosen, (0, chosen))[1]


def load_kernel_state(trace_path):
    """kernel_details.csv 侧车 → 内核名 OP State 归并 (static=整图回放内核, dynamic=host 逐次下发)."""
    path = os.path.join(os.path.dirname(trace_path), 'kernel_details.csv')
    if not os.path.exists(path):
        return None
    state = {}
    with open(path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            name = r.get('Name')
            op = r.get('OP State')
            if not name or op not in ('static', 'dynamic'):
                continue
            name = name[:200]
            s = state.setdefault(name, [0, 0])
            s[0 if op == 'static' else 1] += 1
    return state or None


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
    data = open(args.trace, encoding='utf-8').read()
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
    for m in RE_B.finditer(data):
        if m.group(1).startswith('ProfilerStep#') and m.group(5):
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

    step_source = 'profiler_step' if steps else 'none'
    if not steps:
        st = load_step_time(args.trace)
        if st:
            steps = st
            step_source = 'cann_step_time'
            print(f'  无 ProfilerStep#N, 使用侧车 ascend_pytorch_profiler_0.db STEP_TIME: {len(steps)} 步')
    if not steps and py_pids:
        steps, anchor = detect_steps(events, max(py_pids, key=py_pids.get))
        if steps:
            step_source = f'anchor:{anchor}'
            print(f'  无 ProfilerStep#N 且无侧车 STEP_TIME, 按最外层周期容器推断 step: {anchor} ({len(steps)} 步)')
        else:
            print('  无 ProfilerStep#N 且无可推断的周期容器, steps 为空')

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
    CREATE TABLE kernel_state(name TEXT PRIMARY KEY, state TEXT);
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    ''')
    con.executemany('INSERT INTO events VALUES(?,?,?,?,?,?)', events)
    con.executemany('INSERT INTO classes VALUES(?,?,?)', classes)
    con.executemany('INSERT INTO steps VALUES(?,?,?)', steps)
    con.executemany('INSERT INTO meta VALUES(?,?)', [
        ('trace', os.path.abspath(args.trace)), ('ui_zero', str(ui_zero)),
        ('ui_zero_source', zero_src),
        ('classification_source', src), ('n_events', str(len(events))),
        ('step_source', step_source),
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
    api_rows, api_src = load_api_stat(args.trace)
    if api_rows:
        con.executemany('INSERT INTO api_stat VALUES(?,?,?,?,?,?,?,?)', api_rows)
        print(f'  {api_src}: {len(api_rows)} 条 API 聚合')
    else:
        print('  api_detail.csv/api_statistic.csv: 未找到, 跳过')
    ks = load_kernel_state(args.trace)
    if ks:
        con.executemany('INSERT OR REPLACE INTO kernel_state VALUES(?,?)',
                        [(n, 'static' if s[0] and not s[1] else 'dynamic' if s[1] and not s[0] else 'mixed')
                         for n, s in ks.items()])
        n_static = sum(1 for v in ks.values() if v[0] and not v[1])
        print(f'  kernel_details.csv: {len(ks)} 个内核 OP State (static {n_static})')
    else:
        print('  kernel_details.csv: 未找到, 跳过')
    con.commit()
    print(f'索引完成: {out}  (耗时 {time.time()-t0:.1f}s)')


if __name__ == '__main__':
    main()
