#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-thread-affinity-analyzer analyze — 时间序列 → 拓扑富化 → 异常判定 → 报告

输入：<采样目录>/samples.jsonl + topology_raw.txt
输出：<采样目录>/绑核分析报告.html + summary.json（原子写入）
依赖：python3 >= 3.6，纯 stdlib。

退出码：0 成功（发现异常也算成功——异常是产品不是错误）；
        1 用法/IO 错误（含 argparse 用法错误——不与 2 撞码）；
        2 samples.jsonl 无法解析；3 topology_raw.txt 缺失（跨机包须带上；
        仅 --allow-sys-fallback 显式指定时才用本机 /sys 补采并在报告
        标注拓扑来源）。

口径说明（与 SKILL.md 一致）：
- psr 是"最后运行所在核"快照：迁移数为下界估计；
- 活跃样本 = 相邻两个在场 tick 的 utime+stime 增量 / 时窗 >= 20%；
- A6 的 psr ∉ 允许掩码多来自 tick 内 psr 与掩码两读之间的掩码收紧
  竞态（stat/status 分读窗口），预期 <0.5%。
"""
import argparse
import glob as globmod
import html
import json
import os
import re
import sys

VERSION = "1.1.0"

# ------------------------------------------------------------------ 工具

def parse_cpulist(s):
    """'0-3,8,10-11' → {0,1,2,3,8,10,11}；解析失败返回 None"""
    if not s or s == "?" or "unknown" in s:
        return None
    out = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                return None
        else:
            try:
                out.add(int(part))
            except ValueError:
                return None
    return out


def format_cpulist(cpus):
    """{0,1,2,3,8,10,11} → '0-3,8,10-11'"""
    if not cpus:
        return "-"
    xs = sorted(cpus)
    parts, start, prev = [], xs[0], xs[0]
    for x in xs[1:]:
        if x == prev + 1:
            prev = x
            continue
        parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
        start = prev = x
    parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ",".join(parts)


def pct(x, digits=1):
    try:
        return ("%." + str(digits) + "f") % x
    except (TypeError, ValueError):
        return "-"


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ 拓扑

class Topology(object):
    def __init__(self):
        self.host = ""
        self.kernel = ""
        self.clk_tck = 100
        self.cpu_node = {}      # cpu -> node
        self.l3_domains = []    # [set(cpus)]（顺序即 domain id）
        self.npu = []           # [{"bdf","node"}] node 为 int 或 None
        self.target_cpuset = ""
        self.main_allowed = ""
        self.env_vars = {}
        self.source = "raw"

    @property
    def nodes(self):
        return sorted(set(self.cpu_node.values()))

    @property
    def npu_nodes(self):
        return sorted({d["node"] for d in self.npu if d["node"] is not None})

    @classmethod
    def from_raw(cls, path):
        t = cls()
        if not os.path.isfile(path):
            return None
        section = None
        host_line1 = ""      # uname -s -r（"Linux 5.10.0-60.18.0"）
        host_pretty = ""     # /etc/os-release 的 PRETTY_NAME
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("=== "):
                    section = line[4:].strip()
                    continue
                if section == "host":
                    # collector 写两行：uname -s -r + PRETTY_NAME；
                    # kernel 从第一行提取，host 优先取发行版名
                    if not host_line1:
                        host_line1 = line
                        m = re.match(r"Linux (\S+)", line)
                        t.kernel = m.group(1) if m else line
                    elif not host_pretty:
                        host_pretty = line
                        t.host = line
                elif section == "clk_tck":
                    try:
                        t.clk_tck = int(line.strip())
                    except ValueError:
                        pass
                elif section == "node_cpulist":
                    parts = line.split("|")
                    if len(parts) != 2:
                        continue
                    try:
                        node = int(parts[0])
                    except ValueError:
                        continue
                    cpus = parse_cpulist(parts[1])
                    if cpus:
                        for c in cpus:
                            t.cpu_node[c] = node
                elif section == "l3_domains":
                    cpus = parse_cpulist(line)
                    if cpus and cpus not in t.l3_domains:
                        t.l3_domains.append(cpus)
                elif section == "npu_pci":
                    parts = line.split("|")
                    if len(parts) != 2:
                        continue
                    try:
                        node = int(parts[1])
                        if node < 0:
                            node = None      # -1 = sysfs 未暴露归属（VM/容器常见）→ 不可判定
                    except ValueError:
                        node = None
                    t.npu.append({"bdf": parts[0], "node": node})
                elif section == "target_env":
                    if "=" in line:
                        k, v = line.split("=", 1)
                        t.env_vars[k] = v
                elif section == "target_view":
                    if line.startswith("cpuset="):
                        t.target_cpuset = line.split("=", 1)[1]
                    elif line.startswith("Cpus_allowed_list:"):
                        t.main_allowed = line.split(":", 1)[1].strip()
        if not t.host:
            t.host = host_pretty or host_line1 or ""
        return t

    @classmethod
    def from_sys(cls):
        """本机补采（仅 --allow-sys-fallback 显式指定时使用，报告标注来源）。"""
        t = cls()
        t.source = "sys"
        t.host = os.uname().nodename if hasattr(os, "uname") else ""
        t.kernel = os.uname().release if hasattr(os, "uname") else ""
        for f in globmod.glob("/sys/devices/system/node/node*/cpulist"):
            node = os.path.basename(os.path.dirname(f))[4:]
            try:
                node = int(node)
            except ValueError:
                continue
            with open(f) as fh:
                cpus = parse_cpulist(fh.read().strip())
            if cpus:
                for c in cpus:
                    t.cpu_node[c] = node
        seen = []
        for f in sorted(globmod.glob("/sys/devices/system/cpu/cpu[0-9]*/cache/index3/shared_cpu_list")):
            with open(f) as fh:
                cpus = parse_cpulist(fh.read().strip())
            if cpus and cpus not in seen:
                seen.append(cpus)
        t.l3_domains = seen
        for vf in globmod.glob("/sys/bus/pci/devices/*/vendor"):
            try:
                with open(vf) as fh:
                    vendor = fh.read().strip()
            except OSError:
                continue
            if vendor != "0x19e5":
                continue
            bdf = os.path.basename(os.path.dirname(vf))
            # vendor + class 双校验：单查 vendor 会把 Huawei 网卡
            # （hns3/Hi1822，同 0x19e5）计入 NPU 集合
            try:
                with open("/sys/bus/pci/devices/%s/class" % bdf) as fh:
                    klass = fh.read().strip()
            except OSError:
                continue
            if not klass.startswith("0x120000"):
                continue
            node = None
            try:
                with open("/sys/bus/pci/devices/%s/numa_node" % bdf) as fh:
                    node = int(fh.read().strip())
                if node < 0:
                    node = None   # -1 = 未知，不可参与 A2 判定
            except (OSError, ValueError):
                pass
            t.npu.append({"bdf": bdf, "node": node})
        return t

    def resolve(self):
        return bool(self.cpu_node)


# ------------------------------------------------------------------ 角色识别
# 角色规则外置于 references/roles.json（新增框架 = 加配置，不改代码）；
# 真源：Plugins/app-tuner/skills/kpbot-app-tuner/subskills/cpu-affinity-optimization/SKILL.md
# （PTA 分级模型 + vLLM/torchrun 实名映射表）。本表只做 comm→角色标注，
# 不做绑核策略推导；未匹配线程按观测数据呈现，不猜角色。

# 加载失败时的最小回退集（保持 A2 判定可用；stderr 警告，不静默）
_FALLBACK_RULES = [
    {"match": "exact", "pattern": "mainthread", "role": "pta_main", "label": "PTA 前向算子下发"},
    {"match": "contains", "pattern": "acl_thread", "role": "acl_thread", "label": "ACL 二级流水"},
    {"match": "prefix", "pattern": "hccl", "role": "hccl_comm", "label": "HCCL 集合通信"},
    {"match": "contains", "pattern": "hcom", "role": "hcom", "label": "HCCL hcom 通道"},
    {"match": "contains", "pattern": "d2h", "role": "d2h", "label": "Device→Host 拷贝"},
    {"match": "contains", "pattern": "h2d", "role": "h2d", "label": "Host→Device 拷贝"},
]
_FALLBACK_NPU_HOST = ["pta_main", "acl_thread", "hccl_comm", "hcom", "d2h", "h2d", "__main__"]

RULE_VALID_MATCH = ("exact", "prefix", "contains", "suffix")


class RoleConfig(object):
    def __init__(self, rules, npu_host_roles, display, source="fallback"):
        self.rules = rules                      # [(kind, pat, rid, label)]
        self.npu_host_roles = npu_host_roles    # set
        self.source = source                    # 生效配置的来源（roles.json 路径或 fallback）
        self.unnamed_label = display.get("unnamed_label", "未命名（数字/空名）")
        self.unmatched_label = display.get("unmatched_label", "未匹配")

    @classmethod
    def load(cls, path):
        """加载 roles.json；文件缺失/损坏/结构非法 → stderr 警告 + 回退集（safe-fail，不静默）。

        顶层必须是 JSON 对象、rules 必须是数组且条目为对象、npu_host_roles
        必须是数组、display 必须是对象——否则整表转入回退分支并说明原因
        （不因一份结构非法的自定义配置令报告生成失败）。
        """
        rules, npu_host, display = [], set(_FALLBACK_NPU_HOST), {}
        source = "fallback"
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    obj = json.load(fh)
                if not isinstance(obj, dict):
                    raise ValueError("顶层必须是 JSON 对象（got %s）"
                                     % type(obj).__name__)
                raw = obj.get("rules", [])
                if not isinstance(raw, list):
                    raise ValueError("rules 必须是数组")
                rules = []
                for r in raw:
                    if not isinstance(r, dict):
                        continue    # 单条坏规则跳过，不整表报废
                    kind = str(r.get("match", "")).lower()
                    pat = str(r.get("pattern", ""))
                    rid = r.get("role")
                    label = str(r.get("label", ""))
                    if kind not in RULE_VALID_MATCH or not pat or not rid:
                        continue
                    rules.append((kind, pat, str(rid), label))
                raw_host = obj.get("npu_host_roles", _FALLBACK_NPU_HOST)
                if not isinstance(raw_host, list):
                    raise ValueError("npu_host_roles 必须是数组")
                npu_host = set(str(x) for x in raw_host)
                npu_host.add("__main__")
                display = obj.get("display", {}) or {}
                if not isinstance(display, dict):
                    raise ValueError("display 必须是对象")
                source = path
            except (ValueError, OSError) as e:
                print("WARN: roles.json 非法（%s），使用内置回退规则集" % e,
                      file=sys.stderr)
                rules, npu_host, display = [], set(_FALLBACK_NPU_HOST), {}
        else:
            print("WARN: roles 配置缺失（%s），使用内置回退规则集" % (path or "未指定"),
                  file=sys.stderr)
        if not rules:
            rules = [(r["match"], r["pattern"], r["role"], r["label"])
                     for r in _FALLBACK_RULES]
        return cls(rules, npu_host, display, source), source

    def match(self, comm, is_main_thread):
        lc = (comm or "").lower()
        if is_main_thread:
            return "__main__", "进程主线程"
        for kind, pat, rid, label in self.rules:
            if kind == "exact" and lc == pat:
                return rid, label
            if kind == "prefix" and lc.startswith(pat):
                return rid, label
            if kind == "contains" and pat in lc:
                return rid, label
            if kind == "suffix" and lc.endswith(pat):
                return rid, label
        if lc.isdigit() or lc == "":
            return "__unnamed__", self.unnamed_label
        return None, self.unmatched_label


# ------------------------------------------------------------------ 样本解析

class ThreadAgg(object):
    __slots__ = ("tid", "epoch", "comm", "first_seq", "last_seq", "first_ts", "last_ts",
                 "last_utime", "last_stime", "active_samples", "migrations", "delta_ticks",
                 "psr_hist", "allowed_masks", "mask_changes", "mask_events", "violations",
                 "checked", "busy_pct", "flags")

    def __init__(self, tid, epoch):
        self.tid = tid
        self.epoch = epoch
        self.comm = ""
        self.first_seq = None
        self.last_seq = None
        self.first_ts = None
        self.last_ts = None
        self.last_utime = None
        self.last_stime = None
        self.active_samples = 0      # 活跃样本数（相邻在场 tick 对）
        self.migrations = 0          # 活跃样本间 psr 变化次数（下界估计）
        self.delta_ticks = 0         # 生命周期内 utime+stime 总增量
        self.psr_hist = {}           # psr -> 活跃样本计数
        self.allowed_masks = []      # 按时间序的去重掩码
        self.mask_changes = 0
        self.mask_events = []        # [(seq, allowed_str)] 变更时刻（A5 共同区间比较用）
        self.violations = 0          # psr ∉ 允许掩码样本数
        self.checked = 0             # 参与校验的样本数
        self.busy_pct = 0.0
        self.flags = []

    @property
    def key(self):
        return (self.tid, self.epoch)

    def mask_at(self, seq):
        """该线程在 seq tick 时刻的掩码（事件序列回放）。"""
        cur = None
        for s, m in self.mask_events:
            if s <= seq:
                cur = m
            else:
                break
        return cur

    def note_psr(self, psr, allowed_set, prev_psr, active):
        if active:
            self.active_samples += 1
            self.psr_hist[psr] = self.psr_hist.get(psr, 0) + 1
            if prev_psr is not None and prev_psr != psr:
                self.migrations += 1
        if allowed_set is not None:
            self.checked += 1
            if psr not in allowed_set:
                self.violations += 1


def load_samples(path):
    if not os.path.isfile(path):
        return None, [], None, 0
    meta, end, malformed = None, None, 0
    ticks = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if "meta" in obj and meta is None:
                meta = obj["meta"]
            elif "end" in obj:
                end = obj["end"]
            elif "seq" in obj and "ts" in obj:
                ticks.append(obj)
            else:
                malformed += 1
    ticks.sort(key=lambda t: t["seq"])
    return meta, ticks, end, malformed


def aggregate(ticks, meta, topo):
    """线程实例聚合。

    返回 (threads dict key→ThreadAgg, per_tick_active dict seq→{psr:[(tid,epoch)]})。
    per_tick_active 记录的是**活跃样本的线程实例**（A3 挤核按实例判定，
    不按计数——分时共享核的三线程必须逐个核过忙阈值才标记）。
    """
    pid = meta.get("pid")
    interval = float(meta.get("interval", 0.5) or 0.5)
    clk = topo.clk_tck or 100
    threads = {}
    per_tick_active = {}
    prev = {}   # tid -> (epoch, ts, utime, stime, psr)

    for t in ticks:
        if "absent_proc" in t:
            continue
        now_active = {}
        for th in t.get("threads", []):
            tid = th.get("tid")
            psr = th.get("psr")
            if not isinstance(tid, int) or not isinstance(psr, int):
                continue
            comm = th.get("comm", "")
            utime, stime = th.get("utime", 0), th.get("stime", 0)
            allowed = parse_cpulist(th.get("allowed", ""))

            # tid 复用/退出重入：utime 回退 → 新实例（epoch 递增）
            p = prev.get(tid)
            if p is not None and utime >= p[2]:
                epoch = p[0]
            elif p is not None:
                epoch = p[0] + 1
            else:
                epoch = 0
            key = (tid, epoch)
            agg = threads.get(key)
            if agg is None:
                agg = threads[key] = ThreadAgg(tid, epoch)

            # 活跃判定：本 tick 相对上一在场 tick 的增量 / 真实时窗
            active = False
            if p is not None and epoch == p[0]:
                d = (utime - p[2]) + (stime - p[3])
                window = max(t["ts"] - p[1], 1e-6)
                if d >= 0 and d / clk / window >= 0.20:
                    active = True
                agg.delta_ticks += max(d, 0)
            if agg.first_seq is None:
                agg.first_seq, agg.first_ts = t["seq"], t["ts"]
            agg.last_seq, agg.last_ts = t["seq"], t["ts"]
            agg.comm = comm or agg.comm
            if allowed is not None:
                allowed_str = th.get("allowed")
                if not agg.allowed_masks or agg.allowed_masks[-1] != allowed_str:
                    agg.allowed_masks.append(allowed_str)
                    agg.mask_events.append((t["seq"], allowed_str))
            prev_psr = p[4] if (p is not None and epoch == p[0]) else None
            agg.note_psr(psr, allowed, prev_psr, active)
            if active:
                now_active.setdefault(psr, []).append(key)
            prev[tid] = (epoch, t["ts"], utime, stime, psr)
        per_tick_active[t["seq"]] = now_active

    # 忙闲比：生命周期增量 / 生命周期时长
    for agg in threads.values():
        span = (agg.last_ts - agg.first_ts) if agg.last_ts > agg.first_ts else interval
        agg.busy_pct = min(100.0, agg.delta_ticks / clk / span * 100.0)
    return threads, per_tick_active


# ------------------------------------------------------------------ 异常判定

SEV_ORDER = {"high": 0, "medium": 1, "info": 2}


def detect_findings(threads, per_tick_active, topo, meta, busy_threshold, max_conc,
                   roles):
    findings = []
    pid = meta.get("pid")
    have_node = topo.resolve()
    npu_nodes = topo.npu_nodes
    data_ticks = len(per_tick_active)
    instances = len(threads)

    # 主线程（最新实例）与全系统 CPU 数
    main_key = None
    for key in sorted(threads, key=lambda k: (k[1], k[0])):
        if key[0] == pid:
            main_key = key
    main_agg = threads.get(main_key)

    def flag(agg, fid):
        agg.flags.append(fid)

    for agg in threads.values():
        del agg.flags[:]

    # A1 忙线程未绑核跨 NUMA 漂移（掩码证实跨多 node 才下"未绑核"结论）
    a1u = []   # 掩码不可读的跨 node 忙线程：只报告观测，不判"未绑核"
    if have_node:
        a1 = []
        for agg in threads.values():
            nodes = set()
            for psr in agg.psr_hist:
                if psr in topo.cpu_node:
                    nodes.add(topo.cpu_node[psr])
            if agg.busy_pct < busy_threshold or len(nodes) < 2:
                continue
            allowed_set = parse_cpulist(agg.allowed_masks[-1]) if agg.allowed_masks else None
            if allowed_set is None:
                # 掩码不可读：绑核状态未知——只报告已观测到的跨节点运行
                a1u.append((agg, nodes))
                continue
            allowed_nodes = set()
            for c in allowed_set:
                if c in topo.cpu_node:
                    allowed_nodes.add(topo.cpu_node[c])
            if len(allowed_nodes) >= 2:
                a1.append((agg, nodes, allowed_nodes))
                flag(agg, "A1")
        if a1:
            a1.sort(key=lambda x: -x[0].busy_pct)
            findings.append({
                "id": "A1", "severity": "high",
                "title": "忙线程未绑核且跨 NUMA 漂移",
                "detail": "允许掩码覆盖多个 NUMA node 且活跃样本实际跨 node 运行；跨 node 迁移破坏缓存/TLB 局部性",
                "threads": [a.tid for a, _, _ in a1],
                "evidence": [{
                    "tid": a.tid, "comm": a.comm, "busy_pct": round(a.busy_pct, 1),
                    "allowed": a.allowed_masks[-1] if a.allowed_masks else "?",
                    "nodes_touched": sorted(nodes),
                    "migrations_lower_bound": a.migrations,
                } for a, nodes, _ in a1[:20]],
            })
        if a1u:
            a1u.sort(key=lambda x: -x[0].busy_pct)
            findings.append({
                "id": "A1u", "severity": "info",
                "title": "忙线程跨 NUMA 运行（掩码不可读，绑核状态未知）",
                "detail": "活跃样本跨多个 node，但允许掩码读取失败（'?'）——无法判定是否未绑核，仅报告观测事实",
                "threads": [a.tid for a, _ in a1u],
                "evidence": [{
                    "tid": a.tid, "comm": a.comm, "busy_pct": round(a.busy_pct, 1),
                    "allowed": "?",
                    "nodes_touched": sorted(nodes),
                    "migrations_lower_bound": a.migrations,
                } for a, nodes in a1u[:20]],
            })

    # A2 NPU 主机线程与设备 NUMA 错位
    # 主要落点按 NUMA 节点汇总活跃样本后取主要节点（防止"节点内两核
    # 各 30 样本 vs 对端节点单核 40 样本"式的误判）；忙阈值过滤见
    # SKILL.md 判定要点。
    if have_node and npu_nodes:
        a2 = []
        for agg in threads.values():
            role, _ = roles.match(agg.comm, agg.tid == pid)
            if role not in roles.npu_host_roles or agg.busy_pct < busy_threshold:
                continue
            if not agg.psr_hist:
                continue
            per_node = {}
            for psr, n in agg.psr_hist.items():
                nd = topo.cpu_node.get(psr)
                if nd is not None:
                    per_node[nd] = per_node.get(nd, 0) + n
            if not per_node:
                continue
            dom_node = max(per_node, key=lambda k: per_node[k])
            if dom_node in npu_nodes:
                continue
            # 代表 psr：主要节点内计数最大的核（展示用）
            dom_psr = max((psr for psr in agg.psr_hist
                           if topo.cpu_node.get(psr) == dom_node),
                          key=lambda c: agg.psr_hist[c])
            a2.append((agg, role, dom_psr, dom_node))
            flag(agg, "A2")
        if a2:
            findings.append({
                "id": "A2", "severity": "high",
                "title": "NPU 主机线程与设备 NUMA 错位",
                "detail": "算子下发/拷贝/通信角色的主要落点 node（按 NUMA 节点汇总活跃样本取主）"
                          "不在任一 NPU 设备所在 node；H2D/D2H 与控制通路将跨 NUMA",
                "threads": [a.tid for a, _, _, _ in a2],
                "evidence": [{
                    "tid": a.tid, "comm": a.comm, "role": role,
                    "dominant_psr": psr, "dominant_node": node,
                    "npu_nodes": npu_nodes,
                } for a, role, psr, node in a2[:20]],
            })

    # A3 忙线程挤核：按"该 tick 该核上达到忙阈值的活跃实例数 ≥3"判定，
    # 只标记参与挤核且达到忙阈值的实例（分时共享核的三个 25% 线程
    # 不应计入）
    if data_ticks:
        crowded_ticks = 0
        crowding = {}
        a3_flagged = set()
        for seq, act in per_tick_active.items():
            hit = False
            for psr, keys in act.items():
                busy_here = [k for k in keys
                             if threads[k].busy_pct >= busy_threshold]
                if len(busy_here) >= 3:
                    hit = True
                    crowding.setdefault(psr, []).append(seq)
                    a3_flagged.update(busy_here)
            if hit:
                crowded_ticks += 1
        ratio = crowded_ticks / float(data_ticks)
        if ratio >= 0.05:
            for k in a3_flagged:
                threads[k].flags.append("A3")
            top = sorted(crowding.items(),
                         key=lambda kv: -len(kv[1]))[:8]
            findings.append({
                "id": "A3", "severity": "medium",
                "title": "忙线程挤核（同 tick ≥3 个忙实例报同一核）",
                "detail": "同 tick 多个达到忙阈值的线程实例最后运行核相同 = 时间分片共享核的证据；"
                          "psr 快照口径，实际争抢只会更重",
                "threads": [],
                "evidence": {
                    "crowded_tick_ratio": round(ratio, 3),
                    "crowded_ticks": crowded_ticks,
                    "total_data_ticks": data_ticks,
                    "busy_threshold_pct": busy_threshold,
                    "top_crowded_cpus": [{"cpu": c, "ticks": len(seqs)}
                                          for c, seqs in top],
                },
            })

    # A4 掩码震荡
    a4 = [a for a in threads.values() if a.mask_changes >= 3 and a.active_samples >= 1]
    for a in a4:
        flag(a, "A4")
    if a4:
        findings.append({
            "id": "A4", "severity": "medium",
            "title": "允许掩码震荡（OMP 动态绑核特征）",
            "detail": "单线程掩码在采样期内反复变化；若非应用主动调用 sched_setaffinity，常见于 OMP_PROC_BIND=spread/close 的并行区动态绑核",
            "threads": [a.tid for a in a4],
            "evidence": [{
                "tid": a.tid, "comm": a.comm, "mask_changes": a.mask_changes,
                "mask_timeline": a.allowed_masks[:12],
            } for a in a4[:20]],
        })

    # A5 绑核意图未覆盖（库线程掩码宽于主线程）
    # 在两者**同时在场的 tick** 比较：线程先于主线程收紧退出时，拿各自
    # 最终掩码比较会误报（线程的"最终"早于主线程的"最终"）
    if main_agg and main_agg.allowed_masks:
        a5 = []
        for agg in threads.values():
            if agg.key == main_key or not agg.allowed_masks:
                continue
            # 共同在场区间末端（取两者在场区间的交集上界）
            t_seq = min(agg.last_seq, main_agg.last_seq)
            if t_seq < max(agg.first_seq, main_agg.first_seq):
                continue   # 无共同在场区间
            main_set = parse_cpulist(main_agg.mask_at(t_seq))
            s = parse_cpulist(agg.mask_at(t_seq))
            if main_set and s and main_set < s:
                a5.append(agg)
                flag(agg, "A5")
        if a5:
            findings.append({
                "id": "A5", "severity": "medium",
                "title": "绑核意图未覆盖全部线程（库线程掩码宽于主线程）",
                "detail": "两者同时在场的区间内，主线程掩码已收紧而部分线程掩码更宽——"
                          "典型原因是库线程（HCCL/CANN/OMP）自行重置亲和性，绕过了启动绑核",
                "threads": [a.tid for a in a5],
                "evidence": {
                    "main_allowed": main_agg.allowed_masks[-1],
                    "compare_at_seq": "共同在场区间末端",
                    "threads": [{"tid": a.tid, "comm": a.comm,
                                 "allowed": a.allowed_masks[-1]} for a in a5[:20]],
                },
            })

    # A6 观测完整性（psr ∉ 允许掩码）
    checked = sum(a.checked for a in threads.values())
    viol = sum(a.violations for a in threads.values())
    ratio = (viol / float(checked)) if checked else 0.0
    if viol > 0:
        sev = "high" if ratio > 0.05 else "info"
        findings.append({
            "id": "A6", "severity": sev,
            "title": "观测完整性：psr 越出允许掩码的样本",
            "detail": "同一 tick 内 psr（stat）与掩码（status）分读存在时间窗，"
                      "掩码恰在两读之间被收紧（绑定/解绑瞬间）会产生极少量假阳性"
                      "（预期 <0.5%）；比例显著偏高时提示采样异常",
            "threads": [],
            "evidence": {"violations": viol, "checked": checked, "ratio": round(ratio, 5)},
        })

    # A7 线程翻动（实例数 vs 峰值并发）
    churn_ratio = instances / float(max_conc) if max_conc else 0.0
    if max_conc and churn_ratio >= 3.0:
        findings.append({
            "id": "A7", "severity": "info",
            "title": "线程翻动偏高（实例数 ≥3× 峰值并发）",
            "detail": "短命线程反复创建销毁（thread-per-task 反模式或池配置问题）；部分短于采样间隔的线程不可见，实际翻动只会更高",
            "threads": [],
            "evidence": {"thread_instances": instances,
                          "max_concurrent": max_conc,
                          "churn_ratio": round(churn_ratio, 2)},
        })

    findings.sort(key=lambda f: (SEV_ORDER.get(f["severity"], 9), f["id"]))
    return findings, ratio, checked, viol


def compute_max_concurrent(ticks):
    mc = 0
    for t in ticks:
        if "absent_proc" not in t:
            mc = max(mc, len(t.get("threads", [])))
    return mc


# ------------------------------------------------------------------ HTML 报告

CSS = """
body{font-family:'Noto Sans CJK SC','Microsoft YaHei',sans-serif;margin:0;background:#f4f6f8;color:#24292f}
.wrap{max-width:1200px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;border-left:4px solid #0969da;padding-left:8px;margin-top:28px}
.meta{color:#57606a;font-size:13px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px 20px;min-width:120px}
.card .num{font-size:24px;font-weight:700}
.card .lbl{font-size:12px;color:#57606a}
.badge{display:inline-block;border-radius:10px;padding:1px 9px;font-size:12px;color:#fff}
.sev-high{background:#c0392b}.sev-medium{background:#d35400}.sev-info{background:#7f8c8d}.sev-ok{background:#2da44e}
.role-unmatched{background:#f0ad4e;color:#5c3c00;border:1px dashed #b58900}
table{border-collapse:collapse;background:#fff;width:100%;margin-top:8px;font-size:13px}
th,td{border:1px solid #d0d7de;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa;white-space:nowrap}
tr.hidden{display:none}
.finding{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin-top:10px}
.finding h3{margin:0 0 4px;font-size:14px}
.mono{font-family:ui-monospace,Consolas,monospace}
.note{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:10px 14px;font-size:12.5px;margin-top:12px}
.filter{margin:6px 0;font-size:13px}
code{background:#f6f8fa;padding:1px 4px;border-radius:4px}
"""


def esc(x):
    return html.escape(str(x), quote=True)


def render_html(meta, end, ticks, topo, threads, per_tick_active, findings,
                busy_threshold, dq, out_path, roles):
    pid = meta.get("pid")
    total_ins = len(threads)
    busy_cnt = sum(1 for a in threads.values() if a.busy_pct >= busy_threshold)
    max_conc = compute_max_concurrent(ticks)
    data_ticks = sum(1 for t in ticks if "absent_proc" not in t)
    absent_ticks = len(ticks) - data_ticks
    hi = sum(1 for f in findings if f["severity"] == "high")
    md = sum(1 for f in findings if f["severity"] == "medium")
    inf = sum(1 for f in findings if f["severity"] == "info")

    def node_desc(agg):
        if not agg.psr_hist:
            return "—"
        per_node = {}
        for psr, n in agg.psr_hist.items():
            nd = topo.cpu_node.get(psr)
            per_node.setdefault(nd if nd is not None else "?", []).append((psr, n))
        parts = []
        for nd in sorted(per_node, key=str):
            cps = sorted(per_node[nd], key=lambda x: -x[1])[:3]
            total = sum(n for _, n in per_node[nd])
            share = total * 100.0 / sum(agg.psr_hist.values())
            cpus_s = ",".join(str(c) for c, _ in cps)
            parts.append("node%s:%s%%(%s)" % (nd, pct(share, 0), cpus_s))
        return "<br>".join(esc(p) for p in parts)

    def role_of(agg):
        role, label = roles.match(agg.comm, agg.tid == pid)
        if role is None:
            return '<span class="badge role-unmatched">%s</span>' % esc(label)
        if role == "__unnamed__":
            return '<span class="badge role-unmatched">%s</span>' % esc(label)
        return esc(label)

    cards = [
        ("数据 tick", data_ticks), ("线程实例", total_ins),
        ("峰值并发", max_conc), ("忙线程", busy_cnt),
        ("NUMA 节点", len(topo.nodes)), ("NPU 设备", len(topo.npu)),
        ("异常发现", len(findings)),
    ]
    cards_html = "".join(
        '<div class="card"><div class="num">%s</div><div class="lbl">%s</div></div>'
        % (esc(v), esc(k)) for k, v in cards)

    findings_html = ""
    if findings:
        for f in findings:
            ev = f["evidence"]
            if isinstance(ev, list):
                ev_rows = "".join(
                    "<tr>%s</tr>" % "".join("<td>%s</td>" % esc(v) for v in row.values())
                    for row in ev)
                ev_html = "<table><tr>%s</tr>%s</table>" % (
                    "".join("<th>%s</th>" % esc(k) for k in ev[0].keys()), ev_rows)
            elif isinstance(ev, dict):
                # dict 分支：值为 list（如 A3 top_crowded_cpus / A5 threads 的
                # list-of-dicts）时复用表格渲染，标量保持单行——避免渲染成
                # Python repr 不可读
                def _cell(v):
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return ("<table><tr>%s</tr>%s</table>" % (
                            "".join("<th>%s</th>" % esc(k) for k in v[0].keys()),
                            "".join("<tr>%s</tr>" % "".join(
                                "<td>%s</td>" % esc(x) for x in row.values())
                                for row in v)))
                    return esc(v)
                ev_html = "<table>" + "".join(
                    "<tr><th>%s</th><td class=\"mono\">%s</td></tr>"
                    % (esc(k), _cell(v)) for k, v in ev.items()) + "</table>"
            else:
                ev_html = "<code>%s</code>" % esc(ev)
            findings_html += (
                '<div class="finding"><h3><span class="badge sev-%s">%s</span> '
                '%s · %s</h3><p>%s</p>%s</div>'
                % (f["severity"], f["severity"].upper(), f["id"], esc(f["title"]),
                   esc(f.get("detail", "")), ev_html))
    else:
        findings_html = '<p><span class="badge sev-ok">未发现异常模式</span></p>'

    rows = []
    for agg in sorted(threads.values(),
                      key=lambda a: (-a.busy_pct, a.tid, a.epoch)):
        masks = agg.allowed_masks
        allowed_cell = esc(masks[-1] if masks else "?")
        if len(masks) > 1:
            allowed_cell += '<br><span class="meta">变更 %d 次</span>' % agg.mask_changes
        th_flags = " ".join('<span class="badge sev-medium">%s</span>' % fl
                            for fl in agg.flags)
        if any(fl in ("A1", "A2") for fl in agg.flags):
            sev = '<span class="badge sev-high">high</span>'
        elif agg.flags:
            sev = '<span class="badge sev-medium">标记</span>'
        else:
            sev = '<span class="badge sev-ok">ok</span>'
        rows.append(
            '<tr data-anom="%d">'
            "<td>%s</td><td>%s</td><td>%s</td><td>%s%%</td>"
            "<td class=\"mono\">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (1 if getattr(agg, "flags", []) else 0,
               esc("tid%d%s" % (agg.tid, "·e%d" % agg.epoch if agg.epoch else "")),
               role_of(agg), esc(agg.comm), pct(agg.busy_pct),
               allowed_cell, node_desc(agg), agg.migrations, th_flags or sev))

    # 掩码时间线（只给有变更的线程）
    tl = [a for a in threads.values() if a.mask_changes > 0]
    tl.sort(key=lambda a: -a.mask_changes)
    tl_html = ""
    for a in tl[:20]:
        steps = " → ".join(esc(m) for m in a.allowed_masks[:16])
        tl_html += "<tr><td>%s</td><td>%s</td><td class=\"mono\">%s</td><td>%d</td></tr>" % (
            esc(a.tid), esc(a.comm), steps, a.mask_changes)

    npu_html = "".join(
        "<tr><td class=\"mono\">%s</td><td>%s</td></tr>" % (esc(d["bdf"]),
        esc(d["node"] if d["node"] is not None else "unknown"))
        for d in topo.npu) or "<tr><td colspan=2>（未发现昇腾 PCI 设备）</td></tr>"

    duration = ""
    if end:
        duration = "，结束原因 %s" % esc(end.get("reason", "?"))

    doc = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>绑核分析报告 — pid {pid}</title><style>{css}</style></head><body><div class="wrap">
<h1>运行期线程绑核分析报告</h1>
<div class="meta">目标 pid={pid}（{comm}） · host={host} · kernel={kernel} · 拓扑来源={topo_source} · 采样 {data_ticks} tick / 缺席 {absent_ticks}{duration}</div>
<div class="cards">{cards}</div>
<h2>数据质量</h2>
<table>
<tr><th>项目</th><th>值</th></tr>
<tr><td>psr 越出允许掩码样本</td><td class="mono">{viol} / {checked}（{vratio}；psr 与掩码两读间的掩码收紧竞态预期 &lt;0.5%）</td></tr>
<tr><td>线程翻动（实例/峰值并发）</td><td class="mono">{ins} / {maxc}</td></tr>
<tr><td>解析失败的行</td><td class="mono">{malformed}</td></tr>
<tr><td>忙线程阈值</td><td class="mono">{busy}%</td></tr>
{topo_gap_row}
</table>
<h2>异常发现（{n}）</h2>
{findings}
<h2>线程画像</h2>
<div class="filter"><label><input type="checkbox" id="f" onchange="filter()"> 仅看异常线程</label></div>
<table id="tbl">
<tr><th>线程</th><th>角色</th><th>comm</th><th>忙%</th><th>允许核</th><th>实际落点（活跃样本）</th><th>迁移*</th><th>标记</th></tr>
{rows}
</table>
{tl_section}
<h2>NPU 设备与 NUMA 归属</h2>
<table><tr><th>PCI BDF</th><th>NUMA node</th></tr>{npu}</table>
<h2>口径说明</h2>
<div class="note">
* 迁移为<b>下界</b>估计：psr 是"最后运行所在核"快照，两次采样之间的多次迁移不可见。<br>
活跃样本 = 相邻在场 tick 的 utime+stime 增量/时窗 ≥20%；A3 挤核按"同 tick 同核 ≥3 个**忙实例**"判定。<br>
允许掩码（Cpus_allowed_list）已含 cgroup cpuset 约束，容器视角即容器边界。<br>
生存期短于一个采样间隔的线程采不到（见线程翻动）。<br>
角色标注规则来自 {roles_source}（新增框架线程名 = 加配置不改代码）；未匹配线程按观测呈现，不猜角色。<br>
NPU 设备识别为 PCI vendor 0x19e5 + class 0x120000 双校验（collector 采样机快照已过滤，本机补采同样过滤）；多 NPU 分布多 node 时，A2 只标记主要落点不在任一设备 node 的线程。
</div>
<script>
function filter(){{
  var on=document.getElementById('f').checked;
  var rows=document.getElementById('tbl').getElementsByTagName('tr');
  for(var i=1;i<rows.length;i++){{
    rows[i].className = on && rows[i].getAttribute('data-anom')==='0' ? 'hidden':'';
  }}
}}
</script>
</div></body></html>
"""
    tl_section = ""
    if tl_html:
        tl_section = ("<h2>掩码时间线（变更过的线程，前 20）</h2><table>"
                      "<tr><th>tid</th><th>comm</th><th>掩码序列</th><th>变更次数</th></tr>"
                      + tl_html + "</table>")

    topo_gap_row = ""
    if dq.get("topology_gap"):
        topo_gap_row = ('<tr><td>拓扑缺口</td><td class="mono">%s</td></tr>'
                        % esc(dq["topology_gap"]))

    doc = doc.format(
        pid=esc(pid), comm=esc(meta.get("target_comm", "?")), host=esc(topo.host),
        kernel=esc(topo.kernel), topo_source=esc(topo.source),
        data_ticks=data_ticks, absent_ticks=absent_ticks,
        duration=duration, cards=cards_html, viol=dq["viol"], checked=dq["checked"],
        vratio=pct(dq["ratio"] * 100, 3) + "%", ins=total_ins, maxc=max_conc,
        malformed=dq["malformed"], busy=busy_threshold, n=len(findings),
        findings=findings_html, rows="".join(rows), tl_section=tl_section,
        npu=npu_html, roles_source=esc(roles.source),
        topo_gap_row=topo_gap_row, css=CSS)

    _atomic_write(out_path, doc)


# ------------------------------------------------------------------ main

class _ArgParser(argparse.ArgumentParser):
    """用法错误统一退 1（argparse 默认退 2 与「2=samples.jsonl 无法解析」撞码）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        print("ERROR: 参数错误：%s（--help 查看用法）" % message, file=sys.stderr)
        sys.exit(1)


def _atomic_write(path, content):
    """临时文件 + os.replace 原子写入。

    rename(2) 不跟随符号链接——目标位置预置 symlink 时替换的是链接
    本身而非其指向的采样数据；半写中断也不会留下截断的报告。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def main(argv=None):
    ap = _ArgParser(
        description="kptools-thread-affinity-analyzer 报告生成（时间序列 → HTML + summary.json）")
    ap.add_argument("sample_dir",
                    help="kptools-thread-affinity-collector 的输出目录")
    ap.add_argument("--busy", type=float, default=50.0,
                    help="忙线程判定阈值（CPU%%，默认 50）")
    ap.add_argument("--out", default=None, help="报告输出目录（默认采样目录）")
    ap.add_argument("--topology-raw", default=None,
                    help="覆盖 topology_raw.txt 路径（测试用）")
    ap.add_argument("--roles", default=None,
                    help="覆盖线程角色配置 roles.json（默认 <skill>/references/roles.json）")
    ap.add_argument("--allow-sys-fallback", action="store_true",
                    help="显式允许 topology_raw.txt 缺失时用本机 /sys 补采拓扑"
                         "（仅同机分析场景；报告会标注拓扑来源与告警）")
    args = ap.parse_args(argv)

    # 角色配置：默认 skill 目录下 references/roles.json，--roles 可覆盖；
    # 缺失/损坏/结构非法 → stderr 警告 + 内置最小回退集（safe-fail，不静默）
    default_roles = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        os.pardir, "references", "roles.json"))
    roles, roles_source = RoleConfig.load(args.roles or default_roles)

    sample_dir = args.sample_dir
    samples = os.path.join(sample_dir, "samples.jsonl")
    if not os.path.isdir(sample_dir):
        print("ERROR: 目录不存在 %s" % sample_dir, file=sys.stderr)
        return 1
    meta, ticks, end, malformed = load_samples(samples)
    if meta is None:
        print("ERROR: samples.jsonl 无法读取或缺少 meta 行：%s" % samples, file=sys.stderr)
        return 2
    data_ticks = [t for t in ticks if "absent_proc" not in t]
    if not data_ticks:
        print("ERROR: samples.jsonl 无有效数据 tick", file=sys.stderr)
        return 2

    raw_path = args.topology_raw or os.path.join(sample_dir, "topology_raw.txt")
    topo = Topology.from_raw(raw_path)
    topo_gap = None     # 拓扑缺口披露（报告数据质量区 + summary）
    if topo is None:
        # 默认不再静默用分析机 /sys 解释采样机数据（会伪造 NUMA/NPU
        # 归属结论）——跨机包缺 topology_raw.txt 一律退 3
        if not args.allow_sys_fallback:
            print("ERROR: topology_raw.txt 缺失（%s）——跨机采样包必须带上采样机拓扑；"
                  "确认同机分析可加 --allow-sys-fallback 用本机 /sys 补采（报告将标注来源）"
                  % raw_path, file=sys.stderr)
            return 3
        topo = Topology.from_sys()
        topo_gap = ("topology_raw.txt 缺失，已用**本机** /sys 补采拓扑"
                    "（--allow-sys-fallback）——NUMA/NPU 归属解释的是分析机而非采样机")
        print("WARN: %s" % topo_gap, file=sys.stderr)
    if not topo.resolve():
        topo_gap = topo_gap or "topology_raw.txt 无有效 CPU→NUMA 映射（node_cpulist 节缺失或不可解析）——A1/A2/A1u 判定跳过"
        print("WARN: %s" % topo_gap, file=sys.stderr)

    threads, per_tick_active = aggregate(ticks, meta, topo)
    # 统计掩码变更数（aggregate 只记了序列，变更数 = 序列长度-1）
    for agg in threads.values():
        agg.mask_changes = max(0, len(agg.allowed_masks) - 1)

    max_conc = compute_max_concurrent(data_ticks)
    findings, ratio, checked, viol = detect_findings(
        threads, per_tick_active, topo, meta, args.busy, max_conc, roles)

    out_dir = args.out or sample_dir
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, "绑核分析报告.html")
    dq = {"viol": viol, "checked": checked, "ratio": ratio, "malformed": malformed,
          "topology_gap": topo_gap}
    render_html(meta, end, ticks, topo, threads, per_tick_active,
                findings, args.busy, dq, html_path, roles)

    pid = meta.get("pid")
    profiles = []
    for agg in sorted(threads.values(), key=lambda a: (-a.busy_pct, a.tid, a.epoch)):
        role, label = roles.match(agg.comm, agg.tid == pid)
        profiles.append({
            "tid": agg.tid, "epoch": agg.epoch, "comm": agg.comm, "role": role,
            "busy_pct": round(agg.busy_pct, 1),
            "allowed_masks": agg.allowed_masks, "mask_changes": agg.mask_changes,
            "active_psr_top": sorted(agg.psr_hist.items(),
                                     key=lambda kv: -kv[1])[:8],
            "nodes_touched": sorted({topo.cpu_node[c] for c in agg.psr_hist
                                     if c in topo.cpu_node}),
            "migrations_lower_bound": agg.migrations,
            "lifetime_seq": [agg.first_seq, agg.last_seq],
            "flags": getattr(agg, "flags", []),
        })
    summary = {
        "skill": "kptools-thread-affinity-analyzer",
        "version": VERSION,
        "role_config": {"source": roles.source,
                        "rules": len(roles.rules),
                        "note": "rules 为 0 时使用内置回退集（见 stderr 警告）"},
        "target": {
            "pid": pid, "comm": meta.get("target_comm"),
            "interval_s": meta.get("interval"),
            "started": meta.get("started"),
            "end_reason": (end or {}).get("reason"),
            "ticks_total": len(ticks),
        },
        "environment": {
            "host": topo.host, "kernel": topo.kernel, "clk_tck": topo.clk_tck,
            "topology_source": topo.source,
            "numa_nodes": topo.nodes,
            "l3_domains": [format_cpulist(d) for d in topo.l3_domains],
            "npu_devices": topo.npu, "npu_nodes": topo.npu_nodes,
            "target_cpuset": topo.target_cpuset,
            "thread_env_vars": topo.env_vars,
        },
        "threads": {
            "instances": len(threads),
            "max_concurrent": max_conc,
            "busy_threads": sum(1 for a in threads.values()
                                if a.busy_pct >= args.busy),
        },
        "findings": findings,
        "data_quality": {
            "data_ticks": len(data_ticks),
            "absent_ticks": len(ticks) - len(data_ticks),
            "malformed_lines": malformed,
            "psr_outside_allowed": {"violations": viol, "checked": checked,
                                    "ratio": round(ratio, 5)},
            "numa_mapping": bool(topo.resolve()),
            "topology_gap": topo_gap,
        },
        "profiles": profiles,
        "report": os.path.abspath(html_path),
    }
    summary_path = os.path.join(out_dir, "summary.json")
    _atomic_write(summary_path, jdump(summary))

    sev_cnt = {}
    for f in findings:
        sev_cnt[f["severity"]] = sev_cnt.get(f["severity"], 0) + 1
    sev_s = ", ".join("%s %d" % (k, sev_cnt[k]) for k in sorted(sev_cnt)) or "无"
    print("线程实例 %d / 忙线程 %d / 异常 %d（%s）" % (
        len(threads), summary["threads"]["busy_threads"], len(findings), sev_s))
    if topo_gap:
        print("注意: %s" % topo_gap)
    print("报告: %s" % os.path.abspath(html_path))
    print("summary: %s" % os.path.abspath(summary_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
