#!/usr/bin/env python3
"""基于索引的查询工具 (函数锚定 / 任意窗口 / 调用链).

用法:
  query.py <db> find   <子串> [--cat X] [--pid N] [--limit 20]      # 候选函数聚合
  query.py <db> events <子串> [--cat X] [--pid N] [--limit 30]      # 实例列表
  query.py <db> span   <子串> <UI开始ms> [--tol_ms 5]               # 定位实例并给三分类占比
  query.py <db> window <UI起ms> <UI止ms>                            # 任意窗口占比
  query.py <db> chain  <子串> <UI开始ms> [--tol_ms 5] [--limit 25]  # 时间包含该实例的外层调用链
  query.py <db> report [--step N]                                  # 多 step 汇总 + host bound 判定

时间基准: UI ms = 相对 trace 零点 (meta.ui_zero, 即工具显示时间)
"""
import argparse, json, sqlite3, sys


def ui(db):
    r = db.execute("SELECT value FROM meta WHERE key='ui_zero'").fetchone()
    return int(r[0])


def pids(db):
    m = dict(db.execute("SELECT key, value FROM meta"))
    return {'py': int(m.get('python_pid') or 176),
            'dev': int(m.get('device_pid') or 180672),
            'cann': int(m.get('cann_pid') or 180479),
            'oa': int(m.get('oa_pid') or 181216)}


def fmt(ns, uiz):
    return f'UI {(ns - uiz) / 1e6:.3f}ms (abs {ns}ns)'


def resolve(db, sub, ui_ms, tol_ms):
    uiz = ui(db)
    target = uiz + int(round(ui_ms * 1e6))
    tol = int(round(tol_ms * 1e6))
    rows = db.execute(
        'SELECT ts_ns, dur_ns, name, cat, pid, tid FROM events '
        'WHERE name LIKE ? AND dur_ns>0 AND ABS(ts_ns - ?) < ? ORDER BY ABS(ts_ns - ?) LIMIT 5',
        (f'%{sub}%', target, tol, target)).fetchall()
    exact = [r for r in rows if r[2] == sub]
    return (exact or rows), uiz


def _union_covered(db, a, b):
    """窗口内 computing ∪ communication 覆盖时长(ns)."""
    ivs = sorted((max(t, a), min(e, b)) for t, e in db.execute(
        "SELECT start_ns, end_ns FROM classes WHERE kind IN ('computing','communication') "
        'AND end_ns>? AND start_ns<?', (a, b)))
    cov, cur = 0, None
    for s, e in ivs:
        if e <= s:
            continue
        if cur is None:
            cur = [s, e]
        elif s <= cur[1]:
            cur[1] = max(cur[1], e)
        else:
            cov += cur[1] - cur[0]
            cur = [s, e]
    if cur:
        cov += cur[1] - cur[0]
    return cov


def pct_line(db, classes, a, b):
    s = b - a
    tot = {}
    for kind in ('computing', 'communication', 'comm_not_overlapped'):
        rows = db.execute(
            'SELECT start_ns, end_ns FROM classes WHERE kind=? AND end_ns>? AND start_ns<?',
            (kind, a, b)).fetchall()
        tot[kind] = sum(min(e, b) - max(t, a) for t, e in rows)
    tot['free'] = s - _union_covered(db, a, b)
    nfr = db.execute(
        "SELECT COUNT(*) FROM classes WHERE kind='free' AND end_ns>? AND start_ns<?",
        (a, b)).fetchone()[0]
    parts = ' | '.join(f"{k} {tot[k]/s*100:6.2f}% ({tot[k]/1e6:8.3f}ms)"
                       for k in ('computing', 'communication', 'comm_not_overlapped', 'free'))
    return f'  {parts} | OA free区间数{nfr} (Free=窗口−计算∪通信, 含OA未覆盖间隙)', tot


def top_free(db, a, b, n=5):
    rows = db.execute(
        "SELECT start_ns, end_ns FROM classes WHERE kind='free' AND end_ns>? AND start_ns<? "
        'ORDER BY MIN(end_ns,?)-MAX(start_ns,?) DESC LIMIT ?',
        (a, b, b, a, n)).fetchall()
    return [(max(t, a), min(e, b)) for t, e in rows]


def cmd_span(db, args):
    if args.in_step is not None and args.ui_ms is None:
        step = db.execute('SELECT start_ns, end_ns FROM steps WHERE id=?', (args.in_step,)).fetchone()
        if not step:
            print(f'无 step{args.in_step}'); return 2
        rows = db.execute(
            'SELECT ts_ns, dur_ns, name, cat, pid, tid FROM events '
            'WHERE name LIKE ? AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=? ORDER BY ts_ns',
            (f'%{args.sub}%', step[0], step[1])).fetchall()
        print(f'step{args.in_step} 内 "{args.sub}" 共 {len(rows)} 个实例:')
        stats = []
        for i, (ts, dur, name, cat, pid, tid) in enumerate(rows, 1):
            line, tot = pct_line(db, None, ts, ts + dur)
            print(f'#{i:3} dur={dur/1e6:7.3f}ms @ {fmt(ts, ui(db))}')
            print(line)
            s = dur
            stats.append(tuple(tot[k] / s * 100 for k in ('computing', 'communication', 'comm_not_overlapped', 'free')))
        if stats:
            import statistics as st
            names = ('computing', 'communication', 'commNO', 'free')
            print('汇总(min~avg~max %):')
            for j, k in enumerate(names):
                vals = [x[j] for x in stats]
                print(f'  {k:14} {min(vals):6.2f} ~ {st.mean(vals):6.2f} ~ {max(vals):6.2f}')
        return 0
    rows, uiz = resolve(db, args.sub, args.ui_ms, args.tol_ms)
    if not rows:
        print('未找到实例, 放宽 --tol_ms 或检查函数名'); return 2
    ts, dur, name, cat, pid, tid = rows[0]
    if len(rows) > 1 and rows[0][2] != rows[1][2]:
        print('警告: 多个候选, 已取时间最近者; 候选:')
        for r in rows:
            print(f'   {fmt(r[0], uiz)} dur={r[1]/1e6:.3f}ms [{r[3]}] {r[2][:70]}')
    a, b = ts, ts + dur
    print(f'函数: {name}')
    print(f'时段: {fmt(a, uiz)} ~ {fmt(b, uiz)}  dur={dur/1e6:.3f}ms  [{cat}] pid={pid} tid={tid}')
    line, tot = pct_line(db, None, a, b)
    print(line)
    frees = top_free(db, a, b)
    if frees:
        print('  窗口内 Top free 区间:')
        for s, e in frees:
            print(f'    {fmt(s, uiz)} dur={(e-s)/1e6:.3f}ms')


def cmd_window(db, args):
    uiz = ui(db)
    a = uiz + int(round(args.start_ms * 1e6))
    b = uiz + int(round(args.end_ms * 1e6))
    if a >= b:
        print('窗口起止非法'); return 2
    print(f'窗口: {fmt(a, uiz)} ~ {fmt(b, uiz)}  dur={(b-a)/1e6:.3f}ms')
    line, tot = pct_line(db, None, a, b)
    print(line)
    frees = top_free(db, a, b)
    if frees:
        print('  Top free 区间:')
        for s, e in frees:
            print(f'    {fmt(s, uiz)} dur={(e-s)/1e6:.3f}ms')


def cmd_find(db, args):
    uiz = ui(db)
    q = f'%{args.sub}%'
    conds, params = ['name LIKE ?'], [q]
    if args.cat: conds.append('cat LIKE ?'); params.append(f'%{args.cat}%')
    if args.pid is not None: conds.append('pid=?'); params.append(args.pid)
    rows = db.execute(
        f"SELECT name, cat, pid, COUNT(*), SUM(dur_ns), MIN(ts_ns), MAX(ts_ns) FROM events "
        f"WHERE dur_ns>0 AND {' AND '.join(conds)} GROUP BY name, cat, pid "
        f'ORDER BY SUM(dur_ns) DESC LIMIT {args.limit}', params).fetchall()
    print(f'候选 {len(rows)} 组 (按总时长排序):')
    for name, cat, pid, n, tot, lo, hi in rows:
        print(f'  [{cat}] pid={pid} n={n} 总dur={tot/1e6:.2f}ms  {fmt(lo, uiz)} ~ 最新{((hi - uiz) / 1e6):.3f}ms')
        print(f'      {name[:90]}')


def cmd_events(db, args):
    uiz = ui(db)
    q = f'%{args.sub}%'
    conds, params = ['name LIKE ?', 'dur_ns>0'], [q]
    if args.cat: conds.append('cat LIKE ?'); params.append(f'%{args.cat}%')
    if args.pid is not None: conds.append('pid=?'); params.append(args.pid)
    rows = db.execute(
        f"SELECT ts_ns, dur_ns, name, cat, pid, tid FROM events WHERE {' AND '.join(conds)} "
        f'ORDER BY ts_ns LIMIT {args.limit}', params).fetchall()
    print(f'实例 {len(rows)} 条 (按时间排序):')
    for ts, dur, name, cat, pid, tid in rows:
        print(f'  {fmt(ts, uiz)} dur={dur/1e6:9.3f}ms [{cat}] pid={pid} tid={tid}  {name[:70]}')


def cmd_chain(db, args):
    rows, uiz = resolve(db, args.sub, args.ui_ms, args.tol_ms)
    if not rows:
        print('未找到实例'); return 2
    ts, dur, name, cat, pid, tid = rows[0]
    a, b = ts, ts + dur
    print(f'目标: {name}  [{cat}] pid={pid} tid={tid}  {fmt(a, uiz)} dur={dur/1e6:.3f}ms')
    conts = db.execute(
        'SELECT ts_ns, dur_ns, name, cat, pid, tid FROM events '
        'WHERE pid=? AND tid=? AND dur_ns>? AND ts_ns<=? AND ts_ns+dur_ns>=? AND ts_ns< ? '
        'ORDER BY ts_ns LIMIT ?', (pid, tid, dur, a + 2000, b - 2000, ts, args.limit)).fetchall()
    print(f'同线程调用链 (pid={pid} tid={tid}, 共{len(conts)}):')
    for cts, cdur, cname, ccat, cpid, ctid in conts:
        print(f'  {fmt(cts, uiz)} dur={cdur/1e6:10.3f}ms [{ccat}]  {cname[:78]}')
    P = pids(db)
    cann = db.execute(
        'SELECT ts_ns, dur_ns, name, cat, pid, tid FROM events '
        'WHERE pid=? AND dur_ns>0 AND ts_ns<=? AND ts_ns+dur_ns>=? '
        'ORDER BY dur_ns DESC LIMIT 5', (P['cann'], b, a)).fetchall()
    if cann:
        print(f"跨层 CANN 实现 (pid={P['cann']}, 时间覆盖目标):")
        for cts, cdur, cname, ccat, cpid, ctid in cann:
            print(f'  {fmt(cts, uiz)} dur={cdur/1e6:10.3f}ms  {cname[:78]}')


def cls_stats(db, a, b):
    s = b - a
    tot = {}
    for kind in ('computing', 'communication', 'comm_not_overlapped'):
        rows = db.execute(
            'SELECT start_ns, end_ns FROM classes WHERE kind=? AND end_ns>? AND start_ns<?',
            (kind, a, b)).fetchall()
        tot[kind] = sum(min(e, b) - max(t, a) for t, e in rows)
    tot['free'] = s - _union_covered(db, a, b)
    return tot, s


def cmd_report(db, args):
    uiz = ui(db)
    steps = db.execute('SELECT id, start_ns, end_ns FROM steps ORDER BY id').fetchall()
    if args.step is not None:
        steps = [s for s in steps if s[0] == args.step]
        if not steps:
            print(f'无 step{args.step}'); return 2
    print(f'=== report: {len(steps)} steps ===')
    P = pids(db)
    T = dict(dur=0.0, p1=0.0, p2=0.0, p3=0.0, p2c=0.0, p2f=0.0, p3f=0.0, sync=0.0,
             nw=0.0, ew=0.0, nsqe=0.0)
    for sid, s0, s1 in steps:
        # 锚点链: _model_forward(整图回放步也存在) → 模型级 forward → _execute_model_orig
        cand = db.execute(
            f"SELECT ts_ns, dur_ns, name FROM events WHERE pid={P['py']} AND cat='python_function' "
            "AND name LIKE '%_model_forward%' AND dur_ns>100000 AND ts_ns>=? AND ts_ns<=? "
            'ORDER BY dur_ns DESC LIMIT 1', (s0, s1)).fetchone()
        if cand is None:
            cand = db.execute(
                f"SELECT ts_ns, dur_ns, name FROM events WHERE pid={P['py']} AND cat='python_function' "
                "AND name LIKE '%: forward' AND dur_ns>500000 AND ts_ns>=? AND ts_ns<=? "
                "AND name NOT LIKE '%sampler%' AND name NOT LIKE '%mtp%' "
                "AND name NOT LIKE '%<string>%' AND name NOT LIKE '%aot_autograd%' "
                "AND name NOT LIKE '%/layers/%' AND name NOT LIKE '%attention/%' "
                "AND name NOT LIKE '%/ops/%' AND name NOT LIKE '%sample/%' "
                'ORDER BY dur_ns DESC LIMIT 1', (s0, s1)).fetchone()
        if cand:
            w1 = (s0, cand[0]); w2 = (cand[0], cand[0] + cand[1]); w3 = (cand[0] + cand[1], s1)
            fname = cand[2].split('/')[-1]
        else:
            ex = db.execute(
                f"SELECT ts_ns, dur_ns, name FROM events WHERE pid={P['py']} AND cat='python_function' "
                "AND name LIKE '%_execute_model_orig%' AND dur_ns>0 AND ts_ns>=? AND ts_ns<=? "
                'ORDER BY dur_ns DESC LIMIT 1', (s0, s1)).fetchone()
            if ex:
                w1 = (s0, ex[0]); w2 = (ex[0], ex[0] + ex[1]); w3 = (ex[0] + ex[1], s1)
                fname = '(无模型级forward,退化为execute_model窗口)'
            else:
                w1 = (s0, s1); w2 = None; w3 = None; fname = '(未找到forward锚点)'
        t1, s1dur = cls_stats(db, *w1)
        sync = db.execute(
            f"SELECT dur_ns FROM events WHERE pid={P['py']} AND cat='python_function' "
            "AND name LIKE '%npu/utils.py(62): synchronize' AND dur_ns>0 "
            'AND ts_ns+dur_ns>? AND ts_ns<? ORDER BY dur_ns DESC LIMIT 1', (s0, s1)).fetchone()
        if sync is None:
            sync = db.execute(
                f"SELECT dur_ns FROM events WHERE pid={P['cann']} "
                "AND name LIKE '%aclrtSynchronizeDevice%' AND dur_ns>0 "
                'AND ts_ns+dur_ns>? AND ts_ns<? ORDER BY dur_ns DESC LIMIT 1', (s0, s1)).fetchone()
        sync_ms = sync[0] / 1e6 if sync else 0.0
        waits = dict(db.execute(
            f"SELECT name, SUM(MIN(ts_ns+dur_ns,?)-MAX(ts_ns,?)) FROM events WHERE pid={P['dev']} "
            "AND (name LIKE '%NOTIFY_WAIT%' OR name LIKE '%EVENT_WAIT%') AND ts_ns+dur_ns>? AND ts_ns<? "
            'GROUP BY name', (s1, s0, s0, s1)).fetchall())
        nw = waits.get('NOTIFY_WAIT', 0) / 1e6
        ew = waits.get('EVENT_WAIT', 0) / 1e6
        nsqe = waits.get('NOTIFY_WAIT_SQE', 0) / 1e6
        p1f = t1['free'] / s1dur * 100
        prep = db.execute(
            "SELECT end_ns-start_ns FROM classes WHERE kind='free' AND start_ns>=? AND start_ns<? "
            'ORDER BY start_ns LIMIT 1', (s0, s1)).fetchone()
        prep_ms = prep[0] / 1e6 if prep else 0.0
        if w2 and w3:
            t2, sdur2 = cls_stats(db, *w2)
            t3, sdur3 = cls_stats(db, *w3)
            p2c = t2['computing'] / sdur2 * 100
            p2f = t2['free'] / sdur2 * 100
            p3f = t3['free'] / sdur3 * 100
            mode = ('整图回放' if db.execute(
                f"SELECT 1 FROM events WHERE pid={P['py']} AND cat='python_function' "
                "AND name LIKE '%: replay' AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=? LIMIT 1",
                w2).fetchone() else ('FX eager' if db.execute(
                    "SELECT 1 FROM events WHERE name LIKE '%fx_run_eagerly%' AND dur_ns>0 "
                    'AND ts_ns>=? AND ts_ns+dur_ns<=? LIMIT 1', w2).fetchone() else 'eager'))
            if p3f >= 20 and sync_ms < 0.5:
                v = 'host bound(设备提前空闲)'
            elif sync_ms >= 2:
                v = '设备积压(步尾真排水)'
            else:
                v = '设备基本跟上'
            if mode == '整图回放':
                v += '（整图回放步: host 提前完成, 设备在 P3 排空）'
            elif p2f >= 30:
                v += ' + forward供数不足'
            print(f'step{sid} dur={(s1-s0)/1e6:.3f}ms')
            print(f'  P1 {(w1[1]-w1[0])/1e6:.3f}ms (free {p1f:.1f}%, Preparing {prep_ms:.3f}ms)')
            print(f'  forward {(w2[1]-w2[0])/1e6:.3f}ms [{fname[:50]}] ({mode}) (comp {p2c:.1f}%/free {p2f:.1f}%)')
            print(f'  P3 {(w3[1]-w3[0])/1e6:.3f}ms (free {p3f:.1f}%)  end_sync={sync_ms:.3f}ms')
            print(f'  设备同步等待(多流并行合计,可超墙钟): NOTIFY_WAIT {nw:.3f}ms | EVENT_WAIT {ew:.3f}ms | NOTIFY_WAIT_SQE {nsqe:.3f}ms')
            print(f'  判定: {v}')
            T['dur'] += (s1-s0)/1e6; T['p1'] += (w1[1]-w1[0])/1e6; T['p2'] += (w2[1]-w2[0])/1e6
            T['p3'] += (w3[1]-w3[0])/1e6; T['p2c'] += t2['computing']/1e6; T['p2f'] += t2['free']/1e6
            T['p3f'] += t3['free']/1e6; T['sync'] += sync_ms; T['nw'] += nw; T['ew'] += ew; T['nsqe'] += nsqe
        else:
            print(f'step{sid} dur={(s1-s0)/1e6:.3f}ms  P1-only {s1dur/1e6:.3f}ms (free {p1f:.1f}%)  sync={sync_ms:.3f}ms')
            T['dur'] += (s1-s0)/1e6; T['p1'] += s1dur/1e6; T['sync'] += sync_ms
            T['nw'] += nw; T['ew'] += ew; T['nsqe'] += nsqe
    print(f'--- 合计 ---')
    print(f"dur={T['dur']:.3f}ms  P1={T['p1']:.3f}ms  forward={T['p2']:.3f}ms (comp {T['p2c']/T['p2']*100 if T['p2'] else 0:.1f}%/free {T['p2f']/T['p2']*100 if T['p2'] else 0:.1f}%)"
          f"  P3={T['p3']:.3f}ms (free {T['p3f']/T['p3']*100 if T['p3'] else 0:.1f}%)  end_sync合计={T['sync']:.3f}ms")
    print(f"设备同步等待合计: NOTIFY_WAIT {T['nw']:.3f}ms | EVENT_WAIT {T['ew']:.3f}ms | NOTIFY_WAIT_SQE {T['nsqe']:.3f}ms")


def cmd_comm(db, args):
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='comm_ops'").fetchone():
        print('索引中无 comm_ops (建索引时未找到 communication.json)'); return 2
    conds, params = ['1=1'], []
    if args.step is not None:
        conds.append('step_name=?'); params.append(f'step{args.step}')
    if args.sub:
        conds.append('name LIKE ?'); params.append(f'%{args.sub}%')
    rows = db.execute(
        f"SELECT step_name, kind, name, start_ns, elapse_ms, transit_ms, wait_ms, sync_ms, idle_ms "
        f"FROM comm_ops WHERE {' AND '.join(conds)} ORDER BY start_ns", params).fetchall()
    if not rows:
        print('无匹配通信算子'); return 2
    uiz = ui(db)
    from collections import defaultdict
    agg = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    total_rows = {}
    for step_name, kind, name, start_ns, el, tr, wt, sy, idle in rows:
        if name == 'Total Op Info':
            total_rows[step_name] = (el, tr, wt, sy)
            continue
        a = agg[step_name]
        a[0] += 1; a[1] += el; a[2] += wt; a[3] += tr
    print(f'=== 通信算子 (明细 {len(rows) - len(total_rows)}, 含 Total 行 {len(total_rows)}) ===')
    for step_name in sorted(agg, key=lambda s: int(s[4:]) if s[4:].isdigit() else 0):
        n, el, wt, tr = agg[step_name]
        line = f'{step_name}: n={n}  elapse合计={el:.3f}ms  wait合计={wt:.3f}ms  transit合计={tr:.3f}ms'
        if step_name in total_rows:
            el2, tr2, wt2, sy2 = total_rows[step_name]
            line += f'  | CANN Total行: elapse={el2:.3f}ms wait={wt2:.3f}ms sync={sy2:.3f}ms'
        print(line)
    rows = [r for r in rows if r[2] != 'Total Op Info']
    rows.sort(key=lambda r: -r[6])
    print(f'--- Top {min(args.limit, len(rows))} by wait ---')
    for step_name, kind, name, start_ns, el, tr, wt, sy, idle in rows[:args.limit]:
        print(f'  {step_name} UI {(start_ns - uiz) / 1e6:9.3f}ms  elapse={el:7.3f}ms wait={wt:7.3f}ms sync={sy:7.3f}ms  {name[:60]}')


def cmd_api(db, args):
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='api_stat'").fetchone():
        print('索引中无 api_stat (建索引时未找到 api_statistic.csv)'); return 2
    conds, params = ['1=1'], []
    if args.sub:
        conds.append('api LIKE ?'); params.append(f'%{args.sub}%')
    if args.level:
        conds.append('level=?'); params.append(args.level)
    rows = db.execute(
        f"SELECT device, level, api, time_us, cnt, avg_us, min_us, max_us FROM api_stat "
        f"WHERE {' AND '.join(conds)} ORDER BY time_us DESC LIMIT {args.limit}",
        params).fetchall()
    if not rows:
        print('无匹配 API'); return 2
    print(f'=== api_statistic Top {len(rows)} (按总耗时) ===')
    for device, level, api, t, cnt, avg, lo, hi in rows:
        print(f'  [{device}/{level}] {api[:60]:62} 总={t:10.2f}us n={cnt:<6} avg={avg:8.2f} max={hi:9.2f}us')


def cmd_calibrate(db, args):
    uiz = ui(db)
    target = args.raw_ns if args.raw_ns is not None else uiz + int(round(args.ui_ms * 1e6))
    rows = db.execute(
        'SELECT ts_ns, name FROM events WHERE name LIKE ? AND dur_ns>0 '
        'AND ABS(ts_ns - ?) < 5000000 ORDER BY ABS(ts_ns - ?) LIMIT 5',
        (f'%{args.sub}%', target, target)).fetchall()
    if not rows:
        print('未找到参照事件'); return 2
    if len(rows) > 1 and rows[0][2] != rows[1][2]:
        print('多个候选, 请细化函数名:')
        for r in rows:
            print(f'   abs={r[0]}ns  {r[1][:70]}')
        return 2
    new_zero = rows[0][0] - int(round(args.ui_ms * 1e6))
    db.execute("UPDATE meta SET value=? WHERE key='ui_zero'", (str(new_zero),))
    db.execute("INSERT OR REPLACE INTO meta VALUES('ui_zero_source','manual')")
    db.commit()
    print(f'UI 零点已校准: {uiz} → {new_zero} ns (参照: {rows[0][1][:60]} @ UI {args.ui_ms}ms)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('cmd', choices=['find', 'events', 'span', 'window', 'chain',
                                    'calibrate', 'report', 'comm', 'api'])
    ap.add_argument('sub', nargs='?', default=None)
    ap.add_argument('ui_ms', nargs='?', type=float)
    ap.add_argument('ui_ms2', nargs='?', type=float)
    ap.add_argument('--cat', default=None)
    ap.add_argument('--pid', type=int, default=None)
    ap.add_argument('--tol_ms', type=float, default=5.0)
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--raw_ns', type=int, default=None, help='calibrate 用: 参照事件的绝对时间戳(ns)')
    ap.add_argument('--level', default=None, help='api 模式: 过滤 Level (acl/hccl/...)')
    ap.add_argument('--in_step', type=int, default=None, help='span 全实例模式: 限定 step id')
    ap.add_argument('--step', type=int, default=None, help='report 模式: 只看指定 step')
    args = ap.parse_args()
    if args.cmd == 'window':
        if args.sub is None or args.ui_ms is None:
            print('window 需要 <UI起ms> <UI止ms>'); return 2
        args.start_ms, args.end_ms = float(args.sub), args.ui_ms
    elif args.cmd == 'span':
        if args.sub is None or (args.ui_ms is None and args.in_step is None):
            print('span 需要 <函数名> <UI开始ms> 或 <函数名> --in_step N'); return 2
    elif args.cmd == 'chain' and (args.sub is None or args.ui_ms is None):
        print('chain 需要 <函数名> <UI开始ms>'); return 2
    elif args.cmd in ('find', 'events') and args.sub is None:
        print(f'{args.cmd} 需要 <函数名子串>'); return 2
    db = sqlite3.connect(args.db)
    return {'find': cmd_find, 'events': cmd_events, 'span': cmd_span,
            'window': cmd_window, 'chain': cmd_chain, 'calibrate': cmd_calibrate,
            'report': cmd_report, 'comm': cmd_comm, 'api': cmd_api}[args.cmd](db, args)


if __name__ == '__main__':
    sys.exit(main() or 0)
