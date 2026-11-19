#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grade one vllm-eplb-analysis eval run and emit grading.json.

Usage:
    python3 evals/grade.py --eval-id 1 --outputs <run-dir>/outputs --out <run-dir>/grading.json
"""
import argparse
import json
import os
import re

ASSERTIONS = {
    1: [
        "结论为 imbalanced（verdict.json）",
        "至少命中 balancedness、在途请求、TTFT 三个信号",
        "报告给出证据表与阈值对照",
        "报告给出交接命令：dynamic 模式 + 三个 eplb-config 参数",
    ],
    2: [
        "结论为 borderline（verdict.json）",
        "识别出唯一明确信号是 TTFT 倾斜，balancedness 只算边缘区间",
        "建议先做 force EPLB 上限探针而不是直接加冗余专家",
        "说明了探针后如何还原/继续",
    ],
    3: [
        "结论为 balanced（verdict.json）",
        "明确给出不要开 EPLB 的建议",
        "指出应转向排队/KV/通信/请求长度等非专家瓶颈",
        "没有推荐加冗余专家或改模板",
    ],
    4: [
        "说明 balancedness 依赖已开启 EPLB，不是上线前唯一依据",
        "给出 per-engine 指标采集方法（num_requests_* / time_to_first_token_seconds）",
        "给出四种结论与对应动作的判定矩阵",
        "给出可直接执行的采集与分析命令",
    ],
    5: [
        "结论为 inconclusive（verdict.json）",
        "说明窗口不足与可用指标不足的原因",
        "给出补采要求（至少 3 个窗口 + per-engine metrics）",
        "没有直接建议开或不开 EPLB",
    ],
    7: [
        "结论为 imbalanced，且 memory.verdict 为 no_headroom（verdict.json）",
        "动作是 free_memory_first，而不是直接交接 dynamic 加副本",
        "报告说明冗余专家会挤占 KV cache、当前已在换出",
        "报告给出降 KV 压力或按余量收敛副本数的具体做法",
    ],
    6: [
        "先给出 imbalanced 结论（verdict.json）且证据表完整",
        "模板被切成 dynamic：V2 runner=1、export DYNAMIC_EPLB、--enable-eplb 与三个 eplb-config",
        "冗余专家取 32（与分析结论一致，不是无脑默认 16）",
        "修改前有备份且 bash -n 通过",
        "报告串起分析→优化两步，并给出复测口径",
    ],
}


def res(text, passed, evidence):
    return {"text": text, "passed": bool(passed), "evidence": evidence}


def read_outputs(outputs):
    verdict = None
    report = ""
    for root, _d, names in os.walk(outputs):
        for n in sorted(names):
            p = os.path.join(root, n)
            if n in ("verdict.json", "analysis.json"):
                try:
                    verdict = json.load(open(p, encoding="utf-8"))
                except ValueError:
                    pass
            elif n.endswith((".md", ".txt")):
                report += open(p, encoding="utf-8", errors="replace").read() + "\n"
    return verdict, report


def has_any(text, words):
    return any(w in text for w in words)


def grade(eval_id, outputs, template=None):
    verdict, report = read_outputs(outputs)
    v = (verdict or {}).get("verdict", "")
    signals = (verdict or {}).get("signals", [])
    out = []
    if eval_id == 1:
        out.append(res(ASSERTIONS[1][0], v == "imbalanced", "verdict=%r" % v))
        ok = {"I1_balancedness_low", "I2_queue_skew", "I3_ttft_skew"}.issubset(set(signals))
        out.append(res(ASSERTIONS[1][1], ok, "signals=%s" % signals))
        table = ("0.90" in report and has_any(report, ["1.5", "1.3"])) and \
                report.count("|") >= 8
        out.append(res(ASSERTIONS[1][2], table, "报告含阈值与表格" if table else "缺证据表/阈值"))
        handoff = ("dynamic" in report and
                   has_any(report, ["--eplb-config", "--redundant-experts"]) and
                   "step_interval" in report or "step-interval" in report)
        out.append(res(ASSERTIONS[1][3], handoff, "交接命令命中" if handoff else "缺 dynamic 交接参数"))
    elif eval_id == 2:
        out.append(res(ASSERTIONS[2][0], v == "borderline", "verdict=%r" % v))
        edge = ("0.9" in report or "边缘" in report) and ("TTFT" in report or "ttft" in report)
        out.append(res(ASSERTIONS[2][1], edge, "边缘区间 + TTFT 信号" if edge else "未点明边缘/唯一信号"))
        probe = "force" in report.lower()
        out.append(res(ASSERTIONS[2][2], probe, "提到 force 探针" if probe else "未建议 force 探针"))
        back = has_any(report, ["还原", "恢复", "回退", "off"])
        out.append(res(ASSERTIONS[2][3], back, "给出还原路径" if back else "未说明还原/后续"))
    elif eval_id == 3:
        out.append(res(ASSERTIONS[3][0], v == "balanced", "verdict=%r" % v))
        no_enable = has_any(report, ["不要开", "不要启用", "不需要开", "保持 off", "不建议开"])
        out.append(res(ASSERTIONS[3][1], no_enable, "明确不建议开" if no_enable else "缺少明确结论"))
        causes = sum(1 for w in ("排队", "KV", "通信", "请求长度", "队列") if w in report)
        out.append(res(ASSERTIONS[3][2], causes >= 2, "命中 %d 类非专家因素" % causes))
        no_redundant = ("--redundant-experts" not in report and
                        "num_redundant_experts" not in report and
                        "apply_eplb.py --mode dynamic" not in report)
        out.append(res(ASSERTIONS[3][3], no_redundant, "未推荐加副本" if no_redundant else "仍推荐加副本/改模板"))
    elif eval_id == 4:
        dep = "balancedness" in report and has_any(report, ["只在", "只有", "依赖", "已开", "enable-eplb"])
        out.append(res(ASSERTIONS[4][0], dep, "说明 balancedness 前置条件" if dep else "未说明前置条件"))
        metrics = ("num_requests_waiting" in report and
                   "time_to_first_token_seconds" in report)
        out.append(res(ASSERTIONS[4][1], metrics, "指标名齐全" if metrics else "缺 per-engine 指标"))
        matrix = all(w in report for w in ("imbalanced", "borderline", "balanced", "inconclusive"))
        out.append(res(ASSERTIONS[4][2], matrix, "四态判定矩阵齐全" if matrix else "缺判定矩阵"))
        cmds = ("curl" in report and "/metrics" in report and "analyze_eplb_metrics.py" in report)
        out.append(res(ASSERTIONS[4][3], cmds, "采集+分析命令齐全" if cmds else "缺可执行命令"))
    elif eval_id == 7:
        out.append(res(ASSERTIONS[7][0], v == "imbalanced" and
                       ((verdict or {}).get("indicators", {}).get("memory", {}).get("verdict") == "no_headroom"),
                       "verdict=%r memory=%r" % (v, (verdict or {}).get("indicators", {}).get("memory", {}).get("verdict"))))
        out.append(res(ASSERTIONS[7][1],
                       (verdict or {}).get("recommendation", {}).get("action") == "free_memory_first",
                       "action=%r" % (verdict or {}).get("recommendation", {}).get("action")))
        mech = ("KV" in report or "kv" in report) and has_any(report, ["挤占", "挤掉", "换出", "preempt", "OOM"])
        out.append(res(ASSERTIONS[7][2], mech, "说明 KV 挤占/换出" if mech else "未说明显存机制"))
        act = has_any(report, ["max-model-len", "并发", "上下文", "affordable", "余量"]) and             has_any(report, ["降", "减", "收敛", "释放"])
        out.append(res(ASSERTIONS[7][3], act, "给出降 KV/收敛做法" if act else "缺具体做法"))
    elif eval_id == 6:
        out.extend(grade_chain(verdict, report, template))
    elif eval_id == 5:
        out.append(res(ASSERTIONS[5][0], v == "inconclusive", "verdict=%r" % v))
        why = has_any(report, ["窗口", "样本"]) and has_any(report, ["不足", "太少", "噪声"])
        out.append(res(ASSERTIONS[5][1], why, "说明样本不足" if why else "未解释原因"))
        more = has_any(report, ["3 个窗口", "三个窗口", "至少 3", "至少3"]) and \
            ("metrics" in report or "/metrics" in report)
        out.append(res(ASSERTIONS[5][2], more, "给出补采要求" if more else "缺补采要求"))
        no_action = "apply_eplb.py --mode dynamic" not in report
        out.append(res(ASSERTIONS[5][3], no_action, "未给出开/不开结论" if no_action else "仍给了开启动命令"))
    return out


def grade_chain(verdict, report, template):
    import glob
    import subprocess
    out = []
    v = (verdict or {}).get("verdict", "")
    out.append(res(ASSERTIONS[6][0], v == "imbalanced" and report.count("|") >= 8,
                   "verdict=%r" % v))
    text = ""
    if template and os.path.isfile(template):
        text = open(template, encoding="utf-8", errors="replace").read()
    dynamic = ("export VLLM_USE_V2_MODEL_RUNNER=1" in text and
               'export DYNAMIC_EPLB="true"' in text and
               re.search(r"^\s*--enable-eplb\b", text, re.M) is not None and
               all(("--eplb-config.%s" % k) in text for k in
                   ("num_redundant_experts", "window_size", "step_interval")))
    out.append(res(ASSERTIONS[6][1], dynamic, "模板已是 dynamic" if dynamic else "模板不是 dynamic"))
    out.append(res(ASSERTIONS[6][2], "--eplb-config.num_redundant_experts 32" in text,
                   "取 32" if "num_redundant_experts 32" in text else "冗余专家不是 32"))
    bak = glob.glob(template + ".bak-*") if template else []
    try:
        rc = subprocess.run(["bash", "-n", template], capture_output=True).returncode if template else 1
    except OSError:
        rc = 1
    out.append(res(ASSERTIONS[6][3], bool(bak) and rc == 0,
                   "备份 %d 个, bash -n rc=%d" % (len(bak), rc)))
    chain = ("分析" in report and ("优化" in report or "dynamic" in report) and
             ("4 轮" in report or "剔除" in report))
    out.append(res(ASSERTIONS[6][4], chain, "串起分析与复测口径" if chain else "缺复测口径/两步串联"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-id", type=int, required=True, choices=[1, 2, 3, 4, 5, 6, 7])
    ap.add_argument("--outputs", required=True)
    ap.add_argument("--template", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    exps = grade(args.eval_id, args.outputs, args.template)
    passed = sum(1 for e in exps if e["passed"])
    total = len(exps)
    grading = {
        "expectations": exps,
        "summary": {"passed": passed, "failed": total - passed, "total": total,
                    "pass_rate": round(passed / total, 4) if total else 0.0},
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(grading, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(grading["summary"], ensure_ascii=False))
    for e in exps:
        print(("PASS" if e["passed"] else "FAIL"), e["text"], "|", e["evidence"])


if __name__ == "__main__":
    main()
