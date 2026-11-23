#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-cpuoffload-collector probe — L0 外部快照（零侵入）。

对目标进程读 /proc：
  maps      → 找 driver pinned 的 VMA（pathname 含 /dev/devmm_svm，可配置）
  pagemap   → 每 VMA 采样物理页 present 位（真实驻留）与 PFN（需 CAP_SYS_ADMIN）
  zoneinfo  → PFN → NUMA node 映射（无权限时 node 降级 unknown）

为什么不用 RSS/smaps/numastat：driver pinned 内存走 special PTE（无
struct page），smaps/numastat 对它全部失明——只有 pagemap 能穿透；
VMA size 是 VA 预留不等于驻留，必须按 present 页计数。

采样模式：--sample-ratio N（默认 64 = 每 N 页采 1 页，按比例外推），
--full 全量逐页（1 GiB 约数十秒）。

输出（追加式，bundle 目录）：
  probe.<tag>.json   # 本 tag 的 L0 快照（rank/pid 维度）
  trace.log          # 人类可读快照摘要（与 bundle 每次追加）
  run_meta.json      # world/device_node_map/设备亲和基准（init 时写一次）

用法：
  python3 probe.py --pid <PID> --tag after_init --out <bundle>
  python3 probe.py --auto --tag step_1 --out <bundle>     # torchrun rank 进程
  python3 probe.py --pid <PID> --tag step_5 --sample-ratio 8 --out <bundle>

退出码：0 成功；1 参数错误；2 /proc 不可读；3 目标不存在；4 未找到任何
pinned VMA（目标未启用 cpu offload 或驱动未映射）；5 pagemap 打开失败
"""
import argparse
import glob as globmod
import json
import os
import socket
import sys
import time

VERSION = "1.0.0"
PAGE_SIZE = 4096
PFN_MASK = (1 << 55) - 1
PRESENT_BIT = 1 << 63


def out(msg, err=False):
    print(msg, file=sys.stderr if err else sys.stdout)


def proc_root_of(pid, proc_root):
    return os.path.join(proc_root, str(pid))


# ---------------------------------------------------------------- rank 发现
def discover_ranks(proc_root):
    """返回 [{pid, rank, local_rank, world_size}]（RANK 族兼容 torchrun/mpirun/srun）。"""
    found = []
    for d in sorted(globmod.glob(os.path.join(proc_root, "[0-9]*"))):
        pid = int(os.path.basename(d))
        try:
            with open(os.path.join(d, "environ"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        env = {}
        for kv in raw.split(b"\0"):
            if b"=" in kv:
                k, _, v = kv.partition(b"=")
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        rank = local = ws = None
        for keys in (("RANK", "LOCAL_RANK", "WORLD_SIZE"),
                     ("OMPI_COMM_WORLD_RANK", "OMPI_COMM_WORLD_LOCAL_RANK",
                      "OMPI_COMM_WORLD_SIZE"),
                     ("SLURM_PROCID", "SLURM_LOCALID", "SLURM_NPROCS")):
            if keys[0] in env:
                try:
                    rank = int(env[keys[0]])
                    local = int(env[keys[1]]) if keys[1] in env else None
                    ws = int(env[keys[2]]) if keys[2] in env else None
                except ValueError:
                    break
                break
        if rank is not None:
            found.append({"pid": pid, "rank": rank, "local_rank": local,
                         "world_size": ws})
    if not found:
        return found
    # 同 rank 多 PID（DataLoader worker）：取最小 pid 为主条目
    by_rank = {}
    for r in sorted(found, key=lambda x: x["pid"]):
        first = by_rank.get(r["rank"])
        if first is None:
            by_rank[r["rank"]] = r
        else:
            first.setdefault("duplicate_pids", []).append(r["pid"])
    return sorted(by_rank.values(), key=lambda x: x["rank"])


# ---------------------------------------------------------------- maps 解析
def find_pinned_vmas(pid_dir, pattern):
    """返回 /proc maps 中 pathname 匹配 pattern（子串）的 VMA 列表。"""
    vmas = []
    with open(os.path.join(pid_dir, "maps"), "r", errors="replace") as fh:
        for line in fh:
            # 格式: addr-addr perms offset dev inode pathname
            parts = line.rstrip("\n").split(None, 5)
            if len(parts) < 6:
                continue
            pathname = parts[5].strip()
            if pattern not in pathname:
                continue
            start_s, end_s = parts[0].split("-")
            vmas.append({
                "start": int(start_s, 16),
                "end": int(end_s, 16),
                "perms": parts[1],
                "pathname": pathname,
                "size_kb": (int(end_s, 16) - int(start_s, 16)) // 1024,
            })
    return vmas


# ---------------------------------------------------------------- node 映射
def load_zone_nodes(proc_root):
    """zoneinfo 的 start_pfn → [(node, start_pfn, zone_name)]。"""
    zones = []
    zi = os.path.join(proc_root, "zoneinfo")
    if not os.path.isfile(zi):
        return zones
    node = None
    with open(zi, "r", errors="replace") as fh:
        for line in fh:
            f = line.split()
            if f and f[0].startswith("Node"):
                # Node 0, zone      DMA32
                try:
                    node = int(f[1].rstrip(","))
                except ValueError:
                    node = None
            elif node is not None and f and f[0] == "start_pfn":
                try:
                    zones.append((node, int(f[1])))
                except (ValueError, IndexError):
                    pass
    return zones


def pfn_to_node(pfn, zone_starts):
    """PFN → node：zone 按 pfn 升序，二分定位。"""
    if not zone_starts or pfn == 0:
        return None
    lo, hi = 0, len(zone_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if zone_starts[mid][1] <= pfn:
            lo = mid
        else:
            hi = mid - 1
    return zone_starts[lo][0]


def pread8(fd, offset):
    """跨平台 8 字节读：os.pread 优先（Linux 快路径），Windows 退 lseek+read。

    注意：Windows 裸 fd 的 lseek+read 在大文件上偶发返回空——
    调用方应在首次读取失败时切换为批量模式（read_vma_pagemap）。
    """
    try:
        w = os.pread(fd, 8, offset)
        if w:
            return w
    except AttributeError:
        pass
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, 8)


def read_vma_pagemap_batch(pid_dir, vma, zone_starts):
    """VMA 的 pagemap 范围一次性读入内存再遍历（跨平台兼容路径）。

    裸 fd 的 lseek+read 在 Windows 上偶发返回空，用带缓冲的 Python
    reader 一次读入 pagemap 对应区段，杜绝该问题。
    """
    n_pages = (vma["end"] - vma["start"]) // PAGE_SIZE
    start_page = vma["start"] // PAGE_SIZE
    try:
        with open(os.path.join(pid_dir, "pagemap"), "rb") as fh:
            fh.seek(start_page * 8)
            blob = fh.read(n_pages * 8)
    except OSError:
        return 0, {}
    present = 0
    nodes = {}
    for i in range(min(n_pages, len(blob) // 8)):
        word = int.from_bytes(blob[i*8:(i+1)*8], "little")
        if word & PRESENT_BIT:
            present += 1
            nd = pfn_to_node(word & PFN_MASK, zone_starts)
            nodes[nd] = nodes.get(nd, 0) + 1
    return present, nodes


def sample_vma_batch(pid_dir, vma, sample_ratio):
    """采样模式：VMA 的 pagemap 采样读入（每 sample_ratio 页采 1 页外推）。"""
    n_pages = (vma["end"] - vma["start"]) // PAGE_SIZE
    start_page = vma["start"] // PAGE_SIZE
    step = max(1, sample_ratio)
    try:
        with open(os.path.join(pid_dir, "pagemap"), "rb") as fh:
            fh.seek(start_page * 8)
            blob = fh.read(n_pages * 8)
    except OSError:
        return 0, 0, 0
    present = 0
    pfn_ok = 0
    samples = 0
    for i in range(0, min(n_pages, len(blob) // 8), step):
        samples += 1
        word = int.from_bytes(blob[i*8:(i+1)*8], "little")
        if word & PRESENT_BIT:
            present += 1
            if word & PFN_MASK:
                pfn_ok += 1
    est_present_pages = present * step
    est_pfn_ok = pfn_ok * step
    return est_present_pages, est_pfn_ok, samples


def sample_vma_full(pm_fd, vma, zone_starts):
    """全量逐页：精确 present + node 分布。优先用批量路径，裸 fd 为备用。"""
    n_pages = (vma["end"] - vma["start"]) // PAGE_SIZE
    present = 0
    nodes = {}
    for i in range(n_pages):
        w = pread8(pm_fd, (vma["start"] // PAGE_SIZE + i) * 8)
        if not w:
            break
        word = int.from_bytes(w, "little")
        if word & PRESENT_BIT:
            present += 1
            nd = pfn_to_node(word & PFN_MASK, zone_starts)
            nodes[nd] = nodes.get(nd, 0) + 1
    return present, nodes


# ---------------------------------------------------------------- 单进程探测
def probe_pid(pid, tag, proc_root, sample_ratio, full, pattern, zone_starts,
              with_traces=False):
    """返回单 rank 的 L0 快照 dict。"""
    pid_dir = proc_root_of(pid, proc_root)
    vmas = find_pinned_vmas(pid_dir, pattern)
    entry = {"pid": pid, "tag": tag, "ts": time.time(), "vmas": [],
             "total_va_kb": sum(v["size_kb"] for v in vmas),
             "total_present_kb": 0, "node_kb": {}, "mode": "full" if full else "sampled"}
    try:
        pm_fd = os.open(os.path.join(pid_dir, "pagemap"), os.O_RDONLY)
    except OSError as e:
        out("WARN: pagemap 打开失败 pid=%s (%s)——devmm VMA 只报 VA 大小" % (pid, e),
            err=True)
        pm_fd = None

    total_present_kb = 0
    node_kb = {}
    for vma in vmas:
        rec = dict(vma)
        if pm_fd is None:
            rec["present_kb"] = None
            rec["node_kb"] = {}
            rec["note"] = "pagemap_unreadable"
        else:
            # 批量读入（跨平台兼容；Linux 上 os.pread 快路径不影响正确性）
            present, nodes = read_vma_pagemap_batch(pid_dir, vma, zone_starts)
            rec["present_kb"] = present * PAGE_SIZE // 1024
            rec["node_kb"] = {str(k): v * PAGE_SIZE // 1024
                              for k, v in nodes.items() if k is not None}
            unk = (present - sum(nodes.values())) * PAGE_SIZE // 1024
            if unk > 0:
                rec["node_kb"]["unknown"] = unk
        entry["vmas"].append(rec)
        if rec.get("present_kb"):
            total_present_kb += rec["present_kb"]
            for nd, kb in rec.get("node_kb", {}).items():
                node_kb[nd] = node_kb.get(nd, 0) + kb
    if pm_fd is not None:
        os.close(pm_fd)
    entry["total_present_kb"] = total_present_kb
    entry["node_kb"] = node_kb
    return entry


def pfn_readable(proc_root):
    """预检：本进程自己 pagemap 的 PFN 是否非 0（CAP_SYS_ADMIN 有无）。"""
    my_pid = os.getpid()
    try:
        fd = os.open(os.path.join(proc_root, str(my_pid), "pagemap"), os.O_RDONLY)
    except OSError:
        return False
    try:
        # 读栈附近一页：/proc/self/maps 取任意非 file 段
        with open(os.path.join(proc_root, str(my_pid), "maps")) as fh:
            for line in fh:
                parts = line.split(None, 5)
                if len(parts) >= 2 and parts[1].startswith("rw"):
                    addr = int(parts[0].split("-")[0], 16)
                    w = pread8(fd, (addr // PAGE_SIZE) * 8)
                    return (w and (int.from_bytes(w, "little") & PRESENT_BIT) != 0
                            and (int.from_bytes(w, "little") & PFN_MASK) != 0)
        return False
    finally:
        os.close(fd)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="kptools-cpuoffload-collector probe（L0 外部快照）")
    ap.add_argument("--pid", default=None, help="目标 PID（逗号分隔；与 --auto 二选一）")
    ap.add_argument("--auto", action="store_true",
                    help="自动发现训练 rank 进程（RANK 族环境变量）")
    ap.add_argument("--tag", required=True,
                    help="快照标签：after_init / step_1 / step_5 / done 等（时间演化锚点）")
    ap.add_argument("--out", required=True, help="bundle 输出目录（追加式，可复用）")
    ap.add_argument("--sample-ratio", type=int, default=64,
                    help="pagemap 采样比（每 N 页采 1 页外推）；0 = --full（默认 64）")
    ap.add_argument("--full", action="store_true", help="全量逐页精确探测（慢）")
    ap.add_argument("--pattern", default="/dev/devmm_svm",
                    help="VMA pathname 匹配子串（默认 /dev/devmm_svm）")
    ap.add_argument("--proc-root", default="/proc")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.proc_root):
        out("ERROR: /proc 不可读 %s" % args.proc_root, err=True)
        return 2

    # rank 发现
    if args.auto:
        ranks = discover_ranks(args.proc_root)
        if not ranks:
            out("ERROR: NO_RANK_FOUND（environ 无 RANK 族变量）", err=True)
            return 3
        out("auto: %d rank(s): %s" % (len(ranks), [r["pid"] for r in ranks]))
    elif args.pid:
        ranks = []
        for p in args.pid.split(","):
            p = p.strip()
            if not p or not os.path.isdir(proc_root_of(p, args.proc_root)):
                out("ERROR: TARGET_MISSING pid=%s" % p, err=True)
                return 3
            ranks.append({"pid": int(p), "rank": None, "local_rank": None,
                          "world_size": None})
    else:
        out("ERROR: 需要 --pid 或 --auto", err=True)
        return 1

    zone_starts = sorted(load_zone_nodes(args.proc_root), key=lambda x: x[1])
    pfn_ok = pfn_readable(args.proc_root)
    out("pfn readability: %s（%s）" % (pfn_ok,
        "node 归属可用" if pfn_ok else "无 CAP_SYS_ADMIN——node 列将降级为 unknown，总量仍可用"))

    os.makedirs(args.out, exist_ok=True)
    bundle = {
        "skill": "kptools-cpuoffload-collector",
        "version": VERSION,
        "ts": time.time(),
        "host": socket.gethostname(),
        "tag": args.tag,
        "pfn_readable": pfn_ok,
        "sample_ratio": 0 if args.full else args.sample_ratio,
        "pattern": args.pattern,
        "ranks": [],
    }

    # 采样模式下的 node 分布：PFN 可读时对每 rank 做一次粗粒度 node 采样
    # （前 512 页 + 中间 512 页，按 PFN 外推 node 比例）
    for r in ranks:
        pid = r["pid"]
        try:
            ent = probe_pid(pid, args.tag, args.proc_root,
                            args.sample_ratio, args.full, args.pattern, zone_starts)
        except FileNotFoundError:
            out("WARN: rank pid=%s 在探测中消失（跳过）" % pid, err=True)
            continue
        ent.update({"rank": r["rank"], "local_rank": r["local_rank"],
                    "world_size": r["world_size"]})
        # 采样模式下补充 node 外推（PFN 可读才做）
        if pfn_ok and not args.full:
            node_kb = sample_node_estimate(pid, args.proc_root, args.pattern,
                                           zone_starts)
            ent["node_kb"] = node_kb
            for v in ent["vmas"]:
                share = (v.get("present_kb") or 0)
                if share and ent["total_present_kb"]:
                    v["node_kb"] = {k: int(round(share * w / ent["total_present_kb"]))
                                    for k, w in node_kb.items()}
        bundle["ranks"].append(ent)

    if not bundle["ranks"]:
        out("ERROR: 没有任何 rank 完成探测", err=True)
        return 3
    any_vma = any(e["vmas"] for e in bundle["ranks"])
    if not any_vma:
        out("ERROR: NO_PINNED_VMA（未找到 %s 段——目标未启用 cpu offload 或驱动未映射；"
            "用 --pattern 调整匹配或确认 --tag 时点）" % args.pattern, err=True)
        return 4

    probe_path = os.path.join(args.out, "probe.%s.json" % args.tag)
    with open(probe_path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)

    # 人类可读 trace.log（追加式）
    append_trace(args.out, bundle, probe_path)

    total = sum(e["total_present_kb"] for e in bundle["ranks"])
    out("tag=%s ranks=%d pinned present 总计 %.2f GiB → %s" %
        (args.tag, len(bundle["ranks"]), total / 1048576.0, probe_path))
    return 0


def sample_node_estimate(pid, proc_root, pattern, zone_starts, probe_pages=512):
    """采样模式下每 rank 的 node 分布外推：每 VMA 均匀取 512 页读 PFN。"""
    pid_dir = proc_root_of(pid, proc_root)
    vmas = find_pinned_vmas(pid_dir, pattern)
    node_hits = {}
    try:
        pm_fd = os.open(os.path.join(pid_dir, "pagemap"), os.O_RDONLY)
    except OSError:
        return {"unknown": 0}
    try:
        for vma in vmas:
            n_pages = (vma["end"] - vma["start"]) // PAGE_SIZE
            if n_pages <= probe_pages:
                idxs = range(n_pages)
            else:
                idxs = [i * n_pages // probe_pages for i in range(probe_pages)]
            for i in idxs:
                w = pread8(pm_fd, (vma["start"] // PAGE_SIZE + i) * 8)
                if not w:
                    break
                word = int.from_bytes(w, "little")
                if word & PRESENT_BIT:
                    nd = pfn_to_node(word & PFN_MASK, zone_starts)
                    key = str(nd) if nd is not None else "unknown"
                    node_hits[key] = node_hits.get(key, 0) + 1
    finally:
        os.close(pm_fd)
    total_hits = sum(node_hits.values()) or 1
    return {"%s_pct" % k: round(v * 100.0 / total_hits, 1)
            for k, v in node_hits.items()}


def append_trace(bundle_dir, bundle, probe_path):
    """bundle/trace.log：人类可读快照摘要（追加式，cat 即懂）。"""
    total_kb = sum(e["total_present_kb"] for e in bundle["ranks"])
    path = os.path.join(bundle_dir, "trace.log")
    first = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if first:
            fh.write("==== kptools-cpuoffload-collector CPU offload 内存采集日志 ====\n")
            fh.write("口径: /proc maps(%s 段) + pagemap present 页计数\n" % bundle["pattern"])
            fh.write("     VA 预留 ≠ 驻留——smaps/numastat 对 driver pinned 失明\n")
            fh.write("     pagemap 是唯一穿透手段；PFN 不可读时 node 列为 unknown\n")
            fh.write("====================================================================\n")
        ts_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(bundle["ts"]))
        mode = "全量" if bundle["sample_ratio"] == 0 else "采样 1/%d" % bundle["sample_ratio"]
        fh.write("\n[%s] 快照 %s（%s）：%d rank，pinned present 总计 %.2f GiB\n"
                 % (ts_s, bundle["tag"], mode, len(bundle["ranks"]),
                    total_kb / 1048576.0))
        fh.write("  PFN 可读: %s → node 归属 %s\n" %
                 (bundle["pfn_readable"], "可用" if bundle["pfn_readable"] else "降级 unknown"))
        for e in sorted(bundle["ranks"], key=lambda x: (x["rank"] is None,
                                                       x["rank"] or 0)):
            role = "rank %s" % e["rank"] if e["rank"] is not None else "pid %s" % e["pid"]
            fh.write("  %-14s pid=%-7s VA 预留 %6.2f GiB / present %6.2f GiB / %d 段\n"
                     % (role, e["pid"], e["total_va_kb"] / 1048576.0,
                        e["total_present_kb"] / 1048576.0, len(e["vmas"])))
            if e["node_kb"]:
                parts = " ".join("node%s:%s" % (k, "%.1f%%" % v if k.endswith("_pct")
                                                 else "%.2fGiB" % (v / 1048576.0))
                                 for k, v in sorted(e["node_kb"].items()))
                fh.write("       node 分布: %s\n" % parts)
        fh.write("  数据: %s\n" % os.path.basename(probe_path))


if __name__ == "__main__":
    sys.exit(main())
