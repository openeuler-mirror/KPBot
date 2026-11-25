#!/usr/bin/env python3
"""合成 kptools-tpsplit-report 的仿真数据（before/after 两套：TTFT 表格 + 打点日志）。

用途：
  1. 本 skill 的演示/冒烟/回归测试（真实数据来自 TTFT breakdown 解析工具，环境级独立交付）；
  2. 作为表格/日志格式的可执行规格（字段名与口径见 references/stage-glossary.md）。

场景（story）—— TP32 跨机 Ray → TP8 单机闭环 + DP 扩容，P 池 64 卡不变：
  before（TP32×2DP）: sched_qwait 在 burst 下排队爆炸（收益主战场），fwd_pure 偏慢；
  after （TP8×8DP） : qwait 坍缩、fwd_pure 改善；kv_xfer/net_lb/shm_dq 轻微回退。

用法：
  python synth_samples.py --out <dir> [--scenario clean|quality-trap|tables-only]
                           [--seed 42] [--num-prompts 2000] [--force]
产出 <dir>/before_tp32ray/ 与 <dir>/after_tp8dp/，各含：
  ttft_table.csv, p_engine.log, proxy.log, config.json（tables-only 场景不含 .log，
  且只会清掉本工具自己上一轮生成的 .log，不会动来路不明的文件）
config.json 带 data_source: synthetic 标记，分析脚本会在报告里标「仿真数据」。
写盘防护：子目录名与真实采集约定相同，目标目录已有非空同名文件且非本工具
上一轮产物时会拒绝执行（防覆盖唯一一份真实回传数据）；--force 可强制覆盖，
每个被覆盖文件先备份为 *.bak。仿真数据请放在与真实采集目录分离的 demo 目录。
"""
import argparse
import json
import math
import os
import random
import shutil
import sys
import time

STAGES = ["net_lb", "fe_inputq", "fe_addreq", "sched_qwait", "sched_total",
          "fwd_pure", "draft", "kv_xfer", "shm_dq", "other"]

# 侧参数：qwait_mu 为 lognormal 的 mu（ms），fwd_tok 为每 token 前向 ms
SIDE_PARAMS = {
    "before": dict(qwait_mu=6.32, qwait_sigma=0.34, burst_pow=1.0, fwd_tok=0.0255,
                  net_lb=(4.2, 1.0), fe_inputq=(2.1, 0.9), fe_addreq=(1.1, 0.2),
                  sched_total=(18.4, 3.0), draft=(8.2, 1.5), kv_xfer=(2.4, 0.9),
                  shm_dq=(6.8, 1.8), other=(5.1, 1.0), inlen_mu=2048,
                  label="before-tp32ray", tp=32, dp=2, note="P=105+107 Ray TP32"),
    "after": dict(qwait_mu=5.42, qwait_sigma=0.30, burst_pow=0.45, fwd_tok=0.0176,
                  net_lb=(6.2, 1.2), fe_inputq=(1.9, 0.6), fe_addreq=(1.0, 0.2),
                  sched_total=(19.8, 3.5), draft=(8.5, 1.6), kv_xfer=(3.4, 1.3),
                  shm_dq=(7.4, 2.0), other=(4.4, 0.9), inlen_mu=2048,
                  label="after-tp8dp", tp=8, dp=8, note="P=8xTP8 单机闭环"),
}


def burst_weight(t, window):
    """两个高斯突发（burst）叠加，模拟流量突峰；qwait 对它敏感。"""
    t1, t2 = window * 0.35, window * 0.72
    return 1.0 + 2.2 * math.exp(-((t - t1) / 45.0) ** 2) + 1.8 * math.exp(-((t - t2) / 60.0) ** 2)


def gen_side(side, rng, num_prompts, window, corrupt=0.0, inlen_shift=1.0):
    p = SIDE_PARAMS[side]
    reqs = []
    t = 0.0
    while len(reqs) < num_prompts:
        # 泊松到达（均值 3.5 req/s）+ burst 时段实际进队更多
        t += rng.expovariate(3.5)
        if t > window:
            break
        input_len = max(64, int(rng.lognormvariate(math.log(p["inlen_mu"] * inlen_shift), 0.25)))
        w = burst_weight(t, window) ** p["burst_pow"]
        qwait = min(3000.0, rng.lognormvariate(p["qwait_mu"], p["qwait_sigma"]) * w)
        fwd = input_len * p["fwd_tok"] * rng.uniform(0.85, 1.15)
        row = {
            "ts": t, "request_id": None, "input_len": input_len,
            "net_lb": abs(rng.gauss(*p["net_lb"])),
            "fe_inputq": abs(rng.gauss(*p["fe_inputq"])) * (w ** 0.3),
            "fe_addreq": abs(rng.gauss(*p["fe_addreq"])),
            "sched_qwait": qwait,
            "sched_total": abs(rng.gauss(*p["sched_total"])),
            "fwd_pure": fwd,
            "draft": abs(rng.gauss(*p["draft"])),
            "kv_xfer": abs(rng.gauss(*p["kv_xfer"])),
            "shm_dq": abs(rng.gauss(*p["shm_dq"])),
            "other": abs(rng.gauss(*p["other"])),
        }
        row["ttft"] = sum(row[s] for s in STAGES)
        reqs.append(row)
    # 守恒违例注入（quality-trap 用）：ttft 字段额外加一段没归因的时间
    if corrupt > 0:
        n_bad = int(len(reqs) * corrupt)
        for row in reqs[-n_bad:]:
            row["ttft"] += rng.uniform(40.0, 120.0)
    return reqs


def fmt_ts(base_epoch, t, ms_sep=","):
    # 仿真时间戳：从 base_epoch 起的秒数 → 日志用 "10:15:32,114"（vLLM 风格），CSV 用 "."（避免逗号拆列）
    total = base_epoch + t
    ms = int(round((total % 1) * 1000))
    secs = int(total) + ms // 1000   # 四舍五入到 1000ms 时进位到秒，避免产出 ",1000"（4 位非法）
    ms %= 1000
    tm = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(secs))
    return "%s%s%03d" % (tm, ms_sep, ms)


def _dir_is_own(out_dir):
    """目标目录是否为本工具上一轮产物（以 config.json 的 data_source=synthetic 判定）。

    必须在本次写盘前调用：一旦本次覆盖了 config.json，判定就失效了。
    """
    cfgp = os.path.join(out_dir, "config.json")
    if not os.path.isfile(cfgp):
        return False
    try:
        with open(cfgp, encoding="utf-8") as f:
            return "synth" in str(json.load(f).get("data_source", "")).lower()
    except Exception:
        return False


def guard_targets(out_dir, force, own, with_logs):
    """写盘防护：目标目录已有非空同名文件时拒绝执行，防止覆盖真实采集数据。

    - 本工具上一轮产物（own=True，config 带 data_source=synthetic）可安全覆盖；
    - --force 时先把每个将被覆盖的文件备份为 *.bak 再放行；
    - 其余情况拒绝并提示更换 --out 目录（真实回传数据往往只有一份）。
    返回 (allowed, note)；allowed=False 时 note 为拒绝原因。
    """
    written = ["ttft_table.csv", "config.json"] + \
        (["p_engine.log", "proxy.log"] if with_logs else [])
    existing = [n for n in written
                if os.path.isfile(os.path.join(out_dir, n))
                and os.path.getsize(os.path.join(out_dir, n)) > 0]
    if not existing:
        return True, ""
    if own:
        return True, "目标目录是本工具上一轮产物，安全覆盖"
    if not force:
        return False, ("目标目录已存在非空同名文件且非本工具生成：%s\n"
                       "真实采集数据被覆盖后不可恢复——请更换 --out 目录；"
                       "确认要覆盖请加 --force（每个被覆盖文件先备份为 *.bak）"
                       % ", ".join(existing))
    for n in existing:
        shutil.copy2(os.path.join(out_dir, n), os.path.join(out_dir, n + ".bak"))
    return True, "--force：已备份 %s 为 *.bak" % ", ".join(existing)


def write_side(out_dir, side, reqs, window, base_epoch, with_logs=True, force=False):
    os.makedirs(out_dir, exist_ok=True)
    own = _dir_is_own(out_dir)   # 写盘前判定，清理 .log 也以它为准
    allowed, note = guard_targets(out_dir, force, own, with_logs)
    if not allowed:
        sys.exit(note)
    if note:
        print("note: %s %s" % (out_dir, note))
    p = SIDE_PARAMS[side]
    for i, row in enumerate(reqs):
        row["request_id"] = "req-%06d" % i
    # ---- ttft_table.csv（TTFT breakdown 解析工具的口径，见 glossary 第 3 节）----
    with open(os.path.join(out_dir, "ttft_table.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("ts,request_id,input_len,ttft_ms," + ",".join(s + "_ms" for s in STAGES) + "\n")
        for row in reqs:
            vals = [fmt_ts(base_epoch, row["ts"], "."), row["request_id"], row["input_len"],
                    "%.2f" % row["ttft"]] + ["%.2f" % row[s] for s in STAGES]
            f.write(",".join(str(v) for v in vals) + "\n")
    # ---- config.json ----
    cfg = {
        "label": p["label"], "tp_size": p["tp"], "dp_size": p["dp"], "pp": 1,
        "model": "GLM-5.2-W4A8", "p_total_cards": 64,
        "deploy_note": p["note"], "num_requests": len(reqs),
        "window_seconds": round(window, 1),
        # 显式来源标记：分析脚本据此在 md/html/json 三处标「仿真数据」，防止演示报告被当现场结果
        "data_source": "synthetic",
        "bench": {"distribution": "bursty-poisson", "mean_qps": 3.5,
                  "output_len": 128, "note": "synthesized by synth_samples.py"},
    }
    with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    if not with_logs:
        # 同目录复用（先 clean 再 tables-only）时清掉上一轮留下的 .log，
        # 防止旧日志（复用的 request_id 相同）被当成新表格的证据；
        # 只清本工具自己生成的（写盘前的 own 判定），来路不明的 .log 一律不动
        stale = [fn for fn in os.listdir(out_dir) if fn.lower().endswith(".log")]
        if stale:
            if own:
                for fn in stale:
                    os.remove(os.path.join(out_dir, fn))
                print("note: removed stale logs from previous synth run in %s: %s"
                      % (out_dir, ", ".join(stale)))
            else:
                print("note: %s 内存在非本工具生成的 .log，未清理（覆盖/删除真实日志不可逆）；"
                      "仿真数据建议使用独立 demo 目录" % out_dir)
        return
    # ---- 日志行（与表格共享 request_id / 数值，证据链可对回）----
    eng_lines, proxy_lines = [], []
    for row in reqs:
        rid, ts_s = row["request_id"], fmt_ts(base_epoch, row["ts"])
        t0 = row["ts"] - row["ttft"] / 1000.0  # 请求进入 proxy 的时刻
        stg = " ".join("%s=%.2f" % (s, row[s]) for s in STAGES)
        eng_lines.append((row["ts"], "%s INFO  [TTFT][BREAKDOWN] v3 req=%s input_len=%d ttft=%.2f %s"
                          % (ts_s, rid, row["input_len"], row["ttft"], stg)))
        proxy_lines.append((t0, "%s INFO  [LB_DISPATCH] req=%s dst=dp:%d/port:%d cost_ms=%.2f"
                            % (fmt_ts(base_epoch, t0), rid, row["input_len"] % p["dp"],
                               9900 + row["input_len"] % p["dp"], row["net_lb"])))
        # 排序键与打印时间戳同一基准（ts − draft）：行序与时间戳一致，便于按时间抽行
        eng_lines.append((row["ts"] - row["draft"] / 1000.0,
                          "%s INFO  [TTFT_DRAFT] req=%s draft_tokens=%d accepted=%d cost_ms=%.2f"
                          % (fmt_ts(base_epoch, row["ts"] - row["draft"] / 1000.0), rid,
                             rng_drand(3, 4), rng_drand(1, 3), row["draft"])))
        eng_lines.append((row["ts"] - row["kv_xfer"] / 1000.0,
                          "%s INFO  [TTFT_KVXFER][put] req=%s pool=p:%d blocks=%d cost_ms=%.2f"
                          % (fmt_ts(base_epoch, row["ts"] - row["kv_xfer"] / 1000.0), rid,
                             i % p["dp"], max(16, row["input_len"] // 32), row["kv_xfer"])))
        eng_lines.append((row["ts"] - row["kv_xfer"] / 2000.0,
                          "%s INFO  start_load_kv cost time: %.2fms req=%s"
                          % (fmt_ts(base_epoch, row["ts"] - row["kv_xfer"] / 2000.0),
                             row["kv_xfer"] * 0.9, rid)))
    # batch 级 SHM 行（无 req 字段，按 ~0.4s 一条铺满窗口）
    t = 0.0
    while t < window:
        eng_lines.append((t, "%s INFO  [SHM_DEQUEUE_BATCH] dq=%d objs=%d cost_ms=%.2f"
                          % (fmt_ts(base_epoch, t), int(4 + 2 * burst_weight(t, window)) ,
                             int(1 + 2 * random.random()), abs(random.gauss(7.0, 2.0)))))
        t += 0.35 + random.random() * 0.2
    # 噪声行（真实感：启动/健康检查）
    eng_lines[:0] = [(0.0, "%s INFO  Application startup complete. EngineArgs(tp=%d, dp=%d)"
                     % (fmt_ts(base_epoch, 0.0), p["tp"], p["dp"]))]
    eng_lines.append((window, "%s INFO  [HealthCheck] engine alive, processed=%d"
                      % (fmt_ts(base_epoch, window), len(reqs))))
    eng_lines.sort(key=lambda x: x[0])
    proxy_lines.sort(key=lambda x: x[0])
    with open(os.path.join(out_dir, "p_engine.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(l for _, l in eng_lines) + "\n")
    with open(os.path.join(out_dir, "proxy.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(l for _, l in proxy_lines) + "\n")


def rng_drand(a, b):
    return a + int(random.random() * (b - a + 1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="输出根目录（建议与真实采集目录分离的 demo 目录）")
    ap.add_argument("--scenario", default="clean",
                    choices=["clean", "quality-trap", "tables-only"])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--num-prompts", type=int, default=2000)
    ap.add_argument("--force", action="store_true",
                    help="目标目录已有非空同名文件时仍覆盖（每个被覆盖文件先备份为 *.bak）；"
                         "默认拒绝执行，防止覆盖真实采集数据")
    args = ap.parse_args()
    random.seed(args.seed)
    base_epoch = 1763858400  # 2026-11-23 10:00:00 UTC

    window_b, window_a = 600.0, 600.0
    n_after = args.num_prompts
    corrupt, inlen_shift = 0.0, 1.0
    if args.scenario == "quality-trap":
        window_a, n_after = 180.0, 320          # 短窗 + 低样本
        corrupt, inlen_shift = 0.08, 0.6        # 守恒违例 + input_len 分布偏移

    rng_b = random.Random(args.seed)
    rng_a = random.Random(args.seed + 1)
    before = gen_side("before", rng_b, args.num_prompts, window_b)
    after = gen_side("after", rng_a, n_after, window_a, corrupt=corrupt, inlen_shift=inlen_shift)

    write_side(os.path.join(args.out, "before_tp32ray"), "before", before, window_b,
               base_epoch, with_logs=args.scenario != "tables-only", force=args.force)
    write_side(os.path.join(args.out, "after_tp8dp"), "after", after, window_a,
               base_epoch + 4000, with_logs=args.scenario != "tables-only", force=args.force)
    print("scenario=%s  before=%d reqs  after=%d reqs  -> %s"
          % (args.scenario, len(before), len(after), os.path.abspath(args.out)))


if __name__ == "__main__":
    main()
