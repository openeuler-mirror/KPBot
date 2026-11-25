#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llvm-mca 流水线可视化器（鲲鹏 HiSilicon 微架构）

对 ARM64 指令序列做流水线级静态仿真，并把结果渲染为自包含离线 HTML
（内联 SVG，无 CDN 依赖）：指令流水时序图 + 逐周期执行占用 + 逐资源端口压力 + KPI 汇总。

与 llvm-mca-analysis 互补：它给出聚合瓶颈归因（JSON），本脚本额外采集
llvm-mca 的 -timeline 输出（每条指令跨周期的 D/e/r 时序）并画图。

展示口径：默认 --iterations 30 --timeline-max-iterations 6（稳态口径），timeline
解析到多次迭代时自动跳过 iter0（热身）并渲染其余迭代（1..5），可用
--start-iteration/--show-iterations 覆盖；单趟流水形态用 --iterations 1。

默认输出：llvm-mca-visualize-demo-<uarch>-iter5.html（--out 可覆盖；--json 打印 JSON）。

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
  --parse <timeline.txt>  只解析/画图，不调用 llvm-mca（离线调试与测试用）

输出：--out <file.html>（默认 stdout 打印 JSON 数据契约；--html 渲染 HTML）。
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
# 真源：llvm-mca-analysis/scripts/mca_analyze.py（同表归一化）；
# 一致性由 scripts/test/test_uarch_table.py 守护，修改时两边同步。
UARCH_MAP = {
    "hip08": "tsv110", "0xd01": "tsv110", "tsv110": "tsv110",
    "hip09": "hip09", "0xd02": "hip09",
    "hip10": "hip10c", "0xd03": "hip10c", "hip10c": "hip10c",
    "hip11": "hip11", "0xd22": "hip11",
    "hip12": "hip12", "0xd06": "hip12",
}
# LLVM -mcpu -> 优先展示的用户代号
MCPU_TO_UARCH = {
    "tsv110": "hip08",
    "hip09": "hip09",
    "hip10c": "hip10",
    "hip11": "hip11",
    "hip12": "hip12",
}

MTRIPLE = "aarch64-linux-gnu"

ALL_UARCHES = [("hip08", "tsv110"), ("hip09", "hip09"),
               ("hip10", "hip10c"), ("hip11", "hip11"), ("hip12", "hip12")]


def sanitize_resource_name(name):
    """清洗 LLVM JSON 把分组资源编码为的控制字符，形如 "TSV110UnitAB.0"。"""
    return re.sub(r'[\x00-\x1f]', lambda m: str(ord(m.group())), name or "")


def _warn(msg):
    sys.stderr.write(str(msg) + "\n")


def die(msg):
    out = {"llvm_mca_visualize_result": {"success": False, "error_message": str(msg)}}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(1)


def _same_file(a, b):
    """同一路径/符号链接/硬链接判定（不存在时回退 realpath 比较）。"""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _cycle_in_window(cyc, cycles):
    """展示窗口为 [0, cycles-1]；右侧边界 cycle == cycles 不进入 JSON/热力图。"""
    return 0 <= cyc < cycles


def _clip_cycles(cyc, cycles, total):
    """按展示窗口裁剪一次占用的持续周期。

    返回 (visible_cycles, clipped)：起点越界返回 (None, False)；窗口内返回
    (round 后的周期数, False)；跨右边界返回 (剩余周期数, True)。
    """
    if not _cycle_in_window(cyc, total):
        return None, False
    dur = max(1, int(round(float(cycles or 0))))
    remain = total - cyc
    if dur <= remain:
        return dur, False
    return remain, True


def resolve_mcpu(uarch):
    key = (uarch or "").strip().lower()
    if key not in UARCH_MAP:
        die("不支持的微架构代号 '%s'。支持: %s" % (
            uarch, ", ".join(sorted(set(UARCH_MAP.keys())))))
    return UARCH_MAP[key]


def run_cmd(cmd, input_text=None):
    try:
        p = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        die("缺少可执行文件: %s（命令: %s）" % (e, " ".join(cmd)))
    except subprocess.TimeoutExpired:
        die("命令超时: %s" % " ".join(cmd))


# --------------------------------------------------------------------------- #
# 环境与模型支持预检（与 llvm-mca-analysis 一致，防 hip 模型静默回退 generic）
# --------------------------------------------------------------------------- #

def probe_mcpu_support(mcpu, mca_bin="llvm-mca"):
    asm = "mov x0, x0\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".s", delete=False) as tf:
        tf.write(asm)
        path = tf.name
    try:
        rc, out, err = run_cmd(
            [mca_bin, "-mtriple=%s" % MTRIPLE, "-mcpu=%s" % mcpu,
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


def list_supported_uarches(mca_bin="llvm-mca"):
    return [(u, m, probe_mcpu_support(m, mca_bin)[0]) for u, m in ALL_UARCHES]


def has_issue_trace(mca_bin):
    res = run_cmd([mca_bin, "--help"])
    assert res is not None
    _rc, out, err = res
    return "issue-trace" in ((out or "") + (err or ""))


def require_issue_trace(mca_bin):
    if has_issue_trace(mca_bin):
        return
    die(
        "当前 llvm-mca 不支持 `-issue-trace`（真实端口占用导出）。本工具默认只输出真实数据，"
        "不再使用近似模式。\n"
        "请使用打过 IssueTraceView 补丁的 llvm-mca，并通过 --mca-bin 指定（或设置环境变量 MCA_BIN）：\n"
        "  cmake -S llvm -B build-mca -G Ninja -DCMAKE_BUILD_TYPE=Release \\\n"
        "        -DLLVM_TARGETS_TO_BUILD=AArch64 -DLLVM_ENABLE_PROJECTS= \\\n"
        "        -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF\n"
        "  ninja -C build-mca llvm-mca\n"
        "  python3 mca_visualize.py --mca-bin build-mca/bin/llvm-mca ...\n"
        "当前使用的 llvm-mca: %s" % mca_bin)


def preflight(mcpu, uarch, mca_bin="llvm-mca"):
    if os.path.sep in mca_bin or "/" in mca_bin:
        if not os.path.isfile(mca_bin):
            die("指定的 llvm-mca 不存在: %s" % mca_bin)
    elif not shutil.which(mca_bin):
        die("未找到 llvm-mca 可执行文件: %s。请安装 LLVM（须含 llvm-mca 工具）或用 --mca-bin 指定。"
            % mca_bin)
    require_issue_trace(mca_bin)
    ok, msg = probe_mcpu_support(mcpu, mca_bin)
    if ok:
        return
    supported = ["%s(%s)" % (u, m) for u, m, ok2 in list_supported_uarches(mca_bin) if ok2]
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


def check_env(mca_bin="llvm-mca"):
    result = {
        "success": True,
        "env_check": True,
        "mca_bin": mca_bin,
        "issue_trace_supported": has_issue_trace(mca_bin) if (shutil.which(mca_bin) or os.path.isfile(mca_bin)) else False,
        "tools": {
            "llvm-mca": bool(shutil.which(mca_bin) or os.path.isfile(mca_bin)),
            "clang": bool(shutil.which("clang")),
            "llvm-objdump": bool(shutil.which("llvm-objdump")),
        },
        "uarch_support": [
            {"uarch": u, "mcpu": m, "supported": ok}
            for u, m, ok in list_supported_uarches(mca_bin)],
        "note": "hip09/hip10c/hip11/hip12 仅在含 HiSilicon 调度模型的 LLVM 构建（如 openEuler "
                "llvm-toolset）中可用；真实端口数据需要打过 IssueTraceView 补丁的 llvm-mca（-issue-trace）。",
        "error_message": "",
    }
    print(json.dumps({"llvm_mca_visualize_result": result},
                     ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# 汇编获取
# --------------------------------------------------------------------------- #

def read_asm_input(path):
    if path == "-":
        return sys.stdin.read(), "stdin"
    if not os.path.isfile(path):
        die("--asm 指定的文件不存在: %s" % path)
    with open(path, "r", errors="replace") as f:
        return f.read(), path


_CFLAG_ALLOW_EXACT = {
    "-O0", "-O1", "-O2", "-O3", "-Os", "-Oz", "-Ofast", "-Og",
    "-fomit-frame-pointer", "-fno-omit-frame-pointer",
    "-funroll-loops", "-fno-unroll-loops", "-funroll-all-loops",
    "-fvectorize", "-fno-vectorize", "-fslp-vectorize", "-fno-slp-vectorize",
    "-ffast-math", "-fno-fast-math", "-fno-math-errno", "-fno-trapping-math",
    "-freciprocal-math", "-funsafe-math-optimizations", "-ffinite-math-only",
    "-fstrict-aliasing", "-fno-strict-aliasing",
    "-finline-functions", "-fno-inline-functions", "-finline", "-fno-inline",
    "-fopenmp", "-fopenmp-simd",
    "-moutline-atomics", "-mno-outline-atomics",
    "-mstrict-align", "-mno-strict-align",
}
_CFLAG_ALLOW_PATTERNS = (
    r"-march=[A-Za-z0-9+_.:-]+",
    r"-mtune=[A-Za-z0-9+_.:-]+",
    r"-mprefer-vector-width=[0-9]+",
    r"-msve-vector-bits=[0-9]+",
    r"-ffp-contract=(off|on|fast)",
    r"-fvect-cost-model=(unlimited|dynamic|cheap|very-cheap)",
    r"-fsimd-cost-model=(unlimited|dynamic|cheap|very-cheap)",
)


def validate_extra_cflags(cflags):
    """--cflags 白名单：只允许优化/代码生成开关；未识别一律拒绝。"""
    bad = []
    for a in cflags or []:
        if a in _CFLAG_ALLOW_EXACT:
            continue
        if any(re.fullmatch(p, a) for p in _CFLAG_ALLOW_PATTERNS):
            continue
        bad.append(a)
    if bad:
        return ("不支持的 --cflags 选项: %s；仅允许优化/代码生成开关"
                "（-O*、-march=、-mtune=、-funroll-loops、-ffast-math、-fopenmp 等）"
                % ", ".join(bad))
    return ""


def compile_source_to_asm(source, mcpu, extra_cflags):
    bad_flag = validate_extra_cflags(extra_cflags)
    if bad_flag:
        return "", False, bad_flag
    if not shutil.which("clang"):
        die("未找到 clang。--source 模式需要 clang 编译源码。")
    if not os.path.isfile(source):
        die("--source 指定的文件不存在: %s" % source)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".s", delete=False)
    tmp.close()
    cmd = ["clang", "-S", "--target=%s" % MTRIPLE, "-mcpu=%s" % mcpu, "-O2"]
    if extra_cflags:
        cmd += extra_cflags
    # 用户参数在前，-o 紧跟其后：保证最后一个 -o 生效且只写本次临时文件
    cmd += ["-o", tmp.name, source]
    rc, out, err = run_cmd(cmd)
    if rc != 0:
        os.unlink(tmp.name)
        return "", False, err or out
    with open(tmp.name, "r", errors="replace") as f:
        asm = f.read()
    os.unlink(tmp.name)
    return asm, True, ""


def disasm_binary(binary, mcpu):
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
    label_re = re.compile(r"^\s*%s\s*:" % re.escape(func), re.MULTILINE)
    m = label_re.search(asm_text)
    if not m:
        return None, "在编译产物中找不到函数标签 '%s:'" % func
    start = m.end()
    end_re = re.compile(r"^\s*\.cfi_endproc\b", re.MULTILINE)
    em = end_re.search(asm_text, start)
    end = em.start() if em else len(asm_text)
    body = asm_text[start:end]
    cleaned = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
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
    lines = disasm_text.splitlines()
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
                break
            continue
        if re.match(r"^\s*[0-9a-f]+\s+<.+>:\s*$", s):
            break
        m = re.match(r"^\s*[0-9a-f]+:\s+(.*)$", s)
        if m:
            instr = m.group(1).strip()
            if instr:
                instrs.append(instr)
    if not instrs:
        return None, "函数 '%s' 反汇编后无有效指令" % func
    return "\n".join(instrs), ""


def get_assembly(args, mcpu):
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
    die("未指定输入：需提供 --asm / --source+--function / --binary+--function，或 --parse")


# --------------------------------------------------------------------------- #
# 运行 MCA
# --------------------------------------------------------------------------- #

def run_mca(asm_text, mcpu, iterations, extra, mca_bin="llvm-mca"):
    """运行 llvm-mca。extra 为附加参数列表（如 ["-timeline"] 或 ["--json", "-all-stats"]）。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".s", delete=False) as tf:
        tf.write(asm_text)
        asm_path = tf.name
    try:
        cmd = [mca_bin, "-mtriple=%s" % MTRIPLE, "-mcpu=%s" % mcpu,
               "--iterations=%d" % iterations] + extra + [asm_path]
        rc, out, err = run_cmd(cmd)
        if rc != 0:
            return None, err or out, err
        return out, "", err
    finally:
        os.unlink(asm_path)


# --------------------------------------------------------------------------- #
# -timeline 解析
# --------------------------------------------------------------------------- #
# llvm-mca -timeline 输出形如：
#
#   Timeline view:
#                       0123456789
#   Index     0123456789
#
#   [0,0]     Deeer        .    vadd.4s v0.4s, v0.4s, v1.4s
#   [0,1]     D==eeer      .    vmul.4s v1.4s, v1.4s, v2.4s
#   [1,0]     .    Deeer   .    vadd.4s v0.4s, v0.4s, v1.4s
#
# 字符图例（随 LLVM 版本略有差异，按 legend 段自适应）：
#   D : 发射 (dispatch)        e : 执行中 (executing)
#   E : 执行完 (executed)      R : 退休 (retired)
#   - : 执行完到退休间的等待   = : 等待执行 (scheduled)
#   . : 未发射 / 空 (idle)

# 默认字符 -> 阶段
DEFAULT_LEGEND = {
    "D": "dispatch",
    "e": "execute",
    "E": "executed",
    "R": "retire",
    "r": "retire",
    "-": "retire_wait",
    "=": "wait",
    ".": "idle",
    " ": None,
}


# llvm-mca timeline 的网格布局：
#   . 每行 = [iter,index] 前缀（右对齐到固定宽度） + 每周期一字符的网格 + 右对齐的指令文本
#   . 所有行共享同一个网格起始绝对列 G0（cycle 0 所在列），故 周期 = 绝对列 - G0
#   . 活跃阶段字符（D/e/E/R/-/=）在网格里是连续无空白的；空闲/等待是 '.'/空格
# 因此直接从数据行推断 G0 与总周期数 C 即可，无需解析头部数字行（其行列对齐不可靠）。

GRID_VOCAB = set(" DdeER=.-")


def _first_grid_col(data_lines):
    """所有行共享的网格起始绝对列 G0 = 各数据行"前缀后的第一个非空白字符"的最小绝对列。"""
    g = None
    for s in data_lines:
        m = re.match(r"^\s*\[(\d+),(\d+)\]\s*(.*)$", s.rstrip("\n"))
        if not m:
            continue
        base = m.start(3)
        for i, ch in enumerate(m.group(3)):
            if ch not in " \t":
                v = base + i
                if g is None or v < g:
                    g = v
                break
    return g


def _instr_col(s, g0):
    """返回该行指令文本的起始绝对列（网格之后第一个非网格词汇字符）。"""
    l = s.rstrip("\n")
    for i in range(g0, len(l)):
        if l[i] not in GRID_VOCAB:
            return i
    return len(l)


def _read_legend(text):
    """从 timeline 尾部的图例段提取 字符->阶段。找出 ':.'' 前的 Legend 说明行。"""
    legend = dict(DEFAULT_LEGEND)
    m = re.search(r"Legend:?\s*(.*?)(?=\n\n|\Z)", text, re.S)
    if not m:
        return legend
    for line in m.group(1).splitlines():
        mm = re.match(r"^\s*(.):\s*(.+)$", line.strip())
        if mm:
            ch, desc = mm.group(1), mm.group(2).strip().lower()
            if "dispatch" in desc:
                legend[ch] = "dispatch"
            elif "execut" in desc:
                legend[ch] = "execute" if "is executing" in desc or "executing" in desc else "executed"
            elif "retir" in desc:
                legend[ch] = "retire"
            elif "wait" in desc or "schedul" in desc:
                legend[ch] = "wait"
    return legend


def parse_timeline(text):
    """解析 -timeline 文本 → 结构化流水数据。

    返回 dict：ok, error, legend, cycles_total, instructions[]（逐指令逐周期阶段），
    per_cycle{}（每周期 dispatch/execute/retire 计数），critical_indices。
    """
    out = {"ok": False, "error": "", "legend": dict(DEFAULT_LEGEND),
           "cycles_total": 0, "instructions": [], "per_cycle": {
               "dispatch": [], "execute": [], "retire": [], "wait": []}}
    if not text:
        out["error"] = "空 -timeline 输出"
        return out
    idx = text.find("Timeline view:")
    if idx == -1:
        out["error"] = "找不到 'Timeline view:' 段（可能 llvm-mca 未输出 timeline，或版本格式不同）"
        return out
    body = text[idx + len("Timeline view:"):]
    lines = body.splitlines()

    # llvm-mca 固定格式：前缀 PadToColumn(10) + (LastCycle+1) 列网格 + 3 空格 + 指令。
    # 网格宽度从 "Index" 表头取，避免用“首个非网格字符”猜测（eor 的 e 会被误判）。
    header_cells = None
    for ln in lines:
        if ln.startswith("Index"):
            header_cells = len(ln) - 10
            break
    if header_cells is not None and header_cells < 1:
        header_cells = None

    data_lines = []
    in_data = False
    for ln in lines:
        if re.match(r"^\s*\[\d+,\d+\]", ln):
            in_data = True
        if in_data and re.match(r"^\s*\[\d+,\d+\]", ln):
            data_lines.append(ln)
        elif in_data:
            break  # 数据行结束（遇到后续小节标题）
    if not data_lines:
        out["error"] = "timeline 段无数据行"
        return out

    legend = _read_legend(strip_to_timeline_end(text))
    out["legend"] = legend
    g0 = 10 if header_cells is not None else _first_grid_col(data_lines)
    if g0 is None:
        out["error"] = "无法确定 timeline 网格起始列（格式不匹配，请检查 llvm-mca 版本）"
        return out

    # 每行的指令文本起始列 + 网格最后非空白列，用于推算总周期数 C
    rows_info = []
    for s in data_lines:
        m = re.match(r"^\s*\[(\d+),(\d+)\]\s*(.*)$", s.rstrip("\n"))
        if not m:
            continue
        it, ine = int(m.group(1)), int(m.group(2))
        if header_cells is not None:
            ich = g0 + header_cells + 3
            if ich > len(s):
                ich = _instr_col(s, g0)
        else:
            ich = _instr_col(s, g0)
        last_grid = -1
        for i in range(g0, ich):
            if s[i] not in " \t":
                last_grid = i
        rows_info.append((it, ine, ich, s, last_grid))
    if not rows_info:
        out["error"] = "timeline 数据行解析后无有效指令"
        return out

    last_all = max((r[4] for r in rows_info), default=-1)
    if header_cells is not None:
        cycles_total = header_cells
    else:
        if last_all < g0:
            out["error"] = "timeline 数据行无效"
            return out
        cycles_total = last_all - g0 + 1
    if cycles_total < 1:
        out["error"] = "timeline 周期数无效"
        return out

    inst = []
    unknown = {}
    for it, ine, ich, s, _ in rows_info:
        instr_text = s[ich:].strip()
        stages = {}
        for i in range(g0, ich):
            ch = s[i]
            if ch in " \t":
                continue
            st = legend.get(ch)
            if st is None:
                unknown[ch] = unknown.get(ch, 0) + 1
                continue
            stages[i - g0] = st
        cycles = sorted(stages.keys())
        dispatch_cyc = next((c for c in cycles if stages[c] == "dispatch"), None)
        exec_cycles = [c for c in cycles if stages[c] in ("execute", "executed")]
        retire_cyc = next((c for c in cycles if stages[c] == "retire"), None)
        wait_cycles = [c for c in cycles if stages[c] == "wait"]
        inst.append({
            "iter": it, "index": ine, "text": instr_text,
            "dispatch": dispatch_cyc,
            "exec_start": exec_cycles[0] if exec_cycles else None,
            "exec_end": exec_cycles[-1] if exec_cycles else None,
            "retire": retire_cyc,
            "wait_cycles": wait_cycles,
            "stages": {str(c): st for c, st in stages.items()},
        })

    # 每周期计数
    n_instr = max((x["index"] for x in inst), default=0) + 1
    disp = [0] * cycles_total
    exec_occ = [0] * cycles_total
    ret = [0] * cycles_total
    wait = [0] * cycles_total
    for x in inst:
        if x["dispatch"] is not None:
            disp[x["dispatch"]] += 1
        if x["retire"] is not None:
            ret[x["retire"]] += 1
        for st_c, st in x["stages"].items():
            c = int(st_c)
            if st in ("execute", "executed"):
                exec_occ[c] += 1
            elif st == "wait":
                wait[c] += 1

    if unknown:
        chars = ", ".join(repr(c) for c in sorted(unknown))
        out["error"] = ("timeline 含未知阶段字符: %s（LLVM 版本可能变更，请核对 -timeline 输出）"
                        % chars)
        return out

    out["ok"] = True
    out["cycles_total"] = cycles_total
    out["instructions"] = inst
    out["n_instructions"] = n_instr
    out["per_cycle"] = {"dispatch": disp, "execute": exec_occ,
                        "retire": ret, "wait": wait}
    return out


def strip_to_timeline_end(text):
    """截取到 'Timeline view:' 之后的正文（用于读图例/尾部）。"""
    idx = text.find("Timeline view:")
    return text[idx:] if idx != -1 else text


# --------------------------------------------------------------------------- #
# JSON 侧解析（Summary / 资源压力 / 逐指令）
# --------------------------------------------------------------------------- #

def parse_json_stats(json_text):
    """从 --json 输出提取 summary / resource pressure / instructions。"""
    try:
        data = json.loads(json_text)
    except (ValueError, TypeError) as e:
        die("MCA --json 输出解析失败: %s" % e)
    regions = data.get("CodeRegions", [])
    if not regions:
        die("MCA 未返回任何 CodeRegion（输入汇编可能为空或无法解析）")
    region = regions[0]
    target = data.get("TargetInfo", {})
    resources = [sanitize_resource_name(r) for r in target.get("Resources", [])]

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

    rpv = region.get("ResourcePressureView", {}).get("ResourcePressureInfo", [])
    per_resource = [0.0] * len(resources)
    total_idx = max((e.get("InstructionIndex", -1) for e in rpv), default=-2)
    for item in rpv:
        if item.get("InstructionIndex", -1) == total_idx:
            ri = item.get("ResourceIndex", 0)
            if 0 <= ri < len(per_resource):
                per_resource[ri] += item.get("ResourceUsage", 0.0)
    resource_pressure_per_iteration = [
        {"resource": resources[i] if i < len(resources) else "R%d" % i,
         "pressure": round(per_resource[i], 4)}
        for i in range(len(resources))]

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
    # 真实逐次 issue 记录（来自打过 IssueTraceView 补丁的 llvm-mca -issue-trace）
    itv = region.get("IssueTraceView", {})
    issue_trace = {
        "instructions": itv.get("Instructions", 0) or 0,
        "records": itv.get("Records", []) or [],
        "reserved": itv.get("Reserved", []) or [],
    }
    return (summary, resource_pressure_per_iteration, instructions_info,
            resources, dispatch_stalls, issue_trace)


def parse_bottleneck_section(text):
    result = {
        "primary_type": "unknown",
        "backend_pressure_cycles_pct": None,
        "resource_pressure_pct": None,
        "data_dependencies_pct": None,
        "register_dependencies_pct": None,
        "memory_dependencies_pct": None,
        "limiting_resources": [],
        "critical_sequence": [],
    }
    if not text:
        return result
    start = text.find("Cycles with backend pressure")
    end = text.find("Instruction Info:")
    if start == -1:
        return result
    section = text[start:end if end != -1 else len(text)]
    m = re.search(r"Cycles with backend pressure increase\s*\[\s*([\d.]+)%\s*\]", section)
    if m:
        result["backend_pressure_cycles_pct"] = float(m.group(1))
    m = re.search(r"Resource Pressure\s*\[\s*([\d.]+)%\s*\]", section)
    if m:
        result["resource_pressure_pct"] = float(m.group(1))
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

    # 关键序列：+----> N.  <instr>  ## TYPE: detail
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
            {"index": idx, "instruction": instr,
             "dependency_type": dtype, "detail": ann})

    rp = result.get("resource_pressure_pct")
    dd = result.get("data_dependencies_pct")
    bp = result.get("backend_pressure_cycles_pct")
    if bp is not None and bp < 10.0:
        result["primary_type"] = "low_pressure"
    elif rp is not None and dd is not None:
        if rp > dd * 1.5:
            result["primary_type"] = "resource_pressure"
        elif dd > rp * 1.5:
            result["primary_type"] = "data_dependency"
        else:
            result["primary_type"] = "mixed"
    return result


def classify_critical_indices(bottleneck):
    """返回 bottleneck 关键序列涉及的指令 index 集合，用于图上高亮。"""
    idxs = set()
    for c in bottleneck.get("critical_sequence", []):
        if "index" in c:
            idxs.add(c["index"])
    return sorted(idxs)


# --------------------------------------------------------------------------- #
# HTML / SVG 渲染
# --------------------------------------------------------------------------- #

STAGE_COLOR = {
    "dispatch": "#4c6ef5",
    "execute": "#2f9e44",
    "executed": "#2f9e44",
    "wait": "#adb5bd",
    "retire": "#f08c00",
    "retire_wait": "#ffe066",
    "idle": "",
}
STAGE_LABEL_CN = {
    "dispatch": "发射(D)",
    "execute": "执行中(e)",
    "executed": "执行完(E)",
    "wait": "等待(=)",
    "retire": "退休(R)",
    "retire_wait": "退休等待(-)",
    "idle": "空闲",
}


def _esc(s):
    import html
    return html.escape(str(s))


def _kpi_cards(meta, summary, bottleneck):
    cards = [
        ("IPC", "%s" % (summary.get("ipc") if summary.get("ipc") is not None else "-")),
        ("Block RThroughput", "%s" % (summary.get("block_rthroughput")
                                      if summary.get("block_rthroughput") is not None else "-")),
        ("周期/迭代", "%s" % (summary.get("cycles_per_iteration") or "-")),
        ("Dispatch 宽度", "%s" % (summary.get("dispatch_width") or "-")),
    ]
    bt = bottleneck.get("primary_type", "unknown")
    bt_cn = {
        "resource_pressure": "资源压力",
        "data_dependency": "数据依赖",
        "mixed": "混合",
        "low_pressure": "低后端压力",
        "unknown": "未知",
    }.get(bt, bt)
    items = []
    for name, val in cards:
        items.append(
            '<div class="kpi"><div class="kpi-v">%s</div><div class="kpi-n">%s</div></div>' % (_esc(val), _esc(name)))
    items.append('<div class="kpi"><div class="kpi-v kpi-bt">%s</div><div class="kpi-n">%s</div></div>'
                 % (_esc(bt_cn), _esc("瓶颈类型 · " + bt)))
    return '<div class="kpis">%s</div>' % "".join(items)


ITER_COLORS = ["#1c7ed6", "#2f9e44", "#f59f00", "#e8590c", "#e03131"]


def _row_chips(uses):
    """按真实 issue 资源记录生成端口芯片（Kind 已含单元后缀，如 LD.2）。"""
    entries = []
    for u in (uses or []):
        c = float(u.get("Cycles", 0) or 0)
        if c <= 0:
            continue
        entries.append((str(u.get("Kind", "")), c))
    entries.sort(key=lambda x: -x[1])
    chips = ["%s×%g" % (_short_res(n), c) for n, c in entries]
    full = " ".join(chips)
    if len(chips) > 3:
        return " ".join(chips[:3]) + " +%d" % (len(chips) - 3), full
    return full, full


def _iter_window(items):
    """该迭代内所有活跃阶段的最小/最大周期。"""
    lo = hi = None
    for x in items:
        for c_str, st in x["stages"].items():
            if st in ("idle",):
                continue
            c = int(c_str)
            lo = c if lo is None else min(lo, c)
            hi = c if hi is None else max(hi, c)
    return lo, hi


def _timeline_group_svg(items, C, n_rows, critical_ids, color, issue_map, uops_by_index):
    """单个迭代的流水时序 SVG：行=该迭代的 ISA 指令，右侧标注 uOps 与端口占用。"""
    cw = 14       # 每周期像素宽
    ry = 20       # 每行高
    row_h = ry - 5
    margin_l = 70
    margin_r = 340
    margin_t = 30
    svg_w = margin_l + C * cw + margin_r
    svg_h = margin_t + n_rows * ry + 24

    parts = []
    parts.append('<svg class="tl" viewBox="0 0 %d %d" '
                 'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMinYMin meet">' % (svg_w, svg_h))
    # 周期刻度
    for c in range(C + 1):
        if c % 10 == 0:
            x = margin_l + c * cw
            parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="grid"/>' % (x, margin_t, x, svg_h - 4))
            parts.append('<text x="%d" y="%d" class="cyc">%d</text>' % (x, margin_t - 6, c))
    # 行分隔 + 行标签（迭代号 + 指令号，按迭代色）
    for r in range(n_rows):
        y = margin_t + r * ry
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="rowline"/>' % (
            margin_l, y, margin_l + C * cw, y))
        it_no = items[0]["iter"] if items else 0
        parts.append('<text x="%d" y="%d" class="lbl" text-anchor="end" fill="%s">it%d #%d</text>' % (
            margin_l - 6, y + ry - 3, color, it_no, r))
    # 单元格
    start_x = margin_l + C * cw + 8
    for x in items:
        r = x["index"]
        y = margin_t + r * ry
        critical = x["index"] in critical_ids
        chips, chips_full = _row_chips(issue_map.get((x["iter"], x["index"]), []))
        uops = uops_by_index.get(x["index"])
        for c_str, st in x["stages"].items():
            c = int(c_str)
            if not STAGE_COLOR.get(st):
                continue
            px = margin_l + c * cw
            cls = "cell-ret" if st == "retire" else "cell-" + st
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" class="cell %s" '
                         '>%s</rect>' % (
                             px, y + (ry - row_h) / 2, cw, row_h, cls,
                             _tooltip(x, st, c, uops, chips_full)))
        txt = x["text"] or ("#%d" % x["index"])
        row_tip = "it%d #%d: %s\nuOps: %s\n端口占用: %s" % (
            x["iter"], x["index"], txt, uops if uops is not None else "?", chips_full or "-")
        parts.append('<text x="%d" y="%d" class="instr"><title>%s</title>%s</text>' % (
            start_x, y + ry - 3, _esc(row_tip), _esc(txt)))
        if chips:
            chip_x = start_x + int(len(txt) * 6.7) + 10
            parts.append('<text x="%d" y="%d" class="chip">u%s · %s</text>' % (
                chip_x, y + ry - 3, _esc(uops if uops is not None else "?"), _esc(chips)))
        if critical:
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" class="crit" rx="3"/>' % (
                margin_l, y + (ry - row_h) / 2, C * cw, row_h))
    parts.append("</svg>")
    return "".join(parts)


def _timeline_groups(tl, critical_ids, data):
    """按迭代分组的流水时序图（可折叠）；不伪造 uop 时刻，仅标注 uOps 数与端口占用。"""
    if not tl.get("ok"):
        return '<div class="err">%s</div>' % _esc(tl.get("error", "timeline 解析失败"))
    inst = tl["instructions"]
    C = tl["cycles_total"]
    n_rows = tl.get("n_instructions", max((x["index"] for x in inst), default=0) + 1)
    issue_map = {}
    for r in ((data.get("issue_trace") or {}).get("records") or []):
        try:
            key = (int(r.get("Iteration", -1)), int(r.get("Index", -1)))
        except (TypeError, ValueError):
            continue
        issue_map[key] = r.get("Resources") or []
    uops_by_index = {i.get("index"): i.get("uops") for i in (data.get("instructions_info") or [])}

    by_iter = {}
    for x in inst:
        by_iter.setdefault(x["iter"], []).append(x)
    iters = sorted(by_iter)
    collapse_first_two = len(iters) >= 3

    blocks = []
    for pos, it in enumerate(iters):
        color = ITER_COLORS[pos % len(ITER_COLORS)]
        opened = "" if (collapse_first_two and pos < 2) else " open"
        lo, hi = _iter_window(by_iter[it])
        span = ("cycle %d..%d" % (lo, hi)) if lo is not None else "无活跃周期"
        svg = _timeline_group_svg(by_iter[it], C, n_rows, critical_ids, color,
                                  issue_map, uops_by_index)
        blocks.append(
            '<details class="iterg"%s><summary style="color:%s">==== 第 %d 次迭代 ===='
            '<span class="itmeta">%d 条指令 · %s</span></summary>'
            '<div class="scrollx">%s</div></details>' % (
                opened, color, it, len(by_iter[it]), span, svg))

    legend = "".join(
        '<span class="lg"><i style="background:%s"></i>%s</span>' % (STAGE_COLOR[k], STAGE_LABEL_CN[k])
        for k in ["dispatch", "execute", "wait", "retire", "retire_wait"])
    legend += '<span class="lg"><i style="border:1px dashed #e03131;background:none"></i>关键路径</span>'
    iter_legend = "".join(
        '<span class="lg"><i style="background:%s"></i>第 %d 次</span>'
        % (ITER_COLORS[p % len(ITER_COLORS)], it) for p, it in enumerate(iters))
    note = ('<div class="mini">纵轴=ISA 指令（<b>MCA 不导出 uop 级时刻</b>，不拆 uop）；'
            '行右侧标注 <b>uOps 数</b>与<b>真实端口占用</b>（来自 llvm-mca -issue-trace，逐迭代）。'
            '<b>等待(=)为灰色，原因未知</b>（MCA 不区分等寄存器/等资源）；'
            '跨迭代依赖/关键路径看红色虚线框。迭代色仅用于组标题与行标签。</div>'
            '<div class="legendrow">%s</div><div class="legendrow">%s</div>' % (legend, iter_legend))
    return note + "".join(blocks)


def _dispatch_stats_html(ds):
    """DispatchStatistics 聚合等待计数（GROUP/SCHEDQ/RAT/RCU/LQ/SQ）。"""
    if not ds:
        return '<div class="mini">无 dispatch 统计（--parse 或 JSON 不可用）。</div>'
    cn = {"GROUP": "分组限制", "SCHEDQ": "调度队列满", "RAT": "重命名表满",
          "RCU": "退休单元满", "LQ": "加载队列满", "SQ": "存储队列满", "USH": "其它"}
    items = sorted(ds.items(), key=lambda kv: -float(kv[1] or 0))
    mx = max([float(v or 0) for _, v in items] + [1.0])
    rows = []
    for k, v in items:
        w = 100.0 * float(v or 0) / mx
        rows.append('<div class="dbar"><span class="dk">%s</span>'
                    '<span class="dbar-bg"><i style="width:%.1f%%"></i></span>'
                    '<span class="dv">%s</span></div>' % (
                        _esc(cn.get(k, k)), w, _esc(v)))
    return ('<div class="mini">聚合等待计数（MCA <b>DispatchStatistics</b>；非逐指令/逐周期原因，'
            '用于判断哪类结构限制发射）。</div><div class="dbars">%s</div>' % "".join(rows))


def _tooltip(x, st, c, uops=None, chips=None):
    lines = ["it%d #%d: %s" % (x["iter"], x["index"], x["text"]),
             "cycle %d: %s" % (c, STAGE_LABEL_CN.get(st, st))]
    if st == "wait":
        lines.append("等待（原因未知：MCA 不区分等寄存器/等资源）")
    if uops is not None:
        lines.append("uOps: %s" % uops)
    if chips:
        lines.append("端口占用: %s" % chips)
    return "<title>%s</title>" % _esc("\n".join(lines))



def _occupancy_svg(tl, summary):
    """逐周期执行占用（流水饱和/空转视图）。"""
    if not tl.get("ok"):
        return '<div class="err">timeline 解析失败</div>'
    pc = tl["per_cycle"]
    C = tl["cycles_total"]
    n = tl.get("n_instructions", 1)
    occ = pc["execute"]
    maxv = max(max(occ), 1)
    bw = 5
    m = 46
    w = m + C * bw + 20
    h = 220
    pts = []
    for c, v in enumerate(occ):
        x = m + c * bw
        y = h - 30 - (v / float(maxv)) * (h - 60)
        pts.append((x, y))
    top = 30
    bottom = h - 30
    parts = []
    parts.append('<svg class="occ" viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg">' % (w, h))
    # 网格
    for g in range(maxv + 1):
        gy = bottom - (g / float(maxv)) * (bottom - top)
        parts.append('<line x1="%d" y1="%0.1f" x2="%d" y2="%0.1f" class="grid"/>' % (m, gy, m + C * bw, gy))
        parts.append('<text x="%d" y="%0.1f" class="cyc">%d</text>' % (m - 8, gy + 3, g))
    # 执行占用面积
    if pts:
        path = "M%d,%d L%0.1f,%0.1f " % (m, bottom, pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            path += "L%0.1f,%0.1f " % (x, y)
        path += "L%d,%d Z" % (m + C * bw, bottom)
        parts.append('<path d="%s" class="occfill"/>' % path)
        pth = "M%0.1f,%0.1f" % pts[0]
        for x, y in pts[1:]:
            pth += " L%0.1f,%0.1f" % (x, y)
        parts.append('<path d="%s" class="occline"/>' % pth)
    # 周期刻度
    for c in range(0, C, 10):
        x = m + c * bw
        parts.append('<text x="%d" y="%d" class="cyc">%d</text>' % (x, h - 10, c))
    # 块吞吐下界虚线
    rtp = summary.get("block_rthroughput")
    if rtp:
        pass
    parts.append("</svg>")
    ttl = ('<div class="mini">逐周期执行中指令数（e/E，max=%d，总周期=%d）。'
           'Plateau 接近指令行数说明后端饱和，谷底说明依赖等待/空转。</div>' % (maxv, C))
    return ttl + "<div class='scrollx'>" + "".join(parts) + "</div>"


def _resource_svg(resource_pressure_per_iteration):
    """逐资源端口压力横向条形图。"""
    rp = sorted([x for x in resource_pressure_per_iteration if x["pressure"] > 0],
                key=lambda x: x["pressure"], reverse=True)
    if not rp:
        return '<div class="mini">无资源压力数据。</div>'
    maxv = rp[0]["pressure"] if rp[0]["pressure"] > 0 else 1
    lh = 22
    m = 90
    h = 30 + len(rp) * lh
    w = m + 350
    parts = []
    parts.append('<svg class="res" viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg">' % (w, h))
    for i, x in enumerate(rp):
        y = 20 + i * lh
        parts.append('<text x="%d" y="%d" class="lbl" text-anchor="end">%s</text>' % (
            m - 10, y + 13, _esc(_short_res(x["resource"]))))
        bw = (x["pressure"] / float(maxv)) * 300
        parts.append('<rect x="%d" y="%d" width="%0.1f" height="16" class="resbar" rx="2"><title>%s (%.2f)</title></rect>' % (
            m, y, bw, _esc(x["resource"]), x["pressure"]))
        parts.append('<text x="%d" y="%d" class="resval">%.2f</text>' % (m + bw + 6, y + 13, x["pressure"]))
    parts.append("</svg>")
    return '<div class="mini">每个执行资源每迭代的占用。</div>' + \
        "<div class='scrollx'>" + "".join(parts) + "</div>"


def _critical_svg(bottleneck):
    bt = bottleneck.get("primary_type", "unknown")
    cs = bottleneck.get("critical_sequence", [])
    parts = []
    parts.append('<div class="mini"><b>瓶颈归因: %s</b>%s</div>' % (
        _esc(bt),
        (' ·  后端压力周期 %.2f%% ·  资源压力 %.2f%% ·  数据依赖 %.2f%%' % (
            bottleneck.get("backend_pressure_cycles_pct") or 0,
            bottleneck.get("resource_pressure_pct") or 0,
            bottleneck.get("data_dependencies_pct") or 0))
        if bottleneck.get("backend_pressure_cycles_pct") is not None else ""))
    if cs:
        rows = []
        for c in cs:
            rows.append('<tr><td class="mono">#%s</td><td class="mono">%s</td><td class="mono"><span class="dep">%s</span></td></tr>' % (
                _esc(c.get("index", "")), _esc(c.get("instruction", "")), _esc(c.get("detail", ""))))
        parts.append('<table class="crittbl"><tr><th>#</th><th>指令</th><th>依赖</th></tr>%s</table>' % "".join(rows))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 资源×周期占用视图（纵轴=硬件资源单元，横轴=周期；数据来自 -issue-trace 的真实 issue 记录）
# --------------------------------------------------------------------------- #
# 每条指令每次迭代的 Issued 事件给出 (unit, cycles)：单元从 issue cycle 起连续占用
# cycles 个周期（ResourceManager 的 BusyResources 语义）。
# 热力图每格表示“该周期被占用 1 次”，块长度=连续占用周期数，同周期并发才累加。

def _canon_res(name):
    """把各 uarch 的资源名归一化成唯一键（去前缀、小写），用于去重/匹配。"""
    n = (name or "").lower()
    for pre in ("tsv110unit", "hip10cunit", "hip08unit", "hip09unit", "hip10unit",
                "hip11unit", "hip12unit", "tsv110", "hip10c", "hip08", "hip09",
                "hip10", "hip11", "hip12", "hip", "unit"):
        n = n.replace(pre, "")
    return n


def _short_res(name):
    return _canon_res(name).upper() or (name or "")


def _res_type(res):
    """按端口名（去前缀规范名）判定端口类型，用于着色区分。"""
    n = _canon_res(res)
    if not n:
        return "other"
    has_ld = "ld" in n
    has_st = "st" in n
    if has_ld and has_st:
        return "memory"
    if has_ld:
        return "load"
    if has_st:
        return "store"
    if "mdu" in n or n.startswith(("sm", "m")) or "mul" in n:
        return "mul"
    if "fsu" in n or n.startswith(("v", "f")):
        return "vector_fp"
    if "alu" in n or n.startswith("ab"):
        return "scalar"
    if n.startswith(("b", "br")):
        return "branch"
    if n.startswith(("s", "a")):
        return "scalar"
    return "other"


# 端口类型 -> 主色(RGB) 与中文名（图例用）
TYPE_RGB = {
    "load": (25, 113, 194),      # 蓝
    "store": (232, 89, 12),      # 橙
    "vector_fp": (47, 158, 68),  # 绿
    "scalar": (12, 166, 120),    # 青
    "mul": (156, 54, 181),       # 紫
    "branch": (224, 49, 49),     # 红
    "memory": (73, 80, 87),      # 青灰
    "other": (134, 142, 150),    # 灰
}
TYPE_CN = {
    "load": "Load 访存",
    "store": "Store 访存",
    "vector_fp": "Vector/FP",
    "scalar": "Scalar 标量",
    "mul": "Mul 乘除",
    "branch": "Branch 分支",
    "memory": "Mem 合一",
    "other": "Other",
}
# 运算类别描述：说明该类端口服务于哪类运算（用于悬停/图例/JSON resources_detail）
TYPE_OPS = {
    "load": "访存·取数（load）",
    "store": "访存·存数（store）",
    "vector_fp": "浮点/SIMD 运算（标量浮点与向量浮点共用 FP/SIMD 执行单元；含 fmla 等浮点 FMA/MAC）",
    "scalar": "标量整数：加减/逻辑/移位/比较/条件选择/地址计算",
    "mul": "标量整数乘/乘加（MAC）/除",
    "branch": "分支/控制流",
    "memory": "访存·取/存合并单元（如 tsv110 Ld0St）",
    "other": "其它/未分类",
}


def _wcolor(wt, ptype="load"):
    """颜色按端口类型定色相，透明度按同周期并发占用数（正常 1；并发越多越实）。"""
    r, g, b = TYPE_RGB.get(ptype, TYPE_RGB["other"])
    a = min(0.22 + 0.20 * wt, 0.95)
    return "rgba(%d,%d,%d,%0.2f)" % (r, g, b, a)


def _resource_timeline_svg(data):
    """资源×周期热图：基于 patched llvm-mca 的真实 issue 记录（逐迭代、逐单元、逐 cycle）。
    每个色块表示“该周期该单元被占用”；块长度=该次 issue 的连续占用周期数。
    reserved 组在成员行画虚线（MCA 不选具体单元）。
    """
    tl = data.get("timeline", {})
    resources = data.get("resources", [])
    issue = data.get("issue_trace") or {}
    records = issue.get("records") or []
    reserved = issue.get("reserved") or []
    if not tl or not tl.get("ok"):
        return '<div class="err">timeline 解析失败</div>'
    if not records and not reserved:
        return ('<div class="mini">端口资源视图需要真实 issue 数据（llvm-mca -issue-trace）。'
                '--parse 离线模式不提供；请用打过 IssueTraceView 补丁的 llvm-mca 运行。'
                '近似模式已删除。</div>')
    inst = tl["instructions"]
    C = tl["cycles_total"]
    text_by_key = {(x["iter"], x["index"]): (x["text"] or ("#%d" % x["index"]))
                   for x in inst}

    # 行 = 真实记录里出现过的单元（Kind 已含单元后缀，如 HIP12UnitLD.2）
    order = {}
    for i, r in enumerate(resources):
        order.setdefault(_canon_res(r), i)
    used = {}
    for rec in records:
        for u in (rec.get("Resources") or []):
            name = str(u.get("Kind", ""))
            if not name:
                continue
            used.setdefault(_canon_res(name), name)
    for rec in reserved:
        for m in (rec.get("Members") or []):
            used.setdefault(_canon_res(str(m)), str(m))
    if not used:
        return '<div class="mini">真实 issue 记录中没有资源占用。</div>'
    names = sorted(used.values(), key=lambda n: (order.get(_canon_res(n), 10 ** 6), n))
    rows = [{"name": n, "key": _canon_res(n), "type": _res_type(n)} for n in names]
    row_count = len(rows)
    row_by_key = {rows[i]["key"]: i for i in range(row_count)}

    occ = [[0.0] * C for _ in range(row_count)]
    tip = [[[] for _ in range(C)] for _ in range(row_count)]
    for rec in records:
        it = int(rec.get("Iteration", -1))
        idx = int(rec.get("Index", -1))
        cyc = int(rec.get("Cycle", 0))
        for u in (rec.get("Resources") or []):
            ri = row_by_key.get(_canon_res(str(u.get("Kind", ""))))
            if ri is None:
                continue
            wt = float(u.get("Cycles", 0) or 0)
            if wt <= 0:
                continue
            dur = max(1, int(round(wt)))
            if cyc < 0:
                continue
            if cyc + dur > C:
                dur = C - cyc
            if dur <= 0:
                continue
            for k in range(dur):
                c = cyc + k
                # 每格表示“该周期被占用 1 次”；并发才累加
                occ[ri][c] += 1.0
                tip[ri][c].append((it, idx, wt, k + 1, dur))

    # reserved 组：Issued 事件不含具体单元；从模型展开的成员单元画虚线
    rsv = [[[] for _ in range(C)] for _ in range(row_count)]
    for rec in reserved:
        it = int(rec.get("Iteration", -1))
        idx = int(rec.get("Index", -1))
        cyc = int(rec.get("Cycle", 0))
        total = max(1, int(round(float(rec.get("Cycles", 0) or 0))))
        group = str(rec.get("Group", ""))
        members = [str(m) for m in (rec.get("Members") or [])]
        if cyc < 0:
            continue
        if cyc + total > C:
            total = C - cyc
        if total <= 0:
            continue
        for m in members:
            ri = row_by_key.get(_canon_res(m))
            if ri is None:
                continue
            for k in range(total):
                c = cyc + k
                if c < 0 or c >= C:
                    continue
                rsv[ri][c].append((it, idx, group, total, k + 1, members))
    if not any(any(row) for row in occ) and not any(any(row) for row in rsv):
        return '<div class="mini">真实 issue 记录的周期超出展示窗口。</div>'

    cw, ry = 14, 20
    margin_l = 74
    margin_r = 24
    row_h = ry - 5
    svg_w = margin_l + C * cw + margin_r
    svg_h = 34 + row_count * ry
    parts = []
    parts.append('<svg class="res" viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg">' % (svg_w, svg_h))
    for c in range(0, C + 1, 10):
        x = margin_l + c * cw
        if x <= svg_w:
            parts.append('<line x1="%d" y1="10" x2="%d" y2="%d" class="grid"/>' % (x, x, svg_h - 4))
            parts.append('<text x="%d" y="%d" class="cyc">%d</text>' % (x, 24, c))
    for r in range(row_count):
        y = 30 + r * ry
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="rowline"/>' % (margin_l, y, margin_l + C * cw, y))
        tr, tg, tb = TYPE_RGB.get(rows[r]["type"], TYPE_RGB["other"])
        parts.append('<text x="%d" y="%d" class="lbl" style="fill:rgb(%d,%d,%d)" title="%s">%s</text>' % (
            margin_l - 8, y + ry - 3, tr, tg, tb,
            _esc("%s：%s" % (TYPE_CN.get(rows[r]["type"], rows[r]["type"]),
                             TYPE_OPS.get(rows[r]["type"], ""))),
            _esc(_short_res(rows[r]["name"]))))
    for r in range(row_count):
        y = 30 + r * ry
        for c in range(C):
            wt = occ[r][c]
            if wt <= 0:
                continue
            n = len(tip[r][c])
            tx = " · ".join(
                "it%d #%d: %s（本次连续占用 %g cycle，第 %d/%d 周期）" % (
                    it, i, text_by_key.get((it, i), ""), tot, k, dur)
                for it, i, tot, k, dur in tip[r][c])
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" class="hcell" style="fill:%s">%s</rect>' % (
                margin_l + c * cw, y + (ry - row_h) / 2, cw, row_h, _wcolor(wt, rows[r]["type"]),
                "<title>端口 %s [%s] @ cycle %d（本周期被占用 %d 次）\n运算类别：%s\n%s</title>" % (
                    _esc(_short_res(rows[r]["name"])),
                    _esc(TYPE_CN.get(rows[r]["type"], rows[r]["type"])),
                    c, n, _esc(TYPE_OPS.get(rows[r]["type"], "")), _esc(tx))))
    # reserved 组：成员行上叠虚线（不参与实心占用计数）
    for r in range(row_count):
        y = 30 + r * ry
        for c in range(C):
            cells = rsv[r][c]
            if not cells:
                continue
            tx = " · ".join(
                "it%d #%d: 组 %s（成员 %s）整段 %d cycle，第 %d/%d，具体单元未定" % (
                    it, i, grp, "/".join(_short_res(m) for m in members), total, k, total)
                for it, i, grp, total, k, members in cells)
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" class="hcell rsv">%s</rect>' % (
                margin_l + c * cw, y + (ry - row_h) / 2, cw, row_h,
                "<title>端口 %s @ cycle %d（组保留）\n%s</title>" % (
                    _esc(_short_res(rows[r]["name"])), c, _esc(tx))))
    parts.append("</svg>")
    legend = '<div class="legendrow">' + "".join(
        '<span class="lg" title="%s"><i style="background:rgb(%d,%d,%d)"></i>%s</span>' % (
            _esc(TYPE_OPS.get(t, "")),
            TYPE_RGB[t][0], TYPE_RGB[t][1], TYPE_RGB[t][2], TYPE_CN[t])
        for t in ["load", "store", "vector_fp", "scalar", "mul", "branch", "memory", "other"])
    legend += ('<span class="lg"><i style="border:1.2px dashed #495057;background:none"></i>'
               '组保留（单元未定）</span></div>')
    note = ('<div class="legend">每格=该周期被占用 1 次 · 块长度=连续占用周期数 · '
            '端口类型见上方图例 · 虚线=组保留（具体单元未定）</div>'
            '<div class="mini">纵轴=有真实占用/保留的端口单元（共 %d 行），横轴=周期；'
            'hover 显示迭代号、指令、issue cycle 与该次占用的第 k/N 周期。</div>' % row_count)
    return legend + note + "<div class='scrollx'>" + "".join(parts) + "</div>"


CSS = """
*{box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;margin:0;background:#f6f7f9;color:#1c1f24}
.wrap{max-width:1500px;margin:0 auto;padding:18px}
h1{font-size:20px;margin:8px 0 2px}
.meta{color:#6b7280;font-size:13px;margin-bottom:14px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:14px 0}
.kpi{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;text-align:center}
.kpi-v{font-size:22px;font-weight:700}
.kpi-bt{color:#e03131}
.kpi-n{font-size:12px;color:#6b7280;margin-top:2px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px;margin:14px 0}
.card h2{font-size:15px;margin:0 0 10px}
.scrollx{overflow-x:auto;border:1px solid #eceef1;border-radius:6px;padding:4px;background:#fff}
.legend{margin-bottom:8px;font-size:12px;color:#374151}
.legendrow{display:flex;flex-wrap:nowrap;gap:14px;overflow-x:auto;white-space:nowrap;margin-bottom:8px;padding-bottom:4px;font-size:12px;color:#374151}
.legendrow .lg{margin-right:0;flex:0 0 auto;white-space:nowrap}
.lg{margin-right:14px;white-space:nowrap}
.lg i{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.mini{font-size:12px;color:#6b7280;margin-bottom:6px}
.err{color:#c92a2a;font-size:13px}
svg.tl text,svg.occ text,svg.res text{font-family:ui-monospace,Consolas,monospace;font-size:10px}
.grid{stroke:#e9ecef;stroke-width:1}
.rowline{stroke:#f1f3f5;stroke-width:1}
.cell{fill-opacity:0.9;stroke:#fff;stroke-width:0.5}
.hcell{stroke:#fff;stroke-width:0.5}
.hcell.rsv{fill:none;stroke:#495057;stroke-width:1.2;stroke-dasharray:3 2}
.cell-dispatch{fill:#4c6ef5}
.cell-execute,.cell-executed{fill:#2f9e44}
.cell-wait{fill:#adb5bd}
.cell-ret{fill:#f08c00}
.cell-retire_wait{fill:#ffe066}
.crit{fill:none;stroke:#e03131;stroke-width:1.5;stroke-dasharray:4 3}
.cyc,.lbl,.instr,.iterlbl{fill:#495057}
.iterlbl{font-weight:700;fill:#1971c2}
.instr{fill:#212529;font-size:11px}
.occfill{fill:rgba(76,110,245,0.18)}
.occline{fill:none;stroke:#4c6ef5;stroke-width:2}
.resbar{fill:#4c6ef5}
.resval{fill:#495057}
.crittbl{border-collapse:collapse;width:100%;font-size:12px}
.crittbl th,.crittbl td{text-align:left;border-bottom:1px solid #f1f3f5;padding:4px 8px}
.crittbl th{color:#6b7280;font-weight:600}
.dep{color:#e8590c}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}
.chip{fill:#6b7280;font-size:10px}
.tabs{display:flex;gap:8px;margin:16px 0 10px}
.tabbtn{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;color:#374151}
.tabbtn.on{background:#1c7ed6;border-color:#1c7ed6;color:#fff}
.tabpane{display:block}
details.iterg{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:8px 12px;margin:10px 0}
details.iterg>summary{cursor:pointer;font-size:13px;font-weight:700}
details.iterg .itmeta{color:#6b7280;font-weight:400;font-size:12px;margin-left:8px}
.dbars{margin-top:4px}
.dbars .dbar{display:flex;align-items:center;gap:8px;font-size:12px;margin:4px 0}
.dbar .dk{width:110px;color:#374151;flex:0 0 auto}
.dbar-bg{flex:0 0 260px;background:#f1f3f5;border-radius:4px;height:10px;overflow:hidden}
.dbar-bg i{display:block;height:10px;background:#4c6ef5}
.dbar .dv{color:#495057}
"""


def generate_html(data):
    meta = data.get("meta", {})
    summary = data.get("summary", {})
    bottleneck = data.get("bottleneck", {})
    res_pressure = data.get("resource_pressure_per_iteration", [])
    tl = data.get("timeline", {})
    critical_ids = classify_critical_indices(bottleneck)

    shown_iterations = tl.get("show_iterations", 0)
    iter_start = tl.get("iter_start", 0)
    if shown_iterations and iter_start:
        iter_note = "（跳跃热身，展示第 %d 次迭代起共 %d 次；周期已归零）" % (iter_start, shown_iterations)
    elif shown_iterations:
        iter_note = "（展示 %d 次迭代）" % shown_iterations
    else:
        iter_note = ""

    uarch_note = (" · <span class='dep'>%s</span>" % _esc(meta["uarch_note"])) if meta.get("uarch_note") else ""
    html = ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            "<title>llvm-mca 流水可视化</title><style>%s</style></head><body><div class='wrap'>"
            "<h1>LLVM MCA 流水线可视化</h1>"
            "<div class='meta'>%s → %s%s | 模式 %s%s | 迭代 %s %s | %s</div>"
            % (CSS,
               _esc(meta.get("uarch", "-")), _esc(meta.get("mcpu", "-")), uarch_note,
               _esc(meta.get("mode", "-")),
               (" · 函数 " + _esc(meta.get("function"))) if meta.get("function") else "",
               _esc(meta.get("iterations") if meta.get("iterations") is not None else "-"), iter_note,
               " | ".join(_esc(w) for w in meta.get("warnings", []))))
    # 自动检测提示突出显示
    if meta.get("uarch_note"):
        html += "<div class='mini'>⚠ %s（如需其它模型请显式 --uarch 覆盖）</div>" % _esc(meta["uarch_note"])
    html += _kpi_cards(meta, summary, bottleneck)
    if meta.get("iterations") == 1:
        html += ("<div class='mini'>⚠ 单趟口径：<b>--iterations 1</b>，KPI 为单趟（含流水填充/排空），"
                 "非稳态吞吐；瓶颈归因可能因迭代不足显示为 unknown。要看稳态指标请用 <b>--iterations 100</b>。</div>")

    dispatch_html = _dispatch_stats_html(data.get("dispatch_stalls") or {})
    html += "<div class='card'><h2>Dispatch 等待统计</h2>" + dispatch_html + "</div>"
    html += "<div class='card'><h2>瓶颈归因与关键路径</h2>" + _critical_svg(bottleneck) + "</div>"

    view = data.get("view", "all")
    tab_a = ("<div class='card'><h2>指令流水时序图（按迭代分组）</h2>"
             + _timeline_groups(tl, critical_ids, data) + "</div>"
             + "<div class='card'><h2>逐周期执行占用</h2>" + _occupancy_svg(tl, summary) + "</div>"
             + "<div class='card'><h2>读图提示</h2><div class='mini'>"
             "① 同一迭代内大段灰色(=) = 等待（MCA 不区分等寄存器/等资源，需结合关键路径与 Dispatch 统计判断）；"
             "② 红色虚线框 = 关键路径；③ 端口是否饱和请切到「端口资源热力图」；"
             "④ 跨迭代依赖看相邻迭代组里同一指令是否被推迟。</div></div>")
    tab_b = ("<div class='card'><h2>端口资源热力图（真实 issue 数据）</h2>"
             + _resource_timeline_svg(data) + "</div>"
             + "<div class='card'><h2>逐资源端口压力（每迭代聚合）</h2>"
             + _resource_svg(res_pressure) + "</div>")
    if view == "timeline":
        html += tab_a
    elif view == "resource":
        html += tab_b
    else:
        html += ("<div class='tabs'><button class='tabbtn on' data-t='A'>指令流水时序图</button>"
                 "<button class='tabbtn' data-t='B'>端口资源热力图</button></div>"
                 "<div id='tabA'>" + tab_a + "</div><div id='tabB' style='display:none'>" + tab_b + "</div>"
                 "<script>(function(){var b=document.querySelectorAll('.tabbtn');"
                 "b.forEach(function(x){x.addEventListener('click',function(){"
                 "b.forEach(function(y){y.classList.remove('on')});x.classList.add('on');"
                 "var a=document.getElementById('tabA'),c=document.getElementById('tabB');"
                 "if(a)a.style.display=(x.dataset.t==='A'?'':'none');"
                 "if(c)c.style.display=(x.dataset.t==='B'?'':'none');});});})();</script>")
    html += "</div></body></html>"

    return html


# --------------------------------------------------------------------------- #
# 微架构自动检测（从 /proc/cpuinfo 的 CPU part）
# --------------------------------------------------------------------------- #
# 各 part 对应的调度模型代号（与 UARCH_MAP 的 hip* 别名一致）
CPU_ID_TO_UARCH = {
    ("0x48", "0xd01"): "hip08",
    ("0x48", "0xd02"): "hip09",
    ("0x48", "0xd03"): "hip10",
    ("0x48", "0xd22"): "hip11",
    ("0x48", "0xd06"): "hip12",
}


def _norm_hex(v):
    v = (v or "").strip().lower()
    try:
        return "0x%x" % int(v, 16)
    except ValueError:
        return v


def parse_cpuinfo_identity(text):
    """从 /proc/cpuinfo 文本提取 (implementer, part) 组合列表。"""
    pairs = []
    impl = None
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("processor"):
            impl = None
        elif low.startswith("cpu implementer"):
            impl = _norm_hex(s.split(":", 1)[1]) if ":" in s else ""
        elif low.startswith("cpu part"):
            part = _norm_hex(s.split(":", 1)[1]) if ":" in s else ""
            pairs.append((impl, part))
    return pairs


def detect_uarch_from_cpuinfo_text(text):
    """按 implementer+part 组合识别 uarch；未知/缺失/混合一律报错。"""
    pairs = parse_cpuinfo_identity(text)
    if not pairs:
        return None, None, "/proc/cpuinfo 中没有 'CPU part'，无法检测微架构；请显式 --uarch"
    uniq = set(pairs)
    if len(uniq) > 1:
        desc = ", ".join("implementer=%s part=%s" % (i or "?", p)
                         for i, p in sorted(uniq, key=str))
        return None, desc, "检测到多个 CPU 身份（%s），无法确定单一模型；请显式 --uarch" % desc
    impl, part = next(iter(uniq))
    ident = "implementer=%s part=%s" % (impl or "?", part or "?")
    if not impl:
        return None, ident, "缺少 'CPU implementer'，无法区分相同 part 的不同厂商；请显式 --uarch"
    uarch = CPU_ID_TO_UARCH.get((impl, part))
    if not uarch:
        extra = "（ARM Cortex-A53）" if (impl, part) == ("0x41", "0xd03") else ""
        return None, ident, "识别 CPU %s%s，没有对应 hip 调度模型；请显式 --uarch（支持: %s）" % (
            ident, extra, ", ".join(sorted(set(CPU_ID_TO_UARCH.values()))))
    return uarch, ident, ""


def detect_uarch_from_cpuinfo(path="/proc/cpuinfo"):
    """从 /proc/cpuinfo 自动检测 uarch；返回 (uarch, ident, error)。"""
    if not os.path.isfile(path):
        return None, None, "当前系统无 %s（非 Linux aarch64），无法自动检测微架构；请显式 --uarch" % path
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return None, None, "读取 %s 失败: %s；请显式 --uarch" % (path, e)
    return detect_uarch_from_cpuinfo_text(text)

# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def build_data(args):
    """采集并解析，构造数据结构。返回 (data_dict, html_str_or_None)。"""
    uarch_auto = None
    if args.uarch:
        uarch = args.uarch
    elif args.parse:
        uarch = None
    else:
        auto_uarch, auto_part, auto_err = detect_uarch_from_cpuinfo()
        if auto_err:
            die("未指定 --uarch 且自动检测失败：%s。请手动指定 --uarch hip08/hip09/hip10/hip11/hip12。" % auto_err)
        uarch = auto_uarch
        uarch_auto = (auto_uarch, auto_part)
    mcpu = resolve_mcpu(uarch) if uarch else None
    mca_bin = getattr(args, "mca_bin", None) or os.environ.get("MCA_BIN") or "llvm-mca"
    input_info = {}
    asm_text = ""
    warnings = []

    tl_text = None
    if args.parse:
        if not os.path.isfile(args.parse):
            die("--parse 指定的文件不存在: %s" % args.parse)
        with open(args.parse, "r", errors="replace") as f:
            tl_text = f.read()
        input_info = {"mode": "parse", "source": args.parse, "function": None}
        mcpu = mcpu or "?"  # 仅画图模式无 mcpu
    else:
        preflight(mcpu, args.uarch, mca_bin)
        asm_text, input_info = get_assembly(args, mcpu)

        tl_extra = ["-timeline"]
        if args.timeline_max_iterations:
            tl_extra.append("--timeline-max-iterations=%d" % args.timeline_max_iterations)
        if args.timeline_max_cycles is not None:
            tl_extra.append("--timeline-max-cycles=%d" % args.timeline_max_cycles)
        elif args.timeline_max_iterations:
            tl_extra.append("--timeline-max-cycles=0")
        tl_out, terr, twarn = run_mca(asm_text, mcpu, args.iterations, tl_extra, mca_bin)
        if tl_out is None:
            die("llvm-mca (-timeline) 运行失败:\n%s" % terr)
        tl_text = tl_out
        for w in (twarn,):
            if w:
                for line in w.strip().splitlines():
                    line = line.strip()
                    if line and "warning" in line.lower() and line not in warnings:
                        warnings.append(line)

    tl = parse_timeline(tl_text)
    if args.parse and not tl.get("ok"):
        die("解析 --parse 文本失败: %s" % (tl.get("error") or "未知错误"))

    # 再跑 JSON 统计（非 --parse 模式）；-issue-trace 导出真实逐次 issue 资源
    summary, res_pressure, instructions_info, resources, dispatch_stalls = {}, [], [], [], {}
    issue_trace = {"instructions": 0, "records": []}
    bottleneck = {}
    if not args.parse:
        json_out, jerr, jwarn = run_mca(
            asm_text, mcpu, args.iterations,
            ["--json", "-all-stats", "-issue-trace"], mca_bin)
        if json_out is None:
            die("MCA --json 采集失败（--json -all-stats -issue-trace）:\n%s" % (jerr or "(无 stderr)"))
        (summary, res_pressure, instructions_info, resources,
         dispatch_stalls, issue_trace) = parse_json_stats(json_out)
        if not issue_trace.get("records"):
            die("未获取 IssueTraceView 记录，请确认 --mca-bin 为打过 IssueTraceView 补丁的 llvm-mca")
        for w in (jwarn,):
            if w:
                for line in w.strip().splitlines():
                    line = line.strip()
                    if line and "warning" in line.lower() and line not in warnings:
                        warnings.append(line)
        text_out, terr2, twarn2 = run_mca(
            asm_text, mcpu, args.iterations, ["-bottleneck-analysis"], mca_bin)
        if text_out is None:
            die("MCA 瓶颈分析失败（-bottleneck-analysis）:\n%s" % (terr2 or "(无 stderr)"))
        bottleneck = parse_bottleneck_section(text_out)
        for w in (twarn2,):
            if w:
                for line in w.strip().splitlines():
                    line = line.strip()
                    if line and "warning" in line.lower() and line not in warnings:
                        warnings.append(line)
    else:
        # —parse 仅画图：给占位统计（用户可另行传 --json 数据）
        pass

    if not args.parse and not tl.get("ok"):
        warnings.append("timeline 解析失败，已降级为资源视图: %s" % (tl.get("error") or "未知错误"))

    explicit_start = getattr(args, "start_iteration", None)
    explicit_show = getattr(args, "show_iterations", None)
    start_iter = 0 if explicit_start is None else explicit_start
    show_iter = 1 if explicit_show is None else max(1, explicit_show)
    if tl.get("ok"):
        ACTIVE_STAGES = ("dispatch", "execute", "executed", "wait", "retire", "retire_wait")
        all_inst = tl["instructions"]
        avail = sorted({x["iter"] for x in all_inst})
        # 自动口径：解析到多次迭代时跳过热身 iter0，渲染其余全部（显式参数优先）
        if explicit_start is None and explicit_show is None and len(avail) > 1:
            start_iter = avail[0] + 1
            show_iter = max(1, len([a for a in avail if a >= start_iter]))
        # 跳过热身迭代；把 start 钳制到可用迭代范围内
        if avail:
            if start_iter > avail[-1]:
                warnings.append("警告：--start-iteration %d 超出 MCA 打印的迭代范围 (0..%d)，已钳制到 %d。"
                      % (start_iter, avail[-1], avail[-1]))
                start_iter = avail[-1]
            elif start_iter not in avail:
                lower = [a for a in avail if a <= start_iter]
                start_iter = lower[-1] if lower else avail[0]
        # show 钳制到 start 之后的可用迭代数
        remaining = len([a for a in avail if a >= start_iter])
        if remaining and show_iter > remaining:
            if explicit_show is not None:
                warnings.append("警告：--show-iterations %d 超出可用迭代数，已钳制到 %d。"
                      % (show_iter, remaining))
            show_iter = remaining
        inst = [x for x in all_inst if start_iter <= x["iter"] < start_iter + show_iter]
        # 横轴归零：把展示迭代的周期减去其最小活跃周期，使单次迭代从 0 开始
        cycle_offset = 0
        if inst and not getattr(args, "keep_absolute_cycles", False):
            offset = None
            for x in inst:
                for c_str, st in x["stages"].items():
                    if st in ACTIVE_STAGES:
                        c = int(c_str)
                        if offset is None or c < offset:
                            offset = c
            if offset:
                cycle_offset = offset
                shifted = []
                for x in inst:
                    nx = dict(x)
                    nx["stages"] = {str(int(c) - offset): st for c, st in x["stages"].items()}
                    for k in ("dispatch", "exec_start", "exec_end", "retire"):
                        if x[k] is not None:
                            nx[k] = x[k] - offset
                    nx["wait_cycles"] = [c - offset for c in x["wait_cycles"]]
                    shifted.append(nx)
                inst = shifted
        tl = dict(tl)
        tl["instructions"] = inst
        tl["iter_start"] = start_iter
        tl["cycle_offset"] = cycle_offset
        tl["show_iterations"] = len({x["iter"] for x in inst})
        # 横轴只保留展示窗口内实际用到的周期（只统计非 idle 活跃阶段）
        shown_maxc = 0
        for x in inst:
            for c_str, st in x["stages"].items():
                if st not in ACTIVE_STAGES:
                    continue
                c = int(c_str)
                if c > shown_maxc:
                    shown_maxc = c
        shown_maxc = shown_maxc + 1
        C = shown_maxc
        disp = [0] * C
        exec_occ = [0] * C
        ret = [0] * C
        wait = [0] * C
        for x in inst:
            if x["dispatch"] is not None and 0 <= x["dispatch"] < C:
                disp[x["dispatch"]] += 1
            if x["retire"] is not None and 0 <= x["retire"] < C:
                ret[x["retire"]] += 1
            for c_str, st in x["stages"].items():
                c = int(c_str)
                if c < 0 or c >= C:
                    continue
                if st in ("execute", "executed"):
                    exec_occ[c] += 1
                elif st == "wait":
                    wait[c] += 1
        tl["per_cycle"] = {"dispatch": disp, "execute": exec_occ, "retire": ret, "wait": wait}
        tl["n_instructions"] = max((x["index"] for x in inst), default=0) + 1
        tl["cycles_total"] = C

        # 真实 issue 记录：只保留展示迭代，并做与 timeline 相同的周期归零
        shown_iters = {x["iter"] for x in inst}
        text_by_key = {(x["iter"], x["index"]): (x["text"] or ("#%d" % x["index"]))
                       for x in inst}
        it_records = []
        for r in (issue_trace.get("records") or []):
            try:
                it = int(r.get("Iteration", -1))
            except (TypeError, ValueError):
                continue
            if it not in shown_iters:
                continue
            rec = dict(r)
            rec["Iteration"] = it
            rec["Text"] = text_by_key.get((it, int(r.get("Index", -1))), "")
            cyc = int(r.get("Cycle", 0)) - cycle_offset
            if not _cycle_in_window(cyc, C):
                continue
            rec["Cycle"] = cyc
            kept = []
            for u in (rec.get("Resources") or []):
                visible, clipped = _clip_cycles(cyc, u.get("Cycles", 0), C)
                if visible is None:
                    continue
                u2 = dict(u)
                if clipped:
                    u2["Cycles"] = visible
                kept.append(u2)
            if not kept:
                continue
            rec["Resources"] = kept
            it_records.append(rec)
        rsv_records = []
        for r in (issue_trace.get("reserved") or []):
            try:
                it = int(r.get("Iteration", -1))
            except (TypeError, ValueError):
                continue
            if it not in shown_iters:
                continue
            rec = dict(r)
            rec["Iteration"] = it
            rec["Text"] = text_by_key.get((it, int(r.get("Index", -1))), "")
            cyc = int(r.get("Cycle", 0)) - cycle_offset
            if not _cycle_in_window(cyc, C):
                continue
            visible, clipped = _clip_cycles(cyc, rec.get("Cycles", 0), C)
            if visible is None:
                continue
            rec["Cycle"] = cyc
            if clipped:
                rec["Cycles"] = visible
            rsv_records.append(rec)
        issue_trace = {"instructions": issue_trace.get("instructions", 0) or 0,
                       "records": it_records, "reserved": rsv_records}

    # 端口明细（去重）：含类型、中文名、运算类别描述，供下游/JSON 使用
    resources_detail = []
    seen_keys = set()
    for r in resources:
        key = _canon_res(r)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        tp = _res_type(r)
        resources_detail.append({"name": r, "short": _short_res(r), "key": key,
                                 "type": tp, "type_cn": TYPE_CN.get(tp, tp),
                                 "ops": TYPE_OPS.get(tp, "")})
    if uarch_auto:
        meta_extra = {"uarch_auto": True,
                      "uarch_note": "auto-detected /proc/cpuinfo %s → %s"
                                    % (uarch_auto[1], uarch_auto[0])}
    else:
        meta_extra = {}
    data = {
        "success": True,
        "degraded": bool(not tl.get("ok")),
        "meta": dict({
            "uarch": MCPU_TO_UARCH.get(mcpu, mcpu) if mcpu and mcpu != "?" else mcpu,
            "mcpu": mcpu,
            "mode": input_info.get("mode", "?"),
            "function": input_info.get("function"),
            "iterations": args.iterations if not args.parse else None,
            "mca_bin": mca_bin,
            "issue_trace": bool(issue_trace.get("records")) if not args.parse else False,
            "timeline_ok": bool(tl.get("ok")),
            "warnings": warnings,
        }, **meta_extra),
        "summary": summary,
        "bottleneck": bottleneck,
        "resource_pressure_per_iteration": res_pressure,
        "instructions_info": instructions_info,
        "dispatch_stalls": dispatch_stalls,
        "resources": resources,
        "resources_detail": resources_detail,
        "issue_trace": issue_trace,
        "timeline": tl,
        "view": getattr(args, "view", "all"),
    }
    return data


def main():
    ap = argparse.ArgumentParser(
        description="llvm-mca 流水线可视化器（鲲鹏 HiSilicon 微架构）")
    ap.add_argument("--check-env", action="store_true",
                    help="仅检查 llvm-mca 对各 hip 微架构的支持情况（含 -issue-trace 能力）")
    ap.add_argument("--mca-bin", default=None, metavar="PATH",
                    help="llvm-mca 可执行文件（须打过 IssueTraceView 补丁、支持 -issue-trace）。"
                         "默认取环境变量 MCA_BIN，否则用 PATH 中的 llvm-mca")
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--asm", metavar="FILE|-", help="纯汇编文本或 .s 文件（'-' 读 stdin）")
    g.add_argument("--source", metavar="FILE", help="C/C++ 源码（需配合 --function）")
    g.add_argument("--binary", metavar="FILE", help="二进制/目标文件（需配合 --function）")
    g.add_argument("--parse", metavar="TIMELINE.txt",
                   help="只解析/画图给定 -timeline 文本，不调用 llvm-mca（离线测试）")
    ap.add_argument("--function", help="目标函数名（--source/--binary 模式必填）")
    ap.add_argument("--uarch",
                    help="微架构: hip08/0xd01, hip09/0xd02, hip10/0xd03, hip11/0xd22, hip12/0xd06")
    ap.add_argument("--iterations", type=int, default=30,
                    help="MCA 仿真迭代次数（默认 30，稳态吞吐口径；单趟流水分析用 --iterations 1）")
    ap.add_argument("--start-iteration", type=int, default=None,
                    help="从第几次迭代开始画（默认自动：多迭代时跳过 iter0，从 1 开始）")
    ap.add_argument("--show-iterations", type=int, default=None,
                    help="时序图展示的迭代数（默认自动：多迭代时展示除 iter0 外的全部）")
    ap.add_argument("--timeline-max-iterations", type=int, default=None, metavar="N",
                    help="透传 llvm-mca --timeline-max-iterations（默认 6；> --iterations 会被钳制）")
    ap.add_argument("--timeline-max-cycles", type=int, default=None, metavar="N",
                    help="透传 llvm-mca --timeline-max-cycles（0=不限；默认 MCA 的 80 周期，"
                         "若给了 --timeline-max-iterations 则自动传 0 避免截断）")
    ap.add_argument("--keep-absolute-cycles", action="store_true",
                    help="保留 MCA 原始绝对周期（默认会把展示迭代的周期归零到 0）")
    ap.add_argument("--out", metavar="FILE.html",
                    help="输出 HTML 文件路径（默认写 llvm-mca-visualize-demo-<uarch>-iter5.html）")
    ap.add_argument("--json", action="store_true",
                    help="强制输出 JSON 数据契约到 stdout（不写默认 HTML）")
    ap.add_argument("--html", action="store_true", help="输出 HTML 到 stdout")
    ap.add_argument("--view", default="all", choices=["all", "timeline", "resource"],
                    help="HTML 视图：all=全部（默认），timeline=仅指令流水时序图，resource=仅资源×周期占用图")
    ap.add_argument("--cflags", nargs=argparse.REMAINDER, default=[],
                    help="透传给 clang 的额外编译参数（--source 模式）")
    args = ap.parse_args()

    if args.check_env:
        check_env(args.mca_bin or os.environ.get("MCA_BIN") or "llvm-mca")
        return
    if not (args.asm or args.source or args.binary or args.parse):
        die("必须提供 --asm / --source+--function / --binary+--function / --parse 之一")
    if (args.source or args.binary) and not args.function:
        die("--source/--binary 模式必须提供 --function")
    inputs = [p for p in (args.asm, args.source, args.binary, args.parse)
              if p and p != "-"]
    if args.out:
        for p in inputs:
            if _same_file(args.out, p):
                die("--out 与输入文件相同，拒绝覆盖: %s" % args.out)
    if args.source and args.cflags:
        bad_flag = validate_extra_cflags(args.cflags)
        if bad_flag:
            die(bad_flag)

    if args.timeline_max_iterations is None and not args.parse and args.iterations > 1:
        args.timeline_max_iterations = min(6, args.iterations)
    if args.timeline_max_iterations is not None:
        if args.timeline_max_iterations < 1:
            die("--timeline-max-iterations 必须 >= 1")
        if not args.parse and args.timeline_max_iterations > args.iterations:
            _warn("警告：--timeline-max-iterations %d > --iterations %d，已钳制到 %d。"
                  % (args.timeline_max_iterations, args.iterations, args.iterations))
            args.timeline_max_iterations = args.iterations
    if args.timeline_max_cycles is not None and args.timeline_max_cycles < 0:
        die("--timeline-max-cycles 必须 >= 0（0 表示不限）")

    data = build_data(args)

    if args.html:
        sys.stdout.write(generate_html(data))
    elif args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        out = args.out
        if not out:
            uarch = (data.get("meta") or {}).get("uarch") or ""
            if not uarch or uarch == "?":
                uarch = "uarch"
            out = "llvm-mca-visualize-demo-%s-iter5.html" % uarch
        for p in inputs:
            if _same_file(out, p):
                die("--out 与输入文件相同，拒绝覆盖: %s" % out)
        html = generate_html(data)
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print("已写入 %s" % out)

if __name__ == "__main__":
    main()
