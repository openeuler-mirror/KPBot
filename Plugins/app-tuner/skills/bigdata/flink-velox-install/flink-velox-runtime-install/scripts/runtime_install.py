#!/usr/bin/env python3
"""runtime_install.py — install the Flink runtime and the nexmark benchmark pieces for the GFV stack.

Three steps, each idempotent (probe first, skip what is already there):
  flink         the official Flink binary distribution <prefix>/flink-<version>/
                (tgz from a mirror, else archive.apache.org; the sha512 always from
                archive.apache.org), the stable <prefix>/flink symlink, and a minimal
                conf/flink-conf.yaml appended only when the stock file is all comments
  nexmark-jar   nexmark-flink built from the nexmark source checkout (mvn -pl
                nexmark-flink -am package -DskipTests), the jar copied into Flink lib/
  nexmark-tree  the runnable nexmark tree <nexmark-home>/ (bin/ conf/ queries/
                queries-cep from nexmark-flink/src/main/resources, plus lib/ log/ data/)

Three subcommands, the same skeleton as every skill of the install family:
  plan    (default)  probe and print the exact commands and changes; touch nothing
  apply   --yes      execute; refuses without --yes, refuses root-requiring steps
                     when not run as root, stops at the first failing step
  verify             run the flink-velox-env flink-dist-check and nexmark-check

Usage:
  runtime_install.py [plan]    [--version 1.19.x] [--prefix /opt] [--nexmark-home /opt/nexmark]
                                        [--repos-dir DIR] [--mirror URL] [--no-nexmark] [--json] [--out FILE]
  runtime_install.py apply --yes [the same options] [--jobs N]
  runtime_install.py verify

Only the 1.19 line is accepted (major.minor; the stack is built and verified on it).
Root is required when <prefix> or the Flink lib/ is not writable by the current user.

Exit codes: plan 0 / 2 usage; apply 0 every step done or already satisfied, 1 a step
failed (the report says what was done and what remains — rerun plan, it is
idempotent), 2 refused (no --yes, not root for root steps, version off the 1.19
line, a blocked step); verify 0 checks ok, 1 a check fails, 2 check error.

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT prefixes every
absolute system path (/opt, /usr/lib/jvm, ...); ENV_PREPARE_HOME replaces ~;
ENV_PREPARE_EUID overrides the effective uid; ENV_PREPARE_PREFIX_WRITABLE=0 forces
the root requirement; ENV_PREPARE_ENV_FAMILY_DIR points verify at another
flink-velox-env directory; ENV_PREPARE_RETRY_SLEEP seconds between clone attempts.
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

KEY = 'flink_velox_runtime_install_result'

SYSROOT = os.environ.get('ENV_PREPARE_SYSROOT', '')
HOME = os.environ.get('ENV_PREPARE_HOME') or os.path.expanduser('~')

FLINK_LINE = (1, 19)
NEXMARK_REPO = 'https://github.com/nexmark/nexmark.git'
DEFAULT_MIRROR = 'https://mirrors.huaweicloud.com/apache/flink'
ARCHIVE = 'https://archive.apache.org/dist/flink'
MINIMAL_CONF = (
    'jobmanager.rpc.address: localhost', 'jobmanager.rpc.port: 6123', 'jobmanager.bind-host: localhost',
    'taskmanager.bind-host: 0.0.0.0', 'taskmanager.host: localhost', 'taskmanager.data.bind-host: 0.0.0.0',
    'rest.bind-address: localhost', 'client.address: localhost', 'jobmanager.memory.process.size: 2048m',
    'taskmanager.memory.process.size: 8192m', 'taskmanager.numberOfTaskSlots: 8',
)
TREE_RESOURCES = ('bin', 'conf', 'queries', 'queries-cep')


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
    """Whether the current user may create `path`: checked on the nearest existing
    ancestor (the target itself usually does not exist yet). The test seam can
    force 'not writable' to exercise the root requirement."""
    if os.environ.get('ENV_PREPARE_PREFIX_WRITABLE') == '0':
        return False
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return os.access(path, os.W_OK)


# --- Flink -------------------------------------------------------------------
def flink_version_ok(version):
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)$', version or '')
    return bool(m) and (int(m.group(1)), int(m.group(2))) == FLINK_LINE


def conf_needs_minimal(conf):
    """True when the file exists and has no effective (non-comment, non-blank) line."""
    try:
        with open(conf, encoding='utf-8', errors='replace') as fh:
            return not any(re.match(r'^[^#\s]', line) for line in fh)
    except OSError:
        return False


def step_flink(opts):
    version, prefix = opts['version'], opts['prefix']  # an explicit path is used as given
    step = {'name': 'flink', 'actions': []}
    root_needed = not writable(prefix)
    step['needs_root'] = root_needed
    dist = os.path.join(prefix, f'flink-{version}')
    link = os.path.join(prefix, 'flink')
    tgz = f'flink-{version}-bin-scala_2.12.tgz'
    todos = []
    if not os.access(os.path.join(dist, 'bin', 'flink'), os.X_OK):
        todos.append(('download+unpack', [
            f'# {tgz}: try {opts["mirror"]}/flink-{version}/{tgz}, then {ARCHIVE}/flink-{version}/{tgz}',
            f'curl -fsSL --retry 3 --max-time 3600 -o <tmp>/{tgz} <url>',
            f'curl -fsSL --retry 3 --max-time 120 -o <tmp>/{tgz}.sha512 {ARCHIVE}/flink-{version}/{tgz}.sha512',
            f'(cd <tmp> && sha512sum -c {tgz}.sha512)',
            f'tar -xzf <tmp>/{tgz} -C {prefix}']))
    else:
        todos.append(('skip-unpack', [f'# {dist} already installed']))
    real_link = os.path.realpath(link) if os.path.islink(link) else None
    if real_link != os.path.realpath(dist):
        todos.append(('symlink', [f'ln -sfn {dist} {link}']))
    conf = os.path.join(dist, 'conf', 'flink-conf.yaml')
    if not os.access(os.path.join(dist, 'bin', 'flink'), os.X_OK) or conf_needs_minimal(conf):
        todos.append(('minimal-conf', [f'# append {len(MINIMAL_CONF)} keys to {conf} (only if every line is a comment)']))
    plan = [line for _, lines in todos for line in lines]
    step.update(dist=dist, link=link, tgz=tgz,
                pending=[name for name, _ in todos if name != 'skip-unpack'])
    if not step['pending']:
        step.update(status='skipped', detail=f'Flink {version} at {dist} (via {link}); conf already set',
                    plan=['# nothing to do'])
        return step
    step.update(status='todo', detail=f'Flink {version}: ' + ', '.join(step['pending']) + f' into {prefix}', plan=plan)
    return step


def apply_flink(step, opts, log):
    version, prefix = opts['version'], opts['prefix']
    dist, link, tgz = step['dist'], step['link'], step['tgz']
    base = f'{ARCHIVE}/flink-{version}'
    if 'download+unpack' in step['pending']:
        os.makedirs(prefix, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix='runtime-install-')
        try:
            src = None
            for candidate in (f'{opts["mirror"]}/flink-{version}/{tgz}', f'{base}/{tgz}'):
                proc = run(['curl', '-fsSL', '--retry', '3', '--max-time', '3600', '-o', os.path.join(tmp, tgz), candidate],
                           timeout=3700)
                if proc.returncode == 0:
                    src = candidate
                    break
            if not src:
                step.update(status='failed', detail=f'download of {tgz} failed from the mirror and from {ARCHIVE}')
                log.append({'step': 'flink', 'action': 'failed', 'detail': step['detail']})
                return False
            proc = run(['curl', '-fsSL', '--retry', '3', '--max-time', '120', '-o', os.path.join(tmp, tgz + '.sha512'),
                        f'{base}/{tgz}.sha512'], timeout=200)
            if proc.returncode != 0:
                step.update(status='failed', detail=f'checksum file {tgz}.sha512 not fetched from {ARCHIVE}')
                log.append({'step': 'flink', 'action': 'failed', 'detail': step['detail']})
                return False
            proc = run(['sha512sum', '-c', tgz + '.sha512'], cwd=tmp, timeout=600)
            if proc.returncode != 0:
                step.update(status='failed', detail=f'sha512 mismatch for {tgz}: {last_line(proc.stdout + proc.stderr)}')
                log.append({'step': 'flink', 'action': 'failed', 'detail': step['detail']})
                return False
            proc = run(['tar', '-xzf', os.path.join(tmp, tgz), '-C', prefix], timeout=1200)
            if proc.returncode != 0 or not os.access(os.path.join(dist, 'bin', 'flink'), os.X_OK):
                step.update(status='failed', detail=f'unpacking {tgz} into {prefix} failed: {last_line(proc.stderr)}')
                log.append({'step': 'flink', 'action': 'failed', 'detail': step['detail']})
                return False
            log.append({'step': 'flink', 'action': 'downloaded+unpacked',
                        'detail': f'{tgz} from {src} (sha512 verified) -> {dist}'})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if 'symlink' in step['pending']:
        if os.path.lexists(link) and not os.path.islink(link):
            step.update(status='failed', detail=f'{link} exists and is not a symlink — not replaced')
            log.append({'step': 'flink', 'action': 'failed', 'detail': step['detail']})
            return False
        tmp_link = link + '.runtime-install.tmp'
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
        os.symlink(dist, tmp_link)
        os.replace(tmp_link, link)
        log.append({'step': 'flink', 'action': 'symlinked', 'detail': f'{link} -> {dist}'})
    if 'minimal-conf' in step['pending']:
        conf = os.path.join(dist, 'conf', 'flink-conf.yaml')
        if conf_needs_minimal(conf):
            with open(conf, 'a', encoding='utf-8') as fh:
                fh.write('\n'.join(MINIMAL_CONF) + '\n')
            log.append({'step': 'flink', 'action': 'configured', 'detail': f'appended {len(MINIMAL_CONF)} keys to {conf}'})
    step['status'] = 'done'
    step['detail'] = f"Flink {version} at {dist} (via {link})"
    return True


# --- nexmark -----------------------------------------------------------------
def find_jdk():
    for cand in (os.environ.get('JAVA_HOME'),
                 *sorted(glob.glob(sys_path('/usr/lib/jvm/java-17-*'))),
                 *sorted(glob.glob(sys_path('/usr/lib/jvm/java-1.8.0-openjdk-*')))):
        if cand and os.access(os.path.join(cand, 'bin', 'javac'), os.X_OK):
            return cand
    return None


def flink_lib(opts):
    return os.path.join(opts['prefix'], 'flink', 'lib')


def nexmark_jar_in(lib):
    jars = sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, '*nexmark*.jar')))
    return jars[0] if jars else None


def step_nexmark_jar(opts, flink_step):
    step = {'name': 'nexmark-jar', 'actions': []}
    lib = flink_lib(opts)
    step['needs_root'] = not writable(lib)
    src = os.path.join(opts['repos_dir'], 'nexmark')
    step['src'] = src
    if nexmark_jar_in(lib):
        step.update(status='skipped', detail=f'{nexmark_jar_in(lib)} already in {lib}', plan=['# nothing to do'])
        return step
    blockers = []
    if not shutil.which('mvn'):
        blockers.append('mvn is not on PATH (flink-velox-build-env-prepare installs maven)')
    jdk = find_jdk()
    if not jdk:
        blockers.append('no JDK found (JAVA_HOME or /usr/lib/jvm/java-17-* / java-1.8.0-openjdk-*)')
    if flink_step['status'] == 'blocked':
        blockers.append(flink_step['detail'])
    if blockers:
        step.update(status='blocked', detail='; '.join(blockers), fix='fix the above, then rerun plan')
        return step
    plan = []
    if not os.path.isdir(os.path.join(src, '.git')):
        plan.append(f'git clone --depth 1 {NEXMARK_REPO} {src}   (retries 3)')
    plan.append(f'# JAVA_HOME={jdk} mvn -pl nexmark-flink -am package -DskipTests   (in {src})')
    plan.append(f'# cp {src}/nexmark-flink/target/nexmark-flink-*.jar (not -sources) -> {lib}/')
    step.update(status='todo', jdk=jdk, detail=f'build nexmark-flink from {src} with {jdk} and copy the jar into {lib}', plan=plan)
    return step


def apply_nexmark_jar(step, opts, log):
    lib = flink_lib(opts)
    src = step['src']
    if not os.path.isdir(os.path.join(src, '.git')):
        os.makedirs(os.path.dirname(src), exist_ok=True)
        ok = False
        for attempt in range(1, 4):
            proc = run(['git', 'clone', '--depth', '1', NEXMARK_REPO, src], timeout=3600)
            if proc.returncode == 0:
                ok = True
                break
            shutil.rmtree(src, ignore_errors=True)
            if attempt < 3:
                time.sleep(float(os.environ.get('ENV_PREPARE_RETRY_SLEEP', '5')))
        if not ok:
            step.update(status='failed', detail=f'git clone of nexmark failed after 3 attempts: {last_line(proc.stderr)}')
            log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': step['detail']})
            return False
        log.append({'step': 'nexmark-jar', 'action': 'cloned', 'detail': f'nexmark into {src}'})
    env = dict(os.environ, JAVA_HOME=step['jdk'])
    proc = run(['mvn', '-pl', 'nexmark-flink', '-am', 'package', '-DskipTests'], cwd=src, env=env, timeout=14400)
    if proc.returncode != 0:
        step.update(status='failed', detail='mvn build of nexmark-flink failed: ' + last_line(proc.stdout + proc.stderr))
        log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': step['detail']})
        return False
    jars = [j for j in sorted(glob.glob(os.path.join(src, 'nexmark-flink', 'target', 'nexmark-flink-*.jar')))
            if 'sources' not in os.path.basename(j)]
    if not jars:
        step.update(status='failed', detail=f'no nexmark-flink-*.jar under {src}/nexmark-flink/target after the build')
        log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': step['detail']})
        return False
    os.makedirs(lib, exist_ok=True)
    shutil.copy2(jars[0], lib)
    log.append({'step': 'nexmark-jar', 'action': 'built+deployed', 'detail': f'{os.path.basename(jars[0])} -> {lib}'})
    step.update(status='done', detail=f'{os.path.basename(jars[0])} built from {src} and copied into {lib}')
    return True


def step_nexmark_tree(opts, jar_step):
    step = {'name': 'nexmark-tree', 'actions': []}
    home = opts['nexmark_home']
    step['needs_root'] = not writable(home)
    src, res = jar_step['src'], os.path.join(jar_step['src'], 'nexmark-flink', 'src', 'main', 'resources')
    step['home'] = home
    if os.access(os.path.join(home, 'bin', 'run_query.sh'), os.X_OK):
        step.update(status='skipped', detail=f'runnable tree at {home}', plan=['# nothing to do'])
        return step
    if jar_step['status'] == 'blocked':
        step.update(status='blocked', detail=jar_step['detail'], fix=jar_step.get('fix'))
        return step
    if jar_step['status'] == 'skipped' and not os.path.isdir(res):
        step.update(status='blocked', detail=f'nexmark source resources not found at {res} and the jar is already deployed '
                    f'— clone {NEXMARK_REPO} to {src} (or point --repos-dir) and rerun', fix=f'git clone --depth 1 {NEXMARK_REPO} {src}')
        return step
    plan = [f'mkdir -p {home}',
            f'cp -R {res}/{{bin,conf,queries,queries-cep}} {home}/',
            f'mkdir -p {home}/{{lib,log,data}}',
            f'cp {flink_lib(opts)}/nexmark-flink-*.jar {home}/lib/']
    step.update(status='todo', res=res, detail=f'lay out the runnable tree at {home} from {res}', plan=plan)
    return step


def apply_nexmark_tree(step, opts, log):
    home, res = step['home'], step['res']
    missing = [d for d in TREE_RESOURCES if not os.path.isdir(os.path.join(res, d))]
    if missing:
        step.update(status='failed', detail=f'{res} lacks {", ".join(missing)} — not the nexmark-flink resources layout')
        log.append({'step': 'nexmark-tree', 'action': 'failed', 'detail': step['detail']})
        return False
    os.makedirs(home, exist_ok=True)
    for d in TREE_RESOURCES:
        shutil.copytree(os.path.join(res, d), os.path.join(home, d), dirs_exist_ok=True)
    for d in ('lib', 'log', 'data'):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    jar = nexmark_jar_in(flink_lib(opts))
    if jar:
        shutil.copy2(os.path.join(flink_lib(opts), jar), os.path.join(home, 'lib', jar))
    log.append({'step': 'nexmark-tree', 'action': 'laid out',
                'detail': f'{", ".join(TREE_RESOURCES)} + lib/log/data at {home}' + (f' (jar {jar})' if jar else '')})
    step.update(status='done', detail=f'runnable tree at {home}')
    return True


# --- verify ------------------------------------------------------------------
ENV_CHECKS = (('flink-dist', 'flink-velox-flink-dist-check', 'flink_dist_check.py'),
              ('nexmark', 'flink-velox-nexmark-check', 'nexmark_check.py'))


def env_family_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.environ.get('ENV_PREPARE_ENV_FAMILY_DIR') or os.path.join(here, '..', '..', '..', 'flink-velox-env')


def verify(opts, with_nexmark):
    family = os.path.realpath(env_family_dir())
    env = dict(os.environ, FLINK_HOME=opts['prefix'] + '/flink', NEXMARK_HOME=opts['nexmark_home'],
               ENV_CHECK_SYSROOT=SYSROOT)
    items = []
    for name, skill, script in ENV_CHECKS:
        if name == 'nexmark' and not with_nexmark:
            items.append({'name': name, 'status': 'skipped', 'detail': '--no-nexmark: not checked'})
            continue
        path = os.path.join(family, skill, 'scripts', script)
        if not os.path.isfile(path):
            items.append({'name': name, 'status': 'skipped', 'detail': f'{skill} is not installed next to this skill'})
            continue
        proc = run([sys.executable, path, '--json'], timeout=600, env=env)
        try:
            payload = json.loads(proc.stdout)
            r = payload[next(k for k in payload if k.endswith('_result'))]
            items.append({'name': name, 'status': 'ok' if r.get('success') else 'fail',
                          'detail': f'{len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                          'missing': r.get('missing') or []})
        except (ValueError, StopIteration, TypeError):
            items.append({'name': name, 'status': 'fail', 'detail': f'{skill} produced no result (exit {proc.returncode})'})
    return items


# --- assembly ----------------------------------------------------------------
def build_steps(opts):
    flink = step_flink(opts)
    if opts['no_nexmark']:
        return [flink]
    jar = step_nexmark_jar(opts, flink)
    return [flink, jar, step_nexmark_tree(opts, jar)]


def next_step_for(command, steps, opts, refused=None):
    if refused:
        return refused
    if command == 'verify':
        return ''
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        return blocked[0].get('fix') or f'fix the {blocked[0]["name"]} step, then rerun plan'
    todo = [s for s in steps if s['status'] == 'todo']
    if command == 'plan':
        if not todo:
            return 'ready: the Flink runtime' + ('' if opts['no_nexmark'] else ' and nexmark') + \
                   ' are installed (verify with: runtime_install.py verify)'
        root_needed = [s['name'] for s in todo if s['needs_root']]
        return ('as root: ' if root_needed else '') + 'review the plan above, then run runtime_install.py apply --yes'
    failed = [s for s in steps if s['status'] == 'failed']
    if failed:
        return f'fix the {failed[0]["name"]} step ({failed[0]["detail"]}), then rerun plan and apply — it is idempotent'
    return f'{len([s for s in steps if s["status"] == "done"])} step(s) done, nothing left to do; run runtime_install.py verify'


def base_result(command, steps, changes, opts, success, error, next_step, checks=None):
    r = {'success': success, 'checked_at': now(), 'command': command, 'version': opts['version'],
         'prefix': opts['prefix'], 'flink_home': opts['prefix'] + '/flink',
         'nexmark_home': None if opts['no_nexmark'] else opts['nexmark_home'],
         'steps': steps, 'changes': changes,
         'root_steps': [s['name'] for s in steps if s.get('needs_root')], 'next_step': next_step, 'error_message': error}
    if checks is not None:
        r['checks'] = checks
    return {KEY: r}


def build_plan(opts):
    steps = build_steps(opts)
    if not flink_version_ok(opts['version']):
        r = base_result('plan', steps, [], opts, False, f'refused: Flink {opts["version"]} is not on the 1.19 line',
                        f'pick a 1.19.x version (major.minor 1.19; the stack is built and verified on it)', None)
        return r, 2
    return base_result('plan', steps, [], opts, True, '', next_step_for('plan', steps, opts)), 0


def apply_all(opts, yes):
    if not flink_version_ok(opts['version']):
        return base_result('apply', [], [], opts, False, f'refused: Flink {opts["version"]} is not on the 1.19 line',
                           'pick a 1.19.x version', 'pick a 1.19.x version'), 2
    steps = build_steps(opts)
    refusals = []
    if not yes:
        refusals.append('apply needs --yes (show the user the plan first; --yes is the record of their consent)')
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        refusals.append(f'the {blocked[0]["name"]} step is blocked: {blocked[0]["detail"]}'
                       + (f' — {blocked[0]["fix"]}' if blocked[0].get('fix') else ''))
    root_steps = [s['name'] for s in steps if s['status'] == 'todo' and s.get('needs_root')]
    if root_steps and euid() != 0:
        refusals.append('steps ' + ', '.join(root_steps) + f' need root (not writable): run "sudo python3 runtime_install.py apply --yes"')
    if refusals:
        return base_result('apply', steps, [], opts, False, 'refused: ' + '; '.join(refusals), '; '.join(refusals)), 2
    log = []
    appliers = {'flink': apply_flink, 'nexmark-jar': apply_nexmark_jar, 'nexmark-tree': apply_nexmark_tree}
    for step in steps:
        if step['status'] == 'skipped':
            log.append({'step': step['name'], 'action': 'skipped', 'detail': step['detail']})
            continue
        if step['name'] == 'nexmark-jar' and steps[0]['status'] == 'done':
            step['needs_root'] = not writable(os.path.dirname(flink_lib(opts)))
        if not appliers[step['name']](step, opts, log):
            break
    failed = [s for s in steps if s['status'] == 'failed']
    return base_result('apply', steps, log, opts, not failed,
                       f'{failed[0]["name"]} failed' if failed else '',
                       next_step_for('apply', steps, opts)), 1 if failed else 0


def build_verify(opts):
    checks = verify(opts, not opts['no_nexmark'])
    failing = [c for c in checks if c['status'] == 'fail']
    r = base_result('verify', [], [], opts, not failing, '' if not failing else f'{len(failing)} check(s) failing',
                    '' if not failing else 'rerun runtime_install.py plan for the remaining items', checks)
    return r, 0 if not failing else 1


def aborted(exc, command, opts):
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'version': opts.get('version'),
                  'prefix': opts.get('prefix'), 'flink_home': None, 'nexmark_home': None, 'steps': [], 'changes': [],
                  'root_steps': [], 'next_step': 'fix the tool error first (see error_message), then rerun',
                  'error_message': f'tool aborted: {exc!r}'}}


def write_out(path, result, overwrite):
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('runtime_install.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'runtime_install.py: --out already exists, pass --overwrite to replace it: {path}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'runtime_install.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    lines = [f"runtime-install {r['command']}  (Flink {r['version']} -> {r['flink_home']}"
             + (f", nexmark {r['nexmark_home']}" if r.get('nexmark_home') else '') + ')']
    if r['command'] == 'verify':
        for c in r.get('checks', []):
            lines.append(f"  {c['status'].upper():<7}{c['name']}: {c['detail']}")
        lines.append('')
        lines.append('VERIFIED: Flink runtime' + ('' if r.get('nexmark_home') is None else ' and nexmark')
                     + ' checks pass' if r['success'] else f"NOT VERIFIED: {r['error_message']}")
        lines.append(f"next: {r['next_step']}")
        return '\n'.join(line.rstrip() for line in lines)
    if r['error_message'].startswith('tool aborted'):
        return f"TOOL ERROR: {r['error_message']}\nnext: {r['next_step']}"
    for step in r['steps']:
        mark = {'todo': 'TODO    ', 'skipped': 'SKIP    ', 'done': 'DONE    ', 'failed': 'FAILED  ', 'blocked': 'BLOCKED '}[step['status']]
        root = '  # needs root' if step.get('needs_root') and step['status'] in ('todo', 'done') else ''
        lines.append(f"  {step['name']:<13}{mark}{step['detail']}{root}")
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
        blocked = [s for s in r['steps'] if s['status'] == 'blocked']
        lines.append('PLAN: ' + ('nothing to do' if not any(s['status'] == 'todo' for s in r['steps'])
                                 else f"{len([s for s in r['steps'] if s['status'] == 'todo'])} step(s) to apply")
                     + (f" ({len(blocked)} step(s) blocked — see the fix: lines)" if blocked else ''))
    else:
        lines.append('APPLIED: all steps done or already satisfied' if r['success']
                     else f"NOT APPLIED: {r['error_message']}; later steps not run")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the Flink 1.19.x runtime (and the nexmark benchmark pieces) "
                                                 'for the Flink + GlutenFlink stack. plan by default; apply needs --yes.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--version', default='1.19.2', help='Flink version, 1.19 line only (default 1.19.2)')
    parser.add_argument('--prefix', default='/opt', help='where flink-<version>/ and the flink symlink live (default /opt)')
    parser.add_argument('--nexmark-home', default='/opt/nexmark', help='runnable nexmark tree (default /opt/nexmark)')
    parser.add_argument('--repos-dir', help='directory for the nexmark source checkout (default: $GFV_REPOS_DIR, then ~/repos)')
    parser.add_argument('--mirror', default=DEFAULT_MIRROR, help='mirror base for the Flink tgz (sha512 always from archive.apache.org)')
    parser.add_argument('--no-nexmark', action='store_true', help='install only the Flink runtime')
    parser.add_argument('--yes', action='store_true', help='apply: the record that the user saw the plan and consented')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    opts = {'version': args.version,
            'prefix': args.prefix if args.prefix != '/opt' else sys_path('/opt'),
            'nexmark_home': args.nexmark_home if args.nexmark_home != '/opt/nexmark' else sys_path('/opt/nexmark'),
            'mirror': args.mirror,
            'no_nexmark': args.no_nexmark,
            'repos_dir': args.repos_dir or os.environ.get('GFV_REPOS_DIR') or os.path.join(HOME, 'repos')}
    try:
        if args.command == 'plan':
            result, code = build_plan(opts)
        elif args.command == 'apply':
            result, code = apply_all(opts, args.yes)
        else:
            result, code = build_verify(opts)
    except Exception as exc:
        result, code = aborted(exc, args.command, opts), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
