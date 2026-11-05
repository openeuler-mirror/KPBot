#!/usr/bin/env python3
"""无栈 trace step 识别。

输入:
  trace_view.json（必需）
  同目录侧车（可选，按精度优先级自动查找）:
    ascend_pytorch_profiler_*.db  STEP_TIME
    kernel_details.csv            Step Id + Start Time/Duration
    step_trace_time.csv           每步时长
    analysis.db                   StepTraceTime / CommAnalyzerTime
    communication.json            step0..stepN

用法:
  detect_steps.py <trace_view.json> [--json] [--source auto|step_time|kernel_details|
      step_trace_time|comm|trace] [--min-step-ms MS] [--trailing-min-ms MS] [--validate]

说明:
  - ProfilerStep#N 始终不参与 step 识别（本 skill 面向没有该标识的 trace）
  - 只有 trace_view.json 时: 设备级 sync 结束 + host 首个事件
  - --validate 仅用于开发校验（读 ProfilerStep 计算误差，不参与识别）
"""
import argparse
import csv
import glob
import json
import os
import re
import sqlite3
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

RE_A = re.compile(
    r'\{"ph": "X", "name": "([^"]+)", "pid": (\d+), "tid": (\d+), "ts": "([0-9.]+)", "dur": ([0-9.]+), "cat": "([^"]+)"')
RE_B = re.compile(
    r'\{"name": "([^"]+)", "pid": (\d+), "tid": (\d+), "ts": "([0-9.]+)"(?:, "dur": ([0-9.]+))?, "ph": "X"')
RE_TT = re.compile(r'"Task Type": "([^"]+)"')
RE_TS = re.compile(r'"ts": "([0-9.]+)"')

OA_NAMES = {'Computing', 'Communication', 'Communication(Not Overlapped)', 'Free'}
OA_ACTIVE = {'Computing', 'Communication', 'Communication(Not Overlapped)'}
HOST_CATS = {'cpu_op', 'enqueue', 'dequeue'}


def to_ns(s):
    if '.' in s:
        a, b = s.split('.', 1)
        return int(a) * 1000 + int((b + '000')[:3])
    return int(s) * 1000


def ms(ns):
    return ns / 1e6


def load_trace(path):
    data = open(path, encoding='utf-8', errors='replace').read()
    events = []
    for m in RE_A.finditer(data):
        events.append((to_ns(m.group(4)), to_ns(m.group(5)), m.group(1), m.group(6),
                       int(m.group(2)), int(m.group(3))))
    for m in RE_B.finditer(data):
        tail = data[m.end():m.end() + 300]
        tt = RE_TT.search(tail)
        events.append((to_ns(m.group(4)), to_ns(m.group(5)) if m.group(5) else 0,
                       m.group(1), tt.group(1) if tt else None,
                       int(m.group(2)), int(m.group(3))))
    ui_zero = min(to_ns(m.group(1)) for m in RE_TS.finditer(data))

    host_count = {}
    for ts, dur, name, cat, pid, tid in events:
        if cat in HOST_CATS:
            host_count[pid] = host_count.get(pid, 0) + 1
    host_pid = max(host_count, key=host_count.get) if host_count else None
    if host_pid is None and events:
        host_pid = min(events, key=lambda e: e[0])[4]
    return events, host_pid, ui_zero


def _overlap(a, b, s, e):
    return a < e and s < b


def activity_between(events, a, b, host_pid):
    """判断 (a,b) 内是否有真实设备活动，用于剔除退出 sync。"""
    oa = [e for e in events if e[2] in OA_ACTIVE and e[1] > 0 and _overlap(a, b, e[0], e[0] + e[1])]
    if oa:
        return True
    # 无 Overlap Analysis 的旧数据: 用非 sync/非 host 事件兜底
    for ts, dur, name, cat, pid, tid in events:
        if dur <= 0 or pid == host_pid:
            continue
        if 'Synchronize' in name or 'ProfilerStep#' in name or name in OA_NAMES:
            continue
        if _overlap(a, b, ts, ts + dur):
            return True
    return False


def trace_steps(events, host_pid, min_step_ms=0.0):
    """最差情况: 只用 trace_view.json。"""
    host_evs = [e for e in events if e[4] == host_pid and not e[2].startswith('ProfilerStep#')]
    if not host_evs:
        host_evs = [e for e in events if not e[2].startswith('ProfilerStep#')]
    if not host_evs:
        return [], [], 'trace', 'low'
    step0_start = min(e[0] for e in host_evs)

    sync_kinds = ('SynchronizeDevice', 'SynchronizeStream', 'SynchronizeEvent')
    kept = []
    notes = []
    for kind in sync_kinds:
        cand = sorted([e for e in events if kind in e[2] and e[1] > 0], key=lambda e: e[0])
        if not cand:
            continue
        kept = []
        prev = step0_start
        for ts, dur, name, cat, pid, tid in cand:
            end = ts + dur
            if end <= prev:
                continue
            if activity_between(events, prev, end, host_pid):
                kept.append(end)
                prev = end
        if kept:
            notes.append(f'boundary_source={kind}')
            break

    if not kept:
        notes.append('no_device_sync; fallback=idle_gap')
        return _idle_gap_steps(events, host_pid, step0_start, notes)

    if min_step_ms > 0:
        gap = int(min_step_ms * 1e6)
        clustered = []
        for end in kept:
            if clustered and end - clustered[-1][-1] < gap:
                clustered[-1].append(end)
            else:
                clustered.append([end])
        kept = [c[-1] for c in clustered]
        notes.append(f'merge_sync_gap_lt_{min_step_ms}ms')

    bounds = [step0_start] + kept
    steps = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    return steps, notes, 'trace', 'approx'


def _idle_gap_steps(events, host_pid, step0_start, notes):
    """没有 sync 时的兜底: 设备空闲 gap 分割。低置信度。"""
    dev = [e for e in events if e[1] > 0 and e[4] != host_pid and 'Synchronize' not in e[2]
           and 'ProfilerStep#' not in e[2] and e[2] not in OA_NAMES]
    if not dev:
        return [], notes, 'trace', 'low'
    iv = sorted((e[0], e[0] + e[1]) for e in dev)
    union = []
    for s, e in iv:
        if union and s <= union[-1][1]:
            union[-1][1] = max(union[-1][1], e)
        else:
            union.append([s, e])
    gaps = [(union[i + 1][0] - union[i][1], union[i][1]) for i in range(len(union) - 1)]
    if not gaps:
        return [], notes, 'trace', 'low'
    # 自适应阈值: 取 gap 中位数的 4 倍，至少 0.3ms
    med = sorted(g for g, _ in gaps)[len(gaps) // 2]
    thr = max(int(0.3e6), med * 4)
    cuts = [end for g, end in gaps if g >= thr]
    bounds = [step0_start] + cuts
    steps = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    notes.append(f'idle_gap_threshold_ms={thr / 1e6:.3f}')
    return steps, notes, 'trace', 'low'


def load_step_time(base):
    dbs = sorted(glob.glob(os.path.join(base, 'ascend_pytorch_profiler_*.db')))
    best = None
    for db in dbs:
        try:
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
            rows = con.execute('SELECT id, startNs, endNs FROM STEP_TIME ORDER BY id').fetchall()
            con.close()
        except Exception:
            continue
        rows = [(int(i), int(s), int(e)) for i, s, e in rows if e > s]
        if rows and (best is None or len(rows) > len(best[0])):
            best = (rows, os.path.basename(db))
    if best is None:
        return None, None
    return best[0], best[1]


def load_kernel_details(base):
    path = os.path.join(base, 'kernel_details.csv')
    if not os.path.exists(path):
        return None
    groups = {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for r in csv.DictReader(f):
            raw = (r.get('Step Id') or '').strip()
            try:
                sid = int(raw)
                start = float((r.get('Start Time(us)') or '').strip())
                dur = float((r.get('Duration(us)') or '0').strip())
            except (TypeError, ValueError):
                continue
            g = groups.setdefault(sid, [start * 1000, (start + dur) * 1000])
            g[0] = min(g[0], start * 1000)
            g[1] = max(g[1], (start + dur) * 1000)
    if not groups:
        return None
    return [(sid, int(groups[sid][0]), int(groups[sid][1])) for sid in sorted(groups)]


def load_step_trace_time(base):
    path = os.path.join(base, 'step_trace_time.csv')
    rows = []
    if os.path.exists(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            for r in csv.DictReader(f):
                try:
                    sid = int((r.get('Step') or '').strip())
                    dur = float((r.get('Stage') or r.get('stage') or '').strip())
                except (TypeError, ValueError):
                    continue
                rows.append((sid, int(dur * 1000)))
    if rows:
        return rows, 'step_trace_time.csv'
    adb = os.path.join(base, 'analysis.db')
    if os.path.exists(adb):
        try:
            con = sqlite3.connect(f'file:{adb}?mode=ro', uri=True)
            rows = [(int(s), int(float(d) * 1000)) for s, d in
                    con.execute('SELECT step, stage FROM StepTraceTime')]
            con.close()
        except Exception:
            rows = []
    if rows:
        return rows, 'analysis.db:StepTraceTime'
    return None, None


def load_comm_steps(base):
    path = os.path.join(base, 'communication.json')
    if os.path.exists(path):
        try:
            data = json.load(open(path, encoding='utf-8'))
            ids = sorted(int(m.group(1)) for k in data for m in [re.match(r'^step(\d+)$', k)] if m)
            if ids:
                return ids, 'communication.json'
        except Exception:
            pass
    adb = os.path.join(base, 'analysis.db')
    if os.path.exists(adb):
        try:
            con = sqlite3.connect(f'file:{adb}?mode=ro', uri=True)
            ids = sorted(set(int(s.replace('step', '')) for (s,) in
                             con.execute('SELECT DISTINCT step FROM CommAnalyzerTime')))
            con.close()
            if ids:
                return ids, 'analysis.db:CommAnalyzerTime'
        except Exception:
            pass
    return None, None


def _from_relative(steps_rel, base_steps, source, note):
    """侧车只有步数/时长、没有绝对起点时，用 trace sync 边界对齐。"""
    if len(steps_rel) == len(base_steps):
        return [(base_steps[i][0], base_steps[i][1]) for i in range(len(base_steps))], \
            [note], source, 'approx'
    # 数量不一致: 用时长从 trace 起点顺序推，标记 low
    if not base_steps:
        return [], [note + '; no_absolute_boundary'], source, 'low'
    start = base_steps[0][0]
    out = []
    for sid, dur in steps_rel:
        out.append((start, start + dur))
        start += dur
    return out, [note + '; duration_chain'], source, 'low'


def _drop_trailing_short(steps, trailing_min_ms):
    thr = int(trailing_min_ms * 1e6)
    while len(steps) > 1 and (steps[-1][1] - steps[-1][0]) < thr:
        steps = steps[:-1]
    return steps


def detect(trace_path, force='auto', min_step_ms=0.0, trailing_min_ms=0.5):
    base = os.path.dirname(os.path.abspath(trace_path))
    events, host_pid, ui_zero = load_trace(trace_path)
    has_stack = any(e[3] == 'python_function' for e in events)
    notes = []
    if has_stack:
        notes.append('has_stack=true: 这是有栈 trace，应优先使用有栈分析 skill')

    trace_steps_list, trace_notes, _, _ = trace_steps(events, host_pid, min_step_ms)
    notes.extend(trace_notes)

    source = force
    steps = []
    scope = 'host'
    confidence = 'approx'

    if source == 'auto':
        st, st_name = load_step_time(base)
        kd = load_kernel_details(base)
        stt, stt_name = load_step_trace_time(base)
        comm_ids, comm_name = load_comm_steps(base)
        if st:
            source = 'step_time'
        elif kd:
            source = 'kernel_details'
        elif stt:
            source = 'step_trace_time'
        elif comm_ids:
            source = 'comm'
        else:
            source = 'trace'

    if source == 'step_time':
        st, st_name = load_step_time(base)
        if st:
            steps = [(s, e) for _, s, e in st]
            scope = 'host'
            confidence = 'exact'
            notes.append(f'file={st_name}')
        else:
            source, notes = 'trace', notes + ['step_time 不可用，回退 trace']

    if source == 'kernel_details':
        kd = load_kernel_details(base)
        if kd:
            steps = [(s, e) for _, s, e in kd]
            scope = 'device'
            confidence = 'approx'
            notes.append('file=kernel_details.csv; 起点=该 step 首个内核，通常晚于 host step start')
        else:
            source, notes = 'trace', notes + ['kernel_details 不可用，回退 trace']

    if source == 'step_trace_time':
        stt, name = load_step_trace_time(base)
        if stt:
            steps, more, source, confidence = _from_relative(
                stt, trace_steps_list, 'step_trace_time', f'file={name}; 无绝对起点，用 trace sync 对齐')
            notes.extend(more)
        else:
            source, notes = 'trace', notes + ['step_trace_time 不可用，回退 trace']

    if source == 'comm':
        ids, name = load_comm_steps(base)
        if ids:
            if trace_steps_list and len(ids) == len(trace_steps_list):
                steps = trace_steps_list
                confidence = 'approx'
                notes.append(f'file={name}; 无绝对起点，用 trace sync 对齐')
            else:
                confidence = 'low'
                notes.append(f'file={name}; 步数与 trace 边界不一致，无法定位')
        else:
            source, notes = 'trace', notes + ['communication 不可用，回退 trace']

    if source == 'trace':
        steps = trace_steps_list
        notes.append('只用 trace_view.json')

    # trace 回退时，丢掉退出阶段多出来的极短尾步
    if source in ('trace', 'step_trace_time', 'comm'):
        steps = _drop_trailing_short(steps, trailing_min_ms)

    # 统一按 step id 排序
    steps = sorted(steps, key=lambda x: x[0])
    out = []
    for i, (s, e) in enumerate(steps):
        out.append({'id': i, 'start_ns': int(s), 'end_ns': int(e),
                    'start_ms': round(ms(s - ui_zero), 3), 'end_ms': round(ms(e - ui_zero), 3),
                    'dur_ms': round(ms(e - s), 3)})
    return {
        'trace': os.path.abspath(trace_path),
        'ui_zero_ns': ui_zero,
        'has_stack': has_stack,
        'source': source,
        'scope': scope,
        'confidence': confidence,
        'notes': notes,
        'steps': out,
    }


def validate(result, trace_path):
    # 直接从原始 data 提取 ProfilerStep（仅校验用）
    data = open(trace_path, encoding='utf-8', errors='replace').read()
    gt = []
    for m in RE_A.finditer(data):
        if m.group(1).startswith('ProfilerStep#'):
            s = to_ns(m.group(4))
            gt.append((s, s + to_ns(m.group(5))))
    for m in RE_B.finditer(data):
        if m.group(1).startswith('ProfilerStep#') and m.group(5):
            s = to_ns(m.group(4))
            gt.append((s, s + to_ns(m.group(5))))
    gt = sorted(set(gt))
    if not gt:
        return None
    ours = [(s['start_ns'], s['end_ns']) for s in result['steps']]
    n = min(len(gt), len(ours))
    de = [ms(ours[i][1] - gt[i][1]) for i in range(n)]
    ds = [ms(ours[i][0] - gt[i][0]) for i in range(n)]
    dd = [ms((ours[i][1] - ours[i][0]) - (gt[i][1] - gt[i][0])) for i in range(n)]
    return {
        'gt_steps': len(gt), 'our_steps': len(ours),
        'max_start_err_ms': round(max(abs(x) for x in ds), 4) if ds else None,
        'max_end_err_ms': round(max(abs(x) for x in de), 4) if de else None,
        'max_dur_err_ms': round(max(abs(x) for x in dd), 4) if dd else None,
        'start_err_ms': [round(x, 4) for x in ds],
        'end_err_ms': [round(x, 4) for x in de],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('trace')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--source', default='auto',
                    choices=['auto', 'step_time', 'kernel_details', 'step_trace_time', 'comm', 'trace'])
    ap.add_argument('--min-step-ms', type=float, default=0.0,
                    help='trace 回退时合并间隔小于该值的 sync（防一步多 sync）')
    ap.add_argument('--trailing-min-ms', type=float, default=0.5,
                    help='丢弃短于该值的尾部 step（退出 sync 造成），默认 0.5ms')
    ap.add_argument('--validate', action='store_true', help='开发校验：读 ProfilerStep 算误差')
    args = ap.parse_args()

    result = detect(args.trace, args.source, args.min_step_ms, args.trailing_min_ms)
    if args.validate:
        result['validation'] = validate(result, args.trace)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"trace: {result['trace']}")
    print(f"source: {result['source']} ({result['confidence']}, {result['scope']})")
    print(f"has_stack: {result['has_stack']}")
    if result['notes']:
        print('notes: ' + ' | '.join(result['notes']))
    print(f"steps: {len(result['steps'])}")
    for s in result['steps']:
        print(f"  #{s['id']}  UI {s['start_ms']:10.3f} ~ {s['end_ms']:10.3f} ms  dur {s['dur_ms']:9.3f} ms")
    if result.get('validation'):
        v = result['validation']
        print(f"validate(ProfilerStep): gt={v['gt_steps']} our={v['our_steps']} "
              f"max_start_err={v['max_start_err_ms']}ms max_end_err={v['max_end_err_ms']}ms "
              f"max_dur_err={v['max_dur_err_ms']}ms")


if __name__ == '__main__':
    main()
