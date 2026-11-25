#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 静态检查参考实现（kptools-tpsplit-optimization）

对生成的 TP 拓扑部署脚本做拉起前静态验证，不依赖 NPU 环境（现场实测方法论固化）：

  1. 实例脚本清点与识别（含 vllm serve 且挂 --kv-transfer-config 的 .sh）
  2. 变量解析：脚本正文 + source/. 引用的 env 文件（SKILL.md 要求保留的 env
     文件传参形态可直接校验：env 文件放脚本同目录自动解析，或 --env-file 显式指定）；
     仍解析不到的变量（$VAR / <ARG:var>）对应检查项降级为 SKIP，不判 FAIL
  3. KV_TRANSFER_CONFIG 抽取 + json.loads 校验：正文赋值 / --kv-transfer-config
     实参（含 '"$VAR"' 习语与 "$VAR" 变量）按序追溯到 env 文件中的完整定义，
     脚本只 source env 并传参的形态同样可解析，不误报 no KV config parsed
  4. dp_size 守恒自检（两种执行口径）：--expect-dp <全局实例数> 核对全局守恒
     （全量目录执行）；缺省只核各实例 prefill.dp_size 一致、不与本机实例数比较
     （按主机分目录执行）
  5. TP 核验：--tp <目标TP> 显式核对；缺省按脚本实际 --tensor-parallel-size 做
     组内一致性检查，不写死默认值
  6. KV 旧字段核验：期望值取自 --legacy-baseline <现场旧脚本>（engine_id /
     kv_port / lookup_rpc_port / decode 段），不内置写死基线；未提供基线时该项 SKIP
  7. 同机卡互斥与 http 端口错开：主机分组显式化——子目录/文件名 host<N>/node<N>
     模式、--instances-per-host N、--hosts h1=N1,h2=N2 任选其一；都无法判定时按
     单文件独立分组并输出告警（不再按"文件名首个数字 //2"猜每机 2 实例）
  8. 实例间 diff：遮蔽实例级差异（rank/卡/http 端口）与组级差异（DP 组大小/
     主节点 IP）后应完全一致；混合分组（如 4+4+2）用 --group-size 4,2 声明
  9. DP rank 组级核对：解析实际 --data-parallel-rank 值，同组（--data-parallel-address
     相同）内核对取值范围 [0,组大小) 与组内唯一；全量目录执行（--expect-dp）且无
     未解析成员时再核每组 rank 齐全——rank 在实例 diff 中被遮蔽，重复/越界只有
     此检查能拦截
 10. 全量目录执行的实例数核对：--expect-dp 给定时实例清单必须与布局表全局实例数
     一致，漏分发/重复分发都 FAIL（所有配置都写同一 dp_size 时，dp 集合比较
     发现不了缺实例）

用法：
  python l0_static_check.py <脚本目录> [--expect-dp 20] [--tp 8] [--group-size 4]
      [--rpc-port 16600] [--legacy-baseline <现场旧脚本>] [--env-file <env文件>]...
      [--instances-per-host 2 | --hosts h1=2,h2=2]

退出码 0 = 全部通过（SKIP 不计失败）；非 0 = 存在失败项（逐项打印 PASS/FAIL/SKIP
与证据）。单个脚本解析异常只记一条 FAIL，不影响其余脚本逐项结果输出。
"""
import argparse
import json
import os
import re
import sys


def strip_comments(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        if re.match(r'^\s*#', ln):
            continue
        idx = ln.find(' #')
        if idx != -1:
            prefix = ln[:idx]
            if prefix.count('"') % 2 == 0 and prefix.count("'") % 2 == 0:
                ln = prefix.rstrip()
        lines.append(ln)
    return '\n'.join(lines)


def parse_vars(text: str) -> dict:
    v = {}
    for m in re.finditer(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$', text, re.M):
        val = m.group(2).strip()
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'") and len(val) >= 2:
            val = val[1:-1]
        v[m.group(1)] = val
    return v


def resolve(s: str, v: dict) -> str:
    return re.sub(r'\$\{(\w+)\}|\$(\w+)',
                  lambda m: v.get(m.group(1) or m.group(2), m.group(0)), s)


def is_unresolved(s) -> bool:
    """值仍含 $VAR / <ARG:var> 占位 → 变量未解析（env 文件缺失等），对应检查项应 SKIP。"""
    return s is None or ('$' in s) or ('<ARG:' in s)


def parse_env_sources(text: str, script_dir: str, v: dict):
    """解析脚本内 source/. 引用的 env 文件，返回 (变量表, 本地未找到的 source 目标)。
    相对路径相对脚本目录解析；脚本正文定义优先于 env 文件（由调用方合并）。"""
    merged = {}
    missing = []
    for m in re.finditer(r'^\s*(?:source|\.)\s+([^\s#]+)', text, re.M):
        t = m.group(1).strip('\'"')
        t = resolve(t, v)
        if is_unresolved(t):
            missing.append(t)
            continue
        cand = t if os.path.isabs(t) else os.path.join(script_dir, t)
        if not os.path.isfile(cand):
            missing.append(t)
            continue
        try:
            et = open(cand, encoding='utf-8', errors='replace').read()
        except OSError:
            missing.append(t)
            continue
        merged.update(parse_vars(strip_comments(et)))
    return merged, missing


def extract_kvs(text: str, v: dict) -> list:
    """抽取 KV_TRANSFER_CONFIG，按序追溯三类定义形态：
    1) 启动脚本正文赋值 `KV_TRANSFER_CONFIG='...'`（自包含形态）；
    2) --kv-transfer-config 实参为已解析字面量（含 bash '"$VAR"' 拼接习语替换后
       的结果）时直接取实参内容；
    3) 实参为 `"$VAR"`/`$VAR` 变量时追到 v 表（脚本正文或 source 的 env 文件中的
       完整定义）再取值校验——脚本只 source env 并传参、正文无赋值的形态也能
       解析，不误报 no KV config parsed。
    变量未解析时记 <ARG:var> 并标 __unresolved__，由调用方降级为 SKIP 而不是
    json.loads 失败。"""
    text = re.sub(r"'\"\$(\w+)\"'",
                  lambda mm: v.get(mm.group(1), '<ARG:%s>' % mm.group(1)), text)
    cands = [m.group(1) for m in re.finditer(r"KV_TRANSFER_CONFIG\s*=\s*'(.*?)'", text, re.S)]
    if not cands:
        m = re.search(r'--kv-transfer-config[ =]+\'([^\']*)\'', text)
        if m and m.group(1).strip():
            arg = m.group(1).strip()
            if '<ARG:' in arg or re.search(r'\$\w', arg):
                return [{'__unresolved__': sorted(set(
                    re.findall(r'<ARG:(\w+)>', arg) or re.findall(r'\$(\w+)', arg)))}]
            cands.append(arg)
        else:
            m2 = re.search(r'--kv-transfer-config[ =]+"?\$?\{?(\w+)\}?"?', text)
            if m2:
                var = m2.group(1)
                if v.get(var):
                    cands.append(v[var])
                elif '$' in m2.group(0):
                    return [{'__unresolved__': [var]}]
    out = []
    for raw in cands:
        kv = resolve(raw, v)
        if '<ARG:' in kv or re.search(r'\$\w', kv):
            out.append({'__unresolved__': sorted(set(
                re.findall(r'<ARG:(\w+)>', kv) or re.findall(r'\$(\w+)', kv)))})
            continue
        try:
            out.append(json.loads(kv))
        except Exception as e:
            out.append({'__parse_error__': str(e)})
    return out


def flag_value(text: str, flag: str, v: dict):
    m = re.search(r'--%s[ =](\S+)' % re.escape(flag), text)
    if not m:
        return None
    return resolve(m.group(1).strip().strip('"'), v).split()[0] if m.group(1) else None


def has_flag(text: str, flag: str) -> bool:
    return bool(re.search(r'--%s\b' % re.escape(flag), text))


def expand_devices(dev: str):
    out = set()
    for part in re.split(r'[,\s"]+', (dev or '').strip()):
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-')[:2]
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                return None
        else:
            try:
                out.add(int(part))
            except ValueError:
                return None
    return out or None


def host_key(path: str, outdir: str):
    """子目录/文件名中的 host<N>/node<N> 模式 → 主机键；无模式返回 None。"""
    rel = os.path.relpath(path, outdir)
    parts = re.split(r'[\\/]+', rel)
    for part in parts[:-1]:
        m = re.search(r'(?:host|node)[-_]?(\d+)', part, re.I)
        if m:
            return 'host-%s' % m.group(1)
    fn = parts[-1]
    m = re.search(r'(?:host|node)[-_]?(\d+)', fn, re.I)
    if m:
        return 'host-%s' % m.group(1)
    return None


def natural_key(p: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', p)]


def assign_host_keys(paths, outdir: str, args, warn: list) -> dict:
    """主机分组：--hosts h1=N1,... > --instances-per-host N > host<N> 模式；
    都没有 → 单文件独立分组并告警（不猜"每机 2 实例"）。"""
    ordered = sorted(paths, key=natural_key)
    keys = {}
    if args.hosts:
        spec = []
        for part in str(args.hosts).split(','):
            if '=' not in part:
                warn.append('--hosts 条目 %r 缺少 =count' % part)
                continue
            h, n = part.split('=', 1)
            try:
                spec.extend([h.strip()] * int(n))
            except ValueError:
                warn.append('--hosts 条目 %r 数量非数字' % part)
        if len(spec) != len(ordered):
            warn.append('--hosts 实例总数 %d != 脚本数 %d，多余条目忽略、不足按单文件分组'
                        % (len(spec), len(ordered)))
        it = iter(spec)
        for p in ordered:
            keys[p] = next(it, 'file:%s' % os.path.relpath(p, outdir))
    elif args.instances_per_host:
        n = args.instances_per_host if args.instances_per_host >= 1 else 1
        for i, p in enumerate(ordered):
            keys[p] = 'host-%d' % (i // n)
        if len(ordered) % n:
            warn.append('脚本数 %d 不是 --instances-per-host %d 的整数倍，最后一组不满'
                        % (len(ordered), n))
    else:
        for p in ordered:
            k = host_key(p, outdir)
            if k is None:
                rel = os.path.relpath(p, outdir)
                keys[p] = 'file:%s' % rel
                warn.append('%s 主机归属无法判定（目录/文件名无 host<N> 模式）：按单文件独立'
                            '分组，同机卡互斥/端口检查未覆盖；请用 --instances-per-host N、'
                            '--hosts h1=N1,... 或每主机一个子目录明确分组' % rel)
            else:
                keys[p] = k
    return keys


def instance_scripts(outdir: str) -> list:
    scripts = []
    for root, _, files in os.walk(outdir):
        for f in files:
            if not f.endswith('.sh'):
                continue
            p = os.path.join(root, f)
            try:
                t = open(p, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            if 'vllm serve' in t and 'kv-transfer-config' in t:
                scripts.append((p, t))
    return scripts


def mask_varying(text: str, v: dict) -> str:
    """遮蔽实例间允许差异的项，供实例间 diff。
    允许差异：DP rank / 绑定卡 / http 端口（实例级），DP 主节点 IP（组级）。
    旗标位置与承载它们的变量定义行一并遮蔽（按名字模式识别，兼容各现场命名）；
    kv_port/engine_id 等旧字段的一致性由 kv-legacy-fields 检查负责，不靠 diff。"""
    text = re.sub(r'(--data-parallel-rank[ =]?"?)\S+', r'\1<X>', text)
    text = re.sub(r'(--data-parallel-address[ =]?"?)\S+', r'\1<X>', text)
    text = re.sub(r'(--port[ =]?"?)\$?\w+', r'\1<X>', text)
    text = re.sub(r'(ASCEND_RT_VISIBLE_DEVICES=)("\$?\w+"|\S+)', r'\1<X>', text)
    text = re.sub(r'(--data-parallel-size[ =]?"?)\$?\w+', r'\1<X>', text)
    text = re.sub(r'^\s*(?:export\s+)?[A-Za-z_]*(RANK|DEVICES|MASTER|ADDR|GROUP_SIZE)[A-Za-z_]*=.*$',
                  '<X>', text, flags=re.M)
    text = re.sub(r'^\s*(?:export\s+)?HTTP_PORT=.*$', '<X>', text, flags=re.M)
    return text


def legacy_expect(baseline_path: str):
    """从现场旧脚本解析 KV 旧字段期望值（engine_id/kv_port/lookup_rpc_port/decode dp/tp）。"""
    try:
        raw = open(baseline_path, encoding='utf-8', errors='replace').read()
    except OSError as e:
        return None, '无法读取 --legacy-baseline: %s' % e
    t = strip_comments(raw)
    v = parse_vars(t)
    env_vars, _ = parse_env_sources(t, os.path.dirname(os.path.abspath(baseline_path)) or '.', v)
    v = {**env_vars, **v}
    kvs = [k for k in extract_kvs(t, v)
           if isinstance(k, dict) and '__unresolved__' not in k and '__parse_error__' not in k]
    if not kvs:
        return None, '--legacy-baseline 中未解析出有效 KV_TRANSFER_CONFIG'
    kv = kvs[0]
    try:
        conns = kv['kv_connector_extra_config']['connectors']
        mc = [c for c in conns if c.get('kv_connector') == 'MooncakeConnectorV1'][0]
        asc = [c for c in conns if c.get('kv_connector') == 'AscendStoreConnector']
        lp = str(asc[0]['kv_connector_extra_config'].get('lookup_rpc_port')) if asc else None
        dec = mc['kv_connector_extra_config']['decode']
        return {
            'engine_id': str(kv.get('engine_id')),
            'kv_port': str(mc.get('kv_port')),
            'lookup': lp,
            'decode_dp': str(dec['dp_size']),
            'decode_tp': str(dec['tp_size']),
        }, None
    except Exception as e:
        return None, '--legacy-baseline KV 结构不完整: %s' % e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('dir', help='部署脚本目录（递归扫描）')
    ap.add_argument('--expect-dp', type=int, default=None,
                    help='期望的 Mooncake prefill dp_size（全局实例总数）：全量目录执行时核对'
                         '全局守恒；按主机分目录执行时不要给，缺省只核各实例 dp_size 一致')
    ap.add_argument('--tp', type=int, default=None,
                    help='期望 TP size（显式核对）；缺省按脚本实际 --tensor-parallel-size '
                         '做组内一致性检查，不写死默认值')
    ap.add_argument('--group-size', default='4',
                    help='期望 DP 组大小，单值或多值逗号分隔（如 "4" 或 "4,2" 支持混合分组，默认 4）')
    ap.add_argument('--rpc-port', type=int, default=16600, help='期望 DP rpc 端口（默认 16600）')
    ap.add_argument('--keep-legacy', action=argparse.BooleanOptionalAction, default=True,
                    help='核验 KV 旧字段保留：期望值来自 --legacy-baseline 现场旧脚本，不内置'
                         '写死基线；未提供基线时该项 SKIP；--no-keep-legacy 显式关闭')
    ap.add_argument('--legacy-baseline', default=None,
                    help='现场旧启动脚本路径：engine_id/kv_port/lookup_rpc_port/decode 段期望值'
                         '从该脚本解析')
    ap.add_argument('--env-file', action='append', default=[],
                    help='env 文件路径（可重复）：脚本 source 的 env 文件不在脚本目录时显式补充')
    ap.add_argument('--instances-per-host', type=int, default=None,
                    help='每机实例数：脚本按自然排序后每 N 个归同一主机')
    ap.add_argument('--hosts', default=None,
                    help='显式主机分组，如 "h1=2,h2=2"（按自然排序顺序分配，支持混合布局）')
    args = ap.parse_args()
    allowed_sizes = {int(x) for x in str(args.group_size).split(',')}

    outdir = os.path.abspath(args.dir)
    scripts = instance_scripts(outdir)
    results = []

    def record(name, check, passed, evidence):
        results.append((name, check, passed, evidence))

    # 全局 env 文件（--env-file）：最低优先级，随后被脚本 source 的 env 文件与脚本正文覆盖
    global_env = {}
    for ef in args.env_file:
        if os.path.isfile(ef):
            try:
                global_env.update(parse_vars(strip_comments(
                    open(ef, encoding='utf-8', errors='replace').read())))
            except OSError as e:
                record('(global)', 'env-file', False, '读取失败 %s: %s' % (ef, e))
        else:
            record('(global)', 'env-file', False, 'not found: %s' % ef)

    # 主机分组（显式优先；无模式则单文件独立分组并告警）
    group_warn = []
    keys = assign_host_keys([p for p, _ in scripts], outdir, args, group_warn)
    record('(global)', 'host-grouping', True, '; '.join(group_warn) if group_warn else 'ok')

    # legacy 期望值（来自现场旧脚本，不写死）
    baseline = None
    if args.keep_legacy:
        if not args.legacy_baseline:
            record('(global)', 'kv-legacy-fields', True,
                   'SKIP: 未提供 --legacy-baseline（期望值应取自现场旧脚本，不写死基线）；'
                   '关闭该项用 --no-keep-legacy')
        else:
            baseline, err = legacy_expect(args.legacy_baseline)
            if err:
                record('(global)', 'legacy-baseline', False, err)

    n_inst = 0
    per_host = {}
    masked_bodies = []
    dp_collected = []
    tp_collected = []
    dp_rank_info = []       # (文件名, data-parallel-address 实值, group size, rank 实值)
    dp_rank_degraded = False  # 任一实例 rank/addr/size 未解析 → 组齐全核对降级 SKIP
    for p, raw in scripts:
        name = os.path.basename(p)
        try:
            t = strip_comments(raw)
            v0 = parse_vars(t)
            env_vars, missing_sources = parse_env_sources(t, os.path.dirname(p), v0)
            v = {**global_env, **env_vars, **v0}
            if missing_sources:
                record(name, 'env-sources', True,
                       'SKIP: source 文件本地未找到 %s（把 env 文件放脚本同目录或用 --env-file '
                       '指定；相关变量将按未解析降级为 SKIP）' % sorted(set(missing_sources)))
            host = keys.get(p, 'file:%s' % name)

            # TP 值：--tp 显式核对；缺省收集做组内一致性
            tp = flag_value(t, 'tensor-parallel-size', v)
            if tp is None:
                record(name, 'TP flag', False, 'no --tensor-parallel-size found')
            elif is_unresolved(tp):
                record(name, 'TP check', True,
                       'SKIP: %r 变量未解析（env 文件缺失？放同目录或 --env-file）' % tp)
            elif args.tp is not None:
                record(name, 'TP=%d' % args.tp, tp == str(args.tp), 'got %r' % tp)
                tp_collected.append(tp)
            else:
                tp_collected.append(tp)

            # 后端旗标：新方案不写 --distributed-executor-backend
            record(name, 'no-executor-backend',
                   not has_flag(t, 'distributed-executor-backend'),
                   'ray backend' if has_flag(t, 'distributed-executor-backend') else 'ok')

            # DP 组网四旗标（未解析变量降级 SKIP，非数字按 FAIL 记证据不抛异常）
            dps = flag_value(t, 'data-parallel-size', v)
            dpp = flag_value(t, 'data-parallel-rpc-port', v)
            has_rank = has_flag(t, 'data-parallel-rank')
            has_addr = has_flag(t, 'data-parallel-address')
            dpr = flag_value(t, 'data-parallel-rank', v)
            dpa = flag_value(t, 'data-parallel-address', v)
            if (dps is not None and is_unresolved(dps)) or (dpp is not None and is_unresolved(dpp)):
                record(name, 'dp-flags(size=%s,rpc=%d,rank,addr)' % (args.group_size, args.rpc_port),
                       True, 'SKIP: size=%r rpc=%r 变量未解析（env 文件缺失？--env-file）；'
                       'rank=%r addr=%r' % (dps, dpp, dpr, dpa))
            else:
                size_ok = False
                if dps is not None:
                    try:
                        size_ok = int(dps) in allowed_sizes
                    except ValueError:
                        size_ok = False
                dp_ok = (size_ok and dpp == str(args.rpc_port) and has_rank and has_addr)
                ev = 'size=%s rpc=%s rank=%s addr=%s' % (dps, dpp, dpr, dpa)
                if dps is not None and not dps.lstrip('-').isdigit():
                    ev += '（size 非数字，按 FAIL 记录）'
                record(name, 'dp-flags(size=%s,rpc=%d,rank,addr)' % (args.group_size, args.rpc_port),
                       dp_ok, ev)
            # 解析出的 rank 实值进入组级核对（见 main 尾部 dp-rank-* 检查）；
            # 未解析的成员标记降级——rank 在 instance-diff 中被遮蔽，重复/越界只有组级检查能拦
            if ((dpr is not None and is_unresolved(dpr)) or (has_rank and dpr is None)
                    or (dpa is not None and is_unresolved(dpa)) or (has_addr and dpa is None)
                    or (dps is not None and is_unresolved(dps))):
                dp_rank_degraded = True
            if (dpr is not None and not is_unresolved(dpr)
                    and dpa is not None and not is_unresolved(dpa)
                    and dps is not None and not is_unresolved(dps)):
                dp_rank_info.append((name, str(dpa), str(dps), str(dpr)))

            # KV 校验（未解析变量降级 SKIP）
            kvs = extract_kvs(t, v)
            if not kvs:
                record(name, 'kv-extract', False, 'no KV config parsed')
            for kv in kvs:
                if isinstance(kv, dict) and '__unresolved__' in kv:
                    n_inst += 1
                    record(name, 'kv-json', True,
                           'SKIP: 含未解析变量 %s（env 文件未随脚本提供？放同目录或 --env-file），'
                           'JSON 校验跳过' % kv['__unresolved__'])
                    continue
                if not isinstance(kv, dict) or '__parse_error__' in kv:
                    record(name, 'kv-json', False,
                           str(kv.get('__parse_error__') if isinstance(kv, dict) else kv)[:80])
                    continue
                n_inst += 1
                record(name, 'kv-json', True, 'json.loads ok')
                try:
                    conns = kv['kv_connector_extra_config']['connectors']
                    mc = [c for c in conns if c.get('kv_connector') == 'MooncakeConnectorV1'][0]
                    pre = mc['kv_connector_extra_config']['prefill']
                    dec = mc['kv_connector_extra_config']['decode']
                    pdp, ptp = str(pre['dp_size']), str(pre['tp_size'])
                    dp_collected.append(pdp)
                    if is_unresolved(ptp):
                        record(name, 'kv-prefill-tp', True, 'SKIP: %r 变量未解析' % ptp)
                    elif args.tp is not None:
                        record(name, 'kv-prefill-tp', ptp == str(args.tp), 'tp=%s' % ptp)
                        tp_collected.append(ptp)
                    else:
                        tp_collected.append(ptp)
                    if args.keep_legacy and baseline:
                        asc = [c for c in conns if c.get('kv_connector') == 'AscendStoreConnector']
                        lp = str(asc[0]['kv_connector_extra_config'].get('lookup_rpc_port')) if asc else None
                        # 先核对新旧 connector 存在性一致，再核 lookup_rpc_port：
                        # 旧配置含 AscendStoreConnector 而新配置误删时 lp=None，
                        # 只比较端口号会直接放行——旧有新无（或旧无新有）必须 FAIL
                        conn_ok = (len(asc) > 0) == (baseline['lookup'] is not None)
                        lookup_ok = conn_ok and (lp is None or baseline['lookup'] is None
                                                  or lp == baseline['lookup'])
                        legacy_ok = (str(kv.get('engine_id')) == baseline['engine_id']
                                      and str(mc.get('kv_port')) == baseline['kv_port']
                                      and lookup_ok
                                      and str(dec['dp_size']) == baseline['decode_dp']
                                      and str(dec['tp_size']) == baseline['decode_tp'])
                        record(name, 'kv-legacy-fields', legacy_ok,
                               'engine_id=%s kv_port=%s ascend=%s lookup=%s decode=%s/%s（基线 %s/%s/%s/%s/%s）' % (
                                   kv.get('engine_id'), mc.get('kv_port'),
                                   'present' if asc else 'missing', lp, dec['dp_size'], dec['tp_size'],
                                   baseline['engine_id'], baseline['kv_port'], baseline['lookup'],
                                   baseline['decode_dp'], baseline['decode_tp']))
                except Exception as e:
                    record(name, 'kv-structure', False, str(e)[:80])

            # 卡 / 端口
            calls = re.findall(r'launch_instance\s+(\S+)\s+(\S+)\s+"([^"]+)"\s+(\S+)\s+(\S+)\s+(\S+)', t)
            if calls:
                for c in calls:
                    per_host.setdefault(host, []).append(
                        {'file': name, 'dev': c[2], 'port': c[3]})
            else:
                dm = re.search(r'ASCEND_RT_VISIBLE_DEVICES=(\S+)', t)
                dev = resolve(dm.group(1).strip('"'), v) if dm else None
                port = flag_value(t, 'port', v)
                per_host.setdefault(host, []).append({'file': name, 'dev': dev, 'port': port})

            masked_bodies.append(mask_varying(t, v))
        except Exception as e:
            record(name, 'script-check', False, 'exception: %r' % e)

    # 守恒自检：--expect-dp 核对全局守恒；缺省只核各实例 dp_size 一致（不与本机实例数比较）
    if args.expect_dp is not None:
        record('(global)', 'kv-prefill-dp==%d' % args.expect_dp,
               set(dp_collected) == {str(args.expect_dp)},
               'dp values=%s' % sorted(set(dp_collected)))
        # 全量目录执行：实例清单对照布局表全局实例数——漏分发（少）/重复分发（多）都 FAIL，
        # 不能只要求 >0（每份配置都写 dp_size=全局值时，dp 集合比较发现不了缺实例）
        cnt_ok = n_inst == args.expect_dp
        if n_inst < args.expect_dp:
            cnt_ev = 'found %d instances，缺 %d 个（漏分发？对照 2.1 布局表逐台核对）' % (
                n_inst, args.expect_dp - n_inst)
        elif n_inst > args.expect_dp:
            cnt_ev = 'found %d instances，多 %d 个（重复分发/多余脚本？对照 2.1 布局表）' % (
                n_inst, n_inst - args.expect_dp)
        else:
            cnt_ev = 'found %d instances（与布局表全局实例数一致）' % n_inst
        record('(global)', 'instance-count==%d(布局表)' % args.expect_dp, cnt_ok, cnt_ev)
    else:
        hint = ''
        if dp_collected and set(dp_collected) and dp_collected[0] != str(n_inst):
            hint = ('（目录内实例数与 dp_size 不等：按主机分目录执行属正常——全局守恒请用 '
                    '--expect-dp <全局实例数>；全量目录执行请核对布局表全局实例数）')
        record('(global)', 'kv-dp-consistent', len(set(dp_collected)) <= 1,
               'dp values=%s, instances=%d%s' % (sorted(set(dp_collected)), n_inst, hint))
        record('(global)', 'instance-count=%d' % n_inst, n_inst > 0, 'found %d instances' % n_inst)

    # DP rank 组级核对（rank 在 instance-diff 中被遮蔽，重复/越界只有此检查能拦）：
    # 同组（--data-parallel-address 相同）内解析实际 rank，核对取值范围 [0, 组大小)
    # 与组内唯一；全量目录执行（--expect-dp）且无未解析成员时再核每组 rank 齐全。
    dp_groups = {}
    for gname, gaddr, gdps, grank in dp_rank_info:
        if not gdps.lstrip('-').isdigit():
            dp_rank_degraded = True
            continue
        dp_groups.setdefault(gaddr, []).append((gname, int(gdps), grank))
    for gaddr, members in sorted(dp_groups.items()):
        gkey = '(dp-group %s)' % gaddr
        names = sorted(n for n, _, _ in members)
        sizes = sorted({s for _, s, _ in members})
        ranks = [r for _, _, r in members]
        if len(sizes) > 1:
            record(gkey, 'dp-rank-group(size-unique)', False,
                   '组内 size 不一致: %s（files=%s）' % (sizes, names))
            continue
        size = sizes[0]
        range_ok = all(str(r).lstrip('-').isdigit() and 0 <= int(r) < size for r in ranks)
        unique_ok = len(set(ranks)) == len(ranks)
        ev = 'size=%d ranks=%s files=%s' % (size, sorted(ranks), names)
        if args.expect_dp is not None:
            if dp_rank_degraded:
                record(gkey, 'dp-rank-range-unique-complete', True,
                       'SKIP: 存在 rank/addr/size 未解析实例，齐全核对降级'
                       '（补 env 文件后复查）；' + ev)
            else:
                complete_ok = (len(members) == size
                               and sorted(int(r) for r in set(ranks)) == list(range(size)))
                record(gkey, 'dp-rank-range-unique-complete',
                       range_ok and unique_ok and complete_ok,
                       ev + ('；组内 rank 齐全' if complete_ok
                             else '；组内 rank 不齐（缺号或成员数不足）'))
        else:
            record(gkey, 'dp-rank-range-unique', range_ok and unique_ok,
                   ev + '（按主机分目录：只核范围与唯一；齐全核对需全量目录 --expect-dp）')

    # TP 组内一致性（--tp 未显式给出时）
    if args.tp is None:
        tp_set = sorted({x for x in tp_collected if x is not None and not is_unresolved(x)})
        record('(global)', 'tp-group-consistent', len(tp_set) <= 1,
               'tp values=%s（--tp 未指定：按脚本实际值做组内一致性核对；显式核对用 --tp <目标TP>）'
               % tp_set)

    # 同机卡互斥 / 端口错开
    for h, inst in sorted(per_host.items()):
        for i in range(len(inst)):
            for j in range(i + 1, len(inst)):
                d0, d1 = expand_devices(inst[i]['dev']), expand_devices(inst[j]['dev'])
                ok = (d0 is not None and d1 is not None and not (d0 & d1))
                record('%s[%s/%s]' % (h, inst[i]['file'], inst[j]['file']),
                       'cards-disjoint', ok, '%r vs %r' % (inst[i]['dev'], inst[j]['dev']))
                pok = (inst[i]['port'] and inst[j]['port']
                       and inst[i]['port'] != inst[j]['port'])
                record('%s[%s/%s]' % (h, inst[i]['file'], inst[j]['file']),
                       'http-port-distinct', bool(pok), '%r vs %r' % (inst[i]['port'], inst[j]['port']))

    # 实例间 diff：遮蔽三处差异后应一致（仅独立脚本形态；函数/驱动形态结构不同则跳过）
    uniq = set(masked_bodies)
    record('(global)', 'instance-diff(masked)', len(uniq) <= 1,
           '%d distinct bodies after masking rank/dev/port' % len(uniq))

    fails = [r for r in results if not r[2]]
    for name, check, passed, evidence in results:
        print('%s  %-28s %-24s %s' % ('PASS' if passed else 'FAIL', name, check, evidence))
    print('---')
    print('total=%d pass=%d fail=%d' % (len(results), len(results) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
