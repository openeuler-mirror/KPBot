#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_regression.py — analyze_ttft.py 回归测试（合成 v3 日志全断言）。

流程：gen_synth_logs.py 生成合成日志 + expectations.json →
      analyze_ttft.py 解析出表（多 P merge / 多 P 分列两种模式）→
      读回 xlsx 逐行断言（回填 / 信封 / 守恒 / LB / 对账 / burst / 冷启动）。

用法：python test_regression.py [--keep]
退出码：0 = 全部通过；1 = 存在失败断言。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYZE = os.path.join(HERE, "analyze_ttft.py")
GEN = os.path.join(HERE, "gen_synth_logs.py")

MAIN_COMPS = ["tokenize_render", "dispatch", "add_req", "mm_hash", "queue_wait",
              "wait_exec", "load_kv", "forward", "mtp", "sample", "save_kv",
              "step_other", "out_delivery"]
SHEETS = ["总览", "Burst各环节", "逐请求明细", "机器对比delta", "D节点Decode",
          "P节点KV传输", "LB分发", "回传对账"]
TOL = 0.001

failures = []
n_checks = 0


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    if not cond:
        failures.append(f"{name}: {detail}")
    return cond


def read_detail(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    check("sheet 集合", wb.sheetnames == SHEETS, f"实际 {wb.sheetnames}")
    ws = wb["逐请求明细"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    out = [dict(zip(header, r)) for r in rows[1:]]
    wb.close()
    return out, header


def recon_rows(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb["回传对账"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    out = [dict(zip(header, r)) for r in rows[1:]]
    wb.close()
    return out


def close(a, b, tol=TOL):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def check_csv(workdir, merge):
    """CSV 契约断言（下游 kptools-tpsplit-report 的 ttft_table.csv）。"""
    import csv as csvmod
    synth = os.path.join(workdir, "synth")
    csv_path = os.path.join(workdir, "merge" if merge else "nomerge", "ttft_table.csv")
    tag = "merge" if merge else "nomerge"
    with open(csv_path, encoding="utf-8") as fh:
        rdr = list(csvmod.reader(fh))
    header = rdr[0]
    expect_header = ["ts", "request_id", "input_len", "ttft_ms", "net_lb_ms",
                     "fe_inputq_ms", "fe_addreq_ms", "sched_qwait_ms",
                     "sched_total_ms", "fwd_pure_ms", "draft_ms", "kv_xfer_ms",
                     "shm_dq_ms", "other_ms"]
    check(f"[{tag}] CSV 表头契约", header == expect_header, f"{header}")
    check(f"[{tag}] CSV 行数", len(rdr) - 1 == 80, f"{len(rdr) - 1}")
    n_cons = 0
    for row in rdr[1:]:
        d = dict(zip(header, row))
        ttft = float(d["ttft_ms"])
        engine9 = (float(d["fe_inputq_ms"]) + float(d["fe_addreq_ms"])
                   + float(d["sched_qwait_ms"]) + float(d["sched_total_ms"])
                   + float(d["fwd_pure_ms"]) + float(d["draft_ms"])
                   + float(d["kv_xfer_ms"]) + float(d["shm_dq_ms"])
                   + float(d["other_ms"]))
        # 引擎内 9 阶段 + other == ttft（精确守恒；net_lb 在引擎 ttft 之外）
        if abs(engine9 - ttft) <= 0.02:
            n_cons += 1
    check(f"[{tag}] CSV 引擎守恒（Σ9+other==ttft）", n_cons == 80,
          f"{n_cons}/80")
    # kv_xfer 回填口径抽查：等于 load_kv+save_kv（TP-max 回填后）
    with open(csv_path, encoding="utf-8") as fh:
        rows_csv = list(csvmod.DictReader(fh))
    kv_vals = [float(r["kv_xfer_ms"]) for r in rows_csv]
    check(f"[{tag}] CSV kv_xfer 非哨兵（回填生效）",
          abs(sum(kv_vals) / len(kv_vals) - 60.0) < 3.0,
          f"mean={sum(kv_vals) / len(kv_vals):.1f}（预期≈load40+save20）")
    print(f"[{tag}] CSV: {len(rows_csv)} 行，引擎守恒 {n_cons}/80，"
          f"kv_xfer mean={sum(kv_vals) / len(kv_vals):.1f}ms")


def run_case(workdir, merge):
    d = os.path.join(workdir, "merge" if merge else "nomerge")
    os.makedirs(d, exist_ok=True)
    synth = os.path.join(workdir, "synth")
    out = os.path.join(d, "TTFT_test.xlsx")
    csv_out = os.path.join(d, "ttft_table.csv")
    cmd = [sys.executable, ANALYZE,
           f"102:{os.path.join(synth, 'P102_prefill.log')}",
           f"103:{os.path.join(synth, 'P103_prefill.log')}",
           "-o", out, "--burst-gap", "300", "--csv-out", csv_out,
           "--decode", f"D:{os.path.join(synth, 'D103_decode.log')}",
           "--kvxfer", f"102:{os.path.join(synth, 'P102_prefill.log')}",
           "--kvxfer", f"103:{os.path.join(synth, 'P103_prefill.log')}",
           "--lb", f"PROXY:{os.path.join(synth, 'load_balance.log')}",
           "--detailed", f"BENCH:{os.path.join(synth, 'manual_synth_dumpjsonl.json')}"]
    if merge:
        cmd.append("--merge-p")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check(f"[{'merge' if merge else 'nomerge'}] analyzer 退出码 0",
          r.returncode == 0, r.stderr[-500:] if r.stderr else "")

    exp = json.load(open(os.path.join(synth, "expectations.json"),
                         encoding="utf-8"))
    det, header = read_detail(out)
    tag = "merge" if merge else "nomerge"

    # 行数 / burst
    check(f"[{tag}] 明细行数", len(det) == exp["n_req"],
          f"{len(det)} != {exp['n_req']}")
    bcount = {}
    for r in det:
        bcount[r["Burst"]] = bcount.get(r["Burst"], 0) + 1
    check(f"[{tag}] burst 计数", bcount == {int(k): v for k, v in
                                            exp["bursts"].items()}, str(bcount))

    # IP 列
    check(f"[{tag}] IP 列", ("IP" in header) == merge, f"header={header}")

    exp_idx = {(e["machine"], e["req"]): e for e in exp["rows"]}
    det_idx = {(str(r["机器"]), str(r["req"])): r for r in det}
    check(f"[{tag}] (机器,req) 键覆盖", set(exp_idx) == set(det_idx),
          f"缺失 {set(exp_idx) - set(det_idx) & set(exp_idx)}")

    n_env_ok = n_cons_ok = n_clamp = 0
    for k, e in exp_idx.items():
        r = det_idx.get(k)
        if r is None:
            continue
        p = f"[{tag}]{k}"
        # 回填（TP-max）
        check(p + " load_kv", close(r["load_kv"], e["load_kv"]),
              f"{r['load_kv']} != {e['load_kv']}")
        check(p + " mtp", close(r["mtp"], e["mtp"]),
              f"{r['mtp']} != {e['mtp']}")
        check(p + " save_kv", close(r["save_kv"], e["save_kv"]),
              f"{r['save_kv']} != {e['save_kv']}")
        check(p + " ttft", close(r["ttft"], e["ttft"]))
        # 信封
        env = (close(r["step_other"], float(r["dequeue"]) + float(r["return"]))
               and close(float(r["dequeue"]),
                         float(r["dequeue_main"]) + float(r["dequeue_draft"])
                         + float(r["dequeue_take_draft"]) + float(r["dequeue_aux"])))
        n_env_ok += env
        check(p + " step_other 信封", env,
              f"step_other={r['step_other']} deq={r['dequeue']} ret={r['return']}")
        # 守恒
        s13 = sum(float(r[c]) for c in MAIN_COMPS)
        cerr = float(r["conserv_err"])
        check(p + " conserv_err>=0", cerr >= -TOL, f"{cerr}")
        check(p + " conserv_err 重算一致",
              close(cerr, s13 + float(r["residual"]) - float(r["ttft"]), 0.002),
              f"sheet={cerr} 重算={s13 + float(r['residual']) - float(r['ttft'])}")
        if e["clamp"]:
            n_clamp += 1
            check(p + " 截断 conserv_err>0", cerr > 0.1, f"{cerr}")
        else:
            n_cons_ok += close(cerr, 0.0, 0.005)
            check(p + " conserv_err==0", close(cerr, 0.0, 0.005), f"{cerr}")
            # 主列加总不变（回填增量被 step_other 等量吸收）
            check(p + " Σ主列不变", close(s13, e["sum13_log"], 0.01),
                  f"{s13} != {e['sum13_log']}")
        # 冷启动 / 预热
        check(p + " cold_step", int(r["cold_step"]) == e["cold_step"],
              f"{r['cold_step']} != {e['cold_step']}")
        check(p + " fwd_tokens", close(r["fwd_tokens"], e["fwd_tokens"], 0.0),
              f"{r['fwd_tokens']} != {e['fwd_tokens']}")
        # LB 回填
        check(p + " lb_src=proxy", r["lb_src"] == "proxy", f"{r['lb_src']}")
        check(p + " lb_dispatch=c2p+disp",
              close(r["lb_dispatch"], 1.2 + 2.8), f"{r['lb_dispatch']}")

    # 对账
    rec = recon_rows(out)
    joined = [x for x in rec if x["joined"] == "lb"]
    check(f"[{tag}] 对账 joined 数", len(joined) == exp["recon"]["joined"],
          f"{len(joined)} != {exp['recon']['joined']}")
    if joined:
        rets = [float(x["return_ms"]) for x in joined]
        check(f"[{tag}] return ≈ 设计值",
              all(abs(x - exp["recon"]["return"]) <= TOL for x in rets),
              f"min={min(rets)} max={max(rets)}")

    print(f"[{tag}] 行数 {len(det)}，信封 OK {n_env_ok}，守恒 OK {n_cons_ok}，"
          f"截断行 {n_clamp}，对账 {len(joined)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="ttft_regr_")
    try:
        synth = os.path.join(workdir, "synth")
        r = subprocess.run([sys.executable, GEN, "--out-dir", synth,
                            "--n-req", "80", "--seed", "42"],
                           capture_output=True, text=True, encoding="utf-8")
        check("生成器退出码 0", r.returncode == 0, r.stderr[-300:])
        run_case(workdir, merge=True)
        run_case(workdir, merge=False)
        check_csv(workdir, merge=True)
        check_csv(workdir, merge=False)

        print("-" * 60)
        if failures:
            print(f"FAILED：{len(failures)}/{n_checks} 项断言未通过")
            for f in failures[:30]:
                print("  ✗ " + f)
            return 1
        print(f"PASS：全部 {n_checks} 项断言通过（回填/信封/守恒/LB/对账/burst/冷启动）")
        return 0
    finally:
        if args.keep:
            print(f"(保留工作目录 {workdir})")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
