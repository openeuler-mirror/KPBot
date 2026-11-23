#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_eplb.py -- Configure (or check) EPLB in a vLLM prefill (P) node launch template.

Tested with vLLM 0.32 / DeepSeek-V4.1 P/D disaggregated deployments.

Usage:
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode dynamic
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode force
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode off
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode check
    python3 apply_eplb.py --template ... --mode dynamic \
        --redundant-experts 8 --window-size 500 --step-interval 500
    python3 apply_eplb.py --template ... --mode force --dry-run

Modes (mutually exclusive target states):
    off      baseline: V2 model runner, no DYNAMIC_EPLB, no EPLB flags,
             no enable_force_eplb
    dynamic  V2 model runner + export DYNAMIC_EPLB="true" + --enable-eplb
             + --eplb-config.* flags   (优化手段)
    force    V1 model runner (VLLM_USE_V2_MODEL_RUNNER=0) + unset DYNAMIC_EPLB
             + "enable_force_eplb":true merged into the existing
             --additional-config JSON   (均衡性能参照，不作为上线优化手段)
    check    read-only: report which EPLB state the template is currently in

Safety behavior:
  * Refuses to touch a decode (D) node template (detected via kv_consumer).
  * Refuses templates without a `vllm serve` command.
  * Makes a timestamped backup (.bak-eplb-<mode>-<YYYYmmdd-HHMMSS>) before writing.
  * Merges into the existing --additional-config JSON instead of adding a
    second flag; all pre-existing keys are preserved.
  * Idempotent: re-running with the same mode produces no further changes.
  * Runs `bash -n` on the result and verifies the target state before exiting 0.
"""

import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import subprocess
import sys

RE_V2 = re.compile(r'^(\s*)export\s+VLLM_USE_V2_MODEL_RUNNER\s*=.*$')
RE_DYN_EXPORT = re.compile(r'^\s*export\s+DYNAMIC_EPLB\s*=')
RE_DYN_UNSET = re.compile(r'^\s*unset\s+DYNAMIC_EPLB\b')
RE_EPLB_FLAG = re.compile(r'^\s*(--enable-eplb\b|--eplb-config\.)')
RE_EP_LINE = re.compile(r'^(\s*)--enable-expert-parallel\b.*$')
RE_SERVE_LINE = re.compile(r'^\s*vllm\s+serve\b')
RE_AC_SINGLE = re.compile(r"^(\s*)--additional-config\s+'(\{.*\})'\s*(\\?)\s*$")
RE_AC_OPEN = re.compile(r"^(\s*)--additional-config\s+'\{(.*)$")
RE_AC_CLOSE = re.compile(r"^\s*\}'(.*)$")

def die(msg, code=1):
    print(msg)
    sys.exit(code)

def find_additional_config(lines):
    """Locate the --additional-config '{ ... }' block.

    Returns (start, end, json_text, base_indent, suffix, single_line) or None.
    suffix is what followed the closing }' on the closing line (usually ' \\').
    """
    for i, line in enumerate(lines):
        if '--additional-config' not in line:
            continue
        m_single = RE_AC_SINGLE.match(line)
        if m_single:
            base, jtext, cont = m_single.group(1), m_single.group(2), m_single.group(3)
            suffix = (' \\' if cont else '')
            return i, i, jtext, base, suffix, True
        m_open = RE_AC_OPEN.match(line)
        if m_open:
            base = m_open.group(1)
            body = [m_open.group(2)]  # content after '{ on the opening line
            for j in range(i + 1, len(lines)):
                m_close = RE_AC_CLOSE.match(lines[j])
                if m_close:
                    suffix = m_close.group(1)
                    jtext = '{' + '\n'.join(body) + '}'
                    return i, j, jtext, base, suffix, False
                body.append(lines[j])
            return None  # unterminated block
        return None  # unexpected format
    return None

def transform(src, mode, redundant, window, interval):
    """Return the new file content for the target mode, or raise ValueError."""
    lines = src.splitlines()
    v2_value = '0' if mode == 'force' else '1'
    out = []
    v2_seen = False
    serve_idx = None

    # Locate anchor line indexes first (needed if V2 line is missing).
    for idx, line in enumerate(lines):
        if RE_SERVE_LINE.match(line):
            serve_idx = idx
            break
    if serve_idx is None:
        raise ValueError("no `vllm serve` command found -- is this a vLLM launch template?")

    ep_found = any(RE_EP_LINE.match(l) for l in lines)

    def eplb_flag_lines(indent):
        return [
            '%s--enable-eplb \\' % indent,
            '%s--eplb-config.num_redundant_experts %d \\' % (indent, redundant),
            '%s--eplb-config.window_size %d \\' % (indent, window),
            '%s--eplb-config.step_interval %d \\' % (indent, interval),
        ]

    for line in lines:
        if RE_DYN_EXPORT.match(line) or RE_DYN_UNSET.match(line):
            continue
        if RE_EPLB_FLAG.match(line):
            continue
        m_v2 = RE_V2.match(line)
        if m_v2:
            indent = m_v2.group(1)
            out.append('%sexport VLLM_USE_V2_MODEL_RUNNER=%s' % (indent, v2_value))
            v2_seen = True
            if mode == 'dynamic':
                out.append('%sexport DYNAMIC_EPLB="true"' % indent)
            elif mode == 'force':
                out.append('%sunset DYNAMIC_EPLB' % indent)
            continue
        m_ep = RE_EP_LINE.match(line)
        if m_ep and mode == 'dynamic':
            indent = m_ep.group(1)
            if not line.rstrip().endswith('\\'):
                out.append(line + ' \\')
            else:
                out.append(line)
            out.extend(eplb_flag_lines(indent))
            continue
        out.append(line)

    if mode == 'dynamic' and not ep_found:
        raise ValueError("no --enable-expert-parallel flag found; cannot anchor "
                         "--enable-eplb flags (expert parallel is a prerequisite for EPLB)")

    # Insert V2 runner export before `vllm serve` if the template lacked it.
    if not v2_seen:
        indent = re.match(r'^(\s*)', lines[serve_idx]).group(1)
        new_lines = ['%sexport VLLM_USE_V2_MODEL_RUNNER=%s' % (indent, v2_value)]
        if mode == 'dynamic':
            new_lines.append('%sexport DYNAMIC_EPLB="true"' % indent)
        elif mode == 'force':
            new_lines.append('%sunset DYNAMIC_EPLB' % indent)
        out = out[:serve_idx] + new_lines + out[serve_idx:]

    # Merge enable_force_eplb into the --additional-config JSON block.
    ac = find_additional_config(out)
    if ac is not None:
        start, end, jtext, base, suffix, single = ac
        try:
            obj = json.loads(jtext)
        except ValueError as e:
            raise ValueError("cannot parse existing --additional-config JSON: %s" % e)
        if mode == 'force':
            obj['enable_force_eplb'] = True
        else:
            obj.pop('enable_force_eplb', None)
        if single:
            dumped = json.dumps(obj, separators=(',', ':'))
            out[start] = "%s--additional-config '%s'%s" % (base, dumped, suffix)
        else:
            dumped = json.dumps(obj, indent=4, separators=(',', ':'))
            # drop the outer braces: the flag line supplies '{ and the closing
            # line supplies }'
            inner = dumped.splitlines()[1:-1]
            body = [(base + ln) if ln else ln for ln in inner]
            out[start:end + 1] = ["%s--additional-config '{" % base] + body + \
                ["%s}'%s" % (base, suffix)]
    elif mode == 'force':
        raise ValueError("no --additional-config block found to merge "
                         '"enable_force_eplb":true into -- add it manually near '
                         'the other flags, do NOT create a second --additional-config')

    return '\n'.join(out) + '\n'

def verify(text, mode, redundant, window, interval):
    """Check the target state; return (problems, n_checks). Empty problems == OK."""
    problems = []
    checks = {
        'off': [
            ('export VLLM_USE_V2_MODEL_RUNNER=1' in text,
             'VLLM_USE_V2_MODEL_RUNNER must be 1'),
            (not re.search(r'^\s*export\s+DYNAMIC_EPLB\s*=', text, re.M),
             'no export DYNAMIC_EPLB line'),
            (not re.search(r'^\s*unset\s+DYNAMIC_EPLB\b', text, re.M),
             'no unset DYNAMIC_EPLB line'),
            (not re.search(r'^\s*--enable-eplb\b', text, re.M),
             'no --enable-eplb flag'),
            (not re.search(r'^\s*--eplb-config\.', text, re.M),
             'no --eplb-config.* flags'),
            ('enable_force_eplb' not in text, 'no enable_force_eplb key'),
        ],
        'dynamic': [
            ('export VLLM_USE_V2_MODEL_RUNNER=1' in text,
             'VLLM_USE_V2_MODEL_RUNNER must be 1'),
            ('export DYNAMIC_EPLB="true"' in text, 'export DYNAMIC_EPLB="true" present'),
            ('--enable-eplb \\' in text, '--enable-eplb flag present'),
            ('--eplb-config.num_redundant_experts %d \\' % redundant in text,
             '--eplb-config.num_redundant_experts %d present' % redundant),
            ('--eplb-config.window_size %d \\' % window in text,
             '--eplb-config.window_size %d present' % window),
            ('--eplb-config.step_interval %d \\' % interval in text,
             '--eplb-config.step_interval %d present' % interval),
            ('enable_force_eplb' not in text, 'no enable_force_eplb key'),
        ],
        'force': [
            ('export VLLM_USE_V2_MODEL_RUNNER=0' in text,
             'VLLM_USE_V2_MODEL_RUNNER must be 0'),
            (re.search(r'^\s*unset\s+DYNAMIC_EPLB\b', text, re.M),
             'unset DYNAMIC_EPLB present'),
            (not re.search(r'^\s*export\s+DYNAMIC_EPLB\s*=', text, re.M),
             'no export DYNAMIC_EPLB line'),
            (not re.search(r'^\s*--enable-eplb\b', text, re.M),
             'no --enable-eplb flag'),
            (not re.search(r'^\s*--eplb-config\.', text, re.M),
             'no --eplb-config.* flags'),
            ('"enable_force_eplb":true' in text,
             '"enable_force_eplb":true merged into --additional-config'),
        ],
    }
    n_ac = len(re.findall(r'--additional-config', text))
    n_checks = 0
    for ok, msg in checks[mode]:
        n_checks += 1
        if not ok:
            problems.append(msg)
    n_checks += 1
    if n_ac != 1:
        problems.append('expected exactly one --additional-config flag, found %d' % n_ac)
    return problems, n_checks

MODE_DESC = {
    'off': '关闭 EPLB（基线：V2 runner，无 DYNAMIC_EPLB，无 eplb 参数）',
    'dynamic': '动态 EPLB（V2 runner + DYNAMIC_EPLB="true" + --enable-eplb + eplb-config 参数）',
    'force': '强制 EPLB（V2=0 + unset DYNAMIC_EPLB + additional-config 里 enable_force_eplb:true）',
}

def check_state(src, path):
    """Read-only EPLB state report for a launch template."""
    print('模板: %s' % path)
    if 'kv_consumer' in src and 'kv_producer' not in src:
        print('节点角色: D（decode）节点模板 —— EPLB 只配置在 P（prefill）节点，本文件无需 EPLB 配置')
        return 0
    markers = {
        'V2 runner=1': re.search(r'^\s*export\s+VLLM_USE_V2_MODEL_RUNNER=1\s*$', src, re.M) is not None,
        'V2 runner=0': re.search(r'^\s*export\s+VLLM_USE_V2_MODEL_RUNNER=0\s*$', src, re.M) is not None,
        'export DYNAMIC_EPLB': re.search(r'^\s*export\s+DYNAMIC_EPLB\s*=', src, re.M) is not None,
        'unset DYNAMIC_EPLB': re.search(r'^\s*unset\s+DYNAMIC_EPLB\b', src, re.M) is not None,
        '--enable-eplb': re.search(r'^\s*--enable-eplb\b', src, re.M) is not None,
        '--eplb-config.*': re.search(r'^\s*--eplb-config\.', src, re.M) is not None,
        '"enable_force_eplb":true': '"enable_force_eplb":true' in src,
    }
    n_ac = len(re.findall(r'--additional-config', src))
    for k, v in markers.items():
        print('  %-28s %s' % (k, '有' if v else '无'))
    print('  --additional-config 数量      %d%s' % (n_ac, '' if n_ac == 1 else '  <-- 异常，应为 1'))
    dyn = markers['V2 runner=1'] and markers['export DYNAMIC_EPLB'] and markers['--enable-eplb'] \
        and markers['--eplb-config.*'] and not markers['"enable_force_eplb":true']
    force = markers['V2 runner=0'] and markers['unset DYNAMIC_EPLB'] and \
        markers['"enable_force_eplb":true'] and not markers['export DYNAMIC_EPLB'] \
        and not markers['--enable-eplb']
    off = markers['V2 runner=1'] and not markers['export DYNAMIC_EPLB'] \
        and not markers['--enable-eplb'] and not markers['--eplb-config.*'] \
        and not markers['"enable_force_eplb":true']
    if dyn:
        print('状态: dynamic EPLB（优化模式）')
        return 0
    if force:
        print('状态: force EPLB（均衡性能参照，勿作为上线优化手段）')
        return 0
    if off:
        print('状态: 关闭 EPLB（基线）')
        return 0
    print('状态: 不明确/混合 —— 存在互相矛盾的 EPLB 标记，建议用 --mode off|dynamic|force 之一修正')
    return 1

def main():
    ap = argparse.ArgumentParser(description='Configure EPLB in a vLLM prefill-node launch template')
    ap.add_argument('--template', required=True, help='path to run_dp_template.sh (P node)')
    ap.add_argument('--mode', required=True, choices=['off', 'dynamic', 'force', 'check'])
    ap.add_argument('--redundant-experts', type=int, default=16,
                    help='eplb-config.num_redundant_experts (default 16)')
    ap.add_argument('--window-size', type=int, default=1000,
                    help='eplb-config.window_size (default 1000)')
    ap.add_argument('--step-interval', type=int, default=1000,
                    help='eplb-config.step_interval (default 1000)')
    ap.add_argument('--backup-tag', default=None,
                    help='extra tag in the backup filename (default: eplb-<mode>)')
    ap.add_argument('--dry-run', action='store_true', help='show diff, do not write')
    args = ap.parse_args()

    path = args.template
    if not os.path.isfile(path):
        die('ERROR: template not found: %s' % path)
    if os.path.islink(path):
        print('NOTE: %s is a symlink, editing its target %s' % (path, os.path.realpath(path)))
    with open(path) as f:
        src = f.read()

    if args.mode == 'check':
        sys.exit(check_state(src, path))

    # Safety: EPLB is only configured on the prefill (P) node template.
    if 'kv_consumer' in src and 'kv_producer' not in src:
        die("ERROR: %s looks like a decode (D) node template (contains kv_consumer).\n"
            "EPLB is only configured on the prefill (P) node's run_dp_template.sh.\n"
            "Refusing to edit." % path, 2)

    try:
        new = transform(src, args.mode, args.redundant_experts,
                        args.window_size, args.step_interval)
    except ValueError as e:
        die('ERROR: %s' % e)

    problems, n_checks = verify(new, args.mode, args.redundant_experts,
                                args.window_size, args.step_interval)

    print('目标模式: %s' % MODE_DESC[args.mode])
    print('')
    diff = list(difflib.unified_diff(src.splitlines(), new.splitlines(),
                                     fromfile=path + ' (before)',
                                     tofile=path + ' (after)', lineterm=''))
    print('\n'.join(diff))
    print('')

    if problems:
        print('VERIFY FAILED:')
        for p in problems:
            print('  - %s' % p)
        sys.exit(1)

    if new == src:
        print('模板已经是目标状态，无需修改（幂等）。')
        return

    if args.dry_run:
        print('dry-run: 未写入文件，未创建备份。')
        return

    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    tag = args.backup_tag or ('eplb-%s' % args.mode)
    # keep the tag path-safe: it becomes part of a filename next to the template
    tag = re.sub(r'[^A-Za-z0-9._-]', '_', tag)
    bak = '%s.bak-%s-%s' % (path, tag, ts)
    shutil.copy2(path, bak)

    # atomic replace: an interrupted write must never leave a half-written launcher
    tmp_path = '%s.tmp-write' % path
    try:
        with open(tmp_path, 'w') as f:
            f.write(new)
        shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # bash syntax check on the result
    try:
        r = subprocess.run(['bash', '-n', path], capture_output=True, text=True)
        if r.returncode != 0:
            print('WARNING: bash -n reported a syntax error (restoring backup):')
            print(r.stderr)
            shutil.copy2(bak, path)
            sys.exit(1)
    except OSError:
        print('NOTE: bash not available, skipped syntax check')

    print('OK: 已写入 %s' % path)
    print('OK: 备份保存至 %s' % bak)
    print('OK: bash -n 语法检查通过；目标状态校验通过（%d 项）' % n_checks)
    print('')
    print('下一步（由用户在对应节点手动执行，不要代跑）:')
    print('  P节点: docker exec -it <container> bash; cd <P脚本目录>; bash start_mooncake.sh; bash start_p0.sh')
    print('  D节点: docker exec -it <container> bash; cd <D脚本目录>; bash start_d0.sh')
    print('  代理:  cd <P脚本目录>; bash start_proxy.sh')
    print('  压测注意: EPLB 开启后第 1 轮压测是预热离群值，需剔除（建议跑 4 轮取后 3 轮均值）')

if __name__ == '__main__':
    main()
