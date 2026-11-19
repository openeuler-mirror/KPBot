# dump_requests.py：绕开 session_view 的任何分析，直接打印采集文件里某个会话若干请求的原始内容，用来核对页面显示是否与采集一致。
#   python tools/dump_requests.py <采集目录> --session <会话 ID 或前缀> [--req 19 20 21] [--chars 200]
# 每个请求打印：到达时刻、线程无关的原始字段（nm 消息条数、nt 工具数、bb 请求体字节）、vLLM 返回的 prompt_tokens / cached_tokens、
# 每条消息的角色 / 字符数 / 开头若干字，以及（output=text 时）模型输出的正文、思考、工具调用开头若干字。
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcap  # noqa: E402


def head(t: str, n: int) -> str:
    t = t.replace("\n", "⏎")
    return t if len(t) <= n else t[:n] + f"…（共 {len(t)} 字符）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="+")
    ap.add_argument("--session", required=True, help="会话 ID，可用前缀")
    ap.add_argument("--req", nargs="*", type=int, help="会话内请求序号（按到达时间自 0 起）；不给则全部")
    ap.add_argument("--chars", type=int, default=200, help="每条消息与输出打印开头多少字符")
    a = ap.parse_args()
    reqs = [r for r in pcap.iter_requests(a.data) if (r.rec.get("sess") or "").startswith(a.session)]
    reqs.sort(key=lambda r: r.rec["ts"])
    if not reqs:
        print("没有匹配的会话", file=sys.stderr)
        return 1
    ids = {r.rec.get("sess") for r in reqs}
    if len(ids) > 1:
        print(f"前缀匹配到 {len(ids)} 个会话：{sorted(ids)}，请给更长的前缀", file=sys.stderr)
        return 1
    print(f"会话 {reqs[0].rec.get('sess')}：共 {len(reqs)} 个请求")
    for i, r in enumerate(reqs):
        if a.req and i not in a.req:
            continue
        rec, e = r.rec, r.end or {}
        ts = time.strftime("%H:%M:%S", time.localtime(rec["ts"])) + f".{int(rec['ts'] * 1000) % 1000:03d}"
        print(f"\n## 请求 {i}  {ts}  级别={rec.get('lvl')}  ep={rec.get('ep')}  消息 nm={rec.get('nm')}  工具 nt={rec.get('nt')}  "
              f"请求体 bb={rec.get('bb')} 字节  prompt_tokens={e.get('pt')}  cached_tokens={e.get('ct')}  finish={e.get('fr')}  输出 {e.get('ot')} token")
        if not r.text_level:
            print("   （只有元数据，没有全文）")
            continue
        try:
            body = r.body()
        except pcap.MissingBlob as exc:
            print(f"   正文缺失：{exc}")
            continue
        msgs = body.get("messages") or []
        print(f"   messages 共 {len(msgs)} 条，tools {len(body.get('tools') or [])} 个，其他字段：{[k for k in body if k not in ('messages', 'tools')]}")
        for j, m in enumerate(msgs):
            t = pcap.message_text(m)
            extra = ""
            if isinstance(m, dict):
                if m.get("tool_calls"):
                    extra += f"  tool_calls={[((c.get('function') or {}).get('name')) for c in m['tool_calls'] if isinstance(c, dict)]}"
                if m.get("tool_call_id"):
                    extra += f"  tool_call_id={m['tool_call_id']}"
                rc = m.get("reasoning_content") if isinstance(m.get("reasoning_content"), str) else m.get("reasoning")
                if isinstance(rc, str):
                    extra += f"  reasoning={len(rc)} 字符"
            print(f"   [{j}] {m.get('role') if isinstance(m, dict) else '?':<9} {len(t):>7} 字符{extra}")
            print(f"        {head(t, a.chars)}")
        out = r.output()
        if isinstance(out, dict):
            print(f"   模型输出：正文 {len(out.get('content') or '')} 字符，思考 {len(out.get('reasoning') or '')} 字符，工具调用 {len(out.get('tool_calls') or [])} 个")
            if out.get("reasoning"):
                print(f"        思考：{head(out['reasoning'], a.chars)}")
            if out.get("content"):
                print(f"        正文：{head(out['content'], a.chars)}")
            for c in out.get("tool_calls") or []:
                print(f"        调用 {c.get('name')}：{head(str(c.get('arguments') or ''), a.chars)}")
        elif e.get("out") is None and e:
            print("   模型输出全文未采集（output / follow_output 不为 text）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
