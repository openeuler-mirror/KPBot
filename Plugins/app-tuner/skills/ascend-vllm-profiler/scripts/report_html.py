#!/usr/bin/env python3
"""HTML 报告生成: 首页汇总(简要版) + 每 step 一页(详细版).

用法:
  report_html.py <db> [--out report.html] [--step N]

结构:
  首页  全 step 汇总表 (阶段耗时/四分类/end_sync/等待/判定) + 通信信号
  每步  P1(锚点切节)/forward(包装链+算子计数+代表实例)/P3(锚点切节) + 实例级可折叠流程树

节切分锚点(vllm 结构, 缺失时相邻节合并并标注):
  P1: synchronize_input_prep / _update_states / _prepare_inputs / _build_attention_metadata / _preprocess
  P3: compute_logits / sample_tokens / _sample / propose_draft_token_ids / end_step
"""
import argparse, html, json, re, sqlite3, sys
from collections import Counter


US_GROUP = 0.05

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

KNOWLEDGE = {
    'P1-A': '从调度通道取回本步输入并反序列化张量。结束意味着 worker 拿到完整调度结果。',
    'P1-B': 'event 级同步确认上一步收尾，调度结果落到批状态。结束意味着批内容确定。',
    'P1-C': '构造本步全部输入张量（block table、input ids、位置、slot mapping、spec decode 元数据）。首批 H2D 在此发出。',
    'P1-D': '为各注意力组构建执行元数据。结束意味着注意力执行条件全部就绪。',
    'P1-E': 'VLM 视觉编码：图像 batch 视觉 transformer 编码并合并进文本 embedding。纯 LLM 模型无此节。',
    'P2': '编译图回放执行模型前向。层结构用算子计数印证。',
    'P3-A': 'lm_head 投影取 logits 并把 execute_model 输出回传调度进程。',
    'P3-B': '拒绝采样 + MTP draft 草拟（P3 主体）。',
    'P3-B1': '对目标模型 logits 做拒绝采样（top-k/top-p + 约束展开 + 恢复采样）。',
    'P3-B2': 'MTP draft 模型三轮草拟，每轮产出一份 draft logits。轮间 host 段是本节 Free 主体。',
    'P3-B3': '切 aux 流执行执行后状态更新（mamba 融合后处理），随后恢复主流。',
    'P3-C': 'end_step 步级收尾与设备同步。同步时长是 host/device bound 指示器。',
}

BADGE = {'computing': 'c-comp', 'communication': 'c-comm',
         'comm_not_overlapped': 'c-cno', 'free': 'c-free'}

STAGE_COLOR = {'P1': '#6F8FA3', 'P2': '#95879F', 'P3': '#869B85'}
FLAME_SHADE = {
    'P1': ['#71879B', '#7C93A6', '#8FA3B2', '#A3B4C0'],
    'P2': ['#877A93', '#95879F', '#A79BAD', '#B7ADBF'],
    'P3': ['#7A8F79', '#869B85', '#98AA96', '#A8B8A6'],
}

KEY_PATTERNS = (
    'shm_broadcast.py(772): dequeue', 'acquire_read', 'function loads',
    '_load_from_bytes', '_rebuild_tensor_v2',
    ': synchronize_input_prep', ': _update_states',
    'remove_request', '_zero_block_ids', 'register_request', 'add_request',
    '_init_mrope_positions', 'update_req_spec_token_ids', '_may_reorder_batch',
    'refresh_metadata', 'sampling_type', 'on_requests_finished',
    ': _prepare_inputs', 'commit_block_table', '_prepare_input_ids',
    '_calc_mrope_positions', 'copy_to_gpu', 'copy_snapshot_to_gpu',
    ': repeat', '_build_attn_state', '_get_cumsum_and_arange',
    '_compute_prev_positions', '_sync_num_accepted_tokens',
    'update_num_computed_tokens_for_batch_change',
    '_correct_optimistic_seq_lens_cpu', 'compute_slot_mapping',
    '_calc_spec_decode_metadata', '_determine_batch_execution_and_padding',
    'correct_spec_decode_token_counts', '_get_valid_sampled_token_count',
    'preprocess_mamba', 'stage_postprocess_inputs_to_gpu',
    ': _build_attention_metadata', ': _build_attn_group_metadata',
    'attention_v1.py(276): build', 'gdn_attn_builder', 'mamba_get_block_table_tensor',
    '_stable_argsort_for_npu', '_build_non_spec_chunked_prefill_metadata',
    '_build_prefill_has_initial_state', '_treat_single_token_prefills_with_state_as_decodes',
    '_fold_spec_sized_prefill_chunks_into_spec', '_copy_sequence_indices_to_device',
    'compute_causal_conv1d_metadata', '_attach_spec_decode_metadata',
    '_attach_non_spec_prefill_metadata', '_attach_non_spec_decode_metadata',
    '_build_actual_seq_lengths',
    ': _preprocess', '_execute_mm_encoder', 'group_and_batch_mm_kwargs',
    'embed_multimodal', '_process_image_input', 'Qwen3_VisionTransformer',
    'prepare_encoder_metadata', 'VisionBlock', 'PatchMerger', 'PatchEmbed',
    'embed_input_ids', '_merge_multimodal_embeddings', '_gather_mm_embeddings',
    'qwen3_5.py(626): forward', '<string>(19): forward',
    ': compute_logits', ': sample_tokens', ': _sample',
    'rejection_sampler', 'AscendSampler', 'TopKTopP', 'random_sample',
    'apply_sampling_constraints', 'rejection_sample',
    ': propose_draft_token_ids', ': _propose', '_run_merged_draft',
    '_copy_valid_sampled_token_count', 'prepare_inputs_padded', '_get_positions',
    'qwen3_5_mtp.py(418): forward', 'qwen3_5_mtp.py(432): compute_logits',
    'qwen3_5_mtp.py(392): embed_input_ids',
    ': end_step', '_update_states_after_model_execute', 'postprocess_mamba_align_gpu',
    ': handle_output', 'npu/utils.py(62): synchronize',
    ': _model_forward', '_update_full_graph_params_if_needed', 'update_full_graph_params',
    'update_graph_params', 'graph_task_update', 'compilation/acl_graph.py(138)',
    'graphs.py(638): replay', 'vocab_parallel_embedding', '_maybe_pad_and_reduce',
    'tensor_model_parallel_all_reduce',
)


def ui(db):
    return int(db.execute("SELECT value FROM meta WHERE key='ui_zero'").fetchone()[0])


def uims(ns, uiz):
    return (ns - uiz) / 1e6


def union_covered(db, a, b):
    """窗口内 computing ∪ communication 的覆盖时长(ns)."""
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


def free_intervals(db, a, b):
    """窗口内 Free 区间 = 窗口 − (computing ∪ communication).

    Overlap Analysis 区间不覆盖窗口边缘(如步首 1~2.3ms), 这些缺口设备确实
    空闲, 按 SKILL 定义计入 Free, 保证 Computing+未重叠+Free = 100%.
    """
    ivs = sorted((max(t, a), min(e, b)) for t, e in db.execute(
        "SELECT start_ns, end_ns FROM classes WHERE kind IN ('computing','communication') "
        'AND end_ns>? AND start_ns<?', (a, b)))
    out, prev = [], a
    for s, e in ivs:
        if e <= s:
            continue
        if s > prev:
            out.append((prev, s))
        prev = max(prev, e)
    if prev < b:
        out.append((prev, b))
    return [x for x in out if x[1] > x[0]]


def cls_stats(db, a, b):
    s = b - a
    tot = {}
    for kind in ('computing', 'communication', 'comm_not_overlapped'):
        rows = db.execute(
            'SELECT start_ns, end_ns FROM classes WHERE kind=? AND end_ns>? AND start_ns<?',
            (kind, a, b)).fetchall()
        tot[kind] = sum(min(e, b) - max(t, a) for t, e in rows)
    tot['free'] = s - union_covered(db, a, b)
    return tot, s


class WindowClasses:
    """窗口内四分类区间缓存: 每棵树只查一次库, 逐节点内存计算."""

    def __init__(self, db, a, b):
        self.lists = {}
        for kind in ('computing', 'communication', 'comm_not_overlapped'):
            rows = db.execute(
                'SELECT start_ns, end_ns FROM classes WHERE kind=? AND end_ns>? AND start_ns<?',
                (kind, a, b)).fetchall()
            self.lists[kind] = sorted((max(t, a), min(e, b))
                                      for t, e in rows if min(e, b) > max(t, a))
        self.merged = sorted(self.lists['computing'] + self.lists['communication'])

    @staticmethod
    def _covered(ivs, a, b):
        return sum(min(e, b) - max(t, a) for t, e in ivs if t < b and e > a)

    def stats(self, a, b):
        if b <= a:
            return None
        s = b - a
        cov, cur = 0, None
        for t, e in self.merged:
            t2, e2 = max(t, a), min(e, b)
            if e2 <= t2:
                continue
            if cur is None:
                cur = [t2, e2]
            elif t2 <= cur[1]:
                cur[1] = max(cur[1], e2)
            else:
                cov += cur[1] - cur[0]
                cur = [t2, e2]
        if cur:
            cov += cur[1] - cur[0]
        return {'computing': self._covered(self.lists['computing'], a, b),
                'communication': self._covered(self.lists['communication'], a, b),
                'comm_not_overlapped': self._covered(self.lists['comm_not_overlapped'], a, b),
                'free': s - cov}


BAR_COLORS = {'comp': '#4A78A0', 'comm': '#B08968', 'cno': '#B08968',
              'commov': '#CFAF87', 'free': '#A6BE9E'}
BAR_MIN_NS = 100000


def mini_bar(st, dur_ns, big=False):
    """函数行尾的设备状态小条: 定宽按占比分段.

    comm == commNO(无重叠) → 三段 [Computing|Communication|Free];
    comm > commNO → 四段 [纯Computing|重叠通信(浅陶土)|未重叠通信|Free] (加和恒 100%).
    """
    if not st or dur_ns is None or dur_ns <= 0:
        return ''
    s = float(dur_ns)
    comp = st['computing'] / s
    comm = st['communication'] / s
    cno = st['comm_not_overlapped'] / s
    free = st['free'] / s
    ov = comm - cno
    if ov < 0.005:
        segs = (('comp', comp), ('comm', cno), ('free', free))
    else:
        segs = (('comp', max(comp - ov, 0.0)), ('commov', ov), ('cno', cno), ('free', free))
    parts = ''.join(
        f'<i style="width:{v * 100:.2f}%;background:{BAR_COLORS[k]}"></i>'
        for k, v in segs if v >= 0.005)
    tip = (f"Computing {comp * 100:.1f}% ({st['computing'] / 1e6:.3f}ms) · "
           f"Communication {comm * 100:.1f}% (未重叠 {cno * 100:.1f}%, {st['comm_not_overlapped'] / 1e6:.3f}ms) · "
           f"Free {free * 100:.1f}% ({st['free'] / 1e6:.3f}ms)")
    cls = 'mbar big' if big else 'mbar'
    return f'<span class="{cls}" title="{esc(tip)}">{parts}</span>'


BAR_LEGEND = ('<div class="barleg">行尾小条 = 该函数时段内设备状态<i style="background:'
              '#4A78A0"></i>Computing <i style="background:#CFAF87"></i>通信(与计算重叠)'
              '<i style="background:#B08968"></i>通信(未重叠)<i style="background:'
              '#A6BE9E"></i>Free（悬停看精确值）</div>')


def cls_line(db, a, b):
    tot, s = cls_stats(db, a, b)
    c = tot['communication']
    no = tot['comm_not_overlapped']
    return (f"Computing {tot['computing']/s*100:.1f}% / Communication {c/s*100:.1f}%"
            f"(未重叠 {no/s*100:.1f}%) / Free {tot['free']/s*100:.1f}%")


def esc(s):
    return html.escape(str(s), quote=True)


def pids(db):
    m = dict(db.execute("SELECT key, value FROM meta"))
    return {'py': int(m.get('python_pid') or 176),
            'dev': int(m.get('device_pid') or 180672),
            'cann': int(m.get('cann_pid') or 180479),
            'oa': int(m.get('oa_pid') or 181216)}


def longest(db, pattern, a, b, exclude=()):
    P = pids(db)
    conds = [f"pid={P['py']}", "cat='python_function'", 'dur_ns>0', 'name LIKE ?', 'ts_ns>=?', 'ts_ns<=?']
    params = [f'%{pattern}%', a, b]
    for x in exclude:
        conds.append('name NOT LIKE ?')
        params.append(f'%{x}%')
    rows = db.execute(
        f"SELECT ts_ns, dur_ns, name FROM events WHERE {' AND '.join(conds)} "
        'ORDER BY dur_ns DESC LIMIT 1', params).fetchone()
    return rows


def after(db, pattern, ts, b, exclude=()):
    P = pids(db)
    conds = [f"pid={P['py']}", "cat='python_function'", 'dur_ns>0', 'name LIKE ?', 'ts_ns>=?', 'ts_ns<?']
    params = [f'%{pattern}%', ts, b]
    for x in exclude:
        conds.append('name NOT LIKE ?')
        params.append(f'%{x}%')
    return db.execute(
        f"SELECT ts_ns, dur_ns, name FROM events WHERE {' AND '.join(conds)} ORDER BY ts_ns LIMIT 1",
        params).fetchone()


def main_tid(db, a, b):
    r = db.execute(
        f"SELECT tid, COUNT(*) FROM events WHERE pid={pids(db)['py']} AND cat='python_function' AND dur_ns>0 "
        'AND ts_ns>=? AND ts_ns+dur_ns<=? GROUP BY tid ORDER BY 2 DESC LIMIT 1', (a, b)).fetchone()
    return r[0] if r else pids(db)['py']


def build_tree(db, a, b, tid, cats=('python_function',)):
    ph = ','.join('?' for _ in cats)
    P = pids(db)
    evs = [(ts, ts + d, n) for ts, d, n in db.execute(
        f'SELECT ts_ns, dur_ns, name FROM events WHERE pid={P["py"]} AND tid=? AND cat IN ({ph}) '
        'AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=? ORDER BY ts_ns',
        (tid, *cats, a - 2000, b + 2000))]
    roots, stack = [], []
    for s, e, n in evs:
        node = {'n': n, 's': s, 'e': e, 'ch': []}
        while stack and stack[-1]['e'] <= s:
            stack.pop()
        (stack[-1]['ch'] if stack else roots).append(node)
        stack.append(node)

    def lift(nodes):
        out = []
        for nd in nodes:
            if collapsed_name(nd['n']):
                out.extend(lift(nd['ch']))
            else:
                nd['ch'] = lift(nd['ch'])
                out.append(nd)
        return out
    return lift(roots)


def collapsed_name(n):
    return any(p in n for p in COLLAPSE_SUBSTR)


def prune_dur(nodes, min_ms):
    out = []
    for nd in nodes:
        if nd['s'] is not None and (nd['e'] - nd['s']) / 1e6 >= min_ms:
            nd['ch'] = prune_dur(nd['ch'], min_ms)
            out.append(nd)
    return out


def tree_count(nodes):
    return sum(1 + tree_count(n['ch']) for n in nodes)


def fit_budget(nodes, budget=6000):
    min_ms = 0.0
    while tree_count(nodes) > budget and min_ms < 50:
        min_ms = 0.05 if min_ms == 0 else min_ms * 4
        nodes = prune_dur(nodes, min_ms)
    return nodes


def agg_us(nodes):
    out, small, tot = [], 0, 0.0
    for nd in nodes:
        agg_us(nd['ch'])
        if (nd['e'] - nd['s']) / 1e6 < US_GROUP and not nd['ch']:
            small += 1
            tot += (nd['e'] - nd['s']) / 1e6
        else:
            out.append(nd)
    if small >= 4:
        out.append({'n': f'µs 级杂项 ×{small}', 's': None, 'e': None,
                    'ch': [], 'agg': tot})
    return out


def short(n):
    n = n.replace('/vllm-workspace/vllm-ascend/', 'va/').replace('/vllm-workspace/vllm/', 'vllm/')
    parts = n.split('/')
    return '/'.join(parts[-2:]) if len(parts) > 2 else n


def render_tree(nodes, uiz, wc=None, depth=0):
    if not nodes:
        return ''
    out = ['<ul class="tree">']
    for nd in nodes:
        agg = nd.get('agg')
        if agg:
            label = f'{esc(bare_fn(nd["n"]))} ×{agg["count"]}'
            dur_txt = (f'合计{agg["total_ns"] / 1e6:.3f}ms · '
                       f'单次{agg["min_ns"] / 1e6:.3f}~{agg["max_ns"] / 1e6:.3f}ms')
        else:
            label = esc(bare_fn(nd['n']))
            dur_txt = f'{(nd["e"] - nd["s"]) / 1e6:.3f}ms'
        cmt = comment_for(nd['n'])
        cmt_html = f'<span class="cmt">— {esc(cmt)}</span>' if cmt else ''
        bar = ''
        if wc is not None and nd.get('s') is not None and nd['e'] - nd['s'] >= BAR_MIN_NS:
            bar = mini_bar(wc.stats(nd['s'], nd['e']), nd['e'] - nd['s'])
        row = (f'<span class="rowline"><span class="fn">{label}</span>'
               f'<span class="dur">{dur_txt}</span>{cmt_html}{bar}</span>')
        opn = ' open' if depth <= 1 else ''
        if nd['ch']:
            out.append(f'<li><details{opn}><summary>{row}</summary>'
                       + render_tree(nd['ch'], uiz, wc, depth + 1) + '</details></li>')
        else:
            out.append(f'<li>{row}</li>')
    out.append('</ul>')
    return ''.join(out)


def stage_split(db, s0, s1):
    # 首选 _model_forward: 所有执行模式都存在(eager/FX eager/整图回放),
    # 图回放步没有模型级 forward python 帧, 旧锚点会误回退到 _execute_model_orig
    fwd = longest(db, '_model_forward', s0, s1)
    if fwd and fwd[1] > 100000:
        f0, f1 = fwd[0], fwd[0] + fwd[1]
        return (s0, f0), (f0, f1), (f1, s1), fwd[2].split('/')[-1]
    fwd = longest(db, ': forward', s0, s1,
                  exclude=('sampler', 'mtp', '<string>', 'aot_autograd', '/layers/',
                           'attention/', '/ops/', 'sample/'))
    if fwd and fwd[1] > 500000:
        f0, f1, fname = fwd[0], fwd[0] + fwd[1], fwd[2].split('/')[-1]
        return (s0, f0), (f0, f1), (f1, s1), fname
    ex = longest(db, '_execute_model_orig', s0, s1)
    if ex:
        f0, f1 = ex[0], ex[0] + ex[1]
        return (s0, f0), (f0, f1), (f1, s1), '(无模型级forward,退化为execute_model窗口)'
    return (s0, s1), None, None, '(未找到forward锚点)'


P2_MODE_TEXT = {
    'graph_replay': (
        'NPU 整图回放：本步为纯 decode（均匀批：每请求调度 1+draft 数个 token），ACLGraphWrapper 命中引擎初始化时'
        '捕获的 acl graph，host 仅执行 replay 下发（~ms 级）+ 图参数更新（update_stream），'
        '设备异步执行整图——P3 期间的设备忙主要是本图排空，图捕获发生在 profiler 启动前，trace 不可见。'),
    'fx_eager': (
        'torch.compile FX 图 eager 执行：本步批内含 prefill 请求（混合批或纯 prefill，非均匀 decode），cudagraph 模式不匹配，'
        'ACLGraphWrapper 直接执行 runnable，FX 图经 torch_npu fx_run_eagerly 逐算子下发（非整图回放），'
        'host 逐算子下发是 Free 的来源。'),
    'eager': '未识别出编译图/整图回放标志，按普通 eager 前向解读。',


}


def p2_mode(db, a, b):
    if db.execute(
            f"SELECT 1 FROM events WHERE pid={pids(db)['py']} AND cat='python_function' "
            "AND name LIKE '%: replay' AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=? LIMIT 1",
            (a, b)).fetchone():
        return 'graph_replay'
    if db.execute(
            "SELECT 1 FROM events WHERE name LIKE '%fx_run_eagerly%' AND dur_ns>0 "
            'AND ts_ns>=? AND ts_ns+dur_ns<=? LIMIT 1', (a, b)).fetchone():
        return 'fx_eager'
    return 'eager'


def p1_sections(db, s0, w1_end, w2_start):
    sync = after(db, ': synchronize_input_prep', s0, w1_end)
    b0 = sync[0] if sync else s0
    prep = after(db, ': _prepare_inputs', b0, w2_start)
    c0 = prep[0] if prep else b0
    bam = after(db, ': _build_attention_metadata', c0, w2_start)
    d0 = bam[0] if bam else c0
    pre = longest(db, ': _preprocess', d0, w2_start)
    e0 = pre[0] if pre else None
    secs = [('P1-A', '接收调度结果', s0, b0), ('P1-B', '同步+状态更新', b0, c0),
            ('P1-C', '输入准备', c0, d0), ('P1-D', '注意力元数据', d0, e0 or w2_start)]
    if e0:
        secs.append(('P1-E', 'VLM视觉编码', e0, w2_start))
    else:
        secs[-1] = ('P1-D', '注意力元数据+尾部', d0, w2_start)
    return [s for s in secs if s[3] > s[2]]


def p3_sections(db, w3_start, s1):
    st = longest(db, ': sample_tokens', w3_start, s1, exclude=('_sample_tokens',))
    st_ts = st[0] if st else None
    sto = longest(db, '_sample_tokens_orig', w3_start, s1)
    samp = after(db, ': _sample', w3_start, s1, exclude=('_sample_tokens',)) if sto else None
    prop = longest(db, ': propose_draft_token_ids', w3_start, s1)
    es = longest(db, ': end_step', w3_start, s1)
    secs = []
    a1 = sto[0] if sto else (st_ts or w3_start)
    if a1 > w3_start:
        secs.append(('P3-A', 'logits与输出回传', w3_start, a1))
    if sto:
        if samp and prop and prop[0] > samp[0]:
            secs.append(('P3-B1', '拒绝采样', samp[0], prop[0]))
            secs.append(('P3-B2', 'draft草拟', prop[0], prop[0] + prop[1]))
            b3_end = sto[0] + sto[1]
            if b3_end > prop[0] + prop[1]:
                secs.append(('P3-B3', 'aux流收尾', prop[0] + prop[1], b3_end))
        else:
            secs.append(('P3-B', '采样', samp[0] if samp else sto[0], sto[0] + sto[1]))
    if es:
        secs.append(('P3-C', '步收尾', es[0], es[0] + es[1]))
    return [s for s in secs if s[3] > s[2]]


def op_counts(db, a, b):
    """设备算子计数 (pid=device_pid 的 AI 内核按 name 聚合, 排除 hcom/通信协调内核/协调流)."""
    P = pids(db)
    r = db.execute("SELECT value FROM meta WHERE key='coord_streams'").fetchone()
    coord = json.loads(r[0]) if r and r[0] else []
    ph = ','.join('?' * len(coord))
    rows = db.execute(
        f"SELECT name, COUNT(*), SUM(dur_ns) FROM events "
        f"WHERE pid={P['dev']} AND cat IN ('AI_CORE','AI_VECTOR_CORE','MIX_AIC','MIX_AIV','AI_CPU') "
        f"AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=? "
        f"AND name NOT LIKE 'hcom%' AND name NOT LIKE '%AicpuKernel%'"
        + (f' AND tid NOT IN ({ph})' if coord else '') +
        ' GROUP BY name ORDER BY 2 DESC LIMIT 20', (a, b, *coord)).fetchall()
    return rows


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
    if 'topk' in ln or 'topp' in ln or 'sort' in ln or 'sample' in ln or 'argmax' in ln:
        return 'sample'
    return 'other'


def model_structure_html(db, a, b, s0, s1, mode):
    """从主执行流设备内核时序推导模型结构: 层锚点分段 + 重复单元 + 流程图."""
    P = pids(db)
    r = db.execute("SELECT value FROM meta WHERE key='coord_streams'").fetchone()
    coord = json.loads(r[0]) if r and r[0] else []
    ph = ','.join('?' * len(coord))
    w0, w1 = (s0, s1) if mode == 'graph_replay' and s0 is not None else (a, b)
    rows = db.execute(
        f"SELECT tid, name FROM events WHERE pid={P['dev']} "
        f"AND cat IN ('AI_CORE','AI_VECTOR_CORE','MIX_AIC','MIX_AIV','AI_CPU') "
        f"AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=? "
        f"AND name NOT LIKE 'hcom%' AND name NOT LIKE '%AicpuKernel%'"
        + (f' AND tid NOT IN ({ph})' if coord else '') +
        ' ORDER BY ts_ns', (w0, w1, *coord)).fetchall()
    if len(rows) < 8:
        return ''
    main_tid = Counter(t for t, _ in rows).most_common(1)[0][0]
    unit = []
    for t, n in rows:
        if t != main_tid:
            continue
        if not unit or unit[-1] != n:
            unit.append(n)
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
            sc, _mean = seg_score(ix)
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

    def box(n, lab=''):
        cls = _kclass(n)
        css = {'attn': 'ms-attn', 'gemm': 'ms-gemm', 'norm': 'ms-norm', 'act': 'ms-act',
               'cache': 'ms-cache', 'sample': 'ms-sample', 'embed': 'ms-embed',
               'rope': 'ms-rope'}.get(cls, 'ms-ot')
        txt = f'{lab}·{_kshort(n)}' if lab else _kshort(n)
        return f'<span class="msb {css}" title="{esc(n)}">{esc(txt)}</span>'

    arrow = '<i class="msa">→</i>'
    note = f'主执行流 tid={main_tid} · 按内核 ts 顺序 · 名称缩写(悬停看全名)'
    if anchor is None:
        body = arrow.join(box(n) for n in unit[:40])
        note += ' · 未识别出重复层结构'
        return (f'<h3>模型结构（由设备内核执行顺序推导）</h3>'
                f'<div class="mstruct">{body}</div><div class="pbnote">{esc(note)}</div>')

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
        boxes.append(box(n, lab))
    layer = (f'<span class="mslayer">×{L}&nbsp;{arrow.join(boxes)}</span>')

    def head_lab(n, i):
        k = _kclass(n)
        if k == 'gemm':
            return 'QKV'
        if k == 'cache':
            return 'KV写'
        if k == 'rope':
            return 'RoPE'
        if k == 'norm':
            return '输入Norm' if i == 0 else 'QKVNorm'
        return ''

    def tail_lab(n, i, tot):
        k = _kclass(n)
        if k == 'gemm':
            return {1: 'O', 2: 'MLP-up', 3: 'MLP-down'}.get(sum(1 for x in tail[:i + 1] if _kclass(x) == 'gemm'), 'GEMM')
        if k == 'norm':
            return 'Norm'
        if k == 'act':
            return 'Act'
        if k == 'embed':
            return 'Embedding'
        return ''

    head_html = arrow.join(box(n, head_lab(n, i)) for i, n in enumerate(head))
    tail_html = arrow.join(box(n, tail_lab(n, i, len(tail))) for i, n in enumerate(tail))
    parts = []
    if head_html:
        parts.append(head_html + arrow)
    parts.append(layer)
    if tail_html:
        parts.append(arrow + tail_html)
    return (f'<h3>模型结构（由设备内核执行顺序推导）</h3>'
            f'<div class="mstruct">{"".join(parts)}</div><div class="pbnote">{esc(note)}</div>')


def rep_instance(db, pattern, a, b, exclude=()):
    row = longest(db, pattern, a, b, exclude=exclude)
    if not row:
        return None
    ts, dur, name = row
    tid = main_tid(db, ts, ts + dur)
    tree = tuples_to_dicts(agg_siblings(merge_same_name(
        lift_mech(tree_to_tuples(agg_us(build_tree(
            db, ts, ts + dur, tid, cats=('python_function', 'cpu_op'))))))))
    return name, ts, ts + dur, tree


def step_verdict(p3f, sync_ms, p2f, mode=None):
    if p3f >= 20 and sync_ms < 0.5:
        v = 'host bound(设备提前空闲)'
    elif sync_ms >= 2:
        v = '设备积压(步尾真排水)'
    else:
        v = '设备基本跟上'
    if mode == 'graph_replay':
        return v + '（整图回放步：host 提前完成，设备在 P3 排空）'
    if p2f is not None and p2f >= 30:
        v += ' + forward供数不足'
    return v


def comm_line(db, sid):
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='comm_ops'").fetchone():
        return None
    r = db.execute(
        "SELECT COUNT(*), SUM(elapse_ms), SUM(wait_ms), SUM(transit_ms) FROM comm_ops "
        "WHERE step_name=? AND name!='Total Op Info'", (f'step{sid}',)).fetchone()
    if not r or not r[0]:
        return None
    n, el, wt, tr = r
    return f'通信算子 n={n}, elapse {el:.2f}ms, wait {wt:.2f}ms ({wt / el * 100:.0f}%), transit {tr:.2f}ms'


def free_attr(db, s0, s1):
    tid = main_tid(db, s0, s1)
    frames = []

    def flat(nd):
        frames.append((nd['s'], nd['e'], nd['n']))
        for c in nd['ch']:
            flat(c)
    for r in key_flame_tree(db, s0, s1, tid, agg=False):
        flat(r)
    evs = []
    for s, e, name in frames:
        if e <= s1 and s >= s0:
            evs.append((s, 2, 1, name))
            evs.append((e, 0, -1, name))
    for a, b in free_intervals(db, s0, s1):
        evs.append((a, 1, 1, None))
        evs.append((b, 0, -1, None))
    evs.sort(key=lambda x: (x[0], x[1]))
    other = '其他 host 工作/等待（关键函数之外）'
    agg, tot = {}, 0.0
    stack, free_cnt, prev = [], 0, s0
    for t, _, d, name in evs + [(s1, 0, 0, None)]:
        if t > prev and free_cnt > 0:
            raw = stack[-1] if stack else other
            key = bare_fn(raw)
            if key not in agg:
                agg[key] = [raw, 0.0]
            agg[key][1] += (t - prev) / 1e6
            tot += (t - prev) / 1e6
        prev = t
        if d == 1:
            if name is None:
                free_cnt += 1
            else:
                stack.append(name)
        elif d == -1:
            if name is None:
                free_cnt -= 1
            elif stack:
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()
    top = sorted(agg.items(), key=lambda x: -x[1][1])[:5]
    return tot, [(k, v[0], v[1]) for k, v in top]


CSS = """
*{box-sizing:border-box} body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#F4F2EE;color:#3B4750}
nav{position:sticky;top:0;background:#3B4750;padding:9px 16px;z-index:9;display:flex;gap:6px;flex-wrap:wrap;box-shadow:0 1px 4px rgba(60,70,80,.25)}
nav button{background:#4B5862;color:#C9CFD4;border:0;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:13px;transition:background .15s}
nav button:hover{background:#5A6874}
nav button.on{background:#6F8FA3;color:#fff;font-weight:600}
.page{display:none;max-width:1200px;margin:18px auto;padding:0 14px}
.page.on{display:block}
h1{font-size:21px;margin:6px 0 14px;color:#3B4750}
h2{font-size:17px;margin:26px 0 10px;padding:7px 14px;border-radius:6px;color:#fff}
h2.s-P1{background:linear-gradient(90deg,#6F8FA3,#8FA3B2)}
h2.s-P2{background:linear-gradient(90deg,#95879F,#AC9FB0)}
h2.s-P3{background:linear-gradient(90deg,#869B85,#9DB39B)}
h3{font-size:15px;margin:18px 0 6px}
.sect{background:#fff;border:1px solid #E4E0D9;border-radius:8px;padding:14px 18px;margin:12px 0;box-shadow:0 1px 2px rgba(90,85,75,.06)}
.stagebox{background:#fff;border:1px solid #E4E0D9;border-left:5px solid #6F8FA3;border-radius:8px;padding:14px 18px;margin:14px 0;box-shadow:0 1px 2px rgba(90,85,75,.06)}
.stagebox h3{margin:0 0 10px;font-size:15px;color:#3B4750}
.title-line{font-size:13.5px;font-weight:600;margin:2px 0 8px;color:#4A5560}
.sem{font-size:13px;color:#5F6A74;margin:4px 0 10px;line-height:1.6;border-left:3px solid #D8D4CC;padding-left:10px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th,td{border:1px solid #E6E2DA;padding:6px 10px;text-align:left}
th{background:#F0EDE7;font-weight:600;color:#4A5560}
tr:nth-child(even) td{background:#FBFAF7}
.flame{position:relative;background:#FAF8F5;border:1px solid #E8E4DC;border-radius:6px;overflow:hidden;margin:8px 0 4px}
.fseg{position:absolute;height:20px;overflow:hidden;white-space:nowrap;font-size:11px;line-height:20px;color:#fff;padding:0 4px;border-right:1px solid rgba(255,255,255,.5);border-bottom:1px solid rgba(255,255,255,.35);cursor:default;text-overflow:clip}
.fseg:hover{filter:brightness(1.09);z-index:2}
.fseg .lbl{display:block;overflow:hidden;text-overflow:ellipsis}
.chip{display:inline-block;width:11px;height:11px;border-radius:2px;margin:0 4px -1px 10px}
.mstruct{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin:10px 0;font-size:12px}
.msb{border:1px solid #D8D3C8;border-radius:6px;padding:3px 9px;background:#FAF8F5;white-space:nowrap;color:#3B4750}
.ms-attn{background:#4A78A0;color:#fff;border-color:#3D6488;font-weight:600}
.ms-gemm{background:#E3E9EF;color:#2F5D86}
.ms-norm{background:#EFE9DF;color:#7D5F42}
.ms-act{background:#E8EDE3;color:#44603B}
.ms-cache{background:#EDE4DA;color:#8A5560}
.ms-rope{background:#E9E4F0;color:#5E4B75}
.ms-sample{background:#E6E1EC;color:#5E4B75}
.ms-embed{background:#E2E8E6;color:#3E5B52}
.mslayer{border:1.5px dashed #A9B5BE;border-radius:9px;padding:5px 8px;display:inline-flex;flex-wrap:wrap;gap:5px;align-items:center;background:#F4F3EE;font-weight:600;max-width:100%}
.msa{color:#98A1A8;font-style:normal}
.pbnote{font-size:11.5px;color:#8B9299;margin:2px 0 8px}
.tree{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.55;list-style:none;padding-left:18px;margin:4px 0}
.tree>li{margin:1px 0}
details{display:block} details>summary{cursor:pointer;list-style:none;display:block;padding:1px 0}
details>summary:hover .fn{text-decoration:underline}
details>summary::-webkit-details-marker{display:inline-block}
.rowline{display:flex;align-items:center;gap:8px;min-width:0}
.rowline .fn{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rowline .cmt{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mbar{margin-left:auto;flex:0 0 132px;height:11px;border-radius:2.5px;overflow:hidden;display:flex;background:#EDEAE3;border:1px solid #DDD8CF;cursor:default;vertical-align:middle;box-sizing:border-box}
.mbar i{display:block;height:100%}
.mbar.big{flex:1 1 100%;height:16px;border-radius:4px;margin:0}
.barleg{font-size:11.5px;color:#8B9299;margin:8px 0 0}
.barleg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px -1px 10px;border:1px solid rgba(0,0,0,.08)}
.hero{background:linear-gradient(135deg,#FFFFFF 0%,#F5F3ED 100%);border:1px solid #E4E0D9;border-left:6px solid #4A78A0;border-radius:10px;padding:16px 22px 14px;margin:4px 0 18px;box-shadow:0 2px 6px rgba(90,85,75,.09)}
.hero .htitle{font-size:17.5px;font-weight:700;color:#3B4750;margin-bottom:6px}
.hero .htitle .judge{font-weight:600;color:#4F6059}
.hero .hnums{font-size:14px;color:#4A5560;margin:4px 0 10px}
.hero .hnums b{font-size:20px;color:#4F6059}
.hero .hnums .sep{margin:0 8px;color:#C5CBCF}
.fn{color:#44515D} .dur{color:#97A0A8;margin-left:8px;font-size:12px}
.cmt{color:#8A939C;margin-left:10px;font-size:11.5px}
.agg .fn,.agg .dur{color:#B0B7BD;font-style:italic}
.b{display:inline-block;padding:2px 10px;border-radius:4px;font-size:12px;margin:0 3px;color:#fff;font-weight:600}
.c-comp{background:#4A78A0}.c-comm{background:#B08968}.c-cno{background:#B5838D}.c-free{background:#A6BE9E;color:#3E4C3C}
.verdict{font-weight:700}
.v-host{color:#A65D57}.v-dev{color:#6E8B6E}.v-mid{color:#B08968}
.note{font-size:12px;color:#8B9299;margin:6px 0}
.kv{font-size:13px;color:#545E68;margin:2px 0}
.flamehead{font-size:13px;font-weight:600;color:#4A5560;margin:12px 0 4px}
.flamehead button{background:#EDEAE3;border:1px solid #D5D0C6;border-radius:5px;padding:2px 12px;font-size:12px;cursor:pointer;margin-left:10px;color:#5A6570}
.flamehead button:hover{background:#E2DED4}
.ovstrip{margin-bottom:2px}
.ovstrip .fseg{cursor:pointer;font-weight:600}
.ovstrip .fseg:hover{filter:brightness(1.1)}
.vp{position:absolute;top:0;height:100%;background:rgba(35,48,60,.16);border-left:1px solid #5A6874;border-right:1px solid #5A6874;pointer-events:none;box-sizing:border-box}
.fseg.tiny .lbl{visibility:hidden}
.toolbar{margin:4px 0 8px}
.toolbar button{background:#EDEAE3;border:1px solid #D5D0C6;border-radius:5px;padding:2px 12px;font-size:12px;cursor:pointer;margin-right:6px;color:#5A6570}
.toolbar button:hover{background:#E2DED4}
"""

JS = """
function show(id){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('nav button').forEach(b=>b.classList.remove('on'));
  document.getElementById(id).classList.add('on');
  document.getElementById('btn-'+id).classList.add('on');
}
function xall(root,open){root.querySelectorAll('details').forEach(d=>d.open=open)}
"""


def four_badges(db, a, b):
    tot, s = cls_stats(db, a, b)
    parts = []
    for k in ('computing', 'communication', 'comm_not_overlapped', 'free'):
        parts.append(f'<span class="b {BADGE.get(k, "c-free")}">{k} {tot[k] / s * 100:.1f}%</span>')
    return ' '.join(parts), tot


def stage_box_html(db, sid, s0, s1, w1, w2, w3, step_dur):
    uiz = ui(db)
    rows = []
    stage_defs = [('P1', '推理前准备', w1)]
    if w2:
        stage_defs.append(('P2', '模型前向', w2))
    if w3:
        stage_defs.append(('P3', '采样与收尾', w3))
    for key, label, w in stage_defs:
        tot, dur = cls_stats(db, *w)
        cells = ''
        for k in ('computing', 'communication', 'comm_not_overlapped', 'free'):
            cells += f'<td>{tot[k] / dur * 100:.1f}%</td>'
        rows.append(f'<tr><td><b style="color:{STAGE_COLOR[key]}">{key}</b> {label}</td>'
                    f'<td>{dur / 1e6:.3f}ms</td><td>{dur / 1e6 / step_dur * 100:.1f}%</td>{cells}</tr>')
    head = ('<tr><th>阶段</th><th>耗时</th><th>占 step</th><th>NPU Computing</th>'
            '<th>Communication</th><th>Comm 未重叠</th><th>Free</th></tr>')
    return (f'<div class="stagebox"><h3>阶段总览 · step{sid} · 全步 {step_dur:.3f}ms</h3>'
            f'<table>{head}{"".join(rows)}</table>'
            '<div class="note">NPU 占比按各阶段窗口计算；Computing + 未重叠 + Free = 100%（Free = 窗口 − 计算/通信覆盖）。</div>'
            + BAR_LEGEND + '</div>')


COMMENTS = (
    ('qwen3_5_mtp.py(418)', 'MTP 单层 forward（编译图回放）'),
    ('qwen3_5_mtp.py(432)', 'draft lm_head 投影'),
    ('qwen3_5_mtp.py(392)', 'draft embedding'),
    ('qwen3_5.py(626)', '模型级 forward'),
    ('qwen3_next.py(505)', '核心模型 forward'),
    ('(19): forward', 'FX eager 执行主体'),
    ('npu_fx_compiler.py(485)', 'NPU 编译图执行'),
    ('acl_graph.py(821)', 'acl_graph 图执行入口'),
    ('fx_run_eagerly', '整图回放执行'),
    ('(3703)', '与上步收尾 event 级同步（P1-B 头）'),
    ('(3714)', '注意力元数据就绪同步（P1-D 尾）'),
    ('npu/utils.py(62): synchronize', '设备级同步（host/device bound 指示器）'),
    ('streams.py(183)', 'event 级同步（非设备级）'),
    ('dequeue', '从调度通道取本步输入'),
    ('acquire_read', '共享内存取读权/收尾'),
    ('_load_from_bytes', '反序列化张量载荷'),
    ('_rebuild_tensor_v2', '重建张量（aten::empty/set_）'),
    ('on_requests_finished', '已完成请求回调'),
    ('remove_request', '摘除已完成请求'),
    ('_zero_block_ids', '已完成请求块表清零'),
    ('register_request', '注册请求'),
    ('add_request', '新请求加入 batch'),
    ('_init_mrope_positions', 'VLM mrope 位置初始化'),
    ('update_req_spec_token_ids', 'spec decode token ids 更新'),
    ('_may_reorder_batch', 'decode/prefill 重排'),
    ('refresh_metadata', 'batch 元数据刷新'),
    ('sampling_type', '采样类型读取'),
    (': _prepare_inputs', '构造 kernel 输入（首批 H2D 在此）'),
    ('commit_block_table', '块表提交'),
    ('_prepare_input_ids', 'input ids 组装（本节最大）'),
    ('_calc_mrope_positions', 'VLM mrope 位置计算'),
    ('copy_to_gpu', 'H2D 拷贝'),
    ('copy_snapshot_to_gpu', '快照 H2D'),
    (': repeat', '张量 repeat'),
    ('_build_attn_state', '注意力状态构造'),
    ('_get_cumsum_and_arange', 'cumsum/arange 构造'),
    ('_compute_prev_positions', '上步位置计算'),
    ('_sync_num_accepted_tokens', 'spec decode 采纳数同步'),
    ('update_num_computed_tokens_for_batch_change', '已计算 token 数修正'),
    ('_correct_optimistic_seq_lens_cpu', '乐观 seq len 修正'),
    ('compute_slot_mapping', 'slot mapping 计算'),
    ('_calc_spec_decode_metadata', 'spec decode 元数据'),
    ('_determine_batch_execution_and_padding', '批执行/padding 决策'),
    ('correct_spec_decode_token_counts', 'spec token 计数修正'),
    ('_get_valid_sampled_token_count', '有效采样数获取'),
    ('preprocess_mamba', 'mamba 前置处理'),
    ('stage_postprocess_inputs_to_gpu', 'mamba 后处理输入上卡'),
    (': _build_attention_metadata', '4 个注意力组元数据构建'),
    (': _build_attn_group_metadata', '单注意力组元数据'),
    ('attention_v1.py(276)', '注意力组 build'),
    ('gdn_attn_builder.py(539)', 'GDN 组元数据构建'),
    ('mamba_get_block_table_tensor', 'mamba 块表获取'),
    ('_stable_argsort_for_npu', 'NPU 稳定排序'),
    ('_build_non_spec_chunked_prefill_metadata', 'chunked prefill 元数据（主体）'),
    ('_build_prefill_has_initial_state', '初始状态+因果卷积元数据'),
    ('_treat_single_token_prefills_with_state_as_decodes', '单 token prefill 按 decode 处理'),
    ('_fold_spec_sized_prefill_chunks_into_spec', 'spec 块折叠'),
    ('_copy_sequence_indices_to_device', '序列索引上卡'),
    ('compute_causal_conv1d_metadata', '因果卷积元数据'),
    ('_attach_spec_decode_metadata', '附 spec decode 元数据'),
    ('_attach_non_spec_prefill_metadata', '附 prefill 元数据'),
    ('_attach_non_spec_decode_metadata', '附 decode 元数据'),
    ('_build_actual_seq_lengths', '实际序列长度构建'),
    ('_treat_single_token_prefills', '单 token prefill 按 decode 处理'),
    (': _preprocess', '多模态输入预处理'),
    ('_execute_mm_encoder', '视觉编码器执行'),
    ('group_and_batch_mm_kwargs', '图像分组组批'),
    ('embed_multimodal', '多模态嵌入'),
    ('_process_image_input', '图像输入处理'),
    ('Qwen3_VisionTransformer', '视觉 transformer 主干'),
    ('PatchEmbed', 'patch 嵌入'),
    ('prepare_encoder_metadata', '编码器元数据（位置嵌入插值）'),
    ('VisionBlock', '视觉 transformer 块'),
    ('PatchMerger', 'patch 合并'),
    ('_gather_mm_embeddings', '视觉嵌入聚合'),
    ('embed_input_ids', '文本 embedding'),
    ('_merge_multimodal_embeddings', '视觉+文本嵌入合并'),
    (': _update_states_after_model_execute', '执行后状态更新'),
    ('postprocess_mamba_align_gpu', 'mamba 融合后处理'),
    ('run_fused_postprocess', '融合后处理 triton'),
    (': compute_logits', 'lm_head 投影 + TP 拼接'),
    ('_gather_logits', 'TP all_gather 拼全词表'),
    (': handle_output', '结果回传调度进程'),
    (': _sample_tokens_orig', '采样与 draft 总入口'),
    (': _sample', '拒绝采样'),
    ('rejection_sampler.py(147)', '拒绝采样主流程'),
    ('AscendSampler', '基础采样'),
    ('sampler.py(243)', '采样主体'),
    ('TopKTopP', 'top-k/top-p 采样'),
    ('random_sample', '随机采样（aux 流）'),
    ('apply_sampling_constraints', '采样约束展开'),
    ('expand_batch_to_tokens', 'batch 扩展到 token 维'),
    (': rejection_sample', '拒绝采样核'),
    ('generate_uniform_probs', '均匀分布概率生成'),
    ('sample_recovered_tokens', '恢复 token 采样'),
    ('_bookkeeping_sync', '采样簿记同步'),
    (': propose_draft_token_ids', 'MTP draft 草拟'),
    ('prepare_next_token_ids_padded', '上步 token 对齐'),
    ('_copy_valid_sampled_token_count', '有效采样数 D2H'),
    ('prepare_inputs_padded', 'draft 输入准备'),
    ('_get_positions', '位置获取'),
    (': _propose', 'draft 三轮草拟主体'),
    ('_run_merged_draft', '3 轮草拟循环'),
    ('attn_update_stack_num_spec_norm', 'draft 元数据栈更新'),
    ('build_for_drafting', 'draft 模式 build'),
    (': end_step', '步收尾'),
    ('_forward_core', 'GDN 前向核心'),
    ('qwen_gdn_attention_core', 'GDN 注意力核心'),
    ('rearrange_mixed_qkv', 'qkv 重排（torch.split）'),
    ('fused_gdn_gating', 'GDN 门控'),
    ('l2norm_fwd', 'GDN 归一化'),
    ('clear_ssm_states', '清 SSM 状态'),
    ('chunk_gated_delta_rule_fwd', 'GDN 主计算 triton'),
    (': index_put_', '状态缓存写入'),
    ('worker_busy_loop', 'worker 主循环（等待调度输入/执行）'),
    ('queue.py(154)', '等待队列消息'),
    (': _model_forward', '模型前向入口（图回放/eager 通用）'),
    ('_update_full_graph_params_if_needed', '整图回放后更新图动态参数'),
    ('update_full_graph_params', '图参数更新（KV 地址/seq len 等）'),
    ('update_graph_params', '注意力后端图参数更新'),
    ('graph_task_update_end', '图任务参数提交'),
    ('compilation/acl_graph.py(138)', 'ACLGraphWrapper（回放/内联执行分派）'),
    ('graphs.py(638): replay', 'NPU 整图回放（真·图回放标志）'),
    ('vocab_parallel_embedding', '词表并行 embedding'),
    ('_maybe_pad_and_reduce', 'TP padding+归约'),
    ('tensor_model_parallel_all_reduce', 'TP all_reduce'),
)

def comment_for(name):
    for k, c in COMMENTS:
        if k in name:
            return c
    return ''



RE_LINE = re.compile(r'\((\d+)\)')


def strip_line(n):
    return RE_LINE.sub('', n)


MECH_LIFT = ('<built-in function', 'autograd/function.py', 'eval_frame.py',
             'torch/_compile.py(42): inner', 'runtime/jit.py', 'autotuner.py',
             'dynamic_func', 'ascend/driver.py', 'fla/utils.py(45): wrapper',
             'chunk.py(227): forward', 'triton/backends/', 'backends/compiler.py',
             'compilation/decorators.py', 'aot_compile.py', 'compilation/caching.py',
             ': execution_fn', 'piecewise_backend.py', 'compile_fx.py',
             'aot_autograd.py', 'runtime_wrapper', 'call_func_at_runtime_with_args',
             'runtime_wrappers.py', 'utils.py(106): g', '): kernel',
             '__getattr__', '__torch_function__', '_disabled_torch_function_impl',
             '_resolve_layer_name', 'get_forward_context', 'get_pcp_group',
             'next_power_of_2', ': cdiv', '_patched_environ_get', 'patch_getenv.py',
             '__contains__', '__getitem__', ': encode')


def lift_mech(nodes):
    out = []
    for n, s, e, ch in nodes:
        if any(p in n for p in MECH_LIFT):
            out.extend(lift_mech(ch))
        else:
            out.append((n, s, e, lift_mech(ch)))
    return out


GENERIC_FN = {'forward', '__call__', 'wrapper', '_fn', 'sample', 'run', 'apply',
              'build', 'forward_native', 'forward_impl', 'put', 'get', 'load',
              'inner', 'embed_input_ids'}


def bare_fn(n):
    ded = []
    for part in strip_line(n).split(' ⇢ '):
        if ': ' in part:
            f, fn = part.rsplit(': ', 1)
            fn = fn.strip()
            p = f'{f.rsplit("/", 1)[-1]}: {fn}' if fn in GENERIC_FN else fn
        else:
            p = part
        if not ded or ded[-1] != p:
            ded.append(p)
    return ' ⇢ '.join(ded)


def key_match(name):
    return any(p in name for p in KEY_PATTERNS)


def filter_keys(nodes):
    out = []
    for n, s, e, ch in nodes:
        fch = filter_keys(ch)
        if key_match(n) or fch:
            out.append((n, s, e, fch))
    return out


def merge_same_name(nodes):
    out = []
    for n, s, e, ch in nodes:
        ch = merge_same_name(ch)
        if len(ch) == 1 and bare_fn(ch[0][0]) == bare_fn(n):
            out.append((n, s, e, ch[0][3]))
        else:
            out.append((n, s, e, ch))
    return out


def tree_to_tuples(nodes):
    return [(n['n'], n['s'], n['e'], tree_to_tuples(n['ch']))
            for n in nodes if n['s'] is not None]


def agg_siblings(nodes):
    """同名兄弟聚合: 同一父节点下同名(bare_fn)实例合并为一行.

    显示 ×N + 合计 + 单次min~max, 子树保留最长实例的(时间线细节看火焰图).
    """
    prepared = [(n, s, e, ch, None) for n, s, e, ch in
                [(x[0], x[1], x[2], agg_siblings(x[3])) for x in nodes]]
    groups, order = {}, []
    for nd in prepared:
        k = bare_fn(nd[0])
        if k in groups:
            groups[k].append(nd)
        else:
            groups[k] = [nd]
            order.append(k)
    out = []
    for k in order:
        g = groups[k]
        if len(g) == 1:
            out.append(g[0])
            continue
        best = max(g, key=lambda x: x[2] - x[1])
        durs = [x[2] - x[1] for x in g]
        out.append((best[0], best[1], best[2], best[3],
                    {'count': len(g), 'total_ns': sum(durs),
                     'min_ns': min(durs), 'max_ns': max(durs)}))
    return out


def tuples_to_dicts(nodes):
    out = []
    for nd in nodes:
        d = {'n': nd[0], 's': nd[1], 'e': nd[2], 'ch': tuples_to_dicts(nd[3])}
        if len(nd) > 4 and nd[4]:
            d['agg'] = nd[4]
        out.append(d)
    return out


def key_flame_tree(db, a, b, tid, agg=True):
    tree = merge_same_name(filter_keys(lift_mech(tree_to_tuples(build_tree(db, a, b, tid)))))
    if agg:
        tree = agg_siblings(tree)
    return tuples_to_dicts(tree)


def compress_chain(name, s, e, ch):
    ch = [compress_chain(*c) for c in ch]
    while len(ch) == 1:
        cn, cs, ce, cch = ch[0]
        name = f'{name} ⇢ {cn}'
        e = ce
        ch = cch
    return (name, s, e, ch)


def prune_min(nodes, min_ns):
    out = []
    for n, s, e, ch in nodes:
        if e - s >= min_ns:
            out.append((n, s, e, prune_min(ch, min_ns)))
    return out


def flame_html(db, sid, s0, s1, w1, w2, w3):
    total = s1 - s0
    rowh = 22
    r = db.execute("SELECT value FROM meta WHERE key='has_stack'").fetchone()
    if r and r[0] == '0':
        return ('<div class="sect"><div class="note">无栈数据：python 火焰图不可用，'
                '流程语义可从 cpu_op 算子序列 + 设备内核重建。</div></div>')
    stages = [('P1', w1)]
    if w2:
        stages.append(('P2', w2))
    if w3:
        stages.append(('P3', w3))
    frames = []
    ov = []
    for key, w in stages:
        a, b = max(w[0], s0), min(w[1], s1)
        sdur = (b - a) / 1e6
        pct = sdur / (total / 1e6) * 100
        left = (a - s0) / total * 100
        width = (b - a) / total * 100
        ov.append(f'<div class="fseg ovseg" style="left:{left:.3f}%;width:{width:.3f}%;top:2px;height:18px;'
                  f'background:{STAGE_COLOR[key]}" data-s="{(a - s0) / 1e6:.6f}" data-e="{(b - s0) / 1e6:.6f}" '
                  f'title="{key} 阶段 · {sdur:.3f}ms · 占 step {pct:.1f}%（点击跳转到该阶段）">'
                  f'<span class="lbl">{key} {sdur:.1f}ms</span></div>')
        tid = main_tid(db, a, b)
        tree = tree_to_tuples(agg_us(build_tree(db, a, b, tid)))
        tree = lift_mech(tree)
        tree = filter_keys(tree)
        tree = merge_same_name(tree)

        def walk(nodes, depth):
            for n, s, e, ch in nodes:
                if s >= s1:
                    continue
                ee = min(e, s1)
                dur = (ee - s) / 1e6
                p = dur / (total / 1e6) * 100
                disp = bare_fn(n)
                frames.append([depth, (s - s0) / 1e6, (ee - s0) / 1e6, disp, disp,
                               round(dur, 6), round(p, 2), FLAME_SHADE[key][min(depth, 3)]])
                walk(ch, depth + 1)
        walk(tree, 0)
    maxd = max(f[0] for f in frames)
    h = (maxd + 1) * rowh + 6
    cid = f'flame-{sid}'
    fjs = json.dumps(frames, ensure_ascii=False, separators=(',', ':'))
    script = (f'<script>(function(){{'
              f'const F={fjs};'
              f'const H={rowh},TOTAL={total / 1e6:.6f};'
              f'const cont=document.getElementById("{cid}");'
              f'const ov=document.getElementById("ov-{cid}");'
              f'const vp=document.getElementById("vp-{cid}");'
              f'if(!cont)return;'
              f'let v0=0,v1=TOTAL;'
              f'const els=[];'
              f'for(const f of F){{'
              f'const d=document.createElement("div");'
              f'd.className="fseg";d.style.top=(f[0]*H+3)+"px";d.style.background=f[7];'
              f'd.title=f[4]+" · "+f[5].toFixed(3)+"ms · 占step "+f[6].toFixed(1)+"%";'
              f'const sp=document.createElement("span");sp.className="lbl";sp.textContent=f[3];'
              f'd.appendChild(sp);'
              f'd.addEventListener("click",()=>{{v0=f[1];v1=f[2];draw();}});'
              f'cont.appendChild(d);els.push([f,d]);}}'
              f'if(ov)ov.querySelectorAll(".ovseg").forEach(s=>{{'
              f's.addEventListener("click",()=>{{v0=+s.dataset.s;v1=+s.dataset.e;draw();}});}});'
              f'function draw(){{const w=v1-v0;'
              f'for(const [f,el] of els){{'
              f'const l=(f[1]-v0)/w*100,wd=(f[2]-f[1])/w*100;'
              f'if(l>=100||l+wd<=0||wd<=0){{el.style.display="none";continue;}}'
              f'const cl=Math.max(l,0),cw=Math.min(wd,100-cl);'
              f'el.style.display="block";el.style.left=cl+"%";el.style.width=cw+"%";'
              f'el.classList.toggle("tiny",cw<2.2);}}'
              f'if(vp){{vp.style.left=(v0/TOTAL*100)+"%";vp.style.width=((v1-v0)/TOTAL*100)+"%";}}}}'
              f'function reset(){{v0=0;v1=TOTAL;draw();}}'
              f'const btn=cont.parentElement.querySelector("h3 button");'
              f'if(btn)btn.addEventListener("click",reset);'
              f'draw();}})();</script>')
    return (f'<div class="stagebox" style="border-left-color:#8797A5">'
            f'<h3>阶段火焰图 · step{sid}（关键函数视图：点帧放大子树，点上层帧回退，点顶部概览条跳阶段）'
            f'<button>重置视图</button></h3>'
            f'<div class="flame ovstrip" id="ov-{cid}" style="height:24px">{"".join(ov)}'
            f'<div class="vp" id="vp-{cid}"></div></div>'
            f'<div class="flame" id="{cid}" style="height:{h}px"></div>'
            f'<div class="note">' + ' '.join(
                f'<span class="chip" style="background:{STAGE_COLOR[k]}"></span>{k} {((min(w[1], s1) - max(w[0], s0)) / total * 100):.1f}%'
                for k, w in stages)
            + f' · 帧数 {len(frames)}（仅校准关键函数，µs 级叶子已聚合）</div></div>'
            + script)


def section_html(db, key, name, a, b, uiz, parent_dur, parent_label):
    dur = (b - a) / 1e6
    pct = dur / parent_dur * 100 if parent_dur else 0
    badges, _ = four_badges(db, a, b)
    tid = main_tid(db, a, b)
    tree = key_flame_tree(db, a, b, tid)
    sem = KNOWLEDGE.get(key, '')
    wc = WindowClasses(db, a, b)
    body = render_tree(tree, uiz, wc)
    tree_html = f'<div class="toolbar"><button onclick="xall(this.closest(\'.sect\'),true)">展开全部</button><button onclick="xall(this.closest(\'.sect\'),false)">收起全部</button></div><div class="treewrap">{body}</div>' if body else '<div class="note">(窗口内无 python 帧)</div>'
    return (f'<div class="sect" id="{esc(key)}">'
            f'<div class="title-line">{esc(key)} {esc(name)} · UI {uims(a,uiz):.3f}~{uims(b,uiz):.3f}ms · {dur:.3f}ms · 占{esc(parent_label)} {pct:.1f}%</div>'
            f'<div class="title-line">设备状态: {badges}</div>'
            + (f'<div class="sem">{esc(sem)}</div>' if sem else '')
            + tree_html + '</div>')


def p2_html(db, a, b, uiz, step_dur, fname, s0=None, s1=None):
    dur = (b - a) / 1e6
    badges, _ = four_badges(db, a, b)
    tid = main_tid(db, a, b)
    spine = key_flame_tree(db, a, b, tid)
    wc = WindowClasses(db, a, b)
    mode = p2_mode(db, a, b)
    mode_label = {'graph_replay': 'NPU 整图回放', 'fx_eager': 'FX 图 eager 逐算子执行',
                  'eager': 'eager'}[mode]
    counts = op_counts(db, s0 if s0 is not None else a, s1 if s1 is not None else b)
    cnt_rows = ''.join(
        f'<tr><td>{esc(short(n))}</td><td>{c}</td><td>{d / 1e6:.3f}</td>'
        f'<td>{d / c / 1e3:.1f}</td></tr>'
        for n, c, d in counts)
    gdn = rep_instance(db, 'qwen_gdn_attention_core', a, b)
    attn = rep_instance(db, 'unified_attention_with_output', a, b)

    def rep_html(inst, label):
        if not inst:
            return ''
        name, s, e, tree = inst
        d = (e - s) / 1e6
        return (f'<div class="sect"><div class="title-line">代表实例: {esc(bare_fn(name))} · {d:.3f}ms</div>'
                + render_tree(tree, uiz, WindowClasses(db, s, e)) + '</div>')

    return (f'<div class="sect"><div class="title-line">P2 模型前向 · UI {uims(a,uiz):.3f}~{uims(b,uiz):.3f}ms · {dur:.3f}ms · 占step {dur / step_dur * 100:.1f}% · 锚点: {esc(fname)} · 执行模式: {mode_label}</div>'
            f'<div class="title-line">设备状态: {badges}</div>'
            f'<div class="sem">{esc(P2_MODE_TEXT[mode])}</div>'
            f'<div class="toolbar"><button onclick="xall(this.closest(\'.sect\'),true)">展开全部</button><button onclick="xall(this.closest(\'.sect\'),false)">收起全部</button></div>'
            + render_tree(spine, uiz, wc)
            + (f'<h3>设备算子计数（整个 step 窗口的 Ascend Hardware 内核，印证模型结构；'
               f'已排除 hcom/通信协调内核。整图回放步设备异步执行，按 step 而非 P2 host 窗口统计）</h3>'
               f'<table><tr><th>内核</th><th>次数</th><th>合计ms</th><th>单次µs</th></tr>{cnt_rows}</table>' if cnt_rows else '')
            + model_structure_html(db, a, b, s0, s1, mode)
            + rep_html(gdn, 'GDN') + rep_html(attn, 'ATTN')
            + '</div>')


def batch_comp(db, s0, s1):
    """批成分取证: 混合批 / 纯 decode / 纯 prefill.

    update_req_spec_token_ids 每请求一次(源码 gpu_model_runner: 存量请求循环与
    新请求循环各调一次) = 批内请求数; 慢路径(>=6us, 写 token_ids_cpu) = 带 draft
    的 decode 请求(无 spec decode 模型全程快路径, 恒 0); add_request = 新 prefill
    请求(首 chunk); decode = 批内 - 新增(存量请求, 带不带 draft 都是 decode);
    split_decodes_and_prefills 带 cpu_op 子算子 = 批内确有 prefill(纯 decode 走
    早退 ~20us 无张量算子, 含 prefill 130us+ 且有子算子, 8B/27B 实测);
    _execute_mm_encoder ms 级 = 有新图像请求(空调用 ~3us 早退).
    """
    py = pids(db)['py']
    ur = db.execute(
        f"SELECT dur_ns FROM events WHERE pid={py} AND cat='python_function' "
        "AND name LIKE '%update_req_spec_token_ids%' AND dur_ns>0 "
        'AND ts_ns>=? AND ts_ns+dur_ns<=?', (s0, s1)).fetchall()
    has_spec = db.execute("SELECT 1 FROM events WHERE name LIKE '%propose_draft%' LIMIT 1").fetchone()
    total = len(ur)
    slow = sum(1 for (d,) in ur if d >= 6000) if has_spec else 0
    new = db.execute(
        f"SELECT COUNT(*) FROM events WHERE pid={py} AND cat='python_function' "
        "AND name LIKE '%gpu_input_batch.py%: add_request%' AND dur_ns>0 "
        'AND ts_ns>=? AND ts_ns+dur_ns<=?', (s0, s1)).fetchone()[0]
    has_prefill = False
    for ts, d in db.execute(
            f"SELECT ts_ns, dur_ns FROM events WHERE pid={py} AND cat='python_function' "
            "AND name LIKE '%: split_decodes_and_prefills' AND dur_ns>0 "
            'AND ts_ns>=? AND ts_ns+dur_ns<=?', (s0, s1)):
        n = db.execute(
            f"SELECT COUNT(*) FROM events WHERE pid={py} AND cat='cpu_op' "
            'AND ts_ns>=? AND ts_ns+dur_ns<=?', (ts, ts + d)).fetchone()[0]
        if n:
            has_prefill = True
            break
    mm = db.execute(
        "SELECT MAX(dur_ns) FROM events WHERE name LIKE '%_execute_mm_encoder%' "
        'AND dur_ns>0 AND ts_ns>=? AND ts_ns+dur_ns<=?', (s0, s1)).fetchone()[0]
    mm_ms = (mm or 0) / 1e6
    decode = max(total - new, 0)
    if total == 0 and new == 0:
        kind = '（无批请求信号）'
    elif has_prefill and new == 0:
        kind = '含 prefill 续算（chunked 续段，decode 列含续算请求）'
    elif new > 0 and total > new:
        kind = '混合批（decode+prefill）'
    elif new > 0:
        kind = '纯 prefill'
    else:
        kind = '纯 decode'
    return total, decode, slow, new, mm_ms, kind


def build(db, out, only_step):
    uiz = ui(db)
    meta = {k: v for k, v in db.execute('SELECT key, value FROM meta')}
    steps = db.execute('SELECT id, start_ns, end_ns FROM steps ORDER BY id').fetchall()
    if only_step is not None:
        steps = [s for s in steps if s[0] == only_step]
    pages, nav = [], []
    home_rows = []
    bc_rows = []
    fa_rows = []
    step_pages = []
    T = dict(dur=0.0, p1=0.0, p2=0.0, p3=0.0, p2c=0.0, p2f=0.0, p3f=0.0, sync=0.0)
    for sid, s0, s1 in steps:
        w1, w2, w3, fname = stage_split(db, s0, s1)
        mode = p2_mode(db, w2[0], w2[1]) if w2 else None
        sync = db.execute(
            f"SELECT dur_ns FROM events WHERE pid={pids(db)['py']} AND cat='python_function' "
            "AND name LIKE '%npu/utils.py(62): synchronize' AND dur_ns>0 "
            'AND ts_ns+dur_ns>? AND ts_ns<? ORDER BY dur_ns DESC LIMIT 1', (s0, s1)).fetchone()
        if sync is None:
            sync = db.execute(
                f"SELECT dur_ns FROM events WHERE pid={pids(db)['cann']} AND name LIKE '%aclrtSynchronizeDevice%' "
                'AND dur_ns>0 AND ts_ns+dur_ns>? AND ts_ns<? ORDER BY dur_ns DESC LIMIT 1', (s0, s1)).fetchone()
        sync_ms = sync[0] / 1e6 if sync else 0.0
        step_dur = (s1 - s0) / 1e6
        t1, _ = cls_stats(db, *w1)
        p1f = t1['free'] / max(w1[1] - w1[0], 1) * 100
        if w2 and w3:
            t2, _ = cls_stats(db, *w2)
            t3, _ = cls_stats(db, *w3)
            p2c = t2['computing'] / (w2[1] - w2[0]) * 100
            p2f = t2['free'] / (w2[1] - w2[0]) * 100
            p3f = t3['free'] / (w3[1] - w3[0]) * 100
        else:
            p2c = p2f = p3f = None
        verdict = step_verdict(p3f if p3f is not None else 0, sync_ms, p2f, mode)
        comm = comm_line(db, sid)
        bc = batch_comp(db, s0, s1)
        bc_rows.append(
            f'<tr><td>step{sid}</td><td>{bc[0]}</td><td>{bc[1]}</td><td>{bc[2]}</td><td>{bc[3]}</td>'
            f'<td>{"✓ %.0fms" % bc[4] if bc[4] >= 1 else "—"}</td><td>{bc[5]}</td></tr>')
        if w2 and w3:
            p2_cell = f'{(w2[1]-w2[0])/1e6:.1f}ms (comp {p2c:.0f}%/free {p2f:.0f}%)'
            if mode == 'graph_replay':
                p2_cell += ' · 整图回放'
            elif mode == 'fx_eager':
                p2_cell += ' · FX eager'
        fa_rows.append((sid, *free_attr(db, s0, s1)))
        home_rows.append(
            f'<tr><td>step{sid}</td><td>{step_dur:.1f}ms</td>'
            + (f'<td>{(w1[1]-w1[0])/1e6:.1f}ms (free {p1f:.0f}%)</td>'
               f'<td>{p2_cell}</td>'
               f'<td>{(w3[1]-w3[0])/1e6:.1f}ms (free {p3f:.0f}%)</td>' if w2 and w3 else
               f'<td colspan=3>— 无法切分 —</td>')
            + f'<td>{sync_ms:.2f}ms</td></tr>')
        T['dur'] += step_dur; T['p1'] += (w1[1] - w1[0]) / 1e6
        T['sync'] += sync_ms
        if w2 and w3:
            T['p2'] += (w2[1] - w2[0]) / 1e6; T['p3'] += (w3[1] - w3[0]) / 1e6
            t2s, _ = cls_stats(db, *w2); t3s, _ = cls_stats(db, *w3)
            T['p2c'] += t2s['computing'] / 1e6; T['p2f'] += t2s['free'] / 1e6; T['p3f'] += t3s['free'] / 1e6
        pages.append(sid)
        nav.append(f'<button id="btn-step{sid}" onclick="show(\'step{sid}\')">step{sid}</button>')
        parts = [f'<h1>step{sid} 详细分析 <span class="note">(窗口 {uims(s0,uiz):.3f}~{uims(s1,uiz):.3f}ms · {step_dur:.3f}ms · 判定: {esc(verdict)})</span></h1>']
        parts.append(stage_box_html(db, sid, s0, s1, w1, w2, w3, step_dur))
        parts.append(flame_html(db, sid, s0, s1, w1, w2, w3))
        secs = p1_sections(db, s0, w1[1], w2[0] if w2 else s1)
        parts.append('<h2 class="s-P1">P1 推理前准备</h2>')
        for key, name, a, b in secs:
            parts.append(section_html(db, key, name, a, b, uiz, (w1[1] - w1[0]) / 1e6, 'P1'))
        if w2:
            parts.append('<h2 class="s-P2">P2 模型前向</h2>')
            parts.append(p2_html(db, w2[0], w2[1], uiz, step_dur, fname, s0, s1))
        if w3:
            parts.append('<h2 class="s-P3">P3 采样与收尾</h2>')
            for key, name, a, b in p3_sections(db, w3[0], s1):
                plabel = 'P3'
                parts.append(section_html(db, key, name, a, b, uiz, (w3[1] - w3[0]) / 1e6, plabel))
        if comm:
            parts.append(f'<div class="kv">通信: {esc(comm)}</div>')
        step_pages.append(f'<div class="page" id="step{sid}">{"".join(parts)}</div>')
    total_row = (f'<tr><td>合计</td><td>{T["dur"]:.1f}ms</td>'
                 f'<td>{T["p1"]:.1f}ms</td><td>{T["p2"]:.1f}ms (comp {T["p2c"]/T["p2"]*100 if T["p2"] else 0:.0f}%/free {T["p2f"]/T["p2"]*100 if T["p2"] else 0:.0f}%)</td>'
                 f'<td>{T["p3"]:.1f}ms (free {T["p3f"]/T["p3"]*100 if T["p3"] else 0:.0f}%)</td>'
                 f'<td>{T["sync"]:.2f}ms</td></tr>')
    fa_table_rows = []
    for sid, tot, top in fa_rows:
        if not top:
            continue
        for i, (n, raw, v) in enumerate(top, 1):
            cmt = comment_for(raw)
            first = (f'<td rowspan="{len(top)}">step{sid}</td>'
                     f'<td rowspan="{len(top)}">{tot:.1f}ms</td>') if i == 1 else ''
            fa_table_rows.append(
                f'<tr>{first}<td>{i}</td><td>{esc(n)}</td><td>{v:.1f}ms</td>'
                f'<td>{v / tot * 100 if tot else 0:.1f}%</td>'
                f'<td>{esc(cmt)}</td></tr>')
    if steps:
        oa, ob = steps[0][1], steps[-1][2]
        otot, osz = cls_stats(db, oa, ob)
        ofree = otot['free'] / osz * 100
        ocomp = otot['computing'] / osz * 100
        ocomm = otot['communication'] / osz * 100
        ocno = otot['comm_not_overlapped'] / osz * 100
        if ofree >= 40:
            judge = '设备大量空闲，整体偏 host bound（供数不足）'
        elif ofree >= 20:
            judge = '设备中等空闲，host 供数与设备执行大体交替'
        else:
            judge = '设备利用率较高，瓶颈更可能在设备侧或通信'
        hero = (f'<div class="hero">'
                f'<div class="htitle">整份 profiling · {len(steps)} 步 · {(ob - oa) / 1e6:.1f}ms'
                f' —— <span class="judge">{judge}</span></div>'
                f'<div class="hnums">NPU <b>Free {ofree:.1f}%</b><span class="sep">·</span>'
                f'Computing {ocomp:.1f}%<span class="sep">·</span>'
                f'Communication {ocomm:.1f}%（未重叠 {ocno:.1f}%）</div>'
                f'<div>{mini_bar(otot, osz, big=True)}</div>'
                + BAR_LEGEND + '</div>')
    else:
        hero = '<div class="hero"><div class="htitle">无 step 数据</div></div>'
    home = f'''<div class="page on" id="home">
<h1>Profiling 汇总</h1>
{hero}
<div class="kv">trace: {esc(meta.get("trace", "?"))}</div>
<div class="kv">分类源: {esc(meta.get("classification_source", "?"))} · 事件数 {meta.get("n_events", "?")} · steps: {len(steps)}</div>
<table><tr><th>step</th><th>步长</th><th>推理前准备</th><th>推理</th><th>推理后</th><th>步尾同步</th></tr>
{''.join(home_rows)}{total_row}</table>
<div class="note">口径：Free = 窗口 − (Computing ∪ Communication)，OA 未覆盖间隙计入 Free；Computing + 未重叠 + Free = 100%（Communication 与 Computing 可重叠）。步尾同步 = 每步末尾的设备级同步耗时：长 = 设备真排水（积压）；瞬时返回 = 设备早已空闲（host bound）。</div>
<h3>批成分（vLLM v1 continuous batching：一个 step 的批可同时含 prefill 与 decode，非二选一）</h3>
<table><tr><th>step</th><th>批内请求</th><th>decode</th><th>其中带draft</th><th>新增 prefill</th><th>视觉编码</th><th>批类型</th></tr>
{''.join(bc_rows)}</table>
<div class="note">取证：批内请求 = update_req_spec_token_ids 调用数（每请求一次，存量+新增各循环一次）；decode = 批内 − 新增（存量请求，带不带 draft 都是 decode）；其中带draft = 慢路径（≥6µs，写 token_ids_cpu）计数，无 spec decode 模型恒 0；新增 prefill = add_request（gpu_input_batch，新请求=首段 prefill；add_request=0 属正常，勿判为无调度请求）；prefill 存在性 = split_decodes_and_prefills 是否带张量子算子（纯 decode 早退无算子，批类型"含 prefill 续算"即由此判出，此时 decode 列含 chunked 续段请求）；_execute_mm_encoder ms 级 = 有新图像请求（~3µs = 空调用）。混合批（含 prefill）→ 非均匀 → FX eager；纯 decode（均匀）→ 整图回放。</div>
<h3>NPU Free 时间去向（按 free 区间所在最内层校准关键函数归因，每步 Top5）</h3>
<table><tr><th>step</th><th>Free</th><th>#</th><th>去向</th><th>时长</th><th>占 Free</th><th>说明</th></tr>
{''.join(fa_table_rows)}</table>
</div>'''
    doc = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>Ascend vLLM Profiling 报告</title><style>{CSS}</style></head><body>
<nav><button id="btn-home" class="on" onclick="show('home')">首页 · 汇总</button>{''.join(nav)}</nav>
{home}{''.join(step_pages)}
<script>{JS}</script></body></html>'''
    open(out, 'w').write(doc)
    print(f'HTML 报告 → {out}  ({len(doc) / 1e6:.2f}MB, {len(pages)} steps)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('--out', default='report.html')
    ap.add_argument('--step', type=int, default=None)
    args = ap.parse_args()
    db = sqlite3.connect(args.db)
    build(db, args.out, args.step)


if __name__ == '__main__':
    main()
