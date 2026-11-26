#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-train-mem-affinity collect_rank_mem — 训练 rank 内存 NUMA 归属快照

发现训练 rank 进程（environ 的 RANK/OMPI_COMM_WORLD_RANK/SLURM_PROCID...），
聚合每个 rank 的 /proc/PID/numa_maps，输出 node×{anon,file} 驻留分布、
覆盖率校验、top VMA 归因与判据粗筛 hints；支持 --diff 增量对比与
--affinity-dir 联合 kptools-thread-affinity-collector。纯 python3 stdlib（>=3.6）。

口径：观测的是驻留（RSS）页的 node 分布，不是 malloc"申请"；
anon（私有分配）为判据主口径，file 页（共享缓存）仅归因参考。

输出：stdout markdown + <out>/train_mem_affinity.json
退出码：0 成功（含 hints）；1 参数错误（含 --pid 非数字、多作业需指定）；
        2 /proc 不可读；3 未发现任何 rank 进程；4 numa_maps 全部不可读
"""
import argparse
import glob as globmod
import json
import os
import re
import socket
import sys
import time

VERSION = "1.1.0"

NODE_RE = re.compile(r"^N(\d+)$")

# rank 环境变量族（按优先级；命中一组即用该组）
# 元组：(族名, rank_key, local_rank_key, world_size_key, local_size_key)
# local_size_key = 本机期望 rank 数（多机训练时 WORLD_SIZE 是跨主机总数，
# 本机校验必须用本机口径；pmi 族无等价字段 → None = 本机期望数未知）
RANK_ENV_FAMILIES = [
    ("torchrun", "RANK", "LOCAL_RANK", "WORLD_SIZE", "LOCAL_WORLD_SIZE"),
    ("openmpi", "OMPI_COMM_WORLD_RANK", "OMPI_COMM_WORLD_LOCAL_RANK",
     "OMPI_COMM_WORLD_SIZE", "OMPI_COMM_WORLD_LOCAL_SIZE"),
    ("slurm", "SLURM_PROCID", "SLURM_LOCALID", "SLURM_NPROCS",
     "SLURM_LOCAL_NTASKS"),
    ("pmi", "PMI_RANK", "PMI_LOCAL_RANK", "PMI_SIZE", None),
]


# ------------------------------------------------------------------ 工具

def parse_cpulist(s):
    """'0-3,8' → {0,1,2,3,8}；失败返回 None"""
    if not s or s == "?":
        return None
    out = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                return None
        else:
            try:
                out.add(int(part))
            except ValueError:
                return None
    return out


def fmt_mb(kb):
    return "%.1f" % (kb / 1024.0)


def fmt_pct(x):
    return "%.0f%%" % (x * 100.0)


def read_text(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# ------------------------------------------------------------------ rank 发现

def read_environ(proc_root, pid):
    path = os.path.join(proc_root, str(pid), "environ")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    env = {}
    for kv in data.split(b"\0"):
        if b"=" in kv:
            k, _, v = kv.partition(b"=")
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def rank_from_env(env, extra_key=None):
    """返回 (rank, local_rank, world_size, matched_key, local_world_size) 或 None"""
    if env is None:
        return None
    keys = list(RANK_ENV_FAMILIES)
    if extra_key:
        keys = [("custom", extra_key, None, None, None)] + keys
    for fam, rk, lk, wk, lsk in keys:
        if rk in env:
            try:
                rank = int(env[rk])
            except ValueError:
                continue
            local_rank = world_size = local_size = None
            if lk and lk in env:
                try:
                    local_rank = int(env[lk])
                except ValueError:
                    pass
            if wk and wk in env:
                try:
                    world_size = int(env[wk])
                except ValueError:
                    pass
            if lsk and lsk in env:
                try:
                    local_size = int(env[lsk])
                except ValueError:
                    pass
            return rank, local_rank, world_size, rk, local_size
    return None


def _read_stat_ppid(proc_root, pid):
    """ppid from /proc/<pid>/stat（comm 可含空格，取最后一个 ')' 之后）。"""
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), "r",
                  errors="replace") as fh:
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


def _tree_root(proc_root, pid, guard=64):
    """沿 ppid 上溯到最顶层非 init 祖先——torchrun/mpirun 作业的组根。

    容器化训练（Docker CMD / K8s ENTRYPOINT 即 torchrun）：launcher 就是
    PID 1，rank worker 的 ppid 恒为 1——此时返回 1（而非 rank 自身 PID），
    使所有 rank 共享同一树根，不把每个 rank 当独立作业（MULTI_JOB 误报）。
    """
    cur = pid
    for _ in range(guard):
        ppid = _read_stat_ppid(proc_root, cur)
        if ppid is None or ppid <= 0:
            return cur
        if ppid == 1:
            # launcher 即 PID 1（容器化）→ 所有 PID-1 子进程共享根
            return 1
        cur = ppid
    return cur


def _job_key(proc_root, pid, env):
    """作业标识：TORCHELASTIC_RUN_ID（torchrun 每次启动唯一），缺失时
    退化为进程树根（launcher pid）——本机并行两个训练作业时各自的
    rank 0 不会互相混入。"""
    rid = (env or {}).get("TORCHELASTIC_RUN_ID")
    if rid:
        return "run_id:%s" % rid
    return "tree:%d" % _tree_root(proc_root, pid)


def _dedup_by_rank(raw):
    """同 rank 多 PID（DataLoader worker 继承 RANK）归并：取数值 PID
    最小者为主条目（主进程先于 fork 出的 worker 创建），其余记入
    duplicate_pids。rank 为 None 的条目不归并（手工指定的非训练进程）。"""
    by_rank = {}
    order = []
    for r in sorted(raw, key=lambda x: x["pid"]):
        rk = r["rank"]
        if rk is None:
            order.append(r)
            continue
        first = by_rank.get(rk)
        if first is None:
            by_rank[rk] = r
            order.append(r)
        else:
            first.setdefault("duplicate_pids", []).append(r["pid"])
    return sorted(order, key=lambda x: (x["rank"] is None, x["rank"] or 0))


def discover_ranks(proc_root, extra_key=None):
    """扫描 /proc/*/environ，按**作业分组**返回 rank 进程。

    返回 (groups, unreadable)：groups = [{"key": <作业标识>, "ranks": [...]}]。
    同一作业内同 rank 多 PID（DataLoader worker fork/spawn 继承 RANK/
    LOCAL_RANK/WORLD_SIZE 是常态）取 PID 最小者为主条目、其余记入
    duplicate_pids——校验门/C4/diff 均按去重后的 rank 语义工作。
    多作业时由 main 报 MULTI_JOB 并要求 --pid 显式指定。
    """
    found, unreadable = [], []
    for d in sorted(globmod.glob(os.path.join(proc_root, "[0-9]*"))):
        pid_s = os.path.basename(d)
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        env = read_environ(proc_root, pid)
        if env is None:
            unreadable.append(pid)
            continue
        r = rank_from_env(env, extra_key)
        if r is None:
            continue
        rank, local_rank, world_size, matched, local_size = r
        found.append({"pid": pid, "rank": rank, "local_rank": local_rank,
                      "world_size": world_size, "rank_env": matched,
                      "local_world_size": local_size,
                      "_job": _job_key(proc_root, pid, env)})
    groups = {}
    for r in found:
        groups.setdefault(r.pop("_job"), []).append(r)
    out = [{"key": k, "ranks": _dedup_by_rank(v)}
           for k, v in sorted(groups.items(),
                              key=lambda kv: min(x["pid"] for x in kv[1]))]
    return out, unreadable


# ------------------------------------------------------------------ numa_maps 聚合

def parse_numa_maps(text, top_n=0):
    """聚合 numa_maps 文本。

    行格式（注意：没有 perms/offset/dev/inode，那是 smaps）：
        <addr> <policy> [file=path|heap|stack] key=value... N<x>=<pages> ...
    返回 (nodes_agg, vmas)：
        nodes_agg: {node: {"pages","total_kb","anon_kb","file_kb"}}
        vmas: [{"label","total_kb","anon_kb","nodes":{node:kb}}] 驻留降序
    """
    nodes_agg = {}
    vmas = []
    for line in text.splitlines():
        if "N" not in line:            # 快速筛：无 N<node>= 的行无驻留
            continue
        toks = line.split()
        if len(toks) < 2:
            continue
        label = ""
        kvs = {}
        for t in toks[2:]:
            if t == "heap":
                label = label or "[heap]"
            elif t == "stack":
                label = label or "[stack]"
            elif t.startswith("file="):
                label = t[5:]
            elif "=" in t:
                k, _, v = t.partition("=")
                kvs[k] = v
            # 其余裸 token（policy 参数等）忽略
        per_node = {}
        for k, v in kvs.items():
            m = NODE_RE.match(k)
            if m:
                try:
                    per_node[int(m.group(1))] = int(v)
                except ValueError:
                    pass
        if not per_node:
            continue
        kps = 4
        kv = kvs.get("kernelpagesize_kB")
        if kv:
            try:
                kps = int(kv)
            except ValueError:
                pass
        try:
            anon_pages = int(kvs.get("anon", "0"))
        except ValueError:
            anon_pages = 0
        total_pages = sum(per_node.values())
        if total_pages <= 0:
            continue
        total_kb = total_pages * kps
        anon_kb = anon_pages * kps
        file_kb = total_kb - anon_kb
        vma_nodes = {}
        for node, pages in per_node.items():
            kb = pages * kps
            d = nodes_agg.setdefault(node, {"pages": 0, "total_kb": 0,
                                            "anon_kb": 0, "file_kb": 0})
            d["pages"] += pages
            d["total_kb"] += kb
            # anon/file 按 VMA 内页数比例分摊（单 VMA 无法区分页类型归属）
            d["anon_kb"] += int(round(anon_kb * pages / total_pages))
            d["file_kb"] += int(round(file_kb * pages / total_pages))
            vma_nodes[node] = kb
        if label == "":
            label = "file(no-path)" if "file=" in line else "anon(no-label)"
        vmas.append({"label": label, "total_kb": total_kb, "anon_kb": anon_kb,
                     "nodes": vma_nodes})
    vmas.sort(key=lambda v: -v["total_kb"])
    # 无条件截断：top_n=0 → 空表（"0（不打）"的文档口径）。大模型进程
    # 有几十万 VMA，不截断会把单 rank 数十 MB 的全量 VMA 塞进 JSON/markdown
    vmas = vmas[:top_n]
    return nodes_agg, vmas


# ------------------------------------------------------------------ status

def parse_status(text):
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def status_int_kb(d, key):
    v = d.get(key, "")
    m = re.match(r"(\d+)\s*kB", v)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ 系统侧

def collect_system(sys_root, proc_root):
    sysinfo = {"nodes": [], "node_cpulist": {}, "numa_balancing": None,
               "thp": None, "npu": [], "npu_nodes": []}
    for d in sorted(globmod.glob(os.path.join(sys_root,
                                              "devices/system/node/node[0-9]*"))):
        try:
            node = int(os.path.basename(d)[4:])
        except ValueError:
            continue
        entry = {"node": node, "mem_total_kb": None, "mem_free_kb": None,
                 "numastat": {}}
        mi = read_text(os.path.join(d, "meminfo")) or ""
        for line in mi.splitlines():
            # 真实格式: "Node 0 MemTotal:  16777216 kB"（数字在倒数第二列）
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                if "MemTotal:" in line:
                    entry["mem_total_kb"] = int(parts[-2])
                elif "MemFree:" in line:
                    entry["mem_free_kb"] = int(parts[-2])
            except ValueError:
                pass
        ns = read_text(os.path.join(d, "numastat")) or ""
        for line in ns.splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    entry["numastat"][parts[0]] = int(parts[1])
                except ValueError:
                    pass
        sysinfo["nodes"].append(entry)
        cl = read_text(os.path.join(d, "cpulist"))
        if cl is not None:
            sysinfo["node_cpulist"][node] = cl.strip()
    nb = read_text(os.path.join(proc_root, "sys/kernel/numa_balancing"))
    if nb is not None:
        try:
            sysinfo["numa_balancing"] = int(nb.strip())
        except ValueError:
            pass
    thp = read_text(os.path.join(
        sys_root, "kernel/mm/transparent_hugepage/enabled"))
    if thp:
        m = re.search(r"\[(\w+)\]", thp)
        if m:
            sysinfo["thp"] = m.group(1)
    # NPU 识别：vendor 0x19e5 只是初筛——华为全家桶（hns3/Hi1822 网卡、
    # iBMA、加密卡、PCI bridge）同为 0x19e5，单信 vendor 会把它们全算成
    # NPU 令 C2 双向失真。二次校验：PCI class 0x120000（Processing
    # accelerators）或 Ascend device id（d801/d802）
    for vf in globmod.glob(os.path.join(sys_root, "bus/pci/devices/*/vendor")):
        v = (read_text(vf) or "").strip()
        if v != "0x19e5":
            continue
        pdir = os.path.dirname(vf)
        bdf = os.path.basename(pdir)
        cls = (read_text(os.path.join(pdir, "class")) or "").strip().lower()
        dev = (read_text(os.path.join(pdir, "device")) or "").strip().lower()
        is_accel = cls.startswith("0x1200") or dev in ("0xd801", "0xd802")
        if not is_accel:
            continue    # 华为非 NPU 设备（NIC/BMC/加密卡/桥）排除
        nn = read_text(os.path.join(pdir, "numa_node"))
        node = None
        if nn is not None:
            try:
                node = int(nn.strip())
                if node < 0:
                    node = None       # -1 = sysfs 未暴露归属，不可判定
            except ValueError:
                pass
        sysinfo["npu"].append({"bdf": bdf, "node": node})
        if node is not None:
            sysinfo["npu_nodes"].append(node)
    sysinfo["npu_nodes"] = sorted(set(sysinfo["npu_nodes"]))
    return sysinfo


def build_cpu_node_map(sysinfo):
    """node_cpulist → cpu→node 映射"""
    cmap = {}
    for node, cl in sysinfo.get("node_cpulist", {}).items():
        cpus = parse_cpulist(cl)
        if cpus:
            for c in cpus:
                cmap[c] = int(node)
    return cmap


def cpus_to_nodes(cpulist_s, cmap):
    cpus = parse_cpulist(cpulist_s)
    if cpus is None:
        return None
    return {cmap[c] for c in cpus if c in cmap}


# ------------------------------------------------------------------ 采集每 rank

def collect_rank(proc_root, r, top_vmas):
    pid = r["pid"]
    base = os.path.join(proc_root, str(pid))
    info = dict(r)
    status = parse_status(read_text(os.path.join(base, "status")) or "")
    info["comm"] = status.get("Name", "?")
    info["mems_allowed"] = status.get("Mems_allowed_list", "?")
    info["cpus_allowed"] = status.get("Cpus_allowed_list", "?")
    info["rss_kb"] = status_int_kb(status, "VmRSS")

    nm = read_text(os.path.join(base, "numa_maps"))
    if nm is None:
        info["numa_maps_error"] = True
        info["nodes"] = {}
        info["resident_kb"] = 0
        info["coverage"] = None
        info["top_vmas"] = []
        info["hints"] = ["NUMA_MAPS_UNREADABLE"]
        return info
    nodes_agg, vmas = parse_numa_maps(nm, top_vmas)
    info["nodes"] = {str(n): v for n, v in sorted(nodes_agg.items())}
    info["resident_kb"] = sum(v["total_kb"] for v in nodes_agg.values())
    anon_total = sum(v["anon_kb"] for v in nodes_agg.values())
    file_total = sum(v["file_kb"] for v in nodes_agg.values())
    info["anon_kb"] = anon_total
    info["file_kb"] = file_total
    if info["rss_kb"]:
        info["coverage"] = round(info["resident_kb"] / float(info["rss_kb"]), 4)
    else:
        info["coverage"] = None
    info["top_vmas"] = vmas

    # anon 主 node（判据主口径；占比 <60% 时视为无明确主 node）
    info["anon_primary"] = None
    if anon_total > 0:
        pn = max(nodes_agg.items(), key=lambda kv: kv[1]["anon_kb"])
        pct = pn[1]["anon_kb"] / float(anon_total)
        if pct >= 0.60:
            info["anon_primary"] = {"node": pn[0], "pct": round(pct, 4)}
    return info


# ------------------------------------------------------------------ hints

def compute_hints(ranks, sysinfo, cmap):
    npu_nodes = set(sysinfo.get("npu_nodes") or [])
    for r in ranks:
        # 保留采集阶段写入的观测缺口标记（如 NUMA_MAPS_UNREADABLE），
        # 判据 hints 只在其后追加——观测失败必须可见，不可被覆盖
        hints = list(r.get("hints") or [])
        ap = r.get("anon_primary")
        # C6 覆盖率门
        cov = r.get("coverage")
        if cov is not None:
            if cov < 0.90:
                hints.append("C6_LOW_COVERAGE")
            elif cov > 1.05:
                hints.append("C6_OVERCOUNT")
        # C3 强制分离：Mems_allowed_list 的数字就是 NUMA node 号（不经
        # cpu→node 映射——把它当 CPU 号解析会在多 node 机器上双向失真）；
        # Cpus_allowed_list 才做 cpu→node 映射，比较两集合交集
        mems_nodes = parse_cpulist(r.get("mems_allowed", "?")) \
            if r.get("mems_allowed", "?") not in ("", "?") else None
        cpu_nodes = cpus_to_nodes(r.get("cpus_allowed", "?"), cmap) \
            if cmap else None
        if mems_nodes is not None and cpu_nodes is not None \
                and mems_nodes and cpu_nodes and not (mems_nodes & cpu_nodes):
            hints.append("C3_FORCE_REMOTE")
        # C1 远程访存嫌疑：anon 主 node ∉ CPU 侧 node 集合
        if ap is not None and cpu_nodes is not None and cpu_nodes \
                and ap["node"] not in cpu_nodes:
            hints.append("C1_REMOTE_SUSPECT")
        # C2 NPU 错位（numa_node=-1 已在采集侧归 None，不会进 npu_nodes）
        if ap is not None and npu_nodes and ap["node"] not in npu_nodes:
            hints.append("C2_NPU_MISALIGN")
        r["hints"] = hints
        # 解析结果保留进 JSON 供复核（不再用下划线前缀过滤掉）
        r["mems_nodes"] = sorted(mems_nodes) if mems_nodes else None
        r["cpu_nodes"] = sorted(cpu_nodes) if cpu_nodes else None

    # C4 跨 rank 热点（机器粗筛：≥2 rank 同 anon 主 node，且存在
    # 空闲更充足的另一 node）
    primary_groups = {}
    for r in ranks:
        ap = r.get("anon_primary")
        if ap is not None:
            primary_groups.setdefault(ap["node"], []).append(r["rank"])
    nodes_info = {n["node"]: n for n in sysinfo.get("nodes", [])}
    for node, rranks in primary_groups.items():
        if len(rranks) < 2:
            continue
        alt_free = [n["mem_free_kb"] for n in sysinfo.get("nodes", [])
                    if n["node"] != node and n.get("mem_free_kb") is not None]
        cur = nodes_info.get(node, {}).get("mem_free_kb")
        if alt_free and (cur is None or max(alt_free) > cur):
            for r in ranks:
                ap = r.get("anon_primary")
                if ap is not None and ap["node"] == node:
                    r["hints"].append("C4_NODE_HOTSPOT")
    return ranks


# ------------------------------------------------------------------ affinity 联动

def load_affinity_dir(adir):
    """读取 thread-affinity collector 输出目录 →
    (npu_nodes, cpu_node_map, target_pid, thread_primary_node, thread_nodes)

    数据契约（references/affinity-trace-contract.md）：
    - topology_raw.txt：`=== node_cpulist`（node|cpulist）与 `=== npu_pci`
      （bdf|node）分节——npu_pci 为 producer 的 vendor-only 全列
      （未双校验，可能含 Huawei 网卡；联合表判定优先用本地双校验集合）
    - summary.json：target.pid 与 profiles[]（busy_pct / active_psr_top）
    文件缺失/分节全缺/字段缺失输出 WARN（观测缺口可见，不静默降级）。"""
    topo_path = os.path.join(adir, "topology_raw.txt")
    npu_nodes, cpu_node_map = set(), {}
    if os.path.isfile(topo_path):
        text = read_text(topo_path)
        if text is None:      # isfile 与读取之间存在竞态（文件被取走）
            text = ""
        section = None
        for line in text.splitlines():
            if line.startswith("=== "):
                section = line[4:].strip()
                continue
            if section == "node_cpulist" and "|" in line:
                n, cl = line.split("|", 1)
                cpus = parse_cpulist(cl.strip())
                if cpus:
                    try:
                        ni = int(n)
                    except ValueError:
                        continue
                    for c in cpus:
                        cpu_node_map[c] = ni
            elif section == "npu_pci" and "|" in line:
                bdf, nn = line.split("|", 1)
                try:
                    nn = int(nn)
                    if nn >= 0:
                        npu_nodes.add(nn)
                except ValueError:
                    pass
        if not cpu_node_map and not npu_nodes:
            # 契约承诺"缺分节 → WARN 并降级"——文件存在但两分节均无
            # 有效行是同款缺口，不得静默
            print("WARN: affinity topology_raw.txt 存在但 node_cpulist/"
                  "npu_pci 分节均无有效行 —— 联合判读降级（观测缺口可见）",
                  file=sys.stderr)
    else:
        print("WARN: affinity-dir 缺少 topology_raw.txt —— 联合判读降级"
              "（无 cpu→node 映射与 NPU 归属，契约见 references/"
              "affinity-trace-contract.md）", file=sys.stderr)
    target_pid, thread_nodes = None, {}
    spath = os.path.join(adir, "summary.json")
    if os.path.isfile(spath):
        try:
            with open(spath, "r", encoding="utf-8") as fh:
                sm = json.load(fh)
            target_pid = sm.get("target", {}).get("pid")
            for p in sm.get("profiles", []):
                if p.get("busy_pct", 0) < 50:
                    continue
                for cpu, cnt in p.get("active_psr_top", [])[:4]:
                    if cpu in cpu_node_map:
                        nd = cpu_node_map[cpu]
                        thread_nodes[nd] = thread_nodes.get(nd, 0) + cnt
        except (ValueError, OSError) as e:
            print("WARN: affinity-dir summary.json 解析失败（%s）—— 线程落点"
                  "联合判读降级" % e, file=sys.stderr)
            target_pid = None
    else:
        print("WARN: affinity-dir 缺少 summary.json —— 线程落点联合判读降级",
              file=sys.stderr)
    thread_primary = None
    if thread_nodes:
        thread_primary = max(thread_nodes.items(), key=lambda kv: kv[1])[0]
    return sorted(npu_nodes), cpu_node_map, target_pid, thread_primary, \
        thread_nodes


# ------------------------------------------------------------------ 渲染

def render_markdown(meta, sysinfo, ranks, diff=None, affinity=None):
    L = []
    L.append("# 训练 rank 内存 NUMA 快照")
    L.append("")
    L.append("- host=%s kernel=%s ts=%s numa_balancing=%s thp=%s" % (
        meta["host"], meta["kernel"], time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(meta["ts"])),
        sysinfo.get("numa_balancing", "?"), sysinfo.get("thp", "?")))
    ws = max((r.get("world_size") or 0 for r in ranks), default=0)
    lws = [r.get("local_world_size") for r in ranks
           if r.get("local_world_size")]
    expected = None
    wsnote = ""
    if lws and len(set(lws)) == 1:
        # torchrun/openmpi/slurm 提供本机期望数——多机训练 WORLD_SIZE 是
        # 跨主机总数，本机校验必须用本机口径
        expected = lws[0]
        if expected != len(ranks):
            wsnote = "  ⚠ 本机发现数 %d != LOCAL_WORLD_SIZE %d" % (len(ranks),
                                                                    expected)
            print("WARN: ranks found (%d) != LOCAL_WORLD_SIZE (%d) —— 结论"
                  "必须注明缺口" % (len(ranks), expected), file=sys.stderr)
        else:
            wsnote = "  ✓(本机口径)"
    elif ws and ws != len(ranks):
        # 无本机期望数（pmi 族/老启动器）：WORLD_SIZE 为跨主机总数，
        # 多机场景本机发现数少属正常——不能按"缺口"误导
        wsnote = "  ⚠ 发现数 %d != WORLD_SIZE %d（WORLD_SIZE 为跨主机总数，多机属正常）" % (len(ranks), ws)
        print("WARN: ranks found (%d) != WORLD_SIZE (%d) —— WORLD_SIZE 是"
              "跨主机总数（无 LOCAL_WORLD_SIZE）：多机训练本机只发现部分"
              "rank 属正常，单机训练则提示缺口" % (len(ranks), ws),
              file=sys.stderr)
    else:
        wsnote = " ✓"
    L.append("- rank 发现: %d 个%s" % (len(ranks), wsnote))

    node_ids = sorted({int(n) for r in ranks for n in r.get("nodes", {})})
    node_ids += [n["node"] for n in sysinfo.get("nodes", [])
                 if n["node"] not in node_ids]

    L.append("")
    L.append("## rank 内存分布（anon=私有判据口径 / file=共享参考口径）")
    L.append("")
    header = ["rank", "pid", "comm", "rss MB"]
    for n in node_ids:
        header.append("n%d anon MB" % n)
        header.append("n%d file MB" % n)
    header += ["anon主node", "mems_allowed", "hints"]
    L.append("| " + " | ".join(header) + " |")
    L.append("|" + "---|" * len(header))
    for r in sorted(ranks, key=lambda x: (x.get("rank") is None,
                                          x.get("rank") or 0)):
        row = [str(r.get("rank", "?")), str(r["pid"]), r.get("comm", "?"),
               fmt_mb(r.get("rss_kb") or 0)]
        for n in node_ids:
            nd = r.get("nodes", {}).get(str(n))
            row.append(fmt_mb(nd["anon_kb"]) if nd else "-")
            row.append(fmt_mb(nd["file_kb"]) if nd else "-")
        ap = r.get("anon_primary")
        row.append("n%s(%s)" % (ap["node"], fmt_pct(ap["pct"])) if ap
                   else "无(占比<60%)")
        row.append(r.get("mems_allowed", "?"))
        row.append(",".join(r.get("hints", [])) or "-")
        L.append("| " + " | ".join(str(x) for x in row) + " |")

    L.append("")
    covs = ["rank%s: %s" % (r.get("rank", "?"),
                            ("%.1f%%" % (r["coverage"] * 100))
                            if r.get("coverage") is not None else "?")
            for r in ranks]
    L.append("覆盖率（聚合驻留/VmRSS）: " + " / ".join(covs) +
             "  [0.9~1.05 为可信区间]")

    for r in sorted(ranks, key=lambda x: (x.get("rank") is None,
                                          x.get("rank") or 0)):
        if not r.get("top_vmas"):
            continue
        L.append("")
        L.append("### rank %s top VMA" % r.get("rank", "?"))
        L.append("")
        L.append("| MB | anon MB | label | node 分布 |")
        L.append("|---|---|---|---|")
        for v in r["top_vmas"]:
            nd = " ".join("n%s:%s" % (k, fmt_mb(x))
                          for k, x in sorted(v["nodes"].items()))
            L.append("| %s | %s | %s | %s |" % (
                fmt_mb(v["total_kb"]), fmt_mb(v["anon_kb"]),
                v["label"][:60], nd))

    if sysinfo.get("nodes"):
        L.append("")
        L.append("## 系统侧 node 水位")
        L.append("")
        L.append("| node | total MB | free MB | local_node | other_node |")
        L.append("|---|---|---|---|---|")
        for n in sysinfo["nodes"]:
            st = n.get("numastat", {})
            L.append("| %s | %s | %s | %s | %s |" % (
                n["node"],
                fmt_mb(n["mem_total_kb"]) if n["mem_total_kb"] else "-",
                fmt_mb(n["mem_free_kb"]) if n["mem_free_kb"] else "-",
                st.get("local_node", "-"), st.get("other_node", "-")))

    if affinity:
        anp, apid, tpn, matched, has_local, overridden = affinity
        L.append("")
        L.append("## 亲和联合表（thread-affinity: pid %s%s）" % (
            apid, "" if matched else " ⚠ 未匹配到 rank 进程"))
        L.append("")
        npu_col_note = ""
        if has_local:
            npu_col_note = "（本地双校验，已覆盖 affinity 快照集合）" \
                if overridden else "（本地双校验）"
        elif anp:
            npu_col_note = "（affinity 快照，vendor-only 未双校验）"
        L.append("| rank | anon主node | 线程主落点node | NPU nodes%s | 联合判定 |"
                 % npu_col_note)
        L.append("|---|---|---|---|---|")
        for r in sorted(ranks, key=lambda x: (x.get("rank") is None,
                                              x.get("rank") or 0)):
            ap = r.get("anon_primary")
            tj = r.get("thread_join")
            tp = (tj or {}).get("thread_primary_node")
            judge = []
            if ap is not None and tp is not None and ap["node"] != tp:
                judge.append("C1✗ 内存n%s/线程n%s" % (ap["node"], tp))
            if ap is not None and anp and ap["node"] not in anp:
                judge.append("C2✗ 设备n%s" % ",".join(str(x) for x in anp))
            # 亲和数据未匹配到该 rank：显示"无数据"，不得显示"干净"——
            # "干净"会被误读为"已核对且正常"
            if tj is None:
                tp_display = "未匹配" if apid is not None else "-"
                judge_str = "; ".join(judge) if judge else "无数据（未匹配线程）"
            else:
                tp_display = "n%s" % tp if tp is not None else "-"
                judge_str = "; ".join(judge) or "干净"
            L.append("| %s | %s | %s | %s | %s |" % (
                r.get("rank", "?"),
                ("n%s(%s)" % (ap["node"], fmt_pct(ap["pct"]))) if ap else "-",
                tp_display,
                (",".join("n%s" % x for x in anp)) if anp else "unknown/-1",
                judge_str))
        L.append("")
        L.append("> 判定只是粗筛——按 references/methodology.md 核对证据与"
                 "反例后才可写入结论")

    if diff:
        L.append("")
        L.append("## 增量对比")
        L.append("")
        L.append("| rank | Δtotal MB | Δanon MB | per-node Δanon MB | 备注 |")
        L.append("|---|---|---|---|---|")
        for row in diff:
            L.append("| " + " | ".join(str(x) for x in row) + " |")

    return "\n".join(L)


def compute_diff(old, new):
    """返回 markdown 行列表 + diff 结构（按 rank 对齐）。

    逐 node 增量遍历**新旧 node 键的并集**（两侧缺失按零计）——只看新
    快照会把"100 MiB 从 node0 全部迁到 node1"漏掉 node0 的 -100 MiB，
    纯迁移被误报成总内存新增。消失的 rank 与新增 rank 对称进 JSON
    struct（机器可读层不缺条目——OOM/掉队现场的关键信号）。"""
    old_ranks = {r["rank"]: r for r in old.get("ranks", [])
                 if r.get("rank") is not None}
    rows, struct = [], []
    for r in sorted(new.get("ranks", []), key=lambda x: (x.get("rank") is None,
                                                         x.get("rank") or 0)):
        rk = r.get("rank")
        if rk is None:
            continue
        o = old_ranks.get(rk)
        if o is None:
            rows.append([rk, "新增", "-", "-", "旧快照无此 rank"])
            struct.append({"rank": rk, "status": "new"})
            continue
        dn = {}
        new_nodes = r.get("nodes", {})
        old_nodes = o.get("nodes", {})
        for n in set(new_nodes) | set(old_nodes):
            v = new_nodes.get(n) or {"total_kb": 0, "anon_kb": 0}
            ov = old_nodes.get(n) or {"total_kb": 0, "anon_kb": 0}
            dt = v.get("total_kb", 0) - ov.get("total_kb", 0)
            da = v.get("anon_kb", 0) - ov.get("anon_kb", 0)
            if dt or da:
                dn[n] = {"total_kb": dt, "anon_kb": da}
        dt_all = sum(x["total_kb"] for x in dn.values())
        da_all = sum(x["anon_kb"] for x in dn.values())
        note = []
        if o.get("pid") != r.get("pid"):
            note.append("PID 变化(重启)")
        per_node = " ".join("n%s:%+0.1f" % (n, x["anon_kb"] / 1024.0)
                            for n, x in sorted(dn.items()))
        rows.append([rk, "%+0.1f" % (dt_all / 1024.0),
                     "%+0.1f" % (da_all / 1024.0), per_node or "-",
                     ";".join(note) or "-"])
        struct.append({"rank": rk, "delta_total_kb": dt_all,
                       "delta_anon_kb": da_all, "per_node": dn,
                       "restarted": o.get("pid") != r.get("pid")})
    gone = set(old_ranks) - {r.get("rank") for r in new.get("ranks", [])}
    for rk in sorted(gone):
        rows.append([rk, "消失", "-", "-", "新快照无此 rank"])
        struct.append({"rank": rk, "status": "gone"})
    return rows, struct


# ------------------------------------------------------------------ main

def main(argv=None):
    # Windows/GBK 终端兼容：stdout/stderr 强制 UTF-8（Linux 本就 UTF-8，无副作用）
    for _s in (sys.stdout, sys.stderr):
        enc = (getattr(_s, "encoding", "") or "").lower().replace("-", "")
        if enc not in ("utf-8", "utf8", "ascii", "") and \
                hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    # argparse 默认 error() 退出码为 2，与本 skill 契约的"2=/proc 不可读"
    # 撞码——覆盖之，令参数错误退出 1（与 SKILL.md/docstring 声明一致）
    class _Parser(argparse.ArgumentParser):
        def error(self, message):
            self.print_help(sys.stderr)
            print("ERROR: 参数错误: %s" % message, file=sys.stderr)
            sys.exit(1)

    ap = _Parser(
        description="kptools-train-mem-affinity 采集器：训练 rank 内存 NUMA 归属快照")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--pid", default=None,
                    help="指定 rank 进程 PID（逗号分隔；跳过 environ 全扫）")
    ap.add_argument("--top-vmas", type=int, default=0,
                    help="每 rank 打印前 N 大 VMA 归因")
    ap.add_argument("--diff", default=None, help="旧快照 train_mem_affinity.json")
    ap.add_argument("--affinity-dir", default=None,
                    help="kptools-thread-affinity-collector 输出目录（联合判读）")
    ap.add_argument("--rank-env", default=None,
                    help="追加自定义 rank 环境变量名")
    ap.add_argument("--proc-root", default="/proc")
    ap.add_argument("--sys-root", default="/sys")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.proc_root):
        print("ERROR: /proc 不可读 %s" % args.proc_root, file=sys.stderr)
        return 2

    # --- rank 发现
    if args.pid:
        pids = [p.strip() for p in args.pid.split(",") if p.strip()]
        raw = []
        for p in pids:
            if not p.isdigit():
                print("ERROR: 参数错误: --pid 需为数字 PID，got %r" % p,
                      file=sys.stderr)
                return 1
            if not os.path.isdir(os.path.join(args.proc_root, p)):
                print("ERROR: TARGET_MISSING pid=%s" % p, file=sys.stderr)
                return 3
            env = read_environ(args.proc_root, p)
            r = rank_from_env(env, args.rank_env)
            raw.append({"pid": int(p),
                        "rank": r[0] if r else None,
                        "local_rank": r[1] if r else None,
                        "world_size": r[2] if r else None,
                        "rank_env": (r[3] if r else None) or "manual",
                        "local_world_size": r[4] if r else None})
        # 与自动发现同一不变量：同 rank 多 PID 归并（min pid 主条目 +
        # duplicate_pids）——手工指定的两个同 RANK PID（主进程 + 继承
        # RANK 的 DataLoader worker）不得算成两个 rank 触发 C4 假热点
        ranks = _dedup_by_rank(raw)
        unreadable = []
    else:
        groups, unreadable = discover_ranks(args.proc_root, args.rank_env)
        if not groups:
            print("ERROR: NO_RANK_FOUND（environ 无 RANK 类变量；"
                  "确认 torchrun/mpirun 启动或用 --pid 指定；跨 uid 需 root）",
                  file=sys.stderr)
            return 3
        if len(groups) > 1:
            # 本机并行多个训练作业：各自都有 rank 0，全局按 rank 去重会
            # 把另一作业进程记入 duplicate_pids 或拼成一份错误报告
            print("ERROR: MULTI_JOB——发现 %d 个训练作业，按作业分组如下"
                  "（请用 --pid 指定目标作业的 rank 进程）：" % len(groups),
                  file=sys.stderr)
            for g in groups:
                print("  作业 %s: ranks=%s pids=%s" % (
                    g["key"], [r["rank"] for r in g["ranks"]],
                    [r["pid"] for r in g["ranks"]]), file=sys.stderr)
            return 1
        ranks = groups[0]["ranks"]
    if not ranks:
        print("ERROR: NO_RANK_FOUND（environ 无 RANK 类变量；"
              "确认 torchrun/mpirun 启动或用 --pid 指定；跨 uid 需 root）",
              file=sys.stderr)
        return 3
    for pid in unreadable:
        print("WARN: environ unreadable: pid %d（跨 uid 需 root）" % pid,
              file=sys.stderr)

    # --- 采集
    sysinfo = collect_system(args.sys_root, args.proc_root)
    cmap = build_cpu_node_map(sysinfo)

    affinity = None
    if args.affinity_dir:
        anp, amap, apid, tpn, tnodes = load_affinity_dir(args.affinity_dir)
        if not cmap and amap:
            cmap = amap
        # 联合表的 NPU 列与 C2 判定**优先用本地双校验集合**（本 skill 的
        # sysfs 扫描带 vendor+class/device 双校验；affinity producer 的
        # npu_pci 是 vendor-only 全列，含 Huawei 网卡会污染集合）
        local_npu = sorted(sysinfo.get("npu_nodes") or [])
        anp_overridden = False
        if local_npu:
            if anp and set(anp) != set(local_npu):
                print("WARN: affinity 快照 npu_pci 集合 %s 与本地双校验集合"
                      " %s 不一致——联合表按本地双校验集合判定（producer 旧版"
                      " 为 vendor-only 全列，现行已双校验；本地集合优先对两种"
                      " 版本都安全）" % (sorted(anp), local_npu),
                      file=sys.stderr)
                anp_overridden = True
            anp = local_npu
        elif anp:
            print("WARN: 本地无 NPU 集合，联合表沿用 affinity 快照"
                  "npu_pci（vendor-only、未双校验——含网卡风险）",
                  file=sys.stderr)
        affinity = (anp, apid, tpn, False, bool(local_npu), anp_overridden)
        print("INFO: thread-affinity 联动: pid=%s npu_nodes=%s thread_primary=%s"
              % (apid, anp, tpn), file=sys.stderr)

    collected = []
    for r in ranks:
        info = collect_rank(args.proc_root, r, args.top_vmas)
        if args.affinity_dir and info["pid"] == affinity[1]:
            info["thread_join"] = {
                "pid_matched": True,
                "thread_primary_node": affinity[2],
                "thread_nodes": {str(k): v for k, v in tnodes.items()},
            }
            affinity = (affinity[0], affinity[1], affinity[2], True,
                        affinity[4], affinity[5])
        collected.append(info)

    all_err = all(r.get("numa_maps_error") for r in collected)
    if all_err:
        print("ERROR: NUMA_MAPS_UNREADABLE（全部 rank 的 numa_maps 不可读；"
              "跨 uid 需 root）", file=sys.stderr)
        return 4
    # 部分不可读：观测缺口必须可见（该 rank 的 hints 带
    # NUMA_MAPS_UNREADABLE，不被判据 hints 覆盖）
    for r in collected:
        if r.get("numa_maps_error"):
            print("WARN: rank %s (pid %s) numa_maps 不可读——该 rank 观测失败，"
                  "分布为 0 是缺数不是健康，结论必须注明缺口"
                  % (r.get("rank", "?"), r["pid"]), file=sys.stderr)

    compute_hints(collected, sysinfo, cmap)

    # --- diff
    diff_rows = None
    diff_struct = None
    if args.diff:
        try:
            with open(args.diff, "r", encoding="utf-8") as fh:
                old = json.load(fh)
            diff_rows, diff_struct = compute_diff(old, {"ranks": collected})
        except (ValueError, OSError) as e:
            print("WARN: --diff 读取失败（%s），跳过对比" % e, file=sys.stderr)

    meta = {
        "skill": "kptools-train-mem-affinity", "version": VERSION,
        "ts": time.time(), "host": socket.gethostname(),
        # osrelease 恒带尾随换行——不 strip 会把 markdown 首部 bullet 从中截断
        "kernel": ((read_text(os.path.join(args.proc_root, "sys/kernel/osrelease"))
                    or (os.uname().release if hasattr(os, "uname") else ""))
                   or "").strip(),
        "proc_root": args.proc_root, "sys_root": args.sys_root,
        "top_vmas": args.top_vmas,
    }
    if sysinfo.get("numa_balancing") == 1:
        print("WARN: numa_balancing=1 —— 静态快照分布可能被自动迁移污染"
              "（methodology C5），membind 验证建议先关再测", file=sys.stderr)

    out = {
        "meta": meta,
        "system": sysinfo,
        "ranks": [{k: v for k, v in r.items() if not k.startswith("_")}
                  for r in collected],
    }
    if diff_struct is not None:
        out["diff"] = diff_struct
    if args.affinity_dir:
        out["affinity_join"] = {"source": args.affinity_dir,
                                "pid": affinity[1],
                                "matched": affinity[3]}

    # 目录名用真 UTC（与 meta.ts 的 epoch 秒一致口径，跨时区协作不误导）
    out_dir = args.out or ("train_mem_affinity_%s"
                           % time.strftime("%Y%m%d_%H%M%S", time.gmtime()))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "train_mem_affinity.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(out, ensure_ascii=False, indent=2))

    md = render_markdown(meta, sysinfo, out["ranks"],
                         diff=diff_rows, affinity=affinity)
    print(md)
    print()
    print("snapshot: %s" % os.path.abspath(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
