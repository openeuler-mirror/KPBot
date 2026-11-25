#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_eplb.py -- Configure (or check) EPLB in a vLLM prefill (P) node launch template.

Target environment: the site-internal vLLM fork described in SKILL.md.
Two switches (`DYNAMIC_EPLB`, `enable_force_eplb`) are deployment-specific and
do not exist in upstream vLLM; on stock vLLM the dynamic/force knobs are inert.

Usage:
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode dynamic \
        --ep-size 16
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode force
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode off
    python3 apply_eplb.py --template /path/to/run_dp_template.sh --mode check
    python3 apply_eplb.py --template ... --mode dynamic \
        --ep-size 16 --redundant-experts 16 --window-size 1000 --step-interval 1000
    python3 apply_eplb.py --template ... --mode force --dry-run

Modes (mutually exclusive target states):
    off      baseline: no DYNAMIC_EPLB, no EPLB flags, no enable_force_eplb.
             Only rewrites VLLM_USE_V2_MODEL_RUNNER when the live (non-comment)
             config actually carried EPLB markers; a plain/V1 template -- or one
             that merely mentions the knob in a comment -- is left untouched.
    dynamic  V2 model runner + export DYNAMIC_EPLB="true" + --enable-eplb
             + --eplb-config.* flags   (优化手段)
    force    V1 model runner (VLLM_USE_V2_MODEL_RUNNER=0) + unset DYNAMIC_EPLB
             + "enable_force_eplb":true merged into the existing
             --additional-config JSON   (均衡性能参照，不作为上线优化手段)
    check    read-only: report which EPLB state the template is currently in

Safety behavior:
  * Only edits a template whose --kv-transfer-config JSON declares
    "kv_role":"kv_producer" (the positive P-node criterion). Decode templates
    and templates without a role are refused, not guessed.
  * Refuses templates without a `vllm serve` command.
  * Refuses EPLB flags that share a line with other serve arguments, because
    removing them would silently delete neighbouring flags. A boolean switch
    (--eplb-config.log_balancedness) followed by another flag is treated as a
    shared line and refused, not parsed as "value".
  * Requires --ep-size for --mode dynamic and validates/rounds
    --redundant-experts against it before writing (no unverified value on disk).
  * Inserts EPLB flags *before* the --enable-expert-parallel anchor and never
    rewrites that line, so re-running dynamic or switching to off/force cannot
    leave a dangling backslash that swallows the next script line; verify()
    rejects any output whose serve command merged a following shell command.
  * Symlinked templates are resolved first, so the real file is edited.
  * Makes a timestamped backup (.bak-eplb-<mode>-<YYYYmmdd-HHMMSS>) before writing.
  * Merges into the existing --additional-config JSON instead of adding a
    second flag; all pre-existing keys are preserved.
  * Idempotent: re-running with the same mode produces no further changes.
  * Writes through a unique temp file + os.replace (no fixed temp name).
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
import tempfile

RE_V2 = re.compile(r'^(\s*)export\s+VLLM_USE_V2_MODEL_RUNNER\s*=.*$')
RE_DYN_EXPORT = re.compile(r'^\s*export\s+DYNAMIC_EPLB\s*=')
RE_DYN_UNSET = re.compile(r'^\s*unset\s+DYNAMIC_EPLB\b')
RE_EPLB_FLAG = re.compile(r'^(\s*)(--enable-eplb\b|--eplb-config\.)')
RE_EP_LINE = re.compile(r'^(\s*)--enable-expert-parallel\b.*$')
RE_SERVE_LINE = re.compile(r'^\s*vllm\s+serve\b')
RE_AC_SINGLE = re.compile(r"^(\s*)--additional-config\s+'(\{.*\})'\s*(\\?)\s*$")
RE_AC_OPEN = re.compile(r"^(\s*)--additional-config\s+'\{(.*)$")
RE_AC_CLOSE = re.compile(r"^\s*\}'(.*)$")

# eplb-config keys that are boolean switches (take no value)
EPLB_BOOL_KEYS = ('log_balancedness',)

# lines that start a new shell command; seeing one inside the continued serve
# command means a dangling backslash swallowed the next script line
_SHELL_CMD_RE = re.compile(
    r'^\s*(bash|sh|python3?|docker|nohup|cd|source|export|unset|echo|sleep|\.)\b')


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def find_json_block(lines, flag):
    """Locate a `--flag '{...}'` JSON block.

    Returns (start, end, json_text, base_indent, suffix, single_line) or None.
    suffix is what followed the closing }' on the closing line (usually ' \\').
    """
    single_re = re.compile(r"^(\s*)" + re.escape(flag) + r"\s+'(\{.*\})'\s*(\\?)\s*$")
    open_re = re.compile(r"^(\s*)" + re.escape(flag) + r"\s+'\{(.*)$")
    close_re = re.compile(r"^\s*\}'(.*)$")
    for i, line in enumerate(lines):
        if flag not in line:
            continue
        m_single = single_re.match(line)
        if m_single:
            base, jtext, cont = m_single.group(1), m_single.group(2), m_single.group(3)
            suffix = (' \\' if cont else '')
            return i, i, jtext, base, suffix, True
        m_open = open_re.match(line)
        if m_open:
            base = m_open.group(1)
            body = [m_open.group(2)]  # content after '{ on the opening line
            for j in range(i + 1, len(lines)):
                m_close = close_re.match(lines[j])
                if m_close:
                    suffix = m_close.group(1)
                    jtext = '{' + '\n'.join(body) + '}'
                    return i, j, jtext, base, suffix, False
                body.append(lines[j])
            return None  # unterminated block
        return None  # unexpected format
    return None


def node_role(src):
    """Return (role, error). role is 'kv_producer' / 'kv_consumer' / None."""
    block = find_json_block(src.splitlines(), '--kv-transfer-config')
    if block is None:
        return None, 'no --kv-transfer-config block found'
    try:
        obj = json.loads(block[2])
    except ValueError as e:
        return None, 'cannot parse --kv-transfer-config JSON: %s' % e
    return obj.get('kv_role'), None


def find_serve(lines):
    for i, line in enumerate(lines):
        if RE_SERVE_LINE.match(line):
            return i
    return None


def _serve_swallowed_command(lines):
    """Return a shell-command line wrongly merged into the serve command, or None.

    Walks the `vllm serve` command honouring single-quoted JSON blocks and
    backslash continuations. A line inside the command that starts a new shell
    command means a dangling continuation swallowed the next script line.
    """
    start = find_serve(lines)
    if start is None:
        return None
    in_squote = False
    i = start
    while i < len(lines):
        line = lines[i]
        if line.count("'") % 2 == 1:
            in_squote = not in_squote
        if i > start and not in_squote and _SHELL_CMD_RE.match(line):
            return line
        if not in_squote and not line.rstrip().endswith('\\'):
            break
        i += 1
    return None


def _is_single_eplb_arg(body):
    """True only if `body` is exactly one standalone EPLB serve arg.

    A value-taking `--eplb-config.<key>` must be followed by a value that is not
    another flag; a boolean key (e.g. log_balancedness) must not take a value.
    Anything else means the EPLB arg shares its line with other arguments.
    """
    if body == '--enable-eplb':
        return True
    m = re.fullmatch(r'--eplb-config\.([A-Za-z0-9_]+)(?:\s+(\S+))?', body)
    if not m:
        return False
    key, val = m.group(1), m.group(2)
    if key in EPLB_BOOL_KEYS:
        # boolean switch: a following token is another argument, not its value
        return val is None
    # value key: require an explicit value that is not another flag
    return val is not None and not val.startswith('-')


def strip_eplb_args(lines):
    """Remove standalone EPLB serve args; refuse args sharing a line with others."""
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#'):
            out.append(line)
            continue
        if '--enable-eplb' not in line and '--eplb-config.' not in line:
            out.append(line)
            continue
        body = stripped[:-1].rstrip() if stripped.endswith('\\') else stripped
        if _is_single_eplb_arg(body):
            continue
        raise ValueError(
            'EPLB 参数没有独占一行（%r）。为避免误删同一行的其它参数（例如 --port，'
            '或布尔开关 --eplb-config.log_balancedness 后跟的另一个 flag），'
            '请先把 EPLB 参数拆成独立行再运行。' % stripped)
    return out


def _has_effective_eplb_markers(lines):
    """True only when live (non-comment) config actually carries EPLB markers.

    Comments mentioning DYNAMIC_EPLB / --eplb-config must not count, otherwise
    `--mode off` would flip the V2 runner on a V1 baseline just because a comment
    names the knob.
    """
    code = '\n'.join(ln for ln in lines if not ln.lstrip().startswith('#'))
    return bool(re.search(
        r'^\s*export\s+DYNAMIC_EPLB\s*='
        r'|^\s*unset\s+DYNAMIC_EPLB\b'
        r'|^\s*--enable-eplb\b'
        r'|^\s*--eplb-config\.'
        r'|"enable_force_eplb"\s*:\s*true',
        code, re.M))


def _serve_indent(lines, serve_idx):
    return re.match(r'^(\s*)', lines[serve_idx]).group(1)


def _first_index(lines, regex):
    for i, line in enumerate(lines):
        if re.match(regex, line):
            return i
    return None


def _export_has_extra(line, name):
    """True if an `export NAME=...` line also assigns other variables."""
    body = line.strip()
    m = re.match(r'^export\s+', body)
    if not m:
        return False
    rest = body[m.end():]
    rest2 = re.sub(r'^%s\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)' % re.escape(name), '', rest, count=1).strip()
    return bool(rest2)


def _force_enabled(src):
    """Read `enable_force_eplb` as a real JSON boolean, not a substring."""
    block = find_json_block(src.splitlines(), '--additional-config')
    if block is None:
        return False
    try:
        obj = json.loads(block[2])
    except ValueError:
        return False
    return obj.get('enable_force_eplb') is True


def _repair_serve_continuation(lines):
    """Drop a dangling backslash that would swallow the next shell command.

    Only touches the line immediately before a line that starts a new shell
    command while the serve block is still open; a properly terminated command
    is left untouched.
    """
    start = find_serve(lines)
    if start is None:
        return lines
    out = list(lines)
    in_squote = False
    i = start
    while i < len(out):
        line = out[i]
        if line.count("'") % 2 == 1:
            in_squote = not in_squote
        if i > start and not in_squote and _SHELL_CMD_RE.match(line):
            prev = i - 1
            if out[prev].rstrip().endswith('\\'):
                out[prev] = out[prev].rstrip()[:-1].rstrip()
            break
        if not in_squote and not line.rstrip().endswith('\\'):
            break
        i += 1
    return out



def transform(src, mode, redundant, window, interval):
    """Return the new file content for the target mode, or raise ValueError."""
    lines = src.splitlines()
    serves = [ln for ln in lines if RE_SERVE_LINE.match(ln)]
    if not serves:
        raise ValueError("no `vllm serve` command found -- is this a vLLM launch template?")
    if len(serves) > 1:
        raise ValueError("found %d `vllm serve` commands; refusing to edit a multi-serve template "
                         "(role/EP/EPLB flags would not be bound to one producer command)" % len(serves))

    has_markers = _has_effective_eplb_markers(lines)

    # Drop existing DYNAMIC_EPLB lines and standalone EPLB args first.
    out = []
    for line in lines:
        if RE_DYN_EXPORT.match(line):
            if _export_has_extra(line, 'DYNAMIC_EPLB'):
                raise ValueError("DYNAMIC_EPLB 与其它变量写在同一 export 行（%r）；"
                                 "请先拆成独立行，避免切换模式时误删其它赋值" % line.strip())
            continue
        if re.match(r'^\s*unset\s+DYNAMIC_EPLB\s*$', line):
            continue
        if re.match(r'^\s*unset\s+DYNAMIC_EPLB\s+\S', line):
            raise ValueError("unset DYNAMIC_EPLB 与其它变量写在同一行（%r）；请先拆成独立行" % line.strip())
        out.append(line)
    out = strip_eplb_args(out)
    out = _repair_serve_continuation(out)

    serve_idx = find_serve(out)
    indent = _serve_indent(out, serve_idx)

    def set_v2(value):
        nonlocal out, serve_idx
        idx = _first_index(out, RE_V2)
        line = '%sexport VLLM_USE_V2_MODEL_RUNNER=%s' % (
            re.match(r'^(\s*)', out[idx]).group(1) if idx is not None else indent, value)
        if idx is not None:
            if _export_has_extra(out[idx], 'VLLM_USE_V2_MODEL_RUNNER'):
                raise ValueError("VLLM_USE_V2_MODEL_RUNNER 与其它变量写在同一 export 行（%r）；"
                                 "请先拆成独立行" % out[idx].strip())
            out[idx] = line
        else:
            serve_idx = find_serve(out)
            out.insert(serve_idx, line)
            idx = serve_idx
        return idx

    # --- runner / DYNAMIC_EPLB ---
    if mode == 'dynamic':
        v2_idx = set_v2('1')
        out.insert(v2_idx + 1, '%sexport DYNAMIC_EPLB="true"' % indent)
    elif mode == 'force':
        v2_idx = set_v2('0')
        out.insert(v2_idx + 1, '%sunset DYNAMIC_EPLB' % indent)
    else:  # off: only touch V2 when the file actually carried EPLB markers
        if has_markers:
            set_v2('1')

    # --- EPLB serve flags (dynamic only) ---
    if mode == 'dynamic':
        serve_idx = find_serve(out)
        ep_idx = _first_index(out, RE_EP_LINE)
        if ep_idx is None:
            raise ValueError("no --enable-expert-parallel flag found; cannot anchor "
                             "--enable-eplb flags (expert parallel is a prerequisite for EPLB)")
        if ep_idx < serve_idx:
            raise ValueError("--enable-expert-parallel appears before `vllm serve`; "
                             "refusing to guess the serve argument block")
        ep_indent = re.match(r'^(\s*)', out[ep_idx]).group(1)
        flags = ['--enable-eplb',
                 '--eplb-config.num_redundant_experts %d' % redundant,
                 '--eplb-config.window_size %d' % window,
                 '--eplb-config.step_interval %d' % interval]
        # Insert the EPLB flags *before* the --enable-expert-parallel line and
        # never mutate that line. It either already ends the serve command (no
        # trailing backslash) or is a mid-command line that must keep its own
        # backslash. Every inserted flag keeps the command open with a trailing
        # backslash; the untouched EP line then closes the command when it is
        # last. This keeps dynamic idempotent and lets off/force remove the
        # flags without leaving a dangling backslash that would swallow the
        # next script line.
        out[ep_idx:ep_idx] = ['%s%s \\' % (ep_indent, f) for f in flags]

    # --- --additional-config ---
    ac = find_json_block(out, '--additional-config')
    if ac is not None:
        start, end, jtext, base, suffix, single = ac
        try:
            obj = json.loads(jtext)
        except ValueError as e:
            raise ValueError("cannot parse existing --additional-config JSON: %s" % e)
        changed = False
        if mode == 'force':
            if obj.get('enable_force_eplb') is not True:
                obj['enable_force_eplb'] = True
                changed = True
        elif 'enable_force_eplb' in obj:
            obj.pop('enable_force_eplb')
            changed = True
        if not changed:
            return '\n'.join(out) + '\n'
        if single:
            dumped = json.dumps(obj, separators=(',', ':'))
            out[start] = "%s--additional-config '%s'%s" % (base, dumped, suffix)
        else:
            dumped = json.dumps(obj, indent=4, separators=(',', ':'))
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
    lines = text.splitlines()
    serve_idx = find_serve(lines)
    n_ac = len(re.findall(r'--additional-config', text))

    def index_of(pattern):
        return _first_index(lines, pattern)

    def before_serve(pattern):
        idx = index_of(pattern)
        return serve_idx is not None and idx is not None and idx < serve_idx

    def inside_serve(pattern):
        idx = index_of(pattern)
        return serve_idx is not None and idx is not None and idx > serve_idx

    checks = {
        'off': [
            (not re.search(r'^\s*export\s+DYNAMIC_EPLB\s*=', text, re.M),
             'no export DYNAMIC_EPLB line'),
            (not re.search(r'^\s*unset\s+DYNAMIC_EPLB\b', text, re.M),
             'no unset DYNAMIC_EPLB line'),
            (not re.search(r'^\s*--enable-eplb\b', text, re.M),
             'no --enable-eplb flag'),
            (not re.search(r'^\s*--eplb-config\.', text, re.M),
             'no --eplb-config.* flags'),
            (not _force_enabled(text), 'no enable_force_eplb key'),
        ],
        'dynamic': [
            (before_serve(r'^\s*export\s+VLLM_USE_V2_MODEL_RUNNER=1\s*$'),
             'export VLLM_USE_V2_MODEL_RUNNER=1 must be a standalone line before vllm serve'),
            (before_serve(r'^\s*export\s+DYNAMIC_EPLB="true"\s*$'),
             'export DYNAMIC_EPLB="true" must be a standalone line before vllm serve'),
            (inside_serve(r'^\s*--enable-eplb(\s|\\|$)'),
             '--enable-eplb flag present inside the serve command'),
            (inside_serve(r'^\s*--eplb-config\.num_redundant_experts\s+%d(\s|\\|$)' % redundant),
             '--eplb-config.num_redundant_experts %d present' % redundant),
            (inside_serve(r'^\s*--eplb-config\.window_size\s+%d(\s|\\|$)' % window),
             '--eplb-config.window_size %d present' % window),
            (inside_serve(r'^\s*--eplb-config\.step_interval\s+%d(\s|\\|$)' % interval),
             '--eplb-config.step_interval %d present' % interval),
            (not _force_enabled(text), 'no enable_force_eplb key'),
        ],
        'force': [
            (before_serve(r'^\s*export\s+VLLM_USE_V2_MODEL_RUNNER=0\s*$'),
             'export VLLM_USE_V2_MODEL_RUNNER=0 must be a standalone line before vllm serve'),
            (before_serve(r'^\s*unset\s+DYNAMIC_EPLB\s*$'),
             'unset DYNAMIC_EPLB must be a standalone line before vllm serve'),
            (not re.search(r'^\s*export\s+DYNAMIC_EPLB\s*=', text, re.M),
             'no export DYNAMIC_EPLB line'),
            (not re.search(r'^\s*--enable-eplb\b', text, re.M),
             'no --enable-eplb flag'),
            (not re.search(r'^\s*--eplb-config\.', text, re.M),
             'no --eplb-config.* flags'),
            (_force_enabled(text),
             '"enable_force_eplb":true merged into --additional-config'),
        ],
    }
    n_checks = 0
    for ok, msg in checks[mode]:
        n_checks += 1
        if not ok:
            problems.append(msg)
    n_checks += 1
    if mode == 'force' and n_ac != 1:
        problems.append('force requires exactly one --additional-config flag, found %d' % n_ac)
    elif n_ac > 1:
        problems.append('more than one --additional-config flag (found %d)' % n_ac)
    n_checks += 1
    swallowed = _serve_swallowed_command(lines)
    if swallowed is not None:
        problems.append('vllm serve 命令吞并了后续脚本行（悬空续行符）: %r' % swallowed)
    return problems, n_checks


MODE_DESC = {
    'off': '关闭 EPLB（基线：无 DYNAMIC_EPLB、无 eplb 参数、无 enable_force_eplb）',
    'dynamic': '动态 EPLB（V2 runner + DYNAMIC_EPLB="true" + --enable-eplb + eplb-config 参数）',
    'force': '强制 EPLB（V2=0 + unset DYNAMIC_EPLB + additional-config 里 enable_force_eplb:true）',
}

EPLB_CONFIG_KEYS = ('num_redundant_experts', 'window_size', 'step_interval')


def check_state(src, path):
    """Read-only EPLB state report for a launch template."""
    print('模板: %s' % path)
    role, err = node_role(src)
    if role == 'kv_consumer':
        print('节点角色: D（decode）节点模板 —— EPLB 只配置在 P（prefill）节点，本文件无需 EPLB 配置')
        return 0
    if role != 'kv_producer':
        print('节点角色: 无法确认 P（prefill）节点（%s）' % (err or 'kv_role=%r' % role))
        print('状态: 不明确 —— 不会改写该模板；请确认 --kv-transfer-config 的 "kv_role":"kv_producer"')
        return 1

    dyn_export = re.search(r'^\s*export\s+DYNAMIC_EPLB="true"\s*$', src, re.M) is not None
    dyn_any = re.search(r'^\s*export\s+DYNAMIC_EPLB\s*=', src, re.M) is not None
    unset = re.search(r'^\s*unset\s+DYNAMIC_EPLB\b', src, re.M) is not None
    v2_1 = re.search(r'^\s*export\s+VLLM_USE_V2_MODEL_RUNNER=1\s*$', src, re.M) is not None
    v2_0 = re.search(r'^\s*export\s+VLLM_USE_V2_MODEL_RUNNER=0\s*$', src, re.M) is not None
    enable = re.search(r'^\s*--enable-eplb\b', src, re.M) is not None
    cfg = {k: re.search(r'^\s*--eplb-config\.%s\s+\S+' % k, src, re.M) is not None
           for k in EPLB_CONFIG_KEYS}
    force_key = _force_enabled(src)
    n_ac = len(re.findall(r'--additional-config', src))

    markers = {
        'V2 runner=1': v2_1, 'V2 runner=0': v2_0,
        'export DYNAMIC_EPLB="true"': dyn_export,
        'export DYNAMIC_EPLB（任意值）': dyn_any,
        'unset DYNAMIC_EPLB': unset,
        '--enable-eplb': enable,
        '--eplb-config.num_redundant_experts': cfg['num_redundant_experts'],
        '--eplb-config.window_size': cfg['window_size'],
        '--eplb-config.step_interval': cfg['step_interval'],
        '"enable_force_eplb":true': force_key,
    }
    for k, v in markers.items():
        print('  %-38s %s' % (k, '有' if v else '无'))
    print('  --additional-config 数量              %d%s' % (n_ac, '' if n_ac <= 1 else '  <-- 异常，应为 0 或 1'))

    dyn = (v2_1 and dyn_export and enable and all(cfg.values())
           and not force_key and not unset and n_ac <= 1)
    force = (v2_0 and unset and force_key and not dyn_any and not enable and n_ac == 1)
    off = (not dyn_any and not unset and not enable
           and not any(cfg.values()) and not force_key and n_ac <= 1)

    if dyn:
        print('状态: dynamic EPLB（优化模式）')
        return 0
    if force:
        print('状态: force EPLB（均衡性能参照，勿作为上线优化手段）')
        return 0
    if off:
        print('状态: 关闭 EPLB（基线）')
        return 0
    print('状态: 不明确/混合 —— 存在互相矛盾或缺失的 EPLB 标记，建议用 --mode off|dynamic|force 之一修正')
    return 1


def validate_params(mode, redundant, window, interval, ep_size):
    """Return (redundant_after_rounding, notes). Raises SystemExit(2) on invalid input."""
    if mode != 'dynamic':
        return redundant, []
    notes = []
    if window < 1:
        die('ERROR: --window-size 必须 >= 1（当前 %d）' % window, 2)
    if interval < 1:
        die('ERROR: --step-interval 必须 >= 1（当前 %d）' % interval, 2)
    if redundant < 0:
        die('ERROR: --redundant-experts 必须 >= 0（当前 %d）' % redundant, 2)
    if redundant == 0:
        die('ERROR: --redundant-experts 0 只有重排价值，不构成冗余副本；'
            '如需纯重排请显式评估后再手工配置。', 2)
    if ep_size is None:
        die('ERROR: --mode dynamic 必须提供 --ep-size（用于校验 num_redundant_experts '
            '能否被 EP 整除，并在必要时向上取整）；未提供时拒绝写入，'
            '避免落盘一个与预期不符的实际生效值。', 2)
    if ep_size < 1:
        die('ERROR: --ep-size 必须 >= 1', 2)
    rounded = ((redundant + ep_size - 1) // ep_size) * ep_size
    if rounded != redundant:
        notes.append('num_redundant_experts 必须是 ep_size 的整数倍：%d 已向上取整为 %d'
                     % (redundant, rounded))
    return rounded, notes


def main():
    ap = argparse.ArgumentParser(description='Configure EPLB in a vLLM prefill-node launch template')
    ap.add_argument('--template', required=True, help='path to run_dp_template.sh (P node)')
    ap.add_argument('--mode', required=True, choices=['off', 'dynamic', 'force', 'check'])
    ap.add_argument('--ep-size', type=int, default=None,
                    help='expert-parallel size; REQUIRED for --mode dynamic to validate/round '
                         '--redundant-experts')
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
        real = os.path.realpath(path)
        print('NOTE: %s 是符号链接，实际改写目标 %s' % (path, real))
        path = real
    with open(path) as f:
        src = f.read()

    if args.mode == 'check':
        sys.exit(check_state(src, path))

    # Positive P-node criterion: only a kv_producer template is safe to edit.
    role, err = node_role(src)
    if role != 'kv_producer':
        if role == 'kv_consumer':
            die("ERROR: %s is a decode (D) node template (kv_role=kv_consumer).\n"
                "EPLB is only configured on the prefill (P) node's run_dp_template.sh.\n"
                "Refusing to edit." % path, 2)
        die("ERROR: cannot confirm %s is a prefill (P) node template: %s\n"
            "Expected --kv-transfer-config with \"kv_role\":\"kv_producer\".\n"
            "Refusing to edit." % (path, err), 2)

    redundant, notes = validate_params(args.mode, args.redundant_experts,
                                       args.window_size, args.step_interval, args.ep_size)

    try:
        new = transform(src, args.mode, redundant, args.window_size, args.step_interval)
    except ValueError as e:
        die('ERROR: %s' % e)

    problems, n_checks = verify(new, args.mode, redundant, args.window_size, args.step_interval)

    print('目标模式: %s' % MODE_DESC[args.mode])
    for note in notes:
        print('NOTE: %s' % note)
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
    tag = re.sub(r'[^A-Za-z0-9._-]', '_', tag)
    bak = '%s.bak-%s-%s' % (path, tag, ts)
    shutil.copy2(path, bak)

    # Atomic replace through a unique temp file (never a fixed name).
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                                    prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(new)
        shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    syntax_ok = None
    try:
        r = subprocess.run(['bash', '-n', path], capture_output=True, text=True)
        syntax_ok = (r.returncode == 0)
        if not syntax_ok:
            print('WARNING: bash -n reported a syntax error (restoring backup):')
            print(r.stderr)
            shutil.copy2(bak, path)
            sys.exit(1)
    except OSError:
        print('NOTE: bash not available, skipped syntax check')

    print('OK: 已写入 %s' % path)
    print('OK: 备份保存至 %s' % bak)
    if syntax_ok:
        print('OK: bash -n 语法检查通过；目标状态校验通过（%d 项）' % n_checks)
    else:
        print('OK: 目标状态校验通过（%d 项）；bash -n 未验证（bash 不可用）' % n_checks)
    print('')
    print('下一步（由用户在对应节点手动执行，不要代跑）:')
    print('  P节点: docker exec -it <container> bash; cd <P脚本目录>; bash start_mooncake.sh; bash start_p0.sh')
    print('  D节点: docker exec -it <container> bash; cd <D脚本目录>; bash start_d0.sh')
    print('  代理:  cd <P脚本目录>; bash start_proxy.sh')
    print('  压测注意: EPLB 开启后第 1 轮压测是预热离群值，需剔除（建议跑 4 轮取后 3 轮均值）')


if __name__ == '__main__':
    main()
