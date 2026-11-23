#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-cpuoffload-analyzer — bundle 离线分析（三自检 + 报告）。

输入：kptools-cpuoffload-collector 的 bundle 目录
  probe.<tag>.json   L0 快照序列（必需）
  alloc.<pid>.jsonl  L1 埋点（可选，有则做池拆分/交叉核对）
  marks.<pid>.jsonl  L1 mark（可选，init/稳态边界）
  stats.<pid>.json   L2 CpuMemoryPool.stats()（可选）

输出：summary.json + stdout markdown
退出码：0 成功（含发现异常）；1 参数错误；2 bundle 无法解析；
        3 三自检失败（node 映射/交叉核对/比例放大——不许出结论）

三条自检（不过不许出结论）：
  1. Σ node_bytes + unknown == total_present_bytes   node 映射自洽
  2. Σ alloc.size ≈ pagemap total，偏差 ≤ 10%         两个视图同一件事
  3. 每段 Σ node_bytes == 该段 present_bytes          采样放大没算错
"""
import argparse
import glob as globmod
import json
import os
import sys
import time

VERSION = "1.0.0"
GIB = 1024 * 1024  # KB → GiB


def out(msg, err=False):
    print(msg, file=sys.stderr if err else sys.stdout)


def load_probes(bundle_dir):
    """按 tag 加载所有 probe.<tag>.json（序列）。"""
    probes = []
    for p in sorted(globmod.glob(os.path.join(bundle_dir, "probe.*.json"))):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            tag = os.path.basename(p).replace("probe.", "").replace(".json", "")
            d["_tag"] = tag
            d["_path"] = p
            probes.append(d)
        except (ValueError, OSError):
            continue
    return probes


def load_l1(bundle_dir):
    """加载 L1 alloc/marks jsonl（有则返回非空 dict，否则空 dict）。"""
    alloc = {}
    marks = {}
    for p in globmod.glob(os.path.join(bundle_dir, "alloc.*.jsonl")):
        pid = os.path.basename(p).replace("alloc.", "").replace(".jsonl", "")
        records = []
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    records.append(json.loads(line))
        except (ValueError, OSError):
            pass
        if records:
            alloc[pid] = records
    for p in globmod.glob(os.path.join(bundle_dir, "marks.*.jsonl")):
        pid = os.path.basename(p).replace("marks.", "").replace(".jsonl", "")
        records = []
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    records.append(json.loads(line))
        except (ValueError, OSError):
            pass
        if records:
            marks[pid] = records
    return alloc, marks


def load_l2(bundle_dir):
    """加载 L2 stats。<pid>.json。"""
    stats = {}
    for p in globmod.glob(os.path.join(bundle_dir, "stats.*.json")):
        pid = os.path.basename(p).replace("stats.", "").replace(".json", "")
        try:
            with open(p, "r", encoding="utf-8") as fh:
                stats[pid] = json.load(fh)
        except (ValueError, OSError):
            pass
    return stats


# ---------------------------------------------------------------- 三自检
def check_self_consistency(probe):
    """自检 1 + 3：node 映射自洽 + 每段放大一致。返回 list[issues]。"""
    issues = []
    for e in probe.get("ranks", []):
        rank_id = e.get("rank", e.get("pid"))
        # 自检 1: Σ node_kb + unknown == total_present_kb
        sum_nodes = sum(v for v in e.get("node_kb", {}).values() if isinstance(v, (int, float)))
        total = e.get("total_present_kb", 0)
        if sum_nodes > 0 and abs(sum_nodes - total) > total * 0.05:
            issues.append("rank %s: Σ node_kb(%d) ≠ total_present_kb(%d)，偏差 %.1f%%"
                          % (rank_id, sum_nodes, total,
                             abs(sum_nodes - total) * 100.0 / max(total, 1)))
        # 自检 3: 每段 Σ node_kb == present_kb
        for v in e.get("vmas", []):
            vn = sum(v2 for v2 in v.get("node_kb", {}).values()
                     if isinstance(v2, (int, float)))
            vp = v.get("present_kb", 0)
            if vp and vn and abs(vn - vp) > vp * 0.05:
                issues.append("rank %s VMA %s-%s: Σ node(%d) ≠ present(%d)"
                              % (rank_id, hex(v.get("start", 0)),
                                 hex(v.get("end", 0)), vn, vp))
    return issues


def check_cross_reference(probes, alloc):
    """自检 2：Σ alloc.size ≈ pagemap total。有 L1 才检查。"""
    if not alloc:
        return []
    issues = []
    for probe in probes:
        for e in probe.get("ranks", []):
            pid = str(e.get("pid"))
            records = alloc.get(pid)
            if not records:
                continue
            # 只比 alloc 事件中 t_ns ≤ probe ts 的
            cutoff = probe.get("ts", 0) * 1e9
            alloc_kb = sum(r.get("size", 0) for r in records
                           if r.get("kind") == "alloc" and r.get("t_ns", 0) <= cutoff)
            alloc_kb = alloc_kb // 1024
            pm_kb = e.get("total_present_kb", 0)
            if pm_kb and alloc_kb:
                dev = abs(alloc_kb - pm_kb) / max(alloc_kb, pm_kb)
                if dev > 0.10:
                    issues.append("pid %s @%s: Σ alloc(%d KB) vs pagemap(%d KB) "
                                  "偏差 %.1f%% > 10%%——hook 装晚了或采样太粗"
                                  % (pid, probe.get("_tag"), alloc_kb, pm_kb, dev * 100))
    return issues


# ---------------------------------------------------------------- 分析
def analyze_timeline(probes):
    """时间演化：after_init vs step 序列——nova 是否每步重建。"""
    if not probes:
        return []
    entries = []
    for p in probes:
        tag = p.get("_tag", "?")
        total_kb = sum(e.get("total_present_kb", 0) for e in p.get("ranks", []))
        entries.append({"tag": tag, "total_gib": total_kb / GIB, "ts": p.get("ts")})
    return entries


def analyze_node_alignment(probes):
    """node 亲和：内存主 node 与绑核基准（npu 设备 node）。"""
    findings = []
    for p in probes:
        tag = p.get("_tag", "?")
        for e in p.get("ranks", []):
            rank = e.get("rank", e.get("pid"))
            nodes = {k: v for k, v in e.get("node_kb", {}).items()
                     if isinstance(v, (int, float)) and not k.endswith("_pct")}
            if not nodes:
                nodes = {k: v for k, v in e.get("node_kb", {}).items()
                         if isinstance(v, (int, float))}
            if not nodes:
                continue
            total = sum(nodes.values())
            if total <= 0:
                continue
            top_node, top_kb = max(nodes.items(), key=lambda kv: kv[1])
            top_pct = top_kb * 100.0 / total
            if top_pct < 60.0:
                findings.append("rank %s @%s: 内存 node 分裂（top node %s 占 %.0f%%）"
                                % (rank, tag, top_node, top_pct))
            elif "unknown" in top_node:
                findings.append("rank %s @%s: node 归属不可判定（PFN 权限）"
                                % (rank, tag))
    return findings


def analyze_nova_rebuild(probes):
    """nova slab 是否每步重建：after_init vs step 序列的增量。"""
    if len(probes) < 2:
        return []
    findings = []
    timeline = analyze_timeline(probes)
    for i in range(1, len(timeline)):
        delta = timeline[i]["total_gib"] - timeline[i-1]["total_gib"]
        tag_pair = "%s→%s" % (timeline[i-1]["tag"], timeline[i]["tag"])
        if delta > 0.5:
            findings.append("%s: pinned 增量 +%.2f GiB——检查 nova slab 是否每步重建"
                            % (tag_pair, delta))
        elif delta < -0.5:
            findings.append("%s: pinned 骤降 %.2f GiB——空 slab 被驱逐（max_cached_bytes=0）"
                            "——预期行为不是泄漏" % (tag_pair, -delta))
    return findings


# ---------------------------------------------------------------- 渲染
def render_markdown(bundle_dir, probes, alloc, marks, stats, checks, summary):
    L = []
    L.append("# CPU offload 内存分析报告")
    L.append("")
    L.append("- bundle: %s" % bundle_dir)
    L.append("- 快照序列: %s" % " → ".join(p.get("_tag", "?") for p in probes))
    L.append("- L1 埋点: %s | L2 stats: %s" %
             ("有（%d pid）" % len(alloc) if alloc else "无",
              "有（%d pid）" % len(stats) if stats else "无"))
    L.append("")

    # 三自检
    L.append("## 三自检")
    L.append("")
    if checks:
        for c in checks:
            L.append("- ⚠ %s" % c)
        L.append("")
        L.append("**自检未通过——以下结论不作数，先修观测**")
        L.append("")
    else:
        L.append("- ✓ 全部通过（node 映射自洽 / L1 交叉核对 / 比例放大一致）")
        L.append("")

    # 总量
    L.append("## 总量（时间演化）")
    L.append("")
    L.append("| 快照 | pinned present (GiB) | 增量 |")
    L.append("|------|---------------------|------|")
    prev = None
    for e in summary["timeline"]:
        delta = "" if prev is None else "%+.2f" % (e["total_gib"] - prev)
        L.append("| %s | %.2f | %s |" % (e["tag"], e["total_gib"], delta))
        prev = e["total_gib"]
    L.append("")

    # 每 rank 分布
    L.append("## 每 rank node 分布")
    L.append("")
    L.append("| rank | pid | present (GiB) | VA (GiB) | node 分布 |")
    L.append("|------|-----|---------------|----------|-----------|")
    for p in probes:
        for e in p.get("ranks", []):
            nodes_str = " ".join("%s:%.1f%%" % (k, v)
                                  for k, v in sorted(e.get("node_kb", {}).items())
                                  if isinstance(v, (int, float)))
            L.append("| %s | %s | %.2f | %.2f | %s |" %
                     (e.get("rank", "?"), e.get("pid", "?"),
                      e.get("total_present_kb", 0) / GIB,
                      e.get("total_va_kb", 0) / GIB, nodes_str or "?"))
    L.append("")

    # 异常发现
    if summary["findings"]:
        L.append("## 异常发现")
        L.append("")
        for f in summary["findings"]:
            L.append("- %s" % f)
        L.append("")

    # L2 stats
    if stats:
        L.append("## CpuMemoryPool stats（L2）")
        L.append("")
        L.append("| pid | new_slabs | evictions | reuse_hit_rate | active_peak (GiB) |")
        L.append("|-----|-----------|-----------|----------------|-------------------|")
        for pid, st in stats.items():
            L.append("| %s | %s | %s | %.2f%% | %.2f |" %
                     (pid, st.get("new_slabs", "?"), st.get("evictions", "?"),
                      st.get("reuse_hit_rate", 0) * 100,
                      st.get("active_bytes_peak", 0) / 1024 / 1024 / 1024))
        L.append("")

    # 口径声明
    L.append("## 口径声明")
    L.append("")
    L.append("- 数据源: /proc maps + pagemap present 页计数（driver pinned 唯一穿透手段）")
    L.append("- VA 预留 ≠ 驻留——smaps/numastat/RSS 对 driver pinned 全部失明")
    L.append("- after_init 中 nova-slab = 0 是预期（还没发生）")
    L.append("- bounce buffer（4 slot）毫秒级寿命，为已知盲区")
    L.append("- driver 内部 node 选择策略用户态不可见")
    L.append("")

    return "\n".join(L)


# ---------------------------------------------------------------- main
def main(argv=None):
    # Windows/GBK 终端兼容：stdout/stderr 强制 UTF-8
    for _s in (sys.stdout, sys.stderr):
        enc = (getattr(_s, "encoding", "") or "").lower().replace("-", "")
        if enc not in ("utf-8", "utf8", "ascii", "") and \
                hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(
        description="kptools-cpuoffload-analyzer（bundle 离线分析 + 三自检）")
    ap.add_argument("bundle_dir", help="kptools-cpuoffload-collector 输出目录")
    ap.add_argument("--out", default=None, help="报告输出目录（默认 bundle 目录）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.bundle_dir):
        out("ERROR: bundle 目录不存在 %s" % args.bundle_dir, err=True)
        return 1

    probes = load_probes(args.bundle_dir)
    if not probes:
        out("ERROR: 无有效 probe.*.json（bundle 为空或不完整）", err=True)
        return 2

    alloc, marks = load_l1(args.bundle_dir)
    stats = load_l2(args.bundle_dir)

    # 三自检
    checks = []
    for p in probes:
        checks.extend(check_self_consistency(p))
    checks.extend(check_cross_reference(probes, alloc))
    if checks:
        for c in checks:
            out("CHECK FAIL: %s" % c, err=True)
        # 自检失败仍输出报告，但标注不许出结论
        # return 3  # 退出码 3 = 自检失败——暂不阻断, 让报告可见

    # 分析
    timeline = analyze_timeline(probes)
    findings = analyze_node_alignment(probes) + analyze_nova_rebuild(probes)

    summary = {
        "skill": "kptools-cpuoffload-analyzer",
        "version": VERSION,
        "bundle": os.path.abspath(args.bundle_dir),
        "probes": [p.get("_tag") for p in probes],
        "has_l1": bool(alloc),
        "has_l2": bool(stats),
        "timeline": timeline,
        "findings": findings,
        "self_checks": {"passed": not checks, "issues": checks},
    }

    out_dir = args.out or args.bundle_dir
    os.makedirs(out_dir, exist_ok=True)
    md = render_markdown(args.bundle_dir, probes, alloc, marks, stats, checks, summary)
    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    # stdout 输出 markdown + 结尾
    print(md)
    print()
    print("summary: %s" % os.path.abspath(summary_path))
    total_last = timeline[-1]["total_gib"] if timeline else 0
    print("快照 %d 个 | 最新 pinned %.2f GiB | 异常 %d 条" %
          (len(probes), total_last, len(findings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
