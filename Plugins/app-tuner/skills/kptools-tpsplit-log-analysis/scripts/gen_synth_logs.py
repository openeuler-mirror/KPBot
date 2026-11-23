#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_synth_logs.py — 合成 v3 口径 TTFT 打点日志（回归/演示用）。

生成一套自洽的四份输入 + expectations.json（ground truth）：
  P102_prefill.log / P103_prefill.log   P 节点（breakdown + kvxfer + pool + shm + draft）
  D103_decode.log                        D 节点 [TTFT_DECODE]
  load_balance.log                       proxy [LB_DISPATCH]
  manual_synth_dumpjsonl.json            压测端 --save-detailed

设计要点（与 analyze_ttft.py 的回填口径互锁）：
  * engine 侧 load_kv / save_kv 为哨兵 0（defer_finalize 设计），真实值在
    pool_worker / [TTFT_KVXFER][put] 日志里，由解析层 TP-max 回填；
  * mtp 日志缺列（→0），由 [TTFT_DRAFT] TP-max 回填；
  * 正常行 step_other 足够吸收回填增量（conserv_err==0），
    另造 2 行 step_other 不足 → conserv_err>0（截断路径）；
  * 2 个冷启动行（add_req<1ms 且 forward>2×中位数）+ fwd_tokens=40（未预热）。

用法：python gen_synth_logs.py --out-dir <dir> [--n-req 80] [--seed 42]
"""

import argparse
import json
import os
import random

MACHINES = ["102", "103"]
REQS_PER_BATCH = 4
TP = 8
CLIENT_SEND_BASE = 1732000000000  # ms epoch


def synth(out_dir, n_req, seed):
    rnd = random.Random(seed)
    p_machines = {m: [] for m in MACHINES}
    bench, lb, expectations = [], [], {"n_req": n_req, "rows": []}
    burst_bounds = {}
    # 两段 burst：ts 100..~160 与 2000..~2060（间隔 > 300s）
    n_b1 = n_req // 2
    for i in range(n_req):
        burst = 1 if i < n_b1 else 2
        machine = MACHINES[i % len(MACHINES)]
        ts = 100.0 + i * 1.5 if burst == 1 else 2000.0 + (i - n_b1) * 1.5
        burst_bounds.setdefault(burst, []).append(i)

    # batch 划分：按机器各自由连续请求组成（batch 与机器严格对齐，不跨机）
    machine_reqs = {m: [i for i in range(n_req) if MACHINES[i % len(MACHINES)] == m]
                    for m in MACHINES}
    batches = []
    for m in MACHINES:
        for j in range(0, len(machine_reqs[m]), REQS_PER_BATCH):
            batches.append({
                "machine": m,
                "batch": f"{rnd.getrandbits(32):08x}",
                "idxs": machine_reqs[m][j:j + REQS_PER_BATCH],
                # pool / draft 是批级 TP-max，批内请求共享
                "pool_max": 40.0 + rnd.uniform(-2, 2),
                "draft_max": 8.0 + rnd.uniform(-1, 1)})
    req_to_batch = {i: b for b in batches for i in b["idxs"]}

    cold_idx = {n_req // 3, n_req // 3 + 1}
    fwd_median_approx = 250.0

    for i in range(n_req):
        burst = 1 if i < n_b1 else 2
        machine = MACHINES[i % len(MACHINES)]
        ts = 100.0 + i * 1.5 if burst == 1 else 2000.0 + (i - n_b1) * 1.5
        req = f"req_{i:04d}"
        b = req_to_batch[i]
        b["reqs"] = b.get("reqs", [])
        if req not in b["reqs"]:
            b["reqs"].append(req)
        cold = i in cold_idx

        # ---- 分量（ms）
        tokenize = 2.0 + rnd.uniform(-0.5, 0.5)
        dispatch = 1.5 + rnd.uniform(-0.3, 0.3)
        add_req = 0.3 if cold else 3.0 + rnd.uniform(-0.5, 0.5)
        mm_hash = 0.5 + rnd.uniform(-0.2, 0.2)
        queue_wait = 8.0 + rnd.uniform(-2, 3)
        wait_exec = 12.0 + rnd.uniform(-3, 4)
        load_kv_log = 0.0            # 哨兵：真实值由 pool_worker 回填
        forward = (3.0 * fwd_median_approx) if cold else 250.0 + rnd.uniform(-30, 30)
        sample = 0.6 + rnd.uniform(-0.1, 0.1)
        save_kv_log = 0.0            # 哨兵：真实值由 [put] 回填
        step_other_log = 5.0 if cold else 80.0 + rnd.uniform(-10, 10)
        out_delivery = 2.0 + rnd.uniform(-0.5, 0.5)
        residual = 0.0
        ttft = (tokenize + dispatch + add_req + mm_hash + queue_wait
                + wait_exec + load_kv_log + forward + sample + save_kv_log
                + step_other_log + out_delivery + residual)

        # ---- 回填真实值（TP-max）：pool/draft 批级共享，put 逐请求
        pool_max = b["pool_max"]
        draft_max = b["draft_max"]
        put_total = 20.0 + rnd.uniform(-1, 1)
        # dequeue（SHM TP-max 累计，1 步）：main/draft/take_draft/aux
        shm_waits = {"main": 3.0, "draft": 1.0, "take_draft": 0.5, "aux": 1.5}
        dequeue_raw = sum(shm_waits.values())

        p_machines[machine].append({
            "req": req, "batch": b["batch"], "ts": ts, "ttft": ttft,
            "tokenize_render": tokenize, "dispatch": dispatch, "add_req": add_req,
            "mm_hash": mm_hash, "queue_wait": queue_wait, "wait_exec": wait_exec,
            "load_kv": load_kv_log, "forward": forward, "sample": sample,
            "save_kv": save_kv_log, "step_other": step_other_log,
            "out_delivery": out_delivery, "residual": residual,
            "pool_max": pool_max, "draft_max": draft_max,
            "put_total": put_total, "cold": cold, "burst": burst,
            "fwd_tokens": 40 if cold else 12,
        })

        # ---- proxy / 压测端
        client_send = CLIENT_SEND_BASE + int(ts * 1000)
        c2p = 1.2
        disp = 2.8
        first_token = ttft + disp + 10.0
        ret = 0.5
        client_ttft = c2p + first_token + ret
        lb.append({"req": req, "wall_recv": client_send + 1,
                   "client_send": client_send, "client2proxy_ms": c2p,
                   "dispatch_ms": disp, "wait_p_ms": 15.0,
                   "first_token_ms": first_token,
                   "prefiller": machine, "decoder": "D103"})
        bench.append({"req": req, "client_send_ts": client_send,
                      "first_token_ts": client_send + client_ttft,
                      "client_ttft": client_ttft})

        # ---- expectations
        delta = (pool_max - load_kv_log) + (draft_max - 0.0) + (put_total - save_kv_log)
        step_other_exp = max(0.0, step_other_log - delta)
        deq_exp = min(dequeue_raw, step_other_exp)
        expectations["rows"].append({
            "machine": machine, "req": req, "burst": burst, "ttft": ttft,
            "load_kv": pool_max, "mtp": draft_max, "save_kv": put_total,
            "step_other": step_other_exp, "dequeue": deq_exp,
            "return": step_other_exp - deq_exp,
            "clamp": step_other_exp == 0.0,
            "conserv_err": (delta - step_other_log) if delta > step_other_log else 0.0,
            "cold_step": 1 if cold else 0, "fwd_tokens": 40 if cold else 12,
            "sum13_log": (tokenize + dispatch + add_req + mm_hash + queue_wait
                          + wait_exec + load_kv_log + forward + sample
                          + save_kv_log + step_other_log + out_delivery),
        })

    expectations["bursts"] = {str(b): len(v) for b, v in burst_bounds.items()}
    expectations["recon"] = {"joined": n_req, "return": 0.5}

    # ---- 写 P 日志（breakdown + 辅助行）
    lines_by_machine = {m: [] for m in MACHINES}
    for m in MACHINES:
        for r in p_machines[m]:
            info = (f"lb_dispatch=5.0 hash=1.0 ext_lookup=2.0 alloc=1.0 "
                    f"overhead=0.5 sched_total=4.0 update=1.0 emit=0.5 "
                    f"step_exec=250.0 add_req_iq=1.0 add_req_proc=2.0 "
                    f"preempt=0 fwd_tokens={r['fwd_tokens']} fwd_steps=1")
            lines_by_machine[m].append(
                f"[TTFT][BREAKDOWN] req={r['req']} ts={r['ts']:.3f} "
                f"batch={r['batch']} ttft={r['ttft']:.3f} "
                f"tokenize_render={r['tokenize_render']:.3f} "
                f"dispatch={r['dispatch']:.3f} add_req={r['add_req']:.3f} "
                f"mm_hash={r['mm_hash']:.3f} queue_wait={r['queue_wait']:.3f} "
                f"wait_exec={r['wait_exec']:.3f} load_kv={r['load_kv']:.3f} "
                f"forward={r['forward']:.3f} sample={r['sample']:.3f} "
                f"save_kv={r['save_kv']:.3f} step_other={r['step_other']:.3f} "
                f"out_delivery={r['out_delivery']:.3f} residual={r['residual']:.3f} "
                + info)
        # [TTFT_KVXFER][put]（每 req 一条 20ms 真实值 + 一条干扰小值）
        for r in p_machines[m]:
            lines_by_machine[m].append(
                f"[TTFT_KVXFER][put] req={r['req']} total={r['put_total']:.3f} "
                f"build_key=0.3 lookup=0.2 prepare=1.0 sync=0.5 put=18.0 finalize=0.2")
            lines_by_machine[m].append(
                f"[TTFT_KVXFER][put] req={r['req']} total=0.1 build_key=0.0 "
                f"lookup=0.0 prepare=0.1 sync=0.0 put=0.0")
        # pool_worker start_load_kv 逐 TP（TP0 = max）
        for b in [x for x in batches if x["machine"] == m]:
            for tp in range(TP):
                cost = (b["pool_max"] if tp == 0
                        else b["pool_max"] - rnd.uniform(5, 15))
                lines_by_machine[m].append(
                    f"[pool_worker] tp={tp}/{TP} start_load_kv batch={b['batch']} "
                    f"reqs={len(b['reqs'])} cost {cost:.3f}ms")
        # [TTFT_DRAFT] 逐 TP
        for b in [x for x in batches if x["machine"] == m]:
            for tp in range(TP):
                dw = b["draft_max"] if tp == 0 else b["draft_max"] - rnd.uniform(1, 4)
                lines_by_machine[m].append(
                    f"[TTFT_DRAFT] batch={b['batch']} draft_wall={dw:.3f} "
                    f"reqs={len(b['reqs'])} tp={tp}/{TP}")
        # [SHM_DEQUEUE_BATCH] 逐 TP（TP0 = max wait；sub 四类）
        for b in [x for x in batches if x["machine"] == m]:
            for sub, wmax in {"main": 3.0, "draft": 1.0, "take_draft": 0.5,
                              "aux": 1.5}.items():
                for tp in range(TP):
                    w = wmax if tp == 0 else wmax - rnd.uniform(0.1, 0.4)
                    lines_by_machine[m].append(
                        f"[SHM_DEQUEUE_BATCH] batch={b['batch']}-1 tp={tp}/{TP} "
                        f"wait={w:.3f} read=0.2 nreq={len(b['reqs'])} "
                        f"reqs=na sub={sub}")
        # 少量 [SHM_DEQUEUE] 聚合行 + 噪声行
        for k in range(5):
            lines_by_machine[m].append(
                f"[SHM_DEQUEUE] t={1000.0 + k} wait={30.0 + k} read=0.3 "
                f"mono=1732000000000 tid=99 total=99.0 count=10 avg=9.9")
        lines_by_machine[m].append("INFO 2026-11-23 engine heartbeat (noise line)")
        rnd.shuffle(lines_by_machine[m])

    # ---- D 日志
    dlines = []
    for s in range(30):
        dlines.append(
            f"[TTFT_DECODE][EngineCore] ts={2000.5 + s * 0.5:.3f} "
            f"decode_forward={12.0 + rnd.uniform(-1, 2):.3f} load=2.0 "
            f"save=1.0 sample=0.5 num_total=0 num_gen=16 "
            f"sched_total=5.0 decode_only=9.0")

    # ---- proxy / bench
    lblines = [f"[LB_DISPATCH] req={l['req']} wall_recv={l['wall_recv']} "
               f"client_send={l['client_send']} "
               f"client2proxy_ms={l['client2proxy_ms']} "
               f"dispatch_ms={l['dispatch_ms']} wait_p_ms={l['wait_p_ms']} "
               f"first_token_ms={l['first_token_ms']:.3f} "
               f"prefiller={l['prefiller']} decoder={l['decoder']}"
               for l in lb]

    os.makedirs(out_dir, exist_ok=True)
    for m in MACHINES:
        with open(os.path.join(out_dir, f"P{m}_prefill.log"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(lines_by_machine[m]) + "\n")
    with open(os.path.join(out_dir, "D103_decode.log"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(dlines) + "\n")
    with open(os.path.join(out_dir, "load_balance.log"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(lblines) + "\n")
    with open(os.path.join(out_dir, "manual_synth_dumpjsonl.json"), "w",
              encoding="utf-8") as fh:
        for b in bench:
            fh.write(json.dumps(b) + "\n")
    with open(os.path.join(out_dir, "expectations.json"), "w",
              encoding="utf-8") as fh:
        json.dump(expectations, fh, indent=1)

    print(f"合成日志已生成 → {out_dir}")
    print(f"  请求数 {n_req}（burst: {expectations['bursts']}），"
          f"P 机器 {MACHINES}，TP={TP}")
    return expectations


def main():
    ap = argparse.ArgumentParser(description="合成 v3 TTFT 打点日志 + ground truth")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--n-req", type=int, default=80)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    synth(args.out_dir, args.n_req, args.seed)


if __name__ == "__main__":
    main()
