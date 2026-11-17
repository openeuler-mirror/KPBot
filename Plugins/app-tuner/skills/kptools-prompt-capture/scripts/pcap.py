# pcap.py：读取 proxy 采集目录的公共库（analyze.py / export_corpus.py / replay.py / 测试脚本共用），只依赖标准库；
# 分词相关函数需要 transformers（按需导入）。
#
# 采集目录里是一个个「分段」文件 seg-*.jsonl（写入中）或 seg-*.jsonl.gz（已轮转压缩）。
# 每个分段自带它引用的全部内容，可以单独拷走分析。行类型（t 字段）：
#   h 分段头     r 请求记录   e 结束记录（响应侧信息）
#   m 消息正文   n 消息链节点（父节点 + 消息哈希，整个会话历史是一条链，共享前缀只存一次）
#   b 其他内容（k=tools/prompt/param/ids/ptxt/out）
# 字段说明见 README.md「数据格式」。
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
from typing import Any, Iterable, Iterator

FORMAT = 1
PROGRESS_EVERY_S = 10  # iter_requests(progress=True) 每隔多少秒报一次进度


# ---------------------------------------------------------------- 找文件、逐行读
def find_segments(paths: Iterable[str]) -> list[str]:
    """返回按文件名（即创建时间）排序的分段文件；同一分段的 .jsonl 与 .jsonl.gz 同时存在时取 .gz（压缩已完成）。"""
    found: dict[str, str] = {}
    for p in paths:
        if os.path.isfile(p):
            cands = [p]
        else:
            cands = []
            for root, _dirs, files in os.walk(p):
                cands += [os.path.join(root, f) for f in files]
        for f in cands:
            base = os.path.basename(f)
            if not base.startswith("seg-"):
                continue
            if base.endswith(".jsonl.gz"):
                found[base[:-3]] = f
            elif base.endswith(".jsonl"):
                found.setdefault(base, f)
    return [found[k] for k in sorted(found)]


def open_lines(path: str) -> Iterator[str]:
    """逐行读；容忍写入中的文件最后半行、被截断的 gzip 尾部。"""
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="surrogatepass") as f:
            for line in f:
                if line.endswith("\n"):
                    yield line[:-1]
                elif line.strip():  # 最后一行没写完：试着解析，失败就丢
                    try:
                        json.loads(line)
                        yield line
                    except ValueError:
                        pass
    except (EOFError, OSError) as exc:  # 未正常关闭的 gzip
        print(f"[pcap] {os.path.basename(path)}: stopped early ({type(exc).__name__}: {exc})", file=sys.stderr)


class MissingBlob(KeyError):
    pass


# ---------------------------------------------------------------- 分段与请求
class Segment:
    """一个分段文件的全部内容。消息正文与 blob 先存原始行，用到时才解析。"""

    def __init__(self, path: str):
        self.path = path
        self.header: dict | None = None
        self.nodes: dict[str, dict] = {}
        self.records: list[dict] = []
        self.ends: dict[str, dict] = {}
        self._raw_m: dict[str, str] = {}
        self._raw_b: dict[str, str] = {}
        self._m: dict[str, Any] = {}
        self._b: dict[str, Any] = {}
        self.bad_lines = 0
        for line in open_lines(path):
            if line.startswith('{"t":"m","h":"') and line[34:35] == '"':
                self._raw_m[line[14:34]] = line
                continue
            if line.startswith('{"t":"b","h":"') and line[34:35] == '"':
                self._raw_b[line[14:34]] = line
                continue
            try:
                o = json.loads(line)
            except ValueError:
                self.bad_lines += 1
                continue
            t = o.get("t")
            if t == "n":
                self.nodes[o["h"]] = o
            elif t == "r":
                self.records.append(o)
            elif t == "e":
                self.ends[o["id"]] = o
            elif t == "h":
                self.header = self.header or o
            elif t == "m":
                self._m[o["h"]] = o["d"]
            elif t == "b":
                self._b[o["h"]] = o["d"]

    def msg(self, h: str) -> Any:
        if h not in self._m:
            if h not in self._raw_m:
                raise MissingBlob(h)
            self._m[h] = json.loads(self._raw_m.pop(h))["d"]
        return self._m[h]

    def blob(self, h: str) -> Any:
        if h not in self._b:
            if h not in self._raw_b:
                raise MissingBlob(h)
            self._b[h] = json.loads(self._raw_b.pop(h))["d"]
        return self._b[h]

    def has_blob(self, h: str) -> bool:
        return h in self._b or h in self._raw_b


class Req:
    """一条被采集的请求：rec 是 r 行，end 是 e 行（可能为 None），seg 用来取正文。"""

    __slots__ = ("seg", "rec", "end", "end_seg")

    def __init__(self, seg: Segment, rec: dict, end: dict | None, end_seg: Segment | None = None):
        self.seg, self.rec, self.end = seg, rec, end
        self.end_seg = end_seg or seg

    @property
    def id(self) -> str:
        return self.rec["id"]

    @property
    def text_level(self) -> bool:
        return self.rec.get("lvl") == "text"

    def chain(self) -> list[dict]:
        """从第一条消息到最后一条消息的节点列表。"""
        out, h = [], self.rec.get("ml")
        while h is not None:
            n = self.seg.nodes.get(h)
            if n is None:
                raise MissingBlob(h)
            out.append(n)
            h = n.get("p")
        out.reverse()
        return out

    def messages(self) -> list | None:
        if not self.text_level or self.rec.get("ml") is None and self.rec.get("nm") is None:
            return None
        if self.rec.get("nm") == 0:
            return []
        return [self.seg.msg(n["m"]) for n in self.chain()]

    def tools(self) -> Any:
        h = self.rec.get("tools")
        return self.seg.blob(h) if h and self.text_level else None

    def prompt(self) -> Any:
        h = self.rec.get("prompt")
        return self.seg.blob(h) if h and self.text_level else None

    def params(self) -> dict:
        out = {}
        for k, v in (self.rec.get("params") or {}).items():
            if isinstance(v, dict) and "$blob" in v and self.text_level:
                v = self.seg.blob(v["$blob"])
            out[k] = v
        return out

    def body(self) -> dict:
        """按原始键顺序重建请求体（仅 text 级别记录）。keep_media=false 时图片等大字符串是占位串。"""
        if not self.text_level:
            raise ValueError("meta-level record has no text")
        params = self.params()
        out: dict[str, Any] = {}
        for k in self.rec.get("ko") or []:
            if k == "messages":
                out[k] = self.messages()
            elif k == "prompt":
                out[k] = self.prompt()
            elif k == "tools":
                out[k] = self.tools()
            elif k in params:
                out[k] = params[k]
        return out

    def gold_ids(self) -> list[int] | None:
        e = self.end
        if not e or not e.get("ids"):
            return None
        ids: list[int] = []
        for h in e["ids"]:
            ids += self.end_seg.blob(h)
        return ids

    def gold_text(self) -> str | None:
        e = self.end
        if not e or not e.get("ptxt"):
            return None
        return "".join(self.end_seg.blob(h) for h in e["ptxt"])

    def output(self) -> dict | None:
        e = self.end
        if not e or not e.get("out"):
            return None
        return self.end_seg.blob(e["out"])

    def headers(self) -> dict:
        return self.rec.get("hd") or {}


# ---------------------------------------------------------------- 进度与并行（各工具共用）
class Progress:
    """每隔 PROGRESS_EVERY_S 秒往 stderr 打一行：已完成数、百分比、已用时间、预计剩余时间。
    预计剩余 = 已用时间 / 已完成数 × 未完成数，前后请求大小差别大时只是粗略估计。"""

    def __init__(self, tag: str, total: int | None = None, unit: str = "requests", every: float | None = None):
        self.tag, self.total, self.unit = tag, total, unit
        self.every = PROGRESS_EVERY_S if every is None else every
        self.done = 0
        self.t0 = self.t_last = time.time()

    def step(self, n: int = 1) -> None:
        self.done += n
        now = time.time()
        if now - self.t_last >= self.every:
            self.t_last = now
            self.show(now)

    def show(self, now: float | None = None) -> None:
        el = (now or time.time()) - self.t0
        if self.total:
            left = el / self.done * (self.total - self.done) if self.done else 0.0
            print(f"[{self.tag}] {self.done}/{self.total} {self.unit} ({self.done / self.total:.0%}), "
                  f"{el:.0f}s elapsed, ~{left:.0f}s left", file=sys.stderr)
        else:
            print(f"[{self.tag}] {self.done} {self.unit}, {el:.0f}s elapsed", file=sys.stderr)


def detach(req: "Req") -> "Req":
    """把一条请求连同它用到的消息、工具定义、golden 拷成一个自包含的小对象，可以 pickle 给子进程。
    原来的 Req 指着整个分段（可能几百 MB），不能直接传。缺的内容照样缺着，子进程里会同样抛 MissingBlob。"""
    seg = Segment.__new__(Segment)
    seg.path, seg.header, seg.bad_lines = req.seg.path, req.seg.header, 0
    seg.nodes, seg.records, seg.ends = {}, [], {}
    seg._raw_m, seg._raw_b, seg._m, seg._b = {}, {}, {}, {}

    def cp(src: Segment, h: Any, kind: str) -> None:
        if not isinstance(h, str):
            return
        parsed, raw = (src._m, src._raw_m) if kind == "m" else (src._b, src._raw_b)
        dp, dr = (seg._m, seg._raw_m) if kind == "m" else (seg._b, seg._raw_b)
        if h in parsed:
            dp[h] = parsed[h]
        elif h in raw:
            dr[h] = raw[h]

    rec, e = req.rec, req.end or {}
    h = rec.get("ml")
    while h is not None and h not in seg.nodes:
        n = req.seg.nodes.get(h)
        if n is None:
            break
        seg.nodes[h] = n
        cp(req.seg, n.get("m"), "m")
        h = n.get("p")
    cp(req.seg, rec.get("tools"), "b")
    cp(req.seg, rec.get("prompt"), "b")
    for v in (rec.get("params") or {}).values():
        if isinstance(v, dict) and "$blob" in v:
            cp(req.seg, v["$blob"], "b")
    for k in ("ids", "ptxt"):
        for bh in e.get(k) or []:
            cp(req.end_seg, bh, "b")
    cp(req.end_seg, e.get("out"), "b")
    return Req(seg, rec, req.end, seg)


class Workers:
    """有序的进程池。imap(fn, items) 按输入顺序给出结果，同时在途的任务不超过 window 个（任务里带着请求原文，
    所以要限制）。jobs <= 1 时不起子进程，在本进程里直接算，走的是同一段代码。
    fn 与 init 必须是模块顶层函数：子进程用 spawn 启动（不继承父进程里已加载的分词器状态），靠名字找到它们。"""

    def __init__(self, jobs: int, init: Any = None, initargs: tuple = (), tag: str = "pcap"):
        self.jobs = max(1, int(jobs or 1))
        self.pool = None
        if self.jobs > 1:
            import multiprocessing  # noqa: PLC0415

            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")  # 每个子进程只用一个核，总核数由 --jobs 决定
            self.pool = multiprocessing.get_context("spawn").Pool(self.jobs, initializer=init, initargs=initargs)
            print(f"[{tag}] {self.jobs} worker processes", file=sys.stderr)
        else:
            if init is not None:
                init(*initargs)
            print(f"[{tag}] single process; add --jobs N to use N cores", file=sys.stderr)

    def imap(self, fn: Any, items: Iterable[Any], window: int | None = None) -> Iterator[Any]:
        if self.pool is None:
            for it in items:
                yield fn(it)
            return
        import collections  # noqa: PLC0415

        window = window or self.jobs * 3
        q: collections.deque = collections.deque()
        for it in items:
            q.append(self.pool.apply_async(fn, (it,)))
            if len(q) >= window:
                yield q.popleft().get()
        while q:
            yield q.popleft().get()

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
            self.pool.join()
            self.pool = None

    def __enter__(self) -> "Workers":
        return self

    def __exit__(self, et: Any, ev: Any, tb: Any) -> None:
        if self.pool is not None and et is not None:
            self.pool.terminate()
            self.pool = None
        self.close()


def iter_requests(paths: Iterable[str], *, join_late: bool = True, progress: "bool | str" = False,
                  ordered: bool = True) -> Iterator[Req]:
    """逐条给出请求。
    ordered=True（默认）：按到达时刻 ts 全局排序。多个 worker 的分段在时间上交错，先扫一遍建索引（每个分段的
      (ts, id)），再做 k 路归并；内存里只保留时间范围与当前时刻重叠的那几个分段（约等于同时写入的写盘进程数）。
    ordered=False：按分段文件顺序，每次只放一个分段在内存里（更省内存，但多 worker 时不是到达顺序）。
    join_late：把写在后续分段里的结束记录（请求超过 pending_max_min 才结束，或写盘进程中途重启）接回来。"""
    import heapq  # noqa: PLC0415

    files = find_segments(paths)
    late: dict[str, tuple[dict, str]] = {}
    index: list[list[tuple[float, str]]] = []
    for f in files:
        recs: list[tuple[float, str]] = []
        for line in open_lines(f):
            if ordered and line.startswith('{"t":"r"'):
                try:
                    o = json.loads(line)
                    recs.append((float(o.get("ts") or 0), o["id"]))
                except (ValueError, KeyError, TypeError):
                    pass
            elif join_late and line.startswith('{"t":"e"') and ('"late":true' in line or '"orphan":true' in line):
                try:
                    o = json.loads(line)
                    late[o["id"]] = (o, f)
                except (ValueError, KeyError):
                    pass
        recs.sort()
        index.append(recs)
    late_segs: dict[str, Segment] = {}

    def make(seg: Segment, rec: dict) -> Req:
        end, end_seg = seg.ends.get(rec["id"]), None
        if end is None and rec["id"] in late:
            end, lf = late[rec["id"]]
            if end.get("ids") or end.get("ptxt") or end.get("out"):
                if lf not in late_segs:
                    late_segs.clear()
                    late_segs[lf] = Segment(lf)
                end_seg = late_segs[lf]
        return Req(seg, rec, end, end_seg)

    if not ordered:
        for i, f in enumerate(files):
            if progress:
                print(f"[pcap] loading segment {i + 1}/{len(files)} {os.path.basename(f)}", file=sys.stderr)
            seg = Segment(f)
            for rec in seg.records:
                yield make(seg, rec)
        return
    heap = [(recs[0][0], i, 0) for i, recs in enumerate(index) if recs]
    heapq.heapify(heap)
    loaded: dict[int, tuple[Segment, dict[str, dict]]] = {}
    n_loaded = 0
    total = sum(len(recs) for recs in index)
    pg = Progress(progress if isinstance(progress, str) else "pcap", total) if progress else None
    if pg:
        print(f"[{pg.tag}] {total} requests in {len(files)} segments", file=sys.stderr)
    first = True
    while heap:
        _ts, i, k = heapq.heappop(heap)
        if pg and not first:  # 上一条已经被调用方处理完了：两次之间的时间含调用方的处理时间
            pg.step()
        first = False
        if i not in loaded:
            n_loaded += 1
            if progress:
                print(f"[{pg.tag}] loading segment {n_loaded}/{len(files)} {os.path.basename(files[i])} "
                      f"(in memory: {len(loaded) + 1})",
                      file=sys.stderr)
            seg = Segment(files[i])
            loaded[i] = (seg, {r["id"]: r for r in seg.records})
        seg, by_id = loaded[i]
        rec = by_id.get(index[i][k][1])
        if rec is not None:
            yield make(seg, rec)
        if k + 1 < len(index[i]):
            heapq.heappush(heap, (index[i][k + 1][0], i, k + 1))
        else:
            del loaded[i]  # 这个分段的请求都给出去了，释放


# ---------------------------------------------------------------- 仿 vLLM 的 chat 预处理（离线分词用）
# 依据 vLLM v0.30.0：
#   chat_completion/protocol.py ChatCompletionRequest._normalize_messages_before：reasoning_content 改名为 reasoning
#   chat_utils.py _parse_chat_message_content：只保留 role/content/tool_calls/reasoning(+reasoning_content)/
#     tool_call_id/name；"string" 内容格式下文本片段用 "\n" 连接
#   chat_utils.py _postprocess_messages：tool_calls[].function.arguments 从 JSON 字符串解析成 dict
#   online_renderer.py：tools 用 ChatCompletionToolsParam.model_dump()（缺的 description/parameters 为 None）
#   chat_completion/protocol.py build_chat_params + renderers/params.py merge_kwargs：
#     模板参数 = 请求的 chat_template_kwargs，再被 add_generation_prompt 等覆盖（值为 None/"auto" 的不覆盖）
# 没有覆盖到的：developer 角色转换、多条 system 合并、多模态占位符、Mistral/Harmony/DeepSeek-V4 自带编码器等。
# 离线结果与 vLLM 是否一致，analyze.py 会用 P 返回的 prompt_tokens 与 golden ids 逐条核对（tok_match_* 列）。
_CONTENT_LOOP = re.compile(r"for\s+\w+\s+in\s+\w+(?:\[['\"]content['\"]\]|\.content)\b")


def detect_content_format(template: str | None) -> str:
    return "openai" if template and _CONTENT_LOOP.search(template) else "string"


def vllm_like_conversation(messages: list, content_format: str = "string") -> list[dict]:
    conv: list[dict] = []
    for raw in messages:
        msg = json.loads(json.dumps(raw))  # 深拷贝，下面会原地改
        if not isinstance(msg, dict):
            continue
        rc = msg.pop("reasoning_content", None)
        if rc is not None and msg.get("reasoning") is None:
            msg["reasoning"] = rc
        role = msg.get("role")
        content = msg.get("content")
        reasoning = msg.get("reasoning")
        if content is None:
            parts: list = []
        elif isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        else:
            parts = list(content)
        if content_format == "openai":
            c: Any = []
            for p in parts:
                if isinstance(p, str):
                    c.append({"type": "text", "text": p})
                elif isinstance(p, dict) and p.get("type") == "text":
                    c.append({"type": "text", "text": p.get("text", "")})
                elif isinstance(p, dict):
                    c.append({"type": p.get("type")})
        else:
            texts = [p if isinstance(p, str) else p.get("text", "") for p in parts
                     if isinstance(p, str) or (isinstance(p, dict) and p.get("type") == "text")]
            c = "\n".join(texts)
        out: dict[str, Any] = {"role": role, "content": c}
        if role == "assistant":
            if msg.get("tool_calls") is not None:
                out["tool_calls"] = list(msg["tool_calls"])
            if reasoning is not None:
                out["reasoning"] = reasoning
                out["reasoning_content"] = reasoning
        elif role == "tool" and "tool_call_id" in msg:
            out["tool_call_id"] = msg["tool_call_id"]
        if isinstance(msg.get("name"), str):
            out["name"] = msg["name"]
        conv.append(out)
    for m in conv:
        if m["role"] != "assistant" or "tool_calls" not in m or not isinstance(m["tool_calls"], list):
            continue
        if not m["tool_calls"]:
            m.pop("tool_calls")
            continue
        for item in m["tool_calls"]:
            fn = item.get("function") if isinstance(item, dict) else None
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments")
            if args:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = None
                fn["arguments"] = args if isinstance(args, dict) else {}
            else:
                fn["arguments"] = {}
    return conv


def vllm_like_tools(tools: Any) -> list[dict] | None:
    if not isinstance(tools, list):
        return None
    out = []
    for t in tools:
        fn = (t or {}).get("function") or {}
        f: dict[str, Any] = {"name": fn.get("name"), "description": fn.get("description"),
                             "parameters": fn.get("parameters")}
        if fn.get("strict") is not None:
            f["strict"] = fn["strict"]
        dl = fn.get("defer_loading") if fn.get("defer_loading") is not None else t.get("defer_loading")
        if dl is not None:
            f["defer_loading"] = dl
        d: dict[str, Any] = {"type": "function", "function": f}
        if t.get("defer_loading") is not None:
            d["defer_loading"] = t["defer_loading"]
        out.append(d)
    return out


def vllm_like_template_kwargs(body: dict, server_defaults: dict | None = None) -> dict:
    extra = {
        "add_generation_prompt": body.get("add_generation_prompt", True),
        "continue_final_message": body.get("continue_final_message", False),
        "documents": body.get("documents"),
        "reasoning_effort": body.get("reasoning_effort"),
    }
    user = body.get("chat_template_kwargs") or {}
    if body.get("reasoning_effort") is not None and "enable_thinking" not in user:
        extra["enable_thinking"] = body["reasoning_effort"] != "none"
    merged = dict(server_defaults or {})
    merged.update({k: v for k, v in user.items() if v not in (None, "auto")})
    merged.update({k: v for k, v in extra.items() if v not in (None, "auto")})
    return merged


class Renderer:
    """用 HF 分词器离线重现 vLLM 对一个请求的模板渲染与分词。"""

    def __init__(self, tokenizer_path: str, content_format: str = "auto", server_template_kwargs: dict | None = None,
                 trust_remote_code: bool = False):
        from transformers import AutoTokenizer  # noqa: PLC0415

        self.tok = AutoTokenizer.from_pretrained(
            tokenizer_path, trust_remote_code=trust_remote_code)
        tmpl = getattr(self.tok, "chat_template", None)
        if isinstance(tmpl, dict):
            tmpl = tmpl.get("default") or next(iter(tmpl.values()), None)
        self.content_format = detect_content_format(tmpl) if content_format == "auto" else content_format
        self.server_kwargs = server_template_kwargs or {}

    def render(self, body: dict, ep: str) -> str:
        if ep == "chat":
            conv = vllm_like_conversation(body.get("messages") or [], self.content_format)
            kw = vllm_like_template_kwargs(body, self.server_kwargs)
            if body.get("chat_template"):
                kw["chat_template"] = body["chat_template"]
            return self.tok.apply_chat_template(conv, tools=vllm_like_tools(body.get("tools")), tokenize=False, **kw)
        p = body.get("prompt")
        if isinstance(p, list):
            p = p[0] if p and isinstance(p[0], str) else None
        return p if isinstance(p, str) else ""

    def encode(self, text: str, ep: str, body: dict | None = None) -> list[int]:
        if ep == "chat":
            add = bool((body or {}).get("add_special_tokens", False))
        else:
            add = bool((body or {}).get("add_special_tokens", True))
        return self.tok(text, add_special_tokens=add)["input_ids"]


# ---------------------------------------------------------------- 文本特征（与分词器无关）
# 字符类别按 UTF-8 首字节划分：一次 bytes.translate + 若干次 count，1 MB 文本约 1 ms。
CHAR_CLASSES = [
    ("ascii_letter", "A-Z a-z"), ("digit", "0-9"), ("space", "空格 U+0020"), ("tab", "\\t"), ("newline", "\\n"),
    ("cr", "\\r"), ("ascii_punct", "其余可见 ASCII 符号"), ("ctrl", "其余 ASCII 控制字符"),
    ("u2", "2 字节 UTF-8：U+0080–07FF（拉丁扩展、希腊、西里尔、阿拉伯等）"),
    ("u3_misc", "3 字节 E0–E2：U+0800–2FFF（印度系、泰文、通用标点“”…、箭头、制表符等）"),
    ("u3_cjk_sym", "3 字节 E3：U+3000–3FFF（中日标点。、假名、注音）"),
    ("u3_han", "3 字节 E4–E9：U+4000–9FFF（CJK 统一汉字）"),
    ("u3_hangul", "3 字节 EA–ED：U+A000–DFFF（韩文等）"),
    ("u3_fullwidth", "3 字节 EE–EF：U+E000–FFFF（私用区、全角符号，，：（）等）"),
    ("u4", "4 字节：U+10000 以上（emoji、扩展汉字）"),
]
_CODES = {name: bytes([65 + i]) for i, (name, _d) in enumerate(CHAR_CLASSES)}


def _build_table() -> bytes:
    t = bytearray(256)
    for b in range(256):
        if 65 <= b <= 90 or 97 <= b <= 122:
            name = "ascii_letter"
        elif 48 <= b <= 57:
            name = "digit"
        elif b == 32:
            name = "space"
        elif b == 9:
            name = "tab"
        elif b == 10:
            name = "newline"
        elif b == 13:
            name = "cr"
        elif 33 <= b <= 126:
            name = "ascii_punct"
        elif b < 128:
            name = "ctrl"
        elif b < 0xC0:
            t[b] = ord("x")  # 续字节
            continue
        elif b < 0xE0:
            name = "u2"
        elif b < 0xE3:
            name = "u3_misc"
        elif b == 0xE3:
            name = "u3_cjk_sym"
        elif b < 0xEA:
            name = "u3_han"
        elif b < 0xEE:
            name = "u3_hangul"
        elif b < 0xF0:
            name = "u3_fullwidth"
        else:
            name = "u4"
        t[b] = _CODES[name][0]
    return bytes(t)


_TABLE = _build_table()
_SP, _TB, _NL, _CR, _DG = (_CODES[k] for k in ("space", "tab", "newline", "cr", "digit"))
_RE_WS_RUN = re.compile(b"[" + _SP + _TB + b"]{4,}")  # 连续 ≥4 个空格/制表符（代码缩进）
_RE_WS_ANY = re.compile(b"[" + _SP + _TB + b"]+")
_RE_WORD = re.compile(b"[^" + _SP + _TB + _NL + _CR + b"]+")  # 不含空白的连续片段，长度按 UTF-8 字节
_RE_DIGITS = re.compile(_DG + b"+")
_RE_LINE = re.compile(b"[^" + _NL + b"]+")
_RE_B64 = re.compile(r"[A-Za-z0-9+/=_-]{100,}")
_RE_SPECIAL_LIKE = re.compile(r"<\|[^|<>\s]{1,40}\|>|<｜[^｜<>\s]{1,40}｜>|\[gMASK\]|<sop>|<eop>|<think>|</think>")


def text_features(s: str) -> dict[str, float]:
    b = s.encode("utf-8", "surrogatepass")
    t = b.translate(_TABLE)
    f: dict[str, float] = {"chars": len(s), "bytes": len(b)}
    for name, code in _CODES.items():
        f["cc_" + name] = t.count(code)
    f["lines"] = f["cc_newline"] + (1 if s else 0)
    f["line_max_bytes"] = max((m.end() - m.start() for m in _RE_LINE.finditer(t)), default=0)
    f["ws_run_max"] = max((m.end() - m.start() for m in _RE_WS_ANY.finditer(t)), default=0)
    f["ws_runs_ge4"] = sum(1 for _ in _RE_WS_RUN.finditer(t))
    wmax = w64 = w256 = w256b = 0
    for m in _RE_WORD.finditer(t):
        n = m.end() - m.start()
        if n > wmax:
            wmax = n
        if n >= 64:
            w64 += 1
            if n >= 256:
                w256 += 1
                w256b += n
    f["word_max_bytes"], f["words_ge64b"], f["words_ge256b"], f["bytes_in_words_ge256b"] = wmax, w64, w256, w256b
    dmax = dn = 0
    for m in _RE_DIGITS.finditer(t):
        dn += 1
        dmax = max(dmax, m.end() - m.start())
    f["digit_runs"], f["digit_run_max"] = dn, dmax
    f["base64_like_runs"] = sum(1 for _ in _RE_B64.finditer(s))
    f["special_like"] = sum(1 for _ in _RE_SPECIAL_LIKE.finditer(s))
    f["code_fences"] = s.count("```")
    f["urls"] = s.count("http://") + s.count("https://")
    f["json_punct"] = sum(b.count(c) for c in (b"{", b"}", b"[", b"]", b'"', b":", b","))
    return f


def dominant_script(f: dict[str, float]) -> str:
    """按字符类别给整段文本一个标签：cjk / ascii / latin_ext / mixed / other。"""
    n = f["chars"] - f["cc_space"] - f["cc_tab"] - f["cc_newline"] - f["cc_cr"]
    if n <= 0:
        return "empty"
    cjk = f["cc_u3_han"] + f["cc_u3_cjk_sym"] + f["cc_u3_fullwidth"] + f["cc_u3_hangul"]
    ascii_ = f["cc_ascii_letter"] + f["cc_digit"] + f["cc_ascii_punct"]
    if cjk / n >= 0.3:
        return "cjk"
    if ascii_ / n >= 0.95:
        return "ascii"
    if f["cc_u2"] / n >= 0.3:
        return "latin_ext"
    if cjk / n >= 0.05:
        return "mixed_cjk"
    return "mixed"


def message_text(m: Any) -> str:
    """一条消息里会进入模板的全部文本（content、reasoning、工具调用名与参数），用 \\n 连接。"""
    if not isinstance(m, dict):
        return json.dumps(m, ensure_ascii=False)
    out: list[str] = []
    c = m.get("content")
    if isinstance(c, str):
        out.append(c)
    elif isinstance(c, list):
        for p in c:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
    r = m.get("reasoning") if isinstance(m.get("reasoning"), str) else m.get("reasoning_content")
    if isinstance(r, str):
        out.append(r)
    for call in m.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            out.append(str(fn.get("name") or ""))
            a = fn.get("arguments")
            out.append(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
    return "\n".join(out)


def client_family(ua: str | None) -> str:
    """与 proxy 写盘进程里 _cap_client_family 相同的规则。"""
    u = (ua or "").lower()
    for key, name in (("opencode", "opencode"), ("claude-cli", "claude-code"), ("claude-code", "claude-code"),
                      ("cline", "cline"), ("roo", "roo-code"), ("kilo", "kilo-code"), ("cursor", "cursor"),
                      ("continue", "continue"), ("openai/python", "openai-python"), ("openai/js", "openai-node"),
                      ("langchain", "langchain"), ("litellm", "litellm"), ("curl/", "curl"),
                      ("python-requests", "python-requests"), ("python-httpx", "python-httpx"),
                      ("aiohttp", "aiohttp"), ("go-http-client", "go"), ("okhttp", "okhttp"), ("mozilla", "browser")):
        if key in u:
            return name
    return "other" if u else "none"
