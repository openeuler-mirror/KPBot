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
    """CSV 契约断言（references/ttft_table_contract.md）。

    非 tautology 设计：不依赖 other 的定义式自证，而是
    ① 逐阶段与 ground truth（expectations）对值（映射改坏即 FAIL）；
    ② other 与 max(0, ttft−Σ8_ground_truth) 对值，负值落盘 = FAIL；
    ③ 守恒差值：正常行 ≈0；截断行（conserv_err>0）= 溢出量（下游质量信号）。
    """
    import csv as csvmod
    synth = os.path.join(workdir, "synth")
    csv_path = os.path.join(workdir, "merge" if merge else "nomerge", "ttft_table.csv")
    tag = "merge" if merge else "nomerge"
    exp = json.load(open(os.path.join(synth, "expectations.json"),
                         encoding="utf-8"))
    exp_idx = {(e["machine"], e["req"]): e for e in exp["rows"]}
    # machine 归并口径：merge 与 nomerge 的行集相同（60+20 台各自 30/50 请求）
    with open(csv_path, encoding="utf-8") as fh:
        rdr = list(csvmod.reader(fh))
    header = rdr[0]
    expect_header = ["ts", "request_id", "input_len", "ttft_ms", "net_lb_ms",
                     "fe_inputq_ms", "fe_addreq_ms", "sched_qwait_ms",
                     "sched_total_ms", "fwd_pure_ms", "draft_ms", "kv_xfer_ms",
                     "shm_dq_ms", "other_ms"]
    check(f"[{tag}] CSV 表头契约", header == expect_header, f"{header}")
    csv_rows = [dict(zip(header, r)) for r in rdr[1:]]
    check(f"[{tag}] CSV 行数", len(csv_rows) == 80, f"{len(csv_rows)}")

    stage_keys = ["net_lb", "fe_inputq", "fe_addreq", "sched_qwait",
                  "sched_total", "fwd_pure", "draft", "kv_xfer", "shm_dq"]
    n_match = n_neg = 0
    for d in csv_rows:
        req = d["request_id"]
        ttft = float(d["ttft_ms"])
        # ① ground truth 对值（同 req 名全局唯一）
        e = next(v for k, v in exp_idx.items() if k[1] == req)
        ok = True
        for st in stage_keys:
            if abs(float(d[st + "_ms"]) - e[st]) > 0.02:
                check(f"[{tag}] CSV {st} 对值 ({req})",
                      False, f"csv={d[st + '_ms']} exp={e[st]:.3f}")
                ok = False
        if ok:
            n_match += 1
        # ttft_ms = net_lb + 引擎侧 ttft（同边界守恒）
        check(f"[{tag}] CSV ttft_ms 含 net_lb ({req})",
              abs(ttft - (e["ttft"] + e["net_lb"])) <= 0.02,
              f"csv={ttft} exp={e['ttft'] + e['net_lb']:.3f}")
        # ② other：正常行 = 引擎侧 other（ttft − Σ8，非负）；截断行钳 0，
        #    溢出量转成守恒差值（供下游质量检查，对应 conserv_err>0）
        sum8 = (e["sched_qwait"] + 1.0 + 2.0 + 4.0 + e["fwd_pure"] + e["draft"]
                + e["kv_xfer"] + e["shm_dq"])
        other_exp = max(0.0, e["ttft"] - sum8)
        other = float(d["other_ms"])
        if other < -0.005:
            n_neg += 1
            check(f"[{tag}] CSV other<0 落盘 ({req})", False, f"other={other}")
        check(f"[{tag}] CSV other 对值/钳 0 ({req})",
              abs(other - other_exp) <= 0.02,
              f"csv={other} exp={other_exp:.3f}")
        # ③ 守恒差值：正常行 |Σ10−ttft_ms|≈0；截断行 = Σ8 超出引擎实测 ttft
        #    的毫秒数（other 已钳 0，回填溢出以差值暴露给下游质量检查）
        sum10 = sum(float(d[st + "_ms"]) for st in stage_keys) + other
        viol = sum10 - ttft
        if e["clamp"]:
            check(f"[{tag}] CSV 截断行守恒差值=回填溢出 ({req})",
                  abs(viol - (sum8 - e["ttft"])) <= 0.05,
                  f"viol={viol:.3f} exp={sum8 - e['ttft']:.3f}")
        else:
            check(f"[{tag}] CSV 精确守恒 ({req})", abs(viol) <= 0.02,
                  f"viol={viol:.3f}")
    check(f"[{tag}] CSV 阶段对值 80/80", n_match == 80, f"{n_match}/80")
    check(f"[{tag}] CSV other 无负值落盘", n_neg == 0, f"{n_neg} 行")
    print(f"[{tag}] CSV: {len(csv_rows)} 行，阶段对值 {n_match}/80，"
          f"other 负值 {n_neg}")


def check_edge_cases():
    """检视意见修复的边界用例：重复时间戳排序 / 匹配串算 / 混合 ts 分段 / 负 other。"""
    import importlib.util
    import io
    import contextlib
    spec = importlib.util.spec_from_file_location("analyze_ttft_mod", ANALYZE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    # ---- 1) reconcile：两个 LB 行 client_send 相同（旧实现 sorted 比较字典 → TypeError）
    def lb(req, send, ft):
        return {"machine": "PROXY", "req": req, "wall_recv": 0, "client_send": send,
                "client2proxy_ms": 1.0, "dispatch_ms": 2.0, "wait_p_ms": 0.0,
                "first_token_ms": ft, "prefiller": "", "decoder": ""}
    def bench(req, send, cttft):
        return {"machine": "BENCH", "req": req, "client_send_ts": send,
                "first_token_ts": None, "client_ttft": cttft}
    lb_rows = [lb("r1", 1000, 10.0), lb("r2", 1000, 20.0), lb("r3", 1001, 30.0)]
    bench_rows = [bench("r1", 1000, 11.5), bench("r2", 1000, 21.5),
                  bench("r3", 1001, 31.5), bench("q1", 2000, 99.0)]
    try:
        recon, joined = m.reconcile(lb_rows, bench_rows)
        check("edge: reconcile 重复时间戳不抛 TypeError", True)
    except TypeError as e:
        check("edge: reconcile 重复时间戳不抛 TypeError", False, str(e))
        return
    idx = {r["req"]: r for r in recon}
    check("edge: 同 ts 用请求 ID 消歧，3/3 对上", joined == 3, f"joined={joined}")
    check("edge: r1 匹配自身（first_token=10）",
          abs(idx["r1"]["first_token_ms"] - 10.0) < 1e-9)
    check("edge: r2 匹配自身（first_token=20）",
          abs(idx["r2"]["first_token_ms"] - 20.0) < 1e-9)
    check("edge: 每条 LB 只被用一次（return 各 0.5）",
          all(abs(idx[k]["return_ms"] - 0.5) < 1e-9 for k in ("r1", "r2", "r3")))

    # ---- 2) ambiguous：同 ts 多候选且请求 ID 均不命中 → 标记而非乱配
    lb2 = [lb("x1", 3000, 10.0), lb("x2", 3000, 20.0)]
    b2 = [bench("q9", 3000, 12.0)]
    recon2, joined2 = m.reconcile(lb2, b2)
    check("edge: 无唯一匹配标 ambiguous（不算 joined）",
          joined2 == 0 and recon2[0]["joined"] == "ambiguous",
          f"joined={joined2} tag={recon2[0]['joined']}")
    # ---- 2b) 已用记录不再参与（防串算）：第一条用掉 x1 后第二条只剩 x2
    lb3 = [lb("x1", 3000, 10.0), lb("x2", 3000, 20.0)]
    b3 = [bench("x1", 3000, 12.0), bench("x2", 3000, 22.0)]
    recon3, joined3 = m.reconcile(lb3, b3)
    i3 = {r["req"]: r for r in recon3}
    check("edge: 已用 LB 排除后两条都能唯一对上", joined3 == 2, f"joined={joined3}")
    check("edge: x2 匹配 x2（不被 x1 抢占）",
          abs(i3["x2"]["first_token_ms"] - 20.0) < 1e-9)

    # ---- 3) assign_bursts：混合 ts（部分缺）不崩溃，缺 ts 归入最后 burst
    rows = [{"ts": 1.0, "burst": None}, {"ts": 2.0, "burst": None},
            {"ts": None, "burst": None}, {"ts": 100.0, "burst": None}]
    n_burst, note = m.assign_bursts(rows, one_burst=False, burst_gap_s=10.0)
    check("edge: 混合 ts 不抛异常且分段正确", n_burst == 2, f"n={n_burst}")
    check("edge: 缺 ts 归入最后一个 burst", rows[2]["burst"] == 2,
          f"burst={rows[2]['burst']}")
    check("edge: 归组方式写进说明", "缺时间戳" in note, note)

    # ---- 5) ts 毫秒 epoch 自适应（归一到秒，防 --burst-gap 语义错 1000 倍）
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False,
                                     encoding="utf-8") as tf:
        tf.write("[TTFT][BREAKDOWN] req=ms1 ts=1732000000123.0 ttft=10.0 "
                 "forward=5.0 fwd_tokens=12 fwd_steps=1\n")
        ms_log = tf.name
    try:
        rows_ms = m.load_breakdown(ms_log, "P")
        check("edge: 毫秒 ts 归一到秒", abs(rows_ms[0]["ts"] - 1732000000.123) < 1e-6,
              f"ts={rows_ms[0]['ts']}")
    finally:
        os.unlink(ms_log)

    # ---- 6) 压测 jsonl 坏行显式报错（带文件与行号，不静默吞）
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                     encoding="utf-8") as tf:
        tf.write('{"req": "ok1", "client_send_ts": 1, "client_ttft": 2}\n')
        tf.write('{"req": broken json\n')
        bad_json = tf.name
    try:
        try:
            m.load_bench(bad_json, "BENCH")
            check("edge: 坏 jsonl 显式报错", False, "未抛 SystemExit")
        except SystemExit as e:
            msg = str(e)
            check("edge: 坏 jsonl 显式报错", "第 2 行解析失败" in msg, msg[:120])
    finally:
        os.unlink(bad_json)

    # ---- 4) write_csv：other<0（阶段双计）不崩溃且打 stderr 告警
    row = {"machine": "P", "ip": None, "req": "neg1", "batch": "b", "ts": 1.0,
           "burst": 1, "ttft": 10.0, "_input_len": 0,
           "tokenize_render": 0, "dispatch": 0, "add_req": 0, "mm_hash": 0,
           "queue_wait": 3.0, "wait_exec": 0, "load_kv": 0, "forward": 6.0,
           "mtp": 0, "sample": 0, "save_kv": 0, "step_other": 0,
           "out_delivery": 0, "residual": 0, "lb_dispatch": 1.0,
           "hash": 0, "ext_lookup": 0, "alloc": 0, "overhead": 0,
           "sched_total": 2.0, "update": 0, "emit": 0, "step_exec": 0,
           "dequeue": 0, "dequeue_main": 0, "dequeue_draft": 0,
           "dequeue_take_draft": 0, "dequeue_aux": 0, "return": 0,
           "add_req_iq": 1.0, "add_req_proc": 2.0, "preempt": 0,
           "fwd_tokens": 12, "fwd_steps": 1, "fwd_ms_per_kT": 0,
            "conserv_err": 0, "lb_src": "engine", "cold_step": 0}
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
        csv_path = tf.name
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            m.write_csv([row], csv_path)
        check("edge: other<0 不崩溃且打告警", "other<0" in err.getvalue(),
              err.getvalue()[:200])
        import csv as _csvmod
        with open(csv_path, encoding="utf-8") as fh:
            rows_csv = list(_csvmod.DictReader(fh))
        check("edge: other<0 钳 0 落盘", float(rows_csv[0]["other_ms"]) == 0.0,
              f"other={rows_csv[0]['other_ms']}")
    finally:
        os.unlink(csv_path)

    # ---- 7) split_label 盘符启发式平台门控：Linux 下 'D:/abs' 必须拆 LABEL+路径
    #          （否则文档化用法 FileNotFoundError；Windows 盘符路径仍保持整体）
    saved = os.name
    try:
        os.name = "posix"
        lbl, p = m.split_label("D:/abs/decode.log")
        check("edge: Linux 'D:/abs' 拆 LABEL+路径",
              lbl == "D" and not str(p).startswith("D:"), f"{lbl}:{p}")
        lbl, p = m.split_label("D:decode.log")
        check("edge: Linux 'D:相对路径' 正常拆", lbl == "D", f"{lbl}")
        os.name = "nt"
        lbl, p = m.split_label("C:/x/y.log")
        check("edge: Windows 盘符路径保持整体（不被当 LABEL）",
              str(p).startswith("C:"), f"{p}")
        lbl, p = m.split_label("D:decode.log")
        check("edge: Windows LABEL:path 正常拆", lbl == "D", f"{lbl}")
    finally:
        os.name = saved

    # ---- 8) --burst-gap 0 不被默认值静默替换（0 = 不聚合，逐请求成段）
    rows0 = [{"ts": float(t), "burst": None} for t in (1.0, 1.4, 5.0, 5.2)]
    n0, _note = m.assign_bursts(rows0, one_burst=False, burst_gap_s=0.0)
    check("edge: burst_gap=0 不聚合（逐请求成段）", n0 == 4, f"n={n0}")
    rows1 = [{"ts": float(t), "burst": None} for t in (1.0, 1.4, 5.0, 5.2)]
    n1, _note1 = m.assign_bursts(rows1, one_burst=False, burst_gap_s=300.0)
    check("edge: burst_gap=300 正常聚合", n1 == 1, f"n={n1}")


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
        check_edge_cases()

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
