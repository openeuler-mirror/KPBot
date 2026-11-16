# anatomy.py：把一个会话里的每次请求「解剖」成分词器真正处理的片段，并算出相邻请求之间的变化。
# 被 session_view.py 使用；只依赖标准库 + pcap.py，token 级信息需要 --tokenizer（transformers）。
#
# 一次请求 = proxy 收到的 messages + tools + 参数 → vLLM 套模板得到一段文本 → 分词器把它切成 token。
# 这里把模板后文本按来源切成不重叠的片段（segment），每个片段有字符区间与 token 区间：
#   prefix               模板开头（[gMASK]<sop>、Reasoning Effort 等，任何消息之前的部分）
#   tools_def            模板渲染出的工具定义块
#   system / user        消息正文
#   assistant.content    assistant 的回复正文
#   assistant.reasoning  assistant 回传的思考内容（模板可能删掉：见 status=stripped）
#   assistant.tool_call  工具调用（函数名 + 参数值）
#   tool.result          工具结果（role=tool 的 content）
#   markup               模板加的标记（<|user|>、<tool_call>、<arg_key> 等），归到它前面/所属的消息
#   gen_prompt           末尾的生成提示（<|assistant|><think>）
# 定位方法：按消息顺序，在模板后文本里从当前游标向后查找每个片段的原文；找不到就记为「被模板丢弃」，游标不动。
# 相邻两次请求的比较同时做两层：消息层（采集时的消息链节点哈希）与 token 层（两段 token ids 的最长公共前缀）。
# 两层结论不一致（消息没变但 token 前缀断了）就是模板改写了历史：例如 GLM-5.2 的模板只保留最后一条 user 消息之后的
# assistant reasoning，新的 user 消息一到，前面所有 <think>…</think> 都变成 <think></think>。
from __future__ import annotations

import bisect
import hashlib
import json
import os
import sys
import re
from typing import Any

import pcap

KINDS = ["prefix", "tools_def", "system", "user", "assistant.content", "assistant.reasoning", "assistant.tool_call",
         "tool.result", "markup", "gen_prompt"]
KIND_LABEL = {"prefix": "模板开头", "tools_def": "工具定义", "system": "system", "user": "user",
              "assistant.content": "assistant 正文", "assistant.reasoning": "assistant 思考",
              "assistant.tool_call": "工具调用", "tool.result": "工具结果", "markup": "模板标记", "gen_prompt": "生成提示"}


def h8(s: str) -> str:
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def _text_of_content(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p if isinstance(p, str) else (p.get("text") or "") for p in c
                       if isinstance(p, str) or (isinstance(p, dict) and p.get("type") == "text"))
    return ""


# 模板的控制标记：GLM / Qwen / Kimi 用 <|…|>，DeepSeek 用全角竖线 <｜…｜>（内部可含 ▁）。角色标记、工具调用边界都属于这一类
MARKER_RE = re.compile(r"<[|｜][^<>]{1,48}[|｜]>|\]~!b\[|\]~b\]|\[e~\[")  # <|x|>、全角 <｜x｜>、MiniMax 的 ]~!b[ ]~b] [e~[
# 一条消息的收尾标记：<|im_end|>（Qwen、Kimi）、<|end|> <|call|> <|return|>（gpt-oss）、<｜end▁of▁sentence｜>（DeepSeek）、<|eot_id|>、
# <|tool_call_end|> 之类；归上一条消息，不归下一条
END_RE = re.compile(r"<[|｜](?:[^<>|｜]*(?:end|eos|eot)[^<>|｜]*|call|return|close)[|｜]>|\[e~\[|</s>")  # Kimi K3 的 <|close|>标签<|sep|> 也是收尾


def _find_marker(text: str, start: int, end: int | None = None) -> int:
    m = MARKER_RE.search(text, start, len(text) if end is None else end)
    return m.start() if m else -1


def _rfind_marker(text: str, start: int) -> int:
    last = -1
    for m in MARKER_RE.finditer(text, start):
        last = m.start()
    return last


def _esc(t: str) -> str:
    """JSON 转义后的形态（模板用 tojson 渲染时换行成 \\n、引号成 \\"）。"""
    return json.dumps(t, ensure_ascii=False)[1:-1]


def _list_alts(content: list) -> list[str]:
    """列表形式 content 在不同模板下的整体形态：JSON 列表、Python repr 列表、逐项 repr / JSON 直接拼接或换行拼接。"""
    parts_repr = [str(p) for p in content]
    parts_json = [json.dumps(p, ensure_ascii=False) for p in content]
    return [json.dumps(content, ensure_ascii=False), str(content), "".join(parts_repr), "\n".join(parts_repr),
            "".join(parts_json), "\n".join(parts_json)]


def _alts(t: str) -> list[str]:
    """字符串 content 的其他形态：JSON 转义；以及 vLLM 按 openai 格式把字符串包成 [{"type":"text","text":…}] 后，
    模板按 JSON / Python repr 逐项输出的形态（GLM-4.7 对 tool 消息就是这样）。"""
    e = _esc(t)
    out = [e] if e != t else []
    part = {"type": "text", "text": t}
    return out + [json.dumps(part, ensure_ascii=False), str(part)]


def _pieces(msgs: list) -> list[tuple[int, str, str, str | None]]:
    """每条消息拆成会进入模板的原文片段：(消息下标, 片段种类, 原文, 工具名)。顺序与常见模板的输出顺序一致。"""
    return [(i, k, t, tool) for (i, k, t, tool, _alts_, _grp) in _pieces_ext(msgs)]


def _pieces_ext(msgs: list) -> list[tuple[int, str, str, str | None, list[str], tuple | None]]:
    """同 _pieces，另带：alts = 该片段在模板后文本里可能出现的其他形态（JSON 转义、列表 content 的 JSON / Python repr、
    整个 arguments 串），group = ((消息下标, 调用序号), 整串形态元组)：同一次工具调用的各参数值片段共用，整串匹配成功后其余参数值不再单独找。"""
    out: list = []
    call_tool: dict[str, str] = {}
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            r = m.get("reasoning") if isinstance(m.get("reasoning"), str) else m.get("reasoning_content")
            c = _text_of_content(m.get("content"))
            full_c = None
            if "</think>" in c:
                # 客户端把思考写在 content 里（<think>…</think>正文）：模板按 </think> 拆成思考与正文（GLM-5.2 模板第 67～69 行），
                # 这里同样拆开，思考按 assistant.reasoning 处理（可能被模板删除）；有的模板（Kimi）原样输出整段，整段作为正文的备选形态。
                # 同时还有 reasoning 字段时（客户端的 bug），正文里的思考段单独作为一个思考片段（Qwen3-Coder 先输出它再输出 reasoning 字段）
                full_c = c.strip()
                before = c.split("</think>")[0]
                think_part = before.split("<think>")[-1]
                # 思考段里还有 <think> 字面量时，模板可能从第一个 <think> 起算、也可能把里面的 <think> 去掉：都作为备选形态
                think_alts = [x for x in (before.split("<think>", 1)[-1].strip(), before.replace("<think>", "").strip()) if x and x != think_part.strip()]
                c = c.split("</think>")[-1]
                if not isinstance(r, str):
                    r = think_part
                    r_alts = think_alts
                else:
                    r_alts = []
                    if think_part.strip():
                        out.append((i, "assistant.reasoning", think_part, None, _alts(think_part) + think_alts, None))
            else:
                r_alts = []
            if isinstance(r, str) and r:
                out.append((i, "assistant.reasoning", r, None, _alts(r) + r_alts, None))
            if not c.strip() and full_c:
                c = full_c  # 正文整个都是 <think>…</think>：模板原样输出时按正文认领（GLM-5.2 对带 reasoning 字段的消息不拆）
            if c.strip():
                alts_c = _alts(c.strip()) + ([full_c] if full_c and full_c != c.strip() else [])
                if full_c and full_c.count("</think>") > 1:
                    after_first = full_c.split("</think>", 1)[1].strip()
                    if after_first and after_first != c.strip():
                        alts_c.append(after_first)  # 原样输出整段的模板（Kimi）：第一个 </think> 之后的全部都是正文（GLM 只保留最后一个之后的）
                if "<think>" in c or "</think>" in c:
                    alts_c.append(re.sub(r"</?think>", "", c).strip())  # 有的模板（Nemotron）把正文里的 <think> 字面量去掉
                    if "</think>" in c:
                        alts_c.append(c.split("</think>")[-1].strip())  # 或者把 </think> 之前的都当思考去掉
                    elif "<think>" in c:
                        alts_c.append(c.split("<think>")[0].strip())  # 或者从 <think> 起截断
                out.append((i, "assistant.content", c.strip(), None, [a_ for a_ in alts_c if a_], None))
            for ci, call in enumerate(m.get("tool_calls") or []):
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                name = str(fn.get("name") or "")
                if isinstance(call, dict) and call.get("id"):
                    call_tool[str(call["id"])] = name
                a = fn.get("arguments")
                raw_args = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
                if name:
                    own = [raw_args] if raw_args and raw_args.strip() not in ("", "{}") else []
                    if own and isinstance(a, str):
                        try:
                            parsed_ = json.loads(a)
                            if isinstance(parsed_, dict) and parsed_:
                                own.append(json.dumps(parsed_, ensure_ascii=False))  # vLLM 解析后模板再 tojson 的紧凑形态
                        except ValueError:
                            pass
                    out.append((i, "assistant.tool_call", name, name, own, ((i, ci), None)))  # 函数名：group 的整串形态为 None；alts 放本次调用的整串参数（原文 / 紧凑）
                if isinstance(a, str):
                    try:
                        a = json.loads(a)
                    except ValueError:
                        a = None
                    if not isinstance(a, dict) and raw_args.strip():
                        # 不是 JSON 对象（非法 JSON、JSON 字符串 / 数组 / null）：模板只能整串输出（Kimi K3 放进 <|open|>json …）
                        out.append((i, "assistant.tool_call", raw_args, name, _alts(raw_args) + [raw_args.strip()], ((i, ci), (raw_args,))))
                        a = None
                if isinstance(a, dict):
                    grp = (i, ci)
                    first = True
                    lits = _arg_literals(raw_args) if isinstance(fn.get("arguments"), str) else {}
                    for key, v in a.items():  # 模板通常输出参数值；非字符串值按 JSON 输出，有的模板按 Python 形式输出（True/None/['a']）
                        s_ = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                        if not s_.strip():
                            continue  # 空的或只有空白的值无法定位；整串 arguments 的形态挂到第一个有内容的值上
                        alts = _alts(s_) + ([str(v)] if not isinstance(v, str) and str(v) != s_ else [])
                        if not isinstance(v, str) and lits.get(key) and lits[key] != s_:
                            alts.append(lits[key])  # 原始字面量（Kimi K3 原样输出 1e2、[1,2] 这类非字符串值的原文）
                        whole: list[str] = []
                        if first:  # 有的模板整串输出 arguments（原文或转义）：作为第一个参数值的备选形态，命中后本次调用的其余参数值不再找
                            whole = [raw_args] + _alts(raw_args) + [json.dumps(a, ensure_ascii=False)]
                            first = False
                        out.append((i, "assistant.tool_call", s_, name, alts + whole, (grp, tuple(whole))))
        elif role == "tool":
            out += _content_pieces(i, "tool.result", m.get("content"), call_tool.get(str(m.get("tool_call_id"))))
        else:
            out += _content_pieces(i, "system" if role in ("system", "developer") else "user", m.get("content"), None)
    return out


def _arg_literals(s: str) -> dict[str, str]:
    """arguments 为 JSON 字符串时，每个顶层值在原文里的字面量（只解析一层，解析失败就返回已得到的部分）。"""
    out: dict[str, str] = {}
    dec = json.JSONDecoder()
    n = len(s)
    i = s.find("{")
    if i < 0:
        return out
    i += 1
    while True:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n or s[i] == "}":
            break
        try:
            key, i = dec.raw_decode(s, i)
        except ValueError:
            break
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n or s[i] != ":":
            break
        i += 1
        while i < n and s[i] in " \t\r\n":
            i += 1
        st = i
        try:
            _v, i = dec.raw_decode(s, i)
        except ValueError:
            break
        if isinstance(key, str):
            out[key] = s[st:i]
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i < n and s[i] == ",":
            i += 1
    return out


def _content_pieces(i: int, kind: str, content: Any, tool: str | None) -> list:
    """一条消息的 content 拆成片段。字符串：一个片段。列表：每个文本部分一个片段（模板可能逐项输出、中间夹分隔标记，
    GLM-4.7 对多部分的 tool 消息就是每部分一对 <tool_response>）；整体形态（JSON 列表、repr 列表）作为第一个部分的备选。"""
    if isinstance(content, str):
        return [(i, kind, content, tool, _alts(content), None)] if content else []
    if not isinstance(content, list):
        return []
    out = []
    parts = [p for p in content if isinstance(p, str) or (isinstance(p, dict) and p.get("type") == "text")]
    for n, p in enumerate(parts):
        t = p if isinstance(p, str) else (p.get("text") or "")
        if not t:
            continue
        alts = _alts(t) + ([json.dumps(p, ensure_ascii=False), str(p)] if isinstance(p, dict) else [])
        if n == 0:
            alts += _list_alts(content)
        out.append((i, kind, t, tool, alts, None))
    return out


def _tool_pieces(tools: Any) -> list[str]:
    out = []
    for t in tools or []:
        fn = (t or {}).get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict):
            for k in ("name", "description"):
                if isinstance(fn.get(k), str) and fn[k]:
                    out.append(fn[k])
    return out


class Seg:
    __slots__ = ("kind", "msg", "tool", "c0", "c1", "t0", "t1", "blob")

    def __init__(self, kind: str, msg: int | None, tool: str | None, c0: int, c1: int):
        self.kind, self.msg, self.tool, self.c0, self.c1 = kind, msg, tool, c0, c1
        self.t0 = self.t1 = 0
        self.blob = ""

    def as_dict(self) -> dict:
        return {"k": self.kind, "m": self.msg, "tool": self.tool, "c0": self.c0, "c1": self.c1, "t0": self.t0,
                "t1": self.t1, "b": self.blob}


_JSON = json.JSONDecoder()
_THINK_LEAD = re.compile(r"(?:<think>|<[|｜]message[|｜]>|<\|open\|>think<\|sep\|>|" + END_RE.pattern + r")\s*$")  # 思考片段前面必须是 <think>（gpt-oss 为 <|message|>；DeepSeek 连续两条 assistant 时第二条紧跟上一条的收尾标记）
_DEBUG = bool(os.environ.get("ANATOMY_DEBUG"))
# 函数名之前、去掉标记后还允许出现的调用头文字：Qwen 的 {"name": "、"arguments": {}}；gpt-oss 的 to=functions. 与频道名；JSON 标点
_CALL_HEAD = re.compile(r'"name"|"arguments"|to=functions\.|functions\.|commentary|analysis|final|json|index="?\d{1,4}"?|[{}\[\]",:<>=/.\']|\\n')  # index="1"：Kimi K3 上一个调用标签在函数名之后的剩余属性  # 打印每个被判为丢弃的片段与其候选位置（排查归属问题用）


def _json_tool_at(text: str, k: int, name: str, desc: str = "") -> int:
    """text[k] 是 '{'：从这里解析一个 JSON 对象，若它是名为 name 的工具定义（{"name": …} 或 {"type": "function", "function": {"name": …}}），
    返回对象结束位置；否则 -1。按 JSON 解析可以跳过描述、枚举值里出现的角色标记字面量。
    给了 desc 时还要求对象里的描述与之相同（同名工具靠描述区分：模板跳过了前一个同名工具时不能把后一个认成它）。"""
    try:
        obj, end = _JSON.raw_decode(text, k)
    except ValueError:
        return -1
    if not isinstance(obj, dict):
        return -1
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    n = fn.get("name")
    if n != name:
        return -1
    d = fn.get("description")
    if desc and isinstance(d, str) and d.strip() != desc.strip():
        return -1
    return end


def _tool_anchor(text: str, name: str, desc: str, c: int) -> tuple[int, int, str] | None:
    """从 c 起找工具 name 的定义，返回 (起点, 终点, 形式)。按出现位置最靠前的形式为准：
    json  一个工具一个 JSON 对象（GLM、Qwen、MiniMax <tool>{…}</tool>、Kimi 的 JSON 数组、DeepSeek-V4、Mistral）：名字按 "name": "x" 找，
          对象起点取它前面最远的、能解析成该工具的 {（Qwen 的 {"type": "function", "function": {…}} 取外层）
    xml   <function>\n<name>x</name>…</function>（Qwen3-Coder、Nemotron）
    ts    // 描述\ntype x = (_: {…}) => any;（gpt-oss 的 TypeScript 命名空间）
    desc  以上都找不到时按描述文本定位（原文或 JSON 转义），名字只用来找起点"""
    best: tuple[int, int, str] | None = None

    def take(a: int, b: int, form: str) -> None:
        nonlocal best
        if a >= c and b > a and (best is None or a < best[0]):
            best = (a, b, form)

    for pat in (f'"name": "{name}"', f'"name":"{name}"'):
        k, tries = text.find(pat, c), 0
        while k >= 0 and tries < 32:
            tries += 1
            lo_ = max(c, k - 8192)  # 对象起点可能离 "name" 很远（Kimi K3 把键排序后 description 在 name 之前）
            p = text.rfind("{", lo_, k)
            cands = []
            while p >= lo_:
                cands.append(p)
                p = text.rfind("{", lo_, p)
            for p in reversed(cands):  # 最外层优先
                e = _json_tool_at(text, p, name, desc)
                if e > 0:
                    take(p, e, "json")
                    break
            else:
                k = text.find(pat, k + 1)
                continue
            break
    xm = re.compile(r"<function>\s*<name>" + re.escape(name) + r"</name>")
    m = xm.search(text, c)
    while m:
        # 块的结束是后面紧跟 <function>、</tools> 或文末的 </function>（描述文字里也可能有 </function> 字面量）
        em = re.compile(r"</function>(?=\s*(?:<function>|</tools>|$))").search(text, m.end())
        if not em:
            break
        e = em.start()
        dm = re.compile(r"<description>(.*?)</description>(?=\s*(?:<parameters>|</function>))", re.S).search(text, m.end(), e)
        if desc.strip() and dm and dm.group(1).strip() != desc.strip():  # 同名工具按描述区分
            m = xm.search(text, e)
            continue
        take(m.start(), e + len("</function>"), "xml")
        break
    m = re.compile(r"(?m)^type " + re.escape(name) + r" = \(").search(text, c)
    if m:
        e = text.find("=> any;", m.end())
        if e > 0:
            a = text.find("\n// ", max(c - 1, 0), m.start())  # 前面的 // 描述行：上一个工具之后的第一个 // 行
            take(a + 1 if a >= 0 else m.start(), e + len("=> any;"), "ts")
    if best is not None:
        return best
    d = desc.strip()
    anchors = []
    if len(d) >= 12:
        anchors.append(d)
        esc = json.dumps(d, ensure_ascii=False)[1:-1]
        if esc != d:
            anchors.append(esc)
    j, anchor = -1, None
    for a_ in anchors:
        jj = text.find(a_, c)
        if jj >= 0 and (j < 0 or jj < j):
            j, anchor = jj, a_
    if anchor is None:
        for pat in (f'"name": \'{name}\'', f"'name': '{name}'", json.dumps(name, ensure_ascii=False)):
            k = text.find(pat, c)
            while k >= 0 and text[max(0, k - 9):k].rstrip().endswith('"name":'):
                k = text.find(pat, k + 1)  # "name": "x" 形式的命中是同名、描述不同的另一个工具（JSON 校验已否决），不是这个工具
            if k >= 0:
                nl = text.rfind("\n", c, k)
                return (nl + 1 if nl >= c else k, k + len(pat), "desc")
        return None
    st = j
    for pat in (f'"name": \'{name}\'', f"'name': '{name}'", json.dumps(name, ensure_ascii=False)):
        k = text.find(pat, c, j)
        if k >= 0:
            st = k
            break
    nl = text.rfind("\n", c, st)
    return (nl + 1 if nl >= c else st, j + len(anchor), "desc")


def _locate_tools(text: str, tools: Any, msg_texts: set | None = None) -> tuple[tuple[int, int, int] | None, list[tuple[str, str | None, int, int]]]:
    """定位模板渲染出的工具定义块，返回 ((lo, hi, hi0), [(kind, 工具名, a, b), ...])；找不到返回 (None, [])。
    hi0 是最后一个工具定义本身的结束位置，hi 另含其后的调用格式说明与收尾标记。
    每个工具按 _tool_anchor 依次定位（同名工具按出现顺序一一对应）；块首向前并入紧邻的 <tools> / <tool> 包裹标签，
    块尾向后并入 </tool> / </tools> 及其后到下一个角色标记之前的说明文本。"""
    items = []
    for t in tools or []:
        fn = (t or {}).get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("name"), str) and fn["name"]:
            items.append((fn["name"], fn.get("description") if isinstance(fn.get("description"), str) else ""))
    if not items:
        return None, []
    found: list[tuple[int, int, str, str]] = []  # (起点, 终点, 名字, 定位形式)
    c = 0
    for n_, (name, desc) in enumerate(items):
        r = _tool_anchor(text, name, desc, c)
        if r is None:
            continue
        if r[2] == "desc" and msg_texts and desc.strip() in msg_texts:
            continue  # 按描述定位、而描述又与某条消息正文相同：找到的多半是那条消息（模板根本不渲染 tools 时）
        # 模板可能不渲染某些工具（GLM-5.2 跳过 defer_loading 的工具）：若下一个工具的定义出现在本工具之前，说明本工具没被渲染、
        # 这次找到的是后面某个同名工具，跳过它（否则会把中间的工具全吞掉）
        skip_ = False
        for n2 in range(n_ + 1, len(items)):  # 后面第一个能定位到的工具（中间可能连着几个没被渲染的）
            r2 = _tool_anchor(text, items[n2][0], items[n2][1], c)
            if r2 is not None:
                skip_ = r2[0] < r[0] or (r2[0] == r[0] and r[2] == "desc")
                break
        if skip_:
            continue
        a, b, form_ = r
        if found and a < found[-1][1]:
            a = found[-1][1]
        found.append((a, b, name, form_))
        c = b
    if _DEBUG:
        print(f"[anatomy] tools located: {[(nm, fm, a, b) for (a, b, nm, fm) in found]} of {[n for n, _d in items]}", file=sys.stderr)
    if not found or all(f_[3] == "desc" for f_ in found):
        return None, []  # 没有任何工具是按名字的已知形式（JSON / XML / TypeScript）定位到的：模板不渲染 tools，按描述碰到的多半是消息正文
    lo, hi = found[0][0], found[-1][1]
    for _ in range(2):  # 向前并入 <tool>、<tools>（中间只允许空白）
        k = lo
        while k > 0 and text[k - 1] in " \n\t\r":
            k -= 1
        for tag in ("<tool>", "<tools>"):
            if text[:k].endswith(tag):
                lo = k - len(tag)
                break
        else:
            break
    for _ in range(2):  # 向后并入 </tool>、</tools>（中间没有角色标记）
        k = hi
        while k < len(text) and text[k] in " \n\t\r":
            k += 1
        for tag in ("</tool>", "</tools>"):
            if text.startswith(tag, k):
                hi = k + len(tag)
                break
        else:
            break
    hi0 = hi  # 最后一个工具定义（含 </tools>）的结束位置；之后到下一个角色标记之前的调用格式说明也属于工具定义块
    k = _find_marker(text, hi)
    if k > hi:
        hi = k
    em = END_RE.match(text, hi)  # 紧跟块的收尾标记（Kimi 的 tool_declare 段、gpt-oss 的 developer 段以 <|im_end|> / <|end|> 结束）
    if em:
        hi = em.end()
        xm = re.compile(r"(?:[a-z_]+<\|sep\|>)?(?:<\|end_of_msg\|>)?").match(text, hi)  # Kimi K3：<|close|>message<|sep|><|end_of_msg|> 的其余部分
        if xm and xm.end() > hi:
            hi = xm.end()
    out = []
    for i, (st, _end, name, _form) in enumerate(found):
        a = lo if i == 0 else st
        b = found[i + 1][0] if i + 1 < len(found) else hi
        out.append(("tools_def", name, a, b))
    return (lo, hi, hi0), out


def _split_gap(text: str, lo: int, hi: int) -> int:
    """模板标记归属的分界：cur..a 之间的空隙里，收尾标记（</arg_value>、</tool_call>、</tool_response> 这类以 </ 开头的）
    归上一条消息，从第一个非收尾标记（<|user|>、<|assistant|>、<|observation|>、<tool_response>、<think> 等以 < 开头且
    后面不是 / 的）起归下一条；两者之间的空白随后者。找不到非收尾标记时全部归下一条。"""
    i = lo
    while i < hi:
        j = text.find("<", i, hi)
        j2 = text.find("]~b]", i, hi)  # MiniMax 的角色标记
        if j < 0 or (0 <= j2 < j):
            j = j2
        if j < 0:
            break
        xm = _XTML_CLOSE_RE.match(text, j, hi)
        if xm:
            i = xm.end()  # Kimi K3 的 <|close|>标签<|sep|> 整体是收尾
            continue
        if text[j] == "]" or (j + 1 < hi and text[j + 1] != "/" and not END_RE.match(text, j)):
            k = j
            while k > lo and text[k - 1] in " \n\t\r":
                k -= 1
            return k
        i = j + 1
    # 没有非收尾标记：只有收尾标记（及空白）时全部归上一条（DeepSeek 把工具结果和后续 user 文本放在同一个 User 段里，
    # 中间只有 </tool_result>）；只有空白时归下一条
    head = text[lo:hi].lstrip()
    return hi if head.startswith("</") or END_RE.match(head) else lo


# Kimi K3 的 XTML 标记：<|open|>标签 属性="值"…<|sep|> 正文 <|close|>标签<|sep|>，标签名与属性（role、tool、index、key、type）都是模板文字；
# 编码器按 thinking_effort / tool_choice / response_format 自动插入的 system 段（type="thinking-effort" 等）整段是模板文字（与 GLM 的 <reminder> 同理）
_XTML_SYS = r'(?s:<\|open\|>message role="system" type="(?:thinking-effort|tool-choice|response-format)"<\|sep\|>.*?<\|close\|>message(?:<\|sep\|>)?(?:<\|end_of_msg\|>)?)'
_XTML_OPEN = r'<\|open\|>[a-z_]+(?: [a-z_]+(?:="[^"\n]*"|=")?)*(?:<\|sep\|>)?'  # 末尾允许只写到 属性=" 的半个标签（函数名之前的 call tool="）
_XTML_CLOSE = r'<\|close\|>[a-z_]+(?:<\|sep\|>)?'
_XTML_CLOSE_RE = re.compile(_XTML_CLOSE)
_XTML_BODY_RE = re.compile(r'[a-z_]+(?: [a-z_]+="[^"\n]*")*')  # 两个标记之间只有 XTML 的标签名与属性
_TAGS = re.compile(_XTML_SYS + "|" + _XTML_OPEN + "|" + _XTML_CLOSE + "|" + r"(?s:<reminder>.*?</reminder>)|<arg_key>[^<>]{0,80}</arg_key>|\[gMASK\]|## Return of [^\n]{0,80}|<\|tool_call_begin\|>[^<>]{0,80}<\|tool_call_argument_begin\|>|\{'type': 'text', 'text': '(?:\\[nrt]|\s)*'\}|\{\"type\": \"text\", \"text\": \"(?:\\[nrt]|\s)*\"\}|\{'type': None\}|/nothink|"
                   r"(?:<[^<>]{1,80}>|\]~!?b[\[\]]|\[e~\[)(?:\s*(?:system|user|assistant|tool|developer|ai)\b)?|\s+")


def _residue(t: str) -> str:
    """去掉标记（<…> 形式的控制符与标签及其后的角色名、GLM 的 <arg_key>键名</arg_key>、MiniMax 的标记、GLM 对图片插入的 <reminder> 提示、
    空文本部分渲染出来的壳）和空白后剩下的文本。"""
    return _TAGS.sub("", t)


def _pop_leading_ends(text: str, run: list) -> None:
    """去掉标记串打头的收尾标记；Kimi K3 的 <|close|>标签<|sep|> 两个标记算一个收尾。"""
    while len(run) > 1 and END_RE.match(text, run[0].start()):
        m = run.pop(0)
        if m.group() == "<|close|>" and len(run) > 1 and run[0].group() == "<|sep|>" and re.fullmatch(r"[a-z_]+", text[m.end():run[0].start()]):
            run.pop(0)


def _tag_gap_ok(s: str) -> bool:
    """两个控制标记之间的文字是否只是角色名之类（不超过 12 字），或 Kimi K3 的标签名与属性（message role="assistant"）。"""
    return len(s) <= 12 or bool(_XTML_BODY_RE.fullmatch(s))


def _gen_prompt_start(text: str, cur: int) -> int:
    """生成提示的起点：末尾那一串角色标记（标记之间只有不超过 12 字的角色名），去掉打头的收尾标记
    （Kimi 的 <|im_end|><|im_assistant|>assistant<|im_middle|><think>、Qwen 的 <|im_end|>\n<|im_start|>assistant\n）；没有标记时 -1。"""
    ms = list(MARKER_RE.finditer(text, cur))
    if not ms:
        return -1
    run = [ms[-1]]
    for prev in reversed(ms[:-1]):
        if not _tag_gap_ok(text[prev.end():run[0].start()]):
            break
        run.insert(0, prev)
    _pop_leading_ends(text, run)
    return run[0].start()


_XTML_OPEN_RE = re.compile(_XTML_OPEN)


def _inside_tag(text: str, j: int) -> bool:
    """位置 j 是否在一个 <…> 标记的内部（含 Kimi K3 的 <|open|>标签 属性="值"<|sep|> 整个开标签：type="null" 里的 null 不是参数值）。"""
    p2 = text.rfind("<|open|>", max(0, j - 256), j)
    if p2 >= 0:
        m = _XTML_OPEN_RE.match(text, p2)
        if m and m.end() > j and m.group().endswith("<|sep|>"):
            return True
    p = text.rfind("<", max(0, j - 64), j)
    if p < 0 or text.find(">", p, j) >= 0:
        return False
    q = text.find(">", j, j + 64)
    return q >= 0 and text.find("<", j, q) < 0


def _first_split(text: str, j: int) -> int:
    """没有工具定义块时，模板开头与第一条消息的标记的分界：第一条消息的标记从紧贴正文的那一串角色标记开始
    （Kimi 的 <|im_system|>system<|im_middle|>、gpt-oss 的 <|end|><|start|>developer<|message|> 之类，标记之间只有不超过 12 字的角色名），
    再往前的（GLM 的 [gMASK]<sop><|system|>Reasoning Effort: Max、gpt-oss 模板自带的 system 段）是模板开头。"""
    ms = list(MARKER_RE.finditer(text, 0, j))
    if not ms:
        return _split_gap(text, 0, j)
    k = ms[-1].start()
    run = [ms[-1]]
    for prev in reversed(ms[:-1]):
        if not _tag_gap_ok(text[prev.end():k]):
            break
        k = prev.start()
        run.insert(0, prev)
    _pop_leading_ends(text, run)  # 模板自带段落的收尾标记（Qwen2-VL 默认 system 的 <|im_end|>）归模板开头
    return run[0].start()


def _locate_approx(text: str, piece: str, cur: int) -> tuple[int, int] | None:
    """原文整段找不到时（模板改写了消息内容：转义、换行、截断），按首尾各 48 字定位：首段在游标之后出现，尾段在其后
    合理距离内出现，就把两者之间当作这条消息。只对 48 字以上的片段做，跨度须在原长的 0.5～2 倍之间。"""
    if len(piece) < 48:
        return None
    for n in (24, 12):  # 先用较长的锚点，改写发生在锚点内部时退到较短的
        head, tail = piece[:n], piece[-n:]
        if len(_residue(head)) < 6 or len(_residue(tail)) < 4:
            continue  # 锚点本身几乎全是标记字面量（</minimax:tool_call>、aaaa 之类除外）：到处都能碰到，不可靠
        j = text.find(head, cur)
        if j < 0:
            continue
        # 尾锚点可能在片段内部重复出现（如结尾是重复的字），取使跨度最接近原长的那一次
        best = None
        k = text.find(tail, j + len(head))
        while 0 <= k <= j + 2 * len(piece):
            end = k + len(tail)
            if 0.5 * len(piece) <= end - j <= 2 * len(piece) and (best is None or abs(end - j - len(piece)) < abs(best - j - len(piece))):
                best = end
            k = text.find(tail, k + 1)
        if best is not None:
            return j, best
    return None


def attribute(text: str, msgs: list, tools: Any, starts: list[int] | None) -> tuple[list[Seg], list[dict], list[dict]]:
    """把模板后文本切成片段。starts 是每个 token 的起始字符位置（有分词器时），None 时只有字符区间。
    返回 (片段列表, 未找到的片段列表[{msg, kind, chars}], 按首尾近似定位的片段列表[{msg, kind, chars, rendered_chars}])。
    片段元组的 group：函数名为 ((消息, 调用序号), None)，参数值为 ((消息, 调用序号), 整串形态元组)（只有第一个参数值带整串形态）。"""
    segs: list[Seg] = []
    dropped: list[dict] = []
    approx: list[dict] = []
    cur = 0
    n = len(text)
    last_msg: list = [None, None]  # 上一个已认领片段所属的 (消息下标, 工具名)

    def add(kind: str, msg: int | None, tool: str | None, a: int, b: int) -> None:
        nonlocal cur
        if a > cur:  # 中间没被认领的文本 = 模板标记。收尾部分归上一条消息，自角色标记起归本片段所属的消息
            if last_msg[0] is not None and last_msg[0] != msg:
                k = _split_gap(text, cur, a)
                if k > cur:
                    segs.append(Seg("markup", last_msg[0], last_msg[1], cur, k))
                if a > k:
                    segs.append(Seg("markup", msg, tool, k, a))
            elif last_msg[0] is None and segs and len(_residue(text[cur:a])) > 32:
                # 工具定义块之后、第一条消息之前还有一段模板自带的文字（Kimi 没给 system 时插入的 "You are Kimi…" 段）：算模板开头
                k = max(cur, _first_split(text, a))
                if k > cur:
                    segs.append(Seg("prefix", None, None, cur, k))
                if a > k:
                    segs.append(Seg("markup", msg, tool, k, a))
            else:
                segs.append(Seg("markup", msg, tool, cur, a))
        if b > a:
            segs.append(Seg(kind, msg, tool, a, b))
        cur = max(cur, b)
        last_msg[0], last_msg[1] = msg, tool

    # 工具定义块：模板用自己的格式渲染 tools。按每个工具的 description（长而独特）定位，name 只用来找该工具在块内的起点，
    # 不用裸的工具名在全文里搜索（工具名如 bash、list 可能出现在模板开头或消息正文里）。块可以在任何位置（GLM 在 system 之前，
    # 有的模板在 system 之后），到达其位置时再输出
    pieces_ext = _pieces_ext(msgs)
    pieces = [(i, k, t, tool) for (i, k, t, tool, _a, _g) in pieces_ext]
    skip_groups: set = set()  # 整串 arguments 已匹配的工具调用：其余参数值片段跳过
    name_dropped: set = set()  # 函数名没渲染的工具调用：参数值只认整串形态（Kimi），不再逐个找（模板根本不渲染调用时，短参数值会落到别的消息里）
    tools_span, tools_segs = _locate_tools(text, tools, {p_[2].strip() for p_ in pieces if p_[1] != "assistant.tool_call"})
    tools_done = tools_span is None
    if tools_span is not None:
        lo_, hi_, hi0_ = tools_span
        # 块尾的说明文字只到第一条消息正文为止：Qwen3.5 把 system 正文放在工具块与格式说明之后、同一个 system 段里。
        # 先看块尾（到下一个角色标记之前）是否以第一条消息的正文结尾；否则按第一条消息各段正文依次相接的位置找；
        # 其他消息的长正文（12 字以上）出现在块尾里也算
        first = -1
        first_parts = [p_[2].strip() for p_ in pieces if p_[0] == pieces[0][0] and p_[1] not in ("assistant.tool_call",) and p_[2].strip()] if pieces else []
        if first_parts and text.find(first_parts[0], 0, lo_) >= 0:
            first_parts = []  # 第一条消息渲染在工具块之前（Qwen3-Coder 的 system）：块尾里出现它的字只是巧合
        tail = text[hi0_:hi_]
        for cand in ("\n".join(first_parts), "".join(first_parts)) if first_parts else ():
            k_ = tail.rstrip().rfind(cand)
            if k_ >= 0 and not tail.rstrip()[k_ + len(cand):].strip():
                first = hi0_ + k_
                break
        if first < 0 and first_parts:
            start_ = hi0_
            while True:
                k_ = text.find(first_parts[0], start_)  # 正文本身可能含角色标记字面量而把 hi_ 卡在正文中间：只要求起点在块尾之内
                if k_ < 0 or k_ >= hi_:
                    break
                ok_, q_ = True, k_ + len(first_parts[0])
                for part in first_parts[1:]:
                    while q_ < n and text[q_] in " \n\t\r":
                        q_ += 1
                    if not text.startswith(part, q_):
                        ok_ = False
                        break
                    q_ += len(part)
                # 各段正文之后到下一个角色标记之前只能剩空白：说明块尾确实以这条消息的正文结束（否则找到的是说明文字里的同样的字）
                if ok_ and q_ <= hi_ and not END_RE.sub("", text[q_:hi_]).strip() and (len(first_parts[0]) >= 12 or len(first_parts) > 1 or not (first_parts[0][0].isalnum() and k_ > 0 and text[k_ - 1].isalnum())):
                    first = k_
                    break
                if ok_ and q_ > hi_:
                    first = k_  # 正文含角色标记字面量，hi_ 卡在正文中间
                    break
                start_ = k_ + 1
        others = [text.find(p_[2].strip(), hi0_) for p_ in pieces if (not pieces or p_[0] != pieces[0][0]) and len(p_[2].strip()) >= 12 and len(_residue(p_[2])) >= 8 and p_[1] != "assistant.tool_call"]
        others = [j_ for j_ in others if j_ >= 0]
        if others and (first < 0 or min(others) < first):
            first = min(others)
        if hi0_ <= first < hi_:
            first = max(hi0_, _first_split(text, first)) if MARKER_RE.search(text, hi0_, first) else first  # 正文前的角色标记归消息
            while first > hi0_ and text[first - 1] in " \n\t\r":
                first -= 1
            hi_ = first
            tools_segs = tools_segs[:-1] + [(tools_segs[-1][0], tools_segs[-1][1], tools_segs[-1][2], hi_)]
        tools_span = (lo_, hi_)

    def emit_tools() -> None:
        nonlocal cur, tools_done
        lo, hi = tools_span
        if lo < cur:
            # 定位到的块与已认领的正文重叠（只能按描述定位、而描述又出现在消息里；模板根本不渲染 tools）：当作没有工具定义块
            tools_done = True
            return
        if lo > cur:
            if not segs:
                segs.append(Seg("prefix", None, None, 0, lo))  # 块之前的全部文本 = 模板开头
            elif lo - cur > 64 and _find_marker(text, cur, lo) < 0:
                # 工具块放在 system 之后的模板（DeepSeek）：块前的「## Tools … You have access to …」说明文字属于工具定义块
                segs.append(Seg("tools_def", None, None, cur, lo))
            else:
                segs.append(Seg("markup", last_msg[0], last_msg[1], cur, lo))
        for kind_, name_, a_, b_ in tools_segs:
            segs.append(Seg(kind_, None, name_, a_, b_))
        cur = max(cur, hi)
        tools_done = True

    def in_pending_tools(j_: int) -> bool:
        return not tools_done and tools_span[0] <= j_ < tools_span[1]

    def find(sub: str, start: int) -> int:
        # 尚未输出的工具定义块里的命中不算（工具描述里可能有与消息相同的文本）：跳到块后再找
        j_ = text.find(sub, start)
        if j_ >= 0 and in_pending_tools(j_):
            j_ = text.find(sub, tools_span[1])
        return j_

    def gap_ranges(j_: int) -> list[tuple[int, int]]:
        # 游标到匹配点之间的区间；尚未输出的工具定义块及其前面的说明文字（DeepSeek、Qwen 把块放在 system 正文之后）是模板文字，不算；
        # 还没认领过任何正文时，模板开头（GLM 的 Reasoning Effort: Max、gpt-oss 自带的 system 段）也不算，只从紧贴正文的那串角色标记起
        start = cur if last_msg[0] is not None else max(cur, _first_split(text, j_))  # 还没认领过任何消息正文（工具块可能已输出）
        if not tools_done and start <= tools_span[0] < j_:
            return [(min(max(tools_span[1], start), j_), j_)]
        return [(start, j_)]

    def far_match_is_later_message(j_: int, piece_: str = "") -> bool:
        # 游标到匹配点之间隔了很长一段：可能这个片段其实被模板丢弃了，匹配到的是后面某条消息里相同的文本
        # （例如被删掉的 reasoning 与后面一条的 reasoning 内容相同；只有一个角色标记字面量的 reasoning）。
        # 校验：后面的片段若出现在这段空隙里，本片段视为丢弃；很短的片段（8 字以内）离游标太远也视为丢弃
        ranges = gap_ranges(j_)
        gap_text = "".join(text[a:b] for a, b in ranges)
        if last_msg[0] is None:
            # 还没认领任何正文：模板开头不算距离，但开头（已输出的工具定义块之后）到匹配点之间若出现了后面的消息，仍说明本片段匹配错了位置
            # （第一条消息是模板不渲染的 developer、而它的短正文「parameters」出现在后面某条消息里）
            base = max((s_.c1 for s_ in segs if s_.kind in ("prefix", "tools_def")), default=0)
            full = [(base, j_)] if tools_done or not (tools_span[0] < j_) else [(base, tools_span[0]), (min(max(tools_span[1], base), j_), j_)]
            ranges = [(a, b) for a, b in full if b > a]
        if len(gap_text) <= 8 and ranges == gap_ranges(j_):
            return False
        if len(_residue(gap_text)) < 16 and ranges == gap_ranges(j_):
            return False  # 空隙里只有标记（空的工具结果、调用包裹、角色标记）：中间没有藏着别的消息
        if len(piece_.strip()) < 8 and len(gap_text) > 400:
            return True
        for _i2, k2, p2, t2 in pieces[idx + 1:idx + 13]:
            p2 = p2.strip()
            if k2 == "assistant.tool_call" and t2 == p2:
                continue  # 函数名是普通单词（question、read），模板说明文字里也有
            # 只用有实际文字的片段校验：本身就是标记字面量的片段（</think>、<|user|>）到处都能找到
            if len(p2) >= 6 and len(_residue(p2)) >= 4 and any(0 <= text.find(p2, a, b + len(p2) - 1) < b for a, b in ranges if b > a):
                return True  # 后面的片段从空隙里开始（哪怕延伸到匹配点之后）：本片段匹配到的是那条消息里的文字
        return False

    if not tools_done and (not pieces or tools_span[0] < text.find(pieces[0][2]) or text.find(pieces[0][2]) < 0):
        emit_tools()
    run_seen: set = set()  # 已检查过渲染顺序的 tool 消息
    for idx, (i, kind, piece, tool) in enumerate(pieces):
        if kind == "tool.result" and i not in run_seen:
            # 连续的 tool 消息：有的模板按 assistant tool_calls 的顺序重排工具结果（GLM-5.3、Kimi K3 在 id 全部对得上时；DeepSeek-V4 把
            # id 对不上的挪到前面）。取每条结果最长、有实际文字的片段在游标之后首次出现的位置，顺序与消息顺序不同就按文本顺序处理这一串
            run = [i]
            while run[-1] + 1 < len(msgs) and isinstance(msgs[run[-1] + 1], dict) and msgs[run[-1] + 1].get("role") == "tool":
                run.append(run[-1] + 1)
            run_seen.update(run)
            run_set = set(run)
            L = 0
            while idx + L < len(pieces) and pieces[idx + L][0] in run_set:
                L += 1
            if len(run) >= 2:
                anchors: dict[int, str] = {}
                for (i2, k2, p2, _t2) in pieces[idx:idx + L]:
                    p2s = p2.strip()
                    if k2 == "tool.result" and len(_residue(p2s)) >= 4 and len(p2s) > len(anchors.get(i2, "")):
                        anchors[i2] = p2s
                pos: dict[int, int] = {}
                for j_, a_ in anchors.items():  # 内容相同的几条结果（同样的报错）按消息顺序各取下一次出现的位置
                    p_ = find(a_, cur)
                    while p_ >= 0 and p_ in pos.values():
                        p_ = find(a_, p_ + 1)
                    pos[j_] = p_
                if anchors and all(p_ >= 0 for p_ in pos.values()):
                    last_pos = -1
                    key: dict[int, int] = {}
                    for j_ in run:  # 没有可定位片段的结果（空、只有空白或标记）跟在前一条有片段的结果后面
                        last_pos = pos.get(j_, last_pos)
                        key[j_] = last_pos
                    order = sorted(run, key=lambda j_: (key[j_], j_))
                    if order != run:
                        if _DEBUG:
                            print(f"[anatomy] tool results rendered out of order: msgs {run} -> {order} (pos {pos})", file=sys.stderr)
                        by_msg: dict[int, list] = {j_: [] for j_ in run}
                        by_ext: dict[int, list] = {j_: [] for j_ in run}
                        for q_ in range(idx, idx + L):
                            by_msg[pieces[q_][0]].append(pieces[q_])
                            by_ext[pieces[q_][0]].append(pieces_ext[q_])
                        pieces[idx:idx + L] = [x for j_ in order for x in by_msg[j_]]
                        pieces_ext[idx:idx + L] = [x for j_ in order for x in by_ext[j_]]
                        i, kind, piece, tool = pieces[idx]
        alts, grp = pieces_ext[idx][4], pieces_ext[idx][5]
        if grp is not None and grp[0] in skip_groups:
            continue
        if not piece.strip():
            continue  # 只有空白的正文：在模板后文本里无法定位，随模板标记
        if kind == "assistant.tool_call" and grp is not None and grp[1] is None:
            # 工具调用的函数名：只认整词（前后不是字母数字、下划线、连字符），否则会落在别的词里（nonexistent-skill 里的 skill）；
            # 函数名紧跟在调用标记之后（GLM <tool_call>、Qwen {"name": 、DeepSeek invoke name=、MiniMax <invoke name=、gpt-oss to=functions.），
            # 游标到函数名之间除了标记只允许调用头的固定文字（Qwen 的 {"name": "、前一个空参数调用的 "arguments": {}}、gpt-oss 的 to=functions.）；
            # 模板不渲染函数名时（Kimi 只输出调用 id）记为丢弃
            name_re = re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(piece) + r"(?![A-Za-z0-9_\-])")
            j, mm = -1, None
            for mm in name_re.finditer(text, cur):
                # 同名的词可能先出现在标记里（函数名 open / user / message 与 Kimi K3 的 <|open|>、GLM 的 <|user|> 同形）：逐个往后试，
                # 直到名字之前（去掉标记后）出现了调用头之外的文字为止——再往后只会更远
                if in_pending_tools(mm.start()):
                    continue
                if last_msg[0] is None:
                    # 前面还没认领过任何正文（开头的消息只有空白）：模板开头不算距离，只看紧贴名字的那串角色标记
                    gap_t = re.sub(r"<[^<>]*$", "", text[max(cur, _first_split(text, mm.start())):mm.start()])
                else:
                    gap_t = re.sub(r"<[^<>]*$", "", "".join(text[a:b] for a, b in gap_ranges(mm.start())))  # 名字前未闭合的标签（<invoke name="）不算
                if alts and any(w_ in gap_t for a_ in alts for w_ in (a_, _esc(a_))):
                    break  # 名字之前已经出现了本次调用的参数：所有模板都先输出名字再输出参数，这个候选不是调用头（Kimi 把工具名当作 tool 消息的角色标签）
                if len(gap_t) < 400:
                    gap_t = re.sub(r"\{[^{}]{0,120}\}", "", gap_t)  # 前一个调用整串输出的小 JSON（DeepSeek 对只有空值的参数输出 {"n": ""}）不算
                if _CALL_HEAD.sub("", _residue(gap_t)):
                    break
                # 名字前一个字符必须是调用头的分隔符：> （GLM <tool_call>read）、" （Qwen "name": "read"、invoke name="read"）、= （<function=read>）、
                # . （gpt-oss functions.read）；模板不渲染调用时（Qwen2-VL、TinyLlama）同名词前面是换行或空格，不认
                if text[mm.start() - 1:mm.start()] in ('>', '"', '=', '.', "'"):
                    j = mm.start()
                    break
            if j >= 0 and far_match_is_later_message(j, piece):
                j = -1
            span, cands = None, []
            if j < 0:
                if _DEBUG:
                    print(f"[anatomy] drop name msg={i} cur={cur} name={piece!r} first_at={mm.start() if mm else -1} gap={text[cur:mm.start()][-160:]!r}" if mm else f"[anatomy] drop name msg={i} cur={cur} name={piece!r} first_at=-1", file=sys.stderr)
                name_dropped.add(grp[0])
                dropped.append({"msg": i, "kind": kind, "chars": len(piece), "head": piece[:48]})
                continue
        else:
            forms = [(piece, False), (piece.strip(), False)] + [(a_, False) for a_ in alts] + ([(w_, True) for w_ in grp[1]] if grp is not None and grp[1] else [])
            if grp is not None and grp[1] is not None and grp[0] in name_dropped:
                forms = [(w_, True) for w_ in grp[1]]  # 函数名没渲染：只认整串 arguments
            # 候选形态：原文、去掉首尾空白的原文、其他形态（JSON 转义、列表 content 的 JSON / repr、整串 arguments）。
            # 按出现位置从前往后取第一个不是「落在后面消息里」的；整串 arguments 出现得不比参数值晚就整串认领
            cands: list[tuple[int, str, bool]] = []
            for form, whole in forms:
                if not form or len(form) < 8 and _residue(form) == "" and form != piece:
                    continue
                if last_msg[0] == i and kind in ("user", "system", "tool.result"):
                    # 同一条消息的下一个 content 部分紧接着上一个部分（模板直接拼接或用换行连接）：紧贴游标的命中直接认，
                    # 不受短片段规则限制（GLM 把 "…ok a" + "result" + "type…" 拼成一串）
                    k_ = cur
                    while k_ < n and k_ - cur <= 4 and text[k_] in " \n\t\r":
                        k_ += 1
                    if text.startswith(form, k_):
                        cands.append((k_, form, False))
                        continue
                jj = find(form, cur)
                def next_part_follows(jj_: int) -> bool:
                    # 同一条消息的下一段正文紧跟其后：模板把多段 content 直接拼接（GLM 的 "to" + "parameters…"），右边紧挨着字也是它
                    if idx + 1 >= len(pieces) or pieces[idx + 1][0] != i or pieces[idx + 1][1] != kind:
                        return False
                    nxt = pieces[idx + 1][2].strip()
                    q_ = jj_ + len(form)
                    while q_ < n and q_ - jj_ - len(form) <= 4 and text[q_] in " \n\t\r":
                        q_ += 1
                    return bool(nxt) and text.startswith(nxt[:24], q_)

                while 0 <= jj and len(form) < 8 and not next_part_follows(jj) and (_inside_tag(text, jj) or (form.isalnum() and (
                        (jj > 0 and (text[jj - 1].isalnum() or text[jj - 1] == "_")) or (jj + len(form) < n and (text[jj + len(form)].isalnum() or text[jj + len(form)] == "_"))))):
                    jj = find(form, jj + 1)  # 很短的正文（is、ok、a）落在某个标记里面（<|assistant|> 里的 is）或别的词里面（list 里的 is、assistant 里的 a）：不是它。
                    # 模板把多段 content 直接拼接的情况（GLM 的 "…ok a" + "result" + "type…"）由上面「紧贴游标」的候选和 next_part_follows 覆盖
                raw_args_piece = kind == "assistant.tool_call" and grp is not None and bool(grp[1]) and piece in grp[1]  # 整串输出的非对象 arguments（null、"x"、[1]）
                if 0 <= jj and ((len(form) < 4 and not any(ch.isalnum() for ch in form)) or not _residue(form) or raw_args_piece):
                    # 一两个标点的正文（"、}}）或本身就是标记字面量的正文（<tools>、<|user|>）、非对象的整串 arguments（null 这类到处都有，而且有的模板
                    # 根本不渲染它）：只认游标到它之间没有别的文字的那个（函数名之后收尾的 > 不算；尚未输出的工具块不算）。标记字面量最多往后找 8 次
                    tries_ = 0
                    while 0 <= jj and _CALL_HEAD.sub("", _residue(re.sub(r"\{[^{}]{0,120}\}", "", re.sub(r'^["\']?>', "", "".join(text[a_:b_] for a_, b_ in gap_ranges(jj)))))):  # 剩下的 JSON 标点（整串参数之后的 }）、只有空值的参数 JSON（{"query": ""}）不算
                        tries_ += 1
                        jj = find(form, jj + 1) if (not _residue(form) and tries_ < 8) else -1
                if 0 <= jj and kind == "assistant.reasoning" and not _THINK_LEAD.search(text, max(cur, jj - 64), jj):
                    jj = -1  # 思考只会出现在 <think> 之后（gpt-oss 为 <|message|> 之后）：别处的同样文字是正文里的（模板已经把这段思考删掉了）
                if jj >= 0:
                    cands.append((jj, form, whole))
            cands.sort(key=lambda c_: (c_[0], not c_[2]))
            j, span = -1, None
            far_seen = False
            for jj, form, whole in cands:
                if far_match_is_later_message(jj, form):
                    far_seen = True
                    continue
                j, piece = jj, form
                if whole or (grp is not None and grp[1] and form in grp[1]):
                    skip_groups.add(grp[0])  # 匹配到的是整串 arguments：本次调用的其余参数值不再单独找
                break
            if j < 0 and not far_seen and not (grp is not None and grp[1] is not None and grp[0] in name_dropped):
                # 整段找不到：按首尾定位（模板改写了消息内容：转义、换行、截断）
                span = _locate_approx(text, piece, cur)
                if span is not None and in_pending_tools(span[0]):
                    span = _locate_approx(text, piece, tools_span[1])
                if span is not None and far_match_is_later_message(span[0], piece):
                    span = None
                if span is not None and kind == "assistant.reasoning" and not _THINK_LEAD.search(text, max(cur, span[0] - 64), span[0]):
                    span = None  # 思考只会出现在 <think> 之后
                if span is not None:
                    j = span[0]
                    approx.append({"msg": i, "kind": kind, "chars": len(piece), "rendered_chars": span[1] - span[0]})
        if j < 0:
            if _DEBUG:
                print(f"[anatomy] drop msg={i} kind={kind} cur={cur} piece={piece[:60]!r} cands={[(c_[0], c_[1][:30]) for c_ in cands][:6] if kind != 'assistant.tool_call' or tool != piece else '(name)'}", file=sys.stderr)
            dropped.append({"msg": i, "kind": kind, "chars": len(piece), "head": piece.strip()[:48]})
            continue
        if not tools_done and tools_span[0] < j:
            emit_tools()
        if not segs and j > 0:
            # 没有工具定义块：开头到第一条消息之间，紧贴正文的那串角色标记之前是模板开头，自角色标记起是第一条消息的标记
            k = _first_split(text, j)
            if k > 0:
                segs.append(Seg("prefix", None, None, 0, k))
            cur = k
        add(kind, i, tool, j, span[1] if span else j + len(piece))
    if not tools_done:
        emit_tools()
    if cur < n:
        last = pieces[-1][0] if pieces else None
        # 末尾：最后一条消息的收尾标记 + 生成提示。生成提示从最后一个 <| 开始（多数模板的 <|assistant|>）
        k = _gen_prompt_start(text, cur)
        end = k if k >= cur else n
        if end > cur:
            if last_msg[0] is not None and last != last_msg[0]:
                # 最后一条消息没有认领任何片段（只有空白的 user）：上一条消息的收尾标记归上一条
                k2 = _split_gap(text, cur, end)
                if k2 > cur:
                    segs.append(Seg("markup", last_msg[0], last_msg[1], cur, k2))
                if end > k2:
                    segs.append(Seg("markup", last, None, k2, end))
            else:
                segs.append(Seg("markup", last, last_msg[1], cur, end))
        if k >= cur:
            segs.append(Seg("gen_prompt", None, None, k, n))
    if starts is not None:
        for s in segs:
            s.t0 = bisect.bisect_left(starts, s.c0)
            s.t1 = bisect.bisect_left(starts, s.c1)
    for s in segs:
        s.blob = h8(text[s.c0:s.c1])
    return segs, dropped, approx


def _common_prefix_chars(a: str, b: str) -> int:
    n = min(len(a), len(b))
    lo, hi = 0, n
    if a[:n] == b[:n]:
        return n
    while lo < hi:  # 二分：a[:m] == b[:m] 单调
        m = (lo + hi + 1) // 2
        if a[:m] == b[:m]:
            lo = m
        else:
            hi = m - 1
    return lo


def common_prefix_len(a: list[int], b: list[int]) -> int:
    n = min(len(a), len(b))
    step = 256
    i = 0
    while i + step <= n and a[i:i + step] == b[i:i + step]:
        i += step
    while i < n and a[i] == b[i]:
        i += 1
    return i


class ReqAnatomy:
    """一次请求的解剖结果（供 session_view 输出 md/json/html）。"""

    def __init__(self, req: pcap.Req):
        rec, e = req.rec, req.end or {}
        self.id = rec["id"]
        self.ts = rec["ts"]
        self.ep = rec.get("ep")
        self.level = rec.get("lvl")
        self.followed = bool(rec.get("fol"))
        self.model = rec.get("model")
        self.stream = bool(rec.get("stream"))
        self.session = rec.get("sess")
        self.headers = req.headers()
        self.params = {k: v for k, v in (rec.get("params") or {}).items()}
        self.body_bytes = rec.get("bb")
        self.pt, self.ct = e.get("pt"), e.get("ct")
        self.prefill_ms, self.ttft_ms, self.e2e_ms = e.get("p_ms"), e.get("ttft_ms"), e.get("e2e_ms")
        self.status = e.get("st", "no_end_record")
        self.finish = e.get("fr")
        self.out_tokens = e.get("ot")
        self.out_chars, self.out_reasoning, self.out_tool_calls = e.get("oc"), e.get("orc"), e.get("otc")
        self.gold_n = e.get("ids_n")
        self.n_msgs = rec.get("nm")
        self.n_tools, self.tools_bytes, self.tools_hash = rec.get("nt"), rec.get("tb"), rec.get("tools")
        self.chain: list[dict] = []
        try:
            self.chain = req.chain() if rec.get("ml") else []
        except pcap.MissingBlob:
            pass
        self.system_hash = self.chain[0]["m"] if self.chain and self.chain[0]["r"] in ("system", "developer") else None
        self.thread = "main"
        self.turn = 0
        self.turn_in_thread = 0
        self.gap_s: float | None = None
        # token 级
        self.text: str | None = None
        self.ids: list[int] | None = None
        self.tok_src: str | None = None
        self.tok_match_vllm: bool | None = None
        self.tok_match_gold: bool | None = None
        self.render_ms: float | None = None
        self.encode_ms: float | None = None
        self.segs: list[Seg] = []
        self.dropped: list[dict] = []
        self.approx: list[dict] = []  # 原文整段未找到、按首尾定位的片段（模板改写了消息内容）
        self.msg_rows: list[dict] = []
        self.by_kind_tok: dict[str, int] = {}
        self.by_kind_chars: dict[str, int] = {}
        self.diff: dict[str, Any] = {}
        self.error: str | None = None
        self.messages: list | None = None
        self.tools: Any = None
        self.tok_attr = False  # 是否有 token 级归属（需要分词器给出偏移）
        self.out: dict | None = None  # 模型输出全文（output=text 时）：{content, reasoning, tool_calls}
        self.out_echo: dict | None = None  # 本请求的输出在同线程下一请求中是否被客户端回传
        self.compact_from: list[dict] = []  # 历史压缩请求：压缩前后之间的旁路请求及其输出是否出现在本请求的消息中
        self.compact_for: int | None = None  # 旁路请求：其输出出现在哪个历史压缩请求的消息中

    @property
    def n_tokens(self) -> int | None:
        if self.ids is not None:
            return len(self.ids)
        return self.pt if isinstance(self.pt, int) else None


_EMPTY_ROW = {"tok": 0, "chars": 0, "content": 0, "reasoning": 0, "calls": 0, "markup": 0,
              "content_c": 0, "reasoning_c": 0, "calls_c": 0, "markup_c": 0, "tool": None}


def _norm_args(a: Any) -> str:
    """工具调用参数的规范形式：能解析为 JSON 的按 JSON 规范化后比较，否则按原字符串去首尾空白比较。"""
    if not isinstance(a, str):
        a = json.dumps(a, ensure_ascii=False, sort_keys=True)
    try:
        return json.dumps(json.loads(a), ensure_ascii=False, sort_keys=True)
    except ValueError:
        return a.strip()


def echo_check(out: dict, msgs: list, k: int) -> dict:
    """上一请求的输出 out 与本请求新增消息 msgs[k:] 里第一条 assistant 消息比较：客户端是否把正文、思考、工具调用原样写回历史。
    每项取值 True（相同）/ False（不同或未回传）/ None（输出中没有这一项，无从比较）。"""
    asst = [(i, m) for i, m in enumerate(msgs) if i >= k and isinstance(m, dict) and m.get("role") == "assistant"]
    oc, orc, otc = (out.get("content") or "").strip(), (out.get("reasoning") or "").strip(), out.get("tool_calls") or []
    res = {"msg": None, "content": None if not oc else False, "reasoning": None if not orc else False,
           "tool_calls": None if not otc else False, "reasoning_sent": None}
    if not asst:
        return res
    i, m = asst[0]
    res["msg"] = i
    c = _text_of_content(m.get("content")).strip()
    r = m.get("reasoning") if isinstance(m.get("reasoning"), str) else m.get("reasoning_content")
    r = (r or "").strip() if isinstance(r, str) else ""
    if oc:
        res["content"] = c == oc
    if orc:
        res["reasoning"] = r == orc
        res["reasoning_sent"] = bool(r)
    if otc:
        got = [((call.get("function") or {}).get("name") or "", _norm_args((call.get("function") or {}).get("arguments") or ""))
               for call in (m.get("tool_calls") or []) if isinstance(call, dict)]
        want = [(x.get("name") or "", _norm_args(x.get("arguments") or "")) for x in otc]
        res["tool_calls"] = got == want
    return res


class Anatomist:
    def __init__(self, renderer: "pcap.Renderer | None"):
        self.r = renderer
        self.tok = renderer.tok if renderer else None

    def encode_offsets(self, text: str, add_special: bool) -> tuple[list[int], list[int]]:
        """分词并给出每个 token 的起始字符位置。快分词器直接用 offsets；慢分词器（Kimi K2.x / K3 的 TikTokenTokenizer）没有 offsets，
        用 tiktoken 还原每个 token 的字节、按字节累加换算成字符位置（token 从某个字符中间开始时算作该字符）。两者都不行时抛 NotImplementedError。"""
        try:
            enc = self.tok(text, add_special_tokens=add_special, return_offsets_mapping=True)
            if "offset_mapping" in enc:
                return enc["input_ids"], [o[0] for o in enc["offset_mapping"]]
        except (NotImplementedError, ValueError, TypeError):
            pass
        ids = self.tok(text, add_special_tokens=add_special)["input_ids"]
        model = getattr(self.tok, "model", None)
        if model is None or not hasattr(model, "decode_single_token_bytes"):
            raise NotImplementedError("tokenizer gives no offsets")
        bts = [model.decode_single_token_bytes(t) for t in ids]
        raw = text.encode("utf-8", "surrogatepass")
        joined = b"".join(bts)
        if joined != raw:
            if add_special and joined.endswith(raw):  # 开头的特殊 token（BOS）不占字符
                pass
            else:
                raise NotImplementedError("token bytes do not reproduce the text")
        char_starts = [0]
        for ch in text:
            char_starts.append(char_starts[-1] + len(ch.encode("utf-8", "surrogatepass")))
        char_starts.pop()
        starts, b = [], len(joined) - len(raw)
        b = -b
        for bt in bts:
            starts.append(max(0, bisect.bisect_right(char_starts, max(b, 0)) - 1) if b >= 0 else 0)
            b += len(bt)
        return ids, starts

    def analyze(self, req: pcap.Req, prev: ReqAnatomy | None) -> ReqAnatomy:
        a = self.compute(req)
        self._diff(a, prev)
        return a

    def compute(self, req: pcap.Req) -> ReqAnatomy:
        """一条请求自身的解剖：渲染、分词、片段归属。与上一请求无关，session_view --jobs 在子进程里算这一步；
        与上一请求的比较（_diff）留在主进程按到达顺序做。"""
        import time  # noqa: PLC0415

        a = ReqAnatomy(req)
        if not req.text_level or req.rec.get("err"):
            a.error = "meta_only" if not req.text_level else req.rec.get("err")
            return a
        try:
            body = req.body()
        except pcap.MissingBlob as exc:
            a.error = f"missing_blob:{exc}"
            return a
        a.messages = body.get("messages") if a.ep == "chat" else None
        a.tools = body.get("tools") if a.ep == "chat" else None
        gold_ids, gold_text = req.gold_ids(), req.gold_text()
        try:
            o = req.output()
            if isinstance(o, dict):
                a.out = {"content": o.get("content") or "", "reasoning": o.get("reasoning") or "",
                         "tool_calls": [c for c in (o.get("tool_calls") or []) if isinstance(c, dict)]}
        except pcap.MissingBlob:
            pass
        text, ids, starts = None, None, None
        prompt = body.get("prompt") if a.ep == "completions" else None
        if isinstance(prompt, list) and prompt and all(isinstance(x, int) for x in prompt):
            # completions 接口、prompt 直接是 token id 列表：没有文本可归属，token 数就是列表长度
            a.ids, a.tok_src = list(prompt), "prompt_ids"
            if isinstance(a.pt, int):
                a.tok_match_vllm = len(a.ids) == a.pt
            if gold_ids is not None:
                a.tok_match_gold = a.ids == gold_ids
            return a
        if self.r is not None:
            try:
                t0 = time.perf_counter()
                text = self.r.render(body, a.ep)
                t1 = time.perf_counter()
                add = bool(body.get("add_special_tokens", a.ep != "chat"))
                try:
                    ids, starts = self.encode_offsets(text, add)
                except NotImplementedError:
                    ids, starts = self.r.encode(text, a.ep, body), None  # 没有 offsets：token 数仍准确，归属按字符
                t2 = time.perf_counter()
                a.render_ms, a.encode_ms = round((t1 - t0) * 1000, 3), round((t2 - t1) * 1000, 3)
                a.tok_src = "offline"
                if isinstance(a.pt, int):
                    a.tok_match_vllm = len(ids) == a.pt
                if gold_ids is not None:
                    a.tok_match_gold = ids == gold_ids
                    if a.tok_match_gold:
                        a.tok_src = "gold"  # 离线与线上完全一致：偏移可直接用于 golden ids
                    elif gold_text is not None and gold_text == text:
                        a.tok_src = "offline!=gold"
            except Exception as exc:
                a.error = f"render/encode: {type(exc).__name__}: {exc}"[:300]
                text = ids = starts = None
        if text is None and gold_text is not None:
            text, ids, a.tok_src = gold_text, gold_ids, "gold_text_only"  # 没有分词器：有文本与 ids，但没有 token 偏移
        if text is None:
            if a.ep == "chat" and a.messages is not None:
                self._rows(a)  # 没有模板后文本（无分词器且无 golden 文本）：消息表只有各消息原文的字符数
            return a
        a.text, a.ids = text, ids
        a.tok_attr = starts is not None
        if a.ep == "chat":
            a.segs, a.dropped, a.approx = attribute(text, a.messages or [], a.tools, starts)
        else:
            s = Seg("user", None, None, 0, len(text))  # completions：整段 prompt 是一个片段，没有消息下标
            if starts is not None:
                s.t1 = len(ids)
            s.blob = h8(text)
            a.segs = [s]
        self._rows(a)
        return a

    def _rows(self, a: ReqAnatomy) -> None:
        by_t: dict[str, int] = {k: 0 for k in KINDS}
        by_c: dict[str, int] = {k: 0 for k in KINDS}
        per_msg: dict[int, dict] = {}
        for s in a.segs:
            by_t[s.kind] += s.t1 - s.t0
            by_c[s.kind] += s.c1 - s.c0
            if s.msg is None:
                continue
            row = per_msg.setdefault(s.msg, dict(_EMPTY_ROW))
            row["tok"] += s.t1 - s.t0
            row["chars"] += s.c1 - s.c0
            f = {"markup": "markup", "assistant.reasoning": "reasoning", "assistant.tool_call": "calls"}.get(s.kind, "content")
            row[f] += s.t1 - s.t0
            row[f + "_c"] += s.c1 - s.c0
            if s.tool and not row["tool"]:
                row["tool"] = s.tool
        if a.segs:
            a.by_kind_tok, a.by_kind_chars = by_t, by_c
        dropped_msgs = {d["msg"] for d in a.dropped if d["kind"] == "assistant.reasoning"}
        rows = []
        msgs = a.messages or []
        call_tool: dict[str, str] = {}  # 没有模板后文本时，工具结果的工具名由 tool_call_id 反查
        for m in msgs:
            for call in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
                if isinstance(call, dict) and call.get("id") and isinstance(call.get("function"), dict):
                    call_tool[str(call["id"])] = str(call["function"].get("name") or "")
        for i, m in enumerate(msgs):
            node = a.chain[i] if i < len(a.chain) else {}
            r = per_msg.get(i, _EMPTY_ROW)
            role = m.get("role") if isinstance(m, dict) else "?"
            if not a.segs:  # 没有模板后文本：字符数取消息原文（content + reasoning + 工具调用）的长度
                r = dict(_EMPTY_ROW, chars=len(pcap.message_text(m)),
                         tool=call_tool.get(str(m.get("tool_call_id"))) if role == "tool" and isinstance(m, dict) else None)
            tool = r["tool"]
            if role == "assistant" and isinstance(m, dict) and m.get("tool_calls"):
                names = [((c.get("function") or {}).get("name") or "?") for c in m["tool_calls"] if isinstance(c, dict)]
                tool = ",".join(names)
            preview = pcap.message_text(m)
            rows.append({"i": i, "role": role, "tool": tool, "h": node.get("m"), "chars": r["chars"], "tok": r["tok"],
                         "content": r["content"], "reasoning": r["reasoning"], "calls": r["calls"],
                         "markup": r["markup"], "content_c": r["content_c"], "reasoning_c": r["reasoning_c"],
                         "calls_c": r["calls_c"], "markup_c": r["markup_c"], "reasoning_dropped": i in dropped_msgs,
                         "src_chars": node.get("c", len(preview)),
                         "preview": preview[:160].replace("\n", "⏎")})
        a.msg_rows = rows

    def _diff(self, a: ReqAnatomy, prev: ReqAnatomy | None) -> None:
        d: dict[str, Any] = {"prev": prev.id if prev else None}
        if prev is None:
            d["kind"] = "first"
            d["prefix_msgs"] = 0
            d["new_msgs"] = len(a.chain)
            a.diff = d
            return
        if a.ep != "chat":
            # completions 接口没有 messages：没有消息层可比，只做 token 层的公共前缀
            d.update({"kind": "no_messages", "prefix_msgs": 0, "new_msgs": 0, "tools_changed": False, "prefix_chars": 0, "new_chars": 0})
            if a.ids is not None and prev.ids is not None:
                cp = common_prefix_len(prev.ids, a.ids)
                d.update({"tok_prefix": cp, "tok_new": len(a.ids) - cp, "prev_tokens": len(prev.ids), "cause": "append" if cp == len(prev.ids) else "message_changed"})
            a.diff = d
            return
        # 消息层
        pc, cc = [n["h"] for n in prev.chain], [n["h"] for n in a.chain]
        same_tools = prev.tools_hash == a.tools_hash
        k = 0
        while k < min(len(pc), len(cc)) and pc[k] == cc[k]:
            k += 1
        if not same_tools:
            k = 0
        if cc == pc and same_tools:
            kind = "repeat"
        elif k == len(pc) and k < len(cc):
            kind = "append_" + (a.chain[-1]["r"] if a.chain else "?")
        elif len(cc) < 0.5 * len(pc) and k <= 2:
            kind = "compact_like"
        else:
            kind = "rewrite"
        d.update({"kind": kind, "prefix_msgs": k, "new_msgs": len(cc) - k, "tools_changed": not same_tools,
                  "prefix_chars": sum(n["c"] for n in a.chain[:k]),
                  "new_chars": sum(n["c"] for n in a.chain[k:])})
        for row in a.msg_rows:
            row["status"] = "reused" if row["i"] < k else "new"
        if prev.out is not None and a.messages is not None and kind.startswith("append"):
            prev.out_echo = {"next": a.turn, **echo_check(prev.out, a.messages, k)}
        # token 层
        if a.ids is not None and prev.ids is not None:
            cp = common_prefix_len(prev.ids, a.ids)
            tail = prev.by_kind_tok.get("gen_prompt", 0)
            prev_body = len(prev.ids) - tail
            d["tok_prefix"] = cp
            d["tok_new"] = len(a.ids) - cp
            d["prev_tokens"] = len(prev.ids)
            d["prev_body_tokens"] = prev_body
            if a.tok_attr and prev.tok_attr:
                seg = next((s for s in a.segs if s.t0 <= cp < s.t1), None)
                pseg = next((s for s in prev.segs if s.t0 <= cp < s.t1), None)
            elif a.text is not None and prev.text is not None:
                # 没有分词器偏移（token 序列取自 golden）：分歧点所在的片段按两段模板后文本的公共字符前缀定位，
                # 分歧原因与消息状态的判定与有分词器时相同
                cc = _common_prefix_chars(prev.text, a.text)
                seg = next((s for s in a.segs if s.c0 <= cc < s.c1), None)
                pseg = next((s for s in prev.segs if s.c0 <= cc < s.c1), None)
            else:
                seg = pseg = None
            if cp >= prev_body - 2 and kind.startswith("append"):
                cause = "append"
            elif not same_tools:
                cause = "tools_changed"
            elif kind in ("compact_like", "rewrite", "repeat"):
                cause = kind
            elif pseg is not None and pseg.kind == "assistant.reasoning":
                cause = "template_stripped_reasoning"
            elif pseg is not None and seg is not None and pseg.msg == seg.msg and pseg.kind == seg.kind:
                cause = "template_rewrite"  # 消息相同但渲染不同（模板依赖位置的输出）
            else:
                cause = "message_changed"
            d["cause"] = cause
            d["diverge_at"] = cp
            d["diverge_msg"] = seg.msg if seg else None
            d["diverge_kind"] = seg.kind if seg else None
            d["diverge_prev_kind"] = pseg.kind if pseg else None
            if cause.startswith("template"):
                d["template_lost_tokens"] = max(0, prev_body - cp)  # 消息没变、却要重新分词/重新 prefill 的 token
            # 消息级状态修正：消息层说 reused、token 层却在它内部断开 → 被模板改写
            if seg is not None and seg.msg is not None and cause.startswith("template"):
                for row in a.msg_rows:
                    if row["i"] >= seg.msg and row["i"] < k:
                        # stripped：这条 assistant 消息的 reasoning 被模板删了；shifted：消息本身没变，但它前面有片段
                        # 被改写，所以它的 token 不再是上一请求的前缀（前缀缓存与增量分词都得重做）
                        row["status"] = "stripped" if row.get("reasoning_dropped") else "shifted"
        a.diff = d


def in_order(reqs: list[pcap.Req]) -> list[pcap.Req]:
    return sorted(reqs, key=lambda r: r.rec["ts"])


def assemble_session(reqs: list[pcap.Req], anat: Anatomist, computed: Any = None) -> list[ReqAnatomy]:
    """按到达顺序解剖一个会话的全部请求；每个请求与同一「线程」（相同 system 提示 + 接口）的上一个请求比较。
    computed：已经算好的 Anatomist.compute 结果，与 in_order(reqs) 一一对应（可以是迭代器）；不给就在这里逐条算。"""
    out: list[ReqAnatomy] = []
    prev_by_thread: dict[tuple, ReqAnatomy] = {}
    thread_names: dict[tuple, str] = {}
    last_ts = None
    computed = iter(computed) if computed is not None else None
    for i, req in enumerate(in_order(reqs)):
        chain = []
        try:
            chain = req.chain() if req.rec.get("ml") else []
        except pcap.MissingBlob:
            pass
        sh = chain[0]["m"] if chain and chain[0]["r"] in ("system", "developer") else None
        key = (sh, req.rec.get("ep"))
        if key not in thread_names:
            thread_names[key] = "main" if not thread_names else f"side{len(thread_names)}"
        prev = prev_by_thread.get(key)
        a = next(computed) if computed is not None else anat.compute(req)
        a.turn = i
        anat._diff(a, prev)
        a.thread = thread_names[key]
        a.turn_in_thread = (prev.turn_in_thread + 1) if prev else 0
        a.gap_s = round(a.ts - last_ts, 3) if last_ts is not None else None
        last_ts = a.ts
        prev_by_thread[key] = a
        out.append(a)
    # 线程命名：最长的线程叫 main（会话的第一个请求常是起标题的旁路请求）
    counts: dict[str, int] = {}
    for a in out:
        counts[a.thread] = counts.get(a.thread, 0) + 1
    if counts:
        main = max(counts, key=counts.get)
        if main != "main":
            for a in out:
                if a.thread == main:
                    a.thread = "main"
                elif a.thread == "main":
                    a.thread = main
    # 历史压缩的对接：主线程请求与其前一个主线程请求之间的旁路请求，若其输出正文出现在本请求的某条消息里，该旁路请求即客户端的
    # 压缩请求（opencode 1.18：system 为摘要指令、user 为 <conversation> 全文；下一个主线程请求为 user「What did we do so far?」+
    # assistant 摘要 + 保留的尾部消息 + 后续 user）。对上之后本请求的类型记为 compact_like，不论消息条数是否减半；
    # 输出全文未采集时只能靠消息条数减半的判定，此时仅列出中间的旁路请求供核对
    last_main = None
    for i, a in enumerate(out):
        if a.thread != "main":
            continue
        if last_main is not None and a.diff.get("kind") != "first":
            texts = [pcap.message_text(m) for m in (a.messages or [])]
            linked = False
            cands = []
            for x in out[last_main + 1:i]:
                if x.thread == "main":
                    continue
                oc = ((x.out or {}).get("content") or "").strip()
                hit = None
                if len(oc) >= 32:
                    hit = next((j for j, t in enumerate(texts) if oc in t), None)
                cands.append({"req": x.turn, "n_msgs": x.n_msgs, "tok": x.n_tokens, "out_chars": len(oc) if x.out else None, "msg": hit})
                if hit is not None:
                    x.compact_for = a.turn
                    linked = True
            if linked or a.diff.get("kind") == "compact_like":
                a.compact_from = cands
            if linked and a.diff.get("kind") != "compact_like":
                a.diff["kind"] = "compact_like"
                if a.diff.get("cause") in ("rewrite", "repeat", "message_changed"):
                    a.diff["cause"] = "compact_like"
        last_main = i
    return out
