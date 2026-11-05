#!/usr/bin/env python3
"""无栈 trace 的近似阶段/调用栈/Free 归因。

依据：设备内核顺序 + host cpu_op/CANN API + vllm/vllm-ascend 0.23.0 源码知识。
注意：没有 python_function，以下都是 operator/phase 级近似，不是真实 Python 栈。
"""
import csv
import html
import os
import re
import bisect
from collections import Counter, defaultdict

COMM_HINTS = ('hcom', 'allreduce', 'allgather', 'reducescatter', 'alltoall', 'broadcast')
FWD_HINTS = ('matmul', 'attention', 'split_qkv', 'addrmsnorm', 'swiglu', 'reshapeandcache')
SAMPLE_HINTS = ('sort', 'applytopk', 'softmax', 'argmax', 'randomuniform')
P1_KERNEL_HINTS = ('fill', 'index', 'cast', 'add', 'sub', 'ge', 'lt', 'bitwise', 'muls',
                   'mul', 'copy', 'slot_mapping', 'greater', 'less', 'logical')

FUNC_LABELS = {
    'allreduce': 'TP all_reduce / all_gather',
    'split_qkv': 'triton_split_qkv_rmsnorm_mrope',
    'attention': 'forward_fused_infer_attention',
    'gemm': 'unquantized_gemm / linear',
    'norm': 'npu_add_rms_norm_bias',
    'act': 'npu_swiglu',
    'cache': 'reshape_and_cache',
    'sampling': 'AscendSampler (top-k/top-p/softmax/argmax)',
    'logits': 'compute_logits',
    'slot': '_prepare_inputs (slot mapping)',
    'input': '_update_states / input prep',
    'forward_aux': 'forward 辅助 (index/slice/cast)',
    'comm': 'all_reduce / all_gather',
    'moe': 'MoE gate / expert GEMM',
    'other': '其他',
}


def esc(s):
    return html.escape(str(s), quote=True)


def read_kernel_rows(base):
    path = os.path.join(base, 'kernel_details.csv')
    if not os.path.exists(path):
        return {}
    out = defaultdict(list)
    for r in csv.DictReader(open(path, encoding='utf-8', errors='replace')):
        try:
            sid = int((r.get('Step Id') or '').strip())
            start = float((r.get('Start Time(us)') or '').strip()) * 1000
            dur = float((r.get('Duration(us)') or '0').strip()) * 1000
            name = r['Name'][:120]
        except (TypeError, ValueError):
            continue
        out[sid].append({'start': start, 'end': start + dur, 'dur': dur,
                         'name': name, 'type': r.get('Type', ''),
                         'stream': r.get('Stream ID', '')})
    return out


def is_comm(name):
    low = name.lower()
    return any(h in low for h in COMM_HINTS)


def is_forward(name):
    low = name.lower()
    return any(h in low for h in FWD_HINTS)


def is_sample(name):
    low = name.lower()
    return any(h in low for h in SAMPLE_HINTS)


def is_moe(name):
    low = name.lower()
    return any(h in low for h in ('moe', 'gating', 'groupedmatmul'))


def is_p1(name):
    low = name.lower()
    return any(h in low for h in P1_KERNEL_HINTS)


def kernel_func(name, phase=None):
    low = name.lower()
    if is_comm(name):
        return 'allreduce'
    if 'slot_mapping' in low:
        return 'slot' if phase in (None, 'P1') else 'forward_aux'
    if phase == 'P1':
        if is_p1(name):
            return 'input'
        if 'matmul' in low or 'gemm' in low:
            return 'gemm'
        return 'other'
    if phase == 'P3':
        if 'matmul' in low or 'gemm' in low:
            return 'logits'
        if is_sample(name) or any(x in low for x in ('cast', 'transpose', 'realdiv', 'copy')):
            return 'sampling'
        return 'other'
    # P2 / 未指定: forward 内部
    if 'split_qkv' in low:
        return 'split_qkv'
    if 'attention' in low:
        return 'attention'
    if is_moe(name):
        return 'moe'
    if 'matmul' in low or 'gemm' in low:
        return 'gemm'
    if 'addrmsnorm' in low:
        return 'norm'
    if 'swiglu' in low or 'silu' in low or 'gelu' in low:
        return 'act'
    if 'reshapeandcache' in low or 'cache' in low:
        return 'cache'
    if 'applytopk' in low or 'softmax' in low or 'argmax' in low or 'sort' in low:
        return 'sampling'
    if is_p1(name):
        return 'forward_aux'
    return 'other'


def main_stream(rows):
    if not rows:
        return []
    sc = Counter(r['stream'] for r in rows)
    main = sc.most_common(1)[0][0]
    return sorted([r for r in rows if r['stream'] == main], key=lambda r: r['start'])


def phase_windows(step, rows, events=None, host_pid=None):
    ks = main_stream(rows)
    fwd = [r for r in ks if is_forward(r['name'])]
    sample = [r for r in ks if is_sample(r['name'])]
    if not fwd:
        return None
    p2_start = fwd[0]['start']
    if sample:
        last_mat = [r for r in ks if r['start'] < sample[0]['start'] and 'matmul' in r['name'].lower()]
        p3_start = last_mat[-1]['start'] if last_mat else sample[0]['start']
    else:
        p3_start = max(r['end'] for r in fwd)
    # compute_logits 起点: 采样前最后一个 all_gather host 事件（有则用，和 with-stack 的 P3 语义对齐）
    if events and host_pid is not None and sample:
        host = [e for e in events if e[4] == host_pid and e[1] > 0 and step['start_ns'] <= e[0] < step['end_ns']]
        samp_host = [e for e in host if any(x in e[2].lower() for x in ('applytopk', 'softmax', 'argmax', 'sort'))]
        limit = min(samp_host[0][0], sample[0]['start']) if samp_host else sample[0]['start']
        ag = [e for e in host if any(x in e[2].lower() for x in ('all_gather', 'allgather')) and e[0] < limit]
        if ag:
            # 只有 all_gather 之后不再有 attention 层时，才视为 compute_logits 的 all_gather；
            # 否则它只是 forward 内部的 all_gather，不能把 P3 提前。
            ag_t = ag[-1][0]
            # compute_logits 之后最多只有 MTP draft 的少量 attention（≤3）；
            # 若后面还有大量 attention 层，说明该 all_gather 属于 forward 内部。
            attn_after = sum(1 for r in rows if r['start'] >= ag_t and 'attention' in r['name'].lower())
            moe_after = any(is_moe(r['name']) for r in rows if r['start'] >= ag_t)
            if not moe_after and attn_after <= 3:
                p3_start = min(p3_start, ag_t)
    return {
        'P1': (step['start_ns'], p2_start),
        'P2': (p2_start, p3_start),
        'P3': (p3_start, step['end_ns']),
    }


def free_by_phase(steps, events, windows):
    comp = [(e[0], e[0] + e[1]) for e in events if e[2] == 'Computing' and e[1] > 0]
    comm = [(e[0], e[0] + e[1]) for e in events if e[2] == 'Communication' and e[1] > 0]
    free = sorted([(e[0], e[0] + e[1]) for e in events if e[2] == 'Free' and e[1] > 0])
    out = {}
    for s in steps:
        w = windows.get(s['id'])
        if not w:
            continue
        phases = {}
        for p, (a, b) in w.items():
            fl = (b - a) - _cover(comp + comm, a, b)
            n = sum(1 for fs, fe in free if a <= fs < b)
            phases[p] = {'free': fl, 'n': n, 'window': b - a}
        out[s['id']] = phases
    return out


def free_attribution(step_id, rows, events, window, phase):
    """Free 区间按最近设备内核归到近似函数；返回 top 列表 + host idle 统计。"""
    # Free = 窗口 − (Computing ∪ Communication)，包含 OA 未覆盖的边缘间隙
    comp_comm = [(e[0], e[0] + e[1]) for e in events
                 if e[2] in ('Computing', 'Communication') and e[1] > 0]
    gaps = _complement(comp_comm, window[0], window[1])
    host = [e for e in events if e[3] in ('cpu_op', 'enqueue', 'dequeue') and e[1] > 0]
    ks = sorted([r for r in rows if not is_comm(r['name']) or True], key=lambda r: r['start'])
    ks = [r for r in ks if window[0] <= r['start'] < window[1]]
    starts = [r['start'] for r in ks]
    agg = defaultdict(float)
    total = 0.0
    host_idle = 0.0
    for fs, fe in gaps:
        total += fe - fs
        if not any(h[0] < fe and h[0] + h[1] > fs for h in host):
            host_idle += fe - fs
        # nearest kernel in this window
        i = bisect.bisect_left(starts, fs)
        prev = ks[i - 1] if i > 0 else None
        nxt = ks[i] if i < len(ks) else None
        dp = fs - prev['end'] if prev else 10 ** 18
        dn = nxt['start'] - fe if nxt else 10 ** 18
        k = prev if dp <= dn else nxt
        if k:
            agg[kernel_func(k['name'], phase)] += fe - fs
    return {'total': total, 'host_idle': host_idle,
            'top': sorted(agg.items(), key=lambda kv: -kv[1])}


def _kshort(name):
    n = re.sub(r'_\d{4,}$', '', name)
    n = re.sub(r'^aclnn', '', n)
    n = n.replace('AiCore', '').replace('AIV', '')
    seg = [s for s in n.split('_') if s]
    while len(seg) >= 2 and seg[-1].lower() == seg[-2].lower():
        seg.pop()
    n = '_'.join(seg)
    if 'matmul' in n.lower() and len(seg) > 1:
        n = seg[-1]
    return n


def _kclass(name):
    ln = name.lower()
    if 'moe' in ln or 'gating' in ln or 'groupedmatmul' in ln:
        return 'moe'
    if 'attention' in ln or 'attn' in ln:
        return 'attn'
    if 'matmul' in ln or 'gemm' in ln:
        return 'gemm'
    if 'norm' in ln:
        return 'norm'
    if 'swiglu' in ln or 'silu' in ln or 'gelu' in ln:
        return 'act'
    if 'cache' in ln:
        return 'cache'
    if 'rope' in ln or 'mrope' in ln:
        return 'rope'
    if 'embed' in ln:
        return 'embed'
    if 'index' in ln or 'slice' in ln or 'check' in ln:
        return 'index'
    if 'topk' in ln or 'topp' in ln or 'sort' in ln or 'sample' in ln or 'argmax' in ln:
        return 'sample'
    return 'other'


def _box(n, lab=''):
    cls = _kclass(n)
    css = {'attn': 'ms-attn', 'gemm': 'ms-gemm', 'norm': 'ms-norm', 'act': 'ms-act',
           'cache': 'ms-cache', 'sample': 'ms-sample', 'embed': 'ms-embed',
           'rope': 'ms-rope', 'index': 'ms-index', 'moe': 'ms-moe'}.get(cls, 'ms-ot')
    txt = f'{lab}·{_kshort(n)}' if lab else _kshort(n)
    return f'<span class="msb {css}" title="{esc(n)}">{esc(txt)}</span>'


def model_structure_html(rows, window=None):
    ks = [r for r in rows if not is_comm(r['name']) and _kclass(r['name']) != 'other']
    if window:
        pre = [r for r in ks if r['start'] < window[0] and _kclass(r['name']) in ('norm', 'embed')]
        ks = ([pre[-1]] if pre else []) + [r for r in ks if window[0] <= r['start'] < window[1]]
    ks = main_stream(ks)
    if len(ks) < 8:
        return ''
    unit = []
    for r in ks:
        if not unit or unit[-1] != r['name']:
            unit.append(r['name'])
    if len(unit) < 4:
        return ''

    def seg_score(idxs):
        segs = [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
        if not segs:
            return None, 0
        mean = sum(segs) / len(segs)
        var = sum((x - mean) ** 2 for x in segs) / len(segs)
        return var / max(mean, 1), mean

    anchor, idxs = None, None
    for n in [x for x in dict.fromkeys(unit) if _kclass(x) == 'attn']:
        ix = [i for i, x in enumerate(unit) if x == n]
        if 2 <= len(ix) <= 128:
            sc, _ = seg_score(ix)
            if sc is not None and sc < 1.0:
                anchor, idxs = n, ix
                break
    if anchor is None:
        best = None
        for n in dict.fromkeys(unit):
            ix = [i for i, x in enumerate(unit) if x == n]
            if not (2 <= len(ix) <= 128):
                continue
            sc, mean = seg_score(ix)
            if sc is None:
                continue
            key = (round(sc, 2), -mean)
            if best is None or key < best[0]:
                best = (key, n, ix)
        if best:
            anchor, idxs = best[1], best[2]

    arrow = '<i class="msa">→</i>'
    note = '主执行流设备内核顺序 · 名称缩写(悬停看全名) · 无栈近似'
    if anchor is None:
        body = arrow.join(_box(n) for n in unit[:40])
        return f'<h3>模型结构（由设备内核顺序推导）</h3><div class="mstruct">{body}</div><div class="pbnote">{esc(note)} · 未识别重复层</div>'

    L = len(idxs)
    segs = [tuple(unit[idxs[i] + 1:idxs[i + 1]]) for i in range(len(idxs) - 1)]
    template = list(Counter(segs).most_common(1)[0][0]) if segs else []
    head, tail = unit[:idxs[0]], unit[idxs[-1] + 1:]
    seqL = [anchor] + template
    attn_i = next((i for i, n in enumerate(seqL) if _kclass(n) == 'attn'), None)
    if attn_i == 0:
        q = max(i for i, n in enumerate(seqL) if _kclass(n) == 'gemm')
        seqL = seqL[q:] + seqL[:q]
        attn_i = next((i for i, n in enumerate(seqL) if _kclass(n) == 'attn'), None)
    gi = 0
    boxes = []
    for i, n in enumerate(seqL):
        k = _kclass(n)
        lab = ''
        if k == 'gemm' and attn_i is not None:
            if i < attn_i:
                lab = 'QKV' if i == 0 else f'GEMM{i}'
            else:
                gi += 1
                lab = {1: 'O', 2: 'MLP-up', 3: 'MLP-down'}.get(gi, f'GEMM{gi}')
        elif k == 'attn':
            lab = 'Attention'
        elif k == 'norm':
            lab = 'Norm'
        elif k == 'act':
            lab = 'Act'
        elif k == 'cache':
            lab = 'KV写'
        elif k == 'rope':
            lab = 'RoPE'
        elif k == 'moe':
            ln = n.lower()
            if 'gating' in ln or 'topk' in ln:
                lab = 'MoE Gate'
            elif 'routing' in ln:
                lab = 'MoE Routing'
            elif 'unpermute' in ln:
                lab = 'MoE Unpermute'
            else:
                lab = 'Expert GEMM'
        boxes.append(_box(n, lab))
    layer = f'<span class="mslayer">×{L}&nbsp;{arrow.join(boxes)}</span>'

    def lab_head(n, i):
        k = _kclass(n)
        if k == 'norm' and 'split_qkv' in n.lower():
            return 'QKVNorm'
        return {'gemm': 'QKV', 'cache': 'KV写', 'rope': 'RoPE'}.get(k, '输入Norm' if k == 'norm' and i == 0 else '')

    def lab_tail(n, i):
        k = _kclass(n)
        if k == 'gemm':
            return {1: 'O', 2: 'MLP-up', 3: 'MLP-down'}.get(sum(1 for x in tail[:i + 1] if _kclass(x) == 'gemm'), 'GEMM')
        if k == 'moe':
            ln = n.lower()
            if 'gating' in ln or 'topk' in ln:
                return 'MoE Gate'
            if 'routing' in ln:
                return 'MoE Routing'
            if 'unpermute' in ln:
                return 'MoE Unpermute'
            return 'Expert GEMM'
        return {'norm': 'Norm', 'act': 'Act', 'embed': 'Embedding'}.get(k, '')

    parts = []
    if head:
        parts.append(arrow.join(_box(n, lab_head(n, i)) for i, n in enumerate(head)) + arrow)
    parts.append(layer)
    if tail:
        parts.append(arrow + arrow.join(_box(n, lab_tail(n, i)) for i, n in enumerate(tail)))
    return f'<h3>模型结构（由设备内核顺序推导）</h3><div class="mstruct">{"".join(parts)}</div><div class="pbnote">{esc(note)}</div>'


def phase_table_html(windows, free_stats):
    rows = []
    for p in ('P1', 'P2', 'P3'):
        a, b = windows[p]
        st = free_stats.get(p, {})
        rows.append(f'<tr><td>{p}</td><td>{(b-a)/1e6:.3f}ms</td>'
                    f'<td>{st.get("free",0)/1e6:.3f}ms</td>'
                    f'<td>{st.get("n",0)}</td></tr>')
    return ('<table><tr><th>阶段</th><th>窗口</th><th>Free</th><th>Free 片段数</th></tr>'
            + ''.join(rows) + '</table>')


def calltree_html(rows, windows, steps, sync_ms):
    """近似调用树：P1 输入准备 / P2 forward / P3 采样收尾。"""
    ks = main_stream(rows)
    phase_ks = {p: [r for r in ks if windows[p][0] <= r['start'] < windows[p][1]] for p in windows}

    def agg_funcs(rs, phase):
        out = defaultdict(lambda: [0.0, 0])
        for r in rs:
            f = kernel_func(r['name'], phase)
            out[f][0] += r['dur']
            out[f][1] += 1
        return sorted(out.items(), key=lambda kv: -kv[1][0])

    def li(f, dur, cnt):
        return f'<li><span class="fn">{esc(FUNC_LABELS.get(f, f))}</span><span class="dur">{dur/1e6:.3f}ms ×{cnt}</span></li>'

    p1 = agg_funcs(phase_ks.get('P1', []), 'P1')
    p2 = agg_funcs(phase_ks.get('P2', []), 'P2')
    p3 = agg_funcs(phase_ks.get('P3', []), 'P3')
    out = '<ul class="tree">'
    out += f'<li><b>P1 输入准备</b> <span class="dur">{(windows["P1"][1]-windows["P1"][0])/1e6:.3f}ms</span><ul>'
    for f, (d, c) in p1:
        out += li(f, d, c)
    out += '</ul></li>'
    out += f'<li><b>P2 forward</b> <span class="dur">{(windows["P2"][1]-windows["P2"][0])/1e6:.3f}ms</span><ul>'
    for f, (d, c) in p2:
        out += li(f, d, c)
    out += '</ul></li>'
    out += f'<li><b>P3 采样收尾</b> <span class="dur">{(windows["P3"][1]-windows["P3"][0])/1e6:.3f}ms</span><ul>'
    for f, (d, c) in p3:
        out += li(f, d, c)
    if sync_ms is not None:
        out += f'<li><span class="fn">end_step 设备同步</span><span class="dur">{sync_ms:.3f}ms</span></li>'
    out += '</ul></li></ul>'
    return out


def free_table_html(attr, used_total=None):
    total = used_total if used_total is not None else attr['total']
    if not total:
        return '<div class="note">无 Free 片段</div>'
    rows = []
    for f, d in attr['top'][:8]:
        rows.append(f'<tr><td>{esc(FUNC_LABELS.get(f, f))}</td><td>{d/1e6:.3f}ms</td>'
                    f'<td>{d/total*100:.1f}%</td></tr>')
    host_idle = attr.get('host_idle', 0)
    if host_idle:
        note = f'其中 host 无重叠事件（可能阻塞/未采样）{host_idle/1e6:.3f}ms'
    else:
        note = 'Free 期间 host 持续有 cpu_op/CANN 事件，未检测到 host 空闲；设备 Free 更可能来自等待/下发节奏/依赖'
    return ('<table><tr><th>Free 去向（近似）</th><th>时长</th><th>占 Free</th></tr>'
            + ''.join(rows) + '</table><div class="note">' + esc(note) + '</div>')

def _union(ivs):
    out = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _cover(ivs, a, b):
    return sum(max(0, min(e, b) - max(s, a)) for s, e in _union(ivs))


def _complement(ivs, a, b):
    out, prev = [], a
    for s, e in _union(ivs):
        if s > prev:
            out.append((prev, min(s, b)))
        prev = max(prev, e)
        if prev >= b:
            break
    if prev < b:
        out.append((prev, b))
    return out


def oa_classes(steps, events):
    """在检测出的 step 窗口内用 Overlap Analysis 重算四分类（µs）。

    注意：step_trace_time.csv 的 Free/Stage 可能对应不同窗口，不能直接当本 step 的四分类。
    """
    comp = [(e[0], e[0] + e[1]) for e in events if e[2] == 'Computing' and e[1] > 0]
    comm = [(e[0], e[0] + e[1]) for e in events if e[2] == 'Communication' and e[1] > 0]
    cno = [(e[0], e[0] + e[1]) for e in events if e[2] == 'Communication(Not Overlapped)' and e[1] > 0]
    out = {}
    for s in steps:
        a, b = s['start_ns'], s['end_ns']
        dur = b - a
        cl = _cover(comp, a, b)
        ml = _cover(comm, a, b)
        nl = _cover(cno, a, b)
        fl = dur - _cover(comp + comm, a, b)
        out[s['id']] = {
            'stage': dur / 1000.0,
            'comp': cl / 1000.0,
            'comm_no': nl / 1000.0,
            'comm': ml / 1000.0,
            'free': fl / 1000.0,
            'overlap': max(0.0, (ml - nl) / 1000.0),
        }
    return out
