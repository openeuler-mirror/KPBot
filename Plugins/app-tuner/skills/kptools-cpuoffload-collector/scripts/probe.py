#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-cpuoffload-collector probe — L0 外部快照（零侵入）。

对目标进程读 /proc：
  maps      → 找 driver pinned 的 VMA（pathname 含 /dev/devmm_svm，可配置）
  pagemap   → 每 VMA 读物理页 present 位（真实驻留）与 PFN（需 CAP_SYS_ADMIN）
  zoneinfo  → PFN → NUMA node 映射（无权限时 node 降级 unknown）

为什么不用 RSS/smaps/numastat：driver pinned 内存走 special PTE（无
struct page），smaps/numastat 对它全部失明——只有 pagemap 能穿透；
VMA size 是 VA 预留不等于驻留，必须按 present 页计数。

采样模式：--sample-ratio N（默认 64 = 每 N 页读 1 页按比例外推，
present/node 均为 KB 外推值）；--full 全量逐页精确（1 GiB 约数十秒）。
基页大小从运行内核获取（os.sysconf，64 KiB 基页主机正确），可用
AT_PAGE_SIZE 覆盖（测试注入）。

输出（bundle 目录）：
  probe.<tag>.json   # 本 tag 的 L0 快照（rank/pid 维度，同 tag 覆盖需 --force）
  trace.log          # 人类可读快照摘要（追加式）
  run_meta.json      # world_size/device_node_map（首个快照写一次）

用法：
  python3 probe.py --pid <PID> --tag after_init --out <bundle>
  python3 probe.py --auto --tag step_1 --out <bundle>     # torchrun rank 进程
  python3 probe.py --pid <PID> --tag step_5 --sample-ratio 8 --out <bundle>

退出码：0 成功；1 参数错误（含 tag 已存在、多作业需指定）；2 /proc 不可读；
3 目标不存在/无 rank；4 未找到任何 pinned VMA（目标未启用 cpu offload）；
5 maps/pagemap 无权限或不可读；6 部分 rank 探测失败（partial，明细在 JSON）
"""
import argparse
import glob as globmod
import json
import os
import socket
import sys
import time

VERSION = "1.1.0"
PFN_MASK = (1 << 55) - 1
PRESENT_BIT = 1 << 63


def _page_size():
    """基页大小：AT_PAGE_SIZE（测试注入）> 内核 sysconf > 4096 兜底。"""
    try:
        v = int(os.environ.get("AT_PAGE_SIZE", "0"))
        if v > 0:
            return v
    except ValueError:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 4096


PAGE_SIZE = _page_size()


def out(msg, err=False):
    print(msg, file=sys.stderr if err else sys.stdout)


def proc_root_of(pid, proc_root):
    return os.path.join(proc_root, str(pid))


# ---------------------------------------------------------------- rank 发现
def _parse_environ(pid_dir):
    env = {}
    try:
        with open(os.path.join(pid_dir, "environ"), "rb") as fh:
            raw = fh.read()
    except OSError:
        return env
    for kv in raw.split(b"\0"):
        if b"=" in kv:
            k, _, v = kv.partition(b"=")
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def _read_stat_ppid(pid_dir):
    """ppid from /proc/<pid>/stat（comm 可含空格，取最后一个 ')' 之后）。"""
    try:
        with open(os.path.join(pid_dir, "stat"), "r", errors="replace") as fh:
            s = fh.read()
    except OSError:
        return None
    rp = s.rfind(")")
    if rp < 0:
        return None
    fields = s[rp + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _tree_root(pid, proc_root, guard=64):
    """沿 ppid 上溯到最顶层非 init 祖先——torchrun/mpirun 作业的组根。"""
    cur = pid
    for _ in range(guard):
        ppid = _read_stat_ppid(proc_root_of(cur, proc_root))
        if ppid is None or ppid <= 1:
            return cur
        cur = ppid
    return cur


def _rank_fields(env):
    """RANK 族环境变量（torchrun/mpirun/srun）→ (rank, local_rank, world_size)。"""
    for keys in (("RANK", "LOCAL_RANK", "WORLD_SIZE"),
                 ("OMPI_COMM_WORLD_RANK", "OMPI_COMM_WORLD_LOCAL_RANK",
                  "OMPI_COMM_WORLD_SIZE"),
                 ("SLURM_PROCID", "SLURM_LOCALID", "SLURM_NPROCS")):
        if keys[0] in env:
            try:
                return (int(env[keys[0]]),
                        int(env[keys[1]]) if keys[1] in env else None,
                        int(env[keys[2]]) if keys[2] in env else None)
            except ValueError:
                return (None, None, None)
    return (None, None, None)


def discover_ranks(proc_root):
    """发现 rank 进程并按作业分组。

    返回 [{"key": <作业标识>, "ranks": [{pid, rank, local_rank, world_size,
    duplicate_pids}]}]。作业标识：TORCHELASTIC_RUN_ID（torchrun 每次启动
    唯一），缺失时退化为进程树根（launcher pid）。同作业内同 rank 多 PID
    （DataLoader worker）取最小 pid 为主条目。
    """
    candidates = []
    for d in sorted(globmod.glob(os.path.join(proc_root, "[0-9]*"))):
        try:
            pid = int(os.path.basename(d))
        except ValueError:
            continue
        env = _parse_environ(d)
        rank, local, ws = _rank_fields(env)
        if rank is None:
            continue
        rid = env.get("TORCHELASTIC_RUN_ID")
        key = "run_id:%s" % rid if rid else "tree:%d" % _tree_root(pid, proc_root)
        candidates.append({"pid": pid, "rank": rank, "local_rank": local,
                           "world_size": ws, "_key": key})
    groups = {}
    for c in candidates:
        groups.setdefault(c["_key"], []).append(c)
    out_groups = []
    for key in sorted(groups, key=lambda k: min(c["pid"] for c in groups[k])):
        by_rank = {}
        for r in sorted(groups[key], key=lambda x: x["pid"]):
            first = by_rank.get(r["rank"])
            if first is None:
                by_rank[r["rank"]] = {k: r[k] for k in
                                      ("pid", "rank", "local_rank", "world_size")}
            else:
                first.setdefault("duplicate_pids", []).append(r["pid"])
        out_groups.append({"key": key,
                           "ranks": sorted(by_rank.values(),
                                           key=lambda x: x["rank"])})
    return out_groups


# ---------------------------------------------------------------- maps 解析
def find_pinned_vmas(pid_dir, pattern):
    """返回 /proc maps 中 pathname 匹配 pattern（子串）的 VMA 列表。

    目标消失抛 FileNotFoundError；跨 uid 无权限抛 PermissionError（由
    main 统一处理为 missing/退出码 5）。
    """
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
    """zoneinfo 的 start_pfn → [(node, start_pfn)]。

    真实内核输出行是 "  start_pfn:           0"（带冒号，Linux >= 3.10），
    解析时按 token 去尾部冒号兼容两种写法。
    """
    zones = []
    zi = os.path.join(proc_root, "zoneinfo")
    if not os.path.isfile(zi):
        return zones
    node = None
    with open(zi, "r", errors="replace") as fh:
        for line in fh:
            f = line.split()
            if not f:
                continue
            if f[0].startswith("Node") and len(f) > 1 and f[1].endswith(","):
                # Node 0, zone      DMA32
                try:
                    node = int(f[1].rstrip(","))
                except ValueError:
                    node = None
            elif node is not None and f[0].rstrip(":") == "start_pfn":
                try:
                    zones.append((node, int(f[1])))
                except (ValueError, IndexError):
                    pass
    return zones


def pfn_to_node(pfn, zone_starts):
    """PFN → node：zone 按 pfn 升序，二分定位。pfn=0（内核屏蔽）→ None。"""
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
    """跨平台 8 字节读：os.pread 优先（Linux 快路径），Windows 退 lseek+read。"""
    try:
        w = os.pread(fd, 8, offset)
        if w:
            return w
    except AttributeError:
        pass
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, 8)


def _read_pagemap_bytes(pid_dir, start_page, n_pages):
    """一次性读入 pagemap 区段（跨平台兼容；Windows 裸 fd 逐读偶发返回空）。"""
    path = os.path.join(pid_dir, "pagemap")
    with open(path, "rb") as fh:
        fh.seek(start_page * 8)
        return fh.read(n_pages * 8)


def count_vma_full(pid_dir, vma, zone_starts):
    """全量逐页：精确 present + per-node 页数 + 未知 node 页数。

    present 但 PFN 被屏蔽/zone 外的页计入 unknown（不混入任何 node 桶）。
    """
    n_pages = (vma["end"] - vma["start"]) // PAGE_SIZE
    start_page = vma["start"] // PAGE_SIZE
    try:
        blob = _read_pagemap_bytes(pid_dir, start_page, n_pages)
    except FileNotFoundError:
        raise  # 进程消失：交给上层记 missing，不得假报 present=0
    except OSError:
        return 0, {}, 0
    present = 0
    nodes = {}
    unknown = 0
    for i in range(min(n_pages, len(blob) // 8)):
        word = int.from_bytes(blob[i * 8:(i + 1) * 8], "little")
        if word & PRESENT_BIT:
            present += 1
            nd = pfn_to_node(word & PFN_MASK, zone_starts)
            if nd is None:
                unknown += 1
            else:
                nodes[nd] = nodes.get(nd, 0) + 1
    return present, nodes, unknown


def count_vma_sampled(pid_dir, vma, step, zone_starts):
    """按步长读 pagemap（真采样：只读 1/step 的页，不是整段读入）。

    返回采样到的 present/per-node/unknown 页数与采样数，外推由调用方做。
    """
    n_pages = (vma["end"] - vma["start"]) // PAGE_SIZE
    start_page = vma["start"] // PAGE_SIZE
    present = 0
    nodes = {}
    unknown = 0
    samples = 0
    path = os.path.join(pid_dir, "pagemap")
    with open(path, "rb") as fh:  # 进程消失时 FileNotFoundError 由上层记 missing
        for i in range(0, n_pages, step):
            fh.seek((start_page + i) * 8)
            w = fh.read(8)
            if len(w) < 8:
                break
            samples += 1
            word = int.from_bytes(w, "little")
            if word & PRESENT_BIT:
                present += 1
                nd = pfn_to_node(word & PFN_MASK, zone_starts)
                if nd is None:
                    unknown += 1
                else:
                    nodes[nd] = nodes.get(nd, 0) + 1
    return present, nodes, unknown, samples


def _kb(pages):
    return pages * PAGE_SIZE // 1024


# ---------------------------------------------------------------- 单进程探测
def probe_pid(pid, tag, proc_root, sample_ratio, full, pattern, zone_starts):
    """返回单 rank 的 L0 快照 dict（present/node 全 KB 口径）。"""
    pid_dir = proc_root_of(pid, proc_root)
    vmas = find_pinned_vmas(pid_dir, pattern)
    step = 1 if full else max(1, sample_ratio)
    entry = {"pid": pid, "tag": tag, "ts": time.time(), "vmas": [],
             "total_va_kb": sum(v["size_kb"] for v in vmas),
             "total_present_kb": 0, "node_kb": {},
             "mode": "full" if full else "sampled_1_%d" % step}
    try:
        pm_fd = os.open(os.path.join(pid_dir, "pagemap"), os.O_RDONLY)
    except FileNotFoundError:
        raise
    except OSError as e:
        entry["pagemap_unreadable"] = True
        entry["pagemap_error"] = str(e)
        out("WARN: pagemap 打开失败 pid=%s (%s)——本 rank 只报 VA 大小" % (pid, e),
            err=True)
        pm_fd = None
    else:
        os.close(pm_fd)  # 批量路径按 VMA 自行 open/seek，无需常驻 fd

    total_present_kb = 0
    node_kb = {}
    for vma in vmas:
        rec = dict(vma)
        if pm_fd is None:
            rec["present_kb"] = None
            rec["node_kb"] = {}
            rec["note"] = "pagemap_unreadable"
        else:
            if full:
                present, nodes, unknown = count_vma_full(pid_dir, vma,
                                                          zone_starts)
                rec["present_kb"] = _kb(present)
                rec["node_kb"] = {str(k): _kb(v) for k, v in nodes.items()}
            else:
                present, nodes, unknown, samples = count_vma_sampled(
                    pid_dir, vma, step, zone_starts)
                est_present = min(present * step, vma["size_kb"] * 1024 // PAGE_SIZE)
                rec["present_kb"] = _kb(est_present)
                rec["node_kb"] = {str(k): _kb(v * step)
                                  for k, v in nodes.items()}
                rec["sampled"] = {"samples": samples, "step": step}
            if unknown:
                ukb = _kb(unknown * step) if not full else _kb(unknown)
                rec["node_kb"]["unknown"] = ukb
        entry["vmas"].append(rec)
        if rec.get("present_kb"):
            total_present_kb += rec["present_kb"]
            for nd, kb in rec.get("node_kb", {}).items():
                node_kb[nd] = node_kb.get(nd, 0) + kb
    entry["total_present_kb"] = total_present_kb
    entry["node_kb"] = node_kb
    return entry


def pfn_readable(proc_root):
    """预检：本进程自己 pagemap 的 PFN 是否非 0（CAP_SYS_ADMIN 有无）。"""
    my_pid = os.getpid()
    try:
        fd = os.open(os.path.join(proc_root, str(my_pid), "pagemap"),
                     os.O_RDONLY)
    except OSError:
        return False
    try:
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


# ---------------------------------------------------------------- NPU 拓扑
def scan_npu_nodes(sys_root):
    """PCI 设备 → NUMA node（vendor 0x19e5 + class 0x120000 双校验）。

    单查 vendor 会把 Huawei 网卡（hns3/Hi1822）误计入 NPU 集合，
    双校验排除同 vendor 的非加速设备（对齐 kptools-proc-collector 先例）。
    """
    out_map = {}
    base = os.path.join(sys_root, "bus", "pci", "devices")
    if not os.path.isdir(base):
        return out_map
    for d in sorted(globmod.glob(os.path.join(base, "*"))):
        name = os.path.basename(d)
        try:
            with open(os.path.join(d, "vendor")) as fh:
                vendor = fh.read().strip().lower()
            with open(os.path.join(d, "class")) as fh:
                klass = fh.read().strip().lower()
            with open(os.path.join(d, "numa_node")) as fh:
                node = int(fh.read().strip() or "-1")
        except (OSError, ValueError):
            continue
        if vendor == "0x19e5" and klass.startswith("0x120000"):
            out_map[name] = {"node": node}
    return out_map


def write_run_meta(bundle_dir, ranks, sys_root):
    """run_meta.json：world/设备亲和基准，bundle 首个快照时写一次。"""
    path = os.path.join(bundle_dir, "run_meta.json")
    if os.path.exists(path):
        return path
    ws = [r.get("world_size") for r in ranks if r.get("world_size")]
    meta = {
        "skill": "kptools-cpuoffload-collector",
        "version": VERSION,
        "ts": time.time(),
        "world_size": max(ws) if ws else None,
        "device_node_map": scan_npu_nodes(sys_root),
        "sys_root": sys_root,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- main
class _ArgParser(argparse.ArgumentParser):
    """用法错误统一退 1（argparse 默认退 2 会与「2=/proc 不可读」撞码）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        out("ERROR: 参数错误：%s（--help 查看用法）" % message, err=True)
        sys.exit(1)


def main(argv=None):
    ap = _ArgParser(
        description="kptools-cpuoffload-collector probe（L0 外部快照）")
    ap.add_argument("--pid", default=None,
                    help="目标 PID（逗号分隔；与 --auto 二选一）")
    ap.add_argument("--auto", action="store_true",
                    help="自动发现训练 rank 进程（RANK 族环境变量）")
    ap.add_argument("--tag", required=True,
                    help="快照标签：after_init / step_1 / step_5 / done 等（时间锚点）")
    ap.add_argument("--out", required=True, help="bundle 输出目录（追加式，可复用）")
    ap.add_argument("--sample-ratio", type=int, default=64,
                    help="pagemap 采样比（每 N 页采 1 页外推）；0 等价 --full（默认 64）")
    ap.add_argument("--full", action="store_true",
                    help="全量逐页精确探测（慢）")
    ap.add_argument("--pattern", default="/dev/devmm_svm",
                    help="VMA pathname 匹配子串（默认 /dev/devmm_svm）")
    ap.add_argument("--force", action="store_true",
                    help="允许覆盖已存在的 probe.<tag>.json（trace.log 始终追加）")
    ap.add_argument("--proc-root", default="/proc")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.proc_root):
        out("ERROR: /proc 不可读 %s" % args.proc_root, err=True)
        return 2

    # rank 发现
    if args.auto:
        groups = discover_ranks(args.proc_root)
        if not groups:
            out("ERROR: NO_RANK_FOUND（environ 无 RANK 族变量）", err=True)
            return 3
        if len(groups) > 1:
            out("ERROR: MULTI_JOB——发现 %d 个训练作业，请用 --pid 指定目标：" %
                len(groups), err=True)
            for g in groups:
                out("  作业 %s: ranks=%s pids=%s" %
                    (g["key"], [r["rank"] for r in g["ranks"]],
                     [r["pid"] for r in g["ranks"]]), err=True)
            return 1
        ranks = groups[0]["ranks"]
        out("auto: %d rank(s)（作业 %s）: %s" %
            (len(ranks), groups[0]["key"], [r["pid"] for r in ranks]))
    elif args.pid:
        ranks = []
        for p in args.pid.split(","):
            p = p.strip()
            if not p.isdigit():
                out("ERROR: 参数错误：--pid 需为数字，got %r" % p, err=True)
                return 1
            if not os.path.isdir(proc_root_of(p, args.proc_root)):
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
        "node 归属可用" if pfn_ok else "无 CAP_SYS_ADMIN——PFN 屏蔽的页归入 unknown，总量仍可用"))

    probe_path = os.path.join(args.out, "probe.%s.json" % args.tag)
    if os.path.exists(probe_path) and not args.force:
        out("ERROR: TAG_EXISTS %s 已存在——换一个 tag，或加 --force 覆盖"
            "（trace.log 为追加式不受影响）" % probe_path, err=True)
        return 1

    os.makedirs(args.out, exist_ok=True)
    bundle = {
        "skill": "kptools-cpuoffload-collector",
        "version": VERSION,
        "ts": time.time(),
        "host": socket.gethostname(),
        "tag": args.tag,
        "pfn_readable": pfn_ok,
        "sample_ratio": 0 if args.full else max(1, args.sample_ratio),
        "pattern": args.pattern,
        "expected_ranks": len(ranks),
        "ranks": [],
        "missing": [],
    }

    missing = []
    for r in ranks:
        pid = r["pid"]
        try:
            ent = probe_pid(pid, args.tag, args.proc_root,
                            args.sample_ratio, args.full, args.pattern,
                            zone_starts)
        except FileNotFoundError:
            out("WARN: rank pid=%s 在探测中消失（记为 missing）" % pid, err=True)
            missing.append({"pid": pid, "rank": r.get("rank"),
                            "reason": "process_gone"})
            continue
        except PermissionError as e:
            out("WARN: rank pid=%s 无权限读 /proc（记为 missing：%s）" % (pid, e),
                err=True)
            missing.append({"pid": pid, "rank": r.get("rank"),
                            "reason": "permission_denied"})
            continue
        ent.update({"rank": r["rank"], "local_rank": r["local_rank"],
                    "world_size": r["world_size"]})
        bundle["ranks"].append(ent)
    bundle["missing"] = missing
    bundle["partial"] = bool(missing)

    if not bundle["ranks"]:
        # 全部 rank 失败：区分"进程没了"与"无权限读"——后者按契约退 5
        # （部分失败退 6 的语义不变），否则跨 uid --pid 场景被引向"进程
        # 不存在"的排障路径
        if missing and all(m.get("reason") == "permission_denied"
                           for m in missing):
            out("ERROR: PROCFS_PERMISSION_DENIED（所有目标的 /proc 均无权限读"
                "——跨 uid 需同 uid 或 root）", err=True)
            return 5
        out("ERROR: 没有任何 rank 完成探测", err=True)
        return 3

    tss = [e["ts"] for e in bundle["ranks"]]
    bundle["ts_min"] = min(tss)
    bundle["ts_max"] = max(tss)
    bundle["duration_s"] = round(max(tss) - min(tss), 3)

    any_vma = any(e["vmas"] for e in bundle["ranks"])
    all_pm_dead = all(e.get("pagemap_unreadable") for e in bundle["ranks"])
    rc = 0
    if not any_vma:
        out("ERROR: NO_PINNED_VMA（未找到 %s 段——目标未启用 cpu offload 或驱动未映射；"
            "用 --pattern 调整匹配或确认 --tag 时点）" % args.pattern, err=True)
        rc = 4
    elif all_pm_dead:
        out("ERROR: PAGEMAP_UNREADABLE（所有 rank 的 pagemap 均不可读——跨 uid 需同 uid "
            "或 root；本快照只有 VA 大小，判读无意义）", err=True)
        rc = 5
    elif bundle["partial"]:
        rc = 6

    if rc in (0, 6):
        # 只有有效数据才落盘——rc=4/5 不写，tag 不被失败运行占用
        run_meta_path = write_run_meta(
            args.out, ranks, os.environ.get("AT_SYS_ROOT", "/sys"))
        with open(probe_path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, indent=2)
        append_trace(args.out, bundle, probe_path)
    else:
        run_meta_path = "(未落盘)"

    total = sum(e["total_present_kb"] for e in bundle["ranks"])
    out("tag=%s ranks=%d/%d pinned present 总计 %.2f GiB → %s（run_meta: %s）" %
        (args.tag, len(bundle["ranks"]), bundle["expected_ranks"],
         total / 1048576.0, probe_path, run_meta_path))
    if rc == 6:
        out("WARN: PARTIAL——%d rank 探测失败（明细见 JSON missing 字段），"
            "退出码 6" % len(missing), err=True)
    return rc


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
            fh.write("     pagemap 是唯一穿透手段；node 分布为 KB 口径（含 unknown 桶）\n")
            fh.write("====================================================================\n")
        ts_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(bundle["ts"]))
        mode = "全量" if bundle["sample_ratio"] == 0 else "采样 1/%d（外推）" % bundle["sample_ratio"]
        fh.write("\n[%s] 快照 %s（%s）：%d/%d rank，pinned present 总计 %.2f GiB\n"
                 % (ts_s, bundle["tag"], mode, len(bundle["ranks"]),
                    bundle["expected_ranks"], total_kb / 1048576.0))
        fh.write("  时间窗: %.3fs（多 rank 顺序采集，各 rank ts 见 JSON）  PFN 可读: %s\n" %
                 (bundle.get("duration_s", 0.0), bundle["pfn_readable"]))
        if bundle.get("partial"):
            fh.write("  PARTIAL: %d rank 失败——%s\n" %
                     (len(bundle["missing"]),
                      ", ".join("pid=%s(%s)" % (m["pid"], m["reason"])
                                for m in bundle["missing"])))
        for e in sorted(bundle["ranks"], key=lambda x: (x["rank"] is None,
                                                        x["rank"] or 0)):
            role = "rank %s" % e["rank"] if e["rank"] is not None else "pid %s" % e["pid"]
            pm = " pagemap不可读" if e.get("pagemap_unreadable") else ""
            fh.write("  %-14s pid=%-7s VA 预留 %6.2f GiB / present %6.2f GiB / %d 段%s\n"
                     % (role, e["pid"], e["total_va_kb"] / 1048576.0,
                        (e["total_present_kb"] or 0) / 1048576.0,
                        len(e["vmas"]), pm))
            if e["node_kb"]:
                parts = " ".join("%s:%.2fGiB" % (k, v / 1048576.0)
                                 for k, v in sorted(e["node_kb"].items()))
                fh.write("       node 分布(KB): %s\n" % parts)
        fh.write("  数据: %s\n" % os.path.basename(probe_path))


if __name__ == "__main__":
    sys.exit(main())
