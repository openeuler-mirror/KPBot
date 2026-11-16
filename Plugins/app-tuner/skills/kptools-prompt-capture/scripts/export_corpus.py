# export_corpus.py：从采集数据导出语料，给分词器的基准测试、PGO 训练、正确性核对和回放使用。
#   python export_corpus.py <采集目录> --out corpus.jsonl --format rendered [--tokenizer <模型目录>]
# --format（每行一个 JSON）：
#   rendered   {"id","ts","session","src","text"}          模板后文本 = 分词器的真实输入。优先用 golden（P 返回的
#              prompt_text），没有 golden 时用 --tokenizer 离线渲染。与 profiling/pgo_train.py、bench_tok.py 的
#              {"text": ...} 格式兼容。
#   gold       {"id","text","ids"}                           只含有 golden 的请求：vLLM 实际的输入文本与 token ids，
#              给 check_tokenizer.py 核对新编译/替换的分词器是否逐 token 一致。
#   requests   {"id","ts","endpoint","headers","body"}      重建的原始请求体，给 replay.py 回放或发给 vLLM /tokenize。
#   messages   {"id","messages","tools"}                     给 Kimi 这类「模板与分词一步完成」的分词器（pgo_train.py 的另一种输入）。
#   texts      {"role","text"}                               去重后的单条消息正文（不套模板），做纯文本分词基准用。
# 选取：--sample N（随机取 N 条）、--max-mb（累计文本上限）、--dedup（相同文本只留一条）、--min/--max-prompt-tokens、
#       --client、--endpoint、--since/--until；默认按到达时间顺序输出（PGO 训练建议保持顺序，词缓存行为更接近线上）。
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcap  # noqa: E402
from private_io import prepare_output_dir, private_open

REPLAY_HEADERS = ("user-agent", "x-session-id", "x-session-affinity", "x-parent-session-id", "x-title",
                  "http-referer")


def main() -> int:
    ap = argparse.ArgumentParser(description="从采集数据导出语料")
    ap.add_argument("data", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--format", required=True, choices=["rendered", "gold", "requests", "messages", "texts"])
    ap.add_argument("--tokenizer", help="rendered 格式在没有 golden 时用它离线渲染")
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--content-format", default="auto", choices=["auto", "string", "openai"])
    ap.add_argument("--template-kwargs", help="vLLM 的 --default-chat-template-kwargs（JSON）")
    ap.add_argument("--no-gold", action="store_true", help="rendered 格式也不用 golden，全部离线渲染")
    ap.add_argument("--sample", type=int, default=0, help="随机抽 N 条（在其余条件过滤之后）；0 = 全部")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-mb", type=float, default=0, help="文本累计达到这么多 MB 就停止；0 = 不限")
    ap.add_argument("--dedup", action="store_true", help="文本（或请求体）完全相同的只留第一条")
    ap.add_argument("--min-prompt-tokens", type=int, default=0)
    ap.add_argument("--max-prompt-tokens", type=int, default=0)
    ap.add_argument("--client", help="只要这个客户端（analyze.py 的 client 列取值，如 opencode）")
    ap.add_argument("--endpoint", choices=["chat", "completions"])
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--split", choices=["train", "eval"],
                    help="按会话哈希把数据切成不重叠的两份：blake2b(split_salt|会话ID) < --split-ratio 的会话归 train，其余归 eval。"
                         "同一会话的请求全在同一份里；没有会话 ID 的请求按采集 ID 哈希。与采集时的 salt 无关")
    ap.add_argument("--split-ratio", type=float, default=0.7, help="train 占的比例（按会话数，默认 0.7）")
    ap.add_argument("--split-salt", default="", help="换一个值就换一种切法")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行进程数：渲染与序列化放到子进程，输出与单进程逐字节相同。只有需要离线渲染的 rendered 格式"
                         "收益明显，其余格式主要耗时在读写文件")
    a = ap.parse_args()
    if not 0.0 < a.split_ratio < 1.0:
        sys.exit("--split-ratio 需要在 0 和 1 之间")

    since = parse_time(a.since)
    until = parse_time(a.until)
    need_render = a.format == "rendered"

    # 第一遍：挑出符合条件的请求 id（抽样需要先知道总数）
    cand: list[str] = []
    for r in pcap.iter_requests(a.data, progress="export 1/2 select"):
        if keep(r, a, since, until):
            cand.append(r.id)
    if a.sample and a.sample < len(cand):
        chosen = set(random.Random(a.seed).sample(cand, a.sample))
    else:
        chosen = set(cand)

    # 第二遍：逐条导出。渲染、序列化、算哈希在 export_lines 里做（--jobs N 时在子进程），主进程按到达顺序去重并写文件
    opts = {"format": a.format, "no_gold": a.no_gold, "dedup": a.dedup}
    init = (a.tokenizer if need_render else None, a.content_format, a.template_kwargs, a.trust_remote_code)

    def jobs():
        for r in pcap.iter_requests(a.data, progress="export 2/2 write"):
            if r.id in chosen:
                yield (pcap.detach(r) if a.jobs > 1 else r), opts

    seen: set[str] = set()
    n = nbytes = skipped = 0
    src_count = {"gold": 0, "offline": 0}
    prepare_output_dir(os.path.dirname(os.path.abspath(a.out)))
    with private_open(a.out) as out, pcap.Workers(a.jobs, make_renderer, init, tag="export") as w:
        for lines, sc in w.imap(export_lines, jobs()):
            if lines is None:
                skipped += 1
                continue
            for k, v in sc.items():
                src_count[k] += v
            for line, hk, nb in lines:
                if a.dedup:
                    if hk in seen:
                        continue
                    seen.add(hk)
                out.write(line + "\n")
                n += 1
                nbytes += nb
                if a.max_mb and nbytes >= a.max_mb * 2**20:
                    break
            if a.max_mb and nbytes >= a.max_mb * 2**20:
                break
    extra = f"（golden {src_count['gold']}，离线渲染 {src_count['offline']}）" if a.format == "rendered" else ""
    print(f"导出 {n} 条、文本 {nbytes / 2**20:.2f} MB -> {a.out}{extra}；候选 {len(cand)}，"
          f"缺内容跳过 {skipped}", file=sys.stderr)
    return 0


_RND: "pcap.Renderer | None" = None
_SEEN: set[str] = set()  # 本进程已经交出去的文本哈希（--dedup）


def make_renderer(tokenizer, content_format, template_kwargs, trust_remote_code) -> None:
    global _RND
    _RND = None
    if tokenizer:
        _RND = pcap.Renderer(tokenizer, content_format, json.loads(template_kwargs) if template_kwargs else None,
                             trust_remote_code=trust_remote_code)


def export_lines(job: tuple) -> tuple:
    """一条请求导出成若干行：返回 ([(这一行的 JSON, 去重用的哈希, 文本字节数)], 来源计数)；缺内容时返回 (None, {})。"""
    r, opts = job
    a = argparse.Namespace(**opts)
    sc = {"gold": 0, "offline": 0}
    try:
        items = list(export_one(r, a, _RND, sc))
    except pcap.MissingBlob:
        return None, {}
    lines = []
    for obj, key_text in items:
        kb = key_text.encode("utf-8", "surrogatepass")
        hk = None
        if a.dedup:
            # 本进程交出去过的文本，主进程一定已经见过（任务按到达顺序分发、结果按到达顺序处理），不用再序列化。
            # 别的进程交出去的由主进程去重。
            hk = hashlib.blake2b(kb, digest_size=16).hexdigest()
            if hk in _SEEN:
                continue
            _SEEN.add(hk)
        line = json.dumps(obj, ensure_ascii=False)
        try:
            line.encode("utf-8")
        except UnicodeEncodeError:  # 含孤立代理项：输出文件按 UTF-8 写不进去，这一行改用 \uXXXX 转义
            line = json.dumps(obj, ensure_ascii=True)
        lines.append((line, hk, len(kb)))
    return lines, sc


def parse_time(s):
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return time.mktime(time.strptime(s.replace("T", " "), "%Y-%m-%d %H:%M:%S" if ":" in s else "%Y-%m-%d"))


def split_of(r: pcap.Req, a) -> str:
    key = r.rec.get("sess") or ("id:" + r.id)
    u = int.from_bytes(hashlib.blake2b((a.split_salt + "|" + key).encode("utf-8", "surrogatepass"),
                                       digest_size=8).digest(), "big") / 2.0**64
    return "train" if u < a.split_ratio else "eval"


def keep(r: pcap.Req, a, since, until) -> bool:
    rec, e = r.rec, r.end or {}
    if not r.text_level or rec.get("err"):
        return False
    if a.split and split_of(r, a) != a.split:
        return False
    if since and rec["ts"] < since or until and rec["ts"] >= until:
        return False
    if a.endpoint and rec.get("ep") != a.endpoint:
        return False
    if a.client and pcap.client_family(r.headers().get("user-agent")) != a.client:
        return False
    pt = e.get("pt")
    if a.min_prompt_tokens and not (isinstance(pt, int) and pt >= a.min_prompt_tokens):
        return False
    if a.max_prompt_tokens and not (isinstance(pt, int) and pt <= a.max_prompt_tokens):
        return False
    if a.format == "gold" and not e.get("ids"):
        return False
    return True


def export_one(r: pcap.Req, a, rnd, src_count):
    rec = r.rec
    base = {"id": r.id, "ts": rec["ts"], "session": rec.get("sess")}
    if a.format == "rendered":
        text = None if a.no_gold else r.gold_text()
        src = "gold"
        if text is None:
            if rnd is None:
                return  # 没有 golden 也没给 --tokenizer：这条导不出
            body = r.body()
            p = body.get("prompt")
            if rec.get("ep") == "completions" and isinstance(p, list) and p and isinstance(p[0], int):
                return  # token id 形式的 prompt 没有文本
            text, src = rnd.render(body, rec.get("ep")), "offline"
        src_count[src] += 1
        yield {**base, "src": src, "text": text}, text
    elif a.format == "gold":
        text, ids = r.gold_text(), r.gold_ids()
        if ids is None:
            return
        if text is None and rec.get("ep") == "completions" and isinstance(r.prompt(), str):
            text = r.prompt()
        if text is None:
            return
        yield {"id": r.id, "endpoint": rec.get("ep"), "text": text, "ids": ids}, text
    elif a.format == "requests":
        body = r.body()
        hd = {k: v for k, v in r.headers().items() if k in REPLAY_HEADERS}
        s = json.dumps(body, ensure_ascii=False, sort_keys=True)
        yield {**base, "endpoint": rec.get("ep"), "headers": hd, "body": body}, s
    elif a.format == "messages":
        if rec.get("ep") != "chat":
            return
        msgs, tools = r.messages(), r.tools()
        obj = {"id": r.id, "messages": msgs}
        if tools is not None:
            obj["tools"] = tools
        yield obj, json.dumps(obj, ensure_ascii=False, sort_keys=True)
    elif a.format == "texts":
        if rec.get("ep") == "chat":
            for n, m in zip(r.chain(), r.messages() or []):
                t = pcap.message_text(m)
                if t:
                    yield {"role": n["r"], "text": t}, t
        else:
            p = r.prompt()
            if isinstance(p, str):
                yield {"role": "prompt", "text": p}, p


if __name__ == "__main__":
    sys.exit(main())
