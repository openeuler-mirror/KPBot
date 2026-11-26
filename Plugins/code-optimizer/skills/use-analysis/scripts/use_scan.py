#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USE 分析法 Linux 性能扫描器

对当前主机的四大核心资源（CPU / 内存 / 磁盘IO / 网络），按 Brendan Gregg 的
USE 方法论逐项检查 使用率(U) / 饱和度(S) / 错误率(E)，运行标准诊断命令采集指标、
按阈值标记瓶颈、输出结构化 JSON 契约（含可读摘要 summary_text）。

模式：
  --scan                 采集当前主机指标并分析（默认）
  --check-env            仅检查诊断工具可用性，不做分析
  --parse <file>         解析用户粘贴的命令输出文件（paste 模式，格式见 SKILL.md）

输出：JSON 契约（含可读摘要 summary_text）。--text 仅打印可读摘要。

本脚本只读不写：仅运行监控类命令，不修改任何系统配置或文件。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

# --------------------------------------------------------------------------- #
# 阈值配置
# --------------------------------------------------------------------------- #
# 这些阈值是经验值，并非绝对真理。其中部分指标（上下文切换、TIME_WAIT、负载）与
# 负载特征强相关，已在 detail 里注明 caveat，解读时结合业务基线判断。
THRESHOLDS = {
    "cpu": {
        "util_warn": 80.0, "util_crit": 90.0,          # us+sy 平均占比 %
        "r_warn_ratio": 0.7, "r_crit_ratio": 1.0,       # 运行队列 r / nproc
        "load_warn_ratio": 0.7, "load_crit_ratio": 1.0, # 1min 负载 / nproc
        "cs_per_core_warn": 5000.0, "cs_per_core_crit": 15000.0,  # 每核每秒上下文切换
    },
    "memory": {
        "avail_pct_warn": 10.0, "avail_pct_crit": 5.0,  # available / total %
        "swap_kb_warn": 1, "swap_kb_crit": 1024,         # si 或 so KB/s
    },
    "disk": {
        "util_warn": 80.0, "util_crit": 95.0,            # %util
        "await_warn": 20.0, "await_crit": 50.0,          # ms
        "fs_full_warn": 90.0, "fs_full_crit": 95.0,      # 文件系统容量使用率 %
    },
    "network": {
        "ifutil_warn": 70.0, "ifutil_crit": 90.0,        # 网卡带宽占用 %
        "timewait_warn": 5000, "timewait_crit": 20000,   # TIME_WAIT 连接数
        "retrans_rate_warn": 0.5, "retrans_rate_crit": 2.0,  # 重传率 %
    },
}

# dmesg / journalctl 错误归类模式（大小写不敏感）。把内核日志按资源维度归类，
# 填进对应资源的 errors 维度。只取最近匹配，避免刷屏。
ERROR_PATTERNS = {
    "cpu": [r"hardware error", r"\bmce\b", r"machine check", r"thermal", r"throttl",
            r"cpu\d+.*error", r"sched.*error"],
    "memory": [r"out of memory", r"oom-kill", r"killed process", r"oom_reaper",
               r"page allocation failure", r"swap.*fail", r"hardware error.*memory"],
    "disk": [r"i/o error", r"read error", r"write error", r"ext4-fs error",
             r"xfs.*error", r"buffer i/o error", r"hung_task", r"reset device",
             r"link power management", r"ncq.*error", r"ata.*error", r"sd \d:\d:.*error"],
    "network": [r"link (is )?down", r"link (is )?up", r"carrier", r"nic.*error",
                r"eth\d+.*error", r"tcp.*error"],
    "system": [r"panic", r"\boops\b", r"bug:", r"call trace", r"segfault",
               r"general protection fault"],
}


def die(msg):
    """构造失败契约并退出。"""
    out = {"use_analysis_result": {"success": False, "error_message": str(msg)}}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(1)


def run_cmd(cmd, timeout=30):
    """运行命令，强制 LC_ALL=C 拿英文输出。返回 (rc, out, err)；
    工具缺失或超时返回 (None, None, None)，不抛异常（USE 工具是可选的）。"""
    env = dict(os.environ, LC_ALL="C", LANG="C", LANGUAGE="C")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return None, None, None
    except subprocess.TimeoutExpired:
        return None, None, None
    except Exception:
        return None, None, None


def which(name):
    return bool(shutil.which(name))


def read_file(path):
    """读文本文件，失败返回 None。"""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def read_proc(path):
    return read_file(path)


# --------------------------------------------------------------------------- #
# CPU 核数
# --------------------------------------------------------------------------- #
def get_nproc():
    """可用 CPU 核数。优先 sched_getaffinity（尊重 cgroup 亲和性，容器内更准），
    回退 os.cpu_count()，再回退 nproc 命令。"""
    try:
        return len(os.sched_getaffinity(0))
    except Exception:
        pass
    n = os.cpu_count()
    if n:
        return n
    rc, out, _ = run_cmd(["nproc"])
    if out:
        try:
            return int(out.strip())
        except ValueError:
            pass
    return 1


# --------------------------------------------------------------------------- #
# 解析器（按表头名解析，容忍列数差异，如 vmstat 的 gu 列）
# --------------------------------------------------------------------------- #
def parse_vmstat(text):
    """解析 vmstat 输出。返回 {r, b, cs, in, us, sy, id, wa, si, so}，
    取间隔行（跳过首行 since-boot）的平均值与峰值。"""
    res = {k: None for k in
           ["r", "b", "cs", "in", "us", "sy", "id", "wa", "st", "si", "so"]}
    if not text:
        return res, []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # 找表头行（含 "r" 和 "cs"）
    header_idx = None
    header = None
    for i, ln in enumerate(lines):
        toks = ln.split()
        if "r" in toks and "cs" in toks:
            header_idx = i
            header = toks
            break
    if header_idx is None:
        return res, []
    data_rows = []
    for ln in lines[header_idx + 1:]:
        toks = ln.split()
        if len(toks) < len(header) or not toks[0].lstrip("-").isdigit():
            continue
        row = dict(zip(header, toks))
        try:
            data_rows.append({k: float(v) for k, v in row.items()
                              if k in res and re.match(r"^-?\d+(\.\d+)?$", v)})
        except ValueError:
            continue
    if not data_rows:
        return res, []
    # 首行是 since-boot 平均，跳过；仅 1 行时用它
    interval = data_rows[1:] if len(data_rows) > 1 else data_rows
    for k in res:
        vals = [r[k] for r in interval if k in r and r[k] is not None]
        if vals:
            res[k] = round(sum(vals) / len(vals), 2)
    return res, interval


def parse_mpstat(text):
    """解析 mpstat -P ALL 输出的 Average 段。返回 {all_util, per_core_max_util, n_cores_sampled}。
    util = 100 - %idle。"""
    if not text:
        return None
    all_idle = None
    per_core_utils = []
    for ln in text.splitlines():
        if not ln.lstrip().startswith("Average:"):
            continue
        toks = ln.split()
        # Average: CPU %usr ... %idle  -> CPU 在 [1]，%idle 在 [-1]
        if len(toks) < 3:
            continue
        cpu = toks[1]
        try:
            idle = float(toks[-1])
        except ValueError:
            continue
        util = round(100.0 - idle, 2)
        if cpu.lower() == "all":
            all_idle = idle
            all_util = util
        elif cpu.isdigit():
            per_core_utils.append(util)
    all_util = round(100.0 - all_idle, 2) if all_idle is not None else None
    per_core_max = max(per_core_utils) if per_core_utils else None
    return {"all_util": all_util, "per_core_max_util": per_core_max,
            "n_cores_sampled": len(per_core_utils)}


def parse_free(text):
    """解析 free -m。返回 {total, used, free, shared, buff_cache, available, swap_total, swap_used}（MB）。"""
    out = {"total": None, "used": None, "free": None, "shared": None,
           "buff_cache": None, "available": None,
           "swap_total": None, "swap_used": None}
    if not text:
        return out
    for ln in text.splitlines():
        toks = ln.split()
        if not toks:
            continue
        if toks[0] == "Mem:":
            vals = toks[1:]
            keys = ["total", "used", "free", "shared", "buff_cache", "available"]
            for i, k in enumerate(keys):
                if i < len(vals):
                    try:
                        out[k] = int(vals[i])
                    except ValueError:
                        pass
        elif toks[0] == "Swap:":
            vals = toks[1:]
            try:
                out["swap_total"] = int(vals[0])
                out["swap_used"] = int(vals[1])
            except (IndexError, ValueError):
                pass
    return out


def parse_iostat(text):
    """解析 iostat -x 间隔行。返回 [{device, util, await, r_s, w_s}]（取最后一组间隔）。"""
    if not text:
        return []
    devices = {}
    header_seen = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("Device:"):
            header_seen = True
            continue
        if not header_seen or not s:
            continue
        toks = s.split()
        # Device rrqm/s wrqm/s r/s w/s rkB/s wkB/s avgrq-sz avgqu-sz await ... %util
        if len(toks) < 10 or not toks[0].replace("/", "").isalnum():
            continue
        dev = toks[0]
        try:
            # %util 是最后一列；await 是倒数往前找
            util = float(toks[-1])
            # await 通常在 r/s w/s 之后若干列，这里按已知 iostat -x 列序取第 10 列(索引9)
            await_ms = None
            # 标准 -x 列: Device rrqm/s wrqm/s r/s w/s rkB/s wkB/s avgrq-sz avgqu-sz await r_await w_await svctm %util
            if len(toks) >= 14:
                await_ms = float(toks[9])
            r_s = float(toks[3]) if len(toks) > 3 else None
            w_s = float(toks[4]) if len(toks) > 4 else None
            devices[dev] = {"device": dev, "util": util, "await": await_ms,
                            "r_s": r_s, "w_s": w_s}
        except (ValueError, IndexError):
            continue
    return list(devices.values())


def parse_diskstats_delta(sample_a, sample_b, interval_s):
    """从两次 /proc/diskstats 采样计算每设备 %util 与 await（iostat 缺失时的兜底）。
    %util = delta(io_ticks) / (interval_s*1000) * 100（多队列设备可能 >100）。
    await = delta(time_in_queue) / delta(reads+writes)（ms）。"""
    fields_a = _diskstats_to_dict(sample_a)
    fields_b = _diskstats_to_dict(sample_b)
    results = []
    for dev, b in fields_b.items():
        a = fields_a.get(dev)
        if not a:
            continue
        d_io_ticks = b["io_ticks"] - a["io_ticks"]
        d_reads = b["reads"] - a["reads"]
        d_writes = b["writes"] - a["writes"]
        d_queue = b["time_in_queue"] - a["time_in_queue"]
        util = round(d_io_ticks / (interval_s * 1000.0) * 100.0, 2) if interval_s > 0 else None
        io = d_reads + d_writes
        await_ms = round(d_queue / io, 2) if io > 0 else 0.0
        results.append({"device": dev, "util": util, "await": await_ms,
                        "r_s": round(d_reads / interval_s, 2) if interval_s > 0 else None,
                        "w_s": round(d_writes / interval_s, 2) if interval_s > 0 else None})
    return results


def _diskstats_to_dict(text):
    """解析 /proc/diskstats，仅保留整盘设备。返回 {dev: {reads, writes, io_ticks, time_in_queue}}。"""
    out = {}
    if not text:
        return out
    # 整盘名模式：sda, vda, nvme0n1, mmcblk0；排除分区 (sda1, nvme0n1p1, mmcblk0p1)
    whole_disk = re.compile(r"^(sd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
    for ln in text.splitlines():
        toks = ln.split()
        if len(toks) < 14:
            continue
        dev = toks[2]
        if not whole_disk.match(dev):
            continue
        try:
            out[dev] = {
                "reads": int(toks[3]),
                "writes": int(toks[7]),
                "io_ticks": int(toks[12]),     # 字段10: time spent doing I/Os (ms)
                "time_in_queue": int(toks[13]),  # 字段11: weighted time (ms)
            }
        except (ValueError, IndexError):
            continue
    return out


def parse_sar_dev(text):
    """解析 sar -n DEV。返回 [{iface, ifutil, rx_kbps, tx_kbps}]（跳过 lo，取最后间隔）。"""
    if not text:
        return []
    ifaces = {}
    for ln in text.splitlines():
        s = ln.strip()
        toks = s.split()
        if len(toks) < 9 or toks[1] == "IFACE":
            continue
        if toks[1] == "lo":
            continue
        try:
            iface = toks[1]
            rxkb = float(toks[4])
            txkb = float(toks[5])
            ifutil = float(toks[-1])
            ifaces[iface] = {"iface": iface, "ifutil": ifutil,
                             "rx_kbps": rxkb, "tx_kbps": txkb}
        except (ValueError, IndexError):
            continue
    return list(ifaces.values())


def parse_sar_edev(text):
    """解析 sar -n EDEV。返回 [{iface, rxerr, txerr, rxdrop, txdrop}]（跳过 lo，取最后间隔）。
    网卡在 TCP 之下的错包/丢包，是网络 E 维度的关键证据（/proc/net/snmp 只到 TCP 层）。"""
    if not text:
        return []
    ifaces = {}
    for ln in text.splitlines():
        toks = ln.strip().split()
        # IFACE rxerr/s txerr/s coll/s rxdrop/s txdrop/s txcarr/s rxfram/s rxfifo/s txfifo/s
        if len(toks) < 9 or toks[1] == "IFACE" or toks[1] == "lo":
            continue
        try:
            iface = toks[1]
            ifaces[iface] = {"iface": iface, "rxerr": float(toks[2]),
                             "txerr": float(toks[3]), "rxdrop": float(toks[5]),
                             "txdrop": float(toks[6])}
        except (ValueError, IndexError):
            continue
    return list(ifaces.values())


def parse_netstat_overflow(text):
    """从 netstat -s 提取 listen 队列溢出计数。返回 {listen_overflowed, syns_to_listen_dropped}。
    非 0 表示 accept/backlog 队列曾溢出--小流量但请求超时的典型成因。"""
    out = {"listen_overflowed": None, "syns_to_listen_dropped": None}
    if not text:
        return out
    m = re.search(r"(\d+)\s+times the listen queue of a socket overflowed", text)
    if m:
        out["listen_overflowed"] = int(m.group(1))
    m = re.search(r"(\d+)\s+SYNs to LISTEN sockets dropped", text)
    if m:
        out["syns_to_listen_dropped"] = int(m.group(1))
    return out


def get_numa_nodes():
    """统计 NUMA 节点数（读 /sys/devices/system/node/）。>1 即多节点机器。"""
    try:
        nodes = [d for d in os.listdir("/sys/devices/system/node")
                 if d.startswith("node")]
        return len(nodes)
    except Exception:
        return 1


def parse_ss_s(text):
    """解析 ss -s。返回 {total, estab, timewait, closed, orphaned}。"""
    out = {"total": None, "estab": None, "timewait": None,
           "closed": None, "orphaned": None}
    if not text:
        return out
    m = re.search(r"Total:\s*(\d+)", text)
    if m:
        out["total"] = int(m.group(1))
    m = re.search(r"TCP:\s*\d+\s*\(([^)]*)\)", text)
    if m:
        inner = m.group(1)
        for k, pat in [("estab", r"estab\s+(\d+)"), ("closed", r"closed\s+(\d+)"),
                       ("orphaned", r"orphaned\s+(\d+)"), ("timewait", r"timewait\s+(\d+)")]:
            mm = re.search(pat, inner)
            if mm:
                out[k] = int(mm.group(1))
    return out


def parse_snmp_tcp(text):
    """解析 /proc/net/snmp 的 Tcp 行。返回 {OutSegs, RetransSegs, InErrs, OutRsts}。"""
    out = {"OutSegs": None, "RetransSegs": None, "InErrs": None, "OutRsts": None}
    if not text:
        return out
    lines = [ln for ln in text.splitlines() if ln.startswith("Tcp:")]
    if len(lines) < 2:
        return out
    header = lines[0].split()
    vals = lines[1].split()
    header = header[1:]  # 去掉开头的 "Tcp:"
    vals = vals[1:]
    row = dict(zip(header, vals))
    for k in out:
        if k in row:
            try:
                out[k] = int(row[k])
            except ValueError:
                pass
    return out


def parse_uptime_loadavg():
    """获取 1/5/15min 负载。优先 os.getloadavg()。"""
    try:
        la = os.getloadavg()
        return {"load_1": round(la[0], 2), "load_5": round(la[1], 2),
                "load_15": round(la[2], 2)}
    except Exception:
        return {"load_1": None, "load_5": None, "load_15": None}


def categorize_dmesg(text):
    """把内核日志按资源维度归类。返回 {cpu:[], memory:[], disk:[], network:[], system:[]}。
    仅返回匹配行（最多每类 5 条，避免刷屏）。"""
    cats = {k: [] for k in ERROR_PATTERNS}
    if not text:
        return cats
    lines = text.splitlines()
    for ln in lines:
        low = ln.lower()
        for cat, patterns in ERROR_PATTERNS.items():
            if len(cats[cat]) >= 5:
                continue
            for pat in patterns:
                if re.search(pat, low):
                    cats[cat].append(ln.strip())
                    break
    return cats


# --------------------------------------------------------------------------- #
# 状态判定
# --------------------------------------------------------------------------- #
def status_for(value, warn, crit):
    """value 越大越坏。返回 ok / warn / crit / unknown。"""
    if value is None:
        return "unknown"
    if value >= crit:
        return "crit"
    if value >= warn:
        return "warn"
    return "ok"


def status_rank(s):
    return {"crit": 3, "warn": 2, "ok": 1, "unknown": 0}.get(s, 0)


# --------------------------------------------------------------------------- #
# 采集器
# --------------------------------------------------------------------------- #
def collect_errors():
    """采集内核日志错误（dmesg 优先，无权限回退 journalctl -k）。返回 (categorized_dict, source, note)。"""
    rc, out, err = run_cmd(["dmesg"], timeout=10)
    text = out if (rc == 0 and out) else None
    source = "dmesg"
    note = ""
    if text is None:
        # dmesg 受限（现代内核 kernel.dmesg_restrict=1），回退 journalctl
        rc2, out2, _ = run_cmd(["journalctl", "-k", "--no-pager", "-n", "500"], timeout=15)
        if rc2 == 0 and out2 and "insufficient permissions" not in out2.lower():
            text = out2
            source = "journalctl -k"
            note = "dmesg 无权限，已回退 journalctl -k"
        else:
            # 再回退 /var/log/messages（部分发行版/用户组可读）
            for path in ("/var/log/messages", "/var/log/syslog"):
                t = read_file(path)
                if t:
                    text = "\n".join(t.splitlines()[-500:])
                    source = path
                    note = "dmesg/journalctl 无权限，已回退 %s" % path
                    break
            if text is None:
                return categorize_dmesg(""), "none", ("非 root 无法读取内核日志（dmesg 受限、"
                    "journalctl 无权限），错误率维度未采集；建议用 root 运行或加入 systemd-journal/adm 组")
    # 只取最后 500 行做归类，避免巨量日志
    tail = "\n".join(text.splitlines()[-500:])
    return categorize_dmesg(tail), source, note


def collect_cpu(count, nproc):
    """采集 CPU 的 U/S/E。"""
    th = THRESHOLDS["cpu"]
    load = parse_uptime_loadavg()

    # 使用率：mpstat 拿 per-core 与 all；vmstat 拿 us+sy 兜底
    rc, out, _ = run_cmd(["mpstat", "-P", "ALL", "1", str(count)], timeout=count + 10)
    mp = parse_mpstat(out) if (rc == 0 and out) else None
    mpstat_ok = mp is not None and mp.get("all_util") is not None
    util = mp["all_util"] if mpstat_ok else None
    per_core_max = mp["per_core_max_util"] if mp else None

    # vmstat 顺带拿饱和度（r/cs）与 us+sy 兜底
    rc2, out2, _ = run_cmd(["vmstat", "1", str(count)], timeout=count + 10)
    vm, vm_rows = parse_vmstat(out2) if (rc2 == 0 and out2) else ({}, [])
    vmstat_ok = bool(vm_rows)
    if util is None and vmstat_ok and vm.get("us") is not None and vm.get("sy") is not None:
        util = round(vm["us"] + vm["sy"], 2)
    cs_per_sec = vm.get("cs") if vmstat_ok else None
    r = vm.get("r") if vmstat_ok else None
    cs_per_core = round(cs_per_sec / nproc, 2) if (cs_per_sec is not None and nproc) else None

    # 使用率状态
    util_status = status_for(util, th["util_warn"], th["util_crit"])
    per_core_status = status_for(per_core_max, th["util_warn"], th["util_crit"])

    # 饱和度状态：r/nproc 与 load/nproc
    r_status = "unknown"
    r_ratio = round(r / nproc, 3) if (r is not None and nproc) else None
    if r_ratio is not None:
        r_status = status_for(r_ratio, th["r_warn_ratio"], th["r_crit_ratio"])
    load_status = "unknown"
    load_ratio = round(load["load_1"] / nproc, 3) if (load.get("load_1") is not None and nproc) else None
    if load_ratio is not None:
        load_status = status_for(load_ratio, th["load_warn_ratio"], th["load_crit_ratio"])
    cs_status = status_for(cs_per_core, th["cs_per_core_warn"], th["cs_per_core_crit"])
    steal_status = "warn" if (vm.get("st") or 0) > 0 else "ok"
    sat_status = max([r_status, load_status, cs_status, steal_status], key=status_rank)

    return {
        "utilization": {
            "collected": util is not None,
            "value": util, "unit": "%", "status": util_status,
            "threshold": {"warn": th["util_warn"], "crit": th["util_crit"]},
            "per_core_max": per_core_max, "per_core_max_status": per_core_status,
            "detail": "平均 us+sy 占比%s" % (
                "（mpstat）" if mpstat_ok else "（vmstat，无 per-core 详情）"),
        },
        "saturation": {
            "collected": vmstat_ok or load.get("load_1") is not None,
            "status": sat_status,
            "run_queue_r": r, "nproc": nproc, "r_to_nproc_ratio": r_ratio, "r_status": r_status,
            "load_avg_1": load.get("load_1"), "load_to_nproc_ratio": load_ratio, "load_status": load_status,
            "cs_per_sec": cs_per_sec, "cs_per_core": cs_per_core, "cs_status": cs_status,
            "threshold": {"r_warn_ratio": th["r_warn_ratio"], "r_crit_ratio": th["r_crit_ratio"],
                          "cs_per_core_warn": th["cs_per_core_warn"], "cs_per_core_crit": th["cs_per_core_crit"]},
            "secondary_signals": {
                "iowait_wa": vm.get("wa"), "steal_st": vm.get("st"),
                "blocked_b": vm.get("b"), "interrupts_in": vm.get("in"),
                "steal_status": steal_status,
                "detail": "wa高=CPU等盘(指向磁盘)；st>0=VM被宿主偷CPU；b>0=有D状态进程(通常等IO)；in高=中断风暴",
            },
            "detail": "运行队列 r / 1min 负载 / 上下文切换，均按核数归一化；另看 wa/st/b/in 辅助信号",
        },
        "errors": None,  # 由 collect_errors 填充
    }


def collect_memory(count):
    """采集内存的 U/S/E。"""
    th = THRESHOLDS["memory"]
    rc, out, _ = run_cmd(["free", "-m"])
    fm = parse_free(out) if (rc == 0 and out) else None
    # swap 饱和度从 vmstat 拿 si/so
    rc2, out2, _ = run_cmd(["vmstat", "1", str(count)], timeout=count + 10)
    vm, _ = parse_vmstat(out2) if (rc2 == 0 and out2) else ({}, [])
    si = vm.get("si") if vm else None
    so = vm.get("so") if vm else None

    avail_pct = None
    util_status = "unknown"
    if fm and fm.get("total") and fm.get("available") is not None:
        avail_pct = round(fm["available"] / fm["total"] * 100.0, 2)
        # 内存“使用率”视角：available 越低越紧张
        util_status = status_for(100.0 - avail_pct, 100 - th["avail_pct_warn"], 100 - th["avail_pct_crit"])

    swap_active = (si and si > 0) or (so and so > 0)
    swap_max = max(si or 0, so or 0)
    swap_status = "unknown"
    if swap_max is not None:
        swap_status = status_for(swap_max, th["swap_kb_warn"], th["swap_kb_crit"])

    return {
        "utilization": {
            "collected": fm is not None,
            "total_mb": fm.get("total") if fm else None,
            "used_mb": fm.get("used") if fm else None,
            "available_mb": fm.get("available") if fm else None,
            "available_pct": avail_pct,
            "status": util_status,
            "threshold": {"available_pct_warn": th["avail_pct_warn"],
                          "available_pct_crit": th["avail_pct_crit"]},
            "detail": "看 available（真实可用），不是 free；free 低但 available 充足属正常（缓存可回收）",
        },
        "saturation": {
            "collected": si is not None or so is not None,
            "swap_in_kbs": si, "swap_out_kbs": so, "swap_active": bool(swap_active),
            "status": swap_status,
            "threshold": {"swap_kb_warn": th["swap_kb_warn"], "swap_kb_crit": th["swap_kb_crit"]},
            "detail": "si/so 持续非 0 = 物理内存不足，频繁换页，性能暴跌",
        },
        "errors": None,
    }


# --------------------------------------------------------------------------- #
# 文件系统容量与挂载健康
# --------------------------------------------------------------------------- #
# 本地文件系统：os.statvfs 直接查（安全、不阻塞）。
LOCAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "tmpfs", "overlay",
            "zfs", "jfs", "reiserfs", "ramfs"}
# 网络/FUSE 等可能卡死的挂载：用 setsid+短超时子进程探测，超时不 wait（避免 D 状态拖死脚本）。
PROBE_FS = ("fuse", "nfs", "cifs", "smb", "gluster", "gpfs", "ceph", "lustre", "9p")
# 跳过的伪文件系统。
SKIP_FS = {"proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "pstore",
           "mqueue", "securityfs", "debugfs", "tracefs", "fusectl", "configfs",
           "autofs", "binfmt_misc", "rpc_pipefs", "hugetlbfs", "selinuxfs",
           "bpf", "tracefs", "none"}


def _probe_mount(mp, timeout=2):
    """安全探测网络/FUSE 挂载点：用 setsid 起独立会话的 df 子进程，超时即放弃不 wait。
    返回 (status, info)：
      status="ok" + info={total_mb,used_mb,avail_mb,pct_full}
      status="unresponsive" + info=msg  （超时，疑似挂载卡死/不可达）
      status="error" + info=msg  （df 返回非零，如 Remote I/O error / Stale file handle）
      status="unknown" + info=msg  （起子进程失败）
    关键：start_new_session=True 把子进程放进新会话，即使它陷入 D 状态不可中断睡眠，
    主脚本退出时也不被它拖住（子进程被 init 收养）。
    """
    try:
        p = subprocess.Popen(["df", "-k", "--output=size,used,avail,pcent", mp],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, env=dict(os.environ, LC_ALL="C"))
    except Exception as e:
        return "unknown", "无法启动 df 探测: %s" % e
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 不 wait。尝试 kill 整个进程组（D 状态可能杀不掉，但子进程已在新会话，不拖累主脚本）。
        try:
            os.killpg(os.getpgid(p.pid), 9)
        except Exception:
            pass
        return "unresponsive", "df 探测 %ds 未返回，疑似挂载卡死/不可达（D 状态）" % timeout
    if p.returncode != 0:
        msg = (err or out or b"").decode(errors="replace").strip()
        low = msg.lower()
        if "permission denied" in low or "denied" in low:
            return "unknown", msg  # 无权限探测，非挂载故障
        return "error", msg or ("df 返回码 %s" % p.returncode)
    # 解析 df 输出：首行表头，次行数据：size used avail pcent
    lines = [ln for ln in out.decode(errors="replace").splitlines() if ln.strip()]
    if len(lines) < 2:
        return "error", "df 输出无法解析"
    toks = lines[-1].split()
    if len(toks) < 4:
        return "error", "df 输出字段不足: %s" % lines[-1]
    try:
        size_k = int(toks[0]); used_k = int(toks[1]); avail_k = int(toks[2])
        pct = float(toks[3].rstrip("%"))
    except ValueError:
        return "error", "df 数值解析失败: %s" % lines[-1]
    return "ok", {"total_mb": size_k // 1024, "used_mb": used_k // 1024,
                  "avail_mb": avail_k // 1024, "pct_full": round(pct, 2)}


def collect_filesystem():
    """采集各文件系统容量与挂载健康。返回 (filesystems_list, worst_fs_status, mount_problems)。"""
    th = THRESHOLDS["disk"]
    mounts = read_proc("/proc/mounts") or ""
    fss = []
    seen = set()
    for ln in mounts.splitlines():
        parts = ln.split()
        if len(parts) < 4:
            continue
        source, mp, fstype = parts[0], parts[1], parts[2]
        if fstype in SKIP_FS or mp in seen:
            continue
        seen.add(mp)
        entry = {"mount": mp, "fstype": fstype, "source": source, "collected": False}
        if fstype in LOCAL_FS:
            # 本地文件系统：os.statvfs 直接查，安全不阻塞
            try:
                st = os.statvfs(mp)
                total = st.f_blocks * st.f_frsize
                if total <= 0:
                    continue
                avail = st.f_bavail * st.f_frsize   # f_bavail = 普通用户可用（不含保留块）
                used = total - avail                 # 与 df 的 Use% 口径一致
                pct = used / total * 100.0
                entry.update(total_mb=total // 1048576, used_mb=used // 1048576,
                             avail_mb=avail // 1048576, pct_full=round(pct, 2),
                             collected=True, probe="local/statvfs",
                             status=status_for(pct, th["fs_full_warn"], th["fs_full_crit"]))
            except OSError as e:
                entry["status"] = "unknown"
                entry["probe_message"] = "statvfs 失败: %s" % e
        elif fstype.startswith(PROBE_FS):
            # 网络/FUSE：安全探测（setsid+短超时，卡死也不拖累脚本）
            status, info = _probe_mount(mp)
            entry["probe"] = "df(timeout=2s)"
            entry["probe_status"] = status
            if status == "ok":
                entry.update(info, collected=True,
                             status=status_for(info["pct_full"], th["fs_full_warn"], th["fs_full_crit"]))
            else:
                entry["collected"] = False
                entry["status"] = ("crit" if status == "unresponsive"
                                   else "unknown" if status == "unknown" else "warn")
                entry["probe_message"] = info
        else:
            # 其它非本地非网络 fs（nsfs/netns/autofs 等）：跳过，避免误报
            continue
        fss.append(entry)
    # 聚合：最差的容量状态 + 挂载问题列表
    worst = "ok"
    mount_problems = []
    for f in fss:
        if f.get("status") and status_rank(f["status"]) > status_rank(worst):
            worst = f["status"]
        ps = f.get("probe_status")
        if ps in ("unresponsive", "error"):
            mount_problems.append({"mount": f["mount"], "fstype": f["fstype"],
                                   "problem": ps, "message": f.get("probe_message", "")})
    return fss, worst, mount_problems


def collect_disk(count):
    """采集磁盘IO的 U/S/E：IO %util/await + 文件系统容量 + 挂载健康。
    iostat 优先，缺失则 /proc/diskstats 两次采样兜底。"""
    th = THRESHOLDS["disk"]
    rc, out, _ = run_cmd(["iostat", "-x", "1", str(count)], timeout=count + 10)
    devs = parse_iostat(out) if (rc == 0 and out) else []
    io_source = "iostat"
    if not devs:
        # 兜底：/proc/diskstats 两次采样
        a = read_proc("/proc/diskstats") or ""
        time.sleep(max(count, 1))
        b = read_proc("/proc/diskstats") or ""
        devs = parse_diskstats_delta(a, b, max(count, 1))
        io_source = "/proc/diskstats" if devs else "none"

    # 文件系统容量 + 挂载健康（始终采集，独立于 iostat）
    filesystems, fs_worst, mount_problems = collect_filesystem()

    if not devs:
        # iostat 与 diskstats 都没有，但文件系统容量仍可用
        io_util_status = "unknown"
        await_status = "unknown"
        top = {"device": None, "util": None, "await": None}
    else:
        top = max(devs, key=lambda d: (d.get("util") or 0))
        io_util_status = status_for(top.get("util"), th["util_warn"], th["util_crit"])
        await_status = status_for(top.get("await"), th["await_warn"], th["await_crit"])

    # 使用率维度：IO %util 与文件系统容量取最差
    util_status = max([io_util_status, fs_worst], key=status_rank)
    sat_status = max([io_util_status, await_status], key=status_rank)

    # 容量最紧的文件系统
    fs_top = None
    for f in filesystems:
        if f.get("collected") and f.get("pct_full") is not None:
            if fs_top is None or (f["pct_full"] or 0) > (fs_top.get("pct_full") or 0):
                fs_top = f

    return {
        "utilization": {
            "collected": (top.get("util") is not None) or (fs_top is not None),
            "top_device": top.get("device"), "io_util": top.get("util"), "unit": "%",
            "status": util_status, "io_status": io_util_status,
            "threshold": {"util_warn": th["util_warn"], "util_crit": th["util_crit"],
                          "fs_full_warn": th["fs_full_warn"], "fs_full_crit": th["fs_full_crit"]},
            "fs_capacity": {
                "collected": fs_top is not None or bool(mount_problems),
                "worst_mount": fs_top["mount"] if fs_top else None,
                "worst_pct_full": fs_top["pct_full"] if fs_top else None,
                "status": fs_worst,
                "detail": "文件系统容量使用率；>=95% 临近 ENOSPC，写盘会变慢甚至失败",
            },
            "all_devices": [{"device": d["device"], "util": d.get("util"),
                             "await": d.get("await"), "r_s": d.get("r_s"), "w_s": d.get("w_s")}
                            for d in devs],
            "detail": "IO %util 持续≥95% 即打满；文件系统容量≥95% 临近写满。NVMe 多队列 %util 可能 >100，看 await 更稳",
        },
        "saturation": {
            "collected": top.get("await") is not None,
            "top_device": top.get("device"), "await_ms": top.get("await"),
            "status": sat_status,
            "threshold": {"warn": th["await_warn"], "crit": th["await_crit"]},
            "detail": "await 高 = IO 请求排队拥堵；使用率不高但 await 高属典型隐性瓶颈",
        },
        "errors": {
            # 挂载问题先填这里，build_report 会再合并 dmesg 的 I/O 错误
            "collected": True,
            "mount_problems": mount_problems,
            "mount_status": "crit" if mount_problems else "ok",
            "matches": [],  # 由 build_report 填入 dmesg 匹配
            "detail": "挂载健康（卡死/Remote I/O error/Stale）+ dmesg I/O 错误",
        },
    }, io_source


def collect_network():
    """采集网络的 U/S/E。"""
    th = THRESHOLDS["network"]
    # 使用率：sar -n DEV 的 %ifutil；缺失则无（iftop 需交互不适用）
    rc, out, _ = run_cmd(["sar", "-n", "DEV", "1", "2"], timeout=15)
    ifaces = parse_sar_dev(out) if (rc == 0 and out) else []
    top_iface = max(ifaces, key=lambda x: x.get("ifutil") or 0) if ifaces else None
    ifutil = top_iface["ifutil"] if top_iface else None
    ifutil_status = status_for(ifutil, th["ifutil_warn"], th["ifutil_crit"])

    # 饱和度：ss -s 的 TIME_WAIT 等
    rc2, out2, _ = run_cmd(["ss", "-s"])
    ss = parse_ss_s(out2) if (rc2 == 0 and out2) else {}
    timewait = ss.get("timewait")
    tw_status = status_for(timewait, th["timewait_warn"], th["timewait_crit"])

    # 错误率：/proc/net/snmp 两次采样算重传率与 InErrs 增量（TCP 层）
    a = parse_snmp_tcp(read_proc("/proc/net/snmp"))
    time.sleep(1)
    b = parse_snmp_tcp(read_proc("/proc/net/snmp"))
    retrans_rate = None
    inerrs_delta = None
    if a["OutSegs"] is not None and b["OutSegs"] is not None:
        d_out = b["OutSegs"] - a["OutSegs"]
        d_retrans = (b["RetransSegs"] or 0) - (a["RetransSegs"] or 0)
        retrans_rate = round(d_retrans / d_out * 100.0, 4) if d_out > 0 else 0.0
    if a["InErrs"] is not None and b["InErrs"] is not None:
        inerrs_delta = b["InErrs"] - a["InErrs"]
    retrans_status = status_for(retrans_rate, th["retrans_rate_warn"], th["retrans_rate_crit"])

    # 网卡级错误/丢包（sar -n EDEV，NIC 层，比 TCP 更早暴露丢包）
    rc3, out3, _ = run_cmd(["sar", "-n", "EDEV", "1", "2"], timeout=15)
    nic_errs = parse_sar_edev(out3) if (rc3 == 0 and out3) else []
    nic_problems = [n for n in nic_errs if (n["rxerr"] or 0) + (n["txerr"] or 0)
                    + (n["rxdrop"] or 0) + (n["txdrop"] or 0) > 0]
    nic_status = "crit" if nic_problems else "ok"

    # accept 队列溢出（netstat -s，累积值；非 0 即 backlog/somaxconn 曾不足）
    rc4, out4, _ = run_cmd(["netstat", "-s"], timeout=10)
    overflow = parse_netstat_overflow(out4) if (rc4 == 0 and out4) else {}
    overflow_active = any((v or 0) > 0 for v in overflow.values())
    overflow_status = "warn" if overflow_active else "ok"

    err_status = max(
        ["crit" if (inerrs_delta is not None and inerrs_delta > 0) else "ok",
         retrans_status, nic_status, overflow_status], key=status_rank)

    return {
        "utilization": {
            "collected": ifutil is not None,
            "top_iface": top_iface["iface"] if top_iface else None,
            "ifutil": ifutil, "unit": "%", "status": ifutil_status,
            "threshold": {"warn": th["ifutil_warn"], "crit": th["ifutil_crit"]},
            "detail": "sar %ifutil；sar 未装则跳过网卡带宽维度",
        },
        "saturation": {
            "collected": timewait is not None,
            "timewait": timewait, "estab": ss.get("estab"), "total": ss.get("total"),
            "status": tw_status,
            "threshold": {"warn": th["timewait_warn"], "crit": th["timewait_crit"]},
            "detail": "TIME_WAIT 过多会占满端口致新连接失败；阈值与并发强相关，需结合业务判断",
        },
        "errors": {
            "collected": retrans_rate is not None or inerrs_delta is not None or bool(nic_errs),
            "retrans_rate_pct": retrans_rate, "inerrs_delta": inerrs_delta,
            "nic_errors": nic_problems,
            "accept_queue_overflow": overflow, "accept_overflow_active": overflow_active,
            "status": err_status,
            "threshold": {"retrans_rate_warn": th["retrans_rate_warn"],
                          "retrans_rate_crit": th["retrans_rate_crit"]},
            "detail": "TCP 重传/InErrs + 网卡级 rxerr/txerr/rxdrop/txdrop(sar EDEV) + accept 队列溢出；任一非 0 即提示丢包/链路/队列异常",
        },
    }


# --------------------------------------------------------------------------- #
# 环境检查
# --------------------------------------------------------------------------- #
def check_env():
    nproc = get_nproc()
    tools = {
        "vmstat": which("vmstat"), "mpstat": which("mpstat"), "pidstat": which("pidstat"),
        "iostat": which("iostat"), "sar": which("sar"), "free": which("free"),
        "ss": which("ss"), "netstat": which("netstat"), "uptime": which("uptime"),
        "journalctl": which("journalctl"),
    }
    # dmesg 是否可读
    rc, out, _ = run_cmd(["dmesg"], timeout=5)
    dmesg_ok = (rc == 0 and out is not None and "denied" not in (out or "").lower()
                and "operation not permitted" not in (out or "").lower())
    result = {
        "success": True,
        "env_check": True,
        "cpu_cores": nproc,
        "tools": tools,
        "dmesg_readable": dmesg_ok,
        "note": ("mpstat/pidstat/iostat/sar 属 sysstat 包，未装则对应维度降级；"
                 "iostat 缺失时脚本用 /proc/diskstats 兜底；"
                 "dmesg 在现代内核常需 root（kernel.dmesg_restrict=1），"
                 "缺失时用 journalctl -k 兜底查错误率。"),
        "error_message": "",
    }
    print(json.dumps({"use_analysis_result": result}, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# 报告组装
# --------------------------------------------------------------------------- #
def build_report(count, resources, nproc):
    """运行采集并组装契约。"""
    env_tools = {
        "vmstat": which("vmstat"), "mpstat": which("mpstat"), "iostat": which("iostat"),
        "sar": which("sar"), "ss": which("ss"),
    }
    missing = [k for k, v in env_tools.items() if not v]
    env_warnings = []
    if not env_tools["mpstat"]:
        env_warnings.append("mpstat 未安装，CPU 每核详情降级为 vmstat 的 us+sy 聚合；可装 sysstat")
    if not env_tools["iostat"]:
        env_warnings.append("iostat 未安装，磁盘 IO 改用 /proc/diskstats 兜底（无设备级 await 精度略低）")
    if not env_tools["sar"]:
        env_warnings.append("sar 未安装，网卡带宽使用率与网卡级错误(sar EDEV)维度将跳过")
    numa_nodes = get_numa_nodes()
    if numa_nodes > 1:
        env_warnings.append("检测到 %d 个 NUMA 节点：跨节点内存访问延迟是 USE 的 U/S/E 三维度都看不出的隐性瓶颈，大核数机器尤甚；需用 perf/SPE/numactl 进一步查" % numa_nodes)

    findings = {}
    sources = {}

    # 错误率统一采集一次，分派到各资源
    err_cats, err_source, err_note = collect_errors()
    if err_note:
        env_warnings.append(err_note)
    sources["errors"] = err_source

    if "cpu" in resources:
        findings["cpu"] = collect_cpu(count, nproc)
        findings["cpu"]["errors"] = {
            "collected": err_source != "none",
            "status": "crit" if err_cats["cpu"] or err_cats["system"] else "ok",
            "matches": err_cats["cpu"] + err_cats["system"],
            "source": err_source,
            "detail": "dmesg/journalctl 中 CPU/硬件/内核严重错误",
        }
    if "memory" in resources:
        findings["memory"] = collect_memory(count)
        findings["memory"]["errors"] = {
            "collected": err_source != "none",
            "status": "crit" if err_cats["memory"] else "ok",
            "matches": err_cats["memory"], "source": err_source,
            "detail": "OOM / 内存分配失败记录",
        }
    if "disk" in resources:
        disk_findings, disk_src = collect_disk(count)
        findings["disk"] = disk_findings
        sources["disk"] = disk_src
        # 合并 dmesg I/O 错误到磁盘 errors（挂载问题已由 collect_disk 填入 mount_problems）
        de = findings["disk"]["errors"]
        de["matches"] = err_cats["disk"]
        de["source"] = err_source
        de["dmesg_collected"] = err_source != "none"
        dmesg_status = "crit" if err_cats["disk"] else "ok"
        de["status"] = max([de.get("mount_status", "ok"), dmesg_status], key=status_rank)
    if "network" in resources:
        findings["network"] = collect_network()
        findings["network"].setdefault("errors", {})
        # 网络错误已在 collect_network 里采集（重传/InErrs），合并 dmesg 网络类
        findings["network"]["errors"]["dmesg_matches"] = err_cats["network"]
        if err_cats["network"]:
            findings["network"]["errors"]["status"] = "crit"

    bottlenecks = []
    # USE 优先级：errors > utilization > saturation
    dim_order = ["errors", "utilization", "saturation"]
    for res, dims in findings.items():
        for dim in dim_order:
            d = dims.get(dim)
            if not d or not d.get("collected"):
                continue
            st = d.get("status", "unknown")
            if st in ("warn", "crit"):
                bottlenecks.append({
                    "resource": res, "dimension": dim, "status": st,
                    "summary": _bottleneck_summary(res, dim, d),
                })
    bottlenecks.sort(key=lambda b: (-status_rank(b["status"]),
                                    dim_order.index(b["dimension"])))

    root_hint = _root_cause_hint(bottlenecks)

    result = {
        "success": True,
        "mode": "scan",
        "cpu_cores": nproc,
        "sample_count": count,
        "environment": {"tools": env_tools, "missing": missing, "warnings": env_warnings,
                        "errors_source": err_source},
        "findings": findings,
        "bottlenecks": bottlenecks,
        "root_cause_hint": root_hint,
        "summary_text": "",
        "error_message": "",
    }
    result["summary_text"] = render_summary(result)
    return {"use_analysis_result": result}


def _bottleneck_summary(res, dim, d):
    if res == "cpu" and dim == "utilization":
        return "CPU 使用率 %.1f%%（峰值 %.1f%%）" % (d.get("value") or 0, d.get("per_core_max") or 0)
    if res == "cpu" and dim == "saturation":
        return "CPU 饱和：r=%s（/nproc=%.2f），负载1min=%s（/nproc=%.2f），cs/核=%s" % (
            d.get("run_queue_r"), d.get("r_to_nproc_ratio") or 0,
            d.get("load_avg_1"), d.get("load_to_nproc_ratio") or 0, d.get("cs_per_core"))
    if res == "memory" and dim == "utilization":
        return "内存紧张：available %.1f%%（%sMB / %sMB）" % (
            d.get("available_pct") or 0, d.get("available_mb"), d.get("total_mb"))
    if res == "memory" and dim == "saturation":
        return "内存饱和：swap si=%s so=%s KB/s" % (d.get("swap_in_kbs"), d.get("swap_out_kbs"))
    if res == "disk" and dim == "utilization":
        io = d.get("io_util")
        fsc = d.get("fs_capacity", {})
        parts = []
        if io is not None:
            parts.append("IO %s %%util=%.1f%%" % (d.get("top_device") or "?", io))
        if fsc.get("worst_mount"):
            parts.append("%s 容量=%.1f%%" % (fsc["worst_mount"], fsc.get("worst_pct_full") or 0))
        return "；".join(parts) if parts else "磁盘使用率异常"
    if res == "disk" and dim == "saturation":
        return "磁盘 %s await=%.1fms" % (d.get("top_device") or "?", d.get("await_ms") or 0)
    if res == "disk" and dim == "errors":
        mp = d.get("mount_problems") or []
        mn = len(d.get("matches") or [])
        parts = []
        if mp:
            parts.append("%d 个挂载异常(%s)" % (len(mp), ",".join(p["mount"] for p in mp[:2])))
        if mn:
            parts.append("%d 条 I/O 错误日志" % mn)
        return "磁盘错误：" + "；".join(parts) if parts else "磁盘错误"
    if res == "network" and dim == "utilization":
        return "网卡 %s %%ifutil=%.1f%%" % (d.get("top_iface"), d.get("ifutil") or 0)
    if res == "network" and dim == "saturation":
        return "TIME_WAIT=%s（estab=%s）" % (d.get("timewait"), d.get("estab"))
    if res == "network" and dim == "errors":
        parts = []
        rr = d.get("retrans_rate_pct")
        if rr is not None:
            parts.append("重传率 %s%%" % rr)
        if d.get("inerrs_delta"):
            parts.append("InErrs增量 %s" % d.get("inerrs_delta"))
        for n in (d.get("nic_errors") or []):
            parts.append("%s rxdrop=%s/rxerr=%s/txdrop=%s" % (
                n["iface"], n["rxdrop"], n["rxerr"], n["txdrop"]))
        if d.get("accept_overflow_active"):
            parts.append("accept队列溢出")
        return "网络错误：" + "，".join(parts) if parts else "网络错误"
    if dim == "errors":
        matches = d.get("matches") or []
        return "%s 错误：%d 条相关日志%s" % (res, len(matches), "（含内核严重错误）" if res == "cpu" else "")
    return "%s %s 异常" % (res, dim)


def _root_cause_hint(bottlenecks):
    """按 USE 优先级（错误 > 使用率 > 饱和度）给出最可疑根因提示。"""
    if not bottlenecks:
        return "未发现明显瓶颈（四大资源 U/S/E 均在阈值内）。若实测仍慢，怀疑 cache/内存层级（MCA/perf/SPE 范畴）或前端/应用层问题，建议结合动态采样进一步排查。"
    # 优先级：crit 的 errors > crit 的 utilization > crit 的 saturation > warn
    crit = [b for b in bottlenecks if b["status"] == "crit"]
    if crit:
        errs = [b for b in crit if b["dimension"] == "errors"]
        if errs:
            return "首要怀疑：错误/故障（%s）--优先排障，性能问题常由硬件/内核异常引起" % "；".join(b["summary"] for b in errs)
        utils = [b for b in crit if b["dimension"] == "utilization"]
        if utils:
            return "首要怀疑：资源使用率打满（%s）--资源即将耗尽" % "；".join(b["summary"] for b in utils)
        sats = [b for b in crit if b["dimension"] == "saturation"]
        if sats:
            return "首要怀疑：饱和度/排队瓶颈（%s）--使用率可能不高但请求拥堵，是典型隐性瓶颈" % "；".join(b["summary"] for b in sats)
    return "存在告警级异常（%s），尚未到临界，建议持续观察或针对性深挖" % "；".join(b["summary"] for b in bottlenecks[:3])


# --------------------------------------------------------------------------- #
# 可读摘要
# --------------------------------------------------------------------------- #
def _st(s):
    return {"crit": "🔴 临界", "warn": "🟡 告警", "ok": "🟢 正常",
            "unknown": "⬜ 未采集"}.get(s, s)


def render_summary(r):
    lines = []
    lines.append("# USE 性能扫描报告")
    lines.append("")
    lines.append("**主机 CPU 核数**: %s | **采样**: 每命令 %d 次 | **错误日志源**: %s" % (
        r["cpu_cores"], r["sample_count"], r["environment"]["errors_source"]))
    if r["environment"]["warnings"]:
        for w in r["environment"]["warnings"]:
            lines.append("- ⚠️ %s" % w)
    lines.append("")
    lines.append("**首要怀疑**: %s" % r["root_cause_hint"])
    lines.append("")

    res_names = {"cpu": "CPU", "memory": "内存", "disk": "磁盘IO", "network": "网络"}
    for res in ["cpu", "memory", "disk", "network"]:
        dims = r["findings"].get(res)
        if not dims:
            continue
        lines.append("## %s" % res_names[res])
        lines.append("| 维度 | 状态 | 关键指标 |")
        lines.append("|------|------|---------|")
        u, s, e = dims.get("utilization", {}), dims.get("saturation", {}), dims.get("errors", {})
        if res == "cpu":
            lines.append("| U 使用率 | %s | 平均 %.1f%%，峰值 %.1f%% |" % (
                _st(u.get("status")), u.get("value") or 0, u.get("per_core_max") or 0))
            ssig = s.get("secondary_signals", {})
            extra = ""
            if ssig:
                ps = []
                if (ssig.get("blocked_b") or 0) > 0:
                    ps.append("b=%s" % ssig.get("blocked_b"))
                if (ssig.get("iowait_wa") or 0) >= 5:
                    ps.append("wa=%s%%" % ssig.get("iowait_wa"))
                if (ssig.get("steal_st") or 0) > 0:
                    ps.append("st=%s%%" % ssig.get("steal_st"))
                if ps:
                    extra = " [" + "，".join(ps) + "]"
            lines.append("| S 饱和度 | %s | r=%s（/核=%.2f），负载1min=%s，cs/核=%s%s |" % (
                _st(s.get("status")), s.get("run_queue_r"), s.get("r_to_nproc_ratio") or 0,
                s.get("load_avg_1"), s.get("cs_per_core"), extra))
            lines.append("| E 错误率 | %s | %s |" % (_st(e.get("status")),
                ("%d 条相关日志" % len(e.get("matches") or [])) if e.get("matches") else "无"))
        elif res == "memory":
            lines.append("| U 使用率 | %s | available %.1f%%（%sMB/%sMB） |" % (
                _st(u.get("status")), u.get("available_pct") or 0, u.get("available_mb"), u.get("total_mb")))
            lines.append("| S 饱和度 | %s | swap si=%s so=%s KB/s |" % (
                _st(s.get("status")), s.get("swap_in_kbs"), s.get("swap_out_kbs")))
            lines.append("| E 错误率 | %s | %s |" % (_st(e.get("status")),
                ("%d 条 OOM 记录" % len(e.get("matches") or [])) if e.get("matches") else "无 OOM"))
        elif res == "disk":
            io_u = ("IO %s=%.1f%%" % (u.get("top_device") or "-", u.get("io_util") or 0)
                    if u.get("io_util") is not None else "IO 未采集")
            fsc = u.get("fs_capacity", {})
            fs_str = ""
            if fsc.get("worst_mount"):
                fs_str = "；%s 容量=%.1f%%" % (fsc["worst_mount"], fsc.get("worst_pct_full") or 0)
            elif fsc.get("collected") is False:
                fs_str = ""
            lines.append("| U 使用率 | %s | %s%s |" % (_st(u.get("status")), io_u, fs_str))
            lines.append("| S 饱和度 | %s | %s await=%.1fms |" % (
                _st(s.get("status")), s.get("top_device") or "-", s.get("await_ms") or 0))
            mp = e.get("mount_problems") or []
            dmesg_n = len(e.get("matches") or [])
            err_parts = []
            if mp:
                err_parts.append("%d 个挂载异常(%s)" % (len(mp), ",".join(p["mount"] for p in mp[:2])))
            if dmesg_n:
                err_parts.append("%d 条 I/O 错误" % dmesg_n)
            if not err_parts and not e.get("dmesg_collected", True):
                err_parts.append("dmesg 未采集")
            lines.append("| E 错误率 | %s | %s |" % (_st(e.get("status")), "；".join(err_parts) or "无"))
        elif res == "network":
            lines.append("| U 使用率 | %s | %s %%ifutil=%.1f%% |" % (
                _st(u.get("status")), u.get("top_iface") or "-", u.get("ifutil") or 0))
            lines.append("| S 饱和度 | %s | TIME_WAIT=%s（estab=%s） |" % (
                _st(s.get("status")), s.get("timewait"), s.get("estab")))
            net_extra = []
            for n in (e.get("nic_errors") or []):
                net_extra.append("%s rxdrop=%s/rxerr=%s/txdrop=%s" % (
                    n["iface"], n["rxdrop"], n["rxerr"], n["txdrop"]))
            if e.get("accept_overflow_active"):
                ov = e.get("accept_queue_overflow") or {}
                net_extra.append("accept队列溢出(累积=%s)" % (ov.get("listen_overflowed") or 0))
            net_str = "重传率 %s%%，InErrs增量 %s" % (e.get("retrans_rate_pct"), e.get("inerrs_delta"))
            if net_extra:
                net_str += "；" + "；".join(net_extra)
            lines.append("| E 错误率 | %s | %s |" % (_st(e.get("status")), net_str))
        lines.append("")

    if r["bottlenecks"]:
        lines.append("## 标记的瓶颈")
        for b in r["bottlenecks"]:
            lines.append("- **[%s] %s / %s**：%s" % (
                _st(b["status"]), res_names.get(b["resource"], b["resource"]),
                {"utilization": "使用率", "saturation": "饱和度", "errors": "错误率"}[b["dimension"]],
                b["summary"]))
        lines.append("")
    lines.append("> 解读口诀：先排错误（E）→ 再看使用率（U）查负载 → 最后看饱和度（S）查拥堵。"
                 "本扫描为快照，饱和度类指标建议多次采样或拉长观察窗口确认。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="USE 分析法 Linux 性能扫描器")
    ap.add_argument("--check-env", action="store_true",
                    help="仅检查诊断工具可用性，不做分析")
    ap.add_argument("--scan", action="store_true", default=True,
                    help="采集当前主机指标并分析（默认）")
    ap.add_argument("--count", type=int, default=3,
                    help="间隔类命令的采样次数（默认 3，约 3-15s）")
    ap.add_argument("--resources", default="cpu,mem,disk,net",
                    help="扫描的资源子集，逗号分隔：cpu,mem,disk,net（默认全部）")
    ap.add_argument("--text", action="store_true",
                    help="仅打印可读摘要（默认输出 JSON 契约）")
    args = ap.parse_args()

    if args.check_env:
        check_env()
        return

    res_map = {"cpu": "cpu", "mem": "memory", "memory": "memory",
               "disk": "disk", "io": "disk", "net": "network", "network": "network"}
    resources = []
    for r in args.resources.split(","):
        r = r.strip().lower()
        if r in res_map and res_map[r] not in resources:
            resources.append(res_map[r])
    if not resources:
        die("--resources 解析为空，可用: cpu,mem,disk,net")

    nproc = get_nproc()
    report = build_report(args.count, resources, nproc)

    if args.text:
        print(report["use_analysis_result"]["summary_text"])
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
