/* session_view.js：sessions.html 的全部逻辑。数据在 <script id="data"> 里（由 session_view.py 生成）。
   路由：#overview（总览）、#s/<会话ID>（单会话）、#s/<会话ID>/r/<请求序号>（单会话并展开某个请求）。
    版式约定（与 report.py 相同）：每张图 = 标题 → 图注（横轴、纵轴、标注，图上方）→ 图 →「结论」框（由数据算出，关键数字加粗）；
    柱子同时标注数量与比例；表格各列在「列说明」中给出定义；悬停信息一律使用卡片。
    措辞约定：书面陈述句；术语全篇一致（需重新处理的 token / 新增内容 / 模板改写 / 额外未命中 / 公共前缀 / 旁路请求）；
    不使用口语、设问、导览语气与不确定限定词。 */
(function () {
  'use strict';
  const D = JSON.parse(document.getElementById('data').textContent);
  const app = document.getElementById('app');
  // 还原 session_view.py pack_for_html 去重后的消息行与片段。按需还原：首次读取 r.msgs / r.segs 时展开
  const ST_CODE = ['reused', 'new', 'stripped', 'shifted'];
  const lazy = (o, name, make) => Object.defineProperty(o, name, {configurable: true, enumerable: true, get() { const v = make(); Object.defineProperty(o, name, {value: v, enumerable: true}); return v; }});
  for (const s of D.sessions) for (const r of s.reqs) {
    if (r.mp) lazy(r, 'msgs', () => { const out = []; for (let i = 0; i < r.mp.length; i += 2) { const m = Object.assign({i: i / 2}, s.mtab[r.mp[i]]); if (r.mp[i + 1] >= 0) m.status = ST_CODE[r.mp[i + 1]]; out.push(m); } return out; });
    if (r.sp) lazy(r, 'segs', () => { let c = 0, t = 0; return r.sp.map(j => { const [k, m, tool, b, cl, tl] = s.stab[j], sg = {k, m, tool, b, c0: c, c1: c + cl, t0: t, t1: t + tl}; c += cl; t += tl; return sg; }); });
  }
  const KINDS = D.kinds, KL = D.kind_label;
  // 单位：有任一请求带 token 级归属（分词器偏移）时按 token；否则（未提供 --tokenizer，或渲染全部失败）来源构成、消息与工具大小按字符
  const ATTR = D.fleet.unit !== 'char', UNIT = ATTR ? 'token' : '字符';
  const sumOf = o => Object.values(o || {}).reduce((a, b) => a + (b || 0), 0);
  const ru = r => r.tok_attr ? 'token' : '字符';  // 单个请求的构成单位
  const uw = (after, u = UNIT) => u + (u === 'token' ? ' ' : '') + after;  // 「token 数」/「字符数」这类单位 + 后缀的写法
  const bkOf = r => r.tok_attr ? (r.by_kind || {}) : (ATTR ? {} : (r.by_kind_c || {}));  // 单个请求的来源构成（按 ru(r) 计）
  const bkTot = r => r.tok_attr ? (r.tok || 0) : sumOf(bkOf(r));
  const msz = (m, r) => r.tok_attr ? m.tok : m.chars;  // 单条消息的大小（按 ru(r) 计）
  const mparts = (m, r) => r.tok_attr ? [m.content, m.reasoning, m.calls, m.markup] : [m.content_c || 0, m.reasoning_c || 0, m.calls_c || 0, m.markup_c || 0];
  const KC = k => `var(--k-${k.replace('.', '_')})`;
  // 模板标记片段去掉控制符（<…> 标签、MiniMax 的 ]~b] [e~[、GLM 对图片插入的 <reminder>…</reminder>、空文本部分渲染出的壳）和空白后剩下的文字
  const residue = t => t.replace(/<reminder>[\s\S]*?<\/reminder>|<arg_key>[^<>]{0,80}<\/arg_key>|\{'type': 'text', 'text': ''\}|<[^<>]{1,80}>|\]~!?b[\[\]]|\[e~\[|\s+/g, '');
  const LIGHT_FILL = new Set(['assistant.tool_call', 'assistant.reasoning', 'tool.result', 'markup']);  // 底色较浅的来源：亮色模式下条内文字用墨色
  const E = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const N = (v, d = 0) => v == null ? '–' : (typeof v === 'number' ? v.toLocaleString('en-US', {maximumFractionDigits: d}) : v);
  const P = v => v == null || !isFinite(v) ? '–' : (100 * v).toFixed(1) + '%';
  const K = v => v == null ? '–' : (v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 10000 ? (v / 1000).toFixed(0) + 'k' : v >= 1000 ? (v / 1000).toFixed(1) + 'k' : String(Math.round(v)));
  const B = v => `<b>${E(v)}</b>`;
  const KIND_CN = {first: '线程首个请求', append_tool: '追加工具结果', append_user: '追加用户消息', append_assistant: '追加 assistant 消息',
    repeat: '与上一请求相同', rewrite: '历史消息被改写', compact_like: '历史压缩（compact）', no_session: '无会话标识', no_messages: '无 messages'};
  const KIND_DEF = {first: '同一线程内无更早的请求，prompt 全部计为新增', append_tool: '上一请求的消息全部保留，末尾新增 assistant 的工具调用及其工具结果',
    append_user: '上一请求的消息全部保留，末尾新增一条 user 消息', append_assistant: '上一请求的消息全部保留，末尾为 assistant 消息',
    repeat: '消息与工具定义均与上一请求相同（重试）', rewrite: '与上一请求的公共消息前缀之后，存在被修改或删除的历史消息', no_messages: 'completions 接口的请求，没有 messages，只比较 token 序列',
    compact_like: '客户端将历史压缩为摘要：压缩请求（旁路请求）的输出正文出现在本请求的消息中；或消息条数降至上一请求的一半以下且公共消息前缀不超过 2 条'};
  const CAUSE_CN = {append: '追加', template_stripped_reasoning: '模板删除历史思考', template_rewrite: '模板改写历史', message_changed: '消息内容变更',
    tools_changed: '工具定义变更', compact_like: '历史压缩', repeat: '与上一请求相同', rewrite: '历史消息被改写'};
  const CAUSE_DEF = {append: '上一请求的全部 token 构成本请求的前缀，本请求仅在末尾新增 token',
    template_stripped_reasoning: '客户端发送的消息未变；聊天模板将某条历史 assistant 消息的 <think>…</think> 渲染为空的 <think></think>，自该位置起 token 序列与上一请求不同',
    template_rewrite: '客户端发送的消息未变；同一条消息的渲染结果与上一请求不同（模板的输出与消息所处位置有关）', message_changed: '某条历史消息的内容被客户端修改', tools_changed: '请求中的工具定义发生变更，prompt 自开头起与上一请求不同',
    compact_like: '客户端将历史压缩为摘要，本请求的消息以摘要替代此前的历史', repeat: '与上一请求相同', rewrite: '与上一请求的公共消息前缀之后，存在被修改或删除的历史消息'};
  const STATUS_CN = {reused: '沿用', new: '新增', stripped: '思考被模板删除', shifted: '受改写影响'};
  const STATUS_DEF = {reused: '与上一请求同一位置的消息内容相同，token 序列相同，位于公共前缀内', new: '本请求新增的消息，或内容发生变更的消息',
    stripped: '消息内容未变；模板未将其思考（reasoning）渲染进 prompt，该消息的 token 序列发生变化',
    shifted: '消息内容与渲染结果均未变；其之前存在被改写的消息，该消息不在与上一请求的公共前缀内，前缀缓存与增量分词均无法复用'};
  const ROLE_C = {system: KC('system'), developer: KC('system'), user: KC('user'), assistant: KC('assistant.content'), tool: KC('tool.result')};
  const FINISH_CN = {stop: '正常结束', tool_calls: '发起工具调用', length: '达到 max_tokens，输出被截断', ok: '正常结束'};

  // ---------- 悬停卡片：全页一个元素，事件委托。data-tip = 纯文本；data-tiph = HTML；data-tipf = "名字:参数"（悬停时才生成 HTML） ----------
  const tip = document.createElement('div'); tip.className = 'tooltip'; document.body.appendChild(tip);
  let tipEl = null, tipFns = {};
  function placeTip(e) {
    if (tip.style.display !== 'block') return;
    const pad = 16, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > window.innerWidth - 8) x = Math.max(8, e.clientX - pad - w);
    if (y + h > window.innerHeight - 8) y = Math.max(8, window.innerHeight - 8 - h);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function showTipHtml(html, e, rich = true) { tip.classList.toggle('rich', rich); tip.innerHTML = html; tip.style.display = 'block'; placeTip(e); }
  function hideTip() { tip.style.display = 'none'; }
  document.addEventListener('mouseover', e => {
    const el = e.target.closest ? e.target.closest('[data-tip],[data-tiph],[data-tipf]') : null;
    if (el === tipEl) return;
    tipEl = el;
    if (!el) { if (!e.target.closest || !e.target.closest('.ladwrap')) hideTip(); return; }
    if (el.dataset.tipf != null) { const [f, a] = el.dataset.tipf.split(':'); if (tipFns[f]) showTipHtml(tipFns[f](a), e); }
    else if (el.dataset.tiph != null) showTipHtml(el.dataset.tiph, e);
    else if (el.dataset.tip) { tip.classList.remove('rich'); tip.textContent = el.dataset.tip; tip.style.display = 'block'; placeTip(e); }
    else hideTip();
  });
  document.addEventListener('mousemove', e => { if (tipEl) placeTip(e); });
  window.addEventListener('scroll', () => { tipEl = null; hideTip(); }, {passive: true});

  // ---------- 小工具 ----------
  const barPath = (x, y, w, h, r = 4) => { r = Math.max(0, Math.min(r, w / 2, h)); return `M${x.toFixed(1)},${(y + h).toFixed(1)} V${(y + r).toFixed(1)} Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)} H${(x + w - r).toFixed(1)} Q${(x + w).toFixed(1)},${y.toFixed(1)} ${(x + w).toFixed(1)},${(y + r).toFixed(1)} V${(y + h).toFixed(1)} Z`; };
  const hbarPath = (x, y, w, h, r = 4) => { r = Math.min(r, h / 2, w); return `M${x.toFixed(1)},${y.toFixed(1)} H${(x + w - r).toFixed(1)} Q${(x + w).toFixed(1)},${y.toFixed(1)} ${(x + w).toFixed(1)},${(y + r).toFixed(1)} V${(y + h - r).toFixed(1)} Q${(x + w).toFixed(1)},${(y + h).toFixed(1)} ${(x + w - r).toFixed(1)},${(y + h).toFixed(1)} H${x.toFixed(1)} Z`; };
  function yticks(vmax, n = 4) {
    if (vmax <= 0) return [0];
    const raw = vmax / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(x => x * mag).find(s => s >= raw);
    const out = []; for (let v = 0; v <= vmax + 1e-9; v += step) out.push(v); if (out[out.length - 1] < vmax) out.push(out[out.length - 1] + step); return out;
  }
  const niceStep = v => [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000].find(s => s >= v) || 2000;
  // o: {read, finding, warn}，三者都是 HTML
  const fig = (title, body, o = {}) => `<figure><h3>${E(title)}</h3>${o.read ? `<p class="read">${o.read}</p>` : ''}${body}${o.finding ? `<div class="finding"><span class="lbl">结论</span>${o.finding}</div>` : ''}${o.warn ? `<div class="warn"><span class="lbl">注意</span>${o.warn}</div>` : ''}</figure>`;
  const defs = items => '<dl class="gloss">' + items.map(([k, v]) => `<dt>${E(k)}</dt><dd>${v}</dd>`).join('') + '</dl>';
  const legend = ks => '<div class="legend">' + ks.map(k => `<span><i style="background:${KC(k)}"></i>${KL[k]}</span>`).join('') + '</div>';
  const kpi = (v, u, t, d) => `<div class="kpi"><b>${v}${u ? `<small>${E(u)}</small>` : ''}</b><span class="t">${E(t)}</span>${d ? `<span class="d">${E(d)}</span>` : ''}</div>`;
  const th = (name, tipText, cls, sub) => `<th${cls ? ` class="${cls}"` : ''}${tipText ? ` data-tip="${E(tipText)}"` : ''}>${E(name)}${sub ? `<small>${E(sub)}</small>` : ''}</th>`;
  const vis = (t, n) => E(String(t).slice(0, n).replace(/\n/g, '⏎')) + (String(t).length > n ? '…' : '');
  const pctOf = (a, b) => b ? P(a / b) : '–';

  // 一个请求中「需重新处理的 token」的分解。nw：与同线程上一请求的公共 token 前缀之后的 token 数；lost：其中所属消息内容未变、由模板改写造成的部分；
  // fresh：新增内容；unc：vLLM 未命中前缀缓存的 token 数（prompt_tokens − cached_tokens）；excess：unc 超出 nw 的部分（额外未命中）。
  function recomputed(r) {
    const tok = r.tok || r.pt || 0;
    const nw = r.tok_new != null ? r.tok_new : (r.kind === 'first' && tok ? tok : null);
    const unc = r.pt != null ? r.pt - (r.ct || 0) : null;
    const lost = Math.min(r.template_lost || 0, nw || 0);
    return {tok, nw, lost, fresh: nw != null ? Math.max(0, nw - lost) : null, unc, excess: nw != null && unc != null ? Math.max(0, unc - nw) : null,
      under: nw != null && unc != null && unc < nw - 16};
  }
  const prevInThread = (reqs, r) => { for (let i = r.i - 1; i >= 0; i--) if (reqs[i].thread === r.thread) return reqs[i]; return null; };

  // 没有 token 级归属或有渲染失败时，页首的提示
  function attrNotice() {
    const F = D.fleet, errs = Object.entries(F.errors || {});
    let h = '';
    if (!ATTR) h += `<div class="warn"><span class="lbl">注意</span><b>本报告没有 token 级归属。</b>生成报告时${D.meta.tokenizer ? '提供了 --tokenizer，但全部含全文请求的模板渲染或分词失败（失败原因见下）' : '未提供 --tokenizer'}，无法将 token 逐段归属到消息与来源。本页的来源构成、消息大小、工具大小按<b>字符</b>统计，单位均已标明；prompt 的 token 总数取自 vLLM 返回的 prompt_tokens 或 golden。重新运行 <code>session_view.py</code> 并加 <code>--tokenizer &lt;模型目录&gt;</code> 可得到 token 级结果。</div>`;
    if (errs.length) h += `<div class="warn"><span class="lbl">注意</span>${N(errs.reduce((a, [, v]) => a + v, 0))} 个请求的模板渲染或分词失败，这些请求没有来源构成与消息级数字：${errs.map(([k, v]) => `<code>${E(k)}</code>（${N(v)} 个）`).join('；')}。</div>`;
    return h;
  }

  // ---------- 悬停卡片内容 ----------
  function reqTipHtml(r, reqs) {
    const bk = bkOf(r), tot = r.tok || 0, btot = bkTot(r) || 1, u = ru(r), x = recomputed(r);
    const ks = KINDS.filter(k => bk[k]).sort((a, b) => bk[b] - bk[a]), mx = Math.max(...ks.map(k => bk[k]), 1);
    let h = `<div class="th">请求 ${r.i}<small>${E(r.time.slice(11))} · ${E(KIND_CN[r.kind] || r.kind)}${r.thread !== 'main' ? ' · 旁路请求 ' + E(r.thread) : ''}</small></div>`;
    h += `<div class="big">${N(tot)}<small>token · ${N(r.n_msgs)} 条消息</small></div><div class="facts">`;
    if (x.nw != null) h += `<span>需重新处理</span><b>${N(x.nw)}</b><span>占 ${pctOf(x.nw, tot)}${x.lost ? `，其中模板改写 ${N(x.lost)}` : ''}</span>`;
    if (r.pt != null) h += `<span>前缀缓存命中</span><b>${N(r.ct || 0)}</b><span>占 ${pctOf(r.ct || 0, r.pt)}</span>`;
    h += '</div>';
    if (ks.length) h += `<table>${u === '字符' ? '<tr><td colspan="4" class="muted">来源构成按字符计（无 token 级归属）</td></tr>' : ''}` + ks.map(k => `<tr><td><i class="sw" style="background:${KC(k)}"></i>${KL[k]}</td><td class="n">${N(bk[k])} ${u}</td><td class="n"><b>${pctOf(bk[k], btot)}</b></td><td><span class="mb"><i style="width:${(100 * bk[k] / mx).toFixed(1)}%;background:${KC(k)}"></i></span></td></tr>`).join('') + '</table>';
    else h += '<div class="hint">该请求无来源归属（不含全文，或渲染失败）；仅有 vLLM 返回的 token 总数</div>';
    const marks = [];
    if (r.kind === 'compact_like') marks.push('compact：客户端将历史压缩为摘要');
    if (r.template_lost) marks.push(`模板改写历史：${N(r.template_lost)} 个 token 所属消息内容未变，需重新处理`);
    if (marks.length) h += `<div class="hint" style="color:var(--fg2)">${marks.map(E).join('；')}</div>`;
    return h + '<div class="hint">点击展开该请求</div>';
  }
  function deltaTipHtml(r, reqs) {
    const x = recomputed(r), pv = prevInThread(reqs, r);
    let h = `<div class="th">请求 ${r.i}<small>${E(KIND_CN[r.kind] || r.kind)}${r.cause ? ' · ' + E(CAUSE_CN[r.cause] || r.cause) : ''}</small></div>`;
    h += `<div class="big">${N(x.unc != null ? Math.max(x.unc, x.nw || 0) : x.nw)}<small>token 需重新处理；prompt 共 ${N(x.tok)} token</small></div><table>`;
    const row = (c, name, v, note) => `<tr><td>${c ? `<i class="sw" style="background:${c}"></i>` : ''}${name}</td><td class="n"><b>${N(v)}</b></td><td class="n">${pctOf(v, x.tok)}</td><td class="muted">${note || ''}</td></tr>`;
    if (x.nw != null) {
      h += row('var(--accent)', '新增内容', x.fresh, r.kind === 'first' ? '线程首个请求，prompt 全部计为新增' : '本请求新增的消息');
      if (x.lost) h += row('var(--stripped)', '模板改写', x.lost, '消息内容未变，渲染结果变化');
      if (x.excess) h += row('var(--excess)', '额外未命中', x.excess, x.excess <= 16 ? '不超过一个缓存块（16 token）' : '与上一请求相同，前缀缓存未命中');
    } else if (x.unc != null) h += row('var(--reused)', '前缀缓存未命中', x.unc, '无分词结果，无法分解');
    h += '</table><div class="facts" style="margin-top:6px">';
    if (r.tok_prefix != null && pv) h += `<span>与请求 ${pv.i} 的公共前缀</span><b>${N(r.tok_prefix)}</b><span>token</span>`;
    if (r.pt != null) h += `<span>cached_tokens</span><b>${N(r.ct || 0)}</b><span>未命中 ${N(x.unc)} token</span>`;
    return h + '</div><div class="hint">点击展开该请求</div>';
  }

  // ---------- 图 ----------
  function stackedArea(reqs, W = 1120) {
    const H = 290, L = 54, R = 12, T = 22, B = 42;
    const pts = reqs.filter(r => r.tok);
    if (!pts.length) return '<p class="def">无 token 数据</p>';
    const xmax = Math.max(1, reqs.length - 1);
    const ticks = yticks(Math.max(...pts.map(r => r.tok), 1)), ymax = ticks[ticks.length - 1] || 1;
    const x = i => L + (W - L - R) * i / xmax, y = v => T + (H - T - B) * (1 - v / ymax);
    let out = [`<svg viewBox="0 0 ${W} ${H}" id="areasvg">`];
    for (const t of ticks) out.push(`<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${K(t)}</text>`);
    out.push(`<text x="${L - 6}" y="10" text-anchor="end">token</text>`);
    // 色带仅连接主线程的请求；旁路请求（生成标题、压缩历史）的 prompt 与主对话无关，单独绘制为细柱
    const mainI = reqs.map((r, i) => i).filter(i => reqs[i].thread === 'main' && reqs[i].tok), sideI = reqs.map((r, i) => i).filter(i => reqs[i].thread !== 'main' && reqs[i].tok);
    // 各来源的厚度：有 token 级归属时取各来源的 token 数；只有字符构成时按字符占比分配该请求的 token 总数；两者都没有时整段为灰色
    const BKS = new Map(reqs.map(r => { const bk = bkOf(r), sum = sumOf(bk); if (!sum || !r.tok) return [r.i, null]; const f = r.tok_attr ? 1 : r.tok / sum, o = {}; KINDS.forEach(k => { if (bk[k]) o[k] = bk[k] * f; }); return [r.i, o]; }));
    const known = r => BKS.get(r.i) ? 1 : 0;
    const parts = r => known(r) ? KINDS.filter(k => BKS.get(r.i)[k]).map(k => [BKS.get(r.i)[k], KC(k)]) : [[r.tok, 'var(--reused)']];
    const order = KINDS.filter(k => pts.some(r => (BKS.get(r.i) || {})[k]));
    if (mainI.length > 1) {
      const cum = mainI.map(() => 0);
      const band = (getV, color) => {
        const top = [], bot = [];
        mainI.forEach((i, j) => { const v = getV(reqs[i]) || 0; bot.push([x(i), y(cum[j])]); cum[j] += v; top.push([x(i), y(cum[j])]); });
        const d = top.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ') + ' ' + bot.reverse().map(p => 'L' + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ') + 'Z';
        out.push(`<path class="band" d="${d}" fill="${color}"/>`);
      };
      for (const k of order) band(r => (BKS.get(r.i) || {})[k], KC(k));
      if (mainI.some(i => known(reqs[i]) === 0)) band(r => known(r) === 0 ? r.tok : 0, 'var(--reused)');
    }
    const sw = Math.max(3, Math.min(10, (W - L - R) / xmax * 0.5));
    for (const i of (mainI.length > 1 ? sideI : sideI.concat(mainI))) {
      let base = 0;
      for (const [v, c] of parts(reqs[i])) { out.push(`<rect x="${(x(i) - sw / 2).toFixed(1)}" y="${y(base + v).toFixed(1)}" width="${sw.toFixed(1)}" height="${Math.max(y(base) - y(base + v), .5).toFixed(1)}" fill="${c}" stroke="var(--bg)" stroke-width="1"/>`); base += v; }
    }
    const step = niceStep(reqs.length / 24), slot = (W - L - R) / xmax;
    reqs.forEach((r, i) => {
      if (r.kind === 'compact_like') out.push(`<line class="compact" x1="${x(i)}" x2="${x(i)}" y1="${T - 4}" y2="${H - B}"/><path class="compact-h" d="M${x(i) - 4},${T - 10} h8 l-4,7 z"/>`);
      if (r.template_lost) out.push(`<circle class="lost" cx="${x(i)}" cy="${y(r.tok)}" r="4.5"/>`);
      if (r.thread !== 'main') out.push(`<rect x="${x(i) - 3}" y="${H - B + 3}" width="6" height="6" rx="1" fill="var(--fg3)"/>`);
      if (i % step === 0) out.push(`<text x="${x(i)}" y="${H - B + 23}" text-anchor="middle">${i}</text>`);
    });
    out.push(`<text x="${(L + W - R) / 2}" y="${H - 3}" text-anchor="middle">请求序号</text>`);
    out.push(`<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/><line class="cross" id="areacross" y1="${T}" y2="${H - B}"/>`);
    reqs.forEach((r, i) => out.push(`<rect class="hit" x="${x(i) - slot / 2}" y="${T}" width="${slot}" height="${H - T - B}" data-tipf="req:${i}" data-req="${i}" data-cx="${x(i)}"/>`));
    out.push('</svg>');
    return out.join('');
  }
  function deltaChart(reqs, W = 1120) {
    const H = 280, L = 54, R = 12, T = 18, B = 42;
    const X = reqs.map(recomputed);
    const tops = X.map(v => Math.max(v.nw || 0, v.unc || 0));
    if (!tops.some(v => v)) return '';
    const ticks = yticks(Math.max(...tops, 1)), ymax = ticks[ticks.length - 1] || 1;
    const n = reqs.length, slot = (W - L - R) / n, bw = Math.min(24, Math.max(2, slot * 0.7));
    const x = i => L + slot * (i + .5), y = v => T + (H - T - B) * (1 - v / ymax);
    let out = [`<svg viewBox="0 0 ${W} ${H}">`];
    for (const t of ticks) out.push(`<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${K(t)}</text>`);
    out.push(`<text x="${L - 6}" y="10" text-anchor="end">token</text>`);
    const step = niceStep(n / 24);
    reqs.forEach((r, i) => {
      const v = X[i], parts = v.nw != null ? [[v.fresh, 'var(--accent)'], [v.lost, 'var(--stripped)'], [v.excess, 'var(--excess)']] : [[v.unc, 'var(--reused)']];
      const live = parts.filter(p => p[0] > 0);
      let base = 0;
      live.forEach((p, j) => {
        const y1 = y(base), y0 = y(base + p[0]), last = j === live.length - 1;
        out.push(last ? `<path d="${barPath(x(i) - bw / 2, y0, bw, Math.max(y1 - y0, .5), Math.min(4, bw / 2))}" fill="${p[1]}"/>` : `<rect x="${(x(i) - bw / 2).toFixed(1)}" y="${y0.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(y1 - y0 - (bw > 5 ? 2 : 0), .5).toFixed(1)}" fill="${p[1]}"/>`);
        base += p[0];
      });
      if (v.under) out.push(`<line x1="${x(i) - bw / 2 - 2}" x2="${x(i) + bw / 2 + 2}" y1="${y(v.unc)}" y2="${y(v.unc)}" stroke="var(--fg)" stroke-width="2"/>`);
      if (r.thread !== 'main') out.push(`<rect x="${x(i) - 3}" y="${H - B + 3}" width="6" height="6" rx="1" fill="var(--fg3)"/>`);
      if (i % step === 0) out.push(`<text x="${x(i)}" y="${H - B + 23}" text-anchor="middle">${i}</text>`);
      out.push(`<rect class="hit" x="${x(i) - slot / 2}" y="${T}" width="${slot}" height="${H - T - B}" data-tipf="delta:${i}" data-req="${i}"/>`);
    });
    out.push(`<text x="${(L + W - R) / 2}" y="${H - 3}" text-anchor="middle">请求序号</text>`);
    out.push(`<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/></svg>`);
    return out.join('');
  }
  // 横向柱：每项 {label, value, color, tip}；柱右写「数值 单位 · 百分比」
  function hbars(items, opts) {
    if (!items.length) return '<p class="def">无数据</p>';
    const W = opts.width || 520, lw = opts.labelWidth || 150, bh = 18, gap = 8, H = items.length * (bh + gap) + 6;
    const vmax = Math.max(...items.map(i => i.value), 1e-9), tot = opts.total || items.reduce((a, i) => a + i.value, 0) || 1;
    let out = [`<svg viewBox="0 0 ${W} ${H}">`];
    items.forEach((it, i) => {
      const yy = 3 + i * (bh + gap), w = Math.max(1, (W - lw - 170) * it.value / vmax);
      out.push(`<text x="${lw - 8}" y="${yy + 13}" text-anchor="end">${E(it.label)}</text><path d="${hbarPath(lw, yy, w, bh)}" fill="${it.color || 'var(--accent)'}" data-tip="${E(it.tip || '')}"/><text x="${lw + w + 6}" y="${yy + 13}" style="fill:var(--fg)">${N(it.value)}${E(opts.unit || '')}<tspan class="sub"> · </tspan><tspan class="pc">${P(it.value / tot)}</tspan></text>`);
    });
    out.push('</svg>');
    return out.join('');
  }
  function smallMultiples(sessions) {
    const curves = sessions.map(s => ({id: s.id, client: s.client, pts: s.reqs.filter(r => r.thread === 'main' && (r.tok || r.pt)).map(r => ({v: r.tok || r.pt, compact: r.kind === 'compact_like', lost: r.template_lost || 0, i: r.i}))})).filter(c => c.pts.length > 1);
    if (!curves.length) return '<p class="def">无数据</p>';
    const xmax = Math.max(...curves.map(c => c.pts.length - 1), 1), ticks = yticks(Math.max(...curves.flatMap(c => c.pts.map(p => p.v)), 1)), ymax = ticks[ticks.length - 1] || 1;
    const W = 360, H = 176, L = 42, R = 8, T = 16, B = 24;
    const x = i => L + (W - L - R) * i / xmax, y = v => T + (H - T - B) * (1 - v / ymax);
    const panel = (title, pts, color, width, href) => {
      let o = [href ? `<a class="sm" href="${href}">` : '<div class="sm">', `<div class="smt">${title}${href ? '<span class="go">打开</span>' : ''}</div><svg viewBox="0 0 ${W} ${H}">`];
      for (const t of ticks) o.push(`<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 5}" y="${y(t) + 4}" text-anchor="end">${K(t)}</text>`);
      pts.forEach((p, i) => { if (p.compact) o.push(`<line class="compact" x1="${x(i)}" x2="${x(i)}" y1="${T - 3}" y2="${H - B}"/><path class="compact-h" d="M${x(i) - 3.5},${T - 9} h7 l-3.5,6 z"/>`); });
      const lineD = pts.map((p, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p.v).toFixed(1)).join(' ');
      o.push(`<path d="${lineD} L${x(pts.length - 1).toFixed(1)} ${H - B} L${x(0).toFixed(1)} ${H - B} Z" fill="${color}" opacity=".09"/>`);
      o.push(`<path d="${lineD}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linejoin="round" stroke-linecap="round" style="vector-effect:non-scaling-stroke"/>`);
      pts.forEach((p, i) => { if (p.lost) o.push(`<circle class="lost" cx="${x(i)}" cy="${y(p.v)}" r="3.6"/>`); });
      pts.forEach((p, i) => o.push(`<rect class="hit" x="${x(i) - (W - L - R) / xmax / 2}" y="${T}" width="${(W - L - R) / xmax}" height="${H - T - B}" data-tiph="${E(`<div class="th">第 ${i} 步${p.i != null ? `<small>会话内请求 ${p.i}</small>` : `<small>${p.n} 个会话的中位数</small>`}</div><div class="big">${N(p.v)}<small>token</small></div>` + (p.compact ? '<div class="hint" style="color:var(--fg2)">compact：客户端将历史压缩为摘要</div>' : '') + (p.lost ? `<div class="hint" style="color:var(--fg2)">模板改写历史：${N(p.lost)} 个 token 所属消息内容未变，需重新处理</div>` : '') + (href ? '<div class="hint">点击打开该会话的页面</div>' : ''))}"/>`));
      for (let i = 0; i <= xmax; i += niceStep(xmax / 6)) o.push(`<text x="${x(i)}" y="${H - 7}" text-anchor="middle">${i}</text>`);
      o.push(`<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/></svg>${href ? '</a>' : '</div>'}`);
      return o.join('');
    };
    const med = [];
    for (let i = 0; i <= xmax; i++) { const vs = curves.filter(c => c.pts.length > i).map(c => c.pts[i].v).sort((a, b) => a - b); if (vs.length) med.push({v: vs[Math.floor(vs.length / 2)], n: vs.length}); }
    let h = '<div class="smgrid">' + panel(`<b>各步中位数</b><span>${curves.length} 个会话</span>`, med, 'var(--median)', 2.2, null);
    curves.forEach(c => { h += panel(`<b>${E(c.id.slice(0, 20))}${c.id.length > 20 ? '…' : ''}</b><span>${E(c.client)} · ${c.pts.length} 步</span>`, c.pts, 'var(--accent)', 2, '#s/' + encodeURIComponent(c.id)); });
    return h + '</div>';
  }
  function compBar(bk, tot, thin) {
    return `<div class="bar${thin ? ' thin' : ''}">` + KINDS.filter(k => bk[k]).map(k => { const p = bk[k] / tot; return `<i${LIGHT_FILL.has(k) ? ' class="k"' : ''} style="width:${(100 * p).toFixed(2)}%;background:${KC(k)}" data-tiph="${E(`<div class="th"><span><i class="sw" style="background:${KC(k)}"></i>${KL[k]}</span></div><div class="big">${P(p)}<small>${N(bk[k])} token</small></div>`)}">${!thin && p >= .045 ? (100 * p).toFixed(p >= .1 ? 0 : 1) + '%' : ''}</i>`; }).join('') + '</div>';
  }
  function sizeTable(entries, what, tot) {
    return '<div class="scroll"><table><thead><tr class="grp"><th></th><th></th><th></th><th></th><th class="g" colspan="3">单次' + what + '的大小（' + UNIT + '）</th></tr><tr>'
      + th('工具', '工具名') + th('次数', `该工具的${what}出现的次数；每次工具调用计一次`, 'n') + th('合计', `该工具全部${what}的 ${uw('数')}之和`, 'n', UNIT) + th('占比', `占全部工具${what}的 ${uw('数')}之和的比例`, 'n')
      + th('P50', `该工具各次${what}的大小的中位数`, 'n') + th('P90', `该工具各次${what}的大小按升序排列后位于 90% 位置的值`, 'n') + th('最大值', `该工具各次${what}的大小的最大值`, 'n') + '</tr></thead><tbody>'
      + entries.map(([k, d]) => `<tr><td>${E(k)}</td><td class="n">${N(d.n)}</td><td class="n s">${N(d.sum)}</td><td class="n s">${pctOf(d.sum, tot)}</td><td class="n">${N(d.p50)}</td><td class="n">${N(d.p90)}</td><td class="n">${N(d.max)}</td></tr>`).join('') + '</tbody></table></div>';
  }

  // ---------- 页面骨架 ----------
  function header(title, meta, tocItems, curId) {
    const links = [`<a href="#overview"${curId ? '' : ' class="cur"'}>总览</a>`].concat(D.sessions.map(s => `<a href="#s/${encodeURIComponent(s.id)}"${s.id === curId ? ' class="cur"' : ''} data-tip="${E(`${s.id}\n${s.client}，${s.n_req} 个请求，最长 prompt ${N(s.max_tok)} token`)}">${E(s.id.slice(0, 12))}…</a>`));
    const dl = meta.map(([k, v]) => `<dt>${E(k)}</dt><dd>${v}</dd>`).join('');
    return `<header class="top"><p class="eyebrow">PROMPT CAPTURE · ${curId ? '单个会话' : '会话总览'}</p><h1>${E(title)}</h1><dl class="meta">${dl}</dl></header><nav class="toc"><div class="row ses"><span class="lbl">会话</span>${links.join('')}</div>${tocItems ? '<div class="row sec"><span class="lbl">章节</span>' + tocItems.map(([a, t]) => `<a href="#${a}" data-jump="${a}">${E(t)}</a>`).join('') + '</div>' : ''}</nav>`;
  }
  // 各来源的说明：内容、产生方、重发情况
  const KIND_NOTE = {
    prefix: ['prompt 开头、位于全部消息之前的固定文本', '聊天模板', '每个请求均包含，内容相同'],
    tools_def: ['客户端在请求的 tools 字段中声明的工具（名称、说明、参数格式），由模板渲染为文本', '客户端声明，模板渲染', '每个请求均包含；同一客户端的内容相同'],
    system: ['系统提示：客户端为模型设定的角色与行为规则', '客户端', '每个请求均包含；同一会话内内容相同'],
    user: ['用户输入的消息', '用户', '进入历史后，其后的每个请求均重发'],
    'assistant.content': ['模型在此前各步输出的回复正文', '模型生成，客户端写回历史', '进入历史后，其后的每个请求均重发'],
    'assistant.reasoning': ['模型在此前各步输出的思考内容（reasoning_content）', '模型生成，客户端写回历史', '客户端在其后的每个请求中重发；是否渲染进 prompt 由模板决定（见第 2 节「模板改写历史」）'],
    'assistant.tool_call': ['模型在此前各步发起的工具调用：函数名与参数值', '模型生成，客户端写回历史', '进入历史后，其后的每个请求均重发'],
    'tool.result': ['工具执行后返回的内容（role=tool 的消息）：文件内容、命令输出、检索结果', '客户端执行工具后写入', '进入历史后，其后的每个请求均重发'],
    markup: ['模板在各条消息前后插入的控制标记，标明消息的角色与工具调用的边界', '聊天模板', '每条消息均包含'],
    gen_prompt: ['prompt 末尾的标记，指示模型自此处开始生成', '聊天模板', '每个请求末尾出现一次'],
  };
  function kindExamples() {
    const ex = {}, minLen = {prefix: 3, markup: 3, gen_prompt: 3};
    for (const s of D.sessions) {
      if (!s.text_embedded) continue;
      for (const r of s.reqs.filter(r => r.thread === 'main').concat(s.reqs.filter(r => r.thread !== 'main'))) for (const sg of r.segs || []) {
        if (ex[sg.k]) continue;
        const t = D.texts[sg.b];
        if (t && t.trim().length >= (minLen[sg.k] || 24)) ex[sg.k] = t.trim();
      }
      if (KINDS.every(k => ex[k])) break;
    }
    return ex;
  }
  function overview() {
    tipFns = {};
    const F = D.fleet, tot = sumOf(F.by_kind) || 1;  // 来源构成的分母：有来源归属的全部 token（或字符）
    const kinds = KINDS.filter(k => F.by_kind[k]);
    const meta = [['数据目录', E(D.meta.data.join(', '))], ['分词器', E(D.meta.tokenizer || '未提供（仅有字符级统计）')], ['范围', `采集数据含 <b>${D.meta.sessions_total}</b> 个会话，本报告分析其中 <b>${D.meta.chosen}</b> 个，共 <b>${N(F.n_req)}</b> 个请求`], ['生成时间', E(D.meta.generated)]];
    let h = header('会话分析报告', meta, [['o1', '1 来源构成'], ['o2', '2 长度增长'], ['o3', '3 工具'], ['o4', '4 优化上限'], ['o5', '5 会话列表']]);
    const top3 = kinds.slice().sort((a, b) => F.by_kind[b] - F.by_kind[a]).slice(0, 3);
    const bullets = [];
    if (top3.length) bullets.push(`分词器处理的${ATTR ? ' token ' : '文本（按字符计）'}中，占比前三位的来源为${top3.map(k => `${KL[k]} ${B(P(F.by_kind[k] / tot))}`).join('、')}。`);
    else bullets.push('没有任何请求带来源构成：所分析的请求均不含全文，或全部渲染失败（见下方「注意」）。');
    if (F.new_share != null) bullets.push(`需重新处理的 token（位于与同线程上一请求的公共前缀之后）占 ${B(P(F.new_share))}；其余 ${B(P(1 - F.new_share))} 位于公共前缀内。`);
    if (F.template_lost) bullets.push(`模板改写历史造成 ${B(N(F.template_lost))} 个 token 需重新处理，其所属消息的内容未变；占全部 token 的 ${B(P(F.template_lost_share))}，占需重新处理的 token 的 ${B(pctOf(F.template_lost, F.new_sum))}，涉及 ${B(F.template_lost_req)} 个请求。`);
    if (F.cache_hit != null) bullets.push(`vLLM 前缀缓存命中率为 ${B(P(F.cache_hit))}（token 加权）。`);
    const dt = F.deltas.append_tool, du = F.deltas.append_user;
    if (dt && du) bullets.push(`单个请求需重新处理的 token 数的中位数：「${KIND_CN.append_tool}」为 ${B(N(dt.p50))}，「${KIND_CN.append_user}」为 ${B(N(du.p50))}。`);
    h += '<div class="summary"><ul>' + bullets.map(b => `<li>${b}</li>`).join('') + '</ul></div>' + attrNotice();
    h += '<div class="kpis">' + kpi(N(F.n_sessions), '个', '会话') + kpi(N(F.n_req), '个', '请求') + kpi(N(F.tok_total / 1e6, 2), 'M token', 'prompt token 合计', '全部请求的 prompt token 数之和')
      + kpi(P(F.new_share), '', '需重新处理的 token 占比', '与同线程上一请求的公共前缀之后的 token 数 / 全部 token 数') + kpi(P(F.cache_hit), '', 'vLLM 前缀缓存命中率', 'cached_tokens 之和 / prompt_tokens 之和')
      + kpi(N(F.template_lost), 'token', '模板改写造成的重新处理量', '所属消息内容未变、因渲染结果变化而需重新处理的 token 数') + '</div>';

    // 1 来源
    const ex = kindExamples();
    const rx = F.resend_example;
    const rxEg = rx ? `<span class="eg">本数据中被重发最多的一条工具结果：会话 ${E(rx.session.slice(0, 16))}… 的请求 ${rx.first} 新增一条 ${E(rx.tool || 'tool')} 工具结果（${N(rx.size)} ${UNIT}），其后的请求重发 ${N(rx.n - 1)} 次，共计入 ${N(rx.n)} 次、${N(rx.n * rx.size)} ${UNIT}。</span>` : '';
    h += `<h2 id="o1"><span class="no">1</span>来源构成${ATTR ? '' : '（按字符）'}</h2>` + defs([
      ['来源', 'prompt 由聊天模板将请求的各组成部分拼接而成。本节按各段文本所对应的请求组成部分，将 prompt 划分为 10 种来源，定义见下表'],
      ['统计范围', `所分析的 ${N(F.n_sessions)} 个会话中有来源归属的请求（${N(ATTR ? F.n_attr : F.n_req)} 个）；各来源的 ${uw('数')}按请求累计`],
      ['累计口径', '客户端在每个请求中重发全部历史，分词器对每个请求的全部文本重新分词，因此同一条消息在其后的每个请求中各计一次。' + rxEg + '本节反映分词器处理量的构成，不反映会话中不重复内容的构成'],
      [uw('数'), ATTR ? '含 golden 的请求取 vLLM 实际的 token；其余请求取离线渲染与分词的结果，并与 vLLM 返回的 prompt_tokens 核对' : '模板后文本中各来源片段的字符数。本报告没有 token 级归属，token 数无法按来源划分']]);
    const hist = ['user', 'assistant.content', 'assistant.reasoning', 'assistant.tool_call', 'tool.result'].reduce((a, k) => a + (F.by_kind[k] || 0), 0);
    const fixed = ['prefix', 'tools_def', 'system'].reduce((a, k) => a + (F.by_kind[k] || 0), 0);
    h += fig(`各来源的占比（${UNIT}）`, (kinds.length ? compBar(F.by_kind, tot) : '<p class="def">没有任何请求带来源构成</p>') + legend(kinds), {
      read: `条形全长对应有来源归属的全部 ${N(tot)} ${UNIT}，各段宽度为该来源的占比；占比不低于 4.5% 的段内标注比例，各来源的数值见下表。`,
      finding: `占比前三位的来源为 ${top3.map(k => `${B(KL[k])} ${B(P(F.by_kind[k] / tot))}`).join('、')}。对话历史（user、assistant 正文与思考、工具调用、工具结果）合计占 ${B(P(hist / tot))}；各请求均包含的固定部分（模板开头、工具定义、system）合计占 ${B(P(fixed / tot))}；模板标记占 ${B(P((F.by_kind.markup || 0) / tot))}。`});
    h += '<div class="scroll"><table><thead><tr>' + th('来源') + th(UNIT, `该来源在全部请求中的 ${uw('数')}之和`, 'n') + th('占比', `占全部 ${uw('的')}比例`, 'n') + th('内容') + th('产生方') + th('重发情况') + th('原文片段', '取自本次采集数据的一段原文的开头；⏎ 表示换行') + '</tr></thead><tbody>'
      + kinds.map(k => { const nt = KIND_NOTE[k] || ['', '', '']; return `<tr><td style="white-space:nowrap"><i class="sw" style="background:${KC(k)}"></i><b>${KL[k]}</b></td><td class="n">${N(F.by_kind[k])}</td><td class="n s">${P(F.by_kind[k] / tot)}</td><td class="note">${E(nt[0])}</td><td class="note">${E(nt[1])}</td><td class="note">${E(nt[2])}</td><td class="eg">${ex[k] ? vis(ex[k], 96) : '<span class="muted">原文未嵌入</span>'}</td></tr>`; }).join('') + '</tbody></table></div>';

    // 2 增长
    h += '<h2 id="o2"><span class="no">2</span>主线程 prompt 长度随步数的增长</h2>' + defs([
      ['主线程', '一个会话的主对话，即 system 提示相同且请求数最多的请求序列。客户端另行发出的旁路请求（生成标题、压缩历史）不计入'],
      ['步', '主线程内请求的序号，自 0 起。agent 每执行一次工具调用或用户每输入一轮，步数加 1']]);
    h += '<dl class="key">'
      + '<dt><svg viewBox="0 0 34 16"><path d="M2 13 L12 9 L22 6 L32 2" fill="none" stroke="var(--accent)" stroke-width="2"/></svg>蓝色折线</dt><dd>单个会话主线程各步的 prompt 长度</dd>'
      + '<dt><svg viewBox="0 0 34 16"><path d="M2 13 L12 9 L22 6 L32 2" fill="none" stroke="var(--median)" stroke-width="2.2"/></svg>黑色折线</dt><dd>各会话在同一步上的 prompt 长度的<b>中位数</b></dd>'
      + '<dt><svg viewBox="0 0 34 16"><line class="compact" x1="17" x2="17" y1="6" y2="16"/><path class="compact-h" d="M13.5,0 h7 l-3.5,6 z"/></svg>红色竖线</dt><dd><b>compact</b>：客户端在该步将历史压缩为摘要，消息条数降至上一步的一半以下，prompt 长度下降</dd>'
      + '<dt><svg viewBox="0 0 34 16"><circle class="lost" cx="17" cy="8" r="4.5"/></svg>粉色空心圆</dt><dd><b>模板改写历史</b>：客户端在该步发送的历史消息与上一步相同，聊天模板对其中部分消息的渲染结果与上一步不同。'
      + '本数据中的成因：模板仅渲染最后一条 user 消息之后的 assistant 思考；新的 user 消息加入后，此前各条 assistant 消息的 <code>&lt;think&gt;…&lt;/think&gt;</code> 被渲染为空的 <code>&lt;think&gt;&lt;/think&gt;</code>。'
      + '影响：自第一处渲染结果变化的位置起，token 序列与上一步不同，<mark>前缀缓存自该位置起失效，其后的 token 需重新分词与 prefill</mark></dd></dl>';
    const nLost = F.template_lost_req || 0, nCompact = (F.kinds || {}).compact_like || 0;
    h += fig('各会话主线程的 prompt 长度', smallMultiples(D.sessions), {
      read: '每格为一个会话，各格纵轴刻度相同。横轴：主线程内的步数。纵轴：该步的 prompt 长度，单位 token；有分词结果时取离线分词的 token 数，否则取 vLLM 返回的 prompt_tokens。交互：悬停显示该步的数值；点击打开该会话的页面。',
      finding: `${N(F.n_sessions)} 个会话中，compact 出现 ${B(nCompact)} 次，模板改写历史出现 ${B(nLost)} 次` + (nLost ? `，后者造成 ${B(N(F.template_lost))} 个 token 需重新处理` : '') + '。'});
    const dE = Object.entries(F.deltas), dN = dE.reduce((a, [, d]) => a + d.n, 0), dS = dE.reduce((a, [, d]) => a + d.sum, 0);
    const egD = dE.slice().sort((a, b) => b[1].n - a[1].n)[0];
    h += defs([
      ['需重新处理的 token', '将本请求与同线程上一请求的 token 序列自开头逐个比较，第一个不同位置之后的全部 token；即本请求中需重新分词与 prefill 的 token。单位 token'],
      ['P50 / P90 / 最大值', '同一类型各请求的需重新处理 token 数按升序排列后，位于 50% / 90% 位置的值与最大值；P50 即中位数' + (egD ? `<span class="eg">本数据：「${E(KIND_CN[egD[0]] || egD[0])}」一行含 ${N(egD[1].n)} 个请求；其中 50% 的请求需重新处理的 token 数不超过 ${N(egD[1].p50)}，90% 的请求不超过 ${N(egD[1].p90)}，最大值为 ${N(egD[1].max)}。</span>` : '')]]);
    h += '<div class="grid2"><div>' + fig('单个请求需重新处理的 token 数，按请求类型', '<div class="scroll"><table><thead><tr class="grp"><th></th><th></th><th></th><th class="g" colspan="3">单个请求的 token 数</th><th class="g" colspan="2">该类型合计</th></tr><tr>'
      + th('请求类型', '本请求的 messages 相对同线程上一请求的变化') + th('请求数', '该类型的请求数', 'n') + th('占比', '占全部可比较请求的比例', 'n') + th('P50', '中位数', 'n', 'token') + th('P90', '按升序排列后位于 90% 位置的值', 'n', 'token') + th('最大值', '', 'n', 'token') + th('合计', '该类型各请求的 token 数之和', 'n', 'token') + th('占比', '占全部需重新处理 token 的比例', 'n') + '</tr></thead><tbody>'
      + dE.map(([k, d]) => `<tr><td data-tip="${E(KIND_DEF[k] || '')}">${E(KIND_CN[k] || k)}</td><td class="n">${N(d.n)}</td><td class="n s">${pctOf(d.n, dN)}</td><td class="n s">${N(d.p50)}</td><td class="n">${N(d.p90)}</td><td class="n">${N(d.max)}</td><td class="n">${N(d.sum)}</td><td class="n s">${pctOf(d.sum, dS)}</td></tr>`).join('') + '</tbody></table></div>',
      {read: '每行为一种请求类型；各类型的判定条件见类型名的悬停说明。统计范围为存在同线程上一请求的请求。', finding: dt && du ? `「${KIND_CN.append_user}」的中位数为 ${B(N(du.p50))} token，为「${KIND_CN.append_tool}」（${B(N(dt.p50))} token）的 ${B((du.p50 / (dt.p50 || 1)).toFixed(1))} 倍` + (F.template_lost ? `；${B(N(F.template_lost_req))} 个请求发生模板改写历史，合计 ${B(N(F.template_lost))} token。` : '。') : ''}) + '</div>';
    const cE = Object.entries(F.causes).sort((a, b) => b[1] - a[1]), cN = cE.reduce((a, [, v]) => a + v, 0);
    h += '<div>' + fig('相邻请求 token 序列的分歧原因', '<div class="scroll"><table><thead><tr>' + th('原因') + th('请求数', '', 'n') + th('占比', '占全部可比较请求的比例', 'n') + th('定义') + '</tr></thead><tbody>'
      + cE.map(([k, v]) => `<tr><td style="white-space:nowrap">${E(CAUSE_CN[k] || k)}</td><td class="n">${N(v)}</td><td class="n s">${pctOf(v, cN)}</td><td class="note">${E(CAUSE_DEF[k] || '')}</td></tr>`).join('') + '</tbody></table></div>',
      {read: '各请求与同线程上一请求比较；线程首个请求无比较对象，不计入。', finding: cE.length ? `分歧原因为「${CAUSE_CN.append}」的请求占 ${B(pctOf((F.causes.append || 0), cN))}，其上一请求的全部 token 可由前缀缓存与增量分词复用` + (nLost ? `；因模板改写历史而在历史消息处产生分歧的请求占 ${B(pctOf(nLost, cN))}。` : '。') : ''}) + '</div></div>';

    // 3 工具
    h += '<h2 id="o3"><span class="no">3</span>工具结果与工具调用的大小</h2>' + defs([
      ['工具结果', '工具执行后返回给模型的内容（role=tool 的消息）'],
      ['工具调用参数', '模型发起工具调用时给出的函数名与参数值（assistant 消息的 tool_calls）'],
      ['统计范围', `每条消息仅在其首次出现的请求中统计一次，作为历史重发时不再计入；对应各步新进入分词器的内容。单位 ${UNIT}`],
      ['次数 / 合计 / P50 / P90 / 最大值', `次数：该工具的结果（或调用）出现的次数。合计：各次 ${uw('数')}之和。P50 / P90：各次的 ${uw('数')}按升序排列后位于 50% / 90% 位置的值。最大值：各次 ${uw('数')}的最大值`]]);
    const ts = Object.entries(F.tool_sizes), cs = Object.entries(F.call_sizes);
    const tsum = ts.reduce((a, [, d]) => a + d.sum, 0), csum = cs.reduce((a, [, d]) => a + d.sum, 0);
    h += '<div class="grid2">';
    const noneNote = ts.length || cs.length ? '' : '<p class="def">没有可统计的工具结果或工具调用：所分析的请求不含全文，或渲染失败。</p>';
    h += noneNote + '<div>' + fig(`工具结果的 ${uw('数')}，按工具`, hbars(ts.slice(0, 14).map(([k, d]) => ({label: `${k}（${d.n} 次）`, value: d.sum, tip: `${k}：${d.n} 次，合计 ${N(d.sum)} ${UNIT}，占 ${pctOf(d.sum, tsum)}\n单次大小：P50 ${N(d.p50)}，P90 ${N(d.p90)}，最大值 ${N(d.max)} ${UNIT}`, color: KC('tool.result')})), {width: 560, labelWidth: 150, unit: ' ' + UNIT, total: tsum}),
      {read: `柱长：该工具全部结果的 ${uw('数')}之和。柱右标注：${uw('数')} · 占全部工具结果 ${N(tsum)} ${uw('的')}比例。`, finding: ts.length ? `${uw('数')}最多的工具为 ${B(ts[0][0])}：${B(N(ts[0][1].n))} 次，合计 ${B(N(ts[0][1].sum))} ${UNIT}，占全部工具结果的 ${B(pctOf(ts[0][1].sum, tsum))}；单次大小的中位数为 ${B(N(ts[0][1].p50))} ${UNIT}，最大值为 ${B(N(ts[0][1].max))} ${UNIT}。` : ''}) + sizeTable(ts, '结果', tsum) + '</div>';
    h += '<div>' + fig(`工具调用参数的 ${uw('数')}，按工具`, hbars(cs.slice(0, 14).map(([k, d]) => ({label: `${k}（${d.n} 次）`, value: d.sum, tip: `${k}：${d.n} 次，合计 ${N(d.sum)} ${UNIT}，占 ${pctOf(d.sum, csum)}\n单次大小：P50 ${N(d.p50)}，P90 ${N(d.p90)}，最大值 ${N(d.max)} ${UNIT}`, color: KC('assistant.tool_call')})), {width: 560, labelWidth: 150, unit: ' ' + UNIT, total: csum}),
      {read: `柱长：该工具全部调用参数（函数名与参数值）的 ${uw('数')}之和。柱右标注：${uw('数')} · 占全部调用参数 ${N(csum)} ${uw('的')}比例。`, finding: cs.length ? `${uw('数')}最多的工具为 ${B(cs[0][0])}：合计 ${B(N(cs[0][1].sum))} ${UNIT}，占全部调用参数的 ${B(pctOf(cs[0][1].sum, csum))}。全部调用参数合计 ${B(N(csum))} ${UNIT}，为全部工具结果（${B(N(tsum))} ${UNIT}）的 ${B(pctOf(csum, tsum))}。` : ''}) + sizeTable(cs, '调用', csum) + '</div></div>';

    // 4 优化空间
    h += '<h2 id="o4"><span class="no">4</span>优化上限估算</h2>' + defs([['口径', '各项为分词与前端侧的<b>上限</b>，即该措施完全生效时可减少的分词量；不含 prefill'], ['依据', '本报告第 1～3 节的统计值' + (ATTR ? '' : '；本报告没有 token 级归属，各来源的占比按字符计')]]);
    const fixedShare = ((F.by_kind.tools_def || 0) + (F.by_kind.system || 0)) / tot;
    const rows = [
      ['按消息缓存分词结果，仅对需重新处理的片段分词', '需重新处理的 token 占比 ' + B(P(F.new_share)), '分词量减少 ' + B(P(1 - (F.new_share || 0)))],
      ['缓存工具定义与 system 的渲染文本及 token', '工具定义与 system 合计占 ' + B(P(fixedShare)) + '，同一客户端的各会话内容相同', '分词量减少 ' + B(P(fixedShare)) + '，并省去这两部分的模板渲染'],
      ['模板标记按角色查表拼接，不经过 BPE', '模板标记占 ' + B(P((F.by_kind.markup || 0) / tot)), '分词量减少 ' + B(P((F.by_kind.markup || 0) / tot))],
      ['分段缓存以删除思考后的形态缓存 assistant 片段；或客户端不回传思考；或设置模板参数 clear_thinking=false', '模板改写历史造成 ' + B(N(F.template_lost)) + ' token 需重新处理，占需重新处理 token 的 ' + B(pctOf(F.template_lost, F.new_sum)), '需重新处理的 token 减少 ' + B(pctOf(F.template_lost, F.new_sum)) + '；前缀缓存不再于该位置失效'],
      ['客户端不回传将被模板删除的思考', '渲染进 prompt 的 assistant 思考合计 ' + B(N(F.reasoning_tok_total)) + ' token；思考被模板删除的消息累计 ' + B(N(F.reasoning_stripped_msgs)) + ' 条次（同一条消息在每个请求中各计一次）', '请求体大小与 JSON 解析量相应减少；减少量未在本报告中测量'],
      ['模板渲染仅处理新增消息', F.render_slope_ms_per_msg != null ? `离线渲染耗时对消息条数的线性回归斜率为每条消息 ${B(F.render_slope_ms_per_msg)} ms（本机，n=${N(F.n_render)}）` : '渲染耗时未测量', '渲染耗时与新增消息条数成正比'],
      ['会话亲和路由、缓存块对齐', 'vLLM 前缀缓存命中率 ' + B(P(F.cache_hit)) + '（token 加权）', '可减少的量为各会话页第 2 节中的「额外未命中」'],
    ];
    h += '<div class="scroll"><table><thead><tr><th>措施</th><th>依据</th><th>上限</th></tr></thead><tbody>' + rows.map(r => `<tr><td>${E(r[0])}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`).join('') + '</tbody></table></div>';

    // 5 会话列表
    h += '<h2 id="o5"><span class="no">5</span>会话列表</h2><p class="read">每行为一个会话；各列的定义见表头的悬停说明。交互：点击一行打开该会话的页面。</p>';
    h += '<div class="scroll"><table><thead><tr>' + th('会话', '会话标识') + th('客户端', '按 User-Agent 归类') + th('请求数', '该会话在采集时间范围内的请求数', 'n') + th('含全文', '其中含全文的请求数', 'n') + th('含 golden', '其中含 vLLM 实际 token ids 的请求数', 'n')
      + th('最长 prompt', '该会话各请求 prompt 长度的最大值', 'n', 'token') + th('token 合计', '该会话全部请求的 prompt token 数之和', 'n') + th('需重新处理', '分歧点之后的 token 数，等于 token 减去与上一请求的公共前缀长度；即本请求中需重新分词与 prefill 的 token 数。括号内为占本请求 token 的比例。线程首个请求记为「全部」', 'n', 'token') + th('模板改写', '所属消息内容未变、因渲染结果变化而需重新处理的 token 数', 'n', 'token')
      + th('缓存命中率', 'cached_tokens 之和 / prompt_tokens 之和', 'n') + th('时长', '首个请求至最后一个请求的时间', 'n', '秒') + '</tr></thead><tbody>'
      + D.sessions.map(s => `<tr class="click" data-href="#s/${encodeURIComponent(s.id)}"><td><a href="#s/${encodeURIComponent(s.id)}">${E(s.id.slice(0, 28))}</a>${s.parent ? '<span class="pill">子代理</span>' : ''}</td><td>${E(s.client)}</td><td class="n">${s.n_req}</td><td class="n">${s.n_text}</td><td class="n">${s.gold}</td><td class="n">${N(s.max_tok)}</td><td class="n">${N(s.sum_tok)}</td><td class="n s">${P(s.new_share)}</td><td class="n">${N(s.template_lost)}</td><td class="n s">${P(s.cache_hit)}</td><td class="n">${N(s.duration_s, 1)}</td></tr>`).join('') + '</tbody></table></div>';
    h += '<p class="foot">由 prompt_capture/tools/session_view.py 生成。字段定义见 docs/REFERENCE.md §7.8。</p>';
    app.innerHTML = h;
    app.querySelectorAll('tr.click').forEach(tr => tr.addEventListener('click', e => { if (e.target.tagName !== 'A') location.hash = tr.dataset.href; }));
  }

  // ---------- 单会话 ----------
  function workedExample(s) {
    const reqs = s.reqs, cand = reqs.filter(r => r.tok_new != null && r.tok_prefix != null);
    if (!cand.length) return '';
    const r = cand.slice().sort((a, b) => (b.template_lost || 0) - (a.template_lost || 0) || recomputed(b).excess - recomputed(a).excess || b.tok_new - a.tok_new)[0];
    const x = recomputed(r), pv = prevInThread(reqs, r);
    let t = `<b>请求 ${r.i} 的分解</b>（${E(KIND_CN[r.kind] || r.kind)}；本会话中模板改写最多、其次额外未命中最多的请求）。prompt 共 ${B(N(x.tok))} token。与同线程上一请求（请求 ${pv ? pv.i : '–'}）的 token 序列自开头逐个比较，前 ${B(N(r.tok_prefix))} 个相同，其后的 ${B(N(x.nw))} 个为需重新处理的 token。`;
    if (x.lost) t += `其中 ${B(N(x.fresh))} 个属于新增内容；${B(N(x.lost))} 个所属消息的内容未变，因渲染结果变化而需重新处理（${E(CAUSE_CN[r.cause] || r.cause)}）。`;
    else t += '需重新处理的 token 全部属于新增内容。';
    if (r.pt != null) t += `vLLM 返回 cached_tokens = ${B(N(r.ct || 0))}，未命中 ${N(r.pt)} − ${N(r.ct || 0)} = ${B(N(x.unc))} 个 token` + (x.excess <= 16 && !x.under ? `，与需重新处理的 ${N(x.nw)} 个相差 ${N(Math.abs(x.unc - x.nw))} 个，不超过一个缓存块（16 token）；<mark>vLLM 前缀缓存的失效位置与 token 序列的分歧位置一致</mark>。` : x.excess > 16 ? `，较需重新处理的 ${N(x.nw)} 个多 ${B(N(x.excess))} 个；<mark>该部分 token 与上一请求相同，前缀缓存未命中</mark>。` : `，较需重新处理的 ${N(x.nw)} 个少 ${B(N(x.nw - x.unc))} 个；前缀缓存命中的长度超过与上一请求的公共前缀。`);
    return t;
  }
  let curS = null;
  function sessionPage(s, openReq) {
    curS = s;
    const reqs = s.reqs;
    tipFns = {req: i => reqTipHtml(reqs[+i], reqs), delta: i => deltaTipHtml(reqs[+i], reqs)};
    const meta = [['会话', E(s.id) + (s.parent ? `（子代理；父会话 ${E(s.parent)}）` : '')], ['客户端', E(s.client) + ' · ' + E(s.ua || '')], ['模型', E(s.model)], ['时间', `${s.first} 至 ${s.last}（${N(s.duration_s, 1)} s）`], ['记录', `<b>${s.n_req}</b> 个请求，含全文的 <b>${s.n_text}</b> 个，含 golden 的 <b>${s.gold}</b> 个${s.followed ? '，由整会话跟踪采集的 ' + s.followed + ' 个' : ''}；线程 <b>${s.n_threads}</b> 条` + (s.text_embedded ? '' : '；原文未嵌入（超出 --embed-mb）')]];
    let h = header('会话 ' + s.id.slice(0, 28), meta, [['s1', '1 长度增长'], ['s2', '2 重新处理量'], ['s3', '3 消息梯子'], ['s4', '4 逐请求']], s.id);
    h += '<div class="kpis">' + kpi(N(s.max_tok), 'token', '最长 prompt', '各请求 prompt 长度的最大值') + kpi(N(s.sum_tok), 'token', 'prompt token 合计', '全部请求的 prompt token 数之和') + kpi(P(s.new_share), '', '需重新处理的 token 占比', '与同线程上一请求的公共前缀之后的 token 数 / 全部 token 数')
      + kpi(N(s.template_lost), 'token', '模板改写造成的重新处理量', '所属消息内容未变、因渲染结果变化而需重新处理的 token 数') + kpi(P(s.cache_hit), '', 'vLLM 前缀缓存命中率', 'cached_tokens 之和 / prompt_tokens 之和') + kpi(N(s.max_msgs), '条', '最大消息条数', '各请求 messages 数组长度的最大值') + '</div>' + attrNotice();

    // 1 增长
    const unknown = reqs.some(r => r.tok && !sumOf(bkOf(r)));
    const mainR = reqs.filter(r => r.thread === 'main' && r.tok);  // 结论只取主线程：旁路请求的 prompt 与主对话无关
    const first = mainR[0], lastR = mainR[mainR.length - 1], peak = mainR.reduce((a, r) => r.tok > a.tok ? r : a, mainR[0] || {tok: 0});
    const nC = reqs.filter(r => r.kind === 'compact_like').length, nL = reqs.filter(r => r.template_lost).length, nSide = reqs.filter(r => r.thread !== 'main').length;
    h += '<h2 id="s1"><span class="no">1</span>prompt 长度随请求序号的变化</h2>';
    h += '<dl class="key">'
      + '<dt><span class="cell" style="background:var(--k-tool_result)"></span>色带</dt><dd>每层对应一种来源，层厚为该来源在该请求中的 token 数；各层之和为该请求的 prompt 长度。色带仅连接主线程的请求</dd>'
      + '<dt><svg viewBox="0 0 34 16"><line class="compact" x1="17" x2="17" y1="6" y2="16"/><path class="compact-h" d="M13.5,0 h7 l-3.5,6 z"/></svg>红色竖线</dt><dd><b>compact</b>：客户端在该请求中将历史压缩为摘要，prompt 长度下降</dd>'
      + '<dt><svg viewBox="0 0 34 16"><circle class="lost" cx="17" cy="8" r="4.5"/></svg>粉色空心圆</dt><dd><b>模板改写历史</b>：客户端发送的历史消息与上一请求相同，模板对其中部分消息的渲染结果不同（历史 assistant 消息的思考被删除）；自渲染结果变化的位置起，token 需重新处理，数量见第 2 节</dd>'
      + '<dt><svg viewBox="0 0 34 16"><rect x="14" y="1" width="6" height="9" fill="var(--k-user)"/><rect x="14" y="12" width="6" height="4" rx="1" fill="var(--fg3)"/></svg>细柱与灰色方块</dt><dd><b>旁路请求</b>：生成标题、压缩历史等主对话之外的请求，其 system 提示与主对话不同。以细柱单独绘制，并在横轴下方以灰色方块标出</dd></dl>';
    h += fig('各请求的 prompt token 数，按来源堆叠', stackedArea(reqs) + legend(KINDS.filter(k => reqs.some(r => bkOf(r)[k]))) + (unknown ? '<div class="legend"><span><i style="background:var(--reused)"></i>构成未知：该请求无来源归属（不含全文，或渲染失败），高度取 vLLM 返回的 prompt_tokens</span></div>' : ''),
      {read: '横轴：请求序号，按到达时间排列，自 0 起。纵轴：prompt 的 token 数。' + (ATTR ? '' : '本报告没有 token 级归属：各层厚度按该来源的字符占比分配该请求的 token 总数。') + '交互：悬停显示该请求各来源的数量与占比；点击展开该请求。',
        finding: first && lastR ? `主线程共 ${B(N(mainR.length))} 个请求；prompt 长度在首个请求（请求 ${first.i}）为 ${B(N(first.tok))} token，在最后一个请求（请求 ${lastR.i}）为 ${B(N(lastR.tok))} token，最大值为请求 ${peak.i} 的 ${B(N(peak.tok))} token。compact 出现 ${B(nC)} 次，模板改写历史出现 ${B(nL)} 次；旁路请求 ${B(nSide)} 个。` : ''});

    // 2 重算分解
    const X = reqs.map(recomputed), sum = k => X.reduce((a, v) => a + (v[k] || 0), 0);
    const sF = sum('fresh'), sL = sum('lost'), sE = sum('excess'), sT = sF + sL + sE;
    h += '<h2 id="s2"><span class="no">2</span>各请求需重新处理的 token</h2>' + defs([
      ['需重新处理的 token', '本请求中位于「与同线程上一请求的公共 token 前缀」之后的 token。公共前缀可由前缀缓存与增量分词复用，其后的 token 需重新分词与 prefill'],
      ['计算方法', '将本请求与同线程上一请求的 token 序列自开头逐个比较，取第一个不同的位置；该位置之前为公共前缀，之后为需重新处理的 token。线程首个请求的 prompt 全部计为需重新处理']]);
    h += '<dl class="key">'
      + '<dt><span class="cell" style="background:var(--accent)"></span>蓝：新增内容</dt><dd>本请求新增的消息所对应的 token；线程首个请求的 prompt 全部计入。该部分不可避免</dd>'
      + '<dt><span class="cell" style="background:var(--stripped)"></span>粉：模板改写</dt><dd>所属消息内容未变、因模板渲染结果与上一请求不同而需重新处理的 token。对应的措施见总览第 4 节</dd>'
      + '<dt><span class="cell" style="background:var(--excess)"></span>黄：额外未命中</dt><dd>vLLM 未命中的 token 数（prompt_tokens − cached_tokens）超出前两项之和的部分。这些 token 与上一请求相同，前缀缓存未命中；成因不在本报告的采集范围内。不超过 16 token 的差值来自缓存按块对齐</dd>'
      + '<dt><svg viewBox="0 0 34 16"><line x1="6" x2="28" y1="8" y2="8" stroke="var(--fg)" stroke-width="2"/></svg>黑色横线</dt><dd>vLLM 未命中的 token 数低于需重新处理的 token 数（相差超过 16）时，标出 vLLM 未命中的 token 数；此时前缀缓存命中的长度超过与上一请求的公共前缀</dd>'
      + (X.some(v => v.nw == null && v.unc != null) ? '<dt><span class="cell" style="background:var(--reused)"></span>灰</dt><dd>该请求无分词结果，仅有 vLLM 未命中的 token 数，无法分解</dd>' : '') + '</dl>';
    h += fig('各请求需重新处理的 token 数，按成因分解', deltaChart(reqs), {
      read: '横轴：请求序号；横轴下方的灰色方块标出旁路请求。纵轴：token 数。每柱自下而上依次为新增内容、模板改写、额外未命中。交互：悬停显示各部分的数值；点击展开该请求。',
      finding: sT ? `全部请求需重新处理的 token 与额外未命中合计 ${B(N(sT))} token，占全部 prompt token 的 ${B(pctOf(sT, s.sum_tok))}。其中新增内容 ${B(N(sF))} token（${B(pctOf(sF, sT))}），模板改写 ${B(N(sL))} token（${B(pctOf(sL, sT))}），额外未命中 ${B(N(sE))} token（${B(pctOf(sE, sT))}）。` + (sL + sE > 0 ? `<mark>模板改写与额外未命中合计占 ${pctOf(sL + sE, sT)}</mark>。` : '') : ''});
    const we = workedExample(s);
    if (we) h += `<div class="eg" style="font-size:13.5px;padding:12px 16px;margin:12px 0 0">${we}</div>`;

    // 3 消息梯子
    const cnt = {reused: 0, new: 0, stripped: 0, shifted: 0};
    reqs.forEach(r => (r.msgs || []).forEach(m => { cnt[r.kind === 'first' ? 'new' : (m.status || 'new')]++; }));
    const cells = cnt.reused + cnt.new + cnt.stripped + cnt.shifted || 1;
    h += '<h2 id="s3"><span class="no">3</span>消息梯子</h2>' + defs([
      ['格', '一个请求中的一条消息。<b>列</b>为请求，自左至右按到达时间排列；<b>行</b>为消息在 messages 数组中的位置，自上至下递增。一列为该请求发送的全部消息'],
      ['形态', '各请求重发全部历史并追加新消息，各列较前一列增加若干格，新增的格沿对角线排列。列高度下降对应 compact']]);
    h += '<dl class="key">' + ['reused', 'new', 'stripped', 'shifted'].map(k => `<dt><span class="cell" style="background:var(--${k})"></span>${STATUS_CN[k]}</dt><dd>${E(STATUS_DEF[k])}。共 <b>${N(cnt[k])}</b> 格，占 <b>${pctOf(cnt[k], cells)}</b></dd>`).join('')
      + '<dt><span class="cell" style="background:linear-gradient(90deg,var(--k-system) 25%,var(--k-user) 25% 50%,var(--k-assistant_content) 50% 75%,var(--k-tool_result) 75%)"></span>左侧色条</dt><dd>该行消息的角色：<i class="sw" style="background:var(--k-system)"></i>system <i class="sw" style="background:var(--k-user);margin-left:8px"></i>user <i class="sw" style="background:var(--k-assistant_content);margin-left:8px"></i>assistant <i class="sw" style="background:var(--k-tool_result);margin-left:8px"></i>tool</dd></dl>';
    h += `<figure><h3>${N(reqs.length)} 个请求 × ${N(s.max_msgs)} 条消息</h3><p class="read">格的尺寸随请求数与消息条数缩放，整图完整显示。交互：悬停显示该格的消息，格的边长小于 10 像素时附局部放大图；点击打开该请求并定位至该消息。缩放：拖动选取矩形区域放大到该区域；＋／－ 按钮以 1.6 倍逐级缩放；按住 Ctrl（Mac 为 ⌘）滚动鼠标滚轮以光标处为中心连续缩放；双击放大 2 倍。放大后移动显示范围：拖动画布右侧与下方的滚动条，直接滚动鼠标滚轮（横向为 Shift + 滚轮），切换到「拖动平移」后拖动画布，或按方向键；「显示全部」恢复整图。</p>`
      + '<div class="ladbar"><span id="ladinfo"></span><span class="ladbtns"><button id="ladin" data-tip="放大 1.6 倍">＋ 放大</button><button id="ladout" disabled data-tip="缩小 1.6 倍">－ 缩小</button><button id="ladreset" disabled>显示全部</button><span class="seg"><button id="ladmz" class="on" data-tip="在画布上拖出矩形，放大到该区域">框选放大</button><button id="ladmp" data-tip="放大后拖动画布移动显示范围">拖动平移</button></span></span></div><div class="ladwrap" id="ladwrap"><canvas class="base"></canvas><canvas class="ov" tabindex="0"></canvas></div>'
      + `<div class="finding"><span class="lbl">结论</span>全部 ${B(N(cells))} 格中，沿用占 ${B(pctOf(cnt.reused, cells))}，新增占 ${B(pctOf(cnt.new, cells))}` + (cnt.stripped + cnt.shifted ? `；内容未变而无法复用的（思考被模板删除、受改写影响）共 ${B(N(cnt.stripped + cnt.shifted))} 格，占 ${B(pctOf(cnt.stripped + cnt.shifted, cells))}，分布在 ${B(nL)} 个请求上` : '') + '。</div></figure>';

    // 4 逐请求
    h += '<h2 id="s4"><span class="no">4</span>逐请求</h2><p class="read">每行为一个请求；各列的定义见表头「?」的悬停说明。交互：点击一行，在表格下方展开该请求的消息表与原文。</p>';
    h += '<div class="scroll"><table id="rt"><thead><tr class="grp"><th class="g" colspan="4">请求</th><th class="g" colspan="2">本请求的 prompt</th><th class="g" colspan="4">与同线程上一请求相比</th><th class="g" colspan="4">vLLM 返回</th><th class="g" colspan="2">耗时（proxy 计时）</th><th></th></tr><tr>'
      + th('#', '请求在会话内的序号，按到达 proxy 的时间排列，自 0 起') + th('时间', '请求到达 proxy 的时刻') + th('间隔', '与本会话上一请求到达时刻之差', 'n', '秒') + th('线程', 'main：主对话。side1、side2…：旁路请求，即客户端在主对话之外发出的请求（生成标题、压缩历史），其 system 提示与主对话不同。请求仅与同一线程的上一请求比较')
      + th('消息', 'messages 数组的长度', 'n', '条') + th('token', '本请求 prompt 的 token 数。取离线分词的结果（含 golden 的请求与 vLLM 实际的 token 相同）；无分词结果时取 vLLM 返回的 prompt_tokens', 'n')
      + th('类型', '本请求的 messages 相对上一请求的变化。\n' + Object.entries(KIND_DEF).map(([k, v]) => `${KIND_CN[k]}：${v}`).join('\n')) + th('分歧原因', '本请求与上一请求的 token 序列自开头逐个比较，第一个不同的位置为分歧点；本列为分歧点处 token 不同的原因。\n' + ['append', 'template_stripped_reasoning', 'template_rewrite', 'message_changed', 'tools_changed', 'compact_like'].map(k => `${CAUSE_CN[k]}：${CAUSE_DEF[k]}`).join('\n')) + th('需重新处理', '分歧点之后的 token 数；括号内为占本请求 token 的比例', 'n', 'token') + th('模板改写', '需重新处理的 token 中，所属消息内容未变、因渲染结果变化而产生的部分', 'n', 'token')
      + th('prompt', 'prompt_tokens：vLLM 分词后的 prompt token 数', 'n', 'tokens') + th('cached', 'vLLM 前缀缓存命中的 token 数；括号内为命中率', 'n', 'token') + th('输出', '模型本次生成的 token 数', 'n', 'token') + th('结束', '模型停止生成的原因（finish_reason）')
      + th('prefill', 'proxy 发出 P 请求至收到 P 响应的时间', 'n', 'ms') + th('TTFT', '请求到达 proxy 至首个 token 的时间', 'n', 'ms') + th('核对', 'golden✓：离线分词的 token 序列与 vLLM 实际的 token 逐个相同，本行的 token 数即线上实际值。\n离线：该请求不含 golden，token 数取自离线分词，已与 prompt_tokens 核对。\n≠ prompt_tokens：离线分词的 token 数与 vLLM 返回值不同。\n≠ golden：离线分词的 token 序列与 vLLM 实际的不同。\n不含全文：该请求只有元数据。\ngold_text_only：没有分词器，token 序列与文本取自 golden，没有 token 级归属') + '</tr></thead><tbody>';
    h += reqs.map(r => { const x = recomputed(r); return `<tr class="click" data-req="${r.i}" id="req${r.i}"><td>${r.i}</td><td>${r.time.slice(11)}</td><td class="n">${N(r.gap, 2)}</td><td>${r.thread}${r.compact_for != null ? ' <span class="pill" data-tip="该请求的输出正文出现在请求 ' + r.compact_for + '（历史压缩）的消息中，即客户端用于压缩历史的请求">压缩</span>' : ''}</td><td class="n">${N(r.n_msgs)}</td><td class="n s">${N(r.tok)}</td><td data-tip="${E(KIND_DEF[r.kind] || '')}">${E(KIND_CN[r.kind] || r.kind)}</td><td data-tip="${E(CAUSE_DEF[r.cause] || '')}">${E(CAUSE_CN[r.cause] || r.cause || '')}</td><td class="n">${r.tok_new != null ? `${N(r.tok_new)} <span class="muted">(${pctOf(r.tok_new, r.tok)})</span>` : '<span class="muted">全部</span>'}</td><td class="n">${r.template_lost ? N(r.template_lost) : ''}</td><td class="n">${N(r.pt)}</td><td class="n">${r.pt != null ? `${N(r.ct || 0)} <span class="muted">(${pctOf(r.ct || 0, r.pt)})</span>` : '–'}</td><td class="n">${N(r.out_tok)}</td><td data-tip="${E(FINISH_CN[r.finish || r.status] || '')}">${E(r.finish || r.status)}</td><td class="n">${N(r.prefill_ms, 1)}</td><td class="n">${N(r.ttft_ms, 1)}</td><td>${check(r)}</td></tr>`; }).join('') + '</tbody></table></div>';
    h += '<div id="reqpanel"></div><p class="foot">由 prompt_capture/tools/session_view.py 生成。字段定义见 docs/REFERENCE.md §7.8。</p>';
    app.innerHTML = h;
    const cross = document.getElementById('areacross');
    app.querySelectorAll('#areasvg .hit').forEach(el => { el.addEventListener('mouseenter', () => { cross.setAttribute('x1', el.dataset.cx); cross.setAttribute('x2', el.dataset.cx); cross.style.opacity = .5; }); el.addEventListener('mouseleave', () => { cross.style.opacity = 0; }); });
    app.querySelectorAll('[data-req]').forEach(el => el.addEventListener('click', () => openRequest(s, +el.dataset.req, null)));
    initLadder(s);
    if (openReq != null && reqs[openReq]) showReq(s, reqs[openReq]);
  }
  function openRequest(s, i, msg) {
    pendingMsg = msg;
    const target = `#s/${encodeURIComponent(s.id)}/r/${i}`;
    if (location.hash === target) showReq(s, s.reqs[i]); else location.hash = target;
  }
  function check(r) {
    if (r.error) return `<span class="pill">${E(r.error === 'meta_only' ? '不含全文' : r.error)}</span>`;
    let s = r.tok_src === 'gold' ? '<span class="pill ok">golden✓</span>' : r.tok_src === 'offline' ? '<span class="pill">离线</span>' : r.tok_src ? `<span class="pill">${E(r.tok_src)}</span>` : '';
    if (r.match_vllm === false) s += '<span class="pill bad">≠ prompt_tokens</span>';
    if (r.match_gold === false) s += '<span class="pill bad">≠ golden</span>';
    return s;
  }

  // ---------- 消息梯子：画布绘制，整图按容器宽度缩放；悬停卡片附局部放大图；拖动选取矩形区域放大 ----------
  let ladderRedraw = null;
  window.addEventListener('resize', () => { if (ladderRedraw && document.getElementById('ladwrap')) { clearTimeout(ladderRedraw.t); ladderRedraw.t = setTimeout(ladderRedraw, 120); } });
  if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (ladderRedraw && document.getElementById('ladwrap')) ladderRedraw(); });
  function initLadder(s) {
    const reqs = s.reqs, nC0 = reqs.length, nR0 = s.max_msgs || 0, wrap = document.getElementById('ladwrap');
    if (!wrap) return;
    if (!nR0) { wrap.innerHTML = '<p class="def">该会话无消息结构数据</p>'; return; }
    const cv = wrap.querySelector('canvas.base'), ov = wrap.querySelector('canvas.ov'), info = document.getElementById('ladinfo');
    const bIn = document.getElementById('ladin'), bOut = document.getElementById('ladout'), bReset = document.getElementById('ladreset'), bMz = document.getElementById('ladmz'), bMp = document.getElementById('ladmp');
    const ST = ['reused', 'new', 'stripped', 'shifted'];
    const grid = reqs.map(r => { const a = new Int8Array(nR0).fill(-1); (r.msgs || []).forEach(m => { if (m.i < nR0) a[m.i] = r.kind === 'first' ? 1 : Math.max(0, ST.indexOf(m.status || 'new')); }); return a; });
    const rowMsg = [];  // 各行的标签：取最后一个含该行的请求中的消息
    for (let i = 0; i < nR0; i++) for (let k = nC0 - 1; k >= 0; k--) { const m = (reqs[k].msgs || [])[i]; if (m) { rowMsg[i] = m; break; } }
    // 显示范围 view 用格坐标表示，允许小数（缩放动画与连续缩放）；FULL 为整图
    const FULL = {c0: 0, c1: nC0, r0: 0, r1: nR0}, SB = 12, MINC = Math.min(3, nC0), MINR = Math.min(3, nR0);
    let view = {...FULL}, goal = null, L = null, cur = null, drag = null, mode = 'zoom', anim = null;  // goal：动画进行中的目标范围
    const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    const nCv = () => view.c1 - view.c0, nRv = () => view.r1 - view.r0;
    const isZoomed = () => nCv() < nC0 - 1e-6 || nRv() < nR0 - 1e-6;
    // 画布尺寸只取决于整图：格宽最大 30 像素、行高最大 16 像素且格区总高不超过 640 像素。放大不改变画布，只改变格的尺寸；
    // 放大后在格区右侧与下方画出滚动条
    function layout() {
      const W = Math.max(320, wrap.clientWidth), nC = nCv(), nR = nRv();
      const ch0 = Math.min(16, Math.max(640 / nR0, 1)), gridH = Math.ceil(ch0 * nR0), top = 24;
      const ch = gridH / nR, wide = ch >= 11, gut = wide ? 184 : 64;
      const gridW = Math.max(60, Math.min(30 * nC0, W - gut - 2 - SB - 4)), cw = gridW / nC;
      L = {W: Math.ceil(gut + gridW + 2 + SB + 4), H: Math.ceil(top + gridH + 2 + SB + 4), gut, top, cw, ch, nC, nR, wide, gridW, gridH, small: cw < 10 || ch < 10, zoomed: isZoomed()};
    }
    const X = c => L.gut + (c - view.c0) * L.cw, Y = r => L.top + (r - view.r0) * L.ch;
    const hsb = () => ({x: L.gut, y: L.top + L.gridH + 4, w: L.gridW, h: SB, tx: L.gut + L.gridW * view.c0 / nC0, tw: Math.max(18, L.gridW * nCv() / nC0)});
    const vsb = () => ({x: L.gut + L.gridW + 4, y: L.top, w: SB, h: L.gridH, ty: L.top + L.gridH * view.r0 / nR0, th: Math.max(18, L.gridH * nRv() / nR0)});
    const rr = (g, x, y, w, h, r) => { g.beginPath(); if (g.roundRect) g.roundRect(x, y, w, h, r); else g.rect(x, y, w, h); g.fill(); };
    function draw() {
      layout();
      const dpr = window.devicePixelRatio || 1, snap = v => Math.round(v * dpr) / dpr;
      for (const c of [cv, ov]) { c.width = Math.round(L.W * dpr); c.height = Math.round(L.H * dpr); c.style.width = L.W + 'px'; c.style.height = L.H + 'px'; }
      const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, L.W, L.H);
      const C = ST.map(k => css('--' + k)), RC = {system: css('--k-system'), developer: css('--k-system'), user: css('--k-user'), assistant: css('--k-assistant_content'), tool: css('--k-tool_result')};
      const gx = L.cw >= 9 ? 2 : 0, gy = L.ch >= 9 ? 2 : 0, rad = L.cw >= 10 && L.ch >= 10 && g.roundRect ? 3 : 0;  // 格的边长不小于 9 像素时留间隙；更小时间隙会形成条纹
      const cA = Math.max(0, Math.floor(view.c0)), cB = Math.min(nC0, Math.ceil(view.c1)), rA = Math.max(0, Math.floor(view.r0)), rB = Math.min(nR0, Math.ceil(view.r1));
      g.save(); g.beginPath(); g.rect(L.gut, L.top, L.gridW, L.gridH); g.clip();
      for (let c = cA; c < cB; c++) {
        const a = grid[c], x0 = snap(X(c)), x1 = snap(X(c + 1)) - gx;
        let r = rA;
        while (r < rB) {
          const st = a[r];
          if (st < 0) { r++; continue; }
          let e = r + 1;
          if (!gy) while (e < rB && a[e] === st) e++;
          const y0 = snap(Y(r)), y1 = snap(Y(e)) - gy, w = Math.max(x1 - x0, 1 / dpr), hh = Math.max(y1 - y0, 1 / dpr);
          g.fillStyle = C[st];
          if (rad) { g.beginPath(); g.roundRect(x0, y0, w, hh, rad); g.fill(); } else g.fillRect(x0, y0, w, hh);
          r = e;
        }
      }
      g.restore();
      // 上方：请求序号；旁路请求在序号下方以灰点标出
      g.font = '10.5px -apple-system,"PingFang SC",system-ui,sans-serif'; g.textBaseline = 'alphabetic';
      const fg2 = css('--fg2'), fg3 = css('--fg3');
      const sc = niceStep(30 / L.cw);
      g.save(); g.beginPath(); g.rect(L.gut - 4, 0, L.gridW + 8, L.top); g.clip(); g.textAlign = 'center';
      for (let c = cA; c < cB; c++) {
        if (c % sc === 0) { g.fillStyle = fg2; g.fillText(String(c), X(c) + L.cw / 2, 11); }
        if (reqs[c].thread !== 'main') { g.fillStyle = fg3; g.fillRect(X(c) + L.cw / 2 - 1.5, 15, 3, 3); }
      }
      g.restore();
      g.textAlign = 'left'; g.fillStyle = fg3; g.fillText('请求', 4, 11);
      // 左侧：角色色条与行号；行高不小于 11 像素时标注角色与工具名
      const sr = niceStep(15 / L.ch);
      g.save(); g.beginPath(); g.rect(0, L.top - 1, L.gut, L.gridH + 2); g.clip(); g.textAlign = 'right';
      for (let r = rA; r < rB; r++) {
        const m = rowMsg[r];
        if (m) { g.fillStyle = RC[m.role] || fg3; g.fillRect(L.gut - 8, snap(Y(r)), 4, Math.max(snap(Y(r + 1)) - snap(Y(r)) - gy, 1 / dpr)); }
        g.fillStyle = fg2;
        if (L.wide) { let t = `${r}  ${m ? m.role + (m.tool ? ' · ' + m.tool : '') : ''}`; while (g.measureText(t).width > L.gut - 18 && t.length > 4) t = t.slice(0, -2) + '…'; g.fillText(t, L.gut - 13, Y(r) + L.ch / 2 + 3.5); }
        else if (r % sr === 0) g.fillText(String(r), L.gut - 13, Y(r) + Math.min(L.ch, 10) / 2 + 3.5);
      }
      g.restore();
      g.save(); g.translate(10, L.top + 40); g.rotate(-Math.PI / 2); g.textAlign = 'right'; g.fillStyle = fg3; if (!L.wide) g.fillText('消息位置', 0, 0); g.restore();
      if (L.zoomed) {  // 滚动条：轨道 + 滑块，滑块长度与显示范围占整图的比例一致
        const h_ = hsb(), v_ = vsb();
        g.fillStyle = css('--bg3'); rr(g, h_.x, h_.y, h_.w, h_.h, SB / 2); rr(g, v_.x, v_.y, v_.w, v_.h, SB / 2);
        g.fillStyle = css('--fg3'); rr(g, h_.tx, h_.y + 2, h_.tw, SB - 4, (SB - 4) / 2); rr(g, v_.x + 2, v_.ty, SB - 4, v_.th, (SB - 4) / 2);
      }
      info.innerHTML = `显示范围：请求 <b>${Math.floor(view.c0)}–${Math.ceil(view.c1) - 1}</b>（共 ${nC0} 个），消息 <b>${Math.floor(view.r0)}–${Math.ceil(view.r1) - 1}</b>（共 ${nR0} 条）`
        + (L.zoomed ? ` · 放大 <b>${(nC0 / nCv()).toFixed(1)}×</b>（横）/ <b>${(nR0 / nRv()).toFixed(1)}×</b>（纵）` : '');
      bReset.disabled = bOut.disabled = !L.zoomed;
      bIn.disabled = nCv() <= MINC + 1e-6 && nRv() <= MINR + 1e-6;
      overlay();
    }
    function overlay() {
      const dpr = window.devicePixelRatio || 1, g = ov.getContext('2d');
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, L.W, L.H);
      const fg = css('--fg');
      if (drag && drag.kind === 'zoom' && drag.moved) {
        const x0 = Math.min(drag.x0, drag.x1), y0 = Math.min(drag.y0, drag.y1), w = Math.abs(drag.x1 - drag.x0), hh = Math.abs(drag.y1 - drag.y0);
        g.globalAlpha = .12; g.fillStyle = css('--accent'); g.fillRect(x0, y0, w, hh); g.globalAlpha = 1; g.strokeStyle = css('--accent'); g.lineWidth = 1.5; g.strokeRect(x0, y0, w, hh);
        return;
      }
      if (!cur) return;
      g.save(); g.beginPath(); g.rect(0, 0, L.gut + L.gridW, L.top + L.gridH); g.clip();
      g.globalAlpha = .1; g.fillStyle = fg;
      g.fillRect(X(cur.c), 0, Math.max(L.cw, 1), L.top + L.gridH); g.fillRect(L.gut - 10, Y(cur.r), L.gridW + 10, Math.max(L.ch, 1));
      g.globalAlpha = 1; g.strokeStyle = fg; g.lineWidth = 1.5;
      g.strokeRect(X(cur.c) - .5, Y(cur.r) - .5, Math.max(L.cw, 3), Math.max(L.ch, 3));
      g.restore();
    }
    function clampView(v) {
      const nC = Math.min(nC0, Math.max(MINC, v.c1 - v.c0)), nR = Math.min(nR0, Math.max(MINR, v.r1 - v.r0));
      const c0 = Math.max(0, Math.min(nC0 - nC, v.c0)), r0 = Math.max(0, Math.min(nR0 - nR, v.r0));
      return {c0, c1: c0 + nC, r0, r1: r0 + nR};
    }
    function setView(v, animate) {
      const to = clampView(v);
      if (anim) { cancelAnimationFrame(anim); anim = null; }
      goal = to;
      if (!animate) { view = to; draw(); return; }
      const from = {...view}, t0 = performance.now(), dur = 220;
      const step = now => {
        const t = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - t, 3);
        view = {c0: from.c0 + (to.c0 - from.c0) * e, c1: from.c1 + (to.c1 - from.c1) * e, r0: from.r0 + (to.r0 - from.r0) * e, r1: from.r1 + (to.r1 - from.r1) * e};
        draw();
        anim = t < 1 ? requestAnimationFrame(step) : null;
      };
      anim = requestAnimationFrame(step);
    }
    function zoomAt(f, cc, rc, animate) {  // f > 1 放大；(cc, rc) 为缩放中心的格坐标，缩放后仍在画布上的同一位置。动画进行中以其目标范围为基准，连续点击可累加
      const b = anim && goal ? goal : view, bC = b.c1 - b.c0, bR = b.r1 - b.r0;
      const nC = bC / f, nR = bR / f, fx = (cc - b.c0) / bC, fy = (rc - b.r0) / bR;
      setView({c0: cc - fx * nC, c1: cc - fx * nC + nC, r0: rc - fy * nR, r1: rc - fy * nR + nR}, animate);
    }
    const center = () => { const b = anim && goal ? goal : view; return [(b.c0 + b.c1) / 2, (b.r0 + b.r1) / 2]; };
    function at(e) {
      const b = ov.getBoundingClientRect(), x = e.clientX - b.left, y = e.clientY - b.top;
      const fc = view.c0 + (x - L.gut) / L.cw, fr = view.r0 + (y - L.top) / L.ch, c = Math.floor(fc), r = Math.floor(fr);
      const h_ = hsb(), v_ = vsb();
      return {x, y, fc, fr, c: Math.min(nC0 - 1, Math.max(0, c)), r: Math.min(nR0 - 1, Math.max(0, r)),
        inside: x >= L.gut && x < L.gut + L.gridW && y >= L.top && y < L.top + L.gridH && c >= 0 && c < nC0 && r >= 0 && r < nR0,
        onH: L.zoomed && y >= h_.y && y <= h_.y + h_.h && x >= h_.x && x <= h_.x + h_.w,
        onV: L.zoomed && x >= v_.x && x <= v_.x + v_.w && y >= v_.y && y <= v_.y + v_.h};
    }
    function cellTip(c, r) {
      const rq = reqs[c], m = (rq.msgs || [])[r], st = grid[c][r] < 0 ? null : ST[grid[c][r]];
      let h = `<div class="th">请求 ${c} · 消息 ${r}<small>${E(KIND_CN[rq.kind] || rq.kind)}${rq.thread !== 'main' ? ' · 旁路请求' : ''}</small></div>`;
      if (m) {
        h += `<div style="margin:3px 0"><i class="sw" style="background:${ROLE_C[m.role] || 'var(--fg3)'}"></i><b>${E(m.role)}</b>${m.tool ? ' · ' + E(m.tool) : ''}<span class="pill ${st}">${STATUS_CN[st]}</span> <b>${N(msz(m, rq))}</b> ${ru(rq)}</div>`;
        h += `<div class="hint" style="margin-top:0;color:var(--fg2)">${E(STATUS_DEF[st])}</div><div class="pvw">${vis(m.preview, 220)}</div>`;
      } else h += `<div class="hint" style="color:var(--fg2)">请求 ${c} 含 ${N((rq.msgs || []).length)} 条消息，位置 ${r} 无消息</div>`;
      if (L.small) {
        const hc = 6, hr = 4, c0 = Math.max(0, Math.min(nC0 - 2 * hc - 1, c - hc)), c1 = Math.min(nC0, c0 + 2 * hc + 1), r0 = Math.max(0, Math.min(nR0 - 2 * hr - 1, r - hr)), r1 = Math.min(nR0, r0 + 2 * hr + 1);
        h += `<div class="hint">局部放大：请求 ${c0}–${c1 - 1}，消息 ${r0}–${r1 - 1}</div><div class="lens" style="grid-template-columns:max-content repeat(${c1 - c0},13px)"><span></span>`;
        for (let cc = c0; cc < c1; cc++) h += `<span class="c">${cc === c || cc === c0 || cc === c1 - 1 ? cc : ''}</span>`;
        for (let rr_ = r0; rr_ < r1; rr_++) {
          const mm = rowMsg[rr_];
          h += `<span${rr_ === r ? ' style="color:var(--fg);font-weight:650"' : ''}>${rr_} ${mm ? E(mm.role) : ''}</span>`;
          for (let cc = c0; cc < c1; cc++) { const v = grid[cc][rr_]; h += `<i${cc === c && rr_ === r ? ' class="cur"' : ''} style="background:${v < 0 ? 'transparent' : `var(--${ST[v]})`}"></i>`; }
        }
        h += '</div>';
      }
      return h + (m ? `<div class="hint">点击打开请求 ${c} 并定位至该消息；${mode === 'pan' && L.zoomed ? '拖动以平移' : '拖动选取区域以放大'}；双击放大 2 倍</div>` : '');
    }
    ov.addEventListener('mousemove', e => {
      if (drag) return;
      const p = at(e);
      ov.style.cursor = p.onH || p.onV ? 'default' : (p.inside ? (mode === 'pan' && L.zoomed ? 'grab' : 'crosshair') : 'default');
      if (!p.inside) { cur = null; hideTip(); overlay(); return; }
      if (!cur || cur.c !== p.c || cur.r !== p.r) { cur = {c: p.c, r: p.r}; overlay(); showTipHtml(cellTip(p.c, p.r), e); } else placeTip(e);
    });
    ov.addEventListener('mouseleave', () => { if (!drag) { cur = null; hideTip(); overlay(); } });
    ov.addEventListener('mousedown', e => {
      if (e.button !== 0) return;
      const p = at(e);
      if (p.onH) {  // 水平滚动条：按在滑块上拖动；按在轨道上翻一屏
        const h_ = hsb();
        if (p.x >= h_.tx && p.x <= h_.tx + h_.tw) drag = {kind: 'hs', x0: p.x, v0: {...view}};
        else { const d = (p.x < h_.tx ? -1 : 1) * nCv() * .8; setView({c0: view.c0 + d, c1: view.c1 + d, r0: view.r0, r1: view.r1}, true); }
        hideTip(); e.preventDefault(); return;
      }
      if (p.onV) {
        const v_ = vsb();
        if (p.y >= v_.ty && p.y <= v_.ty + v_.th) drag = {kind: 'vs', y0: p.y, v0: {...view}};
        else { const d = (p.y < v_.ty ? -1 : 1) * nRv() * .8; setView({c0: view.c0, c1: view.c1, r0: view.r0 + d, r1: view.r1 + d}, true); }
        hideTip(); e.preventDefault(); return;
      }
      if (!p.inside) return;
      if (mode === 'pan' && L.zoomed) { drag = {kind: 'pan', x0: p.x, y0: p.y, v0: {...view}}; ov.style.cursor = 'grabbing'; cur = null; hideTip(); overlay(); }
      else drag = {kind: 'zoom', x0: p.x, y0: p.y, x1: p.x, y1: p.y, c0: p.c, r0: p.r, c1: p.c, r1: p.r, moved: false};
      ov.focus({preventScroll: true});
      e.preventDefault();
    });
    // 鼠标可能在画布之外移动或释放，因此在 window 上监听
    const onMove = e => {
      if (!drag) return;
      const p = at(e);
      if (drag.kind === 'hs') { const dc = (p.x - drag.x0) / L.gridW * nC0; setView({c0: drag.v0.c0 + dc, c1: drag.v0.c1 + dc, r0: view.r0, r1: view.r1}, false); }
      else if (drag.kind === 'vs') { const dr = (p.y - drag.y0) / L.gridH * nR0; setView({c0: view.c0, c1: view.c1, r0: drag.v0.r0 + dr, r1: drag.v0.r1 + dr}, false); }
      else if (drag.kind === 'pan') { const dc = -(p.x - drag.x0) / L.cw, dr = -(p.y - drag.y0) / L.ch; setView({c0: drag.v0.c0 + dc, c1: drag.v0.c1 + dc, r0: drag.v0.r0 + dr, r1: drag.v0.r1 + dr}, false); }
      else {
        drag.x1 = Math.max(L.gut, Math.min(L.gut + L.gridW, p.x)); drag.y1 = Math.max(L.top, Math.min(L.top + L.gridH, p.y)); drag.c1 = p.c; drag.r1 = p.r;
        if (Math.abs(drag.x1 - drag.x0) > 6 || Math.abs(drag.y1 - drag.y0) > 6) drag.moved = true;
        if (drag.moved) { hideTip(); overlay(); }
      }
    };
    const onUp = e => {
      if (!document.body.contains(ov)) { window.removeEventListener('mouseup', onUp); window.removeEventListener('mousemove', onMove); return; }
      const d = drag; drag = null;
      if (!d) return;
      if (d.kind === 'zoom') {
        if (d.moved) {
          let c0 = Math.min(d.c0, d.c1), c1 = Math.max(d.c0, d.c1) + 1, r0 = Math.min(d.r0, d.r1), r1 = Math.max(d.r0, d.r1) + 1;
          cur = null; setView({c0, c1, r0, r1}, true);
        } else { const p = at(e); if (p.inside && e.target === ov) { hideTip(); openRequest(s, p.c, (reqs[p.c].msgs || [])[p.r] ? p.r : null); } else overlay(); }
      } else { ov.style.cursor = ''; overlay(); }
    };
    window.addEventListener('mouseup', onUp);
    window.addEventListener('mousemove', onMove);
    // 滚轮：Ctrl / ⌘ + 滚轮以光标处为中心缩放；放大后的普通滚轮移动显示范围（Shift + 滚轮为横向）；整图状态下不拦截页面滚动
    ov.addEventListener('wheel', e => {
      const p = at(e);
      if (e.ctrlKey || e.metaKey) { e.preventDefault(); if (p.inside) { hideTip(); zoomAt(Math.exp(-e.deltaY * 0.01), p.fc, p.fr, false); } return; }
      if (!L.zoomed) return;
      e.preventDefault(); hideTip(); cur = null;
      const dx = e.shiftKey ? e.deltaY : e.deltaX, dy = e.shiftKey ? 0 : e.deltaY;
      setView({c0: view.c0 + dx / L.cw, c1: view.c1 + dx / L.cw, r0: view.r0 + dy / L.ch, r1: view.r1 + dy / L.ch}, false);
    }, {passive: false});
    ov.addEventListener('dblclick', e => { const p = at(e); if (p.inside) { hideTip(); zoomAt(2, p.fc, p.fr, true); } e.preventDefault(); });
    ov.addEventListener('keydown', e => {
      const k = e.key, sc = nCv() * .1, sr = nRv() * .1;
      const pan = (dc, dr) => setView({c0: view.c0 + dc, c1: view.c1 + dc, r0: view.r0 + dr, r1: view.r1 + dr}, true);
      if (k === 'ArrowLeft') pan(-sc, 0); else if (k === 'ArrowRight') pan(sc, 0); else if (k === 'ArrowUp') pan(0, -sr); else if (k === 'ArrowDown') pan(0, sr);
      else if (k === '+' || k === '=') zoomAt(1.6, ...center(), true); else if (k === '-') zoomAt(1 / 1.6, ...center(), true); else if (k === '0') setView(FULL, true);
      else return;
      e.preventDefault();
    });
    bIn.addEventListener('click', () => zoomAt(1.6, ...center(), true));
    bOut.addEventListener('click', () => zoomAt(1 / 1.6, ...center(), true));
    bReset.addEventListener('click', () => setView(FULL, true));
    const setMode = m => { mode = m; bMz.classList.toggle('on', m === 'zoom'); bMp.classList.toggle('on', m === 'pan'); };
    bMz.addEventListener('click', () => setMode('zoom'));
    bMp.addEventListener('click', () => setMode('pan'));
    ladderRedraw = draw;
    draw();
  }

  // ---------- 展开单个请求 ----------
  let pendingMsg = null;  // 自梯子的格进入时需高亮的消息序号
  function msgText(r, i, limit) {
    // 单条消息的原文：按顺序拼接其非标记片段；原文未嵌入时使用采集时保存的预览
    let out = [], n = 0;
    for (const sg of r.segs || []) { if (sg.m !== i || sg.k === 'markup') continue; const t = D.texts[sg.b]; if (t == null) continue; out.push((out.length ? `\n── ${KL[sg.k]} ──\n` : '') + t); n += t.length; if (n > limit) break; }
    const t = out.join('');
    return t ? (t.length > limit ? t.slice(0, limit) + `\n…（已截断；全文见下方「模板后文本」）` : t) : null;
  }
  let outParts = [];  // 当前展开请求的模型输出各部分全文（供「显示其余」按钮使用）
  function echoText(r, reqs) {
    if (!r.out) return r.out_tok != null || r.out_chars != null ? '输出全文未采集（采集配置的 output / follow_output 不为 text），仅有输出统计。' : '';
    const oe = r.out_echo, nx = reqs.slice(r.i + 1).find(q => q.thread === r.thread);
    if (!oe) return nx ? `回传核对：同线程下一请求（请求 ${nx.i}，${E(KIND_CN[nx.kind] || nx.kind)}）不是追加类型或不含全文，未比较。` : '回传核对：本请求为同线程最后一个请求，无下一请求可比较。';
    const st = v => v === true ? '<b>相同</b>' : v === false ? '<b>不同或未回传</b>' : '输出中无此项';
    return `回传核对：同线程下一请求（请求 ${oe.next}）${oe.msg != null ? `新增的 assistant 消息（消息 ${oe.msg}）` : '中未找到新增的 assistant 消息'}与本输出逐项比较：正文 ${st(oe.content)}，思考 ${st(oe.reasoning)}${oe.reasoning_sent === false ? '（客户端未回传思考）' : ''}，工具调用 ${st(oe.tool_calls)}。`;
  }
  function showReq(s, r) {
    const panel = document.getElementById('reqpanel'), reqs = s.reqs, sid = encodeURIComponent(s.id);
    app.querySelectorAll('#rt tr').forEach(tr => tr.classList.toggle('sel', tr.dataset.req == r.i));
    const bk = bkOf(r), btot = bkTot(r) || 1, u = ru(r), x = recomputed(r), pv = prevInThread(reqs, r);
    tipFns.msg = i => { const m = r.msgs[+i], t = msgText(r, +i, 1500); return `<div class="th">消息 ${m.i}<small><i class="sw" style="background:${ROLE_C[m.role] || 'var(--fg3)'}"></i>${E(m.role)}${m.tool ? ' · ' + E(m.tool) : ''} · ${N(msz(m, r))} ${u}</small></div><div class="pvw" style="max-height:60vh">${E(t != null ? t : m.preview.replace(/⏎/g, '\n'))}</div><div class="hint">点击该行，定位至下方「模板后文本」中的该消息</div>`; };
    let h = `<h2><span>请求 ${r.i}</span><span class="sub">${r.time} · ${E(KIND_CN[r.kind] || r.kind)}${r.cause ? ' · ' + E(CAUSE_CN[r.cause] || r.cause) : ''}${r.thread !== 'main' ? ' · 旁路请求 ' + E(r.thread) : ''}</span></h2>`;
    const dl = [['参数', E(Object.entries(r.params || {}).map(([k, v]) => k + '=' + JSON.stringify(v).slice(0, 60)).join('  ')) || '–'],
      ['大小', `请求体 <b>${N(r.body_bytes)}</b> 字节；工具定义 <b>${N(r.n_tools)}</b> 个，共 ${N(r.tools_bytes)} 字节；模板后文本 <b>${N(r.text_chars)}</b> 字符`],
      ['vLLM 返回', `prompt_tokens <b>${N(r.pt)}</b>，cached_tokens <b>${N(r.ct)}</b>${r.pt ? `，命中率 <b>${pctOf(r.ct || 0, r.pt)}</b>` : ''}；输出 <b>${N(r.out_tok)}</b> token（正文 ${N(r.out_chars)} 字符，思考 ${N(r.out_reasoning)} 字符，工具调用 ${N(r.out_calls)} 个）；结束原因 ${E(r.finish || r.status)}${FINISH_CN[r.finish || r.status] ? '（' + FINISH_CN[r.finish || r.status] + '）' : ''}`],
      ['耗时', `proxy 计时：prefill <b>${N(r.prefill_ms, 1)}</b> ms，TTFT <b>${N(r.ttft_ms, 1)}</b> ms，端到端 <b>${N(r.e2e_ms, 1)}</b> ms。本机离线：模板渲染 ${N(r.render_ms, 1)} ms，分词 ${N(r.encode_ms, 1)} ms`],
      ['核对', check(r) || '–']];
    if (r.kind !== 'first' && r.prefix_msgs != null) {
      dl.push([`与请求 ${pv ? pv.i : '–'} 相比`, `消息：前 <b>${r.prefix_msgs}</b> 条相同，新增 <b>${r.new_msgs}</b> 条（${N(r.new_chars)} 字符）` + (r.tok_prefix != null ? `。token：公共前缀 <b>${N(r.tok_prefix)}</b> 个，需重新处理 <b>${N(r.tok_new)}</b> 个，占 <b>${pctOf(r.tok_new, r.tok)}</b>` + (r.diverge_kind ? `；分歧点位于消息 ${r.diverge_msg} 的 ${E(KL[r.diverge_kind] || r.diverge_kind)}，${r.diverge_prev_kind ? '上一请求同一位置为 ' + E(KL[r.diverge_prev_kind] || r.diverge_prev_kind) : '上一请求的 token 序列在此结束'}` : '') : '') + (r.template_lost ? `。<mark>其中 <b>${N(r.template_lost)}</b> 个 token 所属消息内容未变，由模板改写历史造成</mark>` : '') + (r.tools_changed ? '。工具定义发生变更' : '')]);
    }
    if (r.dropped && r.dropped.length) dl.push(['未渲染的片段', '客户端已发送、模板未渲染进 prompt 的片段：' + r.dropped.map(d => `消息 ${d.msg} 的 ${KL[d.kind] || d.kind}（${N(d.chars)} 字符）`).join('，')]);
    if (r.approx && r.approx.length) dl.push(['内容被模板改写的片段', '原文在模板后文本中不能整段找到，按首尾定位：' + r.approx.map(d => `消息 ${d.msg} 的 ${KL[d.kind] || d.kind}（原文 ${N(d.chars)} 字符，渲染后 ${N(d.rendered_chars)} 字符）`).join('，')]);
    if (r.unattributed) dl.push(['未归属的文本', `<mark>${N(r.unattributed)} 字符的文本未能对应到任何消息</mark>，在下方「模板后文本」中以黄色标出并计入模板标记。模板标记片段不会超过 200 字符，出现这种片段说明模板对消息内容做了改写，或该消息在请求体里的形式与页面的定位方法不匹配；请用 tools/dump_requests.py 对照原始请求体。`]);
    if (r.compact_from && r.compact_from.length) dl.push(['历史压缩', r.compact_from.map(cf => `上一主线程请求之后、本请求之前的旁路请求 <a href="#s/${sid}/r/${cf.req}">请求 ${cf.req}</a>（${N(cf.n_msgs)} 条消息，prompt ${N(cf.tok)} token${cf.out_chars != null ? `，输出正文 ${N(cf.out_chars)} 字符` : '，输出全文未采集'}）；其输出正文${cf.msg != null ? `出现在本请求的消息 <b>${cf.msg}</b> 中：该旁路请求即客户端的压缩请求，其输出为压缩后的历史` : '未出现在本请求的消息中'}`).join('。') + '。']);
    if (r.compact_for != null) dl.push(['用途', `本请求的输出正文出现在 <a href="#s/${sid}/r/${r.compact_for}">请求 ${r.compact_for}</a>（历史压缩）的消息中：本请求为客户端的压缩请求，其输出为压缩后的历史`]);
    h += '<dl class="meta reqmeta">' + dl.map(([k, v]) => `<dt>${E(k)}</dt><dd>${v}</dd>`).join('') + '</dl>';
    if (r.tok && sumOf(bk)) h += fig(`prompt 的来源构成，共 ${N(btot)} ${u}${u === '字符' ? `（prompt 共 ${N(r.tok)} token；无 token 级归属，按字符划分）` : ''}`, compBar(bk, btot) + '<div class="legend">' + KINDS.filter(k => bk[k]).map(k => `<span><i style="background:${KC(k)}"></i>${KL[k]} ${N(bk[k])} ${u} · <b style="color:var(--fg)">${pctOf(bk[k], btot)}</b></span>`).join('') + '</div>');
    // 模型输出
    outParts = [];
    if (r.out) {
      const o = r.out, parts = [];
      if (o.reasoning) parts.push(['assistant.reasoning', '思考（reasoning_content）', o.reasoning]);
      if (o.content) parts.push(['assistant.content', '正文（content）', o.content]);
      (o.tool_calls || []).forEach(c => parts.push(['assistant.tool_call', `工具调用 ${c.name || ''}`, typeof c.arguments === 'string' ? c.arguments : JSON.stringify(c.arguments || '')]));
      h += `<h3>模型输出</h3><p class="read">D 实例返回给客户端的内容，取自 proxy 记录的输出全文；结束原因 ${E(r.finish || r.status)}，${N(r.out_tok)} token。${echoText(r, reqs)}</p>`;
      h += '<div class="prompt outcards">' + (parts.length ? parts.map(([k, t, txt], j) => { outParts.push(txt); const long = txt.length > SEG_LIMIT; return `<div class="pc" style="--c:${KC(k)}"><div class="pch" style="cursor:default"><span class="role">${E(t)}</span><span class="tk"><b>${N(txt.length)}</b> 字符</span></div><div class="pcb"><span>${E(long ? txt.slice(0, SEG_HEAD) : txt)}</span>${long ? `<button class="xp" data-o="${j}">显示其余 ${N(txt.length - SEG_HEAD)} 字符</button>` : ''}</div></div>`; }).join('') : '<p class="def">输出为空。</p>') + '</div>';
    } else if (r.out_tok != null || r.out_chars != null) {
      h += `<h3>模型输出</h3><p class="read">${echoText(r, reqs)}${r.out_chars != null ? ` proxy 记录的输出统计：正文 ${N(r.out_chars)} 字符，思考 ${N(r.out_reasoning)} 字符，工具调用 ${N(r.out_calls)} 个，${N(r.out_tok)} token。` : ''}</p>`;
    }
    if (r.msgs && r.msgs.length) {
      const stc = {}; r.msgs.forEach(m => { const k = r.kind === 'first' ? 'new' : (m.status || 'new'); stc[k] = (stc[k] || 0) + 1; });
      h += '<h3>消息表</h3><p class="read">每行为本请求 messages 数组中的一条消息，按数组顺序排列。各列的定义见表头「?」的悬停说明' + (r.tok_attr ? '' : '；本请求没有 token 级归属，大小按字符计') + '。各状态的消息条数：'
        + Object.entries(stc).map(([k, v]) => `<span class="pill ${k}" style="margin-left:0">${STATUS_CN[k]}</span> <b>${v}</b> 条`).join('，') + '。交互：悬停「预览」显示该消息的内容；点击一行，定位至下方「模板后文本」中的该消息。</p>';
      h += `<div class="scroll"><table id="mt"><thead><tr class="grp"><th></th><th></th><th></th><th></th><th class="g" colspan="4">${uw('的', u)}构成</th><th></th><th></th></tr><tr>` + th('#', '消息在 messages 数组中的位置，自 0 起') + th('角色', 'system：系统提示；user：用户消息；assistant：模型此前的输出；tool：工具结果') + th('工具', 'tool 消息：返回该结果的工具。assistant 消息：该消息调用的工具') + th(u, `该消息在 prompt 中的 ${uw('数', u)}，等于正文、思考、调用、标记四列之和。括号内为占本请求 ${uw('的', u)}比例`, 'n')
        + th('正文', `消息 content 字段的 ${uw('数', u)}：user 与 system 的文本、assistant 的回复正文、工具结果的内容`, 'n') + th('思考', `assistant 消息的思考内容（reasoning_content）的 ${uw('数', u)}。「已删除」表示客户端已发送思考、模板未将其渲染进 prompt，计为 0`, 'n') + th('调用', `assistant 消息的工具调用的 ${uw('数', u)}：函数名与各参数值`, 'n') + th('标记', `聊天模板为该消息插入的控制标记的 ${uw('数', u)}，如 <|user|>、<tool_call>、<arg_key>；不属于消息内容`, 'n') + th('状态', '相对上一请求同一位置的消息。\n' + Object.keys(STATUS_CN).map(k => `${STATUS_CN[k]}：${STATUS_DEF[k]}`).join('\n')) + th('预览', '消息内容的开头部分；⏎ 表示换行') + '</tr></thead><tbody>'
        + r.msgs.map(m => { const st = r.kind === 'first' ? 'new' : (m.status || 'new'), sz = msz(m, r), pr = mparts(m, r); return `<tr id="msg${m.i}" class="click" data-msg="${m.i}"><td>${m.i}</td><td><i class="sw" style="background:${ROLE_C[m.role] || 'var(--fg3)'}"></i>${E(m.role)}</td><td>${E(m.tool || '')}</td><td class="n s">${N(sz)} <span class="muted" style="font-weight:400">(${pctOf(sz, btot)})</span></td><td class="n">${N(pr[0])}</td><td class="n">${N(pr[1])}${m.reasoning_dropped ? '<span class="pill stripped">已删除</span>' : ''}</td><td class="n">${N(pr[2])}</td><td class="n">${N(pr[3])}</td><td><span class="pill ${st}" style="margin-left:0" data-tip="${E(STATUS_DEF[st])}">${STATUS_CN[st] || st}</span></td><td class="pv" data-tipf="msg:${m.i}">${E(m.preview.slice(0, 110))}</td></tr>`; }).join('') + '</tbody></table></div>';
    }
    if (r.segs && r.segs.length && s.text_embedded) {
      h += '<h3>模板后文本，按来源着色</h3><p class="read">聊天模板对本请求渲染所得的完整 prompt，即分词器的输入。每张卡片为一条消息，或模板开头、一个工具定义、生成提示；卡片左侧的色标标明来源，灰底文字为模板插入的控制标记。交互：点击卡片的标题行折叠或展开该卡片；右侧控制栏按类型折叠或展开全部同类卡片，其下的色块与卡片一一对应，点击色块展开或折叠对应的卡片并定位到它；「折叠公共前缀」把与上一请求相同的连续卡片收成一行。</p>'
        + legend(KINDS.filter(k => bk[k] && k !== 'markup'));
      h += '<div class="pwrap"><div class="prompt" id="ptxt">' + promptHtml(r) + '</div>' + railHtml(r) + '</div>';
    } else if (r.segs && r.segs.length) {
      h += '<p class="def">原文未嵌入（超出 --embed-mb）；原文见该会话的 md 文件。</p>';
    }
    panel.innerHTML = h;
    const only = document.getElementById('onlynew'), mk = document.getElementById('showmk'), pt = document.getElementById('ptxt'), rail = document.getElementById('prail');
    if (only) only.addEventListener('change', () => { pt.classList.toggle('all', !only.checked); syncMini(); });
    if (mk) mk.addEventListener('change', () => pt.classList.toggle('nomk', !mk.checked));
    if (pt) pt.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (b) {
        if (b.dataset.fold != null) { const body = b.closest('.fold').nextElementSibling; const open = body.classList.toggle('open'); b.textContent = open ? '收起' : '展开'; syncMini(); }
        else if (b.dataset.b) { const t = D.texts[b.dataset.b] || ''; b.previousElementSibling.textContent = t; b.remove(); }
        return;
      }
      const hd = e.target.closest('.pch');
      if (hd && !e.target.closest('.pill')) { hd.parentElement.classList.toggle('closed'); syncMini(); }
    });
    const oc = panel.querySelector('.outcards');
    if (oc) oc.addEventListener('click', e => { const b = e.target.closest('button[data-o]'); if (b) { b.previousElementSibling.textContent = outParts[+b.dataset.o]; b.remove(); } });
    if (rail) {
      rail.addEventListener('change', e => { const inp = e.target.closest('input[data-type]'); if (!inp) return; pt.querySelectorAll(`.pc[data-type="${inp.dataset.type}"]`).forEach(c => c.classList.toggle('closed', !inp.checked)); syncMini(); });
      rail.addEventListener('click', e => {
        const b = e.target.closest('button[data-all]');
        if (b) {
          const open = b.dataset.all === 'open';
          pt.querySelectorAll('.pc').forEach(c => c.classList.toggle('closed', !open));
          if (open) pt.querySelectorAll('.foldbody').forEach(body => { body.classList.add('open'); const fb = body.previousElementSibling.querySelector('button'); if (fb) fb.textContent = '收起'; });
          rail.querySelectorAll('input[data-type]').forEach(i => { i.checked = open; });
          syncMini(); return;
        }
        const i = e.target.closest('.mini i');
        if (!i) return;
        const c = document.getElementById(i.dataset.card);
        if (!c) return;
        const body = c.closest('.foldbody'), hiddenByFold = body && !body.classList.contains('open') && !pt.classList.contains('all');
        if (c.classList.contains('closed') || hiddenByFold) { openCard(c); c.scrollIntoView({behavior: 'smooth', block: 'center'}); flashCard(c); }
        else c.classList.add('closed');
        syncMini();
      });
    }
    panel.querySelectorAll('#mt tr[data-msg]').forEach(tr => tr.addEventListener('click', () => jumpToMsg(+tr.dataset.msg)));
    syncMini();
    if (pendingMsg != null) {
      const row = document.getElementById('msg' + pendingMsg);
      pendingMsg = null;
      if (row) { row.classList.add('hl'); row.scrollIntoView({behavior: 'smooth', block: 'center'}); return; }
    }
    panel.scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  function openCard(c) {
    const body = c.closest('.foldbody');
    if (body && !body.classList.contains('open')) { body.classList.add('open'); const b = body.previousElementSibling.querySelector('button'); if (b) b.textContent = '收起'; }
    c.classList.remove('closed');
  }
  function flashCard(c) { c.classList.remove('flash'); void c.offsetWidth; c.classList.add('flash'); }
  // 控制栏的色块与卡片状态同步：卡片被折叠，或位于未展开的公共前缀折叠区内时，色块显示为空心
  function syncMini() {
    const pt = document.getElementById('ptxt'), rail = document.getElementById('prail');
    if (!pt || !rail) return;
    rail.querySelectorAll('.mini i').forEach(i => {
      const c = document.getElementById(i.dataset.card), body = c && c.closest('.foldbody');
      const hidden = !c || c.classList.contains('closed') || (body && !body.classList.contains('open') && !pt.classList.contains('all'));
      i.classList.toggle('off', hidden);
    });
  }
  function jumpToMsg(i) {
    const card = document.getElementById('pm' + i);
    if (!card) return;
    openCard(card);
    card.scrollIntoView({behavior: 'smooth', block: 'center'});
    flashCard(card);
    syncMini();
  }
  const SEG_LIMIT = 1600, SEG_HEAD = 900;
  const TYPE_CN = {prefix: '模板开头', tools_def: '工具定义', system: 'system', developer: 'developer', user: 'user', assistant: 'assistant', tool: '工具结果', gen_prompt: '生成提示'};
  const TYPE_C = {prefix: KC('prefix'), tools_def: KC('tools_def'), system: KC('system'), developer: KC('system'), user: KC('user'), assistant: KC('assistant.content'), tool: KC('tool.result'), gen_prompt: KC('gen_prompt')};
  let lastGroups = [];  // promptHtml 生成的卡片列表，供 railHtml 画控制栏
  function promptHtml(r) {
    const pre = r.tok_prefix, msgs = r.msgs || [], attr = !!r.tok_attr, u = ru(r), btot = bkTot(r) || 1;
    const size = sg => attr ? sg.t1 - sg.t0 : sg.c1 - sg.c0;
    // 1) 片段按所属消息分组；模板开头、生成提示各自成组；工具定义按工具各成一组
    const groups = [];
    for (const sg of r.segs) {
      const key = sg.m != null ? 'm' + sg.m : (sg.k === 'tools_def' ? 'kt' + (sg.tool || '') : 'k' + sg.k);
      let g = groups[groups.length - 1];
      if (!g || g.key !== key) { g = {key, m: sg.m, k: sg.k, tool: sg.tool, segs: [], tok: 0, reused: true, kinds: new Set()}; groups.push(g); }
      const reused = attr && pre != null && sg.t1 <= pre && sg.k !== 'gen_prompt';
      g.segs.push({sg, reused}); g.tok += size(sg);
      if (sg.k !== 'markup') g.kinds.add(sg.k);
      if (!reused && size(sg) > 0) g.reused = false;
    }
    // 沿用判定：有 token 级前缀比较时按 token 区间；否则按消息层的状态（消息位于与上一请求的公共消息前缀内）
    if (!(attr && pre != null)) for (const g of groups) {
      const m = g.m != null ? msgs[g.m] : null;
      g.reused = r.kind !== 'first' && (m ? m.status === 'reused' : (g.k !== 'gen_prompt' && !r.tools_changed));
    }
    const dropped = {}; (r.dropped || []).forEach(d => { (dropped[d.msg] = dropped[d.msg] || []).push(d); });
    // 没有任何片段的消息（内容全部被模板丢弃，如只有思考的 assistant）也给一张卡片，保证卡片与消息表一一对应
    const present = new Set(groups.filter(g => g.m != null).map(g => g.m));
    msgs.forEach(m => {
      if (present.has(m.i)) return;
      const g = {key: 'm' + m.i, m: m.i, k: null, tool: null, segs: [], tok: 0, reused: false, kinds: new Set(), placeholder: true};
      let at = groups.findIndex(x => (x.m != null && x.m > m.i) || x.k === 'gen_prompt');
      if (at < 0) at = groups.length;
      groups.splice(at, 0, g);
    });
    lastGroups = [];
    const card = (g, idx) => {
      const m = g.m != null ? msgs[g.m] : null, role = m ? m.role : null, type = m ? role : g.k;
      const color = TYPE_C[type] || 'var(--fg3)';
      const st = m ? (r.kind === 'first' ? 'new' : (m.status || 'new')) : (g.reused ? 'reused' : 'new');
      const id = m ? 'pm' + g.m : 'pc' + idx;
      const label = m ? `消息 ${g.m} · ${role}${m.tool ? ' · ' + m.tool : ''}` : (g.k === 'tools_def' && g.tool ? `工具定义 · ${g.tool}` : KL[g.k]);
      lastGroups.push({id, type, color, label, tok: g.tok, reused: g.reused});
      let body = '';
      for (const {sg, reused} of g.segs) {
        const t = D.texts[sg.b];
        if (sg.k === 'markup') {
          if (t != null && t.length > 200 && residue(t).length > 200) body += `<span class="sg unattr" style="--c:var(--shifted)"><span class="sgl"><i class="sw" style="background:var(--shifted);margin:0"></i><b>未归属的文本</b> · ${N(size(sg))} ${u} · ${N(t.length)} 字符 · 未能对应到任何消息，计入模板标记</span><span>${E(t.length > SEG_LIMIT ? t.slice(0, SEG_HEAD) : t)}</span>${t.length > SEG_LIMIT ? `<button class="xp" data-b="${sg.b}">显示其余 ${N(t.length - SEG_HEAD)} 字符</button>` : ''}</span>`;
          else body += `<span class="mk" data-tip="模板标记：聊天模板在消息之间插入的控制标记（角色、工具调用与结果的边界），不属于任何消息的内容，但占用 prompt 的 token">${t == null ? '…' : E(t.replace(/\n/g, '⏎'))}</span>`;
          continue;
        }
        const long = t != null && t.length > SEG_LIMIT;
        const lab = `<b>${KL[sg.k]}</b> · ${N(size(sg))} ${u}${t != null && attr ? ' · ' + N(t.length) + ' 字符' : ''}`;
        body += `<span class="sg${reused ? ' old' : ''}" style="--c:${KC(sg.k)}"><span class="sgl"><i class="sw" style="background:${KC(sg.k)};margin:0"></i>${lab}</span><span>${t == null ? '（原文未嵌入）' : E(long ? t.slice(0, SEG_HEAD) : t)}</span>${long ? `<button class="xp" data-b="${sg.b}">显示其余 ${N(t.length - SEG_HEAD)} 字符</button>` : ''}</span>`;
      }
      if (m && dropped[g.m]) body += dropped[g.m].map(d => `<span class="note">该消息的 ${KL[d.kind] || d.kind}（${N(d.chars)} 字符）已由客户端发送，模板未将其渲染进 prompt</span>`).join('');
      if (g.placeholder && !(m && dropped[g.m])) body += `<span class="note">该消息（${N(m.src_chars || m.chars || 0)} 字符）已由客户端发送，模板未将其任何部分渲染进 prompt，本请求的 prompt 中没有对应的 token</span>`;
      const title = m ? `<span class="role">${E(role)}${m.tool ? ' · ' + E(m.tool) : ''}</span><span class="idx">消息 ${g.m}</span>`
        : g.k === 'tools_def' && g.tool ? `<span class="role">工具定义 · ${E(g.tool)}</span><span class="idx">${E(KIND_NOTE.tools_def[0])}</span>`
        : `<span class="role">${KL[g.k]}</span><span class="idx">${E((KIND_NOTE[g.k] || [''])[0])}</span>`;
      return `<div class="pc${g.reused ? ' reused' : ''}" id="${id}" data-type="${E(type)}" style="--c:${color}"><div class="pch" data-tip="点击折叠或展开这张卡片"><span class="chev"></span>${title}<span class="pill ${st}" data-tip="${E(STATUS_DEF[st] || '')}">${STATUS_CN[st]}</span><span class="tk"><b>${N(g.tok)}</b> ${u} · ${pctOf(g.tok, btot)}</span></div><div class="pcb">${body}</div></div>`;
    };
    // 2) 连续的、位于公共前缀内的组折叠为一行
    let out = [], run = [];
    const flush = () => {
      if (!run.length) return;
      const tok = run.reduce((a, g) => a + g.tok, 0), ms = run.filter(g => g.m != null).map(g => g.m), fx = run.filter(g => g.m == null);
      const fxs = [...new Set(fx.map(g => g.k === 'tools_def' ? '工具定义' : KL[g.k]))].map(n => n === '工具定义' ? `工具定义（${fx.filter(g => g.k === 'tools_def').length} 个）` : n);
      const what = [fxs.join('、'), ms.length ? (ms.length > 1 ? `消息 ${ms[0]}–${ms[ms.length - 1]}（${ms.length} 条）` : `消息 ${ms[0]}`) : ''].filter(Boolean).join('，');
      out.push(`<div class="fold"><span>与上一请求的公共前缀，已折叠：<b>${E(what)}</b>，共 <b>${N(tok)}</b> ${u}，占 ${pctOf(tok, btot)}</span><button data-fold>展开</button></div><div class="foldbody">${run.map(g => card(g, groups.indexOf(g))).join('')}</div>`);
      run = [];
    };
    for (const g of groups) { if (g.reused) run.push(g); else { flush(); out.push(card(g, groups.indexOf(g))); } }
    flush();
    return out.join('');
  }
  // 模板后文本右侧的控制栏：显示开关、按类型折叠、与卡片一一对应的色块
  function railHtml(r) {
    const cnt = {};
    lastGroups.forEach(g => { cnt[g.type] = (cnt[g.type] || 0) + 1; });
    const order = ['prefix', 'tools_def', 'system', 'developer', 'user', 'assistant', 'tool', 'gen_prompt'].filter(t => cnt[t]);
    return '<aside class="rail" id="prail"><div class="rt">显示</div><label><input type="checkbox" id="onlynew" checked> 折叠公共前缀</label><label><input type="checkbox" id="showmk" checked> 模板标记</label>'
      + '<div class="rt">按类型展开<small>取消勾选即折叠该类全部卡片</small></div>' + order.map(t => `<label class="tp"><input type="checkbox" data-type="${t}" checked><i class="sw" style="background:${TYPE_C[t]}"></i>${E(TYPE_CN[t] || t)}<small>${cnt[t]}</small></label>`).join('')
      + '<div class="rt">卡片<small>点击展开或折叠</small></div><div class="btns"><button data-all="open">全部展开</button><button data-all="close">全部折叠</button></div><div class="mini">'
      + lastGroups.map(g => `<i data-card="${g.id}" style="--c:${g.color}" data-tip="${E(g.label)} · ${N(g.tok)} ${ru(r)}${g.reused ? ' · 位于公共前缀' : ''}"></i>`).join('') + '</div></aside>';
  }
  function route() {
    hideTip(); tipEl = null;
    const h = location.hash || '#overview';
    const m = h.match(/^#s\/([^/]+)(?:\/r\/(\d+))?/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      const s = D.sessions.find(x => x.id === id);
      if (s) { if (m[2] == null || !document.getElementById('rt') || app.dataset.sid !== id) { app.dataset.sid = id; sessionPage(s, m[2] != null ? +m[2] : null); if (m[2] == null) window.scrollTo(0, 0); } else showReq(s, s.reqs[+m[2]]); spy(); return; }
    }
    app.dataset.sid = '';
    overview();
    window.scrollTo(0, 0);
    spy();
  }
  document.addEventListener('click', e => { const a = e.target.closest('a[data-jump]'); if (a) { e.preventDefault(); const el = document.getElementById(a.dataset.jump); if (el) el.scrollIntoView({behavior: 'smooth'}); } });
  // 目录随滚动高亮当前章节
  function spy() {
    const links = [...document.querySelectorAll('nav.toc a[data-jump]')];
    if (!links.length) return;
    const y = window.scrollY + 140;
    let k = 0;
    links.forEach((a, i) => { const h = document.getElementById(a.dataset.jump); if (h && h.getBoundingClientRect().top + window.scrollY <= y) k = i; });
    links.forEach((a, i) => a.classList.toggle('cur', i === k));
  }
  window.addEventListener('scroll', spy, {passive: true});
  window.addEventListener('hashchange', route);
  route();
})();
