#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 静态检查参考实现（kptools-tpsplit-optimization）

对生成的 TP 拓扑部署脚本做拉起前静态验证，不依赖 NPU 环境（现场实测方法论固化）：

  1. 实例脚本清点与识别（含 vllm serve 且挂 --kv-transfer-config 的 .sh）
  2. KV_TRANSFER_CONFIG 抽取 + json.loads 校验
  3. 守恒自检：prefill.dp_size == 实例总数（--expect-dp 指定期望值则按期望值核对）
  4. TP / 后端 / DP 旗标核验（TP 值；无 --distributed-executor-backend；DP 组网四旗标）
  5. KV 旧字段保留核验（engine_id / kv_port / lookup_rpc_port / decode 段按 legacy 基线；
     现场基线不同时 --no-keep-legacy 关闭，以现场旧脚本实际值为准）
  6. 同机卡互斥（ASCEND_RT_VISIBLE_DEVICES）与 http 端口错开
  7. 实例间 diff：剥离注释并遮蔽实例级差异（rank/卡/http 端口）与组级差异
     （DP 组大小/主节点 IP）后，各实例应完全一致；混合分组（如 4+4+2）用
     --group-size 4,2 声明合法组大小

用法：
  python l0_static_check.py <脚本目录> [--expect-dp 20] [--tp 8] [--group-size 4] [--rpc-port 16600]

退出码 0 = 全部通过；非 0 = 存在失败项（逐项打印 PASS/FAIL 与证据）。
现场另一种等价形态：stub 掉 vllm 可执行文件捕获实际命令行（无需 NPU 拉起），
再按本脚本同样口径检查；两种形态的检查项一致。
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


def extract_kvs(text: str, v: dict) -> list:
    """抽取 KV_TRANSFER_CONFIG。先在全文替换 bash '\"$VAR\"' 拼接习语再抽取，
    避免习语内引号截断匹配。函数形参占位为 <ARG:var>（由调用点人工核对）。"""
    text = re.sub(r"'\"\$(\w+)\"'",
                  lambda mm: v.get(mm.group(1), '<ARG:%s>' % mm.group(1)), text)
    out = []
    for m in re.finditer(r"KV_TRANSFER_CONFIG\s*=\s*'(.*?)'", text, re.S):
        kv = resolve(m.group(1), v)
        try:
            out.append(json.loads(kv))
        except Exception as e:
            out.append({"__parse_error__": str(e)})
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


def host_key(path: str, outdir: str) -> str:
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
    m = re.search(r'(\d+)', fn)
    if m:
        return 'host-%d' % (int(m.group(1)) // 2)
    return 'host-?'


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('dir', help='部署脚本目录（递归扫描）')
    ap.add_argument('--expect-dp', type=int, default=None,
                    help='期望的 Mooncake prefill dp_size（缺省=按实例总数守恒核对）')
    ap.add_argument('--tp', type=int, default=8, help='期望 TP size（默认 8）')
    ap.add_argument('--group-size', default='4',
                    help='期望 DP 组大小，单值或多值逗号分隔（如 "4" 或 "4,2" 支持混合分组，默认 4）')
    ap.add_argument('--rpc-port', type=int, default=16600, help='期望 DP rpc 端口（默认 16600）')
    ap.add_argument('--keep-legacy', action=argparse.BooleanOptionalAction, default=True,
                    help='核验 KV 旧字段保留（engine_id=0/kv_port=30000/lookup=10001/decode 64-1，'
                         '按 legacy 基线核验；现场基线不同时用 --no-keep-legacy 关闭，'
                         '以现场旧脚本实际值为准）')
    args = ap.parse_args()
    allowed_sizes = {int(x) for x in str(args.group_size).split(',')}

    outdir = os.path.abspath(args.dir)
    scripts = instance_scripts(outdir)
    results = []          # (name, check, passed, evidence)

    def record(name, check, passed, evidence):
        results.append((name, check, passed, evidence))

    n_inst = 0
    per_host = {}
    masked_bodies = []
    dp_collected = []
    for p, raw in scripts:
        name = os.path.basename(p)
        t = strip_comments(raw)
        v = parse_vars(t)
        host = host_key(p, outdir)

        # TP 值
        tp = flag_value(t, 'tensor-parallel-size', v)
        record(name, 'TP=%d' % args.tp, tp == str(args.tp), 'got %r' % tp)

        # 后端旗标：新方案不写 --distributed-executor-backend
        record(name, 'no-executor-backend',
               not has_flag(t, 'distributed-executor-backend'),
               'ray backend' if has_flag(t, 'distributed-executor-backend') else 'ok')

        # DP 组网四旗标
        dps = flag_value(t, 'data-parallel-size', v)
        dpp = flag_value(t, 'data-parallel-rpc-port', v)
        dp_ok = (dps is not None and int(dps) in allowed_sizes
                 and dpp == str(args.rpc_port)
                 and has_flag(t, 'data-parallel-rank') and has_flag(t, 'data-parallel-address'))
        record(name, 'dp-flags(size=%s,rpc=%d,rank,addr)' % (args.group_size, args.rpc_port),
               dp_ok, 'size=%s rpc=%s rank=%s addr=%s' % (
                   dps, dpp, has_flag(t, 'data-parallel-rank'), has_flag(t, 'data-parallel-address')))

        # KV 校验
        kvs = extract_kvs(t, v)
        if not kvs:
            record(name, 'kv-extract', False, 'no KV config parsed')
        for kv in kvs:
            n_inst += 1
            if not isinstance(kv, dict) or '__parse_error__' in kv:
                record(name, 'kv-json', False, str(kv.get('__parse_error__') if isinstance(kv, dict) else kv)[:80])
                continue
            record(name, 'kv-json', True, 'json.loads ok')
            try:
                conns = kv['kv_connector_extra_config']['connectors']
                mc = [c for c in conns if c.get('kv_connector') == 'MooncakeConnectorV1'][0]
                pre = mc['kv_connector_extra_config']['prefill']
                dec = mc['kv_connector_extra_config']['decode']
                pdp, ptp = str(pre['dp_size']), str(pre['tp_size'])
                dp_collected.append(pdp)
                record(name, 'kv-prefill-tp', ptp == str(args.tp), 'tp=%s' % ptp)
                if args.keep_legacy:
                    asc = [c for c in conns if c.get('kv_connector') == 'AscendStoreConnector']
                    lp = str(asc[0]['kv_connector_extra_config'].get('lookup_rpc_port')) if asc else None
                    legacy_ok = (str(kv.get('engine_id')) == '0' and str(mc.get('kv_port')) == '30000'
                                 and (lp is None or lp == '10001')
                                 and str(dec['dp_size']) == '64' and str(dec['tp_size']) == '1')
                    # 注：decode 64/1 与 engine_id/kv_port 旧值按 legacy 基线核验，
                    # 现场基线不同时以 --no-keep-legacy 关闭并以现场旧脚本为准
                    record(name, 'kv-legacy-fields', legacy_ok,
                           'engine_id=%s kv_port=%s lookup=%s decode=%s/%s' % (
                               kv.get('engine_id'), mc.get('kv_port'), lp, dec['dp_size'], dec['tp_size']))
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

    # 守恒自检：所有实例 prefill.dp_size 一致；未指定期望值时还须等于实例总数
    if args.expect_dp is not None:
        record('(global)', 'kv-prefill-dp==%d' % args.expect_dp,
               set(dp_collected) == {str(args.expect_dp)},
               'dp values=%s' % sorted(set(dp_collected)))
    else:
        record('(global)', 'kv-dp-conservation(dp==n_inst)',
               set(dp_collected) == {str(n_inst)},
               'dp values=%s, instances=%d' % (sorted(set(dp_collected)), n_inst))
    record('(global)', 'instance-count=%d' % n_inst, n_inst > 0, 'found %d instances' % n_inst)

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
