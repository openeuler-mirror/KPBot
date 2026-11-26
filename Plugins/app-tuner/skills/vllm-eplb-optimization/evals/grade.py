#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grade one eplb-optimization eval run and emit grading.json.

Usage:
    python3 evals/grade.py --eval-id 1 --template /tmp/opencode/eplb-eval-1/run_dp_template.sh \
        --out <run-dir>/grading.json
    python3 evals/grade.py --eval-id 5 --outputs <run-dir>/outputs --out <run-dir>/grading.json
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

TEMPLATE_ASSERTIONS = {
    1: [
        "VLLM_USE_V2_MODEL_RUNNER 被设为 0（force 依赖 V1 runner）",
        "写入 unset DYNAMIC_EPLB 且不存在 export DYNAMIC_EPLB",
        "--additional-config 的 JSON 里加上了 \"enable_force_eplb\":true",
        "additional-config 原有的 4 个键全部保留（enable_cpu_binding/enable_fused_mc2/enable_flashcomm1/enable_shared_expert_dp）",
        "--additional-config 恰好出现 1 次（没有新增第二个 flag）",
        "没有 --enable-eplb 或 --eplb-config.* 参数（未混入 dynamic 残留）",
        "修改前创建了带时间戳的 .bak-* 备份文件",
        "bash -n 语法检查通过",
    ],
    2: [
        "VLLM_USE_V2_MODEL_RUNNER 保持为 1（dynamic 依赖 V2 runner）",
        "写入 export DYNAMIC_EPLB=\"true\"",
        "vllm serve 命令里出现 --enable-eplb",
        "三个 eplb-config 参数为 16 / 1000 / 1000（num_redundant_experts / window_size / step_interval）",
        "additional-config 里没有 enable_force_eplb 键",
        "additional-config 原有 4 个键保留且该 flag 恰好出现 1 次",
        "修改前创建了带时间戳的 .bak-* 备份文件",
        "bash -n 语法检查通过",
    ],
    3: [
        "VLLM_USE_V2_MODEL_RUNNER 从 0 改回 1",
        "unset DYNAMIC_EPLB 行被移除且写入 export DYNAMIC_EPLB=\"true\"",
        "vllm serve 命令里出现 --enable-eplb 与三个 eplb-config 参数（16/1000/1000）",
        "additional-config 里的 enable_force_eplb 键被移除",
        "additional-config 其他 4 个键保留且该 flag 恰好出现 1 次",
        "无 force 残留（不存在 VLLM_USE_V2_MODEL_RUNNER=0 或 unset DYNAMIC_EPLB）",
        "修改前创建了带时间戳的 .bak-* 备份文件",
        "bash -n 语法检查通过",
    ],
    4: [
        "VLLM_USE_V2_MODEL_RUNNER 为 1",
        "不存在 export DYNAMIC_EPLB 与 unset DYNAMIC_EPLB",
        "不存在 --enable-eplb 与 --eplb-config.* 参数",
        "additional-config 里不存在 enable_force_eplb",
        "原有非 EPLB 配置全部保留（DP=8、EP、kv-transfer-config、max-model-len 131072、max-num-seqs 32、port 8000、additional-config 4 个键）",
        "修改前创建了带时间戳的 .bak-* 备份文件",
        "bash -n 语法检查通过",
    ],
    7: [
        "给出冗余比例 8.33%（32/384）与每 rank 多 2 个专家",
        "按 2.4 GiB/副本/rank 估算每 rank 增量 4.8 GiB",
        "判定 3 GiB 放不下，给出可负担上限 16",
        "建议收敛到 16 或先释放显存，并说明比例本身落在常规带",
    ],
    6: [
        "模板文件内容与原始文件完全一致（dry-run 未写入）",
        "没有产生新的 .bak-* 备份文件",
        "报告中包含 unified diff（@@ hunk 与 + 行）",
        "diff 里能看到 --enable-eplb 与 eplb-config 参数",
        "说明了当前状态或未修改文件这一事实",
    ],
}

RATIO_ASSERTIONS = [
    "给出冗余比例 8.33%（32/384）与每 rank 多 2 个专家",
    "按 2.4 GiB/副本/rank 估算每 rank 增量 4.8 GiB",
    "判定 3 GiB 放不下，给出可负担上限 16",
    "建议收敛到 16 或先释放显存，并说明比例本身落在常规带",
]

DATASET_ASSERTIONS = [
    "生成了 dumpjsonl 数据集文件（.jsonl，约 100 行）",
    "每行都是合法 JSON 且带 messages[0].content 字段（可直接被 benchmark_serving --dataset-name dumpjsonl 读取）",
    "每条请求的上下文长度达到 ~100k token 量级（内容 >= 120000 字符，按 1.5 字符/token 估算）",
    "说明了三组对照：关闭 EPLB / dynamic EPLB / force EPLB",
    "说明了 4 轮剔除第 1 轮、取后 3 轮 mean TTFT 均值的口径",
    "说明了混合/在线均衡流量不能用于验证 EPLB 收益",
]

AC_KEYS = ["enable_cpu_binding", "enable_fused_mc2", "enable_flashcomm1", "enable_shared_expert_dp"]
NON_EPLB_KEEP = ["--data-parallel-size 8", "--enable-expert-parallel", "--kv-transfer-config",
                 "--max-model-len 131072", "--max-num-seqs 32", "--port 8000"]


def res(text, passed, evidence):
    return {"text": text, "passed": bool(passed), "evidence": evidence}


def grade_template(eval_id, path):
    out = []
    if not os.path.isfile(path):
        for t in TEMPLATE_ASSERTIONS[eval_id]:
            out.append(res(t, False, "模板文件不存在: %s" % path))
        return out
    text = open(path, encoding="utf-8", errors="replace").read()
    lines = text.splitlines()
    n_ac = len(re.findall(r"--additional-config", text))
    bak = glob.glob(path + ".bak-*")
    try:
        r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        bash_ok, bash_ev = r.returncode == 0, (r.stderr.strip() or "bash -n rc=0")
    except OSError:
        bash_ok, bash_ev = False, "bash 不可用，无法执行 bash -n"
    has_v2_1 = "export VLLM_USE_V2_MODEL_RUNNER=1" in text
    has_v2_0 = "export VLLM_USE_V2_MODEL_RUNNER=0" in text
    has_dyn_export = re.search(r"^\s*export\s+DYNAMIC_EPLB\s*=", text, re.M) is not None
    has_dyn_unset = re.search(r"^\s*unset\s+DYNAMIC_EPLB\b", text, re.M) is not None
    has_enable = re.search(r"^\s*--enable-eplb\b", text, re.M) is not None
    has_cfg = re.search(r"^\s*--eplb-config\.", text, re.M) is not None
    cfg_vals = (("--eplb-config.num_redundant_experts 16" in text)
                and ("--eplb-config.window_size 1000" in text)
                and ("--eplb-config.step_interval 1000" in text))
    has_force = "enable_force_eplb" in text
    keys_ok = all(k in text for k in AC_KEYS)

    if eval_id == 1:
        out.append(res(TEMPLATE_ASSERTIONS[1][0], has_v2_0 and not has_v2_1,
                       "V2=0 存在" if has_v2_0 else "未找到 VLLM_USE_V2_MODEL_RUNNER=0"))
        out.append(res(TEMPLATE_ASSERTIONS[1][1], has_dyn_unset and not has_dyn_export,
                       "unset=%s export=%s" % (has_dyn_unset, has_dyn_export)))
        out.append(res(TEMPLATE_ASSERTIONS[1][2], '"enable_force_eplb":true' in text,
                       "匹配到 enable_force_eplb:true" if '"enable_force_eplb":true' in text
                       else "未找到 \"enable_force_eplb\":true"))
        out.append(res(TEMPLATE_ASSERTIONS[1][3], keys_ok,
                       "缺少: %s" % [k for k in AC_KEYS if k not in text] if not keys_ok
                       else "4 个键全部保留"))
        out.append(res(TEMPLATE_ASSERTIONS[1][4], n_ac == 1, "--additional-config 出现 %d 次" % n_ac))
        out.append(res(TEMPLATE_ASSERTIONS[1][5], not has_enable and not has_cfg,
                       "enable-eplb=%s eplb-config=%s" % (has_enable, has_cfg)))
        out.append(res(TEMPLATE_ASSERTIONS[1][6], bool(bak),
                       "备份: %s" % ([os.path.basename(b) for b in bak] or "无")))
        out.append(res(TEMPLATE_ASSERTIONS[1][7], bash_ok, bash_ev))

    elif eval_id == 2:
        out.append(res(TEMPLATE_ASSERTIONS[2][0], has_v2_1,
                       "V2=1 存在" if has_v2_1 else "未找到 VLLM_USE_V2_MODEL_RUNNER=1"))
        out.append(res(TEMPLATE_ASSERTIONS[2][1], 'export DYNAMIC_EPLB="true"' in text,
                       "匹配" if 'export DYNAMIC_EPLB="true"' in text else "未找到 export DYNAMIC_EPLB=\"true\""))
        out.append(res(TEMPLATE_ASSERTIONS[2][2], has_enable, "--enable-eplb=%s" % has_enable))
        out.append(res(TEMPLATE_ASSERTIONS[2][3], cfg_vals,
                       "三个参数齐备" if cfg_vals else "缺少或数值不符"))
        out.append(res(TEMPLATE_ASSERTIONS[2][4], not has_force, "enable_force_eplb 残留" if has_force else "无"))
        out.append(res(TEMPLATE_ASSERTIONS[2][5], keys_ok and n_ac == 1,
                       "keys_ok=%s n_ac=%d" % (keys_ok, n_ac)))
        out.append(res(TEMPLATE_ASSERTIONS[2][6], bool(bak),
                       "备份: %s" % ([os.path.basename(b) for b in bak] or "无")))
        out.append(res(TEMPLATE_ASSERTIONS[2][7], bash_ok, bash_ev))

    elif eval_id == 3:
        out.append(res(TEMPLATE_ASSERTIONS[3][0], has_v2_1 and not has_v2_0,
                       "V2=1 存在" if has_v2_1 else "V2 未改回 1"))
        out.append(res(TEMPLATE_ASSERTIONS[3][1], (not has_dyn_unset) and has_dyn_export,
                       "unset=%s export=%s" % (has_dyn_unset, has_dyn_export)))
        out.append(res(TEMPLATE_ASSERTIONS[3][2], has_enable and cfg_vals,
                       "enable=%s cfg_ok=%s" % (has_enable, cfg_vals)))
        out.append(res(TEMPLATE_ASSERTIONS[3][3], not has_force,
                       "enable_force_eplb 残留" if has_force else "已移除"))
        out.append(res(TEMPLATE_ASSERTIONS[3][4], keys_ok and n_ac == 1,
                       "keys_ok=%s n_ac=%d" % (keys_ok, n_ac)))
        out.append(res(TEMPLATE_ASSERTIONS[3][5], not has_v2_0 and not has_dyn_unset,
                       "v2_0=%s unset=%s" % (has_v2_0, has_dyn_unset)))
        out.append(res(TEMPLATE_ASSERTIONS[3][6], bool(bak),
                       "备份: %s" % ([os.path.basename(b) for b in bak] or "无")))
        out.append(res(TEMPLATE_ASSERTIONS[3][7], bash_ok, bash_ev))

    elif eval_id == 4:
        out.append(res(TEMPLATE_ASSERTIONS[4][0], has_v2_1,
                       "V2=1 存在" if has_v2_1 else "V2 不是 1"))
        out.append(res(TEMPLATE_ASSERTIONS[4][1], not has_dyn_export and not has_dyn_unset,
                       "export=%s unset=%s" % (has_dyn_export, has_dyn_unset)))
        out.append(res(TEMPLATE_ASSERTIONS[4][2], not has_enable and not has_cfg,
                       "enable-eplb=%s eplb-config=%s" % (has_enable, has_cfg)))
        out.append(res(TEMPLATE_ASSERTIONS[4][3], not has_force,
                       "enable_force_eplb 残留" if has_force else "无"))
        missing = [k for k in NON_EPLB_KEEP if k not in text] + [k for k in AC_KEYS if k not in text]
        out.append(res(TEMPLATE_ASSERTIONS[4][4], not missing,
                       "缺少: %s" % missing if missing else "全部保留"))
        out.append(res(TEMPLATE_ASSERTIONS[4][5], bool(bak),
                       "备份: %s" % ([os.path.basename(b) for b in bak] or "无")))
        out.append(res(TEMPLATE_ASSERTIONS[4][6], bash_ok, bash_ev))

    return out


def grade_dry_run(template, fixture, outputs):
    out = []
    if not os.path.isfile(template):
        for t in TEMPLATE_ASSERTIONS[6]:
            out.append(res(t, False, "模板文件不存在: %s" % template))
        return out
    same = False
    if fixture and os.path.isfile(fixture):
        same = open(template, "rb").read() == open(fixture, "rb").read()
    bak = glob.glob(template + ".bak-*")
    reports = []
    if outputs and os.path.isdir(outputs):
        for root, _d, names in os.walk(outputs):
            for n in names:
                if n.endswith((".md", ".txt")):
                    reports.append(open(os.path.join(root, n), encoding="utf-8",
                                        errors="replace").read())
    report = "\n".join(reports)
    plus = [ln for ln in report.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    minus = [ln for ln in report.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    has_diff = ("@@" in report) and bool(plus)
    out.append(res(TEMPLATE_ASSERTIONS[6][0], same,
                   "与原始文件一致" if same else "模板内容被改动（dry-run 不应写入）"))
    out.append(res(TEMPLATE_ASSERTIONS[6][1], not bak,
                   "备份: %s" % ([os.path.basename(b) for b in bak] or "无")))
    out.append(res(TEMPLATE_ASSERTIONS[6][2], has_diff,
                   "diff 行: +%d / -%d" % (len(plus), len(minus))))
    diff_text = "\n".join(plus)
    out.append(res(TEMPLATE_ASSERTIONS[6][3],
                   "--enable-eplb" in diff_text and "--eplb-config." in diff_text,
                   "diff 命中 eplb 参数" if "--enable-eplb" in diff_text else "diff 未包含 eplb 参数"))
    out.append(res(TEMPLATE_ASSERTIONS[6][4],
                   ("未修改" in report or "未写入" in report or "当前" in report),
                   "报告说明命中" if ("未修改" in report or "未写入" in report or "当前" in report)
                   else "报告未说明未修改/当前状态"))
    return out


def grade_dataset(outputs):
    out = []
    files = []
    for root, _dirs, names in os.walk(outputs):
        for n in names:
            files.append(os.path.join(root, n))
    jsonls = [f for f in files if f.endswith((".jsonl", ".json")) and
              "metrics" not in os.path.basename(f) and "grading" not in os.path.basename(f)]
    payloads = []
    for f in jsonls:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    payloads.append(json.loads(line))
        except Exception:
            pass
    texts = []
    for f in files:
        if f.endswith((".md", ".txt", ".rst")):
            try:
                texts.append(open(f, encoding="utf-8", errors="replace").read())
            except Exception:
                pass
    report = "\n".join(texts)

    out.append(res(DATASET_ASSERTIONS[0], bool(jsonls) and len(payloads) >= 90,
                   "jsonl=%s，解析出 %d 条请求" % ([os.path.basename(f) for f in jsonls], len(payloads))))
    ok_msgs = all(isinstance(p, dict) and p.get("messages") and
                  isinstance(p["messages"], list) and p["messages"] and
                  isinstance(p["messages"][0], dict) and "content" in p["messages"][0]
                  for p in payloads) if payloads else False
    out.append(res(DATASET_ASSERTIONS[1], ok_msgs,
                   "全部 %d 行均为合法 chat payload" % len(payloads) if ok_msgs else "存在非法行或缺 messages"))
    import statistics
    if payloads:
        lens = [len(p["messages"][0].get("content", "")) for p in payloads
                if p.get("messages") and p["messages"]]
        med = int(statistics.median(lens)) if lens else 0
    else:
        med = 0
    out.append(res(DATASET_ASSERTIONS[2], med >= 120000, "内容长度中位数 %d 字符" % med))
    has_three = (("force" in report.lower() or "force eplb" in report.lower())
                 and ("dynamic" in report.lower())
                 and ("off" in report.lower() or "关闭" in report or "基线" in report))
    out.append(res(DATASET_ASSERTIONS[3], has_three,
                   "说明文本命中三组对照" if has_three else "说明文本未完整覆盖三组对照"))
    has_rounds = ("4 轮" in report or "4轮" in report or "四轮" in report) and \
                 ("剔除" in report or "去掉" in report or "排除" in report or "预热" in report)
    out.append(res(DATASET_ASSERTIONS[4], has_rounds,
                   "命中 4 轮+剔首轮口径" if has_rounds else "未说明 4 轮剔首轮口径"))
    has_mixed = ("混合" in report or "在线" in report) and \
                ("不能" in report or "不适用" in report or "预期" in report or "无法" in report)
    out.append(res(DATASET_ASSERTIONS[5], has_mixed,
                   "命中混合流量不适用说明" if has_mixed else "未说明混合流量不适用"))
    return out


def grade_ratio(outputs):
    out = []
    report = ""
    if outputs and os.path.isdir(outputs):
        for root, _d, names in os.walk(outputs):
            for n in sorted(names):
                if n.endswith((".md", ".txt")):
                    report += open(os.path.join(root, n), encoding="utf-8", errors="replace").read() + "\n"
    ratio = ("8.33" in report or "8.3%" in report)
    out.append(res(RATIO_ASSERTIONS[0], ratio, "命中 8.33% 比例" if ratio else "比例缺失"))
    mem = ("4.8" in report and "2.4" in report)
    out.append(res(RATIO_ASSERTIONS[1], mem, "命中 4.8 / 2.4 GiB" if mem else "显存增量缺失"))
    nofit = has_any(report, ["放不下", "不够", "超出", "超预算"]) and "16" in report
    out.append(res(RATIO_ASSERTIONS[2], nofit, "判定放不下并给出 16" if nofit else "未给出可负担上限"))
    advice = has_any(report, ["收敛", "降到", "释放", "先降"]) and \
             has_any(report, ["常规", "推荐", "2%~12%", "2%-12%"])
    out.append(res(RATIO_ASSERTIONS[3], advice, "给出收敛/释放建议与比例带" if advice else "缺建议或比例带"))
    return out


def has_any(text, words):
    return any(w in text for w in words)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-id", type=int, required=True, choices=[1, 2, 3, 4, 5, 6, 7])
    ap.add_argument("--template", default=None)
    ap.add_argument("--fixture", default=None)
    ap.add_argument("--outputs", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.eval_id == 5:
        expectations = grade_dataset(args.outputs)
    elif args.eval_id == 6:
        expectations = grade_dry_run(args.template, args.fixture, args.outputs)
    elif args.eval_id == 7:
        expectations = grade_ratio(args.outputs)
    else:
        expectations = grade_template(args.eval_id, args.template)

    passed = sum(1 for e in expectations if e["passed"])
    total = len(expectations)
    grading = {
        "expectations": expectations,
        "summary": {
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "pass_rate": round(passed / total, 4) if total else 0.0,
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(grading, f, ensure_ascii=False, indent=2)
    print(json.dumps(grading["summary"], ensure_ascii=False))
    for e in expectations:
        print(("PASS" if e["passed"] else "FAIL"), e["text"], "|", e["evidence"])


if __name__ == "__main__":
    main()
