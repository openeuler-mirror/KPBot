#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llvm-mca 瓶颈分析器（鲲鹏 HiSilicon 微架构）

对 ARM64 指令序列做流水线级静态仿真，输出 IPC、Block RThroughput、逐资源端口压力、
bottleneck 归因（资源压力 vs 数据依赖）和关键依赖序列。

支持微架构（用户代号 -> LLVM -mcpu）：
  hip08 / 0xd01 / tsv110  -> tsv110
  hip09 / 0xd02           -> hip09
  hip10 / 0xd03           -> hip10c   (注意 LLVM 名带 c)
  hip11 / 0xd22           -> hip11
  hip12 / 0xd06           -> hip12

输入模式：
  --asm <file|->          纯汇编文本或 .s 文件（'-' 读 stdin）
  --source <file> --function <name>   C/C++ 源码，clang -S 编译后提取函数体
  --binary <file> --function <name>   二进制/目标文件，llvm-objdump -d 反汇编后提取函数

输出：JSON 契约（含可读摘要 summary_text）。--text 仅打印可读摘要。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# 用户代号 -> LLVM -mcpu。多别名映射到同一调度模型。
UARCH_MAP = {
    "hip08": "tsv110", "0xd01": "tsv110", "tsv110": "tsv110",
    "hip09": "hip09", "0xd02": "hip09",
    "hip10": "hip10c", "0xd03": "hip10c", "hip10c": "hip10c",
    "hip11": "hip11", "0xd22": "hip11",
    "hip12": "hip12", "0xd06": "hip12",
}
# LLVM -mcpu -> 优先展示的用户代号（用于报告里的 uarch 字段）
MCPU_TO_UARCH = {
    "tsv110": "hip08",
    "hip09": "hip09",
    "hip10c": "hip10",
    "hip11": "hip11",
    "hip12": "hip12",
}

MTRIPLE = "aarch64-linux-gnu"


def sanitize_resource_name(name):
    """LLVM JSON 把分组资源的子单元编码为末尾控制字符（如 "TSV110UnitAB.\\x00"），
    清洗成可读形式 "TSV110UnitAB.0"。"""
    return re.sub(r'[\x00-\x1f]', lambda m: str(ord(m.group())), name or "")


def die(msg):
    """构造失败契约并退出。"""
    out = {"llvm_mca_analysis_result": {"success": False, "error_message": str(msg)}}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(1)


def resolve_mcpu(uarch):
    key = (uarch or "").strip().lower()
    if key not in UARCH_MAP:
        die("不支持的微架构代号 '%s'。支持: %s" % (
            uarch, ", ".join(sorted(set(UARCH_MAP.keys())))))
    return UARCH_MAP[key]


def run_cmd(cmd, input_text=None):
    """运行命令，返回 (returncode, stdout, stderr)。"""
    try:
        p = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        die("缺少可执行文件: %s（命令: %s）" % (e, " ".join(cmd)))
    except subprocess.TimeoutExpired:
        die("命令超时: %s" % " ".join(cmd))


# --------------------------------------------------------------------------- #
# 环境与模型支持预检
# --------------------------------------------------------------------------- #
# hip09/hip10c/hip11/hip12 是 HiSilicon 贡献的调度模型，仅在特定 LLVM 构建
# （如 openEuler 的 llvm-toolset）中可用；tsv110(hip08) 是上游模型。
# 在不支持这些模型的 LLVM 上，llvm-mca 遇到未识别的 -mcpu 会静默回退到 generic
# 模型（不报错退出），产出无意义的 IPC。所以必须在运行前主动探测。

# 全部支持的 uarch 代号 -> LLVM -mcpu（用于 check-env / 失败时列出可用项）
ALL_UARCHES = [("hip08", "tsv110"), ("hip09", "hip09"),
               ("hip10", "hip10c"), ("hip11", "hip11"), ("hip12", "hip12")]


def probe_mcpu_support(mcpu):
    """探测 llvm-mca 是否真支持 -mcpu=<mcpu>。

    未识别的 CPU 名 llvm-mca 不会报错退出，而是打印
    "'<name>' is not a recognized processor ... (ignoring processor)" 后回退到
    generic 模型。因此靠 stderr/stdout 文本判定，而非返回码。
    返回 (supported: bool, message: str)。
    """
    asm = "mov x0, x0\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".s", delete=False) as tf:
        tf.write(asm)
        path = tf.name
    try:
        rc, out, err = run_cmd(
            ["llvm-mca", "-mtriple=%s" % MTRIPLE, "-mcpu=%s" % mcpu,
             "--iterations=1", path])
        combined = ((err or "") + (out or "")).lower()
        if "not a recognized processor" in combined or "no scheduling model" in combined:
            return False, (err or out or "").strip()
        if rc != 0:
            return False, (err or out or "").strip()
        return True, ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def list_supported_uarches():
    """探测全部 5 个 uarch 模型在当前系统的支持情况。返回 [(uarch, mcpu, supported)]。"""
    return [(u, m, probe_mcpu_support(m)[0]) for u, m in ALL_UARCHES]


def preflight(mcpu, uarch):
    """运行前预检：llvm-mca 是否安装、是否支持目标微架构。失败则 die 给出清晰说明。"""
    if not shutil.which("llvm-mca"):
        die("未找到 llvm-mca 可执行文件。请安装 LLVM（须含 llvm-mca 工具）。")
    ok, msg = probe_mcpu_support(mcpu)
    if ok:
        return
    supported = ["%s(%s)" % (u, m) for u, m, ok2 in list_supported_uarches() if ok2]
    die(
        "当前系统的 llvm-mca 不支持微架构 %s（-mcpu=%s）。\n"
        "这通常意味着你使用的是非 openEuler / 标准 LLVM 构建，缺少 HiSilicon hip 调度模型：\n"
        "  - hip09 / hip10c / hip11 / hip12 是 HiSilicon 贡献的调度模型，仅在特定 LLVM 构建"
        "（如 openEuler 的 llvm-toolset）中可用；\n"
        "  - tsv110（hip08）是上游模型，标准 LLVM 通常也支持。\n"
        "当前系统支持的型号: %s\n"
        "请安装支持 hip 模型的 LLVM（如 openEuler llvm-toolset），或改用上述已支持的微架构。\n"
        "原始探测输出: %s" % (
            uarch, mcpu,
            ", ".join(supported) if supported
            else "（无 -- 连 tsv110 都不支持，请确认 llvm-mca 支持 aarch64 目标）",
            msg or "(无)"))


def check_env():
    """显式环境检查：报告 llvm-mca / clang / llvm-objdump 及 5 个 uarch 模型的支持情况。"""
    result = {
        "success": True,
        "env_check": True,
        "tools": {
            "llvm-mca": bool(shutil.which("llvm-mca")),
            "clang": bool(shutil.which("clang")),
            "llvm-objdump": bool(shutil.which("llvm-objdump")),
        },
        "uarch_support": [
            {"uarch": u, "mcpu": m, "supported": ok}
            for u, m, ok in list_supported_uarches()],
        "note": "hip09/hip10c/hip11/hip12 仅在含 HiSilicon 调度模型的 LLVM 构建（如 openEuler "
                "llvm-toolset）中可用；tsv110(hip08) 为上游模型。",
        "error_message": "",
    }
    print(json.dumps({"llvm_mca_analysis_result": result},
                     ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# 汇编获取
# --------------------------------------------------------------------------- #

def read_asm_input(path):
    """--asm 模式：读文件或 stdin。"""
    if path == "-":
        return sys.stdin.read(), "stdin"
    if not os.path.isfile(path):
        die("--asm 指定的文件不存在: %s" % path)
    with open(path, "r", errors="replace") as f:
        return f.read(), path


def compile_source_to_asm(source, mcpu, extra_cflags):
    """clang -S 编译 C/C++ 源码为汇编。返回 (asm_text, ok, err)。"""
    if not shutil.which("clang"):
        die("未找到 clang。--source 模式需要 clang 编译源码。")
    if not os.path.isfile(source):
        die("--source 指定的文件不存在: %s" % source)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".s", delete=False)
    tmp.close()
    cmd = ["clang", "-S", "--target=%s" % MTRIPLE, "-mcpu=%s" % mcpu,
           "-O2", "-o", tmp.name]
    if extra_cflags:
        cmd += extra_cflags
    cmd.append(source)
    rc, out, err = run_cmd(cmd)
    if rc != 0:
        os.unlink(tmp.name)
        return "", False, err or out
    with open(tmp.name, "r", errors="replace") as f:
        asm = f.read()
    os.unlink(tmp.name)
    return asm, True, ""


def disasm_binary(binary, mcpu):
    """llvm-objdump -d 反汇编二进制。返回 (text, ok, err)。"""
    if not shutil.which("llvm-objdump"):
        die("未找到 llvm-objdump。--binary 模式需要它反汇编。")
    if not os.path.isfile(binary):
        die("--binary 指定的文件不存在: %s" % binary)
    cmd = ["llvm-objdump", "-d", "--no-show-raw-insn", binary]
    rc, out, err = run_cmd(cmd)
    if rc != 0:
        return "", False, err or out
    return out, True, ""


def extract_function_from_compiler_asm(asm_text, func):
    """从 clang 生成的 .s 中提取函数体。

    clang .s 形如：
        foo:
        .Ltmp0:
          .cfi_startproc
          <指令>
          ret
        .Ltmp1:
        .size foo, .Ltmp1-foo
          .cfi_endproc

    取 <func>: 到 .cfi_endproc（或 .size <func>）之间的内容。
    """
    # 找函数标签行：行首 func 名 + 冒号（行尾可能有注释，如 "dot:  // @dot"）
    label_re = re.compile(r"^\s*%s\s*:" % re.escape(func), re.MULTILINE)
    m = label_re.search(asm_text)
    if not m:
        return None, "在编译产物中找不到函数标签 '%s:'" % func
    start = m.end()
    # 找函数结束边界
    end_re = re.compile(r"^\s*\.cfi_endproc\b", re.MULTILINE)
    em = end_re.search(asm_text, start)
    end = em.start() if em else len(asm_text)
    body = asm_text[start:end]
    # 剥离对 MCA 无意义且可能引发警告的伪指令，保留指令与标签
    cleaned = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        # 纯注释行（AArch64 用 // 注释）
        if s.startswith("//"):
            continue
        if s.startswith((".cfi_", ".loc", ".file", ".p2align", ".balign",
                         ".align", ".type", ".size", ".Ltmp", ".Lfunc",
                         ".LBB", ".subsection", ".variant_pcs", ".gnu_inline",
                         ".note", ".text", ".globl", ".weak")):
            continue
        cleaned.append(line)
    if not cleaned:
        return None, "函数 '%s' 提取后无有效指令" % func
    return "\n".join(cleaned), ""


def extract_function_from_objdump(disasm_text, func):
    """从 llvm-objdump -d 输出中提取函数指令。

    形如：
        00000000 <foo>:
               0: d2800000     mov     x0, #0
               4: d65f03c0     ret
               8: ...
        <nextsym>:

    --no-show-raw-insn 已去掉 hex 字节列，但地址列仍在。
    """
    lines = disasm_text.splitlines()
    # 找函数头：<func>:  或  <addr> <func>:
    header_idx = None
    for i, line in enumerate(lines):
        if re.search(r"<%s>:" % re.escape(func), line):
            header_idx = i
            break
    if header_idx is None:
        return None, "在反汇编结果中找不到函数 '<%s>:'" % func
    instrs = []
    for line in lines[header_idx + 1:]:
        s = line.rstrip()
        if not s.strip():
            if instrs:
                break  # 函数体后的空行视为结束
            continue
        # 遇到下一个符号头 <name>: 结束
        if re.match(r"^\s*[0-9a-f]+\s+<.+>:\s*$", s):
            break
        # 指令行：形如 "       0:\t<mnemonic> <operands>"
        m = re.match(r"^\s*[0-9a-f]+:\s+(.*)$", s)
        if m:
            instr = m.group(1).strip()
            if instr:
                instrs.append(instr)
    if not instrs:
        return None, "函数 '%s' 反汇编后无有效指令" % func
    return "\n".join(instrs), ""


def get_assembly(args, mcpu):
    """根据输入模式获取干净的汇编文本。返回 (asm_text, input_info)。"""
    if args.asm:
        text, src = read_asm_input(args.asm)
        return text, {"mode": "asm", "source": src, "function": None}
    if args.source:
        asm, ok, err = compile_source_to_asm(args.source, mcpu, args.cflags)
        if not ok:
            die("源码编译失败:\n%s" % err)
        body, err = extract_function_from_compiler_asm(asm, args.function)
        if err:
            die(err)
        return body, {"mode": "source", "source": args.source, "function": args.function}
    if args.binary:
        text, ok, err = disasm_binary(args.binary, mcpu)
        if not ok:
            die("反汇编失败:\n%s" % err)
        body, err = extract_function_from_objdump(text, args.function)
        if err:
            die(err)
        return body, {"mode": "binary", "source": args.binary, "function": args.function}
    die("未指定输入：需提供 --asm / --source+--function / --binary+--function 之一")


# --------------------------------------------------------------------------- #
# 运行 MCA
# --------------------------------------------------------------------------- #

def run_mca(asm_text, mcpu, iterations, want_json):
    """运行 llvm-mca。want_json=True 用 --json 拿结构化数据，否则拿文本（含 bottleneck）。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".s", delete=False) as tf:
        tf.write(asm_text)
        asm_path = tf.name
    try:
        cmd = ["llvm-mca", "-mtriple=%s" % MTRIPLE, "-mcpu=%s" % mcpu,
               "--iterations=%d" % iterations, asm_path]
        if want_json:
            cmd += ["--json", "-all-stats"]
        else:
            cmd += ["-bottleneck-analysis"]
        rc, out, err = run_cmd(cmd)
        if rc != 0:
            return None, err or out, err
        return out, "", err
    finally:
        os.unlink(asm_path)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def parse_bottleneck_section(text):
    """从 MCA 文本输出解析 bottleneck-analysis 段。

    返回 dict：backend_pressure_cycles_pct, resource_pressure_pct,
    limiting_resources[], data_dependencies_pct, register/memory deps pct,
    critical_sequence[], raw_text。解析失败时各字段为空，raw_text 保留原文。
    """
    result = {
        "backend_pressure_cycles_pct": None,
        "resource_pressure_pct": None,
        "limiting_resources": [],
        "data_dependencies_pct": None,
        "register_dependencies_pct": None,
        "memory_dependencies_pct": None,
        "critical_sequence": [],
        "raw_text": "",
    }
    if not text:
        return result
    # 截取 bottleneck 段：从 "Cycles with backend pressure" 到 "Instruction Info:" 之前
    start = text.find("Cycles with backend pressure")
    end = text.find("Instruction Info:")
    if start == -1:
        # 没有瓶颈分析段（可能 iterations 太少或无瓶颈）
        return result
    section = text[start:end if end != -1 else len(text)]
    result["raw_text"] = section.strip()

    m = re.search(r"Cycles with backend pressure increase\s*\[\s*([\d.]+)%\s*\]", section)
    if m:
        result["backend_pressure_cycles_pct"] = float(m.group(1))

    m = re.search(r"Resource Pressure\s*\[\s*([\d.]+)%\s*\]", section)
    if m:
        result["resource_pressure_pct"] = float(m.group(1))
        # 紧随其后的 "  - <Resource>  [ X% ]"
        for rm in re.finditer(r"^\s*-\s+(\S+)\s*\[\s*([\d.]+)%\s*\]", section, re.MULTILINE):
            result["limiting_resources"].append(
                {"resource": rm.group(1), "pct": float(rm.group(2))})

    m = re.search(r"Data Dependencies:?\s*\[\s*([\d.]+)%\s*\]", section)
    if m:
        result["data_dependencies_pct"] = float(m.group(1))
        rm = re.search(r"Register Dependencies\s*\[\s*([\d.]+)%\s*\]", section)
        if rm:
            result["register_dependencies_pct"] = float(rm.group(1))
        mm = re.search(r"Memory Dependencies\s*\[\s*([\d.]+)%\s*\]", section)
        if mm:
            result["memory_dependencies_pct"] = float(mm.group(1))

    # 关键序列：+----> N.  <instr>   ## TYPE: detail
    for cm in re.finditer(
        r"^\s*\+---->\s+(\d+)\.\s+(.+?)\s*##\s*(.+?)\s*$", section, re.MULTILINE):
        idx = int(cm.group(1))
        instr = cm.group(2).strip()
        ann = cm.group(3).strip()
        if ann.startswith("RESOURCE interference"):
            dtype = "resource_interference"
        elif ann.startswith("REGISTER dependency"):
            dtype = "register"
        elif ann.startswith("MEMORY dependency"):
            dtype = "memory"
        else:
            dtype = "other"
        result["critical_sequence"].append(
            {"index": idx, "instruction": instr, "dependency_type": dtype, "detail": ann})
    return result


def classify_bottleneck(bn):
    """根据解析结果给出主要瓶颈类型。"""
    rp = bn.get("resource_pressure_pct")
    dd = bn.get("data_dependencies_pct")
    bp = bn.get("backend_pressure_cycles_pct")
    if bp is None and rp is None and dd is None:
        return "unknown"
    if (bp is not None and bp < 10.0):
        return "low_pressure"  # 后端压力小，瓶颈在前端/dispatch/retire 或已充分流水化
    rp = rp or 0.0
    dd = dd or 0.0
    if rp == 0 and dd == 0:
        return "low_pressure"
    if rp > dd * 1.5:
        return "resource_pressure"
    if dd > rp * 1.5:
        return "data_dependency"
    return "mixed"


def build_report(json_text, bn_text, mcpu, iterations, input_info, warnings):
    """合并 JSON 结构化数据与文本瓶颈解析，构造契约。"""
    uarch = MCPU_TO_UARCH.get(mcpu, mcpu)
    region = None
    if json_text:
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as e:
            die("MCA JSON 解析失败: %s" % e)
        regions = data.get("CodeRegions", [])
        if not regions:
            die("MCA 未返回任何 CodeRegion（输入汇编可能为空或无法解析）")
        region = regions[0]
        target = data.get("TargetInfo", {})
        resources = [sanitize_resource_name(r) for r in target.get("Resources", [])]
    else:
        die("MCA 未产出 JSON")

    sv = region.get("SummaryView", {})
    summary = {
        "iterations": sv.get("Iterations"),
        "instructions": sv.get("Instructions"),
        "total_cycles": sv.get("TotalCycles"),
        "total_uops": sv.get("TotaluOps"),
        "dispatch_width": sv.get("DispatchWidth"),
        "uops_per_cycle": round(sv.get("uOpsPerCycle", 0.0), 4),
        "ipc": round(sv.get("IPC", 0.0), 4),
        "block_rthroughput": round(float(sv.get("BlockRThroughput", 0.0)), 4),
        "cycles_per_iteration": round(
            sv.get("TotalCycles", 0) / sv.get("Iterations", 1), 4) if sv.get("Iterations") else None,
    }

    # 逐资源压力（聚合到每个资源）。MCA 的 per-iteration 汇总行 InstructionIndex
    # = 指令数（即所有条目里的最大 InstructionIndex），不是 -1。
    rpv = region.get("ResourcePressureView", {}).get("ResourcePressureInfo", [])
    per_resource = [0.0] * len(resources)
    total_idx = max((e.get("InstructionIndex", -1) for e in rpv), default=-2)
    for item in rpv:
        if item.get("InstructionIndex", -1) == total_idx:  # 仅汇总行
            ri = item.get("ResourceIndex", 0)
            if 0 <= ri < len(per_resource):
                per_resource[ri] += item.get("ResourceUsage", 0.0)
    resource_pressure_per_iteration = [
        {"resource": resources[i] if i < len(resources) else "R%d" % i,
         "pressure": round(per_resource[i], 4)}
        for i in range(len(resources))]

    # 逐指令信息
    iiv = region.get("InstructionInfoView", {}).get("InstructionList", [])
    instrs = region.get("Instructions", [])
    instructions_info = []
    for i, info in enumerate(iiv):
        instructions_info.append({
            "index": i,
            "instruction": instrs[i] if i < len(instrs) else "",
            "uops": info.get("NumMicroOpcodes"),
            "latency": info.get("Latency"),
            "rthroughput": round(float(info.get("RThroughput", 0.0)), 4),
            "may_load": bool(info.get("mayLoad", False)),
            "may_store": bool(info.get("mayStore", False)),
            "has_unmodeled_side_effects": bool(info.get("hasUnmodeledSideEffects", False)),
        })

    dispatch_stalls = region.get("DispatchStatistics", {})

    bn = parse_bottleneck_section(bn_text)
    primary_type = classify_bottleneck(bn)
    bottleneck = {
        "primary_type": primary_type,
        "backend_pressure_cycles_pct": bn["backend_pressure_cycles_pct"],
        "resource_pressure_pct": bn["resource_pressure_pct"],
        "data_dependencies_pct": bn["data_dependencies_pct"],
        "register_dependencies_pct": bn["register_dependencies_pct"],
        "memory_dependencies_pct": bn["memory_dependencies_pct"],
        "limiting_resources": bn["limiting_resources"],
        "critical_sequence": bn["critical_sequence"],
        "raw_text": bn["raw_text"],
    }

    result = {
        "success": True,
        "uarch": uarch,
        "mcpu": mcpu,
        "input": dict(input_info, instruction_count=len(instructions_info)),
        "iterations": iterations,
        "warnings": warnings,
        "summary": summary,
        "bottleneck": bottleneck,
        "dispatch_stalls": dispatch_stalls,
        "resource_pressure_per_iteration": resource_pressure_per_iteration,
        "instructions_info": instructions_info,
        "summary_text": "",
        "error_message": "",
    }
    result["summary_text"] = render_summary(result)
    return {"llvm_mca_analysis_result": result}


# --------------------------------------------------------------------------- #
# 可读摘要
# --------------------------------------------------------------------------- #

def render_summary(r):
    s = r["summary"]
    b = r["bottleneck"]
    lines = []
    lines.append("# LLVM MCA 瓶颈分析报告")
    lines.append("")
    lines.append("**目标微架构**: %s (%s) | **输入**: %s%s | **迭代**: %s" % (
        r["uarch"], r["mcpu"], r["input"]["mode"],
        ("，函数 `%s`" % r["input"]["function"]) if r["input"].get("function") else "",
        r["iterations"]))
    if r.get("warnings"):
        lines.append("**注意**: " + "; ".join(r["warnings"]))
    lines.append("")
    lines.append("## 汇总指标")
    lines.append("| 指标 | 值 |")
    lines.append("|------|-----|")
    lines.append("| IPC | %s |" % s["ipc"])
    lines.append("| Block RThroughput | %s |" % s["block_rthroughput"])
    lines.append("| 每迭代周期数 | %s |" % s["cycles_per_iteration"])
    lines.append("| Dispatch 宽度 | %s |" % s["dispatch_width"])
    lines.append("| uOps/周期 | %s |" % s["uops_per_cycle"])
    lines.append("| 总指令数(含迭代) | %s |" % s["instructions"])
    lines.append("")
    lines.append("## 瓶颈归因")
    type_cn = {
        "resource_pressure": "资源压力 (Resource Pressure)",
        "data_dependency": "数据依赖 (Data Dependency)",
        "mixed": "混合 (资源压力 + 数据依赖)",
        "low_pressure": "后端压力低 (前端/dispatch/retire 主导或已充分流水化)",
        "unknown": "未知",
    }.get(b["primary_type"], b["primary_type"])
    lines.append("**主要瓶颈类型**: %s" % type_cn)
    if b["backend_pressure_cycles_pct"] is not None:
        lines.append("- 后端压力周期占比: %.2f%%" % b["backend_pressure_cycles_pct"])
    if b["resource_pressure_pct"] is not None:
        lines.append("- 资源压力: %.2f%%" % b["resource_pressure_pct"])
        if b["limiting_resources"]:
            lim = "，".join("%s (%.2f%%)" % (x["resource"], x["pct"]) for x in b["limiting_resources"])
            lines.append("  - 饱和资源: %s" % lim)
    if b["data_dependencies_pct"] is not None:
        lines.append("- 数据依赖: %.2f%% (寄存器 %.2f%%，内存 %.2f%%)" % (
            b["data_dependencies_pct"],
            b["register_dependencies_pct"] or 0.0,
            b["memory_dependencies_pct"] or 0.0))
    if b["critical_sequence"]:
        lines.append("")
        lines.append("**关键序列**:")
        for c in b["critical_sequence"]:
            lines.append("  #%d `%s` — %s" % (c["index"], c["instruction"], c["detail"]))
    lines.append("")
    # 逐资源压力 Top
    rp = sorted(r["resource_pressure_per_iteration"],
                key=lambda x: x["pressure"], reverse=True)
    rp = [x for x in rp if x["pressure"] > 0]
    if rp:
        lines.append("## 逐资源端口压力（每迭代）")
        for x in rp[:8]:
            lines.append("- %s: %s" % (x["resource"], x["pressure"]))
        lines.append("")
    # 逐指令信息
    if r["instructions_info"]:
        lines.append("## 逐指令信息")
        lines.append("| # | 指令 | uOps | 延迟 | RThroughput | Load | Store |")
        lines.append("|---|------|------|------|-------------|------|-------|")
        for ii in r["instructions_info"]:
            lines.append("| %d | `%s` | %s | %s | %s | %s | %s |" % (
                ii["index"], ii["instruction"], ii["uops"], ii["latency"],
                ii["rthroughput"],
                "Y" if ii["may_load"] else "",
                "Y" if ii["may_store"] else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="llvm-mca 瓶颈分析器（鲲鹏 HiSilicon 微架构）")
    ap.add_argument("--check-env", action="store_true",
                   help="仅检查当前系统的 llvm-mca 是否支持各 hip 微架构，不做分析")
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--asm", metavar="FILE|-",
                   help="纯汇编文本或 .s 文件（'-' 读 stdin）")
    g.add_argument("--source", metavar="FILE",
                   help="C/C++ 源码文件（需配合 --function）")
    g.add_argument("--binary", metavar="FILE",
                   help="二进制/目标文件（需配合 --function）")
    ap.add_argument("--function", help="目标函数名（--source/--binary 模式必填）")
    ap.add_argument("--uarch",
                   help="微架构代号: hip08/0xd01, hip09/0xd02, hip10/0xd03, hip11/0xd22, hip12/0xd06")
    ap.add_argument("--iterations", type=int, default=100,
                   help="MCA 仿真迭代次数（默认 100，越大瓶颈分析越稳）")
    ap.add_argument("--cflags", nargs=argparse.REMAINDER, default=[],
                   help="透传给 clang 的额外编译参数（--source 模式）")
    ap.add_argument("--text", action="store_true",
                   help="仅打印可读摘要（默认输出 JSON 契约）")
    args = ap.parse_args()

    if args.check_env:
        check_env()
        return

    if not args.uarch:
        die("必须提供 --uarch（除非使用 --check-env）")
    if not (args.asm or args.source or args.binary):
        die("必须提供 --asm / --source+--function / --binary+--function 之一")
    if (args.source or args.binary) and not args.function:
        die("--source/--binary 模式必须提供 --function")

    mcpu = resolve_mcpu(args.uarch)
    preflight(mcpu, args.uarch)
    asm_text, input_info = get_assembly(args, mcpu)

    # 1) JSON 拿结构化数据
    json_out, jerr, jwarn = run_mca(asm_text, mcpu, args.iterations, want_json=True)
    if json_out is None:
        die("llvm-mca (JSON) 运行失败:\n%s" % jerr)
    # 2) 文本拿 bottleneck-analysis
    text_out, terr, twarn = run_mca(asm_text, mcpu, args.iterations, want_json=False)
    if text_out is None:
        # bottleneck 文本失败不致命，降级为无瓶颈段
        text_out = ""

    warnings = []
    seen = set()
    for w in (jwarn, twarn):
        if w:
            for line in w.strip().splitlines():
                line = line.strip()
                if line and "warning" in line.lower() and line not in seen:
                    seen.add(line)
                    warnings.append(line)

    report = build_report(json_out, text_out, mcpu, args.iterations, input_info, warnings)

    if args.text:
        print(report["llvm_mca_analysis_result"]["summary_text"])
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
