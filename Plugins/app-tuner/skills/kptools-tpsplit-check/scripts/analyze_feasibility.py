#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_feasibility.py — TP→DP 扩容可行性测算与报告生成

按「全卡复制 / TP 切分 / EP 切分 / 额外(未归类)」四桶拆解权重，扫描候选 TP，
做单卡容量校验与 DP 扩容收益测算，输出固定 9 段结构的 Markdown 报告 + JSON。

用法:
  python analyze_feasibility.py --facts facts.env --model-dir /path/to/model \
      --total-cards 160 --ep-group 32 --tp-candidates 32,16,8 \
      --out feasibility.md --json feasibility.json
  # 无模型目录时直接给分桶:
  python analyze_feasibility.py --facts facts.env --weights-json buckets.json ...
  # 只看测算计划与输入清单（无需任何输入，可单独跑）:
  python analyze_feasibility.py --dump-plan
  # 自测（复现 GLM5.2 参考量级 + 分桶/口径/校准路径，写到 examples/selftest_report.md）:
  python analyze_feasibility.py --selftest

口径:
  - "Model weights take" 与 Ascend "Actual usage: X GiB for weights" 都是每卡值（日志 device profiling）。
  - W4A8 权重按 safetensors header 的 nbytes 计，即打包后真实字节。
  - 每卡权重(tp) = 复制桶 + 额外桶 + TP桶/tp + EP桶/ep_group（EP 关闭时 EP桶/tp）。
  - MTP 层（层号 ≥ num_hidden_layers）与普通层同规则分桶，不整层按复制算；
    额外桶只收 --weights-json 手工值与日志校准回灌的未归类量（每卡复制，保守口径）。
  - 可用口径优先级: 日志 Desired(total) > 日志权重+KV+反推开销 > 总显存×util−保留。
  - KV/token 优先级: 日志 KV池÷token数 > Maximum concurrency×max_model_len > config 公式
    （config 公式漏 DSA indexer 等附加缓存，约低 8%）。
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import struct
import sys
from datetime import datetime

GIB = 1024 ** 3
TOOL = "analyze_feasibility.py v0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.normpath(os.path.join(HERE, "..", "examples"))

DTYPE_BYTES = {
    "BF16": 2, "F16": 2, "F32": 4, "F64": 8,
    "I8": 1, "U8": 1, "I16": 2, "U16": 2, "I32": 4, "U32": 4, "I64": 8, "U64": 8,
    "F8_E4M3": 1, "F8_E5M2": 1, "F8_E8M0": 1, "BOOL": 1,
}

LAYER_RE = re.compile(r"(?:^|\.)layers\.(\d+)\.")  # 仅用于报告标注 MTP 归属，分桶不再用层号

# 桶中文名
BUCKET_CN = {"rep": "全卡复制", "tp": "TP 切分", "ep": "EP 切分", "extra": "额外(未归类,每卡复制)"}
BUCKET_RULE = {
    "rep": "norm/bias、router gate、e_score_correction_bias、MLA 降维投影(q_a/kv_a_proj)",
    "tp": "attention 主干(q_b/kv_b/o/qkv)、embedding/lm_head、dense MLP、shared_expert（含 MTP 层主干）",
    "ep": "路由专家 experts.<n>.*（W4A8 打包字节，含 MTP 层专家）",
    "extra": "MTP 层按普通层规则分桶，不进此桶；此桶只收 --weights-json 手工值与日志校准回灌的未归类量（每卡复制，保守口径）",
}

VERDICT_CN = {
    "ok": "✅ 可行",
    "oom": "❌ KV池≤0（显存不够）",
    "len": "❌ 放不下 1 条满长请求",
    "topology": "⚠️ EP 组不整除",
    "unknown": "❓ 数据不足未校验",
    "cur": "—（当前）",
}


# ---------------------------------------------------------------- 基础工具
def _num(v, default=None):
    if isinstance(v, str):
        v = v.replace(",", "").strip()  # 容错千分位逗号（手工编辑的 facts：131,072 / 331,000）
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=None):
    n = _num(v)
    return int(n) if n is not None else default


def parse_env(path):
    facts = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            # 容错：剥成对引号（旧版采集端或手工编辑的 facts 里 TP_SIZE="32" 这类值，
            # 不剥会被 _int 当 None → 当缺失处理，报告却标「实测」，误导）
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            facts[k.strip()] = v
    return facts


def fmt(x, nd=2):
    return ("{:,.%df}" % nd).format(x) if isinstance(x, (int, float)) else "—"


def tok(x):
    if not isinstance(x, (int, float)) or x <= 0:
        return "—"
    return f"{int(round(x)):,}"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 权重分桶
def read_safetensors(model_dir):
    """读全部 *.safetensors header。返回 (tensors, 文件数, 解析失败数)。
    分片缺失/解析失败时调用方会把权重标为不完整（只报现状，不做分桶测算）。"""
    tensors = []
    files = sorted(_glob.glob(os.path.join(model_dir, "*.safetensors")))
    failed = 0
    for fp in files:
        try:
            with open(fp, "rb") as f:
                n = struct.unpack("<Q", f.read(8))[0]
                hdr = json.loads(f.read(n).decode("utf-8"))
            for name, meta in hdr.items():
                if name == "__metadata__":
                    continue
                off = meta.get("data_offsets", [0, 0])
                tensors.append((name, off[1] - off[0]))
        except (OSError, ValueError, struct.error) as e:
            failed += 1
            print(f"# WARN: 跳过无法解析的 safetensors: {fp} ({e})", file=sys.stderr)
    return tensors, len(files), failed


def classify_tensor(name):
    """张量名 → 桶。MTP 层（层号 ≥ num_hidden_layers）与普通层同规则分桶
    （attention→tp、experts→ep、norm→rep），不整层按复制算。
    规则表见 references/weight-classification.md。"""
    if "shared_expert" in name:
        return "tp"  # 共享专家按 dense MLP 走 TP
    if ("experts." in name) or ("experts_" in name):
        return "ep"  # 路由专家（MTP 层里的专家同样进这里）
    if "e_score_correction_bias" in name:
        return "rep"
    if re.search(r"(?:^|\.)mlp\.gate\.weight$", name):
        return "rep"  # router gate（注意不是 gate_proj）
    if "q_a_proj" in name or "kv_a_proj" in name:
        return "rep"  # MLA 降维投影，每卡复制
    if "norm" in name.lower() or name.endswith(".bias"):
        return "rep"
    return "tp"  # attention 主干 / embedding / lm_head / dense MLP（含 MTP 层主干）


def load_model_info(model_dir):
    """读 config.json + safetensors header（不加载权重），返回分桶结果。
    config.json 缺失时 mla=None（未知，交给分析端按 MLA 假设并告警，而不是断言 MHA）；
    分片缺失/解析失败/索引清单缺片时 complete=False（只报现状，不做分桶测算）。"""
    cfg = {}
    cfg_path = os.path.join(model_dir, "config.json")
    cfg_found = os.path.isfile(cfg_path)
    if cfg_found:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            cfg = json.load(f)
    n_layers = cfg.get("num_hidden_layers")
    tensors, n_files, n_failed = read_safetensors(model_dir)
    # 索引清单核对：模型自带 model.safetensors.index.json 时，weight_map 引用的分片必须
    # 全部在本地——缺片即使本地其余全部可解析、facts 又没给 SAFETENSORS_COUNT，
    # 也能靠索引本身发现（索引就是权威分片清单，核对不依赖 facts）
    index_missing, index_total = [], None
    idx_path = os.path.join(model_dir, "model.safetensors.index.json")
    if os.path.isfile(idx_path):
        try:
            with open(idx_path, "r", encoding="utf-8", errors="replace") as f:
                idx = json.load(f)
            referenced = sorted({os.path.basename(str(v)) for v in (idx.get("weight_map") or {}).values()})
            index_total = len(referenced)
            local_names = {os.path.basename(p) for p in _glob.glob(os.path.join(model_dir, "*.safetensors"))}
            index_missing = [s for s in referenced if s not in local_names]
        except (OSError, ValueError) as e:
            print(f"# WARN: 无法解析 {idx_path}，跳过索引清单核对 ({e})", file=sys.stderr)
    sums = {"rep": 0, "tp": 0, "ep": 0, "extra": 0}
    tlist = []
    for name, nb in tensors:
        b = classify_tensor(name)
        sums[b] += nb
        tlist.append((name, nb, b))
    incomplete_reason = ""
    if n_files == 0:
        incomplete_reason = "无 *.safetensors 可读（目录只有 config.json？）"
    elif n_failed:
        incomplete_reason = f"{n_failed}/{n_files} 个分片解析失败"
    elif index_missing:
        incomplete_reason = (f"索引清单缺分片：model.safetensors.index.json 引用 {index_total} 个，"
                             f"本地缺 {', '.join(index_missing)}")
    return {
        "source": "model-dir: config.json + safetensors headers",
        "rep_gib": sums["rep"] / GIB,
        "tp_gib": sums["tp"] / GIB,
        "ep_gib": sums["ep"] / GIB,
        "extra_gib": sums["extra"] / GIB,
        "tensors": tlist,
        "n_tensors": len(tlist),
        "n_layers": n_layers,
        "mla": ((cfg.get("kv_lora_rank") or 0) > 0) if cfg_found else None,
        "config": cfg,
        "config_exists": cfg_found,
        "model_dir": model_dir,
        # 权重完整性：无分片 / 解析失败 / 索引清单缺片 → 分桶不可用，只报告现状
        "complete": not incomplete_reason,
        "n_shard_files": n_files,
        "n_shard_failed": n_failed,
        "index_total": index_total,
        "index_missing": index_missing,
        "incomplete_reason": incomplete_reason,
    }


def load_weights_json(path):
    d = json.load(open(path, "r", encoding="utf-8"))
    rep = _num(d.get("replicated_gib", d.get("rep_gib")), 0.0)
    tp = _num(d.get("tp_gib", d.get("tp_bucket_gib")), 0.0)
    ep = _num(d.get("ep_gib", d.get("ep_bucket_gib")), 0.0)
    extra = _num(d.get("extra_gib", d.get("mtp_gib")), 0.0)
    tensors = []
    for t in d.get("tensors", []):
        try:
            tensors.append((str(t[0]), int(t[1]), str(t[2])))
        except (TypeError, ValueError, IndexError):
            continue  # 容忍手工编辑的 malformed 条目
    return {
        "source": f"weights-json: {os.path.basename(path)}",
        "rep_gib": rep, "tp_gib": tp, "ep_gib": ep, "extra_gib": extra,
        "tensors": tensors, "n_tensors": len(tensors),
        "n_layers": d.get("num_hidden_layers"),
        "mla": d.get("mla"),
        "config": d.get("config", {}),
        "model_dir": None,
        "note": d.get("note", ""),
        "complete": True,  # 手工给的分桶视为权威输入
        "n_shard_files": None, "n_shard_failed": None, "incomplete_reason": "",
    }


def calibrate(w, log_weights_gib, tp_cur, ep_div_cur):
    """用日志每卡权重校准分桶；|差|>5% 时把差额回灌进额外桶（每卡复制）。"""
    calc = w["rep_gib"] + w["extra_gib"] + w["tp_gib"] / tp_cur + w["ep_gib"] / ep_div_cur
    diff, applied = None, 0.0
    if log_weights_gib is not None:
        diff = log_weights_gib - calc
        if abs(diff) > 0.05 * log_weights_gib:
            new_extra = max(0.0, w["extra_gib"] + diff)
            applied = new_extra - w["extra_gib"]
            w["extra_gib"] = new_extra
    return calc, diff, applied


# ---------------------------------------------------------------- 运行态
def _npu_fact(facts, suffix):
    """读单卡显存事实：NPU_<suffix> 别名优先（与卡号无关）；无别名时扫描全部
    NPU<id>_<suffix>，取最小值代表单卡规格（同型卡最保守口径）并返回卡号。
    采集端 --npu 采别的卡时分析端不会静默丢弃。返回 (值, 卡号或 None)。"""
    v = facts.get("NPU_" + suffix, "")
    if v != "":
        return _num(v), None
    best, best_id = None, None
    for k, val in facts.items():
        m = re.match(r"^NPU(\d+)_%s$" % re.escape(suffix), k)
        if m and val != "":
            n = _num(val)
            if n is not None and (best is None or n < best):
                best, best_id = n, int(m.group(1))
    return best, best_id


def per_stage_max_weights(weights, tp, ep_div, pp):
    """PP>1 时按显存占用最高的流水线阶段核算每卡权重（防止把所有阶段层权重
    都记到一张卡上、把放得下的候选误判为 OOM）。
    阶段划分：普通层按 层号÷ceil(层数/pp)；MTP 层（层号≥层数）与 lm_head/
    最终 norm 等无层号项归末阶段；embedding 归首阶段。
    阶段张量覆盖核对：tensors 缺失、或字节总量对不上汇总桶（如 weights-json 只给
    汇总桶）→ 无法按阶段分配，返回 None（unknown）——不能返回 0 低估每卡权重。
    覆盖 ≥99% 的小额缺失：缺失字节**并入最重阶段**保守计算（127/128 GiB 明细 →
    按 128 核算，缺失部分可能在任何阶段，并入最重阶段保证是上界、不低估）。
    缺层数同样返回 None。"""
    n_layers = weights.get("n_layers")
    if pp <= 1 or not n_layers:
        return None
    tensors = weights.get("tensors") or []
    buckets_bytes = ((weights.get("rep_gib") or 0.0) + (weights.get("tp_gib") or 0.0)
                     + (weights.get("ep_gib") or 0.0)) * GIB
    tensors_bytes = sum(nb for _, nb, _ in tensors)
    missing = max(0.0, buckets_bytes - tensors_bytes)
    if not tensors or buckets_bytes <= 0:
        return None  # 无阶段张量：分阶段核算不可用 → unknown
    if missing > 0.01 * buckets_bytes:
        return None  # 阶段张量覆盖不足 99%：缺失大到可能改变判定 → unknown
    per = (n_layers + pp - 1) // pp
    stages = [{"rep": 0.0, "tp": 0.0, "ep": 0.0} for _ in range(pp)]
    for name, nb, b in tensors:
        m = LAYER_RE.search(name)
        if m:
            li = int(m.group(1))
            st = pp - 1 if li >= n_layers else min(li // per, pp - 1)  # MTP 挂末阶段
        elif name.endswith("embed_tokens.weight"):
            st = 0
        else:
            st = pp - 1  # lm_head / 最终 norm 等无层号项归末阶段（保守：最重阶段）
        stages[st][b if b in ("rep", "tp", "ep") else "rep"] += nb
    worst = max(s["rep"] + s["tp"] / tp + s["ep"] / ep_div for s in stages)
    # 小额缺失并入最重阶段（保守上界）：127/128 GiB 明细 → 按 128 核算
    worst += missing
    return worst / GIB + (weights.get("extra_gib") or 0.0)


def build_runtime(args, facts, weights):
    """收集 TP/DP/EP/显存口径，标注每项来源。"""
    src = {}

    def get(key, num=True, default=None):
        v = facts.get(key, "")
        if v == "":
            return default
        return _num(v) if num else v

    tp_cur = _int(get("TP_SIZE"), None)
    dp_cur = _int(get("DP_SIZE"), None)  # 缺 DP_SIZE → None（未采集），不静默当 1
    pp = _int(get("PP_SIZE"), 1)
    if pp is None or pp < 1:
        if pp is not None:
            print(f"# WARN: PP_SIZE={pp} 无效（须 ≥1），按 1 处理", file=sys.stderr)
        pp = 1
    ep_raw = str(get("EP_ENABLED", num=False, default="")).strip().lower()
    if ep_raw:
        ep_enabled = ep_raw in ("1", "true", "yes")
        ep_src = "实测(ps/script)"
    else:
        # facts 缺 EP_ENABLED：按启用假设处理（MoE+EP 是本 skill 主场景），
        # 误判成"关闭"会把 EP 桶除以 tp，权重爆炸到离谱
        ep_enabled = True
        ep_src = "假设启用（facts 缺 EP_ENABLED；若实际未启用 EP，专家按 TP 切，结论不同）"

    # EP 组大小: 显式 --ep-group > facts EP_GROUP_SIZE（采集端=实例 TP×DP）> 实例 TP×DP 推断 > 未知
    # vLLM EP 世界大小 = tp × dp；只有 tp 没有 dp 时无法推断，按未知处理并告警（不再拍"=当前 tp"，
    # 否则 TP8×DP4 的实例会被当成 EP8，专家权重除数偏离该实例真实的 EP32）
    ep_group = _int(args.ep_group, None)
    if ep_group is not None and ep_group < 1:
        print(f"# WARN: --ep-group {ep_group} 无效（须 ≥1），已忽略", file=sys.stderr)
        ep_group = None
    if ep_group is not None:
        src["ep_group"] = "输入 --ep-group"
    else:
        ep_group = _int(get("EP_GROUP_SIZE"), None)
        if ep_group is not None:
            src["ep_group"] = "facts EP_GROUP_SIZE（采集端 = 实例 TP×DP）"
        elif tp_cur and dp_cur:
            ep_group = tp_cur * dp_cur
            src["ep_group"] = "推断 = 实例 TP×DP"
        elif tp_cur:
            ep_group = None
            src["ep_group"] = "未知（缺 DP_SIZE 无法推断 EP 组，建议显式传 --ep-group）"
        else:
            ep_group = None
            src["ep_group"] = "未知（缺 TP_SIZE）"
    if ep_group is not None and ep_group < 1:
        # facts 或推断出的 EP 组非正数（TP/DP 被采成负值等）→ 按未知处理，防止取模/除法出错
        print(f"# WARN: EP 组大小 {ep_group} 无效（须 ≥1），按未知处理", file=sys.stderr)
        ep_group = None
        src["ep_group"] = "无效值已忽略（须 ≥1），按未知处理"
    ep_div_cur = ep_group if (ep_enabled and ep_group) else (tp_cur or 1)

    # 总卡数: 显式 > facts 里的 TP×DP×PP
    total_cards = _int(args.total_cards, None)
    if total_cards is not None and total_cards < 1:
        print(f"# WARN: --total-cards {total_cards} 无效（须 ≥1），按未知处理", file=sys.stderr)
        total_cards = None
    if total_cards is not None:
        src["total_cards"] = "输入 --total-cards"
    elif tp_cur is not None:
        total_cards = tp_cur * (dp_cur or 1) * (pp or 1)
        src["total_cards"] = "推算 = TP×DP×PP"
    else:
        src["total_cards"] = "未知"

    # 显存口径：NPU_<名> 别名（与卡号无关）优先；无别名时扫描任意 NPU<id>_<名>，
    # 取最小值代表单卡规格并注明取自第几号卡——采集端用 --npu 采非 0 卡时不会静默丢失
    total_mb, total_npu = _npu_fact(facts, "TOTAL_MB")
    total_gib = total_mb / 1024 if total_mb is not None else None
    if total_gib is not None:
        src["total_gib"] = ("实测 npu-smi（NPU_TOTAL_MB 别名）" if total_npu is None
                            else f"实测 npu-smi（卡 {total_npu}，多卡取最小规格）")
    else:
        src["total_gib"] = "缺失"
    used_mb, used_npu = _npu_fact(facts, "USED_MB")
    used_gib = used_mb / 1024 if used_mb is not None else None
    used_src = "—" if used_gib is None else (
        "实测 npu-smi（NPU_USED_MB 别名）" if used_npu is None else f"实测 npu-smi（卡 {used_npu}）")
    hbm_rate, _ = _npu_fact(facts, "HBM_RATE_PCT")
    if used_gib is None and total_gib is not None and hbm_rate is not None:
        # 老卡兜底：只有 HBM Usage Rate(%) 时推算已用
        used_gib = total_gib * hbm_rate / 100.0
        used_src = "推算 = 总显存×HBM Usage Rate(%)"

    util = get("GPU_MEMORY_UTILIZATION")
    util_src = "实测(启动参数)"
    if util is None:
        util = 0.90
        util_src = "假设默认 0.90"
    reserved_gib = get("RESERVED_GIB")  # 一般没有，走默认
    if reserved_gib is None:
        reserved_gib = 3.6
        res_src = "假设默认 3.6 GiB（驱动/CANN/HCCL 保留）"
    else:
        res_src = "facts"
    act_gib = get("ACTIVATION_GIB")
    act_src = "假设默认 2.5" if act_gib is None else "facts"
    if act_gib is None:
        act_gib = 2.5
    other_gib = get("OTHER_GIB")
    oth_src = "假设默认 1.8" if other_gib is None else "facts"
    if other_gib is None:
        other_gib = 1.8

    log_w = get("LOG_MODEL_WEIGHTS_GIB")
    log_kv = get("LOG_AVAILABLE_KV_GIB")
    desired = get("LOG_DESIRED_TOTAL_GIB")
    actual_total = get("LOG_ACTUAL_TOTAL_GIB")
    log_non_torch = get("LOG_NON_TORCH_GIB")
    log_torch_peak = get("LOG_TORCH_PEAK_GIB")

    # 非 KV 开销（激活+其他）优先用日志反推，不要拍默认值：
    # Total usage − 权重 − KV = 实测开销；激活取假设值，余量归"其他"
    got_residual = False
    if actual_total is not None and log_w is not None and log_kv is not None:
        residual = round(actual_total - log_w - log_kv, 6)
        if residual >= 0:
            got_residual = True
            act_gib = round(min(act_gib, residual), 6)
            other_gib = round(max(0.0, residual - act_gib), 6)
            act_src = "日志反推(合计)"
            oth_src = "日志反推 Total−权重−KV"
    if not got_residual and log_non_torch is not None and log_non_torch > other_gib:
        # Total 行缺失时用 Non-Torch 实测兜底"其他"开销（采集键消费，不再白采）
        other_gib = log_non_torch
        oth_src = "日志实测 Non-Torch memory（Total 行缺失时的兜底口径）"

    # 可用口径优先级:
    #   1) 日志 Desired usage(total) —— vLLM 自己的预算，最可信
    #   2) 日志权重+日志KV+开销 反推
    #   3) 总显存×利用率−保留（推算，可能比实际多 2-3 GiB）
    if desired is not None:
        usable_gib, usable_src = desired, "日志实测 Desired usage(total)"
    elif log_w is not None and log_kv is not None:
        usable_gib = log_w + log_kv + act_gib + other_gib
        usable_src = "日志推算 = 权重(日志)+KV(日志)+激活+其他"
    elif total_gib is not None:
        usable_gib = total_gib * util - reserved_gib
        usable_src = "推算 = 总显存×利用率 − 保留(假设)，可能偏差 2-3 GiB"
    else:
        usable_gib, usable_src = None, "缺失（无 npu-smi 且无日志）"

    # MLA / KV 每 token 字节
    mla = None
    mla_src = "未知"
    if weights is not None:
        if weights.get("mla") is True:
            mla, mla_src = True, "config kv_lora_rank / weights-json"
        elif weights.get("mla") is False:
            mla, mla_src = False, "config / weights-json"
    if mla is None:
        mla, mla_src = True, "假设 MLA（W4A8 MoE 常见；MHA 结论相反，务必确认）"

    # token 容量实测来源优先级: KV cache size 行 > Maximum concurrency × max_model_len
    max_model_len = _int(get("MAX_MODEL_LEN"), None)
    log_kv_tokens = _int(get("LOG_KV_TOKENS"), None)
    kv_tokens_src = "日志实测 GPU/NPU KV cache size" if log_kv_tokens else None
    conc = _num(get("LOG_MAX_CONCURRENCY"), None)  # 保留小数（如 2.50x → 2.50，不再截成整数）
    if log_kv_tokens is None and conc and max_model_len:
        log_kv_tokens = conc * max_model_len
        kv_tokens_src = "日志推算 = Maximum concurrency × max_model_len"

    # KV dtype 字节（实测 KV_CACHE_DTYPE；fp8 系=1B，auto/缺失按 bf16=2B 并标注假设）
    kv_dtype = str(get("KV_CACHE_DTYPE", num=False, default="") or "").strip().lower()
    if kv_dtype in ("fp8", "e4m3", "e5m2", "fp8_e4m3", "fp8_e5m2", "f8_e4m3", "f8_e5m2"):
        kv_dtype_bytes, kv_dtype_src = 1, f"实测 KV_CACHE_DTYPE={kv_dtype}（1B）"
    elif kv_dtype == "auto":
        kv_dtype_bytes, kv_dtype_src = 2, "实测=auto（按模型精度 bf16 2B 计）"
    elif kv_dtype:
        kv_dtype_bytes, kv_dtype_src = 2, f"实测 KV_CACHE_DTYPE={kv_dtype}（未知别名，按 2B 保守计）"
    else:
        kv_dtype_bytes, kv_dtype_src = 2, "缺省按 bf16 2B"

    # 本卡持有的 KV 层数（PP>1 时 = ceil(层数/pp)；缺层数 → None，容量标未知）
    n_layers = (weights.get("n_layers") if weights else None) or \
        ((weights.get("config", {}) or {}).get("num_hidden_layers") if weights else None)
    layers_on_card = ((n_layers + pp - 1) // pp) if n_layers else None

    # 每 token KV 字节: 实测反推优先（config 公式漏 DSA indexer 等附加缓存，约低 8%）
    kv_bpt = None
    kv_bpt_src = "未知（token 级容量未校验）"
    if log_kv_tokens and log_kv is not None and log_kv > 0:
        kv_bpt = log_kv * GIB / log_kv_tokens
        kv_bpt_src = f"日志推算 = KV池(日志)÷token数（{kv_tokens_src}）"
    elif weights is not None and weights.get("config"):
        cfg = weights["config"]
        if mla and cfg.get("kv_lora_rank"):
            rope = cfg.get("qk_rope_head_dim", 64)
            if layers_on_card:
                kv_bpt = (cfg["kv_lora_rank"] + rope) * kv_dtype_bytes * layers_on_card
                kv_bpt_src = (f"config 计算 (kv_lora_rank+rope)×{kv_dtype_bytes}B×{layers_on_card}层"
                              f"{'(本卡,PP%d)' % pp if pp > 1 else ''}"
                              f" [dtype：{kv_dtype_src}；未含 DSA indexer 等附加缓存，实测口径优先，约低 8%]")
            else:
                kv_bpt, kv_bpt_src = None, "未知（缺 num_hidden_layers，无法按层折算 KV；补 config 或用日志实测口径）"
        elif not mla and cfg.get("num_key_value_heads"):
            hd = cfg.get("head_dim") or (cfg.get("hidden_size", 1) // (cfg.get("num_attention_heads") or 1))
            if layers_on_card:
                kv_bpt = 2 * cfg["num_key_value_heads"] * hd * kv_dtype_bytes * layers_on_card / (tp_cur or 1)
                kv_bpt_src = (f"config 计算 2×kv_heads×head_dim×{kv_dtype_bytes}B×{layers_on_card}层"
                              f"{'(本卡,PP%d)' % pp if pp > 1 else ''}÷tp [dtype：{kv_dtype_src}]")
            else:
                kv_bpt, kv_bpt_src = None, "未知（缺层数，MHA 每卡 KV 无法按层折算；否则每 token 字节会被低估层 数倍）"

    param_source = str(facts.get("PARAM_SOURCE", "")).strip().lower() or "unknown"

    return {
        "tp_cur": tp_cur, "dp_cur": dp_cur, "pp": pp, "ep_enabled": ep_enabled,
        "ep_src": ep_src, "ep_group": ep_group, "ep_div_cur": ep_div_cur, "total_cards": total_cards,
        "total_gib": total_gib, "used_gib": used_gib, "used_src": used_src,
        "hbm_rate": hbm_rate, "util": util,
        "reserved_gib": reserved_gib, "act_gib": act_gib, "other_gib": other_gib,
        "usable_gib": usable_gib, "log_weights_gib": log_w, "log_kv_gib": log_kv,
        "log_desired_gib": desired, "log_actual_total_gib": actual_total,
        "log_non_torch_gib": log_non_torch, "log_torch_peak_gib": log_torch_peak,
        "log_kv_tokens": log_kv_tokens, "kv_tokens_src": kv_tokens_src,
        "kv_bpt": kv_bpt, "kv_bpt_src": kv_bpt_src,
        "kv_dtype": kv_dtype, "kv_dtype_bytes": kv_dtype_bytes, "kv_dtype_src": kv_dtype_src,
        "layers_on_card": layers_on_card,
        "mla": mla, "mla_src": mla_src, "max_model_len": max_model_len,
        "param_source": param_source,
        "model_dir": facts.get("MODEL_DIR", ""), "host": facts.get("HOST_NAME", ""),
        "src": src, "util_src": util_src, "res_src": res_src,
        "act_src": act_src, "oth_src": oth_src, "usable_src": usable_src,
    }


def default_candidates(ep_group, tp_cur):
    """默认候选: EP 组的约数（≥4），并保证当前 TP 在列。"""
    cands = []
    if ep_group:
        for t in range(4, ep_group + 1):
            if ep_group % t == 0:
                cands.append(t)
    if tp_cur and tp_cur not in cands:
        cands.append(tp_cur)
    return sorted(set(cands), reverse=True) if cands else ([tp_cur] if tp_cur else [])


# ---------------------------------------------------------------- 候选扫描
def scan_candidates(weights, rt, cands):
    rows = []
    tp_cur, pp = rt["tp_cur"], rt["pp"] or 1
    for tp in cands:
        row = {"tp": tp, "is_cur": tp == tp_cur}
        # 拓扑
        if rt["ep_enabled"] and rt["ep_group"]:
            if rt["ep_group"] % tp != 0:
                row.update({"dp_per_group": None, "cluster_dp": None, "verdict": "topology"})
                rows.append(row)
                continue
            row["dp_per_group"] = rt["ep_group"] // tp
        else:
            row["dp_per_group"] = None
        # 集群可容纳 DP 数（容量口径，仅用于「集群可容纳 DP」列与推荐比较；
        # 「当前 DP」一律用 facts 实测 DP_SIZE，不用这里反推的值冒充实测）
        row["cluster_dp"] = (rt["total_cards"] // (tp * pp)) if rt["total_cards"] else None
        # 每卡权重
        if weights is not None:
            if rt["ep_enabled"] and rt["ep_group"]:
                ep_div = rt["ep_group"]
            elif rt["ep_enabled"]:
                # EP 启用但组大小未知：除数取候选 tp 只作参考展示，判定一律 unknown 并告警
                ep_div = tp
                row["ep_group_unknown"] = True
            else:
                ep_div = tp
            if pp > 1:
                w_pp = per_stage_max_weights(weights, tp, ep_div, pp)
                if w_pp is None:
                    row["weights_gib"] = None
                    row["pp_unknown"] = True  # PP>1 且缺层数 → 无法分阶段核算
                else:
                    row["weights_gib"] = w_pp
            else:
                row["weights_gib"] = (
                    weights["rep_gib"] + weights["extra_gib"]
                    + weights["tp_gib"] / tp + weights["ep_gib"] / ep_div
                )
        else:
            row["weights_gib"] = rt["log_weights_gib"] if row["is_cur"] else None
        # KV 池
        if rt["usable_gib"] is not None and row["weights_gib"] is not None:
            row["kv_pool_gib"] = rt["usable_gib"] - row["weights_gib"] - rt["act_gib"] - rt["other_gib"]
        else:
            row["kv_pool_gib"] = None
        # 每 DP token 容量
        if rt["kv_bpt"] and row["kv_pool_gib"] is not None and row["kv_pool_gib"] > 0:
            bpt = rt["kv_bpt"] if rt["mla"] else rt["kv_bpt"] * (tp_cur or 1) / tp
            row["kv_bpt"] = bpt
            row["per_dp_tokens"] = row["kv_pool_gib"] * GIB / bpt
        else:
            row["kv_bpt"] = rt["kv_bpt"]
            row["per_dp_tokens"] = None
        # 集群 KV 总量：当前行按实测 DP_SIZE 折算（真实现状），候选行按容量口径 DP
        base_dp = (rt["dp_cur"] if (row["is_cur"] and rt["dp_cur"]) else row["cluster_dp"])
        row["cluster_kv_tokens"] = (
            base_dp * row["per_dp_tokens"]
            if base_dp and row["per_dp_tokens"] is not None else None
        )
        # 判定（顺序很重要：先看显存够不够，再看容量有没有实测来源）
        if row["is_cur"]:
            row["verdict"] = "cur"
            if row["kv_pool_gib"] is not None and row["kv_pool_gib"] <= 0:
                row["cur_kv_neg"] = True  # 当前口径算出 KV≤0 说明口径本身有偏差，报告要显式标出
        elif row.get("ep_group_unknown") or row.get("pp_unknown"):
            row["verdict"] = "unknown"
        elif row["kv_pool_gib"] is None:
            # 缺可用口径/权重 → 「未测」不是「不可行」：置标志位，报告导向「缺关键输入」文案
            row["verdict"] = "unknown"
            row["kv_unverified"] = True
        elif row["kv_pool_gib"] <= 0:
            row["verdict"] = "oom"
        elif row["per_dp_tokens"] is None:
            row["verdict"] = "unknown"
        elif rt["max_model_len"] is None:
            # 缺 max_model_len 时无法校验满长请求，不能直接标 ok 并给启动建议
            row["verdict"] = "unknown"
            row["len_unverified"] = True
        elif row["per_dp_tokens"] < rt["max_model_len"]:
            row["verdict"] = "len"
        else:
            row["verdict"] = "ok"
        rows.append(row)
    return rows


def recommend(rows):
    """推荐候选：先要求 verdict=ok，再要求能算出集群 DP（缺 --total-cards 时宁可不推荐）。"""
    ok = [r for r in rows if r["verdict"] == "ok"]
    if not ok:
        return None
    with_dp = [r for r in ok if r.get("cluster_dp")]
    if not with_dp:
        return None  # 缺集群卡数，DP 收益无从比较
    return max(with_dp, key=lambda r: (r["cluster_dp"], r["per_dp_tokens"] or 0))


# ---------------------------------------------------------------- 报告
def gen_report(weights, rt, rows, calc_cur, diff, applied, degrade_note=None):
    L = []
    cur = next((r for r in rows if r["is_cur"]), None)
    rec = recommend(rows)
    mlen = rt["max_model_len"]
    model_name = (rt["model_dir"] or "").rstrip("/").split("/")[-1] or "未知模型"

    L.append(f"# TP→DP 扩容可行性分析报告 — {model_name}")
    L.append("")
    L.append(f"- 生成: {now_str()} ｜ 工具: {TOOL} ｜ 主机: {rt['host'] or '—'}")
    L.append(f"- 权重来源: {weights['source'] if weights else '无（退化模式：仅报现状）'}")
    L.append(f"- 进程参数来源: {rt.get('param_source', 'unknown')}")
    L.append("")

    # ---- 0) 降级告警横幅：先告诉读者哪些结论不可信
    warn = []
    if weights is None:
        warn.append("缺权重分桶（--model-dir/--weights-json）→ 退化模式：仅报现状，候选 TP 未测算"
                    + (f"（{degrade_note}）" if degrade_note else ""))
    if rt.get("kv_bpt") is None:
        warn.append("缺 KV/token 实测来源 → token 级容量未校验，候选表只能判断 KV 池正负")
    if "总显存×利用率" in (rt.get("usable_src") or ""):
        warn.append("可用口径为推算（缺日志 Desired/权重/KV 行），绝对值可能偏差 2-3 GiB")
    if rt.get("param_source") == "ps":
        warn.append("进程参数来自宿主机 ps（能看到所有容器进程，可能拿错实例），建议补 --script 重采")
    if "假设启用" in (rt.get("ep_src") or ""):
        warn.append("facts 缺 EP_ENABLED → 按 EP 启用假设处理；若实际未启用 EP，专家按 TP 切，结论不同")
    if rt.get("ep_enabled") and not rt.get("ep_group"):
        warn.append("EP 组大小未知（缺 TP/DP 且未传 --ep-group）→ 专家切分口径未定，候选一律 unknown")
    if (rt.get("pp") or 1) > 1:
        warn.append("PP>1：候选每卡权重按最重流水线阶段核算" +
                    ("；缺层数无法分阶段核算，候选标 unknown" if not rt.get("layers_on_card") else ""))
    if not mlen:
        warn.append("缺 max_model_len → 满长请求校验未做，候选一律 unknown；"
                    "补 --script 重采或 --overrides max_model_len 后重跑再推荐")
    if rt.get("dp_cur") is None:
        warn.append("缺 DP_SIZE → 当前 DP 未采集，DP 基线/收益倍数不计算（不拿集群容量反推冒充实测）")
    if rt.get("usable_gib") is None:
        warn.append("缺可用口径（无 npu-smi 总显存且无日志预算行）→ 候选 KV 池未测算、一律 unknown（是「未测」，不是「不可行」）")
    if warn:
        L.append("> ⚠️ **降级告警**：" + "；".join(warn))
        L.append("")

    # ---- 1 结论
    L.append("## 1. 结论")
    L.append("")
    if weights is None:
        L.append("> ⚠️ 退化模式：缺模型目录/权重分桶数据，只能报告现状，无法扫描候选 TP。"
                  "补 `--model-dir` 或 `--weights-json` 后重跑。")
        L.append("")
    if rec is not None:
        # 当前 DP 一律用 facts 实测 DP_SIZE（缺 DP_SIZE 不算倍数，不拿集群容量反推冒充实测）
        cur_dp = rt.get("dp_cur")
        gain = (rec["cluster_dp"] / cur_dp) if cur_dp else None
        cap_chg = ((rec["per_dp_tokens"] - cur["per_dp_tokens"]) / cur["per_dp_tokens"] * 100) \
            if cur and cur.get("per_dp_tokens") and rec.get("per_dp_tokens") else None
        kv_gain = (rec["cluster_kv_tokens"] / cur["cluster_kv_tokens"]) \
            if cur and cur.get("cluster_kv_tokens") and rec.get("cluster_kv_tokens") else None
        L.append("| 项 | 值 |")
        L.append("| --- | --- |")
        if cur:
            L.append(f"| 结论 | ✅ 可行：TP {rt['tp_cur']}→{rec['tp']}，"
                     f"DP {cur_dp if cur_dp else '未采集'}→{rec['cluster_dp']}"
                     f"（×{fmt(gain, 1) if gain else '—'}），推荐 **TP{rec['tp']}** |")
        else:
            L.append(f"| 结论 | ✅ 可行（无当前运行基线，未做对比）：推荐 **TP{rec['tp']}**，"
                      f"集群 DP {rec['cluster_dp']} |")
        if cur:
            L.append(f"| 每卡权重 | {fmt(cur['weights_gib'])} → {fmt(rec['weights_gib'])} GiB"
                     f"（{'+' if rec['weights_gib'] >= cur['weights_gib'] else ''}"
                     f"{fmt(rec['weights_gib'] - cur['weights_gib'])}，不是按 TP 倍涨）|")
            L.append(f"| KV 池/卡 | {fmt(cur['kv_pool_gib'])} → {fmt(rec['kv_pool_gib'])} GiB |")
        if cap_chg is not None:
            ok_len = f"，≥ max_model_len={tok(mlen)}" if mlen else ""
            L.append(f"| 每 DP token 容量 | {tok(cur['per_dp_tokens'])} → {tok(rec['per_dp_tokens'])}"
                     f"（{cap_chg:+.1f}%{ok_len}）|")
        elif cur is None and rec.get("per_dp_tokens"):
            ok_len = f"，≥ max_model_len={tok(mlen)}" if mlen else ""
            L.append(f"| 每 DP token 容量 | {tok(rec['per_dp_tokens'])}{ok_len} |")
        if kv_gain is not None:
            L.append(f"| 集群 KV 总量 | {tok(cur['cluster_kv_tokens'])} → {tok(rec['cluster_kv_tokens'])}"
                     f" token（×{fmt(kv_gain, 2)}）|")
        L.append("")
    elif weights is not None:
        if any(r["verdict"] == "ok" for r in rows):
            L.append("> ⚠️ 存在容量可行的候选，但缺集群总卡数（`--total-cards`）或当前 TP 基线，"
                     "无法给出 DP 收益推荐；补齐后重跑。")
        elif any(r["verdict"] == "unknown" and (r.get("len_unverified") or r.get("ep_group_unknown")
                                                or r.get("pp_unknown") or r.get("kv_unverified"))
                 for r in rows):
            L.append("> ⚠️ 候选未完成测算（缺关键输入：max_model_len / EP 组 / 分阶段层数 / 可用口径），"
                      "判定为 unknown，不生成推荐与启动建议；补齐（--script 重采或 --overrides）后重跑。")
        else:
            L.append("> ❌ 在当前显存口径下没有可行候选（KV 池不足或放不下满长请求）。"
                      "见第 4 节逐项判定；可尝试更小的 `--tp-candidates` 或释放激活/其他开销。")
        L.append("")

    # ---- 2 运行态事实
    L.append("## 2. 运行态事实")
    L.append("")
    L.append("| 项 | 值 | 来源 |")
    L.append("| --- | --- | --- |")
    tc_src = "推算 TP×DP×PP" if rt["total_cards"] and rt["tp_cur"] else "—"
    L.append(f"| 集群总卡数 | {rt['total_cards'] if rt['total_cards'] else '—'} | {rt['src'].get('total_cards', '—')} |")
    L.append(f"| EP 组大小 | {rt['ep_group'] if rt['ep_group'] else '—'} | {rt['src'].get('ep_group', '—')} |")
    L.append(f"| 当前 TP/DP/PP | {rt['tp_cur'] or '—'} / {rt['dp_cur'] or '—'} / {rt['pp'] or '—'} | 实测（来源: {rt.get('param_source', 'unknown')}） |")
    L.append(f"| EP enabled | {'是' if rt['ep_enabled'] else '否'} | {rt.get('ep_src', '实测(ps)')} |")
    L.append(f"| 单卡总显存 | {fmt(rt['total_gib'])} GiB | {rt['src']['total_gib']} |")
    L.append(f"| 单卡已用 | {fmt(rt['used_gib'])} GiB | {rt.get('used_src', '—')}（含全部占用）|")
    if rt.get("hbm_rate") is not None:
        L.append(f"| HBM 使用率 | {rt['hbm_rate']}% | 实测 npu-smi（老卡 Rate 兜底口径）|")
    L.append(f"| gpu_memory_utilization | {rt['util']} | {rt['util_src']} |")
    L.append(f"| 单卡可用口径 | {fmt(rt['usable_gib'])} GiB | {rt['usable_src']} |")
    L.append(f"| 激活 / 其他开销 | {fmt(rt['act_gib'], 1)} / {fmt(rt['other_gib'], 1)} GiB | {rt['act_src']} / {rt['oth_src']} |")
    L.append(f"| max_model_len | {tok(mlen)} | {'实测' if mlen else '缺失（容量未校验）'} |")
    L.append(f"| 日志每卡权重 | {fmt(rt['log_weights_gib'])} GiB | {'日志实测 Model weights take / Actual usage for weights' if rt['log_weights_gib'] is not None else '缺失'} |")
    L.append(f"| 日志 KV 池/卡 | {fmt(rt['log_kv_gib'])} GiB | {'日志实测 Available KV cache / Actual usage for kv cache' if rt['log_kv_gib'] is not None else '缺失'} |")
    L.append(f"| 日志 Desired 总预算 | {fmt(rt.get('log_desired_gib'))} GiB | {'日志实测 Desired usage(total)' if rt.get('log_desired_gib') is not None else '缺失（可用口径降级）'} |")
    L.append(f"| 日志 Actual 总占用 | {fmt(rt.get('log_actual_total_gib'))} GiB | {'日志实测 Total usage' if rt.get('log_actual_total_gib') is not None else '缺失'} |")
    L.append(f"| 日志 Non-Torch | {fmt(rt.get('log_non_torch_gib'))} GiB | {'日志实测 Non-Torch memory' if rt.get('log_non_torch_gib') is not None else '缺失'} |")
    L.append(f"| 日志 torch peak | {fmt(rt.get('log_torch_peak_gib'))} GiB | {'日志实测 torch peak memory' if rt.get('log_torch_peak_gib') is not None else '缺失'} |")
    L.append(f"| 日志 KV token 数 | {tok(rt['log_kv_tokens'])} | {rt.get('kv_tokens_src') or '缺失'} |")
    L.append(f"| KV cache dtype | {rt.get('kv_dtype') or '—'} | {rt.get('kv_dtype_src')} |")
    L.append(f"| 每 token 每卡 KV | {tok(rt['kv_bpt'])} B | {rt['kv_bpt_src']} |")
    L.append(f"| MLA(每卡复制 KV) | {rt['mla']} | {rt['mla_src']} |")
    L.append(f"| 模型目录 | {rt['model_dir'] or '—'} | 实测 |")
    if rt["total_cards"] and rt["tp_cur"] and rt["dp_cur"]:
        expect = rt["tp_cur"] * rt["dp_cur"] * (rt["pp"] or 1)
        if rt["total_cards"] != expect:
            L.append(f"| ⚠️ 卡数校验 | --total-cards {rt['total_cards']} ≠ TP×DP×PP {expect} | 报警 |")
    L.append("")

    # ---- 3 权重三分类
    L.append("## 3. 权重三分类拆解")
    L.append("")
    if weights is not None:
        tp_cur, ep_div = rt["tp_cur"] or 1, rt["ep_div_cur"]
        tot = weights["rep_gib"] + weights["tp_gib"] + weights["ep_gib"] + weights["extra_gib"]
        if tot <= 0:
            L.append("> 桶总量为 0（safetensors 未读到张量？），分桶不可用——"
                     "检查模型目录是否有 *.safetensors，或改用 --weights-json。")
            L.append("")
        L.append("| 桶 | 总量 GiB | 每卡@TP{} GiB | 占比 | 识别规则 |".format(
            rt["tp_cur"] if rt["tp_cur"] else "—"))
        L.append("| --- | --- | --- | --- | --- |")
        rows3 = [
            ("rep", weights["rep_gib"], weights["rep_gib"]),
            ("tp", weights["tp_gib"], weights["tp_gib"] / tp_cur),
            ("ep", weights["ep_gib"], weights["ep_gib"] / ep_div),
            ("extra", weights["extra_gib"], weights["extra_gib"]),
        ]
        for b, tot_g, per in rows3:
            L.append(f"| {BUCKET_CN[b]} | {fmt(tot_g)} | {fmt(per)} | "
                     f"{fmt(tot_g / tot * 100 if tot else 0, 1)}% | {BUCKET_RULE[b]} |")
        per_sum = sum(per for _, _, per in rows3)
        L.append(f"| **每卡合计 [计算]** | — | **{fmt(per_sum)}** | 100% | 复制+额外+TP桶/tp+EP桶/ep_group |")
        L.append(f"| 日志实测每卡 | — | {fmt(rt['log_weights_gib'])} | | Model weights take |")
        if diff is None:
            L.append(f"| 校准差额 | — | 未校准（日志缺失） | | 建议补 --log 后重跑 |")
        else:
            L.append(f"| 校准差额 | — | {fmt(diff)} | | "
                     f"{'绝对差≤5%，未回灌' if abs(diff) <= 0.05 * (rt['log_weights_gib'] or 1) else '已回灌进额外桶 ' + fmt(applied) + '（MTP/量化 scale 等漏桶）'} |")
        L.append("")
        if (rt["pp"] or 1) > 1:
            L.append(f"PP={rt['pp']}：上面「每卡@TP」列按全层口径仅作参考；"
                     "候选试算表的每卡权重按**最重流水线阶段**核算（embedding 归首段、"
                     "lm_head/最终 norm/MTP 层归末段），见第 4 节。")
            L.append("")
        # Top 张量表
        if weights["tensors"]:
            L.append("Top 张量（前 12，按字节）：")
            L.append("")
            L.append("| 张量 | 桶 | 总量 GiB | 占比 |")
            L.append("| --- | --- | --- | --- |")
            for name, nb, b in sorted(weights["tensors"], key=lambda t: -t[1])[:12]:
                L.append(f"| `{name}` | {BUCKET_CN[b]} | {fmt(nb / GIB)} | {fmt(nb / GIB / tot * 100 if tot else 0, 1)}% |")
            L.append("")
            L.append(f"（共 {weights['n_tensors']} 个张量；W4A8 打包字节按 header nbytes 计）")
            L.append("")
    else:
        L.append("> 无权重分桶数据（缺模型目录与 --weights-json）。只有日志总每卡权重，无法区分随 TP 变化的部分。")
        L.append("")

    # ---- 4 候选 TP 试算
    L.append("## 4. 候选 TP 试算表")
    L.append("")
    if any(r.get("verdict") not in ("unknown",) or r.get("kv_pool_gib") is not None for r in rows):
        L.append("| TP | 组内DP(ep_group/tp) | 集群DP(可容纳) | 每卡权重 GiB | KV池/卡 GiB | 每DP容量 token | 可行性 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            tag = "（当前）" if r["is_cur"] else ""
            verdict_txt = VERDICT_CN[r["verdict"]]
            if r.get("cur_kv_neg"):
                verdict_txt = "—（当前，⚠️KV池≤0：口径需复核）"
            if r.get("len_unverified"):
                verdict_txt = "❓ 缺 max_model_len 未校验"
            if r.get("ep_group_unknown"):
                verdict_txt = "❓ EP 组未知未测算"
            if r.get("pp_unknown"):
                verdict_txt = "❓ PP 分阶段未知"
            if r.get("kv_unverified"):
                verdict_txt = "❓ 缺可用口径未测"
            weights_txt = fmt(r.get("weights_gib"))
            if r.get("weights_gib") is not None and (rt["pp"] or 1) > 1 and not r.get("pp_unknown"):
                weights_txt += "（最重阶段）"
            L.append(f"| {r['tp']}{tag} | {r.get('dp_per_group') if r.get('dp_per_group') is not None else '—'} | "
                     f"{tok(r.get('cluster_dp')).replace(',', '') if r.get('cluster_dp') else '—'} | "
                     f"{weights_txt} | {fmt(r.get('kv_pool_gib'))} | "
                     f"{tok(r.get('per_dp_tokens'))} | {verdict_txt} |")
        L.append("")
        L.append(f"判定口径：KV池/卡 > 0 且 每DP容量 ≥ max_model_len{'' if not mlen else ' = ' + tok(mlen)}"
                 f"（缺 max_model_len 时候选一律 unknown，不给 ok）。")
        L.append("「集群DP(可容纳)」= 集群总卡数 ÷ (TP×PP) 的容量口径；"
                 "「当前 DP」基线一律取 facts 实测 DP_SIZE（见第 2 节），不用容量反推值。")
        L.append(f"每 DP token 容量来源：{rt['kv_bpt_src']}。")
        L.append("")
    else:
        L.append("> 数据不足，无法试算（见第 9 节假设与未测项）。")
        L.append("")

    # ---- 5 单卡容量校验
    L.append("## 5. 单卡容量校验（分项相加 vs 可用）")
    L.append("")
    L.append("| TP | 权重 | 激活 | 其他 | KV池 | 合计 | 可用 | 差 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        if r.get("kv_pool_gib") is None and r.get("weights_gib") is None:
            continue
        w, kv = r.get("weights_gib"), r.get("kv_pool_gib")
        s = (w or 0) + rt["act_gib"] + rt["other_gib"] + (kv or 0)
        L.append(f"| {r['tp']}{'（当前）' if r['is_cur'] else ''} | {fmt(w)} | {fmt(rt['act_gib'], 1)} | "
                 f"{fmt(rt['other_gib'], 1)} | {fmt(kv)} | {fmt(s)} | {fmt(rt['usable_gib'])} | "
                 f"{fmt(rt['usable_gib'] - s) if rt['usable_gib'] is not None else '—'} |")
    L.append("")

    # ---- 6 DP 扩容收益
    L.append("## 6. DP 扩容收益")
    L.append("")
    L.append("| TP | 每DP卡数 | 集群DP(可容纳) | 组内DP | 每DP容量 token | 集群KV总量 token | vs当前 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        if not r.get("cluster_dp"):
            continue
        # 基线倍数以 facts 实测 DP_SIZE 为基准（缺 DP_SIZE 时不算，不拿容量反推冒充实测）
        ratio = (r["cluster_dp"] / rt["dp_cur"]) if (rt.get("dp_cur") and not r["is_cur"]) else None
        ratio_txt = ('×' + fmt(ratio, 2)) if ratio else ("1.00（实测 DP_SIZE）" if r["is_cur"] and rt.get("dp_cur") else "—")
        L.append(f"| {r['tp']}{'（当前）' if r['is_cur'] else ''} | {r['tp'] * (rt['pp'] or 1)} | "
                 f"{r['cluster_dp']} | {r.get('dp_per_group') or '—'} | {tok(r.get('per_dp_tokens'))} | "
                 f"{tok(r.get('cluster_kv_tokens'))} | {ratio_txt} |")
    if rt["mla"]:
        L.append("")
        L.append("MLA：KV 不随 TP 切分，每 DP 容量≈每卡 KV 池；集群 KV 总量随 DP 数近似线性增长。")
    else:
        L.append("")
        L.append("MHA：KV 随 TP 切分，TP 越小每卡每 token KV 越大，每 DP 容量与 TP 乘积相关。")
    L.append("")

    # ---- 7 风险与前提
    L.append("## 7. 风险与前提")
    L.append("")
    if rec is not None and cur is not None and cur.get("per_dp_tokens") and rec.get("per_dp_tokens"):
        chg = (rec["per_dp_tokens"] - cur["per_dp_tokens"]) / cur["per_dp_tokens"] * 100
        L.append(f"- **每 DP KV 容量 {chg:+.1f}%**：单 DP 能并发的满长请求数下降；max_model_len 边界余量见第 4 节。")
    L.append(f"- **带宽/通信**：DP×{('↑' if rec else '=')} 后 EP all-to-all 与 DP 广播的通信对数增加，"
             f"跨机带宽是否成为新瓶颈需压测验证（带宽/负载指标另行采集，本 skill 不覆盖）。")
    L.append("- **PD 分离**：P/D 两侧 max_model_len 与 KV 需求不同，需分别跑本测算再合并结论；D 侧另有 KV 接收 buffer。")
    L.append("- **KV 传输**：Mooncake/AscendStore 的 tp/dp 必须与新拓扑严格一致（错配表现为 KV 路由错乱/丢 KV）；"
             "这是部署变更的一部分，由部署流程统一处理，本 skill 只读不部署。")
    L.append(f"- **碎片与保留**：CANN/HCCL buffer、大块分配碎片未计入（保留口径 {rt['reserved_gib']} GiB 为"
             f"{'假设' if '假设' in rt['res_src'] else '给定'}值）；实际 KV 池以启动日志为准。")
    L.append("- **本测算为静态容量口径**：不预测吞吐/时延变化，性能结论需上板压测。")
    L.append("")

    # ---- 8 建议启动参数 diff
    L.append("## 8. 建议启动参数 diff（仅文本，不执行）")
    L.append("")
    if rec is not None:
        L.append("```diff")
        if rt["tp_cur"]:
            L.append("  # 当前 → 建议（TP %s → %s）" % (rt["tp_cur"], rec["tp"]))
            L.append("- --tensor-parallel-size %s" % rt["tp_cur"])
        else:
            L.append("  # 建议（当前 TP 未知，无法给出前后对比）")
        L.append("+ --tensor-parallel-size %s" % rec["tp"])
        # 当前 DP：只用 facts 实测 DP_SIZE；缺 DP_SIZE 明说未采集，不拿集群容量反推冒充实测
        if rt["dp_cur"]:
            L.append("- --data-parallel-size %s          # 现状 = 实测 DP_SIZE（单实例口径）" % rt["dp_cur"])
        else:
            L.append("- --data-parallel-size ?           # 当前 DP 未采集（facts 缺 DP_SIZE），请人工确认现状")
        if rt["ep_enabled"] and rec.get("dp_per_group"):
            # EP 启用时一条启动命令只起一个 EP 组：命令里填组内 DP，集群 DP 用 多组×组内DP 表达
            L.append("+ --data-parallel-size %s          # 组内 DP = ep_group %s ÷ TP%s（单组启动命令）" % (
                rec["dp_per_group"], rt["ep_group"], rec["tp"]))
            groups = (rt["total_cards"] // rt["ep_group"]) if (rt["total_cards"] and rt["ep_group"]) else None
            if groups:
                L.append("  # 集群布局: %s 卡 ÷ %s/组 = %s 组 × 组内 %s DP = 集群 %s DP" % (
                    rt["total_cards"], rt["ep_group"], groups, rec["dp_per_group"], rec["cluster_dp"]))
        else:
            L.append("+ --data-parallel-size %s          # %s / (%s×%s)" % (
                rec["cluster_dp"], rt["total_cards"], rec["tp"], rt["pp"] or 1))
        if rt["ep_enabled"]:
            L.append("  --enable-expert-parallel          # 保持；EP 组仍为 %s = %s×%s" % (
                rt["ep_group"], rec["tp"], rec.get("dp_per_group")))
        L.append("  # 其余参数沿用；卡分配/端口/分流等部署细节由部署侧统一安排（本 skill 只读不部署）")
        L.append("```")
        L.append("")
        L.append("**执行前须用户确认；本 skill 只读，不改动任何线上配置。**")
        L.append("")
    else:
        L.append("无可行候选，不生成参数 diff。")
        L.append("")

    # ---- 9 假设与未测项
    L.append("## 9. 假设与未测项")
    L.append("")
    missing = []
    if rt["total_gib"] is None:
        missing.append("NPU*_TOTAL_MB / NPU_TOTAL_MB（npu-smi 单卡显存，任意卡号均可被识别）")
    if rt["tp_cur"] is None:
        missing.append("TP_SIZE（vLLM 进程参数）")
    if rt["dp_cur"] is None:
        missing.append("DP_SIZE（当前 DP 实测；缺失时 DP 基线/倍数/参数 diff 的现状行按未采集处理）")
    if rt["log_weights_gib"] is None:
        missing.append("LOG_MODEL_WEIGHTS_GIB（启动日志 Model weights take）")
    if rt["log_kv_gib"] is None:
        missing.append("LOG_AVAILABLE_KV_GIB（启动日志 Available KV cache）")
    if not rt["log_kv_tokens"]:
        missing.append("LOG_KV_TOKENS（启动日志 GPU KV cache size）")
    if weights is None:
        missing.append("模型目录/权重分桶（候选扫描不可用）")
    if missing:
        L.append("缺失数据：" + "；".join(missing) + "。对应结论项已按假设处理。")
        L.append("")
    L.append(f"- gpu_memory_utilization = {rt['util']}（{rt['util_src']}）")
    L.append(f"- 进程参数来源 = {rt.get('param_source', 'unknown')}"
             f"{'（ps 口径可能混入其他容器实例）' if rt.get('param_source') == 'ps' else ''}")
    L.append(f"- 单卡可用口径：{rt['usable_src']}")
    L.append(f"- 单卡保留 {rt['reserved_gib']} GiB（{rt['res_src']}）")
    L.append(f"- 激活 {fmt(rt['act_gib'], 1)} GiB、其他 {fmt(rt['other_gib'], 1)} GiB（{rt['act_src']} / {rt['oth_src']}）")
    L.append(f"- MLA = {rt['mla']}（{rt['mla_src']}）——MHA 模型 KV 随 TP 切，结论会不同")
    L.append(f"- 每 token KV {tok(rt['kv_bpt'])} B（{rt['kv_bpt_src']}；config 公式未含 DSA indexer 等附加缓存，实测反推优先）")
    L.append("- 未测项：跨机带宽、EP all-to-all 时延、KV 传输吞吐、碎片率、PD 分离 D 侧 buffer。")
    L.append("")
    L.append("---")
    L.append("")
    L.append(f"*本报告由 {TOOL} 生成；数字来源标记：实测 / 日志推算 / 计算 / 假设 / 输入。*")
    return "\n".join(L) + "\n"


def gen_json(weights, rt, rows, calc_cur, diff):
    rec = recommend(rows)
    return {
        "tool": TOOL, "generated_at": now_str(),
        "model_dir": rt["model_dir"], "param_source": rt.get("param_source"),
        "current": {"tp": rt["tp_cur"], "dp": rt["dp_cur"], "pp": rt["pp"],
                    "ep_enabled": rt["ep_enabled"], "ep_group": rt["ep_group"],
                    "total_cards": rt["total_cards"],
                    "per_card_total_gib": rt["total_gib"],
                    "usable_gib": rt["usable_gib"], "usable_source": rt["usable_src"],
                    "log_weights_gib": rt["log_weights_gib"],
                    "log_kv_gib": rt["log_kv_gib"],
                    "log_desired_total_gib": rt.get("log_desired_gib"),
                    "log_actual_total_gib": rt.get("log_actual_total_gib")},
        "weights": None if weights is None else {
            "replicated_gib": weights["rep_gib"], "tp_gib": weights["tp_gib"],
            "ep_gib": weights["ep_gib"], "extra_gib": weights["extra_gib"],
            "source": weights["source"], "calculated_per_card_gib": calc_cur,
            "calibration_diff_gib": diff},
        "kv_bytes_per_token": rt["kv_bpt"], "mla": rt["mla"],
        "max_model_len": rt["max_model_len"],
        "candidates": [
            {"tp": r["tp"], "dp_per_group": r.get("dp_per_group"),
             "cluster_dp": r.get("cluster_dp"), "per_card_weights_gib": r.get("weights_gib"),
             "kv_pool_gib": r.get("kv_pool_gib"), "per_dp_tokens": r.get("per_dp_tokens"),
             "cluster_kv_tokens": r.get("cluster_kv_tokens"), "verdict": r["verdict"]}
            for r in rows],
        "recommended": None if rec is None else {
            "tp": rec["tp"], "cluster_dp": rec["cluster_dp"],
            "dp_per_group": rec.get("dp_per_group"),
            "current_dp_measured": rt.get("dp_cur")},
    }


# ---------------------------------------------------------------- dump-plan
def dump_plan():
    """打印测算计划与输入清单。不依赖任何输入，可单独运行（--dump-plan）。"""
    print(f"""{TOOL} — 测算计划（--dump-plan）

【回答的问题】
  1) 单卡显存里装了什么（权重三分类 + KV + 激活 + 其他）
  2) 当前 TP 在显存约束内能不能切小（TP 切小 = DP 变多）
  3) 切完收益多少、坑在哪

【核心算式】
  每卡权重(tp)   = 复制桶 + 额外桶(未归类,每卡复制) + TP桶/tp + EP桶/ep_group
  KV 池/卡(tp)   = 单卡可用 − 每卡权重(tp) − 激活 − 其他
  每 DP token 容量 = KV池/卡 ÷ 每 token 每卡 KV 字节
  集群 DP 数     = 总卡数 ÷ (tp × pp)；组内 DP = ep_group ÷ tp（须整除）
  可行性         = KV池 > 0 且 每 DP 容量 ≥ max_model_len

【输入清单（collect_env.sh 采集，宿主机执行）】
  必需   机器/SSH；vLLM 启动日志 --log（权重/KV/Desired 行）；启动脚本 --script（比 ps 可靠）
  推荐   模型目录 --model（config.json + *.safetensors，自动分桶）；集群总卡数 --total-cards
  可选   --docker 容器名（日志/模型在容器里时自动 docker exec 读）；--ep-group（默认当前 tp）

【facts.env 关键键（缺失项会降级并在报告里告警）】
  NPU_TOTAL_MB（或任意 NPU<id>_TOTAL_MB，与采集卡号无关）/ TP_SIZE / DP_SIZE /
  LOG_MODEL_WEIGHTS_GIB / LOG_AVAILABLE_KV_GIB
  LOG_DESIRED_TOTAL_GIB（可用口径第 1 优先）/ LOG_KV_TOKENS（token 容量第 1 优先）
  EP_GROUP_SIZE（采集端按实例 TP×DP 推断）/ MAX_MODEL_LEN（缺失时候选一律 unknown）
  PARAM_SOURCE=script|ps（ps 可能混入其他容器实例）

【口径优先级】
  单卡可用: 日志 Desired(total) > 日志权重+KV+反推开销 > 总显存×util−3.6G(推算,偏差2-3G)
  KV/token: 日志 KV池÷token数 > Maximum concurrency×max_model_len > config 公式(漏 DSA,约低8%)
  非KV开销: 日志 Total−权重−KV 反推 > 默认 激活2.5G+其他1.8G(假设)

【输出】
  9 段固定结构 Markdown（结论/事实/三分类/候选试算/容量校验/DP收益/风险/参数diff/假设）
  + JSON；数字全部带 GiB 单位与来源标记（实测/日志推算/计算/假设/输入）

【默认值】
  utilization 0.90；reserved 3.6 GiB；激活 2.5 GiB；其他 1.8 GiB；
  候选 TP = ep_group 的约数(≥4) + 当前 TP

【示例】
  python analyze_feasibility.py --facts facts.env --model-dir /data/models/GLM-5.2-W4A8 \\
      --total-cards 160 --ep-group 32 --tp-candidates 32,16,8 \\
      --out feasibility.md --json feasibility.json
""")


# ---------------------------------------------------------------- 主流程
def run(facts_path, model_dir, weights_json, total_cards, ep_group,
        tp_candidates, out, json_out, overrides):
    facts = parse_env(facts_path) if facts_path else {}
    weights = None
    if model_dir:
        weights = load_model_info(model_dir)
    elif weights_json:
        weights = load_weights_json(weights_json)

    # 权重完整性核对（两项独立判定，任一不符都禁用分桶、只报告现状）：
    #   a) 解析失败/无分片：load_model_info 的 complete 标志（n_files>0 且 n_failed==0）
    #   b) 分片清单比对：facts SAFETENSORS_COUNT ≠ 本地读到的分片数。比对必须独立于 a)——
    #      本地少复制一个分片且其余全部可解析时 a) 仍为真，若比对藏在 a) 的分支里会被跳过，
    #      部分分片会静默进入校准与扫描（桶偏小、候选被高估）
    if weights is not None:
        sa_fact = _int(facts.get("SAFETENSORS_COUNT"), None) if facts else None
        if (sa_fact is not None and weights.get("n_shard_files") is not None
                and sa_fact != weights["n_shard_files"]):
            weights["complete"] = False
            weights["incomplete_reason"] = (f"分片清单不符：facts SAFETENSORS_COUNT={sa_fact} "
                                            f"≠ 本地读到 {weights['n_shard_files']}")
    degrade_note = None
    weights_calc = weights
    if weights is not None and not weights.get("complete", True):
        reason = weights.get("incomplete_reason") or "分片清单与读取结果不符"
        degrade_note = reason
        weights_calc = None
        print(f"# WARN: 权重分桶不可用（{reason}），只报告现状，不扫候选 TP", file=sys.stderr)

    rt = build_runtime(_ArgsProxy(total_cards, ep_group), facts, weights)

    # overrides
    if overrides:
        known = {"activation_gib", "act_gib", "other_gib", "reserved_gib", "utilization",
                 "max_model_len", "kv_bytes_per_token", "rep_gib", "tp_gib", "ep_gib", "extra_gib"}
        for k in overrides:
            if k not in known:
                print(f"# WARN: 未知的覆盖键 {k!r}（支持: {', '.join(sorted(known))}），已忽略",
                      file=sys.stderr)
        for k, v in overrides.items():
            if k in ("activation_gib", "act_gib"):
                rt["act_gib"] = float(v)
            elif k in ("other_gib",):
                rt["other_gib"] = float(v)
            elif k == "reserved_gib":
                rt["reserved_gib"] = float(v)
            elif k == "utilization":
                rt["util"] = float(v)
            elif k == "max_model_len":
                rt["max_model_len"] = int(v)
            elif k == "kv_bytes_per_token":
                rt["kv_bpt"] = float(v)
                rt["kv_bpt_src"] = "输入覆盖"
        if weights_calc:
            for k, v in overrides.items():
                if k in weights_calc and k in ("rep_gib", "tp_gib", "ep_gib", "extra_gib"):
                    weights_calc[k] = float(v)
        # 覆盖后重算可用口径（Desired 行是 vLLM 自己的预算，覆盖时不推翻它）；
        # path3（总显存×util−保留）也必须按覆盖后的 util/reserved 重算，否则改 utilization 不生效
        if rt["log_desired_gib"] is None:
            if rt["log_weights_gib"] is not None and rt["log_kv_gib"] is not None:
                rt["usable_gib"] = rt["log_weights_gib"] + rt["log_kv_gib"] + rt["act_gib"] + rt["other_gib"]
                rt["usable_src"] = "日志推算+覆盖开销"
            elif rt["total_gib"] is not None:
                rt["usable_gib"] = rt["total_gib"] * rt["util"] - rt["reserved_gib"]
                rt["usable_src"] = "推算+覆盖 = 总显存×util − 保留（util/reserved 已按覆盖值）"

    if rt["tp_cur"] is None and not tp_candidates:
        sys.exit("错误：facts 缺 TP_SIZE 且未显式给 --tp-candidates；至少补一个。"
                 "（补了 --tp-candidates 仍缺 TP_SIZE 时：当前基线与 EP 组推断不可用，"
                 "报告会降级告警，不会拿集群容量反推冒充实测。）")

    # 候选 TP 输入卫生：剔除 <1 的无效项（防止除零/取模出错），全无效则报错退出
    if tp_candidates:
        tp_candidates = [t for t in tp_candidates if isinstance(t, int) and t >= 1]
        if not tp_candidates:
            sys.exit("错误：--tp-candidates 全部无效（须为 ≥1 的整数）。")

    cands = tp_candidates or default_candidates(rt["ep_group"], rt["tp_cur"])
    if rt["tp_cur"] and rt["tp_cur"] not in cands:
        cands = sorted(set(cands + [rt["tp_cur"]]), reverse=True)

    calc_cur, diff, applied = None, None, 0.0
    if weights_calc is not None and rt["tp_cur"]:
        calc_cur, diff, applied = calibrate(weights_calc, rt["log_weights_gib"],
                                            rt["tp_cur"], rt["ep_div_cur"])

    rows = scan_candidates(weights_calc, rt, cands)
    report = gen_report(weights_calc, rt, rows, calc_cur, diff, applied, degrade_note)

    if out:
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(report)
        except OSError as e:
            sys.exit(f"错误：无法写报告 {out}: {e}（检查输出目录是否存在/可写）")
        print(f"report  -> {out}")
    else:
        print(report)
    if json_out:
        try:
            with open(json_out, "w", encoding="utf-8") as f:
                json.dump(gen_json(weights_calc, rt, rows, calc_cur, diff), f, ensure_ascii=False, indent=2)
        except OSError as e:
            sys.exit(f"错误：无法写 JSON {json_out}: {e}（检查输出目录是否存在/可写）")
        print(f"json    -> {json_out}")
    return weights, rt, rows, report


class _ArgsProxy:
    def __init__(self, total_cards, ep_group):
        self.total_cards = total_cards
        self.ep_group = ep_group


def _load_overrides(s):
    if not s:
        return {}
    try:
        if os.path.isfile(s):
            return json.load(open(s, "r", encoding="utf-8"))
        return json.loads(s)
    except (ValueError, OSError) as e:
        sys.exit(f"错误：--overrides 不是合法 JSON（文件或内联字符串）: {e}")


# ---------------------------------------------------------------- 自测
def _copy_safetensors(src_dir, dst_dir):
    """复制样例 safetensors 到另一目录（构造"缺 config.json"等变体目录用）。"""
    import shutil as _sh
    for fp in _glob.glob(os.path.join(src_dir, "*.safetensors")):
        _sh.copy(fp, dst_dir)


def _synthetic_model_dir(tmp):
    """构造最小模型目录（config.json + 1 个 safetensors），验证 header 读取与分桶。"""
    md = os.path.join(tmp, "model")
    os.makedirs(md, exist_ok=True)
    cfg = {"num_hidden_layers": 78, "kv_lora_rank": 512, "qk_rope_head_dim": 64,
           "hidden_size": 64, "num_attention_heads": 4, "head_dim": 16,
           "num_key_value_heads": 2, "vocab_size": 100}
    with open(os.path.join(md, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    tensors = {
        "model.embed_tokens.weight": ("BF16", [100, 64], 12800),
        "model.layers.0.input_layernorm.weight": ("BF16", [64], 128),
        "model.layers.0.self_attn.q_a_proj.weight": ("BF16", [32, 64], 512),
        "model.layers.0.self_attn.q_b_proj.weight": ("BF16", [128, 32], 832),
        "model.layers.0.self_attn.kv_a_proj_with_mqa.weight": ("BF16", [32, 64], 512),
        "model.layers.0.mlp.gate.weight": ("BF16", [16, 64], 1216),
        "model.layers.0.mlp.experts.0.gate_proj.weight": ("I8", [32, 64], 2048),
        "model.layers.0.mlp.experts.1.down_proj.weight": ("I8", [64, 32], 2048),
        "model.layers.0.mlp.shared_expert.gate_proj.weight": ("BF16", [64, 64], 832),
        # MTP 层（层号 ≥ 78）：按普通层规则分桶——attention 主干→tp，experts→ep
        "model.layers.78.self_attn.q_b_proj.weight": ("BF16", [128, 32], 832),
        "model.layers.78.mlp.experts.0.gate_proj.weight": ("I8", [32, 64], 2048),
        "model.norm.weight": ("BF16", [64], 128),
        "lm_head.weight": ("BF16", [100, 64], 12800),
    }
    off, meta = 0, {}
    for name, (dt, shape, nb) in tensors.items():
        meta[name] = {"dtype": dt, "shape": shape, "data_offsets": [off, off + nb]}
        off += nb
    hdr = json.dumps(meta).encode("utf-8")
    with open(os.path.join(md, "tiny.safetensors"), "wb") as f:
        f.write(struct.pack("<Q", len(hdr)))
        f.write(hdr)
        f.write(b"\x00" * off)
    return md


def selftest():
    import shutil
    import tempfile

    facts = os.path.join(EXAMPLES, "facts.env.example")
    wj = os.path.join(EXAMPLES, "glm52_weights.json")
    # 自测报告写临时目录：不弄脏工作区、也不与 examples/glm52_report.md 重复入库
    # （样例报告只保留 glm52_report.md 一份，运行产物不入库）
    outdir = tempfile.mkdtemp(prefix="tpsplit_selftest_")
    out = os.path.join(outdir, "selftest_report.md")
    weights, rt, rows, _ = run(facts, None, wj, 160, 32, [32, 16, 8], out, None, None)

    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    by_tp = {r["tp"]: r for r in rows}
    r32, r8, r16 = by_tp.get(32), by_tp.get(8), by_tp.get(16)
    check("TP32 每卡权重≈22.00", r32 and abs(r32["weights_gib"] - 22.00) < 0.05,
          fmt(r32["weights_gib"]) if r32 else "missing")
    check("TP8 每卡权重≈22.90", r8 and abs(r8["weights_gib"] - 22.90) < 0.05,
          fmt(r8["weights_gib"]) if r8 else "missing")
    check("TP8 KV池≈26.80", r8 and abs(r8["kv_pool_gib"] - 26.80) < 0.05,
          fmt(r8["kv_pool_gib"]) if r8 else "missing")
    check("DP 5 -> 20 (x4)", r32 and r8 and r32["cluster_dp"] == 5 and r8["cluster_dp"] == 20,
          f"{r32['cluster_dp']} -> {r8['cluster_dp']}" if r32 and r8 else "missing")
    check("组内DP ep_group/tp 整除", r8 and r16 and r8["dp_per_group"] == 4 and r16["dp_per_group"] == 2,
          f"tp8:{r8['dp_per_group'] if r8 else '?'} tp16:{r16['dp_per_group'] if r16 else '?'}")
    check("KV/token≈89857B", rt["kv_bpt"] and abs(rt["kv_bpt"] - 89857) < 200,
          str(round(rt["kv_bpt"], 1)) if rt["kv_bpt"] else "None")
    check("TP8 每DP容量 ≥ max_model_len 131072",
          r8 and r8["per_dp_tokens"] and r8["per_dp_tokens"] >= 131072,
          tok(r8["per_dp_tokens"]) if r8 else "missing")
    check("三个候选全部可行", all(r["verdict"] == "ok" for r in rows if not r["is_cur"]) and
          r32 and r32["verdict"] == "cur",
          ",".join(r["verdict"] for r in rows))
    check("校准差额≈0（|diff|<0.05）", rt and rt["log_weights_gib"] is not None and
          abs((r32["weights_gib"]) - rt["log_weights_gib"]) < 0.05,
          f"calc {fmt(r32['weights_gib'])} vs log {rt['log_weights_gib']}")
    check("推荐 TP8", recommend(rows) and recommend(rows)["tp"] == 8,
          f"rec tp={recommend(rows)['tp'] if recommend(rows) else None}")
    check("报告文件已写出", os.path.isfile(out), out)

    # ---- Part 2: model-dir 路径（safetensors header 读取 + 分桶 + MTP + config KV 公式）
    tmp = tempfile.mkdtemp(prefix="tpsplit_st_")
    try:
        md = _synthetic_model_dir(tmp)
        f2 = os.path.join(tmp, "facts.env")
        with open(f2, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=1\nEP_ENABLED=1\n"
                    "NPU0_TOTAL_MB=65536\nGPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        w2, rt2, rows2, _ = run(f2, md, None, 160, 32, [32, 8],
                                os.path.join(tmp, "r.md"), None, None)
        # 期望分桶（字节）: rep=2496 tp=27264+832(MTP attn)=28096 ep=4096+2048(MTP专家)=6144 extra=0
        # MTP 层不再整层进复制桶（那会每卡虚增），与普通层同规则
        e = 1e-9
        check("model-dir: rep 桶=2496B", w2 and abs(w2["rep_gib"] - 2496 / GIB) < e,
              f"{w2['rep_gib'] * GIB:.0f}B")
        check("model-dir: tp 桶=28096B(含MTP attn)", w2 and abs(w2["tp_gib"] - 28096 / GIB) < e,
              f"{w2['tp_gib'] * GIB:.0f}B")
        check("model-dir: ep 桶=6144B(含MTP专家)", w2 and abs(w2["ep_gib"] - 6144 / GIB) < e,
              f"{w2['ep_gib'] * GIB:.0f}B")
        check("model-dir: extra=0(MTP 走正常分桶)", w2 and abs(w2["extra_gib"] - 0.0) < e,
              f"{w2['extra_gib'] * GIB:.0f}B")
        check("model-dir: MLA=True(config)", rt2 and rt2["mla"] is True, str(rt2["mla"]))
        # config 公式: (512+64)*2*78 = 89856
        check("model-dir: KV/token=89856B(config)", rt2 and rt2["kv_bpt"] == 89856,
              str(rt2["kv_bpt"]))
        # 可用口径走 path2: 64*0.9-3.6 = 54.0
        check("model-dir: 可用=54.0(path2)", rt2 and abs(rt2["usable_gib"] - 54.0) < 1e-6,
              fmt(rt2["usable_gib"]) if rt2["usable_gib"] is not None else "None")
        # TP8 每卡权重: 2496/GIB + 28096/8/GIB + 6144/32/GIB
        w_tp8 = 2496 / GIB + 28096 / 8 / GIB + 6144 / 32 / GIB
        r8b = {r["tp"]: r for r in rows2}.get(8)
        check("model-dir: TP8 每卡权重一致", r8b and abs(r8b["weights_gib"] - w_tp8) < e,
              f"{fmt(r8b['weights_gib']) if r8b else '?'} vs {fmt(w_tp8, 6)}")

        # ---- Part 3: 校准回灌 + Desired 优先 + Total 反推开销 + 并发反推 token 容量
        f3 = os.path.join(tmp, "facts3.env")
        with open(f3, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=1\nEP_ENABLED=1\n"
                    "NPU0_TOTAL_MB=65536\nGPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=10000\n"
                    "LOG_MODEL_WEIGHTS_GIB=1.00\nLOG_AVAILABLE_KV_GIB=1.00\n"
                    "LOG_DESIRED_TOTAL_GIB=8.00\nLOG_ACTUAL_TOTAL_GIB=4.00\n"
                    "LOG_MAX_CONCURRENCY=12\nPARAM_SOURCE=script\n")
        w3, rt3, rows3, _ = run(f3, md, None, 160, 32, [32, 8],
                                os.path.join(tmp, "r3.md"), None, None)
        # usable 优先级: Desired(8.0) > 日志推算(1+1+2+0=4.0) > path2(54.0)
        check("Desired 优先: usable=8.0", rt3 and abs(rt3["usable_gib"] - 8.0) < 1e-6,
              fmt(rt3["usable_gib"]) if rt3["usable_gib"] is not None else "None")
        # Total(4.0)−权重(1.0)−KV(1.0)=2.0 → 激活 min(2.5,2.0)=2.0，其他 0
        check("Total 反推开销: act=2.0 other=0",
              rt3 and abs(rt3["act_gib"] - 2.0) < 1e-6 and abs(rt3["other_gib"] - 0.0) < 1e-6,
              f"{rt3['act_gib']}/{rt3['other_gib']}" if rt3 else "?")
        # 校准回灌: 日志权重 1.0 与计算值(几 KB)差>5% → 回灌 extra → 每卡≈1.0
        r32c = {r["tp"]: r for r in rows3}.get(32)
        check("校准回灌: TP32 每卡权重≈日志 1.0",
              r32c and abs(r32c["weights_gib"] - 1.0) < 1e-6,
              fmt(r32c["weights_gib"]) if r32c else "?")
        # token 容量: 无 LOG_KV_TOKENS → Maximum concurrency(12)×max_model_len(10000)=120000
        check("并发反推: tokens=120000", rt3 and rt3["log_kv_tokens"] == 120000,
              str(rt3["log_kv_tokens"]) if rt3 else "?")
        # kv_bpt = 1.0 GiB / 120000 = 8947.85
        check("并发反推: KV/token≈8948B", rt3 and rt3["kv_bpt"] and abs(rt3["kv_bpt"] - 8947.85) < 1.0,
              str(round(rt3["kv_bpt"], 2)) if rt3["kv_bpt"] else "?")
        r8c = {r["tp"]: r for r in rows3}.get(8)
        check("Part3: TP8 可行(kv=8-1-2-0=5.0)", r8c and r8c["verdict"] == "ok",
              f"{r8c['verdict']}, kv={fmt(r8c['kv_pool_gib'])}" if r8c else "?")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- Part 4: --dump-plan 可单独运行（无需 facts/weights）
    import subprocess
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--dump-plan"],
                           capture_output=True, text=True, timeout=30)
        check("--dump-plan 独立运行", p.returncode == 0 and "测算计划" in p.stdout,
              f"rc={p.returncode}")
    except Exception as ex:  # noqa: BLE001
        check("--dump-plan 独立运行", False, str(ex))

    # ---- Part 5: 评审修复回归（NPU 别名/EP 推断/缺 mlen/缺 DP/MLA 未知/MHA 层数/PP 分阶段/覆盖重算）
    tmp2 = tempfile.mkdtemp(prefix="tpsplit_rev_")
    try:
        md = _synthetic_model_dir(tmp2)  # Part 2 的临时目录已清理，此处重建

        # 5a: 任意卡号 NPU 键扫描 + EP 组推断(TP×DP) + 缺 max_model_len → unknown
        f5 = os.path.join(tmp2, "facts5.env")
        with open(f5, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=8\nDP_SIZE=4\nPP_SIZE=1\nEP_ENABLED=1\n"
                    "NPU3_TOTAL_MB=65536\nNPU3_USED_MB=60000\n"
                    "GPU_MEMORY_UTILIZATION=0.90\n")
        _, rt5, rows5, _ = run(f5, None, wj, 32, None, [8, 4],
                               os.path.join(tmp2, "r5.md"), None,
                               {"kv_bytes_per_token": 89857})
        check("5a: NPU 任意卡号扫描总显存=64G", rt5 and abs(rt5["total_gib"] - 64.0) < 1e-6,
              fmt(rt5["total_gib"]) if rt5 and rt5["total_gib"] else "?")
        check("5a: EP 组推断 = 实例 TP×DP = 32", rt5 and rt5["ep_group"] == 32,
              f"ep_group={rt5['ep_group'] if rt5 else '?'}")
        r4_5 = next((r for r in rows5 if r["tp"] == 4), None)
        check("5a: 缺 max_model_len → unknown(不标 ok)", r4_5 and r4_5["verdict"] == "unknown"
              and r4_5.get("len_unverified"), str(r4_5["verdict"]) if r4_5 else "?")

        # 5b: 缺 DP_SIZE → EP 组未知 + 候选 unknown（不拍 tp、不崩）
        f5b = os.path.join(tmp2, "facts5b.env")
        with open(f5b, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=8\nPP_SIZE=1\nEP_ENABLED=1\nNPU_TOTAL_MB=65536\n"
                    "GPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        _, rt5b, rows5b, _ = run(f5b, None, wj, 32, None, [8, 4],
                                 os.path.join(tmp2, "r5b.md"), None, None)
        check("5b: 缺 DP_SIZE → EP 组未知(不假设=tp)", rt5b and rt5b["ep_group"] is None,
              f"ep_group={rt5b['ep_group'] if rt5b else '?'}")
        r4_5b = next((r for r in rows5b if r["tp"] == 4), None)
        check("5b: EP 组未知 → 候选 unknown 不崩", r4_5b and r4_5b["verdict"] == "unknown",
              str(r4_5b["verdict"]) if r4_5b else "?")

        # 5c: 只有 safetensors 没有 config.json → mla=None → 按 MLA 假设并告警
        md_nc = os.path.join(tmp2, "model_no_cfg")
        os.makedirs(md_nc, exist_ok=True)
        _copy_safetensors(md, md_nc)
        f5c = os.path.join(tmp2, "facts5c.env")
        with open(f5c, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=1\nEP_ENABLED=1\nNPU_TOTAL_MB=65536\n"
                    "GPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        w5c, rt5c, _, _ = run(f5c, md_nc, None, 160, 32, [32],
                              os.path.join(tmp2, "r5c.md"), None, None)
        check("5c: 缺 config.json → mla=None(未知)→假设 MLA",
              w5c and w5c.get("mla") is None and rt5c and rt5c["mla"] is True
              and "假设" in (rt5c["mla_src"] or ""),
              f"w.mla={w5c.get('mla') if w5c else '?'} rt.mla={rt5c['mla'] if rt5c else '?'}")

        # 5d: MHA config 公式按层折算（78 层）；缺层数 → 未知
        md_mha = os.path.join(tmp2, "model_mha")
        os.makedirs(md_mha, exist_ok=True)
        cfg_mha = {"num_hidden_layers": 78, "num_key_value_heads": 2, "head_dim": 16,
                   "hidden_size": 64, "num_attention_heads": 4}
        with open(os.path.join(md_mha, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg_mha, f)
        _copy_safetensors(md, md_mha)
        _, rt5d, _, _ = run(f5c, md_mha, None, 160, 32, [32],
                            os.path.join(tmp2, "r5d.md"), None, None)
        # 2×kv_heads(2)×head_dim(16)×2B×78层 ÷ tp(32) = 312
        check("5d: MHA KV/token 含层数=312B", rt5d and rt5d["kv_bpt"] == 312,
              str(rt5d["kv_bpt"]) if rt5d else "?")
        md_nl = os.path.join(tmp2, "model_no_layers")
        os.makedirs(md_nl, exist_ok=True)
        cfg_nl = dict(cfg_mha)
        cfg_nl.pop("num_hidden_layers")
        with open(os.path.join(md_nl, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg_nl, f)
        _copy_safetensors(md, md_nl)
        _, rt5e, _, _ = run(f5c, md_nl, None, 160, 32, [32],
                            os.path.join(tmp2, "r5e.md"), None, None)
        check("5d: 缺层数 → KV/token 未知", rt5e and rt5e["kv_bpt"] is None,
              str(rt5e["kv_bpt"]) if rt5e else "?")

        # 5e: PP=2 → 每卡权重按最重阶段核算
        f5f = os.path.join(tmp2, "facts5f.env")
        with open(f5f, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=2\nEP_ENABLED=1\nNPU_TOTAL_MB=65536\n"
                    "GPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        _, rt5f, rows5f, _ = run(f5f, md, None, 160, 32, [32],
                                 os.path.join(tmp2, "r5f.md"), None, None)
        # 最重阶段(stage0) = rep 2368 + tp 14464/32 + ep 4096/32 = 2948B（全层口径会虚高）
        w_pp = (2368 + 14464 / 32 + 4096 / 32) / GIB
        r32_5f = {r["tp"]: r for r in rows5f}.get(32)
        check("5e: PP=2 每卡权重=最重阶段 2948B",
              r32_5f and abs(r32_5f["weights_gib"] - w_pp) < 1e-9,
              f"{r32_5f['weights_gib'] * GIB:.0f}B" if r32_5f else "?")

        # 5f: 只有 config.json 的目录 → 权重不完整 → 只报现状（非当前行全 unknown）
        md_cfg_only = os.path.join(tmp2, "model_cfg_only")
        os.makedirs(md_cfg_only, exist_ok=True)
        with open(os.path.join(md_cfg_only, "config.json"), "w", encoding="utf-8") as f:
            json.dump(json.load(open(os.path.join(md, "config.json"), encoding="utf-8")), f)
        f5g = os.path.join(tmp2, "facts5g.env")
        with open(f5g, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=1\nEP_ENABLED=1\nNPU_TOTAL_MB=65536\n"
                    "GPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        w5g, rt5g, rows5g, rep5g = run(f5g, md_cfg_only, None, 160, 32, [32, 8],
                                       os.path.join(tmp2, "r5g.md"), None, None)
        check("5f: 无分片 → complete=False 只报现状",
              w5g and w5g.get("complete") is False and
              all(r["verdict"] == "unknown" for r in rows5g if not r["is_cur"]) and
              "仅报现状" in (rep5g or ""),
              f"complete={w5g.get('complete') if w5g else '?'}")

        # 5g: path3 覆盖 utilization 后重算（64×0.5−3.6=28.4，而不是保持 54）
        _, rt5h, _, _ = run(f5b, None, None, 32, None, [8], os.path.join(tmp2, "r5h.md"), None,
                            {"utilization": 0.5})
        check("5g: 覆盖 utilization 后 path3 重算=28.4",
              rt5h and rt5h["usable_gib"] is not None and abs(rt5h["usable_gib"] - 28.4) < 1e-6,
              fmt(rt5h["usable_gib"]) if rt5h and rt5h["usable_gib"] is not None else "?")
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # ---- Part 6: 引号 facts 容错 + 缺可用口径的「未测≠不可行」文案
    tmp3 = tempfile.mkdtemp(prefix="tpsplit_q_")
    try:
        # 6a: 带引号的 facts（旧采集端/手工编辑：TP_SIZE="32" / DP_SIZE='5'）不丢数据
        f6 = os.path.join(tmp3, "facts6.env")
        with open(f6, "w", encoding="utf-8") as f:
            f.write('TP_SIZE="32"\nDP_SIZE=\'5\'\nPP_SIZE=1\nEP_ENABLED=1\n'
                    'GPU_MEMORY_UTILIZATION="0.90"\nMAX_MODEL_LEN="131072"\n'
                    'NPU_TOTAL_MB=65536\n')
        _, rt6, _, _ = run(f6, None, wj, 160, 32, [32, 8],
                           os.path.join(tmp3, "r6.md"), None, None)
        check("6a: 双引号 TP_SIZE 容错=32", rt6 and rt6["tp_cur"] == 32,
              f"tp_cur={rt6['tp_cur'] if rt6 else '?'}")
        check("6a: 单引号 DP_SIZE 容错=5", rt6 and rt6["dp_cur"] == 5,
              f"dp_cur={rt6['dp_cur'] if rt6 else '?'}")
        check("6a: 引号 util 容错=0.9", rt6 and rt6["util"] is not None and abs(rt6["util"] - 0.9) < 1e-9,
              str(rt6["util"]) if rt6 else "?")

        # 6b: 缺可用口径（无 npu-smi 无日志）→ unknown + 「缺关键输入」文案，不误报「没有可行候选」
        f6b = os.path.join(tmp3, "facts6b.env")
        with open(f6b, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=1\nEP_ENABLED=1\nMAX_MODEL_LEN=131072\n")
        _, rt6b, rows6b, rep6b = run(f6b, None, wj, 160, 32, [32, 8],
                                     os.path.join(tmp3, "r6b.md"), None, None)
        non_cur = [r for r in rows6b if not r["is_cur"]]
        check("6b: 缺可用口径 → 全部 unknown(kv_unverified)",
              non_cur and all(r["verdict"] == "unknown" and r.get("kv_unverified") for r in non_cur),
              ",".join(r["verdict"] for r in non_cur))
        check("6b: 报告文案=缺关键输入(不是没有可行候选)",
              rep6b and "缺关键输入" in rep6b and "没有可行候选" not in rep6b,
              "文案导向正确" if (rep6b and "缺关键输入" in rep6b) else "文案误导向")
    finally:
        shutil.rmtree(tmp3, ignore_errors=True)

    # ---- Part 7: 复核回归（分片清单比对独立于解析失败 / 汇总桶+PP 的阶段覆盖核对）
    tmp4 = tempfile.mkdtemp(prefix="tpsplit_rk_")
    try:
        # 7a: facts 记 4 分片、本地 1 个可解析分片 → complete=False 只报现状（清单比对不藏在解析失败分支里）
        md7 = _synthetic_model_dir(tmp4)
        f7 = os.path.join(tmp4, "facts7.env")
        with open(f7, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=1\nEP_ENABLED=1\nSAFETENSORS_COUNT=4\n"
                    "NPU_TOTAL_MB=65536\nGPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        w7, rt7, rows7, rep7 = run(f7, md7, None, 160, 32, [32, 8],
                                   os.path.join(tmp4, "r7.md"), None, None)
        check("7a: 清单不符(4≠1)→complete=False", w7 and w7.get("complete") is False,
              f"complete={w7.get('complete') if w7 else '?'}")
        check("7a: 清单不符→非当前行全 unknown 且报告降级",
              all(r["verdict"] == "unknown" for r in rows7 if not r["is_cur"])
              and "分片清单不符" in (rep7 or ""),
              ",".join(r["verdict"] for r in rows7 if not r["is_cur"]))

        # 7b: weights-json 只有汇总桶（无 tensors）+ PP=2 → 阶段覆盖不足 → unknown（不能按 0 权重标 ok）
        wj7 = os.path.join(tmp4, "agg7.json")
        with open(wj7, "w", encoding="utf-8") as f:
            json.dump({"replicated_gib": 0.5, "tp_gib": 64.0, "ep_gib": 0.5, "extra_gib": 0.0,
                       "num_hidden_layers": 32, "mla": True,
                       "config": {"num_hidden_layers": 32, "kv_lora_rank": 512,
                                  "qk_rope_head_dim": 64}}, f)
        f7b = os.path.join(tmp4, "facts7b.env")
        with open(f7b, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=8\nDP_SIZE=4\nPP_SIZE=2\nEP_ENABLED=1\nNPU_TOTAL_MB=65536\n"
                    "GPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        _, rt7b, rows7b, _ = run(f7b, None, wj7, 32, None, [8, 4],
                                 os.path.join(tmp4, "r7b.md"), None, None)
        r4_7 = next((r for r in rows7b if r["tp"] == 4), None)
        check("7b: 汇总桶无 tensors+PP2 → weights=None(不是 0)",
              r4_7 and r4_7.get("weights_gib") is None,
              f"weights={r4_7.get('weights_gib') if r4_7 else '?'}")
        check("7b: 阶段覆盖不足 → pp_unknown → unknown",
              r4_7 and r4_7.get("verdict") == "unknown" and r4_7.get("pp_unknown"),
              str(r4_7["verdict"]) if r4_7 else "?")
    finally:
        shutil.rmtree(tmp4, ignore_errors=True)

    # ---- Part 8: 复核二轮回归（索引清单缺片 / 小额缺失保守并入最重阶段）
    tmp5 = tempfile.mkdtemp(prefix="tpsplit_rk2_")
    try:
        # 8a: index.json 引用 2 片、本地只有 1 片（可解析）、facts 无 SAFETENSORS_COUNT → complete=False
        md8 = _synthetic_model_dir(tmp5)
        with open(os.path.join(md8, "model.safetensors.index.json"), "w", encoding="utf-8") as f:
            json.dump({"weight_map": {"a.weight": "tiny.safetensors",
                                      "b.weight": "part-00002-of-00002.safetensors"}}, f)
        f8 = os.path.join(tmp5, "facts8.env")
        with open(f8, "w", encoding="utf-8") as f:
            f.write("TP_SIZE=32\nDP_SIZE=5\nPP_SIZE=1\nEP_ENABLED=1\n"
                    "NPU_TOTAL_MB=65536\nGPU_MEMORY_UTILIZATION=0.90\nMAX_MODEL_LEN=131072\n")
        w8, rt8, rows8, rep8 = run(f8, md8, None, 160, 32, [32, 8],
                                   os.path.join(tmp5, "r8.md"), None, None)
        check("8a: 索引清单缺片→complete=False（不依赖 facts）",
              w8 and w8.get("complete") is False and "索引清单缺分片" in (w8.get("incomplete_reason") or ""),
              f"complete={w8.get('complete') if w8 else '?'}")
        check("8a: 索引缺片→非当前行全 unknown",
              all(r["verdict"] == "unknown" for r in rows8 if not r["is_cur"]),
              ",".join(r["verdict"] for r in rows8 if not r["is_cur"]))

        # 8b: 覆盖 127/128（>99%）→ 缺失并入最重阶段：0.127 明细 + 0.001 缺 = 0.128
        wj8 = os.path.join(tmp5, "agg8.json")
        with open(wj8, "w", encoding="utf-8") as f:
            json.dump({"replicated_gib": 0.128, "tp_gib": 0.0, "ep_gib": 0.0, "extra_gib": 0.0,
                       "num_hidden_layers": 32, "mla": True,
                       "tensors": [["model.layers.0.norm.weight", round(0.127 * GIB), "rep"]]}, f)
        # per_stage_max_weights 直接单测（不走 run：仅需 weights/tp/ep_div/pp）
        w8b = {"n_layers": 32, "tensors": [("model.layers.0.norm.weight", round(0.127 * GIB), "rep")],
               "rep_gib": 0.128, "tp_gib": 0.0, "ep_gib": 0.0, "extra_gib": 0.0}
        got = per_stage_max_weights(w8b, 32, 32, 2)
        check("8b: 127/128 明细→保守并入=0.128",
              got is not None and abs(got - 0.128) < 1e-9, f"{got}")
        # 覆盖不足 99%（缺失 5%）→ 仍 None（unknown）
        w8c = dict(w8b)
        w8c["tensors"] = [("model.layers.0.norm.weight", round(0.12 * GIB), "rep")]
        got2 = per_stage_max_weights(w8c, 32, 32, 2)
        check("8c: 覆盖<99%→None(unknown)", got2 is None, f"{got2}")
    finally:
        shutil.rmtree(tmp5, ignore_errors=True)

    print("\n=== selftest ===")
    npass = 0
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
        npass += ok
    print(f"{npass}/{len(results)} passed")
    shutil.rmtree(outdir, ignore_errors=True)  # 清理临时报告目录
    return 0 if npass == len(results) else 1


def main():
    ap = argparse.ArgumentParser(description="TP→DP 扩容可行性测算")
    ap.add_argument("--facts", help="collect_env.sh 输出的 facts.env")
    ap.add_argument("--model-dir", help="模型目录（config.json + *.safetensors，只读 header）")
    ap.add_argument("--weights-json", help="直接给分桶 JSON（replicated_gib/tp_gib/ep_gib/extra_gib）")
    ap.add_argument("--total-cards", type=int, help="集群总卡数（默认 TP×DP×PP 推算）")
    ap.add_argument("--ep-group", type=int, help="EP 组卡数（默认当前 tp，建议显式传）")
    ap.add_argument("--tp-candidates", help="逗号分隔候选 TP，默认取 ep_group 约数")
    ap.add_argument("--out", help="Markdown 报告输出路径（缺省打印到 stdout）")
    ap.add_argument("--json", help="JSON 输出路径")
    ap.add_argument("--overrides", help="覆盖项 JSON（文件或内联）：activation_gib/other_gib/"
                        "reserved_gib/utilization/kv_bytes_per_token/max_model_len/extra_gib/tp_gib/ep_gib/rep_gib")
    ap.add_argument("--dump-plan", action="store_true",
                    help="打印测算计划与输入清单（无需任何输入，可单独运行）")
    ap.add_argument("--selftest", action="store_true", help="用内置 GLM5.2 样例自测")
    args = ap.parse_args()

    if args.dump_plan:
        dump_plan()
        return

    if args.selftest:
        sys.exit(selftest())

    cands = None
    if args.tp_candidates:
        try:
            cands = [int(x) for x in re.split(r"[,\s]+", args.tp_candidates.strip()) if x]
        except ValueError as e:
            sys.exit(f"错误：--tp-candidates 含非整数项: {e}（示例：32,16,8）")

    run(args.facts, args.model_dir, args.weights_json, args.total_cards, args.ep_group,
        cands, args.out, args.json, _load_overrides(args.overrides))


if __name__ == "__main__":
    main()
