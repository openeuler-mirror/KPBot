#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-cpuoffload-analyzer — bundle 离线分析（三自检 + 报告）。

输入：kptools-cpuoffload-collector 的 bundle 目录
  probe.<tag>.json   L0 快照序列（必需，按 JSON 内 ts 排序——文件名
                     字典序在 step_10 vs step_2 上会乱序）
  alloc.<pid>.jsonl  L1 埋点（可选；kind=alloc|copy|free，
                     result_ptr/arg_ptr 供存活配对——自检 2 按快照
                     时刻仍存活的分配量比对，而非累计值）
  marks.<pid>.jsonl  L1 mark（可选；opt_step_begin/end → 训练步边界）
  stats.<pid>.json   L2 CpuMemoryPool.stats()（可选；collector/L1 均
                     不自动生产，需训练侧手动落盘，见 data-contract.md）

输出：默认 <bundle>_report/summary.json（不写 bundle 目录，bundle 只读）
      + stdout markdown 报告
退出码：0 成功（含发现异常）；1 参数错误（含 argparse 用法错误）；
        2 bundle 为空（无有效 probe——大概率训练未启用 cpu offload）；
        3 三自检失败（报告仍输出，但结论不作数——先修观测再采）
"""
import argparse
import glob as globmod
import json
import os
import sys

VERSION = "1.1.0"
GIB = 1024 * 1024  # KB → GiB


def out(msg, err=False):
    print(msg, file=sys.stderr if err else sys.stdout)


# ---------------------------------------------------------------- 加载
def load_probes(bundle_dir, warnings):
    """加载全部 probe.<tag>.json，按 JSON 内 ts 升序（文件名序兜底）。"""
    probes = []
    for p in globmod.glob(os.path.join(bundle_dir, "probe.*.json")):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (ValueError, OSError) as e:
            warnings.append("probe 文件解析失败（跳过）：%s（%s）" % (p, e))
            continue
        d["_tag"] = os.path.basename(p)[len("probe."):-len(".json")]
        d["_path"] = p
        probes.append(d)
    probes.sort(key=lambda d: (d.get("ts") or 0, d.get("_tag") or ""))
    return probes


def _load_jsonl(path, warnings):
    """逐行加载 jsonl：坏行计数（尾部半行 = 采集进行中，属正常）。

    返回 (records, bad_lines)。整体文件打不开 → ([], None) 并记 warning。
    """
    records = []
    bad = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError as e:
        warnings.append("文件不可读（跳过）：%s（%s）" % (path, e))
        return [], None
    if bad:
        warnings.append("%s：%d 行无法解析（尾部截断或损坏——采集进行中读到"
                        "半行属正常，已丢弃）" % (os.path.basename(path), bad))
    return records, bad


def load_l1(bundle_dir, warnings):
    """加载 L1 alloc/marks jsonl（有则返回非空 dict，否则空 dict）。"""
    alloc = {}
    marks = {}
    for p in globmod.glob(os.path.join(bundle_dir, "alloc.*.jsonl")):
        pid = os.path.basename(p)[len("alloc."):-len(".jsonl")]
        records, _ = _load_jsonl(p, warnings)
        if records:
            alloc[pid] = records
    for p in globmod.glob(os.path.join(bundle_dir, "marks.*.jsonl")):
        pid = os.path.basename(p)[len("marks."):-len(".jsonl")]
        records, _ = _load_jsonl(p, warnings)
        if records:
            marks[pid] = records
    return alloc, marks


def load_l2(bundle_dir, warnings):
    """加载 L2 stats.<pid>.json（手动落盘的可选项）。"""
    stats = {}
    for p in globmod.glob(os.path.join(bundle_dir, "stats.*.json")):
        pid = os.path.basename(p)[len("stats."):-len(".json")]
        try:
            with open(p, "r", encoding="utf-8") as fh:
                stats[pid] = json.load(fh)
        except (ValueError, OSError) as e:
            warnings.append("stats 文件解析失败（跳过）：%s（%s）" % (p, e))
    return stats


# ---------------------------------------------------------------- 三自检
def check_self_consistency(probe, warnings):
    """自检 1 + 3：node 映射自洽 + 每段放大一致。返回 list[issues]。

    显式零值纳入比较：present > 0 但 node_kb 全零 → 单列"无法验证"，
    不再静默通过。
    """
    issues = []
    for e in probe.get("ranks", []):
        rank_id = e.get("rank", e.get("pid"))
        if e.get("pagemap_unreadable"):
            warnings.append("rank %s @%s：pagemap 不可读（只有 VA 大小），"
                            "自检 1/3 跳过" % (rank_id, probe.get("_tag")))
            continue
        # 自检 1: Σ node_kb + unknown == total_present_kb
        sum_nodes = sum(v for v in e.get("node_kb", {}).values()
                        if isinstance(v, (int, float)))
        total = e.get("total_present_kb") or 0
        if total > 0:
            if sum_nodes == 0:
                issues.append("rank %s: present %d KB 但 node_kb 全零——"
                              "node 映射缺失，无法验证" % (rank_id, total))
            elif abs(sum_nodes - total) > total * 0.05:
                issues.append("rank %s: Σ node_kb(%d) ≠ total_present_kb(%d)，"
                              "偏差 %.1f%%"
                              % (rank_id, sum_nodes, total,
                                 abs(sum_nodes - total) * 100.0 / max(total, 1)))
        # 自检 3: 每段 Σ node_kb == present_kb
        for v in e.get("vmas", []):
            vn = sum(v2 for v2 in v.get("node_kb", {}).values()
                     if isinstance(v2, (int, float)))
            vp = v.get("present_kb") or 0
            if vp > 0:
                if vn == 0:
                    issues.append("rank %s VMA %s-%s: present %d KB 但 node_kb "
                                  "全零——无法验证"
                                  % (rank_id, hex(v.get("start", 0)),
                                     hex(v.get("end", 0)), vp))
                elif abs(vn - vp) > vp * 0.05:
                    issues.append("rank %s VMA %s-%s: Σ node(%d) ≠ present(%d)"
                                  % (rank_id, hex(v.get("start", 0)),
                                     hex(v.get("end", 0)), vn, vp))
    return issues


def _alive_alloc_kb(records, cutoff_ns):
    """按 result_ptr/arg_ptr 配对 alloc/free → 快照时刻仍存活 KB。

    返回 (alive_bytes, unpaired_events)。无配对标识的事件（旧版 hook
    或 _grow 这类无返回值的方法）计入 unpaired——由调用方降级为
    "无法精确验证"而不是拿累计值硬比。
    """
    pending = {}
    unpaired = 0
    for r in sorted(records, key=lambda x: x.get("t_ns") or 0):
        t = r.get("t_ns") or 0
        if t > cutoff_ns:
            break
        kind = r.get("kind")
        size = r.get("size") or 0
        if kind == "free":
            ptr = r.get("arg_ptr")
            if ptr:
                pending.pop(ptr, None)  # 早于 hook 安装的 free：无害忽略
            else:
                unpaired += 1
        elif kind in ("alloc", "copy"):
            ptr = r.get("result_ptr")
            if ptr:
                pending[ptr] = pending.get(ptr, 0) + size
            else:
                unpaired += 1
    return sum(pending.values()), unpaired


def check_cross_reference(probes, alloc, warnings):
    """自检 2：快照时刻仍存活的 Σ alloc ≈ pagemap total（有 L1 才做）。

    用存活量而非累计量——池先分配 1 GiB 回收再分配 1 GiB 时累计 2 GiB
    是正常采样，不是偏差。
    """
    if not alloc:
        return []
    issues = []
    for probe in probes:
        for e in probe.get("ranks", []):
            pid = str(e.get("pid"))
            records = alloc.get(pid)
            if not records:
                continue
            if e.get("pagemap_unreadable"):
                continue
            cutoff = (probe.get("ts") or 0) * 1e9
            alive_b, unpaired = _alive_alloc_kb(records, cutoff)
            pm_kb = e.get("total_present_kb") or 0
            if not pm_kb:
                continue
            n_allocs = sum(1 for r in records if r.get("kind") in ("alloc", "copy"))
            if alive_b == 0 and unpaired >= n_allocs and n_allocs:
                issues.append("pid %s @%s: L1 alloc 事件全部无配对标识——"
                              "交叉核对无法验证（旧版 hook？）"
                              % (pid, probe.get("_tag")))
                continue
            if unpaired:
                warnings.append("pid %s @%s：%d 条 alloc/free 事件无 ptr 配对"
                                "标识，存活量为下界" % (pid, probe.get("_tag"),
                                                          unpaired))
            alive_kb = alive_b // 1024
            if alive_kb and pm_kb:
                dev = abs(alive_kb - pm_kb) / max(alive_kb, pm_kb)
                if dev > 0.10:
                    issues.append("pid %s @%s: 存活 alloc(%d KB) vs pagemap"
                                  "(%d KB) 偏差 %.1f%% > 10%%——hook 装晚了"
                                  "或采样太粗" % (pid, probe.get("_tag"),
                                                   alive_kb, pm_kb, dev * 100))
    return issues


# ---------------------------------------------------------------- L1 分析
def analyze_steps(marks):
    """L1 opt_step_begin 计数 → 每 pid 训练步数（步边界锚点）。"""
    steps = {}
    for pid, records in marks.items():
        n = sum(1 for r in records if r.get("kind") == "opt_step_begin")
        if n:
            steps[pid] = n
    return steps


def analyze_alloc_pools(alloc):
    """L1 按 source 拆池：PinnedFlatPool/CpuMemoryPool/PinnedCpuStorage。"""
    pools = {}
    for pid, records in alloc.items():
        for r in records:
            if r.get("kind") in ("alloc", "copy"):
                src = r.get("source", "?")
                p = pools.setdefault(src, {"events": 0, "total_kb": 0.0})
                p["events"] += 1
                p["total_kb"] += (r.get("size") or 0) / 1024.0
    return pools


# ---------------------------------------------------------------- 分析
def analyze_timeline(probes):
    """时间演化（总量口径：全 rank 求和）。"""
    entries = []
    for p in probes:
        tag = p.get("_tag", "?")
        total_kb = sum(e.get("total_present_kb") or 0
                       for e in p.get("ranks", []))
        entries.append({"tag": tag, "total_gib": total_kb / GIB,
                        "ts": p.get("ts")})
    return entries


def analyze_node_alignment(probes):
    """node 亲和：内存主 node 占比（node_kb 为 KB 口径）。"""
    findings = []
    for p in probes:
        tag = p.get("_tag", "?")
        for e in p.get("ranks", []):
            rank = e.get("rank", e.get("pid"))
            nodes = {k: v for k, v in e.get("node_kb", {}).items()
                     if isinstance(v, (int, float))}
            if not nodes:
                continue
            total = sum(nodes.values())
            if total <= 0:
                continue
            top_node, top_kb = max(nodes.items(), key=lambda kv: kv[1])
            top_pct = top_kb * 100.0 / total
            if "unknown" in top_node:
                findings.append("rank %s @%s: node 归属不可判定（主桶 "
                                "unknown——PFN 权限缺失或 PFN 屏蔽）"
                                % (rank, tag))
            elif top_pct < 60.0:
                findings.append("rank %s @%s: 内存 node 分裂（top node %s "
                                "占 %.0f%%）" % (rank, tag, top_node, top_pct))
    return findings


def analyze_nova_rebuild(probes):
    """nova slab 是否每步重建：相邻快照按共同 rank 对齐后的增量。

    rank/PID 集合变化（某次探测缺 rank）单独披露，不并入增量——
    少采一个 rank 不应被误报成"空 slab 驱逐"。
    """
    if len(probes) < 2:
        return []
    findings = []
    for i in range(1, len(probes)):
        prev, cur = probes[i - 1], probes[i]
        prev_map = {e.get("rank", e.get("pid")): e for e in prev.get("ranks", [])}
        cur_map = {e.get("rank", e.get("pid")): e for e in cur.get("ranks", [])}
        common = sorted(set(prev_map) & set(cur_map))
        gone = sorted(set(prev_map) - set(cur_map), key=str)
        new = sorted(set(cur_map) - set(prev_map), key=str)
        tag_pair = "%s→%s" % (prev.get("_tag"), cur.get("_tag"))
        if gone or new:
            findings.append("%s: rank 集合变化（消失 %s / 新增 %s）——"
                            "增量按共同 rank %s 对齐，集合差异非内存事件"
                            % (tag_pair, gone or "无", new or "无", common))
        delta_kb = sum((cur_map[k].get("total_present_kb") or 0)
                       - (prev_map[k].get("total_present_kb") or 0)
                       for k in common)
        delta_gib = delta_kb / GIB
        if delta_gib > 0.5:
            findings.append("%s: 共同 rank pinned 增量 +%.2f GiB——检查 nova "
                            "slab 是否每步重建" % (tag_pair, delta_gib))
        elif delta_gib < -0.5:
            findings.append("%s: 共同 rank pinned 骤降 %.2f GiB——驻留下降"
                            "（候选解释：空 slab 驱逐 max_cached_bytes=0，"
                            "预期行为非泄漏；可用 L1 free 事件佐证）"
                            % (tag_pair, -delta_gib))
    return findings


# ---------------------------------------------------------------- 渲染
def _fmt_gib(kb):
    if kb is None:
        return "?"
    return "%.2f" % (kb / GIB)


def render_markdown(bundle_dir, probes, alloc, marks, stats, checks, skipped,
                    warnings, summary):
    L = []
    L.append("# CPU offload 内存分析报告")
    L.append("")
    L.append("- bundle: %s" % bundle_dir)
    L.append("- 快照序列（按 ts）: %s" % " → ".join(p.get("_tag", "?")
                                                    for p in probes))
    L.append("- L1 埋点: %s | L2 stats: %s" %
             ("有（%d pid）" % len(alloc) if alloc else "无",
              "有（%d pid，手动落盘）" % len(stats) if stats else "无"))
    L.append("")

    # 数据缺口（解析警告 / partial 快照）
    if warnings:
        L.append("## ⚠ 数据缺口")
        L.append("")
        for w in warnings:
            L.append("- %s" % w)
        L.append("")

    # 三自检
    L.append("## 三自检")
    L.append("")
    if skipped:
        for s in skipped:
            L.append("- - %s" % s)
    if checks:
        for c in checks:
            L.append("- ⚠ %s" % c)
        L.append("")
        L.append("**自检未通过——以下结论不作数，先修观测（退出码 3）**")
        L.append("")
    elif not skipped:
        L.append("- ✓ 全部通过（node 映射自洽 / L1 交叉核对 / 比例放大一致）")
        L.append("")
    L.append("")

    # 总量
    L.append("## 总量（时间演化，全 rank 求和）")
    L.append("")
    L.append("| 快照 | pinned present (GiB) | 增量 |")
    L.append("|------|---------------------|------|")
    prev = None
    for e in summary["timeline"]:
        delta = "" if prev is None else "%+.2f" % (e["total_gib"] - prev)
        L.append("| %s | %.2f | %s |" % (e["tag"], e["total_gib"], delta))
        prev = e["total_gib"]
    L.append("")

    # 每 rank 分布（带快照列；node 分布 KB → GiB）
    L.append("## 每 rank node 分布（KB 口径，采样模式为外推值）")
    L.append("")
    L.append("| 快照 | rank | pid | present (GiB) | VA (GiB) | node 分布 (GiB) |")
    L.append("|------|------|-----|---------------|----------|-----------------|")
    for p in probes:
        for e in p.get("ranks", []):
            nodes_str = " ".join("%s:%s" % (k, _fmt_gib(v))
                                 for k, v in sorted(e.get("node_kb", {}).items())
                                 if isinstance(v, (int, float)))
            L.append("| %s | %s | %s | %s | %s | %s |" %
                     (p.get("_tag", "?"), e.get("rank", "?"), e.get("pid", "?"),
                      _fmt_gib(e.get("total_present_kb")),
                      _fmt_gib(e.get("total_va_kb")),
                      nodes_str or "?"))
    L.append("")

    # L1 步数 + 池拆分
    if alloc:
        L.append("## L1 埋点视图")
        L.append("")
        steps = summary.get("l1_steps") or {}
        if steps:
            L.append("- 训练步数（opt_step_begin 计数）: %s"
                     % ", ".join("pid %s=%d" % kv for kv in sorted(steps.items())))
        pools = summary.get("l1_pools") or {}
        if pools:
            L.append("")
            L.append("| 池（source） | 事件数 | 累计申请 (GiB) |")
            L.append("|------------|--------|----------------|")
            for src, p in sorted(pools.items()):
                L.append("| %s | %d | %.2f |" % (src, p["events"],
                                                 p["total_kb"] / GIB))
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
        L.append("## CpuMemoryPool stats（L2，手动落盘）")
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
    L.append("- node_kb 为 KB 口径（采样模式为外推值）；present 但 PFN 屏蔽的页归入 unknown 桶")
    L.append("- VA 预留 ≠ 驻留——smaps/numastat/RSS 对 driver pinned 全部失明")
    L.append("- after_init 中 nova-slab = 0 是预期（还没发生）")
    L.append("- 增量判定按共同 rank 对齐；rank 集合变化单独披露")
    L.append("- bounce buffer（4 slot）毫秒级寿命，为已知盲区")
    L.append("- driver 内部 node 选择策略用户态不可见")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- main
class _ArgParser(argparse.ArgumentParser):
    """用法错误统一退 1（argparse 默认退 2 与「2=bundle 为空」撞码）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        out("ERROR: 参数错误：%s（--help 查看用法）" % message, err=True)
        sys.exit(1)


def _atomic_write_json(path, obj):
    """临时文件 + os.replace 原子替换（防半写/防 symlink 截断误伤）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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
    ap = _ArgParser(
        description="kptools-cpuoffload-analyzer（bundle 离线分析 + 三自检）")
    ap.add_argument("bundle_dir", help="kptools-cpuoffload-collector 输出目录")
    ap.add_argument("--out", default=None,
                    help="报告输出目录（默认 bundle 同级 <bundle>_report，"
                         "不写 bundle——bundle 只读）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.bundle_dir):
        out("ERROR: bundle 目录不存在 %s" % args.bundle_dir, err=True)
        return 1

    warnings = []
    probes = load_probes(args.bundle_dir, warnings)
    if not probes:
        for w in warnings:
            out("WARN: %s" % w, err=True)
        out("ERROR: 无有效 probe.*.json（bundle 为空或不完整——大概率目标"
            "训练未启用 cpu offload，先检查 --training.enable-cpu-offload）",
            err=True)
        return 2

    alloc, marks = load_l1(args.bundle_dir, warnings)
    stats = load_l2(args.bundle_dir, warnings)

    # 三自检（报告仍输出，但退出码 3——结论不作数）
    checks = []
    for p in probes:
        checks.extend(check_self_consistency(p, warnings))
    checks.extend(check_cross_reference(probes, alloc, warnings))
    skipped = []
    if not alloc:
        skipped.append("自检 2 跳过：无 L1 埋点（alloc.*.jsonl）")
    for w in warnings:
        out("WARN: %s" % w, err=True)
    for c in checks:
        out("CHECK FAIL: %s" % c, err=True)

    # 分析
    timeline = analyze_timeline(probes)
    findings = analyze_node_alignment(probes) + analyze_nova_rebuild(probes)

    summary = {
        "skill": "kptools-cpuoffload-analyzer",
        "version": VERSION,
        "bundle": os.path.abspath(args.bundle_dir),
        "probes": [p.get("_tag") for p in probes],
        "probe_order": "ts",
        "has_l1": bool(alloc),
        "has_l2": bool(stats),
        "l1_steps": analyze_steps(marks),
        "l1_pools": analyze_alloc_pools(alloc),
        "timeline": timeline,
        "findings": findings,
        "self_checks": {"passed": not checks, "issues": checks,
                        "skipped": skipped},
        "parse_warnings": warnings,
    }

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.abspath(args.bundle_dir.rstrip("/\\"))) or ".",
        os.path.basename(os.path.abspath(args.bundle_dir).rstrip("/\\")) + "_report")
    os.makedirs(out_dir, exist_ok=True)
    md = render_markdown(args.bundle_dir, probes, alloc, marks, stats, checks,
                         skipped, warnings, summary)
    summary_path = os.path.join(out_dir, "summary.json")
    _atomic_write_json(summary_path, summary)

    # stdout 输出 markdown + 结尾
    print(md)
    print()
    print("summary: %s" % os.path.abspath(summary_path))
    total_last = timeline[-1]["total_gib"] if timeline else 0
    print("快照 %d 个 | 最新 pinned %.2f GiB | 异常 %d 条 | 自检 %s" %
          (len(probes), total_last, len(findings),
           "通过" if not checks else "失败（rc=3，结论不作数）"))
    return 3 if checks else 0


if __name__ == "__main__":
    sys.exit(main())
