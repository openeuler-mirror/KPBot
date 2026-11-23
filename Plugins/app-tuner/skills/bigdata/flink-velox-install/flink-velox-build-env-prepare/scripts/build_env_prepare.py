#!/usr/bin/env python3
"""build_env_prepare.py — prepare the build environment for the Flink + GlutenFlink (GFV) stack.

Three steps, each idempotent (probe first, skip what is already there):
  packages     the openEuler package set: toolchain (git cmake gcc gcc-c++ maven),
               build tools (ninja-build ccache autoconf automake libtool flex bison
               python3 patchelf), 13 C++ -devel libraries, java-17-openjdk-devel
  repos        clone velox / velox4j / gluten at their baseline branches into <repos-dir>
  source-deps  Velox's source-built C++ libraries: the list is parsed from the
               velox checkout's own setup script and each install_<lib>() is driven
               in a subshell with INSTALL_PREFIX=/usr/local (later libraries build
               against earlier ones, so the first failure stops the run)

Three subcommands, the same skeleton as every skill of the install family:
  plan    (default)  probe and print the exact commands and changes; touch nothing
  apply   --yes      execute; refuses without --yes, refuses root-requiring steps
                     when not run as root, stops at the first failing step
  verify             run the matching flink-velox-env checks and summarise

Usage:
  build_env_prepare.py [plan] [--repos-dir DIR] [--json] [--out FILE [--overwrite]]
  build_env_prepare.py apply --yes [--repos-dir DIR] [--jobs N] [--json] [--out FILE [--overwrite]]
  build_env_prepare.py verify [--repos-dir DIR] [--json]

<repos-dir>: --repos-dir, else $GFV_REPOS_DIR, else ~/repos. Supported for apply:
openEuler, CentOS / RHEL 9 (package names are openEuler's); other distributions
get a plan but apply refuses.

Exit codes: plan 0 / 2 usage; apply 0 every step done or already satisfied, 1 a
step failed (the report says what was done and what remains — rerun plan, it is
idempotent), 2 refused (no --yes, not root for root steps, unsupported OS, git or
dnf missing); verify 0 all checks ok, 1 a check fails, 2 check error.

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT prefixes every
absolute system path (/etc/os-release, /usr/lib/jvm, /usr/local, ...);
ENV_PREPARE_HOME replaces ~; ENV_PREPARE_EUID overrides the effective uid;
ENV_PREPARE_ENV_FAMILY_DIR points verify at another flink-velox-env directory;
ENV_PREPARE_RETRY_SLEEP seconds between clone attempts (default 5).
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
import time

KEY = 'flink_velox_build_env_prepare_result'

SYSROOT = os.environ.get('ENV_PREPARE_SYSROOT', '')
HOME = os.environ.get('ENV_PREPARE_HOME') or os.path.expanduser('~')

# name, upstream, baseline branch, files that make the checkout usable
REPOS = (
    ('velox', 'https://github.com/bigo-sg/velox.git', 'gluten-20260829', ('CMakeLists.txt', 'scripts')),
    ('velox4j', 'https://github.com/bigo-sg/velox4j.git', 'gluten-20260829', ('pom.xml',)),
    ('gluten', 'https://github.com/apache/gluten.git', 'main', ('gluten-flink/pom.xml',)),
)
# openEuler package names (the same tables the env checks probe by)
TOOLCHAIN_PACKAGES = ('git', 'cmake', 'gcc', 'gcc-c++', 'maven')
BUILD_TOOL_PACKAGES = ('ninja-build', 'ccache', 'autoconf', 'automake', 'libtool', 'flex', 'bison',
                       'python3', 'patchelf')
CPP_DEP_PACKAGES = ('libevent-devel', 'openssl-devel', 're2-devel', 'zstd-devel', 'lz4-devel',
                    'double-conversion-devel', 'libdwarf-devel', 'elfutils-libelf-devel', 'libcurl-devel',
                    'libicu-devel', 'libsodium-devel', 'zlib-devel', 'librdkafka-devel')
JDK_PACKAGE = 'java-17-openjdk-devel'
SUPPORTED_OS = ('openeuler', 'centos', 'rhel')  # CentOS/RHEL 9 only; 7 has no velox setup script
SOURCE_PROBE_OVERRIDE = {'protobuf': 'protoc'}


def sys_path(rel):
    return SYSROOT + rel


def make_item(name, status, detail, fix=None, **extra):
    item = {'name': name, 'status': status, 'detail': detail}
    if fix:
        item['fix'] = fix
    item.update(extra)
    return item


def read_os_release():
    info = {}
    try:
        with open(sys_path('/etc/os-release'), encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                m = re.match(r'^([A-Z][A-Z0-9_]*)=(.*)$', raw.strip())
                if m:
                    info[m.group(1)] = m.group(2).strip().strip('"')
    except OSError:
        pass
    return info


def os_supported(os_info):
    os_id = (os_info.get('ID') or '').lower()
    major = re.match(r'\d+', os_info.get('VERSION_ID') or '')
    if os_id == 'openeuler':
        return True, 'openEuler'
    if os_id in ('centos', 'rhel'):
        if major and major.group(0) == '9':
            return True, 'CentOS/RHEL 9'
        return False, f'{os_id} {major.group(0) if major else "?"} (only 9 has a velox setup script)'
    return False, f'{os_id or "unknown distribution"} (package names are openEuler\'s)'


def euid():
    override = os.environ.get('ENV_PREPARE_EUID')
    return int(override) if override and override.isdigit() else os.geteuid()


def run(argv, timeout=None, env=None, cwd=None):
    """Run a fixed argv (never through a shell); returns the CompletedProcess."""
    return subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env, cwd=cwd)


def run_shell(command, env, timeout=None):
    """Run one shell line (only for driving velox's sourced setup functions)."""
    return subprocess.run(['bash', '-c', command], capture_output=True, text=True, errors='replace',
                          timeout=timeout, stdin=subprocess.DEVNULL, env=env)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


# --- step 1: packages --------------------------------------------------------
def rpm_installed(package):
    proc = run(['rpm', '-q', package], timeout=30)
    return proc.returncode == 0


def step_packages(os_info):
    packages = list(TOOLCHAIN_PACKAGES + BUILD_TOOL_PACKAGES + CPP_DEP_PACKAGES + (JDK_PACKAGE,))
    step = {'name': 'packages', 'needs_root': True, 'actions': []}
    if not shutil.which('rpm'):
        step.update(status='blocked', detail='rpm is not on PATH: not an RPM-based system, the openEuler '
                    'package names do not apply', fix='install the toolchain, build tools, C++ libraries and '
                    'JDK 17 with this distribution\'s package manager, then rerun')
        return step
    missing = [p for p in packages if not rpm_installed(p)]
    step['missing'] = missing
    step['present'] = len(packages) - len(missing)
    if not missing:
        step.update(status='skipped', detail=f'all {len(packages)} packages installed',
                    plan=['# nothing to install'])
        return step
    step.update(status='todo', detail=f'{len(missing)} of {len(packages)} packages missing: {", ".join(missing)}',
                plan=[f'dnf install -y {" ".join(missing)}'])
    return step


def apply_packages(step, log):
    proc = run(['dnf', 'install', '-y', *step['missing']], timeout=3600)
    if proc.returncode != 0:
        step.update(status='failed', detail='dnf install failed: ' + (proc.stderr.strip().splitlines() or ['?'])[-1][:200])
        log.append({'step': 'packages', 'action': 'failed', 'detail': step['detail']})
        return False
    step.update(status='done', detail=f'installed {len(step["missing"])} package(s): {", ".join(step["missing"])}')
    log.append({'step': 'packages', 'action': 'installed', 'detail': step['detail']})
    return True


# --- step 2: repos -----------------------------------------------------------
def locate_repos_dir(explicit):
    return explicit or os.environ.get('GFV_REPOS_DIR') or os.path.join(HOME, 'repos')


def step_repos(repos_dir):
    step = {'name': 'repos', 'needs_root': False, 'actions': [], 'clones': []}
    if not shutil.which('git'):
        step.update(status='blocked', detail='git is not on PATH (installed by the packages step)',
                    fix='run apply again after the packages step, or install git')
        return step
    plan, todos, problems = [], [], []
    for name, url, branch, markers in REPOS:
        dest = os.path.join(repos_dir, name)
        if os.path.isdir(dest):
            absent = [m for m in markers if not os.path.exists(os.path.join(dest, *m.split('/')))]
            if absent:
                problems.append(f'{dest} exists but is not a usable {name} checkout (missing {", ".join(absent)})')
            else:
                continue
        else:
            todos.append((name, url, branch, dest))
            plan.append(f'git clone -b {branch} {url} {dest}')
    step['clones'] = [{'name': n, 'url': u, 'branch': b, 'dest': d} for n, u, b, d in todos]
    if problems:
        step.update(status='blocked', detail='; '.join(problems),
                    fix='remove or repair the incomplete checkout by hand (never deleted automatically), then rerun')
        return step
    if not todos:
        step.update(status='skipped', detail='velox, velox4j and gluten are checked out under ' + repos_dir,
                    plan=['# nothing to clone'])
        return step
    step.update(status='todo', detail=f'{len(todos)} checkout(s) to clone into {repos_dir}: '
                + ', '.join(t[0] for t in todos), plan=plan)
    return step


def apply_repos(step, log, attempts=3):
    os.makedirs(step['clones'][0]['dest'].rsplit(os.sep, 1)[0], exist_ok=True)
    for c in step['clones']:
        ok = False
        for attempt in range(1, attempts + 1):
            proc = run(['git', 'clone', '--quiet', '-b', c['branch'], c['url'], c['dest']], timeout=3600)
            if proc.returncode == 0:
                ok = True
                break
            shutil.rmtree(c['dest'], ignore_errors=True)
            last = (proc.stderr.strip().splitlines() or ['?'])[-1][:200]
            if attempt < attempts:
                time.sleep(float(os.environ.get('ENV_PREPARE_RETRY_SLEEP', '5')))
        if not ok:
            step.update(status='failed', detail=f'git clone of {c["name"]} failed after {attempts} attempts: {last}')
            log.append({'step': 'repos', 'action': 'failed', 'detail': step['detail']})
            return False
        log.append({'step': 'repos', 'action': 'cloned', 'detail': f'{c["name"]} ({c["branch"]}) into {c["dest"]}'})
    step.update(status='done', detail='cloned ' + ', '.join(c['name'] for c in step['clones']))
    return True


# --- step 3: source-deps -----------------------------------------------------
def velox_checkout(repos_dir):
    for cand in (os.environ.get('VELOX_HOME'), os.path.join(repos_dir, 'velox')):
        if cand and os.path.isdir(os.path.join(cand, 'scripts')):
            return cand
    return None


def velox_setup_script(os_info):
    os_id = (os_info.get('ID') or '').lower()
    if os_id in ('ubuntu', 'debian'):
        return 'setup-ubuntu.sh'
    return 'setup-centos9.sh'


def velox_source_libs(setup_path):
    """The install_* calls inside install_velox_deps, in order, minus the
    package-manager and conda entries (the same parser as cpp-deps-check)."""
    libs, inside = [], False
    try:
        with open(setup_path, encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                if not inside:
                    if re.match(r'^function install_velox_deps \{', raw):
                        inside = True
                    continue
                if raw.startswith('}'):
                    break
                m = re.match(r'^\s*run_and_time\s+install_(\S+)\s*$', raw)
                if m and not m.group(1).endswith(('_from_dnf', '_from_apt')) and m.group(1) != 'conda':
                    libs.append(m.group(1))
    except OSError:
        return []
    return libs


def source_lib_present(name):
    for c in (name, name[:1].upper() + name[1:], name.upper()):
        if os.path.isdir(sys_path(f'/usr/local/lib/cmake/{c}')) or os.path.isdir(sys_path(f'/usr/local/lib64/cmake/{c}')):
            return True
    if os.path.isdir(sys_path(f'/usr/local/include/{name}')):
        return True
    if os.path.isfile(sys_path(f'/usr/local/include/{name}.h')) or os.path.isfile(sys_path(f'/usr/local/include/lib{name}.h')):
        return True
    override = SOURCE_PROBE_OVERRIDE.get(name)
    return bool(override) and os.access(sys_path(f'/usr/local/bin/{override}'), os.X_OK)


def step_source_deps(repos_dir, os_info, jobs, repos_step=None):
    step = {'name': 'source-deps', 'needs_root': True, 'actions': []}
    velox = velox_checkout(repos_dir)
    if not velox:
        cloning = repos_step and any(c['name'] == 'velox' for c in repos_step.get('clones', []))
        if cloning:  # the list lives in the checkout the repos step is about to make
            setup = os.path.join(repos_dir, 'velox', 'scripts', velox_setup_script(os_info))
            step.update(status='todo', deferred=True,
                        detail=f'library list is parsed from {setup} once the repos step has cloned velox',
                        plan=[f'# after the repos step: source {setup} && install_<lib>() for every library '
                              'missing under /usr/local, in setup-script order'])
        else:
            step.update(status='blocked', detail='no velox checkout with a scripts/ directory under '
                        f'{repos_dir} and the repos step will not clone one (set VELOX_HOME or --repos-dir)',
                        fix='point --repos-dir (or VELOX_HOME) at a velox checkout')
        return step
    setup = os.path.join(velox, 'scripts', velox_setup_script(os_info))
    if not os.path.isfile(setup):
        step.update(status='blocked', detail=f'{setup} not found in this velox checkout',
                    fix='check out the baseline branch of velox (scripts/setup-*.sh)')
        return step
    libs = velox_source_libs(setup)
    if not libs:
        step.update(status='blocked', detail=f'no install_* calls found in install_velox_deps inside {setup}')
        return step
    missing = [lib for lib in libs if not source_lib_present(lib)]
    step.update(setup=setup, libs=libs, missing=missing)
    if not missing:
        step.update(status='skipped', detail=f'all {len(libs)} source-built libraries present under /usr/local '
                    f'(list: {setup})', plan=['# nothing to build'])
        return step
    threads = min(32, jobs or (os.cpu_count() or 1))
    plan = [f'# INSTALL_PREFIX=/usr/local BUILD_THREADS={threads} PROMPT_ALWAYS_RESPOND=n']
    plan += [f'# source {setup} && install_{lib}()   (one subshell per library, in order)' for lib in missing]
    step.update(status='todo', detail=f'{len(missing)} of {len(libs)} source-built libraries missing: '
                f'{", ".join(missing)} (list: {setup})', plan=plan)
    return step


def apply_source_deps(step, log, jobs):
    setup, missing = step['setup'], step['missing']
    threads = min(32, jobs or (os.cpu_count() or 1))
    dep_dir = os.environ.get('GFVBOT_SOURCE_DEPS_DIR') or '/tmp/gfvbot-source-deps'
    env = dict(os.environ, INSTALL_PREFIX=sys_path('/usr/local'), DEPENDENCY_DIR=dep_dir,
               BUILD_THREADS=str(threads), PROMPT_ALWAYS_RESPOND='n')
    built = []
    for lib in missing:
        # a download cut mid-stream leaves a dir with no build entry file: velox would
        # treat it as populated and skip the fetch forever — wipe it first
        d = os.path.join(dep_dir, lib)
        if os.path.isdir(d) and not any(os.path.isfile(os.path.join(d, e)) for e in
                                        ('CMakeLists.txt', 'configure', 'Makefile', 'bootstrap.sh', 'setup.py')):
            shutil.rmtree(d, ignore_errors=True)
        proc = run_shell(f'source {json.dumps(setup)} > /dev/null; set +x; install_{lib}', env, timeout=14400)
        if proc.returncode != 0:
            tail = (proc.stderr.strip().splitlines() or ['?'])[-1][:200]
            step.update(status='failed', detail=f'install_{lib} failed ({tail}); later libraries build against '
                        'it — stopped', missing=missing, built=built)
            log.append({'step': 'source-deps', 'action': 'failed', 'detail': f'install_{lib}: {tail}'})
            return False
        built.append(lib)
        log.append({'step': 'source-deps', 'action': 'built', 'detail': f'install_{lib}() -> /usr/local'})
    step.update(status='done', detail=f'built {len(built)} source libraries into /usr/local: {", ".join(built)}')
    return True


# --- verify ------------------------------------------------------------------
ENV_CHECKS = (('toolchain', 'flink-velox-toolchain-check', 'toolchain_check.py'),
              ('build-tools', 'flink-velox-build-tools-check', 'build_tools_check.py'),
              ('cpp-deps', 'flink-velox-cpp-deps-check', 'cpp_deps_check.py'),
              ('jdk', 'flink-velox-jdk-check', 'jdk_check.py'),
              ('source-repos', 'flink-velox-source-repos-check', 'source_repos_check.py'))


def env_family_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.environ.get('ENV_PREPARE_ENV_FAMILY_DIR') or os.path.join(here, '..', '..', '..', 'flink-velox-env')


def verify(repos_dir):
    family = os.path.realpath(env_family_dir())
    results = []
    for name, skill, script in ENV_CHECKS:
        path = os.path.join(family, skill, 'scripts', script)
        if not os.path.isfile(path):
            results.append(make_item(name, 'skipped', f'{skill} is not installed next to this skill'))
            continue
        argv = [sys.executable, path, '--json']
        if name == 'source-repos' and repos_dir:
            argv += ['--repos-dir', repos_dir]
        proc = run(argv, timeout=600, env=dict(os.environ, GFV_REPOS_DIR=repos_dir) if repos_dir else None)
        try:
            payload = json.loads(proc.stdout)
            key = next(k for k in payload if k.endswith('_result'))
            r = payload[key]
            results.append(make_item(name, 'ok' if r.get('success') else 'fail',
                                     f'{len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                                     missing=r.get('missing') or []))
        except (ValueError, StopIteration, TypeError):
            results.append(make_item(name, 'fail', f'{skill} produced no result (exit {proc.returncode})'))
    return results


# --- assembly ----------------------------------------------------------------
def build_plan(repos_dir, jobs):
    os_info = read_os_release()
    supported, label = os_supported(os_info)
    packages = step_packages(os_info)
    repos = step_repos(repos_dir)
    source_deps = step_source_deps(repos_dir, os_info, jobs, repos)
    steps = [packages, repos, source_deps]
    todo = [s for s in steps if s['status'] == 'todo']
    blocked = [s for s in steps if s['status'] == 'blocked']
    root_needed = [s['name'] for s in todo if s['needs_root']]
    result = {
        'success': True,
        'checked_at': now(),
        'command': 'plan',
        'os': {'id': os_info.get('ID'), 'version': os_info.get('VERSION_ID'), 'supported': supported, 'label': label},
        'repos_dir': repos_dir,
        'steps': steps,
        'changes': [],
        'root_steps': root_needed,
        'next_step': plan_next(supported, blocked, todo, root_needed),
        'error_message': '',
    }
    return {KEY: result}, 0


def plan_next(supported, blocked, todo, root_needed):
    if not supported:
        return 'this distribution is not supported for apply (openEuler, CentOS/RHEL 9); install the packages by hand'
    if blocked:
        return blocked[0].get('fix') or f'fix the {blocked[0]["name"]} step, then rerun plan'
    if not todo:
        return 'ready: the build environment is complete (verify with: build_env_prepare.py verify)'
    prefix = 'as root: ' if root_needed else ''
    return f'{prefix}review the plan above, then run build_env_prepare.py apply --yes'


def apply_all(repos_dir, jobs, yes):
    os_info = read_os_release()
    supported, label = os_supported(os_info)
    log = []
    refusals = []
    if not yes:
        refusals.append('apply needs --yes (show the user the plan first; --yes is the record of their consent)')
    repos = step_repos(repos_dir)
    steps = [step_packages(os_info), repos, step_source_deps(repos_dir, os_info, jobs, repos)]
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        refusals.append(blocked[0].get('fix') or f'the {blocked[0]["name"]} step is blocked: {blocked[0]["detail"]}')
    todo_root = [s['name'] for s in steps if s['status'] == 'todo' and s['needs_root']]
    if not supported and todo_root:
        refusals.append(f'{label} is not supported for apply (openEuler, CentOS/RHEL 9)')
    if todo_root and euid() != 0:
        refusals.append('steps ' + ', '.join(todo_root) + ' need root: run "sudo python3 build_env_prepare.py apply --yes"')
    if steps[0]['status'] == 'todo' and not shutil.which('dnf'):
        refusals.append('dnf is not on PATH (the packages step needs it)')
    if refusals:
        result = {'success': False, 'checked_at': now(), 'command': 'apply', 'os': {'id': os_info.get('ID'), 'version': os_info.get('VERSION_ID'), 'supported': supported, 'label': label},
                  'repos_dir': repos_dir, 'steps': steps, 'changes': [], 'root_steps': [s['name'] for s in steps if s['needs_root']],
                  'next_step': '; '.join(refusals), 'error_message': 'refused: ' + '; '.join(refusals)}
        return {KEY: result}, 2
    appliers = {'packages': apply_packages, 'repos': apply_repos}
    for step in steps:
        if step['name'] == 'source-deps' and step.get('deferred'):
            # the checkout exists now: parse the real list before building
            step.clear()
            step.update(step_source_deps(repos_dir, os_info, jobs, repos))
            if step['status'] == 'skipped':
                log.append({'step': 'source-deps', 'action': 'skipped', 'detail': step['detail']})
                continue
            if step['status'] != 'todo':
                log.append({'step': 'source-deps', 'action': 'failed', 'detail': step['detail']})
                step['status'] = 'failed'
                break
        if step['status'] != 'todo':
            if step['status'] == 'skipped':
                log.append({'step': step['name'], 'action': 'skipped', 'detail': step['detail']})
            continue
        if step['name'] == 'source-deps':
            ok = apply_source_deps(step, log, jobs)
        else:
            ok = appliers[step['name']](step, log)
        if not ok:
            break
    failed = [s for s in steps if s['status'] == 'failed']
    done = [s for s in steps if s['status'] == 'done']
    result = {
        'success': not failed,
        'checked_at': now(),
        'command': 'apply',
        'os': {'id': os_info.get('ID'), 'version': os_info.get('VERSION_ID'), 'supported': supported, 'label': label},
        'repos_dir': repos_dir,
        'steps': steps,
        'changes': log,
        'root_steps': [s['name'] for s in steps if s['needs_root']],
        'next_step': ('fix the ' + failed[0]['name'] + ' step (' + failed[0]['detail'] + '), then rerun plan and apply — '
                      'it is idempotent' if failed else
                      f'{len(done)} step(s) done, nothing left to do; run build_env_prepare.py verify'),
        'error_message': f'{failed[0]["name"]} failed' if failed else '',
    }
    return {KEY: result}, 1 if failed else 0


def build_verify(repos_dir):
    items = verify(repos_dir)
    missing = [f'{i["name"]}: {i["detail"]}' for i in items if i['status'] == 'fail']
    result = {
        'success': not missing,
        'checked_at': now(),
        'command': 'verify',
        'os': {'id': None, 'version': None, 'supported': None, 'label': None},
        'repos_dir': repos_dir,
        'steps': [],
        'changes': [],
        'checks': items,
        'root_steps': [],
        'next_step': '' if not missing else 'rerun build_env_prepare.py plan for the remaining items',
        'error_message': '' if not missing else f'{len(missing)} check(s) failing',
    }
    return {KEY: result}, 0 if not missing else 1


def aborted(exc, command):
    return {KEY: {
        'success': False, 'checked_at': now(), 'command': command,
        'os': {'id': None, 'version': None, 'supported': None, 'label': None},
        'repos_dir': None, 'steps': [], 'changes': [], 'root_steps': [],
        'next_step': 'fix the tool error first (see error_message), then rerun',
        'error_message': f'tool aborted: {exc!r}',
    }}


def write_out(path, result, overwrite):
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('build_env_prepare.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'build_env_prepare.py: --out already exists, pass --overwrite to replace it: {path}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'build_env_prepare.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    lines = [f"build-env-prepare {r['command']}"
             + (f"  ({r['os']['label'] or r['os']['id'] or '?'}, {'supported' if r['os']['supported'] else 'NOT supported for apply'})"
                if r['os'].get('id') else '')]
    if r['command'] == 'verify':
        for i in r.get('checks', []):
            lines.append(f"  {i['status'].upper():<7}{i['name']}: {i['detail']}")
        lines.append('')
        lines.append('VERIFIED: every build-environment check passes' if r['success']
                     else f"NOT VERIFIED: {len([i for i in r['checks'] if i['status'] == 'fail'])} check(s) failing")
        lines.append(f"next: {r['next_step']}")
        return '\n'.join(line.rstrip() for line in lines)
    if r['error_message'].startswith('tool aborted'):
        lines.append(f"TOOL ERROR: {r['error_message']}")
        lines.append(f"next: {r['next_step']}")
        return '\n'.join(lines)
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
        lines.append('PLAN: ' + ('nothing to do' if not any(s['status'] == 'todo' for s in r['steps'])
                                 else f"{len([s for s in r['steps'] if s['status'] == 'todo'])} step(s) to apply"))
    else:
        lines.append('APPLIED: all steps done or already satisfied' if r['success']
                     else f"NOT APPLIED: {r['error_message']}; later steps not run")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare the build environment (packages, source checkouts, '
                                                 "Velox's source-built libraries) for the Flink + GlutenFlink stack. "
                                                 'plan by default; apply needs --yes.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--repos-dir', help='directory for velox/ velox4j/ gluten/ (default: $GFV_REPOS_DIR, then ~/repos)')
    parser.add_argument('--yes', action='store_true', help='apply: the record that the user saw the plan and consented')
    parser.add_argument('--jobs', type=int, help='build parallelism for the source libraries (default: min(32, cores))')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    repos_dir = locate_repos_dir(args.repos_dir)
    try:
        if args.command == 'plan':
            result, code = build_plan(repos_dir, args.jobs)
        elif args.command == 'apply':
            result, code = apply_all(repos_dir, args.jobs, args.yes)
        else:
            result, code = build_verify(repos_dir)
    except Exception as exc:  # a tool bug must never be reported as "applied"
        result, code = aborted(exc, args.command), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
