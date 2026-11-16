# report.py：把 analyze.py 的结果画成一个自包含的 HTML（内联 SVG，无外部依赖，离线可看，跟随系统明暗）。
# 由 analyze.py 调用：report.write(path, rows, sessions, summary, role_comp, extra, args)
# 版式约定：
#   每一节：标题（带数据来源标签）→「名词解释」框 → 图。
#   每张图：标题 →「读图」（横轴、纵轴、每根柱代表什么，正文字号，放在图的上方）→ 图 →「结论」框（由数据算出的句子，关键数字加粗）
#           → 需要注意的地方用「注意」框（琥珀色）。
#   柱状图：凡是标了数量的柱子，同时标出它占合计的百分比；凡是标了百分比的柱子，同时标出对应的绝对量。
from __future__ import annotations

import collections
import html
import math
import os
import time

import pcap


def _q(xs: list, p: float):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "report_base.css"), encoding="utf-8") as _f:
    CSS = _f.read()  # 与 sessions.html 共用的版式
JS = """
(function(){var tip=document.createElement('div');tip.className='tooltip';document.body.appendChild(tip);var cur=null;
function place(e){if(tip.style.display!=='block')return;var pad=16,w=tip.offsetWidth,h=tip.offsetHeight,x=e.clientX+pad,y=e.clientY+pad;
if(x+w>window.innerWidth-8)x=Math.max(8,e.clientX-pad-w);if(y+h>window.innerHeight-8)y=Math.max(8,window.innerHeight-8-h);tip.style.left=x+'px';tip.style.top=y+'px';}
document.querySelectorAll('svg title').forEach(function(t){var el=t.parentElement;if(!el.dataset.tip)el.dataset.tip=t.textContent;t.remove();});
document.addEventListener('mouseover',function(e){var el=e.target.closest?e.target.closest('[data-tip]'):null;if(el===cur)return;cur=el;
if(el&&el.dataset.tip){tip.textContent=el.dataset.tip;tip.style.display='block';place(e);}else tip.style.display='none';});
document.addEventListener('mousemove',function(e){if(cur)place(e);});
window.addEventListener('scroll',function(){cur=null;tip.style.display='none';},{passive:true});
/* 目录跟随滚动：当前所在的节高亮 */
var links=[].slice.call(document.querySelectorAll('nav.toc a[href^="#"]')),heads=links.map(function(a){return document.getElementById(a.getAttribute('href').slice(1));});
function spy(){var y=window.scrollY+130,k=0;heads.forEach(function(h,i){if(h&&h.offsetTop<=y)k=i;});links.forEach(function(a,i){a.classList.toggle('cur',i===k);});}
window.addEventListener('scroll',spy,{passive:true});spy();})();
"""
COMP_GROUPS = [("ASCII 字母", ["ascii_letter"]), ("数字", ["digit"]), ("空白", ["space", "tab", "newline", "cr"]),
               ("ASCII 符号", ["ascii_punct", "ctrl"]), ("中日韩", ["u3_han", "u3_cjk_sym", "u3_fullwidth", "u3_hangul"]),
               ("其他非 ASCII", ["u2", "u3_misc", "u4"])]
PALETTE = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s7)", "var(--s5)", "var(--s4)"]
LIGHT_FILL = {2, 4, 5}  # PALETTE 里底色偏浅的几个：亮色模式下条内文字用墨色
# 各数值的取得方式。报告的每一节、每个指标卡均标注所属类别：线上实测、本机离线计算、离线重放。
SRC = {
    "proxy": ("proxy 记录", "proxy 在请求到达时记录的原始内容：请求体（messages、tools、参数）、请求头、到达时刻。未经估算"),
    "vllm": ("vLLM 返回", "P 实例响应的 usage 字段：prompt_tokens（vLLM 分词后的 token 数）、cached_tokens（前缀缓存命中的 token 数）。线上实际值"),
    "timer": ("proxy 计时", "proxy 记录的时间差：prefill_ms 为发出 P 请求至收到 P 响应，ttft_ms 为请求到达至 D 返回第一块数据。线上实际值，含排队时间"),
    "off": ("离线计算", "analyze.py 在本机（运行分析的机器）以 --tokenizer 指定的分词器，对采集到的请求全文重新渲染模板并分词所得。"
                    "token 数、预分词片段数等计数结果与机器无关，并已与 vLLM 返回的 prompt_tokens 逐条核对"),
    "log": ("vLLM 日志", "vLLM 进程输出的 [TTFT_DEBUG] 日志中记录的各阶段耗时。线上实际值"),
    "sim": ("离线重放", "analyze.py 将采集到的真实请求按到达顺序、按 tokenizers 0.23.2 的缓存规则逐条重放所得的命中率。"
                    "线上进程的线程数、路由与淘汰策略不在重放范围内"),
}
# 请求类型的名称与判定条件：report.py、join_vllm_log.py 共用；session_view.js 里有同一份
KIND_CN = {"first": "线程首个请求", "append_tool": "追加工具结果", "append_user": "追加用户消息", "append_assistant": "追加 assistant 消息",
           "repeat": "与上一请求相同", "rewrite": "历史消息被改写", "compact_like": "历史压缩（compact）", "no_session": "无会话标识",
           "no_messages": "无 messages"}
KIND_DEF = {"first": "同一线程内无更早的请求", "append_tool": "上一请求的消息全部保留，末尾新增 assistant 的工具调用及其工具结果",
            "append_user": "上一请求的消息全部保留，末尾新增一条 user 消息", "append_assistant": "上一请求的消息全部保留，末尾为 assistant 消息",
            "repeat": "消息与工具定义均与上一请求相同（重试）", "rewrite": "与上一请求的公共消息前缀之后，存在被修改或删除的历史消息",
            "compact_like": "客户端将历史压缩为摘要：消息条数降至上一请求的一半以下且公共消息前缀不超过 2 条（session_view 另据压缩请求的输出是否出现在本请求的消息中判定）",
            "no_session": "请求头不含会话标识，无法归入会话", "no_messages": "请求不含 messages（completions 接口）"}
E = html.escape


def src(*keys: str) -> str:
    return "".join(f"<span class='src src-{k}' data-tip='{E(SRC[k][1])}'>{E(SRC[k][0])}</span>" for k in keys)


def B(v) -> str:
    """关键数字：加粗。v 已经是格式化好的字符串。"""
    return f"<b>{E(str(v))}</b>"


# ---------------------------------------------------------------- 数字格式
def num(v, digits: int = 0) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, str):
        return v
    if digits == 0 and abs(v) >= 100:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:,.{digits}f}"
    if digits == 0:
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"
    return f"{v:,.{digits}f}"


def pct(v, digits: int = 1) -> str:
    return "–" if v is None else f"{100 * v:.{digits}f}%"


def pct_small(v) -> str:
    """很小的比例：不写成 0.0000%。"""
    if v is None:
        return "–"
    if v == 0:
        return "0%"
    if v < 1e-4:
        return "<0.01%"
    return f"{100 * v:.2f}%" if v < 0.01 else f"{100 * v:.1f}%"


def big(v) -> str:
    """大数的短写法：12.3M、456k、789。用在柱子旁边放不下完整数字的地方。"""
    if v is None:
        return "–"
    if abs(v) >= 1e9:
        return f"{v / 1e9:.2f}G"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2f}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:.0f}k"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}"


# ---------------------------------------------------------------- 表格与图
def table(head: list, rows: list[list], num_cols: set | None = None, strong_cols: set | None = None,
          note_cols: set | None = None) -> str:
    """head 的元素可以是字符串，也可以是 (列名, 列名下方的小字说明)。"""
    num_cols, strong_cols, note_cols = num_cols or set(), strong_cols or set(), note_cols or set()

    def th(i, x):
        name, sub = x if isinstance(x, tuple) else (x, "")
        return f"<th{' class=n' if i in num_cols else ''}>{E(name)}{f'<small>{E(sub)}</small>' if sub else ''}</th>"

    def td(i, c):
        cls = " ".join(k for k, on in (("n", i in num_cols), ("s", i in strong_cols), ("note", i in note_cols)) if on)
        return f"<td{f' class={chr(34)}{cls}{chr(34)}' if cls else ''}>{E(c if isinstance(c, str) else num(c))}</td>"

    h = "".join(th(i, x) for i, x in enumerate(head))
    b = "".join("<tr>" + "".join(td(i, c) for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>"


def _yticks(vmax: float, n: int = 4) -> list[float]:
    if vmax <= 0:
        return [0]
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min(x * mag for x in (1, 2, 2.5, 5, 10) if x * mag >= raw)
    return [i * step for i in range(int(math.ceil(vmax / step)) + 1)]


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """竖柱：顶端 4px 圆角，基线处方角。"""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + w - r:.1f} "
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def _hbar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """横柱：右端 4px 圆角，基线处方角。"""
    r = min(r, h / 2, w)
    return (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h - r:.1f} "
            f"Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z")


def vbars(labels: list, values: list, *, unit: str = "", color: str = PALETTE[0], width: int = 520, fmt=None,
          sub: list | None = None, xlab: str = "", ylab: str = "") -> str:
    """竖向柱状图。柱顶两行：第一行是柱子的数值（粗体），第二行 sub 是与它配对的量（数量配百分比，百分比配绝对量）。"""
    if not values:
        return "<p class='def'>无数据</p>"
    fmt = fmt or (lambda v: num(v))
    h, top, bottom, left, right = 252, 40 if sub else 24, 46 if xlab else 30, 56, 10
    ph, pw = h - top - bottom, width - left - right
    ticks = _yticks(max(values))
    vmax = ticks[-1] or 1
    y = lambda v: top + ph * (1 - v / vmax)  # noqa: E731
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>"]
    for t in ticks:
        out.append(f"<line class='grid' x1='{left}' x2='{width - right}' y1='{y(t):.1f}' y2='{y(t):.1f}'/>"
                   f"<text x='{left - 6}' y='{y(t) + 4:.1f}' text-anchor='end'>{E(fmt(t))}{E(unit.strip()) if unit.strip() == '%' else ''}</text>")
    out.append(f"<line class='axis' x1='{left}' x2='{width - right}' y1='{top + ph}' y2='{top + ph}'/>")
    n = len(values)
    bw = pw / n
    dense = bw < 40  # 柱子太密：数值标签只写在提示里和数据表里
    for i, (lab, v) in enumerate(zip(labels, values)):
        w = min(24.0, bw * 0.7)
        x = left + i * bw + (bw - w) / 2
        s = sub[i] if sub else None
        tip = f"{lab}：{fmt(v)}{unit}" + (f"（{s}）" if s else "")
        out.append(f"<path d='{_bar_path(x, y(v), w, max(top + ph - y(v), 0.5))}' fill='{color}'><title>{E(tip)}</title></path>")
        if not dense:
            yy = y(v) - 5
            if s and v:
                out.append(f"<text class='sub' x='{x + w / 2:.1f}' y='{yy:.1f}' text-anchor='middle'>{E(s)}</text>")
                yy -= 13
            out.append(f"<text class='val' x='{x + w / 2:.1f}' y='{yy:.1f}' text-anchor='middle'>{E(fmt(v))}{E(unit)}</text>")
        if not dense or i % max(1, n // 12) == 0:
            out.append(f"<text class='lab' x='{x + w / 2:.1f}' y='{top + ph + 16}' text-anchor='middle'"
                       + (" style='font-size:9.5px'" if bw < 46 and len(str(lab)) > 5 else "") + f">{E(str(lab))}</text>")
    if ylab:
        out.append(f"<text x='{left}' y='12' class='lab' text-anchor='end'>{E(ylab)}</text>")
    if xlab:
        out.append(f"<text x='{left + pw / 2:.1f}' y='{h - 6}' class='lab' text-anchor='middle'>{E(xlab)}</text>")
    out.append("</svg>")
    return "".join(out)


def hbars(labels: list, values: list, *, unit: str = "", color: str = PALETTE[0], width: int = 520, fmt=None,
          lab_w: int = 170, sub: list | None = None) -> str:
    """横向柱状图：标签在左，数值在柱右；sub 是跟在数值后面的配对量（粗体），如「882 个 · 98.3%」。"""
    if not values:
        return "<p class='def'>无数据</p>"
    fmt = fmt or (lambda v: num(v))
    bh, gap = 18, 8
    h = len(values) * (bh + gap) + 6
    vmax = max(values) or 1
    room = 150 if sub else 90
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>"]
    for i, (lab, v) in enumerate(zip(labels, values)):
        yy = 3 + i * (bh + gap)
        w = max(1.0, (width - lab_w - room) * v / vmax)
        s = sub[i] if sub else None
        tip = f"{lab}：{fmt(v)}{unit}" + (f"（{s}）" if s else "")
        out.append(f"<text x='{lab_w - 8}' y='{yy + bh * 0.72:.1f}' text-anchor='end' class='lab'>{E(str(lab))}</text>"
                   f"<path d='{_hbar_path(lab_w, yy, w, bh)}' fill='{color}'><title>{E(tip)}</title></path>"
                   f"<text x='{lab_w + w + 6:.1f}' y='{yy + bh * 0.72:.1f}' style='fill:var(--fg)'>{E(fmt(v))}{E(unit)}"
                   + (f"<tspan class='sub'> · </tspan><tspan class='pc'>{E(s)}</tspan>" if s else "") + "</text>")
    out.append("</svg>")
    return "".join(out)


def grouped(labels: list, series: list[tuple[str, list]], *, unit: str = "", width: int = 520, sub: list | None = None,
            xlab: str = "") -> str:
    """分组柱状图。sub：每组横轴标签下面的第二行（如样本数）。"""
    vals = [v for _n, vs in series for v in vs if v is not None]
    if not vals:
        return "<p class='def'>无数据</p>"
    h, top, bottom, left, right = 252, 24, (44 if sub else 30) + (14 if xlab else 0), 56, 10
    ph, pw = h - top - bottom, width - left - right
    ticks = _yticks(max(vals))
    vmax = ticks[-1] or 1
    y = lambda v: top + ph * (1 - v / vmax)  # noqa: E731
    gw = pw / len(labels)
    bw = (gw * 0.75) / len(series)
    w = max(3.0, min(bw - 3, 24))
    leg = "".join(f"<span><i style='background:{PALETTE[i]}'></i>{E(nm)}</span>" for i, (nm, _v) in enumerate(series))
    out = [f"<div class='legend'>{leg}</div><svg viewBox='0 0 {width} {h}' role='img'>"]
    u = unit.strip()
    for t in ticks:
        out.append(f"<line class='grid' x1='{left}' x2='{width - right}' y1='{y(t):.1f}' y2='{y(t):.1f}'/>"
                   f"<text x='{left - 6}' y='{y(t) + 4:.1f}' text-anchor='end'>{E(num(t))}</text>")
    out.append(f"<text x='{left}' y='12' class='lab' text-anchor='end'>{E(u)}</text>")
    out.append(f"<line class='axis' x1='{left}' x2='{width - right}' y1='{top + ph}' y2='{top + ph}'/>")
    for i, lab in enumerate(labels):
        for j, (nm, vs) in enumerate(series):
            v = vs[i]
            if v is None:
                continue
            x = left + i * gw + gw / 2 - (len(series) * (w + 3) - 3) / 2 + j * (w + 3)  # 同组的柱子并在一起，组与组之间留空
            out.append(f"<path d='{_bar_path(x, y(v), w, max(top + ph - y(v), .5))}' "
                       f"fill='{PALETTE[j]}'><title>{E(str(lab))} {E(nm)}：{E(num(v, 1))}{E(unit)}</title></path>")
            if len(labels) <= 8:
                out.append(f"<text class='val' x='{x + w / 2:.1f}' y='{y(v) - 4:.1f}' text-anchor='middle'"
                           + (" style='font-size:10px'" if w < 22 else "") + f">{E(num(v, 1) if v < 100 else num(v))}</text>")
        cx = left + i * gw + gw / 2
        out.append(f"<text class='lab' x='{cx:.1f}' y='{top + ph + 16}' text-anchor='middle'>{E(str(lab))}</text>")
        if sub:
            out.append(f"<text class='sub' x='{cx:.1f}' y='{top + ph + 30}' text-anchor='middle'>{E(str(sub[i]))}</text>")
    if xlab:
        out.append(f"<text x='{left + pw / 2:.1f}' y='{h - 6}' class='lab' text-anchor='middle'>{E(xlab)}</text>")
    out.append("</svg>")
    return "".join(out)


def line(xs: list[float], ys: list[float], *, ylab: str = "", width: int = 1080, yfmt=None, xfmt=None,
         ymin: float | None = None, ymax: float | None = None) -> str:
    if len(xs) < 2:
        return "<p class='def'>数据点少于 2 个，未绘图</p>"
    yfmt = yfmt or (lambda v: num(v))
    h, top, bottom, left, right = 210, 18, 34, 56, 12
    ph, pw = h - top - bottom, width - left - right
    x0, x1 = min(xs), max(xs)
    y0 = min(ys) if ymin is None else ymin
    y1 = max(ys) if ymax is None else ymax
    if y1 == y0:
        y1 = y0 + 1
    X = lambda x: left + pw * (x - x0) / ((x1 - x0) or 1)  # noqa: E731
    Y = lambda y: top + ph * (1 - (y - y0) / (y1 - y0))  # noqa: E731
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>"]
    for g in range(5):
        yv = y0 + (y1 - y0) * g / 4
        out.append(f"<line class='grid' x1='{left}' x2='{width - right}' y1='{Y(yv):.1f}' y2='{Y(yv):.1f}'/>"
                   f"<text x='{left - 6}' y='{Y(yv) + 4:.1f}' text-anchor='end'>{E(yfmt(yv))}</text>")
    out.append(f"<line class='axis' x1='{left}' x2='{width - right}' y1='{top + ph}' y2='{top + ph}'/>")
    pts = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in zip(xs, ys))
    out.append(f"<polygon points='{X(xs[0]):.1f},{top + ph} {pts} {X(xs[-1]):.1f},{top + ph}' fill='var(--s1)' opacity='.10'/>")
    out.append(f"<polyline points='{pts}' fill='none' stroke='var(--s1)' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>")
    out.append(f"<circle cx='{X(xs[-1]):.1f}' cy='{Y(ys[-1]):.1f}' r='4' fill='var(--s1)' stroke='var(--bg)' stroke-width='2'/>")
    step = max(1, len(xs) // 400)
    for x, yv in list(zip(xs, ys))[::step]:
        lab = xfmt(x) if xfmt else num(x)
        out.append(f"<circle cx='{X(x):.1f}' cy='{Y(yv):.1f}' r='7' fill='transparent'><title>{E(lab)}：{E(yfmt(yv))}</title></circle>")
    for x in (x0, x0 + (x1 - x0) * 0.25, (x0 + x1) / 2, x0 + (x1 - x0) * 0.75, x1):
        out.append(f"<text class='lab' x='{X(x):.1f}' y='{h - 12}' text-anchor='middle'>{E(xfmt(x) if xfmt else num(x))}</text>")
    if ylab:
        out.append(f"<text x='{left}' y='11' class='lab'>{E(ylab)}</text>")
    out.append("</svg>")
    return "".join(out)


def stacked100(rows: list[tuple[str, list[float]]], names: list[str], width: int = 1080, total_unit: str = "") -> str:
    """每行合计 100% 的横向堆叠条；条内写百分比，行尾写这一行的绝对合计。"""
    lab_w, bh, gap, tail = 110, 24, 10, 96
    h = len(rows) * (bh + gap) + 6
    pw = width - lab_w - tail
    leg = "".join(f"<span><i style='background:{PALETTE[i]}'></i>{E(n)}</span>" for i, n in enumerate(names))
    out = [f"<div class='legend'>{leg}</div><svg viewBox='0 0 {width} {h}' role='img'>"]
    for i, (lab, vals) in enumerate(rows):
        y = 3 + i * (bh + gap)
        tot = sum(vals) or 1
        out.append(f"<text x='{lab_w - 8}' y='{y + bh * 0.68}' text-anchor='end' class='lab'>{E(lab)}</text>")
        x = lab_w
        for j, v in enumerate(vals):
            w = pw * v / tot
            if w <= 0:
                continue
            out.append(f"<rect x='{x:.1f}' y='{y}' width='{max(w - 2, 0.5):.1f}' height='{bh}' rx='3' fill='{PALETTE[j]}'>"
                       f"<title>{E(lab)} · {E(names[j])}：{v / tot:.1%}（{big(v)} {E(total_unit)}）</title></rect>")
            if w > 34:
                out.append(f"<text class='inl{' k' if j in LIGHT_FILL else ''}' x='{x + w / 2:.1f}' y='{y + bh * 0.68}' text-anchor='middle'>{v / tot:.0%}</text>")
            x += w
        out.append(f"<text x='{lab_w + pw + 6}' y='{y + bh * 0.68}' class='sub'>{big(sum(vals))} {E(total_unit)}</text>")
    out.append("</svg>")
    return "".join(out)


def fig(title: str, body: str, *, read: str = "", finding: str = "", warn: str = "", data_table: str = "",
        badge: str = "") -> str:
    """read / finding / warn 是 HTML（调用方负责转义动态内容）。"""
    parts = [f"<figure><h3>{E(title)}{(' ' + badge) if badge else ''}</h3>"]
    if read:
        parts.append(f"<p class='read'>{read}</p>")
    parts.append(body)
    if finding:
        parts.append(f"<div class='finding'><span class='lbl'>结论</span>{finding}</div>")
    if warn:
        parts.append(f"<div class='warn'><span class='lbl'>注意</span>{warn}</div>")
    if data_table:
        parts.append(f"<details><summary>数据表</summary>{data_table}</details>")
    parts.append("</figure>")
    return "".join(parts)


def session_eg(sessions: list[dict], rows: list[dict]) -> str:
    """会话内请求数的实例：取请求数最接近中位数、且有消息统计的会话，用它最后一个请求的 user / 工具结果条数说明步数。"""
    cand = [s for s in sessions if s.get("n_requests")]
    if not cand:
        return ""
    med = _q([s["n_requests"] for s in cand], .5)
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r.get("session") and r.get("n_msgs") is not None:
            by[r["session"]].append(r)
    cand = [s for s in cand if by.get(s["session"])] or cand
    s0 = min(cand, key=lambda s: abs(s["n_requests"] - med))
    rs = by.get(s0["session"]) or []
    last = max(rs, key=lambda r: r.get("n_msgs") or 0) if rs else None
    t = f"<span class='eg'>本数据：会话 {E(str(s0['session'])[:16])}… 共 {num(s0['n_requests'])} 个请求"
    if last:
        t += (f"；其消息最多的请求含 user 消息 {num(last.get('n_user') or 0)} 条、工具结果 {num(last.get('n_tool') or 0)} 条，"
              f"即用户输入 {num(last.get('n_user') or 0)} 轮、agent 执行工具 {num(last.get('n_tool') or 0)} 次")
    return t + "。</span>"


def reuse_eg(rows: list[dict]) -> str:
    """前缀复用率的实例：取复用率最接近中位数的一个追加类请求，写出它的数字。"""
    cand = [r for r in rows if (r.get("kind") or "").startswith("append") and r.get("reuse_ratio_prev") is not None
            and r.get("prefix_chars_prev") is not None and r.get("new_chars") is not None]
    if not cand:
        return ""
    med = _q([r["reuse_ratio_prev"] for r in cand], .5)
    r0 = min(cand, key=lambda r: abs(r["reuse_ratio_prev"] - med))
    p, n = r0["prefix_chars_prev"], r0["new_chars"]
    return ("<span class='eg'>本数据：" + (f"会话 {E(str(r0.get('session'))[:16])}… 的第 {num((r0.get('turn_idx') or 0) + 1)} 个请求，" if r0.get("session") else "")
            + f"与上一请求相同的消息前缀共 {num(p)} 字符，新增 {num(n)} 字符，前缀复用率 = {num(p)} / {num(p + n)} = {pct(p / (p + n)) if p + n else '–'}。</span>")


def defs(items: list[tuple[str, str]], raw: bool = False) -> str:
    """名词解释：两栏，左边是词，右边是解释。raw=True 时解释部分是 HTML。"""
    return "<dl class='gloss'>" + "".join(f"<dt>{E(k)}</dt><dd>{v if raw else E(v)}</dd>" for k, v in items) + "</dl>"


def h2(anchor: str, no: str, title: str, *srcs: str) -> str:
    return (f"<h2 id='{anchor}'>" + (f"<span class='no'>{E(no)}</span>" if no else "") + f"<span>{E(title)}</span>"
            + (f"<span class='srcs'>{src(*srcs)}</span>" if srcs else "") + "</h2>")


def kpi(value: str, unit: str, title: str, desc: str = "", *srcs: str) -> str:
    return (f"<div class='kpi'><b>{E(value)}<small>{E(unit)}</small></b><span class='t'>{E(title)}</span>"
            + (f"<span class='d'>{E(desc)}</span>" if desc else "") + (src(*srcs) if srcs else "") + "</div>")


# ---------------------------------------------------------------- 报告
def write(path: str, rows: list[dict], sessions: list[dict], summ: list[tuple], role_comp: dict, extra: dict,
          args) -> None:
    import analyze  # noqa: PLC0415

    S = {(s[0], s[1]): s[2] for s in summ}
    n = len(rows)
    text = [r for r in rows if r.get("level") == "text"]
    gold_n = sum(1 for r in rows if r.get("gold_n") is not None)
    pts = [r["prompt_tokens"] for r in rows if isinstance(r.get("prompt_tokens"), int)]
    span = f"{rows[0]['time'][:19]} 至 {rows[-1]['time'][:19]}" if rows else "–"
    order = [analyze.bucket_label(x) for x in analyze.PT_BUCKETS[:-1]]
    bucket_cnt = collections.Counter(analyze.bucket_label(x) for x in pts)
    labs = [b for b in order if bucket_cnt.get(b)]
    tok_by = collections.Counter()
    for x in pts:
        tok_by[analyze.bucket_label(x)] += x
    tot_t = sum(pts) or 1
    kc = collections.Counter(r.get("kind") for r in rows if r.get("kind"))
    kind_cn, kind_def = KIND_CN, KIND_DEF
    P: list[str] = []

    # ---- 头部
    P.append("<header class='top'><p class='eyebrow'>PROMPT CAPTURE · 全体请求</p><h1>Proxy 请求特征报告</h1><dl class='meta'>"
             f"<dt>数据目录</dt><dd>{E(', '.join(os.path.abspath(p) for p in args.data))}</dd>"
             f"<dt>时间范围</dt><dd>{E(span)}</dd>"
             f"<dt>记录</dt><dd>{num(n)} 个请求，{num(len(sessions))} 个会话；含全文的请求 {num(len(text))} 个"
             f"（{pct(len(text) / n) if n else '–'}），含 golden 的请求 {num(gold_n)} 个</dd>"
             f"<dt>分词器</dt><dd>{E(args.tokenizer) if args.tokenizer else '未提供（报告不含第 6 节）'}</dd>"
             f"<dt>生成时间</dt><dd>{time.strftime('%Y-%m-%d %H:%M:%S')}</dd></dl></header>")
    toc = [("summary", "摘要"), ("source", "数据来源"), ("traffic", "1 流量"), ("length", "2 长度"), ("text", "3 文本构成"),
           ("session", "4 会话与复用"), ("vllm", "5 vLLM 侧"), ("tokenizer", "6 分词器"), ("all", "附录")]
    if not extra:
        toc = [t for t in toc if t[0] != "tokenizer"]
    P.append("<nav class='toc'><div class='row sec'>" + "".join(f"<a href='#{k}'>{E(v)}</a>" for k, v in toc) + "</div></nav>")

    # ---- 摘要
    bullets = []
    if pts:
        top_b = max(labs, key=lambda b: tok_by[b])
        bullets.append(f"prompt 长度的中位数为 {B(num(analyze.q(pts, .5)))} token，P99 为 {B(num(analyze.q(pts, .99)))} token；"
                       f"{E(top_b)} 区间占请求数的 {B(pct(bucket_cnt[top_b] / len(pts)))}，占 token 总量的 {B(pct(tok_by[top_b] / tot_t))}。")
    tot_k = sum(kc.values()) or 1
    if kc:
        bullets.append("请求类型占比前三位：" + "，".join(f"{E(kind_cn.get(k, k))} {B(pct(v / tot_k))}" for k, v in kc.most_common(3)) + "。")
    if S.get(("session", "reuse_ratio_prev_weighted")) is not None:
        bullets.append(f"会话内前缀复用率（字符加权）为 {B(pct(S[('session', 'reuse_ratio_prev_weighted')]))}，"
                       "即各请求的文本中与同线程上一请求的开头部分相同的比例。")
    if S.get(("vllm", "cache_hit_ratio_weighted")) is not None:
        s_ = f"vLLM 前缀缓存命中率为 {B(pct(S[('vllm', 'cache_hit_ratio_weighted')]))}（token 加权）"
        ih, ah = S.get(("tokenizer", "ideal_prefix_cache_hit")), S.get(("tokenizer", "actual_prefix_cache_hit_same_requests"))
        if ih is not None and ah is not None:
            s_ += (f"；在已做离线分词的请求上，实际命中率为 {B(pct(ah))}，容量不限、全局共享的前缀缓存的重放命中率为 {B(pct(ih))}，"
                   f"相差 {B(f'{(ih - ah) * 100:.1f}')} 个百分点")
        bullets.append(s_ + "。")
    if S.get(("vllm", "ttft_ms_p50")) is not None:
        bullets.append(f"流式请求的 TTFT 中位数为 {B(num(S[('vllm', 'ttft_ms_p50')]))} ms，P95 为 {B(num(S[('vllm', 'ttft_ms_p95')]))} ms。")
    if S.get(("tokenizer", "offline_count_match_vllm")) is not None:
        g = S.get(("tokenizer", "offline_ids_match_gold"))
        bullets.append(f"离线分词的 token 数与 vLLM 返回的 prompt_tokens 相等的请求占 {B(pct(S[('tokenizer', 'offline_count_match_vllm')]))}"
                       + (f"；离线 token 序列与 golden 逐 token 相同的请求占 {B(pct(g))}（含 golden 的请求共 {num(gold_n)} 个）" if g is not None else "") + "。")
    inc = S.get(("tokenizer", "incremental_new_token_share"))
    if inc is not None:
        bullets.append(f"需重新处理的 token（位于与同线程上一请求的公共前缀之后）占 {B(pct(inc))}；增量分词与分段缓存可减少的分词量上限为 {B(pct(1 - inc))}。")
    P.append("<h2 id='summary'><span>摘要</span></h2><div class='summary'><ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul></div>")
    kp = [kpi(num(n), "个", "请求", "", "proxy"), kpi(num(len(sessions)), "个", "会话", "", "proxy"),
          kpi(num(analyze.q(pts, .5)), "token", "prompt 长度中位数", "", "vllm"),
          kpi(num(analyze.q(pts, .99)), "token", "prompt 长度 P99", "", "vllm"),
          kpi(pct(S.get(("vllm", "cache_hit_ratio_weighted"))), "", "vLLM 前缀缓存命中率", "cached_tokens 之和 / prompt_tokens 之和", "vllm"),
          kpi(pct(S.get(("session", "reuse_ratio_prev_weighted"))), "", "会话内前缀复用率", "与上一请求相同的消息前缀的字符数 / 全部字符数", "proxy")]
    if S.get(("vllm", "ttft_ms_p50")) is not None:
        kp.append(kpi(num(S[("vllm", "ttft_ms_p50")]), "ms", "TTFT 中位数", "仅统计流式请求", "timer"))
    if S.get(("tokenizer", "offline_count_match_vllm")) is not None:
        kp.append(kpi(pct(S[("tokenizer", "offline_count_match_vllm")]), "", "离线与线上 token 数一致率", "离线分词的 token 数等于 prompt_tokens 的请求占比", "off", "vllm"))
    P.append(f"<div class='kpis{' k4' if len(kp) in (7, 8) else ''}'>" + "".join(kp) + "</div>")

    # ---- 数据来源
    P.append("<h2 id='source'><span>数据来源</span><span class='sub'>各指标的取得方式</span></h2>")
    P.append("<p class='read'>报告中的数值按取得方式分为五类。各节标题右侧与各指标卡下方标有所属类别。</p>")
    used = {"proxy": "流量、消息条数、字符数、字符类别、会话与请求类型、前缀复用率", "vllm": "prompt_tokens、cached_tokens、前缀缓存命中率",
            "timer": "prefill_ms、ttft_ms、e2e_ms、tpot_ms", "off": "预分词片段、整词查表比例、每 token 字节数、需重新处理的 token 占比",
            "sim": "词缓存命中率、理想前缀缓存命中率"}
    P.append("<div class='scroll'><table><thead><tr><th>类别</th><th>取得方式</th><th>本报告中属于该类别的指标</th></tr></thead><tbody>"
             + "".join(f"<tr><td>{src(k)}</td><td class='note'>{E(SRC[k][1])}</td><td class='note'>{E(used[k])}"
                       + ("<br><b>本次未提供 --tokenizer，报告不含该类别的指标</b>" if k in ("off", "sim") and not extra else "") + "</td></tr>" for k in used)
             + "</tbody></table></div>")
    P.append("<p class='read'>字符类别仅在含全文的请求上统计，消息条数仅在 chat 接口的请求上统计；含全文请求的比例见页首「记录」。</p>")

    # ---- 1 流量
    P.append(h2("traffic", "1", "流量", "proxy"))
    P.append(defs([("时刻", "请求到达 proxy 的时刻（采集记录的 ts 字段）"),
                   ("客户端", "按请求头 User-Agent 归类；无法识别的归入 other"),
                   ("结束状态", "ok 表示 D 的响应流正常结束；其余取值为 proxy 记录的错误或中断原因")]))
    per_min = collections.Counter(int(r["ts"] // 60) for r in rows)
    if len(per_min) >= 2:
        mins = sorted(per_min)
        xs = list(range(mins[0], mins[-1] + 1))
        ys = [per_min.get(x, 0) for x in xs]
        peak = max(xs, key=lambda x: per_min.get(x, 0))
        P.append(fig("每分钟到达的请求数", line([x * 60 for x in xs], ys, ylab="请求 / 分钟",
                                            xfmt=lambda t: time.strftime("%m-%d %H:%M", time.localtime(t)), ymin=0),
                     read="横轴：本地时间。纵轴：每分钟到达 proxy 的请求数。",
                     finding=f"每分钟到达请求数的中位数为 {B(num(analyze.q(ys, .5)))}，最大值为 {B(num(per_min[peak]))}"
                             f"（{E(time.strftime('%m-%d %H:%M', time.localtime(peak * 60)))}）。"))
    g = []
    for col, title in (("client", "客户端"), ("endpoint", "接口"), ("status", "结束状态"), ("model", "模型")):
        c = collections.Counter(r.get(col) or "–" for r in rows).most_common(10)
        if len(c) == 1 and col in ("endpoint", "model"):
            continue
        k0, v0 = c[0]
        if col == "status":
            bad = n - sum(v for k, v in c if k == "ok")
            fnd = f"结束状态为 ok 的请求占 {B(pct(1 - bad / n))}；其余 {B(num(bad))} 个，占 {B(pct(bad / n))}。"
        else:
            fnd = f"请求数最多的{E(title)}为 {B(k0)}，共 {B(num(v0))} 个请求，占 {B(pct(v0 / n))}。"
        g.append(fig(title, hbars([k for k, _ in c], [v for _, v in c], unit=" 个", sub=[pct(v / n) for _, v in c]),
                     read=f"柱长：请求数。柱右标注：请求数 · 占全部 {num(n)} 个请求的比例。", finding=fnd,
                     data_table=table([title, "请求数", "占比"], [[k, v, pct(v / n)] for k, v in c], {1, 2})))
    P.append("<div class='grid2'>" + "".join(g) + "</div>")

    # ---- 2 长度
    P.append(h2("length", "2", "长度分布", "vllm", "proxy"))
    P.append(defs([("prompt_tokens", "P 实例响应 usage 字段中的 prompt_tokens，即 vLLM 分词后的 token 数"),
                   ("区间", "按 prompt_tokens 划分，1k = 1024；16k-32k 表示 16,384 ≤ prompt_tokens < 32,768"),
                   ("token 占比", "区间内各请求的 prompt_tokens 之和 / 全部请求的 prompt_tokens 之和。"
                                "分词与 prefill 的计算量与 token 数成正比，计算量在各区间的分布以 token 占比衡量")]))
    if pts:
        top_c = max(labs, key=lambda b: bucket_cnt[b])
        top_t = max(labs, key=lambda b: tok_by[b])
        short = [b for b in labs if order.index(b) <= 1]
        P.append("<div class='grid2'>"
                 + fig("各区间的请求数", vbars(labs, [bucket_cnt[b] for b in labs], unit="", sub=[pct(bucket_cnt[b] / len(pts)) for b in labs],
                                        xlab="prompt_tokens 区间", ylab="请求数"),
                       read=f"横轴：prompt_tokens 区间。纵轴：请求数。柱顶标注：请求数；占全部 {num(len(pts))} 个请求的比例。",
                       finding=f"请求数最多的区间为 {B(top_c)}，共 {B(num(bucket_cnt[top_c]))} 个，占 {B(pct(bucket_cnt[top_c] / len(pts)))}。",
                       data_table=table(["区间", "请求数", "占比"], [[b, bucket_cnt[b], pct(bucket_cnt[b] / len(pts))] for b in labs], {1, 2}))
                 + fig("各区间的 token 占比", vbars(labs, [tok_by[b] / tot_t * 100 for b in labs], unit="%", color=PALETTE[1],
                                            fmt=lambda v: f"{v:.1f}", sub=[big(tok_by[b]) for b in labs], xlab="prompt_tokens 区间", ylab="占比"),
                       read=f"横轴：prompt_tokens 区间。纵轴：该区间的 token 数占全部 {big(tot_t)} token 的比例。柱顶标注：token 占比；token 数。",
                       finding=f"{B(top_t)} 区间占请求数的 {B(pct(bucket_cnt[top_t] / len(pts)))}，占 token 总量的 {B(pct(tok_by[top_t] / tot_t))}。"
                               + (f"{E('、'.join(short))} 区间合计占请求数的 {B(pct(sum(bucket_cnt[b] for b in short) / len(pts)))}，"
                                  f"占 token 总量的 {B(pct(sum(tok_by[b] for b in short) / tot_t))}。" if short and top_t not in short else "")
                               + f"<mark>分词与 prefill 的计算量集中在 {E(top_t)} 区间</mark>。",
                       data_table=table(["区间", "token 数", "占比"], [[b, tok_by[b], pct(tok_by[b] / tot_t)] for b in labs], {1, 2}))
                 + "</div>")
    # 各请求的消息条数
    with_n = [r for r in rows if r.get("n_msgs") is not None]
    if with_n:
        nm = collections.Counter(min(r["n_msgs"], 200) // 10 * 10 for r in with_n)
        ks = sorted(nm)
        nms = [r["n_msgs"] for r in with_n]
        med = analyze.q(nms, .5)
        cand = [r for r in with_n if (r.get("kind") or "").startswith("append") and r.get("n_msgs", 0) >= 4 and r.get("new_msgs")]
        eg = ""
        if cand:
            r0 = min(cand, key=lambda r: abs(r["n_msgs"] - med))
            parts = [(r0.get("n_system"), "system"), (r0.get("n_user"), "user"), (r0.get("n_assistant"), "assistant"), (r0.get("n_tool"), "工具结果")]
            eg = ("<span class='eg'>本数据：" + (f"会话 {E(str(r0.get('session'))[:16])}… 的第 {num((r0.get('turn_idx') or 0) + 1)} 个请求，" if r0.get("session") else "")
                  + f"messages 共 <b>{num(r0['n_msgs'])}</b> 条，其中 " + "、".join(f"{E(nm_)} {num(v)} 条" for v, nm_ in parts if v)
                  + f"；最后 <b>{num(r0['new_msgs'])}</b> 条为本次新增，其余 <b>{num(r0['n_msgs'] - r0['new_msgs'])}</b> 条与上一请求相同。</span>")
        P.append(defs([("消息条数", "chat 请求的请求体含 <code>messages</code> 数组，消息条数为该数组的长度。数组依次包含 system 提示与截至当前的全部对话历史；"
                                "每条 user 消息、每条 assistant 回复（含工具调用）、每个工具结果各计 1 条。"
                                "服务端不保存对话状态，客户端在每个请求中重发全部历史，同一会话内各请求的消息条数随步数递增。" + eg),
                       ("用途", "消息条数反映会话已进行的步数与请求中重发历史的规模。聊天模板逐条渲染消息；重发的历史是前缀缓存与分段缓存的作用对象。")], raw=True))
        ge20 = sum(1 for x in nms if x >= 20)
        P.append(fig("各请求的消息条数分布",
                     vbars([f"{k}–{k + 9}" if k < 200 else "≥200" for k in ks], [nm[k] for k in ks], color=PALETTE[2], width=1080,
                           sub=[pct(nm[k] / len(nms)) for k in ks], xlab="消息条数", ylab="请求数"),
                     read=f"横轴：消息条数，每 10 条为一档。纵轴：请求数。柱顶标注：请求数；占全部 {num(len(nms))} 个 chat 请求的比例。",
                     finding=f"消息条数的中位数为 {B(num(med))}，P90 为 {B(num(analyze.q(nms, .9)))}，最大值为 {B(num(max(nms)))}；"
                             f"消息条数不少于 20 的请求占 {B(pct(ge20 / len(nms)))}。",
                     data_table=table(["消息条数", "请求数", "占比"], [[f"{k}–{k + 9}", nm[k], pct(nm[k] / len(nms))] for k in ks], {1, 2})))

    # ---- 3 文本构成
    P.append(h2("text", "3", "文本构成", "proxy"))
    script_cn = {"ascii": "ASCII 为主（≥95%）", "cjk": "中日韩为主（≥30%）", "mixed_cjk": "混合，含中日韩 5–30%", "mixed": "混合",
                 "latin_ext": "拉丁扩展为主", "empty": "空"}
    if text:
        P.append(defs([("统计范围", f"含全文的 {num(len(text))} 个请求，占全部请求的 {pct(len(text) / n)}。统计对象为请求中的原始文本，即各条消息的正文与工具定义的 JSON"),
                       ("字符类别", "按字符的 UTF-8 首字节分为六类：ASCII 字母、数字、空白（空格、Tab、换行）、ASCII 符号、中日韩（汉字、假名、全角标点）、其他非 ASCII"),
                       ("用途", "① 每个 token 对应的字节数与分词吞吐随文字类别变化，按文字分组的数值见第 6 节；"
                              "② 分词器基准测试与 PGO 训练所用语料的字符构成应与线上一致；"
                              "③ 符号与空白的占比反映代码、JSON、命令输出类文本的比重")]))
        comp_rows = []
        allc = collections.Counter()
        for r in text:
            for nm_, _d in pcap.CHAR_CLASSES:
                allc["cc_" + nm_] += (r.get("cc_" + nm_) or 0) * (r.get("text_chars") or 0)
        labels = {"system": "system", "user": "user", "assistant": "assistant", "tool": "工具结果", "tools_def": "工具定义", "other": "其他角色"}
        for lab, comp in [("全部请求", allc)] + [(labels[k], role_comp[k]) for k in labels if role_comp.get(k)]:
            comp_rows.append((lab, [sum(comp.get("cc_" + x, 0) for x in grp) for _g, grp in COMP_GROUPS]))
        tbl = table(["范围"] + [g for g, _ in COMP_GROUPS] + ["字符数"],
                    [[lab] + [pct(v / (sum(vals) or 1)) for v in vals] + [sum(vals)] for lab, vals in comp_rows], set(range(1, 8)))
        share = {lab: [v / (sum(vals) or 1) for v in vals] for lab, vals in comp_rows}
        a = share["全部请求"]
        ascii_all = a[0] + a[1] + a[2] + a[3]
        cjk_by = sorted(((lab, s[4]) for lab, s in share.items() if lab != "全部请求"), key=lambda x: -x[1])
        fnd = (f"分词器实际处理的文本中，ASCII 字符合计占 {B(pct(ascii_all))}（字母 {pct(a[0])}、数字 {pct(a[1])}、空白 {pct(a[2])}、符号 {pct(a[3])}），"
               f"中日韩字符占 {B(pct(a[4]))}。")
        if cjk_by:
            fnd += f"按角色统计，中日韩字符占比最高的为 {B(cjk_by[0][0])}（{B(pct(cjk_by[0][1]))}），最低的为 {B(cjk_by[-1][0])}（{B(pct(cjk_by[-1][1]))}）。"
        fnd += "<mark>基准测试与 PGO 训练语料的字符构成应与「全部请求」一行一致</mark>。"
        P.append(fig("字符类别占比", stacked100(comp_rows, [g for g, _ in COMP_GROUPS], total_unit="字符"),
                     read="每行为一个统计范围；各段宽度为该类字符占该行字符总数的比例，行尾标注字符总数。"
                          "<b>全部请求</b>按各请求实际发送的文本累计，历史消息每重发一次计一次，对应分词器实际处理的文本。"
                          "<b>其余各行</b>按角色统计，同一条消息只计一次。",
                     finding=fnd, data_table=tbl))
        sc = collections.Counter(r.get("script") for r in text if r.get("script")).most_common()
        nt = len(text)
        feats = [("连续无空白片段 ≥256 字节", "words_ge256b", "个",
                  "tokenizers 的词缓存仅缓存长度小于 256 字节的预分词片段，超出者每次出现均执行完整的 BPE 合并。"
                  "本行按空白切分统计，为文本层面的近似值；分词器实际产生的超长片段数见第 6 节"),
                 ("类 base64 串（≥100 字符）", "base64_like_runs", "个", "高熵字符串；其预分词片段的重复率低，进入词缓存后被再次命中的概率低"),
                 ("与特殊词元同形的文本", "special_like", "个",
                  "消息内容中出现 <|user|>、<think> 等与特殊词元同形的文本时，分词器将其切分为特殊词元，存在内容被解析为控制标记的风险"),
                 ("≥4 个连续空格或 Tab", "ws_runs_ge4", "段", "对应代码缩进，其数量反映代码类文本的比重")]
        ft = []
        for lab, k, u, why in feats:
            hit = sum(1 for r in text if r.get(k))
            ft.append([f"{lab}（{u}）", f"{num(hit)}（{pct(hit / nt)}）", num(analyze.q([r.get(k) for r in text], .5)),
                       num(analyze.q([r.get(k) for r in text], .99)), max((r.get(k) or 0 for r in text), default=0), why])
        b256 = S.get(("content", "bytes_in_words_ge256b_share"))
        real256 = S.get(("tokenizer", "pretokens_ge256b"))
        sp = sum(1 for r in text if r.get("special_like"))
        fnd2 = ""
        if b256 is not None:
            fnd2 += f"连续无空白片段 ≥256 字节的内容占全文字节数的 {B(pct(b256))}。"
            if real256 is not None and S.get(("tokenizer", "pretokens_total")):
                fnd2 += (f"经分词器预分词后，长度 ≥256 字节的片段为 {B(num(real256))} 个，"
                         f"占全部 {big(S[('tokenizer', 'pretokens_total')])} 个片段的 {pct_small(real256 / S[('tokenizer', 'pretokens_total')])}。")
        fnd2 += f"含与特殊词元同形文本的请求为 {B(num(sp))} 个，占 {B(pct(sp / nt))}。"
        sc0 = sc[0] if sc else None
        P.append("<div class='grid2'>"
                 + fig("请求按主要文字的分类", hbars([script_cn.get(k, k) for k, _ in sc], [v for _, v in sc], color=PALETTE[2], unit=" 个",
                                            sub=[pct(v / nt) for _, v in sc], lab_w=190),
                       read=f"各请求按其文本中非空白字符的构成归类，归类阈值见类别名称。柱长：请求数。柱右标注：请求数 · 占 {num(nt)} 个含全文请求的比例。",
                       finding=(f"「{E(script_cn.get(sc0[0], sc0[0]))}」类别的请求占 {B(pct(sc0[1] / nt))}。" if sc0 else ""))
                 + "<div></div></div>"
                 + fig("与分词开销和正确性相关的文本特征", table(
                     ["特征", ("出现的请求数", "占含全文请求的比例"), ("每请求个数", "中位数"), ("每请求个数", "P99"), ("每请求个数", "最大值"), "与分词的关系"], ft,
                     {1, 2, 3, 4}, {1}, {5}),
                       read="<b>出现的请求数</b>：含该特征至少 1 处的请求数。<b>每请求个数</b>：单个请求中该特征的出现次数，列出中位数、P99 与最大值。",
                       finding=fnd2))
    else:
        P.append("<p class='def'>未采集到含全文的请求（text_rate=0），本节无数据。</p>")

    # ---- 4 会话
    P.append(h2("session", "4", "会话与前缀复用", "proxy"))
    P.append(defs([("会话", "一次连续对话或一个 agent 任务自开始至结束的全部请求。以请求头中的会话标识区分，依次取 x-session-id、x-session-affinity、session_id、x-conversation-id 中第一个非空者（由采集配置的 session_headers 指定）；请求头不含会话标识时，以 messages 开头至第一条 user 消息（含）的内容哈希区分"),
                   ("线程", "同一会话内 system 提示相同的请求序列。客户端在主对话之外发出的旁路请求（生成标题、压缩历史）使用不同的 system 提示，各自构成独立线程，不与主对话比较"),
                   ("请求类型", "本请求的 messages 相对同线程上一请求的 messages 的变化"),
                   ("前缀复用率", "本请求开头与上一请求完全相同的消息的字符数 / 本请求全部消息的字符数。" + reuse_eg(rows))], raw=True))
    kcl = kc.most_common()
    rr = [r["reuse_ratio_prev"] for r in rows if r.get("reuse_ratio_prev") is not None]
    hb = collections.Counter(min(int(x * 10), 9) for x in rr)
    app = sum(v for k, v in kc.items() if k.startswith("append"))
    P.append("<div class='grid2'>"
             + fig("请求类型", hbars([kind_cn.get(k, k) for k, _ in kcl], [v for _, v in kcl], lab_w=190, unit=" 个", sub=[pct(v / tot_k) for _, v in kcl]),
                   read=f"柱长：请求数。柱右标注：请求数 · 占 {num(tot_k)} 个请求的比例。各类型的判定条件见数据表。",
                   finding=(f"以追加方式延续上一请求的请求占 {B(pct(app / tot_k))}；"
                            f"请求数最多的类型为「{E(kind_cn.get(kcl[0][0], kcl[0][0]))}」，占 {B(pct(kcl[0][1] / tot_k))}。" if kcl else ""),
                   data_table=table(["类型", "请求数", "占比", "判定条件"], [[kind_cn.get(k, k), v, pct(v / tot_k), kind_def.get(k, "")] for k, v in kcl], {1, 2}, None, {3}))
             + (fig("会话内前缀复用率的分布", vbars([f"{i * 10}–{(i + 1) * 10}" for i in range(10)], [hb.get(i, 0) for i in range(10)], color=PALETTE[2],
                                          sub=[pct(hb.get(i, 0) / len(rr)) for i in range(10)], xlab="前缀复用率（%）", ylab="请求数"),
                    read=f"横轴：前缀复用率，每 10 个百分点为一档。纵轴：请求数。柱顶标注：请求数；占 {num(len(rr))} 个请求的比例。"
                         "线程首个请求无上一请求，不计入。",
                    finding=f"前缀复用率不低于 80% 的请求占 {B(pct(sum(hb.get(i, 0) for i in (8, 9)) / len(rr)))}；"
                            f"低于 50% 的请求占 {B(pct(sum(hb.get(i, 0) for i in range(5)) / len(rr)))}。") if rr else "")
             + "</div>")
    if sessions:
        edges = [1, 2, 3, 5, 10, 20, 50, 100]
        lab_e = ["1", "2", "3–4", "5–9", "10–19", "20–49", "50–99", "≥100"]
        bounds = list(zip(edges, edges[1:] + [10**9]))
        cnt_e = [sum(1 for s in sessions if lo <= s["n_requests"] < hi) for lo, hi in bounds]
        req_e = [sum(s["n_requests"] for s in sessions if lo <= s["n_requests"] < hi) for lo, hi in bounds]
        keep = [i for i, c in enumerate(cnt_e) if c] or [0]
        i0, i1 = keep[0], keep[-1]
        lab_e, cnt_e, req_e = lab_e[i0:i1 + 1], cnt_e[i0:i1 + 1], req_e[i0:i1 + 1]
        ns, nr = len(sessions), sum(req_e) or 1
        nreq = [s["n_requests"] for s in sessions]
        long_s = sum(1 for x in nreq if x >= 10)
        long_r = sum(x for x in nreq if x >= 10)
        P.append(defs([("会话内请求数", "采集时间范围内，一个会话向 proxy 发出的请求数。agent 客户端每执行一次工具调用即发出一个新请求；"
                                  "对话类客户端每轮用户输入发出一个请求。"
                                  + session_eg(sessions, rows)),
                       ("用途", "长会话中各请求重发的历史随步数增长。长会话的请求占比决定前缀缓存、会话亲和路由与分段缓存的适用范围")], raw=True))
        P.append("<div class='grid2'>"
                 + fig("会话数，按会话内请求数分档", vbars(lab_e, cnt_e, sub=[pct(c / ns) for c in cnt_e], xlab="会话内请求数", ylab="会话数"),
                       read=f"横轴：会话内请求数。纵轴：会话数。柱顶标注：会话数；占全部 {num(ns)} 个会话的比例。",
                       finding=f"会话内请求数的中位数为 {B(num(analyze.q(nreq, .5)))}，P90 为 {B(num(analyze.q(nreq, .9)))}，最大值为 {B(num(max(nreq)))}。",
                       data_table=table(["会话内请求数", "会话数", "占会话数", "请求数", "占请求数"],
                                        [[a_, b_, pct(b_ / ns), c_, pct(c_ / nr)] for a_, b_, c_ in zip(lab_e, cnt_e, req_e)], {1, 2, 3, 4}))
                 + fig("各档会话的请求占比", vbars(lab_e, [c / nr * 100 for c in req_e], unit="%", color=PALETTE[1], fmt=lambda v: f"{v:.1f}",
                                          sub=[f"{num(c)} 个" for c in req_e], xlab="会话内请求数", ylab="占比"),
                       read=f"横轴：会话内请求数。纵轴：该档会话发出的请求占全部 {num(nr)} 个含会话标识的请求的比例。柱顶标注：请求占比；请求数。",
                       finding=f"请求数不少于 10 的会话占会话数的 {B(pct(long_s / ns))}，其请求占全部请求的 {B(pct(long_r / nr))}。")
                 + "</div>")

    # ---- 5 vLLM
    P.append(h2("vllm", "5", "vLLM 侧：前缀缓存与耗时", "vllm", "timer"))
    P.append(defs([("cached_tokens", "P 实例响应 usage.prompt_tokens_details.cached_tokens：prompt 开头已存在于 vLLM 前缀缓存、无需重新 prefill 的 token 数"),
                   ("前缀缓存命中率", "cached_tokens 之和 / prompt_tokens 之和（token 加权）"),
                   ("prefill_ms", "proxy 发出 P 请求至收到 P 响应的时间，含 P 实例上的排队、模板渲染、分词与 prefill；单位毫秒"),
                   ("ttft_ms", "请求到达 proxy 至 D 第一块响应数据到达的时间；并不保证该块包含首 token。单位毫秒，仅统计流式请求；普通 proxy 未接入 P/D 钩子时缺失"),
                   ("P50 / P95", "样本按数值升序排列后位于 50% / 95% 位置的值；P50 即中位数")]))
    p50, p95, hit, cnts, hit_abs = [], [], [], [], []
    for b in labs:
        xs = [r.get("ttft_ms") for r in rows if r.get("stream") and isinstance(r.get("prompt_tokens"), int) and analyze.bucket_label(r["prompt_tokens"]) == b]
        p50.append(analyze.q(xs, .5))
        p95.append(analyze.q(xs, .95))
        cnts.append(sum(1 for x in xs if x is not None))
        sub_ = [r for r in rows if isinstance(r.get("prompt_tokens"), int) and analyze.bucket_label(r["prompt_tokens"]) == b]
        pt_ = sum(r["prompt_tokens"] for r in sub_)
        ct_ = sum(r.get("cached_tokens") or 0 for r in sub_)
        hit.append(ct_ / pt_ * 100 if pt_ else 0)
        hit_abs.append((ct_, pt_))
    have = [(b, a, c) for b, a, c in zip(labs, p50, p95) if a is not None]
    f_ttft = (f"TTFT 中位数在 {E(have[0][0])} 区间为 {B(num(have[0][1], 1))} ms，在 {E(have[-1][0])} 区间为 {B(num(have[-1][1], 1))} ms；"
              f"P95 分别为 {B(num(have[0][2], 1))} ms 与 {B(num(have[-1][2], 1))} ms。") if len(have) >= 2 else ""
    all_hit = S.get(("vllm", "cache_hit_ratio_weighted"))
    f_hit = ""
    if labs:
        lo_i = min(range(len(labs)), key=lambda i: hit[i])
        hi_i = max(range(len(labs)), key=lambda i: hit[i])
        f_hit = (f"全部请求的命中率为 {B(pct(all_hit))}；各区间中最高为 {B(labs[hi_i])}（{B(f'{hit[hi_i]:.1f}%')}），最低为 {B(labs[lo_i])}（{B(f'{hit[lo_i]:.1f}%')}）。")
        ih, ah = S.get(("tokenizer", "ideal_prefix_cache_hit")), S.get(("tokenizer", "actual_prefix_cache_hit_same_requests"))
        if ih is not None and ah is not None:
            f_hit += (f"在已做离线分词的请求上，实际命中率为 {B(pct(ah))}，容量不限、全局共享的前缀缓存的重放命中率为 {B(pct(ih))}，"
                      f"<mark>相差 {B(f'{(ih - ah) * 100:.1f}')} 个百分点</mark>，对应路由分散、缓存容量与淘汰造成的未命中。")
    P.append("<div class='grid2'>"
             + fig("TTFT，按 prompt 长度分组", grouped(labs, [("P50", p50), ("P95", p95)], unit=" ms", sub=[f"n={num(c)}" for c in cnts], xlab="prompt_tokens 区间"),
                   read="横轴：prompt_tokens 区间，其下 n 为该区间的流式请求数。纵轴：TTFT，单位毫秒。",
                   finding=f_ttft, badge=src("timer"),
                   data_table=table(["区间", "流式请求数", ("P50", "ms"), ("P95", "ms")], [[b, c_, num(a, 1), num(c, 1)] for b, c_, a, c in zip(labs, cnts, p50, p95)], {1, 2, 3}))
             + fig("前缀缓存命中率，按 prompt 长度分组", vbars(labs, hit, unit="%", color=PALETTE[2], fmt=lambda v: f"{v:.1f}",
                                                 sub=[f"{big(c)} / {big(p)}" for c, p in hit_abs], xlab="prompt_tokens 区间", ylab="命中率"),
                   read="横轴：prompt_tokens 区间。纵轴：该区间 cached_tokens 之和 / prompt_tokens 之和。柱顶标注：命中率；cached_tokens 之和 / prompt_tokens 之和。",
                   finding=f_hit, badge=src("vllm"),
                   data_table=table(["区间", "cached_tokens", "prompt_tokens", "命中率"], [[b, c, p, f"{h_:.1f}%"] for b, (c, p), h_ in zip(labs, hit_abs, hit)], {1, 2, 3}))
             + "</div>")
    lat = []
    for col, name, what in (("prefill_ms", "prefill_ms", "proxy 发出 P 请求至收到 P 响应"), ("ttft_ms", "ttft_ms（流式）", "请求到达 proxy 至 D 首块数据"),
                            ("e2e_ms", "e2e_ms", "请求到达 proxy 至响应结束"), ("tpot_ms", "tpot_ms（流式）", "首个 token 之后相邻输出 token 的平均间隔")):
        d = S.get(("vllm", f"{col}_p50"))
        if d is not None:
            lat.append([name, what, num(S.get(("vllm", f"{col}_count"))), num(d, 1), num(S.get(("vllm", f"{col}_p90")), 1),
                        num(S.get(("vllm", f"{col}_p99")), 1), num(S.get(("vllm", f"{col}_max")), 1)])
    if lat:
        P.append(fig("耗时分布", table(["指标", "含义", ("样本数", "请求"), ("P50", "ms"), ("P90", "ms"), ("P99", "ms"), ("最大值", "ms")], lat, {2, 3, 4, 5, 6}, {3}, {1}),
                     read="单位毫秒。P50 / P90 / P99 为样本按数值升序排列后位于 50% / 90% / 99% 位置的值。", badge=src("timer")))

    # ---- 6 分词器
    if extra:
        P.append(h2("tokenizer", "6", "分词器特征", "off", "sim"))
        n_tok = S.get(("tokenizer", "tokenized_requests"))
        P.append(f"<p class='read'>本节数值由 analyze.py 在本机以 <code>--tokenizer</code> 指定的分词器，对含全文的 {B(num(n_tok))} 个请求重新渲染模板并分词得出。"
                 "片段数、每 token 字节数等计数结果与机器无关；词缓存命中率为将这些请求按到达顺序、按缓存规则逐条重放所得。</p>")
        ex = extra.get("example")
        eg, eg_len = "", ""
        if ex and ex.get("pieces"):
            vis = lambda t: E(t.replace("\n", "\\n").replace(" ", "␣"))  # noqa: E731
            bpe = [p for p in ex["pieces"] if not p["direct"]]
            eg = ("<span class='eg'>本次分词器的实际输出：文本 <code>" + vis(ex["text"]) + f"</code> 切分为 {len(ex['pieces'])} 个片段："
                  + " ".join(f"<code>{vis(p['text'])}</code>" for p in ex["pieces"]) + "。其中 "
                  + f"{len(ex['pieces']) - len(bpe)} 个经整词查表得到 token，{len(bpe)} 个需在片段内执行 BPE 合并"
                  + (f"（<code>{vis(bpe[0]['text'])}</code> 合并为 {bpe[0]['n_tokens']} 个 token）" if bpe else "")
                  + f"；全文共 {ex['n_tokens']} 个 token。</span>")
            diff = [p for p in ex["pieces"] if p["bytes"] != len(p["text"].encode("utf-8"))]
            if diff:
                eg_len = (f"<span class='eg'>本数据：片段 <code>{vis(diff[0]['text'])}</code> 的原文为 {len(diff[0]['text'].encode('utf-8'))} 字节，"
                          f"内部表示为 {diff[0]['bytes']} 字节。</span>")
        P.append(defs([("预分词片段", "分词分两步：先将文本切分为片段（按空白、标点与特殊词元切分），再在各片段内执行 BPE 合并得到 token。"
                                 "第一步产生的片段称为预分词片段。" + eg),
                       ("片段长度", "片段在分词器内部表示下的 UTF-8 字节数；词缓存的长度上限按此值判断。" + eg_len),
                       ("整词查表", "片段整体存在于词表时，ignore_merges=true 的分词器直接查表得到 1 个 token，不执行 BPE，不经过词缓存"),
                       ("词缓存", f"tokenizers 0.23.2 为 BPE 设置的缓存，保存「片段 → token 序列」的映射，片段再次出现时直接返回结果。"
                               f"规则：每个分词线程持有一份；容量 {num(args.cache_cap)} 个片段；仅缓存长度小于 {num(args.cache_maxlen)} 字节的片段；"
                               "<b>写满后不再插入，也不淘汰</b>"),
                       ("重放方法", "将含全文的请求按到达顺序排列，以一份空缓存、单线程，按上述规则逐请求、逐片段重放并统计命中次数。"
                                "<b>跨请求</b>：缓存在请求之间保留。<b>仅请求内</b>：每个请求开始时清空缓存，仅统计同一请求内重复片段的命中")], raw=True))
        vd, ww, wc_ = S.get(("tokenizer", "vocab_direct_frac")), S.get(("tokenizer", "word_cache_hit_warm")), S.get(("tokenizer", "word_cache_hit_cold"))
        filled = S.get(("tokenizer", "word_cache_filled"))
        kt = [kpi(pct(vd), "", "整词查表的片段比例", "不执行 BPE，不经过词缓存", "off"),
              kpi(pct(ww), "", "词缓存命中率（跨请求）", "经过词缓存的片段中，缓存在请求之间保留时的命中比例", "sim"),
              kpi(pct(wc_), "", "词缓存命中率（仅请求内）", "经过词缓存的片段中，每个请求开始时清空缓存时的命中比例", "sim"),
              kpi(num(S.get(("tokenizer", "bytes_per_token")), 2), "字节", "每个 token 的字节数", "模板后文本的字节数 / token 数", "off"),
              kpi(pct(S.get(("tokenizer", "incremental_new_token_share"))), "", "需重新处理的 token 占比", "与同线程上一请求的公共前缀之后的 token 数 / 全部 token 数；比较粒度 16 token", "off"),
              kpi(num(S.get(("tokenizer", "pretokens_ge256b"))), "个", f"长度 ≥{num(args.cache_maxlen)} 字节的片段数", "不经过词缓存，每次出现均执行 BPE", "off")]
        P.append("<div class='kpis wide'>" + "".join(kt) + "</div>")
        if vd is not None and ww is not None:
            need = (1 - vd) * (1 - ww)
            f6 = (f"每 100 个预分词片段中，{B(f'{vd * 100:.1f}')} 个经整词查表得到 token，{B(f'{(1 - vd) * 100:.1f}')} 个经过词缓存；"
                  f"缓存在请求之间保留时，后者的命中率为 {B(pct(ww))}，<mark>需执行 BPE 合并的片段为 {B(f'{need * 100:.2f}')} 个</mark>。")
            if wc_ is not None:
                f6 += (f"每个请求开始时清空缓存时，命中率为 {B(pct(wc_))}；两种条件相差 {B(f'{(ww - wc_) * 100:.1f}')} 个百分点，来自跨请求复用。")
            if filled is not None:
                f6 += (f"重放结束时缓存含 {B(num(filled))} 个片段，"
                       + (f"<mark>已达到 {num(args.cache_cap)} 的容量上限，其后出现的新片段未被缓存</mark>。" if filled >= args.cache_cap
                          else f"未达到 {num(args.cache_cap)} 的容量上限。"))
            P.append(f"<div class='finding'><span class='lbl'>结论</span>{f6}</div>")
        tr = [r for r in rows if r.get("tok_src")]
        bs = collections.defaultdict(lambda: [0, 0, 0.0, 0])
        for r in tr:
            if r.get("render_bytes") and r.get("_n_ids"):
                d_ = bs[r.get("script")]
                d_[0] += r["render_bytes"]
                d_[1] += r["_n_ids"]
                d_[2] += r.get("encode_ms") or 0
                d_[3] += 1
        bsl = sorted(bs.items(), key=lambda x: -x[1][1])
        ln = extra.get("len") or []
        jobs = getattr(args, "jobs", 1) or 1
        P.append("<div class='grid2'>"
                 + fig("每个 token 的字节数，按请求的主要文字分组",
                       hbars([script_cn.get(k, k) for k, _ in bsl], [b / t for _k, (b, t, _m, _c) in bsl],
                             fmt=lambda v: f"{v:.2f}", lab_w=190, unit=" 字节/token", sub=[f"n={num(c)}" for _k, (_b, _t, _m, c) in bsl]),
                       read="柱长：该组请求的模板后文本字节数之和 / token 数之和。柱右标注：字节/token · 请求数。本机分词吞吐见数据表。",
                       finding=(f"各组的每 token 字节数介于 {B(f'{min(b / t for _k, (b, t, _m, _c) in bsl):.2f}')} 与 "
                                f"{B(f'{max(b / t for _k, (b, t, _m, _c) in bsl):.2f}')} 之间。" if bsl else ""),
                       warn=(f"数据表中的吞吐在本机以 --jobs {jobs} 并行测得，受进程间争用影响，数值低于单进程实测值；需引用的数值应以 --jobs 1 重新测量。" if jobs > 1
                             else "数据表中的吞吐为本机单进程实测值，仅用于各组之间的相对比较，不代表线上机器的吞吐。"),
                       badge=src("off"),
                       data_table=table(["主要文字", "请求数", "字节数", "token 数", "字节/token", ("本机吞吐", "MB/s")],
                                        [[script_cn.get(k, k), c, b, t, f"{b / t:.2f}", f"{b / 2**20 / (m / 1000):.2f}" if m else "–"]
                                         for k, (b, t, m, c) in bsl], {1, 2, 3, 4, 5}))
                 + fig("预分词片段的长度分布", vbars([x["bytes"] for x in ln], [x["share"] * 100 for x in ln], unit="%", color=PALETTE[1],
                                            fmt=lambda v: f"{v:.1f}", sub=[big(x["count"]) for x in ln], xlab="片段长度（字节）", ylab="占比"),
                       read="横轴：片段长度，单位字节，按分词器内部表示计。纵轴：占全部片段的比例。柱顶标注：占比；片段数。",
                       finding=(f"长度不超过 4 字节的片段占 {B(pct(sum(x['share'] for x in ln[:4])))}；"
                                f"长度 ≥{num(args.cache_maxlen)} 字节、不经过词缓存的片段为 {B(num(ln[-1]['count']))} 个，"
                                f"占 {B(pct_small(ln[-1]['count'] / (sum(x['count'] for x in ln) or 1)))}。" if ln else ""),
                       badge=src("off"),
                       data_table=table(["长度（字节）", "片段数", "占比"], [[x["bytes"], x["count"], pct(x["share"], 2)] for x in ln], {1, 2}))
                 + "</div>")
        ws = extra.get("warm_series") or []
        if len(ws) >= 2:
            k = max(1, len(ws) // 300)
            xs = list(range(0, len(ws), k))
            ys = [statistics_mean([y for _t, y in ws[i:i + k]]) * 100 for i in xs]
            head_ = statistics_mean(ys[:max(1, len(ys) // 10)])
            tail_ = statistics_mean(ys[-max(1, len(ys) // 10):])
            P.append(fig("词缓存命中率（跨请求）随请求序号的变化", line(xs, ys, ylab="命中率 %", ymin=0, ymax=100, yfmt=lambda v: f"{v:.0f}", xfmt=lambda v: f"{v:.0f}"),
                         read="横轴：请求序号，按到达顺序。纵轴：该请求中经过词缓存的片段的命中率"
                              + (f"，每点为相邻 {k} 个请求的均值" if k > 1 else "") + "。重放自空缓存开始。",
                         finding=f"前 10% 的请求平均命中率为 {B(f'{head_:.1f}%')}，后 10% 的请求平均命中率为 {B(f'{tail_:.1f}%')}。",
                         badge=src("sim")))
        top = extra.get("top") or []
        P.append("<figure><h3>出现次数最多的预分词片段 " + src("off") + "</h3><p class='read'><b>次数</b>：该片段在全部含全文请求中出现的总次数。"
                 "<b>占比</b>：占全部片段的比例。<b>长度</b>：片段在分词器内部表示下的字节数。<b>整词在词表</b>：「是」表示经整词查表得到 1 个 token。"
                 "␣ 表示空格，\\n 表示换行。完整列表见 pretokens_top.csv。</p>"
                 f"<details><summary>前 {len(top)} 个片段</summary>"
                 + table(["#", "片段文本", "次数", "占比", ("长度", "字节"), "整词在词表", "token 数"],
                         [[t["rank"], t["text"].replace("\n", "\\n").replace(" ", "␣"), t["count"], pct(t["share"], 2), t["bytes"],
                           "是" if t["in_vocab"] else "否", t["n_tokens"]] for t in top], {2, 3, 4, 6}) + "</details></figure>")

    # ---- 附录
    sec = collections.OrderedDict()
    for s in summ:
        sec.setdefault(s[0], []).append(s)
    P.append("<h2 id='all'><span>附录：全部汇总指标</span><span class='sub'>summary.csv</span></h2><div class='appendix'>")
    for k, items in sec.items():
        P.append(f"<details><summary>{E(k)}（{len(items)} 项）</summary>"
                 + table(["指标", "值", "单位", "说明"], [[m, v, u, nt] for _s, m, v, u, nt in items], {1}) + "</details>")
    P.append("</div><p class='foot'>由 prompt_capture/tools/analyze.py 生成。指标定义见 docs/REFERENCE.md §7。</p>")
    doc = (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Proxy 请求特征报告</title><style>{CSS}</style></head><body><main>{''.join(P)}</main><script>{JS}</script></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def statistics_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else math.nan
