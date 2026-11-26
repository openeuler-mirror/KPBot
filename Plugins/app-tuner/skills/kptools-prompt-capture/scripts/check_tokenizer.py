# check_tokenizer.py：用采集到的 golden（vLLM 线上实际的输入文本与 token ids）核对一个分词器，并测吞吐。
#   python export_corpus.py <采集目录> --format gold --out gold.jsonl
#   python check_tokenizer.py --corpus gold.jsonl --tokenizer <模型目录> [--fastokens] [--repeat 3]
# 用途：重新编译（PGO/LTO/target-cpu）或替换（fastokens 等）分词器之后，先确认逐 token 一致，再看快了多少。
# 调用方式与 vLLM 一致：transformers 的 tokenizer(text, add_special_tokens=False)（chat 请求）。
# 输出：一致/不一致条数、第一个不一致的位置与前后 token、总字节、总 token、吞吐（MB/s）与每条耗时分位数。
# --seg-cache：经 profiling/seg_cache.py 的分段缓存编码（在特殊词元处切段，已编码过的段直接复用）。
#   按语料顺序单遍执行、缓存从空开始，相当于线上按到达顺序处理；此时 --repeat 固定为 1。
#   用来量化 agent 会话里「历史每次重分」可以省掉多少，并确认结果仍与 golden 逐 token 一致。
# --jobs N：N 个进程同时核对。一致/不一致的结论与单进程相同；吞吐是各进程单核耗时之和算出来的，进程之间互相挤占，
#   数字比单进程偏低，所以比较两个分词器谁快时用 --jobs 1。--seg-cache 的缓存与顺序有关，固定单进程。
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcap  # noqa: E402


_TOK = None
_SC = None


def load(tokenizer: str, trust_remote_code: bool, use_fastokens: bool, seg_cache_module: str | None,
         warm: list[tuple[str, bool]]) -> None:
    global _TOK, _SC
    if use_fastokens:
        import fastokens  # noqa: PLC0415

        fastokens.patch_transformers()
    from transformers import AutoTokenizer  # noqa: PLC0415

    _TOK = AutoTokenizer.from_pretrained(tokenizer, trust_remote_code=trust_remote_code)
    for text, add in warm:
        _TOK(text, add_special_tokens=add)  # 预热（不经分段缓存）
    if seg_cache_module:
        import importlib.util  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location("seg_cache", seg_cache_module)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SC = mod.SegmentCache(_TOK)


def encode(text: str, add: bool) -> list[int]:
    if _SC is not None and not add:
        return _SC.encode(text)
    return _TOK(text, add_special_tokens=add)["input_ids"]


def check_one(job: tuple) -> dict:
    r, repeat = job
    add = r.get("endpoint") == "completions"
    ts = []
    ids = None
    for _ in range(max(1, repeat)):
        t0 = time.perf_counter()
        ids = encode(r["text"], add)
        ts.append(time.perf_counter() - t0)
    med = statistics.median(ts)
    nb = len(r["text"].encode("utf-8", "surrogatepass"))
    out = {"med": med, "nb": nb, "ntok": len(ids), "ok": ids == r["ids"], "detail": None}
    if not out["ok"]:
        i = next((k for k, (x, y) in enumerate(zip(ids, r["ids"])) if x != y), min(len(ids), len(r["ids"])))
        ctx = lambda s: s[max(0, i - 3):i + 4]  # noqa: E731
        out["detail"] = (f"不一致 {r['id']}: 长度 {len(ids)} vs golden {len(r['ids'])}，第一个不同在第 {i} 个；"
                         f"本分词器 {ctx(ids)} / golden {ctx(r['ids'])}；对应文本 "
                         f"{_TOK.decode(ids[max(0, i - 3):i + 4])!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="export_corpus.py --format gold 的输出")
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--fastokens", action="store_true", help="先 fastokens.patch_transformers()（同 VLLM_USE_FASTOKENS=1）")
    ap.add_argument("--repeat", type=int, default=3, help="计时重复次数，取每条的中位数")
    ap.add_argument("--show", type=int, default=5, help="最多打印几条不一致的详情")
    ap.add_argument("--seg-cache", action="store_true", help="经分段缓存编码（按语料顺序单遍，缓存从空开始）")
    ap.add_argument("--seg-cache-module", help="经授权使用的外部分段缓存模块路径；--seg-cache 时必填")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行进程数。一致性结论不变；吞吐数字会因进程之间互相挤占而偏低，比较快慢时用 1")
    a = ap.parse_args()
    if a.seg_cache and not a.seg_cache_module:
        ap.error("--seg-cache requires an explicit --seg-cache-module (executes Python code)")
    rows = [json.loads(line) for line in open(a.corpus, encoding="utf-8") if line.strip()]
    if not rows:
        sys.exit("语料为空")
    if a.seg_cache:
        a.repeat = 1
        if a.jobs > 1:
            print("[check] --seg-cache 的缓存与语料顺序有关，改用单进程", file=sys.stderr)
            a.jobs = 1
    warm = [(r["text"], r.get("endpoint") == "completions") for r in rows[: min(20, len(rows))]]
    ok = bad = 0
    shown = 0
    per: list[float] = []
    nbytes = ntok = 0
    t_all = 0.0
    pg = pcap.Progress("check", len(rows), unit="rows")
    with pcap.Workers(a.jobs, load, (a.tokenizer, a.trust_remote_code, a.fastokens,
                                     a.seg_cache_module if a.seg_cache else None, warm), tag="check") as w:
        for res in w.imap(check_one, ((r, a.repeat) for r in rows)):
            pg.step()
            t_all += res["med"]
            nbytes += res["nb"]
            ntok += res["ntok"]
            per.append(res["med"] * 1e9 / max(res["nb"], 1))
            if res["ok"]:
                ok += 1
            else:
                bad += 1
                if shown < a.show:
                    shown += 1
                    print(res["detail"])
    per.sort()
    how = "单线程" if a.jobs <= 1 else f"{a.jobs} 个进程同时测、按单核耗时之和算，比单进程测的偏低"
    print(f"条数 {len(rows)}：一致 {ok}，不一致 {bad}")
    print(f"文本 {nbytes / 2**20:.2f} MB，token {ntok:,}，平均 {nbytes / max(ntok, 1):.3f} 字节/token")
    print(f"吞吐 {nbytes / 2**20 / t_all:.2f} MB/s（{how}，每条取 {a.repeat} 次的中位数）；"
          f"ns/字节 P50 {statistics.median(per):.1f}  P99 {per[min(len(per) - 1, int(0.99 * len(per)))]:.1f}")
    if _SC is not None:
        st = _SC.stats
        print(f"分段缓存：{st['segments']} 段，命中 {st['hit_segments']} 段；按字节命中 {st['hit_bytes'] / max(st['bytes'], 1):.1%}"
              f"（{st['hit_bytes'] / 2**20:.2f} / {st['bytes'] / 2**20:.2f} MB）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
