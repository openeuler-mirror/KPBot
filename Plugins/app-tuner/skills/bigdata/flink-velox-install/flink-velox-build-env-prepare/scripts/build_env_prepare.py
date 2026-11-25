#!/usr/bin/env python3
"""build_env_prepare.py — prepare the build environment for the Flink + GlutenFlink stack.

Three steps, each idempotent (probe first, skip what is already there):
  packages     the openEuler package set: toolchain (git cmake gcc gcc-c++ maven),
               build tools (ninja-build ccache autoconf automake libtool flex bison
               python3 patchelf), 13 C++ -devel libraries, java-17-openjdk-devel
  repos        clone velox / velox4j / gluten at their baseline branches into <repos-dir>;
               every attempt clones into a directory of its own and the checkout
               moves into place only once it is complete — what is already at the
               destination is reported, never removed
  source-deps  Velox's source-built C++ libraries: the list is parsed from the
                velox checkout's own setup script and each install_<lib>() is driven
                in a subshell with INSTALL_PREFIX=/usr/local (later libraries build
                against earlier ones, so the first failure stops the run). The
                checkout the setup script comes from (origin, commit, local
                modifications) and the script's sha256 are on record; with --plan apply
                re-verifies them against the plan's record and refuses to run a script
                that changed between plan and apply

Three subcommands, the same skeleton as every skill of the install family:
  plan    (default)  probe and print the exact commands and changes; touch nothing
  apply   --yes      execute; refuses without --yes, refuses root-requiring steps
                     when not run as root, stops at the first failing step
  verify             run the matching flink-velox-env checks and summarise

Usage:
  build_env_prepare.py [plan] [--repos-dir DIR] [--json] [--out FILE [--overwrite]]
  build_env_prepare.py apply --yes [--repos-dir DIR] [--jobs N] [--plan FILE] [--json] [--out FILE [--overwrite]]
  build_env_prepare.py verify [--repos-dir DIR] [--json]

<repos-dir>: --repos-dir, else $GFV_REPOS_DIR, else ~/repos. Supported for apply:
openEuler, CentOS / RHEL 9 (package names are openEuler's); other distributions
get a plan but apply refuses.

Exit codes: plan 0 / 2 usage; apply 0 every step done or already satisfied, 1 a
step failed (the report says what was done and what remains — rerun plan, it is
idempotent), 2 refused (no --yes, not root for root steps, unsupported OS, a
blocked step, a bad --out, an unreadable --plan record or a setup script that no
longer matches it); verify 0 all checks ok, 1 a check fails or a sibling
check skill is not installed (NOT VERIFIED), 2 check error.

Through sudo the clones run as the invoking user (SUDO_USER, via runuser); only
the package install and the source-built libraries into /usr/local need root.
Nothing of the user's is deleted: a directory that is not a usable checkout, or
a library source directory without a build entry, is reported and kept. --out is
validated before anything runs.

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT prefixes every
absolute system path (/etc/os-release, /usr/lib/jvm, /usr/local, ...);
ENV_PREPARE_HOME replaces ~; ENV_PREPARE_EUID overrides the effective uid;
ENV_PREPARE_ENV_FAMILY_DIR points verify at another flink-velox-env directory;
ENV_PREPARE_RETRY_SLEEP seconds between clone attempts (default 5).
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

KEY = 'flink_velox_build_env_prepare_result'
# The suggested commands carry this script's own path: they run as printed, from any directory.
SCRIPT = os.path.realpath(__file__)

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


def exc_text(exc):
    return f'{type(exc).__name__}: {exc}'


def invoking_user():
    """(name, home) of the user behind sudo, or None. The clones never need root:
    they run as that user, root is kept for dnf and /usr/local."""
    name = os.environ.get('SUDO_USER', '')
    if euid() != 0 or not name or name == 'root':
        return None
    try:
        home = pwd.getpwnam(name).pw_dir
    except KeyError:
        home = os.environ.get('ENV_PREPARE_HOME') or os.environ.get('HOME') or HOME
    return name, home


def user_home():
    if os.environ.get('ENV_PREPARE_HOME'):
        return os.environ['ENV_PREPARE_HOME']
    user = invoking_user()
    return user[1] if user else os.path.expanduser('~')


def as_user_prefix():
    user = invoking_user()
    return f'runuser -u {user[0]} -- ' if user else ''


def run_as(cmd, timeout=None, env=None, cwd=None):
    """A clone command: as the invoking user when we are root through sudo
    (`runuser -u <user> -- cmd` keeps the environment and switches HOME to that
    user's, so ~/<repos-dir> is theirs; util-linux 2.39)."""
    user = invoking_user()
    if not user:
        return run(cmd, timeout=timeout, env=env, cwd=cwd)
    return run(['runuser', '-u', user[0], '--', *cmd], timeout=timeout, env=env, cwd=cwd)


def user_ids():
    """(uid, gid) of the user behind sudo, or None when there is no such account."""
    user = invoking_user()
    if not user:
        return None
    try:
        entry = pwd.getpwnam(user[0])
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def hand_over(path):
    """Give a directory this process (root) just created to the user behind sudo: a demoted
    command has to be able to work inside it, and a directory root keeps to itself (0700)
    is out of its reach. A real chown, so only when really running as root."""
    ids = user_ids()
    if ids and os.geteuid() == 0:
        os.chown(path, *ids)


def git_says(repo, *args):
    """A read-only git answer about a checkout (None when there is no git or no checkout).
    Through sudo it runs as the invoking user: root never runs git inside their checkout
    (a repository's config can make git run a program, core.fsmonitor for one)."""
    if not shutil.which('git') or not os.path.isdir(os.path.join(repo, '.git')):
        return None
    try:
        proc = run_as(['git', '-C', repo, *args], timeout=120)
    except OSError:  # no runuser to hand the query to
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def source_of(repo):
    """Where a checkout comes from — on record so a build can be reviewed."""
    head = git_says(repo, 'rev-parse', 'HEAD')
    if head is None:
        return {'origin': None, 'head': None, 'dirty': None}
    return {'origin': git_says(repo, 'remote', 'get-url', 'origin'), 'head': head,
            'dirty': bool(git_says(repo, 'status', '--porcelain'))}


def source_text(src):
    if src['head'] is None:
        return 'not a git checkout'
    return f'{src["origin"] or "unknown origin"} @ {src["head"][:12]}' + (' with local modifications' if src['dirty'] else '')


def file_sha256(path):
    try:
        with open(path, 'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def plan_recorded_fingerprint(path):
    """The setup-script fingerprint a plan result recorded: {'sha256', 'origin', 'head'},
    or None when that plan recorded none (source-deps was deferred — the checkout, and
    the script, arrive with the apply's own clone, so there is nothing to compare against
    yet). This record is what the user consented to: apply re-probes the script it is
    about to run as root and refuses when the two differ. Raises ValueError when the
    file cannot be read as a plan result."""
    try:
        with open(path, encoding='utf-8') as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise ValueError(f'cannot read {path}: {exc.strerror or exc}') from None
    except ValueError as exc:  # json.JSONDecodeError is one
        raise ValueError(f'{path} is not valid JSON: {exc}') from None
    result = payload.get(KEY) if isinstance(payload, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get('steps'), list):
        raise ValueError(f'{path} holds no plan result (the JSON plan writes)')
    step = next((s for s in result['steps'] if s.get('name') == 'source-deps'), None)
    if step is None:
        raise ValueError(f'{path} holds no source-deps step')
    sha = step.get('setup_sha256')
    if not sha:
        return None
    src = step.get('source') or {}
    return {'sha256': sha, 'origin': src.get('origin'), 'head': src.get('head')}


def fingerprint_gap(recorded, sha, src):
    """What differs between the plan's record and the probed setup script — [] when they
    agree. The probed script is the one root is about to source."""
    gap = []
    if recorded['sha256'] != sha:
        gap.append(f'setup script sha256 {recorded["sha256"][:12]} -> {(sha or "?")[:12]}')
    if (recorded['origin'], recorded['head']) != (src.get('origin'), src.get('head')):
        gap.append(f'velox {(recorded["origin"] or "?")} @ {(recorded["head"] or "?")[:12]} -> '
                   f'{(src.get("origin") or "?")} @ {(src.get("head") or "?")[:12]}')
    return gap


def changed_between_plan_and_apply(gap):
    return ('the setup script changed between plan and apply (' + '; '.join(gap)
            + ') — rerun plan and review, then apply again')


def command_line(opts, sub, yes=False, sudo=False):
    """The command for the options that were actually reviewed, ready to copy: interpreter and
    the path of this script included (it is neither on PATH nor in the working directory)."""
    parts = ['sudo'] if sudo else []
    parts += ['python3', shlex.quote(SCRIPT), sub] + (['--yes'] if yes else [])
    for flag in ('repos_dir', 'jobs'):
        if flag in opts['explicit']:
            parts += [f'--{flag.replace("_", "-")}', shlex.quote(str(opts[flag]))]
    return ' '.join(parts)


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
    if not shutil.which('dnf'):
        step.update(status='blocked', detail='dnf is not on PATH: the packages step needs it',
                    fix='install dnf (the package manager of openEuler / CentOS), then rerun plan')
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
    return explicit or os.environ.get('GFV_REPOS_DIR') or os.path.join(user_home(), 'repos')


def step_repos(repos_dir, packages_step=None):
    """git is only needed to clone; what is already there is probed without it. When
    the packages step is about to install git, this step is not blocked on it."""
    step = {'name': 'repos', 'needs_root': False, 'actions': [], 'clones': []}
    git_later = bool(packages_step) and 'git' in packages_step.get('missing', [])
    if not shutil.which('git') and not git_later:
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
            plan.append(f'{as_user_prefix()}git clone -b {branch} {url} {dest}')
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
                + ', '.join(t[0] for t in todos)
                + ('; git is not on PATH yet — the packages step installs it first' if not shutil.which('git') else ''),
                plan=plan)
    return step


def apply_repos(step, log, attempts=3):
    user = invoking_user()
    for c in step['clones']:
        parent = os.path.dirname(c['dest'])
        if not os.path.isdir(parent):
            os.makedirs(parent)
            hand_over(parent)  # the user's own repos directory: what they clone later fits in
            log.append({'step': 'repos', 'action': 'created directory', 'detail': f'{parent}' + (f' for {user[0]}' if user else '')})
        ok, last = False, '?'
        for attempt in range(1, attempts + 1):
            # every attempt clones into a directory of its own; only that directory is
            # cleaned up, and the checkout moves into place only once it is complete
            tmp = tempfile.mkdtemp(prefix=f'.{c["name"]}-clone-', dir=parent)
            hand_over(tmp)  # root's 0700 directory would be out of reach for the demoted clone
            try:
                dest = os.path.join(tmp, c['name'])
                proc = run_as(['git', 'clone', '--quiet', '-b', c['branch'], c['url'], dest], timeout=3600)
                if proc.returncode == 0:
                    os.rename(dest, c['dest'])
                    ok = True
                    break
                last = (proc.stderr.strip().splitlines() or ['?'])[-1][:200]
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            log.append({'step': 'repos', 'action': 'clone failed', 'detail': f'attempt {attempt} ({c["name"]}): {last}'})
            if attempt < attempts:
                time.sleep(float(os.environ.get('ENV_PREPARE_RETRY_SLEEP', '5')))
        if not ok:
            step.update(status='failed', detail=f'git clone of {c["name"]} failed after {attempts} attempts: {last}')
            log.append({'step': 'repos', 'action': 'failed', 'detail': step['detail']})
            return False
        log.append({'step': 'repos', 'action': 'cloned',
                    'detail': f'{c["name"]} ({c["branch"]}) into {c["dest"]}' + (f' as {user[0]}' if user else '')})
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
    # the setup script is sourced and run as root: where it comes from is on record
    src = source_of(velox)
    sha = file_sha256(setup)
    step.update(source=src, setup_sha256=sha)
    missing = [lib for lib in libs if not source_lib_present(lib)]
    step.update(setup=setup, libs=libs, missing=missing)
    if not missing:
        step.update(status='skipped', detail=f'all {len(libs)} source-built libraries present under /usr/local '
                    f'(list: {setup})', plan=['# nothing to build'])
        return step
    if not shutil.which('bash'):
        step.update(status='blocked', detail='bash is not on PATH: the source-built libraries are installed by '
                    'driving velox\'s setup script in bash', fix='install bash, then rerun plan')
        return step
    threads = min(32, jobs or (os.cpu_count() or 1))
    plan = [f'# INSTALL_PREFIX=/usr/local BUILD_THREADS={threads} PROMPT_ALWAYS_RESPOND=n']
    plan += [f'# source {setup} && install_{lib}()   (one subshell per library, in order)' for lib in missing]
    step.update(status='todo', detail=f'{len(missing)} of {len(libs)} source-built libraries missing: '
                f'{", ".join(missing)} (list: {setup}; velox {source_text(src)}; setup script sha256 {(sha or "?")[:12]})',
                plan=plan)
    return step


def apply_source_deps(step, log, jobs):
    setup, missing = step['setup'], step['missing']
    threads = min(32, jobs or (os.cpu_count() or 1))
    dep_dir = os.environ.get('GFVBOT_SOURCE_DEPS_DIR') or '/tmp/gfvbot-source-deps'
    env = dict(os.environ, INSTALL_PREFIX=sys_path('/usr/local'), DEPENDENCY_DIR=dep_dir,
               BUILD_THREADS=str(threads), PROMPT_ALWAYS_RESPOND='n')
    built = []
    log.append({'step': 'source-deps', 'action': 'source recorded',
                'detail': f'{setup} (sha256 {file_sha256(setup)}) from {source_text(step.get("source") or {})}'})
    for lib in missing:
        # velox skips the fetch when the dependency directory exists: one without a build
        # entry file would be skipped forever. It is not ours to delete — report it and stop.
        d = os.path.join(dep_dir, lib)
        if os.path.isdir(d) and not any(os.path.isfile(os.path.join(d, e)) for e in
                                        ('CMakeLists.txt', 'configure', 'Makefile', 'bootstrap.sh', 'setup.py')):
            step.update(status='failed', missing=missing, built=built,
                        detail=f'install_{lib} not started: {d} exists and has no build entry file (a download cut '
                               'mid-stream?) — left in place; velox would skip the fetch as long as it is there')
            log.append({'step': 'source-deps', 'action': 'failed', 'detail': f'install_{lib}: {d} kept, no build entry file'})
            return False
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
# name, upstream skill, script, the PR that delivers the skill to this repo:
# toolchain / build-tools / cpp-deps are in via !268; jdk and source-repos arrive with
# !320 — until it merges they are not in the tree and verify reports them missing
# (saying so) instead of running a partial check that could pass as a verification.
ENV_CHECKS = (('toolchain', 'flink-velox-toolchain-check', 'toolchain_check.py', None),
              ('build-tools', 'flink-velox-build-tools-check', 'build_tools_check.py', None),
              ('cpp-deps', 'flink-velox-cpp-deps-check', 'cpp_deps_check.py', None),
              ('jdk', 'flink-velox-jdk-check', 'jdk_check.py', '!320'),
              ('source-repos', 'flink-velox-source-repos-check', 'source_repos_check.py', '!320'))


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


def verify(repos_dir):
    results = []
    for name, skill, script, pending_pr in ENV_CHECKS:
        path = checker_path(skill, script)
        if path is None:
            detail = f'{skill} is not installed next to this skill'
            extra = {}
            if pending_pr:
                detail += f' yet — delivered by {pending_pr} (pending merge)'
                extra['pending_pr'] = pending_pr
            results.append(make_item(name, 'missing', detail, skill=skill, **extra))
            continue
        argv = [sys.executable, path, '--json']
        if name == 'source-repos' and repos_dir:
            argv += ['--repos-dir', repos_dir]
        try:
            proc = run(argv, timeout=600, env=dict(os.environ, GFV_REPOS_DIR=repos_dir) if repos_dir else None)
        except Exception as exc:  # the checker did not start or did not finish
            results.append(make_item(name, 'fail', f'{skill} did not run: {exc_text(exc)}', skill=skill))
            continue
        try:
            payload = json.loads(proc.stdout)
            key = next(k for k in payload if k.endswith('_result'))
            r = payload[key]
            results.append(make_item(name, 'ok' if r.get('success') else 'fail',
                                     f'{len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                                     missing=r.get('missing') or [], skill=skill))
        except (ValueError, StopIteration, TypeError):
            results.append(make_item(name, 'fail', f'{skill} produced no result (exit {proc.returncode})', skill=skill))
    return results


# --- assembly ----------------------------------------------------------------
def build_plan(opts):
    repos_dir, jobs = opts['repos_dir'], opts['jobs']
    os_info = read_os_release()
    supported, label = os_supported(os_info)
    packages = step_packages(os_info)
    repos = step_repos(repos_dir, packages)
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
        'next_step': plan_next(opts, supported, blocked, todo, root_needed),
        'error_message': '',
    }
    return {KEY: result}, 0


def plan_next(opts, supported, blocked, todo, root_needed):
    if not supported:
        return 'this distribution is not supported for apply (openEuler, CentOS/RHEL 9); install the packages by hand'
    if blocked:
        return blocked[0].get('fix') or f'fix the {blocked[0]["name"]} step, then rerun plan'
    if not todo:
        return f'ready: the build environment is complete (verify with: {command_line(opts, "verify")})'
    prefix = 'as root: ' if root_needed else ''
    return f'{prefix}review the plan above, then run {command_line(opts, "apply", yes=True)}'


def apply_all(opts, yes):
    repos_dir, jobs = opts['repos_dir'], opts['jobs']
    os_info = read_os_release()
    supported, label = os_supported(os_info)
    log = []
    refusals = []
    if not yes:
        refusals.append('apply needs --yes (show the user the plan first; --yes is the record of their consent)')
    packages = step_packages(os_info)
    repos = step_repos(repos_dir, packages)
    steps = [packages, repos, step_source_deps(repos_dir, os_info, jobs, repos)]
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        refusals.append(f'the {blocked[0]["name"]} step is blocked: {blocked[0]["detail"]}'
                        + (f' — {blocked[0]["fix"]}' if blocked[0].get('fix') else ''))
    # the fingerprint the user consented to (the plan's record, handed in with --plan):
    # the script root is about to source is probed now and again right before it runs
    recorded = None
    if opts.get('plan'):
        try:
            recorded = plan_recorded_fingerprint(opts['plan'])
        except ValueError as exc:
            refusals.append(f'{exc} — pass the file plan wrote with --out, then apply again')
        if recorded:
            sd = next(s for s in steps if s['name'] == 'source-deps')
            if sd['status'] == 'todo' and sd.get('deferred'):
                refusals.append('the setup script changed between plan and apply: the velox checkout the '
                                f'plan recorded (setup sha256 {recorded["sha256"][:12]}) is no longer there '
                                'and apply would re-clone it — rerun plan and review, then apply again')
            elif sd['status'] == 'todo':
                gap = fingerprint_gap(recorded, sd.get('setup_sha256'), sd.get('source') or {})
                if gap:
                    refusals.append(changed_between_plan_and_apply(gap))
    todo_root = [s['name'] for s in steps if s['status'] == 'todo' and s['needs_root']]
    if not supported and todo_root:
        refusals.append(f'{label} is not supported for apply (openEuler, CentOS/RHEL 9)')
    if todo_root and euid() != 0:
        refusals.append('steps ' + ', '.join(todo_root) + ' need root: ' + command_line(opts, 'apply', yes=True, sudo=True))
    if invoking_user() and not shutil.which('runuser'):
        refusals.append('runuser is not on PATH (needed to run git as the invoking user)')
    if refusals:
        result = {'success': False, 'checked_at': now(), 'command': 'apply', 'os': {'id': os_info.get('ID'), 'version': os_info.get('VERSION_ID'), 'supported': supported, 'label': label},
                  'repos_dir': repos_dir, 'steps': steps, 'changes': [], 'root_steps': [s['name'] for s in steps if s['needs_root']],
                  'next_step': '; '.join(refusals), 'error_message': 'refused: ' + '; '.join(refusals)}
        return {KEY: result}, 2
    appliers = {'packages': apply_packages, 'repos': apply_repos}
    refused = ''  # a refusal that could only be found here: the script changed after this run's own probe
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
        if step['name'] == 'source-deps' and recorded:
            # the start-of-run probe can be minutes old by now (the package install): the
            # fingerprint is verified again, against the script root is about to source
            gap = fingerprint_gap(recorded, file_sha256(step['setup']),
                                  source_of(os.path.dirname(os.path.dirname(step['setup']))))
            if gap:
                refused = changed_between_plan_and_apply(gap)
                break
        try:
            ok = apply_source_deps(step, log, jobs) if step['name'] == 'source-deps' else appliers[step['name']](step, log)
        except Exception as exc:  # a command that cannot run or times out is this step's failure, not a crash
            step.update(status='failed', detail=f'{step["detail"]}; {exc_text(exc)}')
            log.append({'step': step['name'], 'action': 'failed', 'detail': exc_text(exc)})
            ok = False
        if not ok:
            break
    if refused:
        result = {'success': False, 'checked_at': now(), 'command': 'apply',
                  'os': {'id': os_info.get('ID'), 'version': os_info.get('VERSION_ID'), 'supported': supported, 'label': label},
                  'repos_dir': repos_dir, 'steps': steps, 'changes': log,
                  'root_steps': [s['name'] for s in steps if s['needs_root']],
                  'next_step': refused, 'error_message': 'refused: ' + refused}
        return {KEY: result}, 2
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
        'next_step': ('fix the ' + failed[0]['name'] + ' step (' + failed[0]['detail'] + '), then rerun plan and '
                      + command_line(opts, 'apply', yes=True) + ' — it is idempotent' if failed else
                      f'{len(done)} step(s) done, nothing left to do; run {command_line(opts, "verify")}'),
        'error_message': f'{failed[0]["name"]} failed' if failed else '',
    }
    return {KEY: result}, 1 if failed else 0


def build_verify(opts):
    repos_dir = opts['repos_dir']
    items = verify(repos_dir)
    failing = [i for i in items if i['status'] == 'fail']
    absent = [i for i in items if i['status'] == 'missing']
    error = ''
    if failing:
        error += f'{len(failing)} check(s) failing'
    if absent:
        error += '; ' if error else ''
        error += f'{len(absent)} check(s) not run (sibling skills not installed): ' + ', '.join(i['skill'] for i in absent)
    hints = []
    if absent:
        hint = f'install the missing check skill(s) ({", ".join(i["skill"] for i in absent)})'
        pending = sorted({i['pending_pr'] for i in absent if i.get('pending_pr')})
        if pending:
            hint += (f' — {", ".join(i["name"] for i in absent if i.get("pending_pr"))} come with '
                     f'{" and ".join(pending)} (still pending merge): install those skills or wait for the merge')
        hints.append(hint)
    if failing:
        hints.append('fix the failing check(s) above')
    next_step = '; '.join(hints)
    if next_step:
        next_step += f', then rerun {command_line(opts, "verify")}'
    missing = failing
    result = {
        'success': not error,
        'checked_at': now(),
        'command': 'verify',
        'os': {'id': None, 'version': None, 'supported': None, 'label': None},
        'repos_dir': repos_dir,
        'steps': [],
        'changes': [],
        'checks': items,
        'root_steps': [],
        'next_step': next_step,
        'error_message': error,
    }
    return {KEY: result}, 0 if not error else 1


def aborted(exc, command):
    return {KEY: {
        'success': False, 'checked_at': now(), 'command': command,
        'os': {'id': None, 'version': None, 'supported': None, 'label': None},
        'repos_dir': None, 'steps': [], 'changes': [], 'root_steps': [],
        'next_step': 'fix the tool error first (see error_message), then rerun',
        'error_message': f'tool aborted: {exc!r}',
    }}


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
        print(f'build_env_prepare.py: {problem}', file=sys.stderr)
        return 2
    target = os.path.abspath(path)
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
            lines.append(f"  {i['status'].upper():<9}{i['name']}: {i['detail']}")
        lines.append('')
        lines.append('VERIFIED: every build-environment check passes' if r['success']
                     else f"NOT VERIFIED: {r['error_message']}")
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
    parser.add_argument('--plan', help='apply: the plan result file (the JSON plan writes with --out) — the setup '
                                       'script fingerprint recorded in it is what --yes consented to; apply re-probes '
                                       'the script and refuses to run one that changed between plan and apply')
    parser.add_argument('--jobs', type=int, help='build parallelism for the source libraries (default: min(32, cores))')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    opts = {'repos_dir': locate_repos_dir(args.repos_dir), 'jobs': args.jobs,
            'explicit': {flag: getattr(args, flag) for flag in ('repos_dir', 'jobs') if getattr(args, flag) is not None},
            'plan': args.plan}
    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs: a bad --out must not change the machine
        if problem:
            print(f'build_env_prepare.py: {problem}', file=sys.stderr)
            result, code = aborted(problem, args.command), 2
            result[KEY].update(error_message='refused: ' + problem, next_step='fix --out, then rerun')
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
            return code
    try:
        if args.command == 'plan':
            result, code = build_plan(opts)
        elif args.command == 'apply':
            result, code = apply_all(opts, args.yes)
        else:
            result, code = build_verify(opts)
    except Exception as exc:  # a tool bug must never be reported as "applied"
        result, code = aborted(exc, args.command), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
