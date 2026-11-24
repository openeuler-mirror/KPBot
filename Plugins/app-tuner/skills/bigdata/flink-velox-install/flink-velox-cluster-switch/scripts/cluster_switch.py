#!/usr/bin/env python3
"""cluster_switch.py — switch the Flink lib/ between the native and the GlutenFlink build and
restart the cluster so it loads what is in lib now.

--to native   every gluten*/velox4j* jar in lib/ moves to <lib>.gfv-backup/ (never
              deleted); the runtime dependencies stay — they are harmless
--to gfv      every jar in <lib>.gfv-backup/ moves back into lib/; when lib/ already
              holds the complete GlutenFlink set nothing moves; the set is the four
              gluten/velox4j component jars plus the 14 runtime dependencies
              flink-velox-stack-build deploys with them, and when it cannot become
              complete (nothing to restore, components or dependencies missing) the
              step is blocked (run flink-velox-stack-build first, or switch to native)

Then the cluster of THIS installation is restarted: stop-cluster.sh, any leftover
daemon of this installation (classpath names lib/flink-dist under this lib) gets
TERM then KILL, wait until REST stops answering, start-cluster.sh, wait until REST
answers with at least one TaskManager (timeout); on timeout the newest taskexecutor
log tail is reported. Nothing runs unless REST answers with jobs-running == 0 while
daemons of this installation are up: a positive count and an unknown count (REST
not answering, key missing) are both hard refusals. Three subcommands, the family
skeleton: plan (default) prints the exact moves and commands; apply --yes executes
(stops at the first failing step, records every change); verify runs the
flink-velox-env cluster-check and lib-state-check with the matching --expect.

Usage:
  cluster_switch.py [plan]    --to native|gfv [--flink-home DIR] [--timeout 180] [--json] [--out FILE]
  cluster_switch.py apply --yes --to native|gfv [--flink-home DIR] [--timeout 180] [--json] [--out FILE]
  cluster_switch.py verify --to native|gfv

Root is required when lib/ is not writable by the current user. Exit codes:
plan 0 / 2 usage or refused; apply 0 switched and cluster back with the expected
build, 1 a step failed (the report says where it stopped; jars already moved are listed),
2 refused (no --yes, no --to, jobs running or unknown, blocked step, not root for root
steps); verify 0 every check ok, 1 a check failed or a sibling check skill is not
installed (NOT VERIFIED), 2 usage.

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT, ENV_PREPARE_PROC_ROOT
(with a fake /proc no signal is ever sent — the intent is only recorded),
ENV_PREPARE_EUID, ENV_PREPARE_PREFIX_WRITABLE=0, ENV_PREPARE_ENV_FAMILY_DIR.
"""
import argparse
import datetime
import glob
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KEY = 'flink_velox_cluster_switch_result'
# The suggested commands carry this script's own path: they run as printed, from any directory.
SCRIPT = os.path.realpath(__file__)

SYSROOT = os.environ.get('ENV_PREPARE_SYSROOT', '')
PROC_ROOT = os.environ.get('ENV_PREPARE_PROC_ROOT', '/proc')
# Signals reach real processes only when PROC_ROOT is the real /proc.
REAL_PROC = os.path.realpath(PROC_ROOT) == '/proc'

DEFAULT_REST_URL = 'http://localhost:8081'
JM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.entrypoint\.\w+Entrypoint$')
TM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.taskexecutor\.TaskManagerRunner$')
GFV_JAR_RE = re.compile(r'^(gluten|velox4j)', re.I)
# A GlutenFlink set is complete only with one jar per component (matched by prefix: the
# versions move with the build, the names do not).
GFV_COMPONENTS = ('gluten-flink-planner', 'gluten-flink-loader', 'gluten-flink-runtime', 'velox4j')
# The runtime dependencies flink-velox-stack-build deploys next to them (the same list
# and the same prefix rule as stack-build's runtime-deps step; a set without them fails
# at class-loading, so the gfv gate demands them too). --to native is not affected: it
# never moves them.
RUNTIME_DEPS = ('guava', 'failureaccess', 'arrow-c-data', 'arrow-format', 'arrow-memory-core',
                'arrow-memory-unsafe', 'arrow-vector', 'commons-io', 'flatbuffers-java',
                'jackson-annotations', 'jackson-core', 'jackson-databind',
                'jackson-datatype-jdk8', 'jackson-datatype-jsr310')
# The blocked fix of every --to gfv completeness gate.
GFV_FIX = ('run flink-velox-stack-build to build and deploy the complete GlutenFlink set first, '
           'or switch to the native build instead (--to native)')
# Flink 1.19 reads flink-conf.yaml when both files exist and ignores config.yaml then.
CONF_FILES = ('flink-conf.yaml', 'config.yaml')


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


def exc_text(exc):
    return f'{type(exc).__name__}: {exc}'


# --- the installation, its conf and REST -------------------------------------
def find_flink_home(explicit):
    """(home, source). An explicit --flink-home is the only candidate: a typo must not
    fall through to another installation on the machine."""
    needed = ('flink', 'start-cluster.sh', 'stop-cluster.sh')
    if explicit:
        cand = os.path.abspath(explicit)
        missing = [n for n in needed if not os.access(os.path.join(cand, 'bin', n), os.X_OK)]
        return (cand, '--flink-home') if not missing else (None, '--flink-home')
    cands = []
    if os.environ.get('FLINK_HOME'):
        cands.append((os.environ['FLINK_HOME'], 'FLINK_HOME'))
    flink = shutil.which('flink')
    if flink:
        cands.append((os.path.dirname(os.path.dirname(os.path.realpath(flink))), 'PATH'))
    cands += [(sys_path('/opt/flink'), 'default'), (sys_path('/usr/local/flink'), 'default')]
    for cand, source in cands:
        if all(os.access(os.path.join(cand, 'bin', n), os.X_OK) for n in needed):
            return cand, source
    return None, None


def parse_flat_yaml(path):
    """Dotted keys of a Flink conf file (nested maps are flattened like Flink does)."""
    conf = {}
    stack = []
    with open(path, encoding='utf-8', errors='replace') as fh:
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


def load_flink_conf(flink_home):
    """(conf file, flat conf) of the one file Flink actually reads — never a merge of both."""
    for name in CONF_FILES:
        path = os.path.join(flink_home, 'conf', name)
        if os.path.isfile(path):
            return path, parse_flat_yaml(path)
    return None, {}


def resolve_rest_url(conf):
    env = os.environ.get('FLINK_REST_URL')
    url = env or f"{conf.get('rest.address') or 'localhost'}:{conf.get('rest.port') or '8081'}"
    url = url.strip().rstrip('/')
    return url if re.match(r'^https?://', url, re.I) else 'http://' + url


def rest_is_local(rest_url):
    """The REST endpoint is on this machine: loopback, unspecified, or an address this
    host can bind (so the tool never restarts a cluster it found by a stray address)."""
    host = urllib.parse.urlsplit(rest_url).hostname or 'localhost'
    if host in ('localhost', '127.0.0.1', '::1', '0.0.0.0', '::') or host.endswith('.localhost'):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for family, _, _, _, sockaddr in infos:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.bind((sockaddr[0], 0))
            return True
        except OSError:
            continue
    return False


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
def missing_components(jars):
    return [c for c in GFV_COMPONENTS if not any(j.startswith(c + '-') for j in jars)]


def missing_deps(jars):
    return [a for a in RUNTIME_DEPS if not any(j.startswith(a + '-') for j in jars)]


def incomplete_note(missing, deps_missing):
    parts = []
    if missing:
        parts.append('missing: ' + ', '.join(missing))
    if deps_missing:
        parts.append('runtime dependencies missing: ' + ', '.join(deps_missing))
    return '; '.join(parts)


def step_jars(opts, lib):
    backup = lib + '.gfv-backup'
    step = {'name': 'jars', 'needs_root': not writable(lib), 'actions': []}
    lib_jars = sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, '*.jar')))
    in_lib = [j for j in lib_jars if GFV_JAR_RE.match(j)]
    in_backup = sorted(os.path.basename(j) for j in glob.glob(os.path.join(backup, '*.jar'))) if os.path.isdir(backup) else []
    step.update(in_lib=in_lib, in_backup=in_backup, backup=backup)
    # Every target is checked before the first file moves: an existing file is never overwritten.
    clashes = sorted(set(in_lib) & set(in_backup))

    def blocked(detail, fix, missing=None, missing_deps=None):
        step.update(status='blocked', detail=detail, fix=fix)
        if missing is not None:
            step['missing'] = missing
        if missing_deps is not None:
            step['missing_deps'] = missing_deps
        return step

    if opts['to'] == 'native':
        if not in_lib:
            step.update(status='skipped', detail='lib holds no gluten/velox jar — already the native build', plan=['# nothing to move'])
            return step
        if clashes:
            return blocked(f'{len(clashes)} jar(s) to move out already exist in the backup directory, moving would overwrite them: {", ".join(clashes)}',
                           f'compare {backup}/<jar> with {lib}/<jar>, rename or remove the older one by hand, then rerun')
        step.update(status='todo', moving=in_lib,
                    detail=f'move {len(in_lib)} gluten/velox jar(s) out of lib: {", ".join(in_lib)}',
                    plan=[f'mkdir -p {backup}'] + [f'mv {lib}/{j} {backup}/{j}' for j in in_lib])
        return step
    # --to gfv: the complete set is components + runtime dependencies, the same
    # definition as stack-build's (a GlutenFlink set without its runtime dependencies
    # does not load: restarting on it is what stack-build explicitly says not to do).
    missing = missing_components(lib_jars)
    deps_missing = missing_deps(lib_jars)
    if not missing and not deps_missing:
        left = f'; {len(in_backup)} older jar(s) left in {backup}' if in_backup else ''
        step.update(status='skipped',
                    detail=f'the GlutenFlink set is in lib already ({len(in_lib)} component jar(s) '
                           f'+ the {len(RUNTIME_DEPS)} runtime dependencies){left}', plan=['# nothing to move'])
        return step
    if clashes:
        return blocked(f'{len(clashes)} backup jar(s) have the same name as jar(s) already in lib, moving them back would overwrite the lib copies: {", ".join(clashes)}',
                       f'compare {lib}/<jar> with the backup copy, rename or remove the older one by hand, then rerun')
    if not in_backup:
        detail = ('no GlutenFlink jar in lib and nothing in the backup directory: there is no GlutenFlink set to switch to' if not in_lib else
                  'lib holds an incomplete GlutenFlink set (' + incomplete_note(missing, deps_missing) + ') and the backup directory is empty')
        return blocked(detail, GFV_FIX, missing=missing, missing_deps=deps_missing)
    still_missing = [c for c in missing if not any(j.startswith(c + '-') for j in in_backup)]
    still_deps = [a for a in deps_missing if not any(j.startswith(a + '-') for j in in_backup)]
    if still_missing or still_deps:
        return blocked('moving the backup back would still leave the GlutenFlink set incomplete ('
                       + incomplete_note(still_missing, still_deps) + ')',
                       GFV_FIX, missing=still_missing, missing_deps=still_deps)
    step.update(status='todo', moving=in_backup,
                detail=f'move {len(in_backup)} jar(s) back from {backup} into lib: {", ".join(in_backup)}',
                plan=[f'mv {backup}/{j} {lib}/{j}' for j in in_backup] + [f'rmdir {backup}   # when empty'])
    return step


def apply_jars(step, log):
    """Move the jars one by one; every completed move is in the log before the next starts."""
    if step.get('moving') is None:
        log.append({'step': 'jars', 'action': 'skipped', 'detail': step['detail']})
        step['status'] = 'skipped'
        return
    src, dst = step['lib'], step['backup']
    action = 'moved out'
    if step['direction'] == 'in':
        src, dst, action = step['backup'], step['lib'], 'moved in'
    os.makedirs(dst, exist_ok=True)
    for jar in step['moving']:
        shutil.move(os.path.join(src, jar), os.path.join(dst, jar))
        log.append({'step': 'jars', 'action': action, 'detail': f'{src}/{jar} -> {dst}/{jar}'})
    if step['direction'] == 'in':
        try:
            os.rmdir(step['backup'])
        except OSError:
            pass
    step['status'] = 'done'


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


def command_line(opts, sub, yes=False, sudo=False):
    """The command for the options that were actually reviewed, ready to copy: interpreter and
    the path of this script included (it is neither on PATH nor in the working directory)."""
    parts = ['sudo'] if sudo else []
    parts += ['python3', shlex.quote(SCRIPT), sub]
    if yes:
        parts.append('--yes')
    parts += ['--to', opts['to']]
    if opts.get('flink_home') and opts.get('flink_home_explicit'):
        parts += ['--flink-home', shlex.quote(opts['flink_home'])]
    if opts.get('timeout_explicit'):
        parts += ['--timeout', str(opts['timeout'])]
    return ' '.join(parts)


def build_steps(opts, flink_home, rest_url, overview):
    lib = os.path.join(flink_home, 'lib')
    jars = step_jars(opts, lib)
    jars.update(lib=lib, direction='out' if opts['to'] == 'native' else 'in')
    daemons = daemons_of(flink_home)
    running = bool(daemons) or overview is not None
    jobs = overview.get('jobs-running') if overview else None
    # "no job is running" needs an explicit integer 0 from REST; anything else is unknown.
    jobs_unknown = bool(daemons) and not isinstance(jobs, int)
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
    return steps, jobs, jobs_unknown


def apply_stop(opts, flink_home, rest_url, steps, log):
    """Returns True when the cluster is down. Every outcome is in the log before it returns."""
    stop = steps[1]
    try:
        proc = run([os.path.join(flink_home, 'bin', 'stop-cluster.sh')], timeout=120, env=dict(os.environ, FLINK_HOME=flink_home))
        log.append({'step': 'stop', 'action': 'stop-cluster.sh', 'detail': f'exit {proc.returncode}' + (f': {last_line(proc.stderr)}' if proc.returncode else '')})
        for sig_name, sig in (('TERM', signal.SIGTERM), ('KILL', signal.SIGKILL)):
            leftovers = daemons_of(flink_home)
            if not leftovers:
                break
            for pid, role in leftovers:
                if not REAL_PROC:  # tier-A sandbox: the intent is recorded, nothing is signalled
                    log.append({'step': 'stop', 'action': f'sent {sig_name} (sandboxed proc root: not sent)', 'detail': f'{role} pid {pid}'})
                    continue
                try:
                    os.kill(pid, sig)
                    log.append({'step': 'stop', 'action': f'sent {sig_name}', 'detail': f'{role} pid {pid}'})
                except OSError as exc:
                    log.append({'step': 'stop', 'action': f'{sig_name} failed', 'detail': f'{role} pid {pid}: {exc.strerror}'})
            if sig_name == 'TERM' and REAL_PROC:
                time.sleep(2)
        if stop['rest_was_up']:
            ok, _ = wait_rest(rest_url, False, opts['timeout'])
            if not ok:
                stop.update(status='failed', detail=f'REST {rest_url} still answers {opts["timeout"]}s after the stop — a daemon of another installation may own the port')
                log.append({'step': 'stop', 'action': 'failed', 'detail': stop['detail']})
                return False
        else:
            log.append({'step': 'stop', 'action': 'skipped', 'detail': f'REST {rest_url} was not answering before the stop; nothing to wait for'})
    except Exception as exc:  # a failing or timing-out command is this step's failure, not a crash
        stop.update(status='failed', detail=exc_text(exc))
        log.append({'step': 'stop', 'action': 'failed', 'detail': exc_text(exc)})
        return False
    stop['status'] = 'done'
    return True


def apply_start(opts, flink_home, rest_url, steps, log):
    start = steps[2]
    try:
        proc = run([os.path.join(flink_home, 'bin', 'start-cluster.sh')], timeout=300, env=dict(os.environ, FLINK_HOME=flink_home))
        log.append({'step': 'start', 'action': 'start-cluster.sh', 'detail': f'exit {proc.returncode}' + (f': {last_line(proc.stderr)}' if proc.returncode else '')})
        ok, overview = wait_rest(rest_url, True, opts['timeout'], want_taskmanagers=1)
        if proc.returncode != 0 or not ok:
            tail = newest_log_tail(flink_home, 'taskexecutor')
            start.update(status='failed', detail=f'the cluster did not come back with a TaskManager within {opts["timeout"]}s'
                         + (f' (start-cluster.sh exit {proc.returncode})' if proc.returncode else '')
                         + (f'; tail of {tail[0]}:\n{tail[1]}' if tail and tail[1] else ''))
            log.append({'step': 'start', 'action': 'failed', 'detail': f'no TaskManager within {opts["timeout"]}s'})
            return False
    except Exception as exc:
        start.update(status='failed', detail=exc_text(exc))
        log.append({'step': 'start', 'action': 'failed', 'detail': exc_text(exc)})
        return False
    start['status'] = 'done'
    start['detail'] = f'cluster back: Flink {overview.get("flink-version") or "?"}, {overview.get("taskmanagers")} TaskManager(s), {overview.get("jobs-running")} job(s) running'
    return True


def no_flink_result(command, opts, error, next_step, code):
    return {KEY: {'success': not error, 'checked_at': now(), 'command': command, 'to': opts['to'], 'flink_home': None,
                  'conf_file': None, 'rest_url': DEFAULT_REST_URL, 'steps': [], 'changes': [], 'root_steps': [],
                  'next_step': next_step, 'error_message': error}}, code


def context(opts):
    """(flink_home, conf_file, rest_url, overview) of the installation to work on."""
    flink_home, source = find_flink_home(opts.get('flink_home'))
    if not flink_home:
        return None, None, DEFAULT_REST_URL, None
    conf_file, conf = load_flink_conf(flink_home)
    rest_url = resolve_rest_url(conf)
    return flink_home, conf_file, rest_url, rest_overview(rest_url)


def refusals_of(opts, flink_home, conf_file, rest_url, steps, jobs, jobs_unknown, yes):
    out = []
    if not yes:
        out.append('apply needs --yes (show the user the plan first; restarting the cluster needs their consent)')
    if jobs:
        out.append(f'{jobs} job(s) running on the cluster — a restart would interrupt them; wait or stop them first')
    if jobs_unknown:
        out.append(f'cannot confirm that no job is running: REST {rest_url} did not answer with jobs-running while daemons of this '
                   f'installation are up — a restart could interrupt them; check rest.address / rest.port in {conf_file} or stop the cluster by hand')
    if not rest_is_local(rest_url):
        out.append(f'REST {rest_url} is not on this machine: this tool only restarts the local installation (check rest.address in {conf_file})')
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        out.append(f'the {blocked[0]["name"]} step is blocked: {blocked[0]["detail"]}' + (f' — {blocked[0]["fix"]}' if blocked[0].get('fix') else ''))
    root_steps = [s['name'] for s in steps if s['status'] == 'todo' and s.get('needs_root')]
    if root_steps and euid() != 0:
        out.append('steps ' + ', '.join(root_steps) + ' need root (lib not writable): ' + command_line(opts, 'apply', yes=True, sudo=True))
    return out


def apply_all(opts, yes):
    flink_home, conf_file, rest_url, overview = context(opts)
    if not flink_home:
        error = ('refused: --flink-home ' + opts['flink_home'] + ' is not a Flink installation (bin/flink, bin/start-cluster.sh and bin/stop-cluster.sh are needed)'
                 if opts.get('flink_home') else 'refused: no Flink installation')
        hint = ('fix --flink-home' if opts.get('flink_home') else
                'no Flink installation (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink); flink-velox-runtime-install installs it')
        return no_flink_result('apply', opts, error, hint, 2)
    steps, jobs, jobs_unknown = build_steps(opts, flink_home, rest_url, overview)
    stop = steps[1]
    stop['rest_was_up'] = overview is not None
    refusals = refusals_of(opts, flink_home, conf_file, rest_url, steps, jobs, jobs_unknown, yes)
    if refusals:
        message = '; '.join(refusals)
        return base_result('apply', opts, flink_home, conf_file, rest_url, steps, [], False, 'refused: ' + message, message), 2
    log = []
    try:
        apply_jars(steps[0], log)
    except Exception as exc:
        steps[0].update(status='failed', detail=f'{steps[0]["detail"]}; {exc_text(exc)}')
        log.append({'step': 'jars', 'action': 'failed', 'detail': exc_text(exc)})
        return base_result('apply', opts, flink_home, conf_file, rest_url, steps, log, False, 'jars failed',
                           'the jar(s) already moved are in changes; fix the jars step, then rerun'), 1
    if stop['status'] == 'todo':
        if not apply_stop(opts, flink_home, rest_url, steps, log):
            return base_result('apply', opts, flink_home, conf_file, rest_url, steps, log, False, 'stop failed',
                               'fix the stop step, then rerun'), 1
    else:
        log.append({'step': 'stop', 'action': 'skipped', 'detail': stop['detail']})
    if not apply_start(opts, flink_home, rest_url, steps, log):
        return base_result('apply', opts, flink_home, conf_file, rest_url, steps, log, False, 'start failed',
                           'fix the start step (see the log tail), then rerun'), 1
    return base_result('apply', opts, flink_home, conf_file, rest_url, steps, log, True, '',
                       f'switched to the {opts["to"]} build and restarted; run {command_line(opts, "verify")}'), 0


def base_result(command, opts, flink_home, conf_file, rest_url, steps, changes, success, error, next_step, checks=None):
    r = {'success': success, 'checked_at': now(), 'command': command, 'to': opts['to'], 'flink_home': flink_home,
         'conf_file': conf_file, 'rest_url': rest_url, 'steps': steps, 'changes': changes,
         'root_steps': [s['name'] for s in steps if s.get('needs_root')], 'next_step': next_step, 'error_message': error}
    if checks is not None:
        r['checks'] = checks
    return {KEY: r}


def build_plan(opts):
    flink_home, conf_file, rest_url, overview = context(opts)
    if not flink_home:
        hint = ('--flink-home ' + opts['flink_home'] + ' is not a Flink installation (bin/flink, bin/start-cluster.sh and bin/stop-cluster.sh are needed)'
                if opts.get('flink_home') else
                'no Flink installation (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink); flink-velox-runtime-install installs it')
        return no_flink_result('plan', opts, 'refused: ' + hint if opts.get('flink_home') else '', hint, 2 if opts.get('flink_home') else 0)
    steps, jobs, jobs_unknown = build_steps(opts, flink_home, rest_url, overview)
    steps[1]['rest_was_up'] = overview is not None
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        nxt = blocked[0].get('fix') or f'fix the {blocked[0]["name"]} step, then rerun plan'
    elif jobs:
        nxt = f'{jobs} job(s) are running — a restart would interrupt them; wait or stop them first, then run {command_line(opts, "apply", yes=True)}'
    elif jobs_unknown:
        nxt = (f'cannot confirm that no job is running: REST {rest_url} did not answer with jobs-running while daemons of this installation '
               f'are up — apply will refuse; check rest.address / rest.port in {conf_file} or stop the cluster by hand')
    elif not rest_is_local(rest_url):
        nxt = f'REST {rest_url} is not on this machine — apply will refuse; check rest.address in {conf_file}'
    else:
        nxt = f'review the plan above, then run {command_line(opts, "apply", yes=True)} (restarting the cluster needs the user\'s consent)'
    return base_result('plan', opts, flink_home, conf_file, rest_url, steps, [], True, '', nxt), 0


# --- verify ------------------------------------------------------------------
CHECKS = (('cluster', 'flink-velox-cluster-check', 'cluster_check.py', ()),
          ('lib-state', 'flink-velox-lib-state-check', 'lib_state_check.py', ('--expect', '{to}')))


def checker_path(skill, script):
    """Where a sibling check skill lives. An explicit family dir (ENV_PREPARE_ENV_FAMILY_DIR)
    is authoritative — nothing else is searched; otherwise the skill name is looked up in the
    source tree (skills/bigdata/flink-velox-env/<check>/) and in the flat layout install.sh
    produces (<config>/skills/<check>/)."""
    here = os.path.dirname(os.path.abspath(__file__))
    family = os.environ.get('ENV_PREPARE_ENV_FAMILY_DIR')
    cands = [os.path.join(family, skill, 'scripts', script)] if family else [
        os.path.join(here, '..', '..', '..', 'flink-velox-env', skill, 'scripts', script),
        os.path.join(here, '..', '..', skill, 'scripts', script),
        os.path.join(here, '..', '..', 'flink-velox-env', skill, 'scripts', script)]
    return next((os.path.realpath(c) for c in cands if os.path.isfile(c)), None)


def verify(opts):
    flink_home, _, _, _ = context(opts)
    env = dict(os.environ, FLINK_HOME=flink_home or '', ENV_CHECK_SYSROOT=SYSROOT, ENV_CHECK_PROC_ROOT=PROC_ROOT)
    checks = []
    for name, skill, script, extra in CHECKS:
        path = checker_path(skill, script)
        if path is None:
            checks.append({'name': name, 'status': 'missing', 'skill': skill,
                           'detail': f'{skill} is not installed next to this skill'})
            continue
        try:
            proc = run([sys.executable, path, '--json', *(a.format(to=opts['to']) for a in extra)], timeout=600, env=env)
        except Exception as exc:  # the checker did not start or did not finish
            checks.append({'name': name, 'skill': skill, 'status': 'fail', 'detail': f'{skill} did not run: {exc_text(exc)}'})
            continue
        try:
            payload = json.loads(proc.stdout)
            r = payload[next(k for k in payload if k.endswith('_result'))]
            checks.append({'name': name, 'skill': skill, 'status': 'ok' if r.get('success') else 'fail',
                           'detail': f'{len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                           'missing': r.get('missing') or []})
        except (ValueError, StopIteration, TypeError):
            checks.append({'name': name, 'skill': skill, 'status': 'fail',
                           'detail': f'{skill} produced no result (exit {proc.returncode})'})
    failing = [c for c in checks if c['status'] == 'fail']
    missing = [c for c in checks if c['status'] == 'missing']
    error = ''
    if failing:
        error += f'{len(failing)} check(s) failing'
    if missing:
        error += '; ' if error else ''
        error += f'{len(missing)} check(s) not run (sibling skills not installed): ' + ', '.join(c['skill'] for c in missing)
    rerun = f', then rerun {command_line(opts, "verify")}'
    next_step = ('' if not error else
                 f'install the missing check skill(s) ({", ".join(c["skill"] for c in missing)}){rerun}' if missing and not failing else
                 f'fix the failing check(s) above{rerun}' if failing and not missing else
                 f'install the missing check skill(s) ({", ".join(c["skill"] for c in missing)}) and fix the failing check(s) above{rerun}')
    result = {KEY: {'success': not error, 'checked_at': now(), 'command': 'verify', 'to': opts['to'],
                    'flink_home': flink_home, 'conf_file': None, 'rest_url': None, 'steps': [], 'changes': [], 'root_steps': [],
                    'checks': checks, 'next_step': next_step, 'error_message': error}}
    return result, 0 if not error else 1


def aborted(exc, command, opts):
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'to': opts.get('to'), 'flink_home': None,
                  'conf_file': None, 'rest_url': None, 'steps': [], 'changes': [], 'root_steps': [],
                  'next_step': 'fix the tool error first (see error_message), then rerun',
                  'error_message': f'tool aborted: {exc!r}'}}


# --- --out -------------------------------------------------------------------
OUT_SCOPE = ('--out must be a regular path inside the project directory (the working directory), '
             'not a symlink or a path outside it')


def out_problem(path, overwrite):
    """Why --out cannot be written, or None. Checked before anything runs and again at the write."""
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(os.path.realpath(os.getcwd()) + os.sep):
        return f'{OUT_SCOPE}: {path}'
    if os.path.exists(target):
        if not overwrite:
            return f'--out already exists, pass --overwrite to replace it: {path}'
        if not stat.S_ISREG(os.stat(target).st_mode):  # a fifo would block open() until a reader shows up
            return f'--out exists and is not a regular file, refusing to overwrite it: {path}'
    return None


def write_out(path, result, overwrite):
    problem = out_problem(path, overwrite)
    if problem:
        print(f'cluster_switch.py: {problem}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w' if overwrite else 'x', encoding='utf-8') as fh:
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
            lines.append(f"  {c['status'].upper():<8}{c['name']}: {c['detail']}")
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
    parser = argparse.ArgumentParser(description='Switch the Flink lib/ between the native and the GlutenFlink build and restart the cluster '
                                                 'for the Flink + GlutenFlink stack. plan by default; apply needs --yes --to.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--to', choices=('native', 'gfv'), help='the build lib should hold after the switch (required for apply and verify)')
    parser.add_argument('--flink-home', help='the Flink installation to switch — the only candidate when given (default: FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink)')
    parser.add_argument('--timeout', type=int, help='seconds to wait for REST to stop / come back (default 180)')
    parser.add_argument('--yes', action='store_true', help='apply: the record that the user consented to the restart')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing regular file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    if not args.to:
        parser.error('--to native|gfv is required')
    opts = {'to': args.to, 'flink_home': args.flink_home, 'flink_home_explicit': args.flink_home is not None,
            'timeout': args.timeout if args.timeout is not None else 180, 'timeout_explicit': args.timeout is not None}
    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs: a bad --out must not change the machine
        if problem:
            print(f'cluster_switch.py: {problem}', file=sys.stderr)
            result, code = no_flink_result(args.command, opts, 'refused: ' + problem, 'fix --out, then rerun', 2)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
            return code
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
