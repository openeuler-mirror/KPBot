#!/usr/bin/env python3
"""cluster_switch.py — switch the Flink lib/ between the native and the GFV build and
restart the cluster so it loads what is in lib now.

--to native   every gluten*/velox4j* jar in lib/ moves to <lib>.gfv-backup/ (never
              deleted); the runtime dependencies stay — they are harmless
--to gfv      every jar in <lib>.gfv-backup/ moves back into lib/; when there is
              nothing to restore and lib/ does not already hold the GFV set the
              step is blocked (run flink-velox-stack-build first)

Then the cluster of THIS installation is restarted: stop-cluster.sh, any leftover
daemon of this installation (classpath names lib/flink-dist under this lib) gets
TERM then KILL, wait until REST stops answering, start-cluster.sh, wait until REST
answers with at least one TaskManager (timeout); on timeout the newest taskexecutor
log tail is reported. Nothing runs while a job is running: REST /overview
jobs-running > 0 is a hard refusal. Three subcommands, the family skeleton:
plan (default) prints the exact moves and commands; apply --yes executes (stops
at the first failing step, records every change); verify runs the
flink-velox-env cluster-check and lib-state-check with the matching --expect.

Usage:
  cluster_switch.py [plan]    --to native|gfv [--flink-home DIR] [--timeout 180] [--json] [--out FILE]
  cluster_switch.py apply --yes --to native|gfv [--flink-home DIR] [--timeout 180] [--json] [--out FILE]
  cluster_switch.py verify --to native|gfv

Root is required when lib/ is not writable by the current user. Exit codes:
plan 0 / 2 usage; apply 0 switched and cluster back with the expected build,
1 a step failed (the report says where it stopped; jars already moved are listed),
2 refused (no --yes, no --to, jobs running, blocked step, not root for root
steps); verify 0 / 1 / 2 as the checks say.

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT, ENV_PREPARE_PROC_ROOT,
ENV_PREPARE_EUID, ENV_PREPARE_PREFIX_WRITABLE=0, ENV_PREPARE_ENV_FAMILY_DIR.
"""
import argparse
import datetime
import glob
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

KEY = 'flink_velox_cluster_switch_result'

SYSROOT = os.environ.get('ENV_PREPARE_SYSROOT', '')
PROC_ROOT = os.environ.get('ENV_PREPARE_PROC_ROOT', '/proc')

DEFAULT_REST_URL = 'http://localhost:8081'
JM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.entrypoint\.\w+Entrypoint$')
TM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.taskexecutor\.TaskManagerRunner$')
GFV_JAR_RE = re.compile(r'^(gluten|velox4j)', re.I)


def sys_path(rel):
    return SYSROOT + rel


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def euid():
    override = os.environ.get('ENV_PREPARE_EUID')
    return int(override) if override and override.isdigit() else os.geteuid()


def run(argv, timeout=None, env=None, cwd=None):
    return subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env, cwd=cwd)


def last_line(text):
    lines = (text or '').strip().splitlines()
    return lines[-1][:200] if lines else '?'


def writable(path):
    if os.environ.get('ENV_PREPARE_PREFIX_WRITABLE') == '0':
        return False
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return os.access(path, os.W_OK)


# --- the installation, its conf and REST -------------------------------------
def find_flink_home(explicit):
    cands = []
    if explicit:
        cands.append((explicit, '--flink-home'))
    if os.environ.get('FLINK_HOME'):
        cands.append((os.environ['FLINK_HOME'], 'FLINK_HOME'))
    flink = shutil.which('flink')
    if flink:
        cands.append((os.path.dirname(os.path.dirname(os.path.realpath(flink))), 'PATH'))
    cands += [(sys_path('/opt/flink'), 'default'), (sys_path('/usr/local/flink'), 'default')]
    for cand, source in cands:
        if os.access(os.path.join(cand, 'bin', 'flink'), os.X_OK):
            return cand, source
    return None, None


def load_flink_conf(flink_home):
    conf = {}
    for name in ('config.yaml', 'flink-conf.yaml'):
        try:
            fh = open(os.path.join(flink_home, 'conf', name), encoding='utf-8', errors='replace')
        except OSError:
            continue
        stack = []
        with fh:
            for raw in fh:
                line = re.sub(r'(^|\s)#.*$', r'\1', raw).rstrip()
                if not line or line in ('---', '...'):
                    continue
                m = re.match(r'^(\s*)([A-Za-z0-9_.-]+):(?:\s+(.*))?$', line)
                if not m:
                    continue
                indent, key, value = m.group(1), m.group(2), (m.group(3) or '').strip()
                while stack and stack[-1][0] >= len(indent):
                    stack.pop()
                prefix = f'{stack[-1][1]}.' if stack else ''
                if value:
                    conf[prefix + key] = value.strip('\'"')
                else:
                    stack.append((len(indent), prefix + key))
    return conf


def resolve_rest_url(conf):
    env = os.environ.get('FLINK_REST_URL')
    url = env or f"{conf.get('rest.address') or 'localhost'}:{conf.get('rest.port') or '8081'}"
    url = url.strip().rstrip('/')
    return url if re.match(r'^https?://', url, re.I) else 'http://' + url


def rest_overview(rest_url):
    """GET <rest>/overview, or None when nothing answers (proxies ignored)."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(rest_url + '/overview', headers={'Accept': 'application/json'})
        with opener.open(req, timeout=3) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None


def read_cmdline(pid):
    try:
        with open(os.path.join(PROC_ROOT, str(pid), 'cmdline'), 'rb') as fh:
            return [tok.decode('utf-8', 'replace') for tok in fh.read().split(b'\0') if tok]
    except OSError:
        return []


def classpath_of(argv):
    for i, tok in enumerate(argv):
        if tok in ('-classpath', '-cp') and i + 1 < len(argv):
            return [p for p in argv[i + 1].split(os.pathsep) if p]
    return []


def daemons_of(flink_home):
    """[(pid, role)] of the daemons of THIS installation."""
    real_lib = os.path.realpath(os.path.join(flink_home, 'lib'))
    found = []
    try:
        pids = sorted(e for e in os.listdir(PROC_ROOT) if e.isdigit())
    except OSError:
        return found
    for pid in pids:
        argv = read_cmdline(pid)
        entry = next((t for t in argv if JM_ENTRY_RE.match(t) or TM_ENTRY_RE.match(t)), None)
        if not entry:
            continue
        dist = [p for p in classpath_of(argv) if os.path.basename(p).startswith('flink-dist')]
        if not any(os.path.dirname(os.path.realpath(p)) == real_lib for p in dist):
            continue
        found.append((int(pid), 'jobmanager' if JM_ENTRY_RE.match(entry) else 'taskmanager'))
    return found


# --- steps -------------------------------------------------------------------
def step_jars(opts, lib):
    backup = lib + '.gfv-backup'
    step = {'name': 'jars', 'needs_root': not writable(lib), 'actions': []}
    in_lib = sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, '*.jar')) if GFV_JAR_RE.match(os.path.basename(j)))
    in_backup = sorted(os.path.basename(j) for j in glob.glob(os.path.join(backup, '*.jar'))) if os.path.isdir(backup) else []
    step.update(in_lib=in_lib, in_backup=in_backup, backup=backup)
    if opts['to'] == 'native':
        if not in_lib:
            step.update(status='skipped', detail='lib holds no gluten/velox jar — already the native build', plan=['# nothing to move'])
            return step
        step.update(status='todo', moving=in_lib,
                    detail=f'move {len(in_lib)} gluten/velox jar(s) out of lib: {", ".join(in_lib)}',
                    plan=[f'mkdir -p {backup}'] + [f'mv {lib}/{j} {backup}/{j}' for j in in_lib])
        return step
    # --to gfv
    if not in_lib and not in_backup:
        step.update(status='blocked', detail='no GFV jar in lib and nothing in the backup directory: there is no GFV set to switch to',
                    fix='run flink-velox-stack-build to build and deploy the GFV set first')
        return step
    if not in_backup:
        step.update(status='skipped', detail=f'the GFV set is in lib already ({len(in_lib)} jar(s))', plan=['# nothing to move'])
        return step
    step.update(status='todo', moving=in_backup,
                detail=f'move {len(in_backup)} jar(s) back from {backup} into lib: {", ".join(in_backup)}',
                plan=[f'mv {backup}/{j} {lib}/{j}' for j in in_backup] + [f'rmdir {backup}   # when empty'])
    return step


def apply_jars(step, log):
    if step.get('moving') is None:
        log.append({'step': 'jars', 'action': 'skipped', 'detail': step['detail']})
        step['status'] = 'skipped'
        return True
    lib, backup = step['lib'], step['backup']
    if step['direction'] == 'out':
        os.makedirs(backup, exist_ok=True)
        for jar in step['moving']:
            shutil.move(os.path.join(lib, jar), os.path.join(backup, jar))
        log.append({'step': 'jars', 'action': 'moved out', 'detail': f'{len(step["moving"])} jar(s) -> {backup}'})
    else:
        for jar in step['moving']:
            shutil.move(os.path.join(backup, jar), os.path.join(lib, jar))
        try:
            os.rmdir(backup)
        except OSError:
            pass
        log.append({'step': 'jars', 'action': 'moved in', 'detail': f'{len(step["moving"])} jar(s) -> {lib}'})
    step['status'] = 'done'
    return True


def wait_rest(rest_url, want_up, timeout, want_taskmanagers=0):
    """Poll /overview until it answers (want_up, with >= want_taskmanagers TMs) or
    stops answering (not want_up). Returns (ok, last_overview)."""
    deadline = time.time() + timeout
    overview = None
    while time.time() < deadline:
        overview = rest_overview(rest_url)
        if want_up:
            tms = overview.get('taskmanagers') if overview else None
            if isinstance(tms, int) and tms >= want_taskmanagers:
                return True, overview
        elif overview is None:
            return True, None
        time.sleep(min(3, max(0.5, deadline - time.time())))
    return False, overview


def newest_log_tail(flink_home, role, lines=20):
    logs = sorted(glob.glob(os.path.join(flink_home, 'log', f'flink-*-{role}-*.log')),
                  key=os.path.getmtime, reverse=True)
    if not logs:
        return None
    try:
        with open(logs[0], encoding='utf-8', errors='replace') as fh:
            return logs[0], ''.join(fh.readlines()[-lines:])
    except OSError:
        return logs[0], None


def build_steps(opts, flink_home, rest_url, overview):
    lib = os.path.join(flink_home, 'lib')
    jars = step_jars(opts, lib)
    jars.update(lib=lib, direction='out' if opts['to'] == 'native' else 'in')
    running = bool(daemons_of(flink_home)) or overview is not None
    jobs = overview.get('jobs-running') if overview else None
    steps = [jars]
    steps.append({'name': 'stop', 'needs_root': False, 'actions': [], 'status': 'todo' if running else 'skipped',
                  'detail': f'stop-cluster.sh, TERM/KILL leftover daemons of this installation, wait until REST {rest_url} stops answering'
                  if running else 'the cluster is not running',
                  'plan': [f'{flink_home}/bin/stop-cluster.sh', '# kill leftover JobManager/TaskManager of this installation (TERM, then KILL after 2 s)',
                           f'# poll GET {rest_url}/overview until it stops answering (timeout {opts["timeout"]}s)'] if running else ['# nothing to stop'],
                  'jobs_running': jobs})
    steps.append({'name': 'start', 'needs_root': False, 'actions': [], 'status': 'todo',
                  'detail': f'start-cluster.sh, wait until REST {rest_url} answers with at least 1 TaskManager (timeout {opts["timeout"]}s)',
                  'plan': [f'{flink_home}/bin/start-cluster.sh', f'# poll GET {rest_url}/overview until taskmanagers >= 1 (timeout {opts["timeout"]}s)']})
    return steps, running, jobs


def apply_all(opts, yes):
    flink_home, source = find_flink_home(opts.get('flink_home'))
    if not flink_home:
        return {KEY: {'success': False, 'checked_at': now(), 'command': 'apply', 'to': opts['to'], 'flink_home': None,
                      'rest_url': DEFAULT_REST_URL, 'steps': [], 'changes': [], 'root_steps': [],
                      'next_step': 'no Flink installation (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink); flink-velox-runtime-install installs it',
                      'error_message': 'refused: no Flink installation'}}, 2
    rest_url = resolve_rest_url(load_flink_conf(flink_home))
    overview = rest_overview(rest_url)
    steps, running, jobs = build_steps(opts, flink_home, rest_url, overview)
    lib = os.path.join(flink_home, 'lib')
    refusals = []
    if not yes:
        refusals.append('apply needs --yes (show the user the plan first; restarting the cluster needs their consent)')
    if jobs:
        refusals.append(f'{jobs} job(s) running on the cluster — a restart would interrupt them; wait or stop them first')
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        refusals.append(f'the {blocked[0]["name"]} step is blocked: {blocked[0]["detail"]}' + (f' — {blocked[0]["fix"]}' if blocked[0].get('fix') else ''))
    root_steps = [s['name'] for s in steps if s['status'] == 'todo' and s.get('needs_root')]
    if root_steps and euid() != 0:
        refusals.append('steps ' + ', '.join(root_steps) + ' need root (lib not writable): run "sudo python3 cluster_switch.py apply --yes --to ' + opts['to'] + '"')
    if refusals:
        return base_result('apply', opts, flink_home, rest_url, steps, [], False, 'refused: ' + '; '.join(refusals), '; '.join(refusals)), 2
    log = []
    if not apply_jars(steps[0], log):
        return base_result('apply', opts, flink_home, rest_url, steps, log, False, 'jars failed', 'fix the jars step, then rerun'), 1
    stop = steps[1]
    if stop['status'] == 'todo':
        proc = run([os.path.join(flink_home, 'bin', 'stop-cluster.sh')], timeout=120, env=dict(os.environ, FLINK_HOME=flink_home))
        log.append({'step': 'stop', 'action': 'stop-cluster.sh', 'detail': f'exit {proc.returncode}' + (f': {last_line(proc.stderr)}' if proc.returncode else '')})
        for sig_name, sig in (('TERM', 'SIGTERM'), ('KILL', 'SIGKILL')):
            leftovers = daemons_of(flink_home)
            if not leftovers:
                break
            for pid, role in leftovers:
                try:
                    os.kill(pid, getattr(__import__('signal'), sig))
                    log.append({'step': 'stop', 'action': f'sent {sig_name}', 'detail': f'{role} pid {pid}'})
                except OSError as exc:
                    log.append({'step': 'stop', 'action': f'{sig_name} failed', 'detail': f'{role} pid {pid}: {exc.strerror}'})
            if sig_name == 'TERM':
                time.sleep(2)
        ok, _ = wait_rest(rest_url, False, opts['timeout'])
        if not ok:
            stop.update(status='failed', detail=f'REST {rest_url} still answers {opts["timeout"]}s after the stop — a daemon of another installation may own the port')
            log.append({'step': 'stop', 'action': 'failed', 'detail': stop['detail']})
            return base_result('apply', opts, flink_home, rest_url, steps, log, False, 'stop failed', 'fix the stop step, then rerun'), 1
        stop['status'] = 'done'
    else:
        log.append({'step': 'stop', 'action': 'skipped', 'detail': stop['detail']})
    start = steps[2]
    proc = run([os.path.join(flink_home, 'bin', 'start-cluster.sh')], timeout=300, env=dict(os.environ, FLINK_HOME=flink_home))
    log.append({'step': 'start', 'action': 'start-cluster.sh', 'detail': f'exit {proc.returncode}' + (f': {last_line(proc.stderr)}' if proc.returncode else '')})
    ok, overview = wait_rest(rest_url, True, opts['timeout'], want_taskmanagers=1)
    if proc.returncode != 0 or not ok:
        tail = newest_log_tail(flink_home, 'taskexecutor')
        start.update(status='failed', detail=f'the cluster did not come back with a TaskManager within {opts["timeout"]}s'
                     + (f' (start-cluster.sh exit {proc.returncode})' if proc.returncode else '')
                     + (f'; tail of {tail[0]}:\n{tail[1]}' if tail and tail[1] else ''))
        log.append({'step': 'start', 'action': 'failed', 'detail': f'no TaskManager within {opts["timeout"]}s'})
        return base_result('apply', opts, flink_home, rest_url, steps, log, False, 'start failed', 'fix the start step (see the log tail), then rerun'), 1
    start['status'] = 'done'
    start['detail'] = f'cluster back: Flink {overview.get("flink-version") or "?"}, {overview.get("taskmanagers")} TaskManager(s), {overview.get("jobs-running")} job(s) running'
    return base_result('apply', opts, flink_home, rest_url, steps, log, True, '',
                       f'switched to the {opts["to"]} build and restarted; run cluster_switch.py verify --to {opts["to"]}'), 0


def base_result(command, opts, flink_home, rest_url, steps, changes, success, error, next_step, checks=None):
    r = {'success': success, 'checked_at': now(), 'command': command, 'to': opts['to'], 'flink_home': flink_home,
         'rest_url': rest_url, 'steps': steps, 'changes': changes,
         'root_steps': [s['name'] for s in steps if s.get('needs_root')], 'next_step': next_step, 'error_message': error}
    if checks is not None:
        r['checks'] = checks
    return {KEY: r}


def build_plan(opts):
    flink_home, source = find_flink_home(opts.get('flink_home'))
    if not flink_home:
        return {KEY: {'success': True, 'checked_at': now(), 'command': 'plan', 'to': opts['to'], 'flink_home': None,
                      'rest_url': DEFAULT_REST_URL, 'steps': [], 'changes': [], 'root_steps': [],
                      'next_step': 'no Flink installation (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink); flink-velox-runtime-install installs it',
                      'error_message': ''}}, 0
    rest_url = resolve_rest_url(load_flink_conf(flink_home))
    overview = rest_overview(rest_url)
    steps, running, jobs = build_steps(opts, flink_home, rest_url, overview)
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        nxt = blocked[0].get('fix') or f'fix the {blocked[0]["name"]} step, then rerun plan'
    elif jobs:
        nxt = f'{jobs} job(s) are running — a restart would interrupt them; wait or stop them first, then run cluster_switch.py apply --yes --to {opts["to"]}'
    else:
        nxt = f'review the plan above, then run cluster_switch.py apply --yes --to {opts["to"]} (restarting the cluster needs the user\'s consent)'
    return base_result('plan', opts, flink_home, rest_url, steps, [], True, '', nxt), 0


def verify(opts):
    family = os.environ.get('ENV_PREPARE_ENV_FAMILY_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                         '..', '..', '..', 'flink-velox-env')
    flink_home, _ = find_flink_home(opts.get('flink_home'))
    env = dict(os.environ, FLINK_HOME=flink_home or '', ENV_CHECK_SYSROOT=SYSROOT, ENV_CHECK_PROC_ROOT=PROC_ROOT)
    checks = []
    for name, skill, script, extra in (('cluster', 'flink-velox-cluster-check', 'cluster_check.py', []),
                                       ('lib-state', 'flink-velox-lib-state-check', 'lib_state_check.py', ['--expect', opts['to']])):
        path = os.path.join(os.path.realpath(family), skill, 'scripts', script)
        if not os.path.isfile(path):
            checks.append({'name': name, 'status': 'skipped', 'detail': f'{skill} is not installed next to this skill'})
            continue
        proc = run([sys.executable, path, '--json', *extra], timeout=600, env=env)
        try:
            payload = json.loads(proc.stdout)
            r = payload[next(k for k in payload if k.endswith('_result'))]
            checks.append({'name': name, 'status': 'ok' if r.get('success') else 'fail',
                           'detail': f'{len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                           'missing': r.get('missing') or []})
        except (ValueError, StopIteration, TypeError):
            checks.append({'name': name, 'status': 'fail', 'detail': f'{skill} produced no result (exit {proc.returncode})'})
    failing = [c for c in checks if c['status'] == 'fail']
    return {KEY: {'success': not failing, 'checked_at': now(), 'command': 'verify', 'to': opts['to'],
                  'flink_home': flink_home, 'rest_url': None, 'steps': [], 'changes': [], 'root_steps': [], 'checks': checks,
                  'next_step': '' if not failing else 'rerun cluster_switch.py plan --to ' + opts['to'],
                  'error_message': '' if not failing else f'{len(failing)} check(s) failing'}}, 0 if not failing else 1


def aborted(exc, command, opts):
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'to': opts.get('to'), 'flink_home': None,
                  'rest_url': None, 'steps': [], 'changes': [], 'root_steps': [],
                  'next_step': 'fix the tool error first (see error_message), then rerun',
                  'error_message': f'tool aborted: {exc!r}'}}


def write_out(path, result, overwrite):
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('cluster_switch.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'cluster_switch.py: --out already exists, pass --overwrite to replace it: {path}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'cluster_switch.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    lines = [f"cluster-switch {r['command']}  (-> {r['to']}, Flink {r['flink_home']}, REST {r['rest_url']})"]
    if r['command'] == 'verify':
        for c in r.get('checks', []):
            lines.append(f"  {c['status'].upper():<7}{c['name']}: {c['detail']}")
        lines.append('')
        lines.append(f"VERIFIED: the cluster runs the {r['to']} build" if r['success'] else f"NOT VERIFIED: {r['error_message']}")
        lines.append(f"next: {r['next_step']}")
        return '\n'.join(line.rstrip() for line in lines)
    if r['error_message'].startswith('tool aborted'):
        return f"TOOL ERROR: {r['error_message']}\nnext: {r['next_step']}"
    for step in r['steps']:
        mark = {'todo': 'TODO    ', 'skipped': 'SKIP    ', 'done': 'DONE    ', 'failed': 'FAILED  ', 'blocked': 'BLOCKED '}[step['status']]
        root = '  # needs root' if step.get('needs_root') and step['status'] in ('todo', 'done') else ''
        lines.append(f"  {step['name']:<8}{mark}{step['detail']}{root}")
        if r['command'] == 'plan' and step['status'] == 'todo':
            for p in step.get('plan', []):
                lines.append(f"        {p}")
        if step.get('fix') and step['status'] == 'blocked':
            lines.append(f"        fix: {step['fix']}")
    if r['command'] == 'apply' and r['changes']:
        lines.append('  changes:')
        for c in r['changes']:
            lines.append(f"    - {c['step']}: {c['action']} — {c['detail']}")
    lines.append('')
    if r['error_message'].startswith('refused'):
        lines.append('REFUSED: ' + r['error_message'][len('refused: '):])
    elif r['command'] == 'plan':
        todo = len([s for s in r['steps'] if s['status'] == 'todo'])
        lines.append(f'PLAN: {todo} step(s) to apply' if todo else 'PLAN: nothing to do')
    else:
        lines.append(f'SWITCHED: lib holds the {r["to"]} build and the cluster was restarted' if r['success']
                     else f"NOT SWITCHED: {r['error_message']}; later steps not run")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Switch the Flink lib/ between the native and the GFV build and restart the cluster '
                                                 'for the Flink + GlutenFlink stack. plan by default; apply needs --yes --to.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--to', choices=('native', 'gfv'), help='the build lib should hold after the switch (required for apply and verify)')
    parser.add_argument('--flink-home', help='the Flink installation (default: FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink)')
    parser.add_argument('--timeout', type=int, default=180, help='seconds to wait for REST to stop / come back (default 180)')
    parser.add_argument('--yes', action='store_true', help='apply: the record that the user consented to the restart')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    opts = {'to': args.to, 'flink_home': args.flink_home, 'timeout': args.timeout}
    if not args.to:
        parser.error('--to native|gfv is required')
    try:
        if args.command == 'plan':
            result, code = build_plan(opts)
        elif args.command == 'apply':
            result, code = apply_all(opts, args.yes)
        else:
            result, code = verify(opts)
    except Exception as exc:
        result, code = aborted(exc, args.command, opts), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
