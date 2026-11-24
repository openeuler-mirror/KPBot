#!/usr/bin/env python3
"""runtime_install.py — install the Flink runtime and the nexmark benchmark pieces for the GlutenFlink stack.

Three steps, each idempotent (probe first, skip what is already there):
  flink         the official Flink binary distribution <prefix>/flink-<version>/
                (tgz from a mirror, else archive.apache.org; the sha512 always from
                archive.apache.org), the stable <prefix>/flink symlink, and a minimal
                conf appended to the config file Flink actually reads, only when that
                file is all comments
  nexmark-jar   nexmark-flink built from the nexmark source checkout at a pinned commit
                (mvn -pl nexmark-flink -am package -DskipTests), the jar copied into
                the lib/ of the version being installed; the jar's sha256 is on record —
                a nexmark jar already in lib is kept (idempotence), not silently: the
                skip recomputes its sha256 and names the requested ref, so a changed
                --nexmark-ref is seen as ignored, and removing the jar builds the new ref
  nexmark-tree  the runnable nexmark tree <nexmark-home>/ (bin/ conf/ queries/
                queries-cep from nexmark-flink/src/main/resources, plus lib/ log/ data/),
                checked against a manifest: what is missing gets copied, what is there
                is kept

Three subcommands, the same skeleton as every skill of the install family:
  plan    (default)  probe and print the exact commands and changes; touch nothing
  apply   --yes      execute; refuses without --yes, refuses root-requiring steps
                     when not run as root, stops at the first failing step
  verify             run the flink-velox-env flink-dist-check and nexmark-check

Usage:
  runtime_install.py [plan]    [--version 1.19.x] [--prefix /opt] [--nexmark-home /opt/nexmark]
                                        [--repos-dir DIR] [--mirror URL] [--nexmark-ref REF] [--no-nexmark]
                                        [--json] [--out FILE]
  runtime_install.py apply --yes [the same options]
  runtime_install.py verify [the same options]

Only the 1.19 line is accepted (major.minor; the stack is built and verified on it).
Root is required when <prefix> or the Flink lib/ is not writable by the current user.
Who builds what follows from who installs. Root installs (euid 0), so only the official
commit is built: --nexmark-ref must then name a commit (plan and apply refuse a tag /
branch — a moving ref would leave what is built to the checkout's owner), the checkout
must come from the official origin, be at the requested commit and report no local
modifications, and the build runs in a root-owned copy whose every file is compared
with the tree of that commit (git ls-tree at the pinned SHA, refs/replace off, the
guards taken again after the copy and the tree read) — the checkout's owner can
influence neither the jar nor the tree. Run through sudo, git runs as the invoking user
(SUDO_USER): the checkout is theirs, in directories root creates and hands over to them;
the Maven build is root's, with root's own ~/.m2. Without root (a prefix the user can
write) the user's checkout is built as it is, from a tag / branch too. Nothing of the
user's is deleted: an existing directory that is not a nexmark checkout is reported,
never removed; --out is validated before anything runs.

Exit codes: plan 0 / 2 usage or refused; apply 0 every step done or already satisfied,
1 a step failed (the report lists what was done — rerun, it is idempotent), 2 refused
(no --yes, not root for root steps, version off the 1.19 line, a blocked step, a bad
--out); verify 0 every check ok, 1 a check failed or a sibling check skill is not
installed (NOT VERIFIED), 2 usage.

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT prefixes every
absolute system path (/opt, /usr/lib/jvm, ...); ENV_PREPARE_HOME replaces ~;
ENV_PREPARE_EUID overrides the effective uid; ENV_PREPARE_PREFIX_WRITABLE=0 forces
the root requirement; ENV_PREPARE_ENV_FAMILY_DIR points verify at another
flink-velox-env directory; ENV_PREPARE_RETRY_SLEEP seconds between clone attempts.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

KEY = 'flink_velox_runtime_install_result'
# The suggested commands carry this script's own path: they run as printed, from any directory.
SCRIPT = os.path.realpath(__file__)

SYSROOT = os.environ.get('ENV_PREPARE_SYSROOT', '')
HOME = os.environ.get('ENV_PREPARE_HOME') or os.path.expanduser('~')

FLINK_LINE = (1, 19)
NEXMARK_REPO = 'https://github.com/nexmark/nexmark.git'
NEXMARK_REPO_SSH = 'git@github.com:nexmark/nexmark.git'
# The nexmark commit nexmark-flink is built from: upstream default branch as of 2025-12-26
# ("[Flink] Fix unexpected job cancellation (#68)"). A new checkout is fetched at exactly
# this commit; an existing checkout at another commit is reported, not silently built.
NEXMARK_PINNED_COMMIT = '6b3646c3baec701f1fa74baf938d235f742e5d3c'
DEFAULT_MIRROR = 'https://mirrors.huaweicloud.com/apache/flink'
ARCHIVE = 'https://archive.apache.org/dist/flink'
# Flink 1.19 reads flink-conf.yaml when both files exist and ignores config.yaml then.
CONF_FILES = ('flink-conf.yaml', 'config.yaml')
MINIMAL_CONF = (
    'jobmanager.rpc.address: localhost', 'jobmanager.rpc.port: 6123', 'jobmanager.bind-host: localhost',
    'taskmanager.bind-host: 0.0.0.0', 'taskmanager.host: localhost', 'taskmanager.data.bind-host: 0.0.0.0',
    'rest.bind-address: localhost', 'client.address: localhost', 'jobmanager.memory.process.size: 2048m',
    'taskmanager.memory.process.size: 8192m', 'taskmanager.numberOfTaskSlots: 8',
)
TREE_RESOURCES = ('bin', 'conf', 'queries', 'queries-cep')
# What run_query.sh needs besides the jar (its classpath is $NEXMARK_HOME/lib/*:$FLINK_HOME/lib/*).
TREE_MANIFEST = ('bin/run_query.sh', 'bin/config.sh', 'conf/log4j.properties', 'conf/nexmark.yaml', 'conf/config.yaml',
                 'queries/ddl_gen.sql', 'queries/ddl_kafka.sql', 'queries/ddl_views.sql', 'queries/insert_kafka.sql')


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


def exc_text(exc):
    return f'{type(exc).__name__}: {exc}'


def writable(path):
    """Whether the current user may create `path`: checked on the nearest existing
    ancestor (the target itself usually does not exist yet). The test seam can
    force 'not writable' to exercise the root requirement."""
    if os.environ.get('ENV_PREPARE_PREFIX_WRITABLE') == '0':
        return False
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return os.access(path, os.W_OK)


def invoking_user():
    """(name, home) of the user behind sudo, or None. The checkout lives in that user's
    home, so git runs as them; what root installs is built by root from a verified copy."""
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
    """A git command on the invoking user's checkout: as that user when we are root through
    sudo (`runuser -u <user> -- cmd` keeps the environment and switches HOME to that user's,
    so ~/repos is theirs; util-linux 2.39). Root itself never runs git in their checkout,
    and nothing that ends up in a directory root owns is built by them."""
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
    is out of its reach. A real chown, so only when really running as root. True when done."""
    ids = user_ids()
    if ids and os.geteuid() == 0:
        os.chown(path, *ids)
        return True
    return False


def command_line(opts, sub, yes=False, sudo=False):
    """The command for the options that were actually reviewed, ready to copy: interpreter and
    the path of this script included (it is neither on PATH nor in the working directory)."""
    parts = ['sudo'] if sudo else []
    parts += ['python3', shlex.quote(SCRIPT), sub] + (['--yes'] if yes else [])
    for flag in ('version', 'prefix', 'nexmark-home', 'repos-dir', 'mirror', 'nexmark-ref'):
        if flag.replace('-', '_') in opts['explicit']:
            parts += [f'--{flag}', shlex.quote(str(opts[flag.replace('-', '_')]))]
    if opts['no_nexmark']:
        parts.append('--no-nexmark')
    return ' '.join(parts)


# --- Flink -------------------------------------------------------------------
def flink_version_ok(version):
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)$', version or '')
    return bool(m) and (int(m.group(1)), int(m.group(2))) == FLINK_LINE


def effective_conf(dist):
    """The one config file Flink reads in this installation (flink-conf.yaml wins over config.yaml)."""
    for name in CONF_FILES:
        path = os.path.join(dist, 'conf', name)
        if os.path.isfile(path):
            return path
    return os.path.join(dist, 'conf', CONF_FILES[-1])


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
    conf = effective_conf(dist)
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
    if not os.access(os.path.join(dist, 'bin', 'flink'), os.X_OK) or conf_needs_minimal(conf):
        todos.append(('minimal-conf', [f'# append {len(MINIMAL_CONF)} keys to {conf} (only if every line is a comment)']))
    plan = [line for _, lines in todos for line in lines]
    step.update(dist=dist, link=link, tgz=tgz, conf=conf,
                pending=[name for name, _ in todos if name != 'skip-unpack'])
    missing_tools = [t for t in ('curl', 'sha512sum', 'tar') if 'download+unpack' in step['pending'] and not shutil.which(t)]
    if missing_tools:
        step.update(status='blocked', detail=f'{", ".join(missing_tools)} not on PATH — needed to download and unpack {tgz}',
                    fix='install the missing tool(s) (flink-velox-build-env-prepare installs them), then rerun plan', plan=plan)
        return step
    if not step['pending']:
        step.update(status='skipped', detail=f'Flink {version} at {dist} (via {link}); conf already set',
                    plan=['# nothing to do'])
        return step
    step.update(status='todo', detail=f'Flink {version}: ' + ', '.join(step['pending']) + f' into {prefix}', plan=plan)
    return step


def apply_flink(step, opts, log, ctx):
    version, prefix = opts['version'], opts['prefix']
    dist, link, tgz, conf = step['dist'], step['link'], step['tgz'], step['conf']
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
            log.append({'step': 'flink', 'action': 'downloaded', 'detail': f'{tgz} from {src}'})
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
            log.append({'step': 'flink', 'action': 'unpacked', 'detail': f'{tgz} (sha512 verified) -> {dist}'})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # this run's own download directory
    if 'symlink' in step['pending']:
        if os.path.lexists(link) and not os.path.islink(link):
            step.update(status='failed', detail=f'{link} exists and is not a symlink — not replaced')
            log.append({'step': 'flink', 'action': 'failed', 'detail': step['detail']})
            return False
        # a temp name of this run's own: nothing that is already there gets unlinked
        tmp_link = f'{link}.runtime-install.{os.getpid()}.{secrets.token_hex(4)}.tmp'
        os.symlink(dist, tmp_link)
        os.replace(tmp_link, link)
        log.append({'step': 'flink', 'action': 'symlinked', 'detail': f'{link} -> {dist}'})
    if 'minimal-conf' in step['pending']:
        conf = effective_conf(dist)
        if conf_needs_minimal(conf):
            with open(conf, 'a', encoding='utf-8') as fh:
                fh.write('\n'.join(MINIMAL_CONF) + '\n')
            log.append({'step': 'flink', 'action': 'configured', 'detail': f'appended {len(MINIMAL_CONF)} keys to {conf}'})
        else:
            log.append({'step': 'flink', 'action': 'skipped', 'detail': f'{conf} already has settings'})
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


def target_lib(opts):
    """lib/ of the version being installed — not of whatever <prefix>/flink points at today."""
    return os.path.join(opts['prefix'], f"flink-{opts['version']}", 'lib')


def nexmark_jar_in(lib):
    jars = sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, '*nexmark*.jar')))
    return jars[0] if jars else None


def deployed_jar_digests(lib):
    """[(name, sha256)] of the nexmark jars already in `lib` — several can sit there. The
    skip branch recomputes them, so the identity of what is kept stays on record without a
    build being forced; a jar that cannot be read is listed with None (probing must not
    fail on a lib the current user can only list)."""
    jars = []
    for path in sorted(glob.glob(os.path.join(lib, '*nexmark*.jar'))):
        try:
            with open(path, 'rb') as fh:
                jars.append((os.path.basename(path), hashlib.sha256(fh.read()).hexdigest()))
        except OSError:
            jars.append((os.path.basename(path), None))
    return jars


def is_commit(ref):
    return bool(re.fullmatch(r'[0-9a-f]{40}', ref or ''))


def clone_commands(dest, ref):
    """The git commands that put the checkout at `dest` at exactly `ref`."""
    if is_commit(ref):
        return [['git', 'init', '-q', dest],
                ['git', '-C', dest, 'remote', 'add', 'origin', NEXMARK_REPO],
                ['git', '-C', dest, 'fetch', '--depth', '1', 'origin', ref],
                ['git', '-C', dest, 'checkout', '-q', '--detach', 'FETCH_HEAD']]
    return [['git', 'clone', '--depth', '1', '--branch', ref, NEXMARK_REPO, dest]]


def git_says(src, *args):
    """A read-only git answer about the checkout. Through sudo it runs as the invoking user:
    root never runs git inside their checkout (a repository's config can make git run a
    program, core.fsmonitor for one). GIT_NO_REPLACE_OBJECTS: refs/replace must not be able
    to swap the object a pinned SHA names — rev-parse, status and ls-tree would otherwise
    describe a forgery of the checkout's owner while still looking clean."""
    try:
        proc = run_as(['git', '-C', src, *args], timeout=120,
                      env=dict(os.environ, GIT_NO_REPLACE_OBJECTS='1'))
    except OSError:  # no runuser to hand the query to
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def checkout_state(src):
    """'absent', 'complete' (a git checkout) or 'conflict' (something else sits there)."""
    if not os.path.lexists(src):
        return 'absent'
    return 'complete' if os.path.isdir(os.path.join(src, '.git')) else 'conflict'


def install_is_privileged():
    """True when root installs the artifacts — into directories the invoking user could not
    write themselves. What is built must then be exactly the official commit, whatever user
    the checkout belongs to and whether or not there is a sudo user to hand commands to."""
    return euid() == 0


def ref_must_be_a_commit(opts):
    """Why a privileged install cannot take a tag / branch: the ref is moving, so it is the
    checkout's owner — not the operator who consented — who decides what root builds and
    installs into the cluster's classpath. A user installing into a prefix they can write
    keeps the right to name a tag / branch (it is their own machine area)."""
    return not opts['no_nexmark'] and install_is_privileged() and not is_commit(opts['nexmark_ref'])


def untrusted_reasons(src, expected_head):
    """Why a checkout is not built: it is at another commit than the one asked for (always),
    or — when root installs the artifacts — it comes from another origin or reports local
    modifications. The same guard for a checkout that was already there and for one this
    run has just cloned; the copy that is built is verified against the commit on top."""
    reasons = []
    head = git_says(src, 'rev-parse', 'HEAD')
    if is_commit(expected_head) and head != expected_head:
        reasons.append(f'it is at {head or "an unknown commit"}, not the requested {expected_head}')
    if install_is_privileged():
        why = 'root installs the artifacts, so only the official commit is built'
        origin = git_says(src, 'remote', 'get-url', 'origin')
        if origin not in (NEXMARK_REPO, NEXMARK_REPO_SSH):
            reasons.append(f'its origin is {origin or "unknown"}, not {NEXMARK_REPO} ({why})')
        if git_says(src, 'status', '--porcelain', '--untracked-files=all'):
            reasons.append(f'it has local modifications ({why})')
    return reasons


def committed_tree(src, ref):
    """{path: (mode, blob hash)} of the files the pinned commit names. Read at the immutable
    SHA, not at the moving HEAD the checkout's owner controls, and with refs/replace off —
    nothing in the working tree or the repository's config can make it describe anything else."""
    entries = {}
    for item in (git_says(src, 'ls-tree', '-r', '-z', ref) or '').split('\0'):
        if item:
            meta, path = item.split('\t', 1)
            mode, kind, digest = meta.split()
            if kind == 'blob':
                entries[path] = (mode, digest)
    return entries


def blob_digest(data, sample):
    """The git blob hash of `data`, in the object format the tree uses (sha1 or sha256)."""
    digest = hashlib.new('sha256' if len(sample) == 64 else 'sha1')
    digest.update(b'blob %d\0' % len(data))
    digest.update(data)
    return digest.hexdigest()


def copy_differences(copy, tree):
    """How a copy of a checkout differs from the commit's tree: a file that is not in the
    commit, differs from it, carries another mode, or is missing."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(copy):
        for name in filenames:
            path = os.path.join(dirpath, name)
            found[os.path.relpath(path, copy).replace(os.sep, '/')] = path
        for name in dirnames:
            path = os.path.join(dirpath, name)  # a symbolic link to a directory is a file to git
            if os.path.islink(path):
                found[os.path.relpath(path, copy).replace(os.sep, '/')] = path
    differences = []
    for rel, path in sorted(found.items()):
        if rel not in tree:
            differences.append(f'{rel} is not in the commit')
            continue
        data = os.readlink(path).encode() if os.path.islink(path) else open(path, 'rb').read()
        mode, digest = tree[rel]
        if blob_digest(data, digest) != digest:
            differences.append(f'{rel} differs from the commit')
        elif (mode == '100755') != bool(os.lstat(path).st_mode & 0o111):
            differences.append(f'{rel} has another mode than in the commit')
    differences += [f'{rel} is missing' for rel in sorted(set(tree) - set(found))]
    return differences


def verified_copy(src, ref, where, log, step_name):
    """For a privileged install: a root-owned copy of the checkout — without its .git, so
    none of the owner's hooks or config reach the build — in which the build runs. Every
    file of the copy is compared with the tree of the pinned commit (read at the immutable
    SHA, refs/replace off), and after the copy and that read the guards are taken again:
    a checkout whose HEAD moved, whose origin changed or that got local modifications in
    between is not built. Returns (copy path or None, guard reasons, tree differences);
    the caller removes the copy's directory when it is done."""
    try:
        holder = tempfile.mkdtemp(prefix='.nexmark-build-', dir=where)
        copy = os.path.join(holder, 'src')
        shutil.copytree(src, copy, symlinks=True, ignore=shutil.ignore_patterns('.git'))
        tree = committed_tree(src, ref)
    except (OSError, shutil.Error) as exc:
        return None, [], [f'{src} cannot be copied into a root-owned directory: {exc_text(exc)}']
    again = untrusted_reasons(src, ref)
    if again:  # the same guards, after the copy and the baseline read
        shutil.rmtree(holder, ignore_errors=True)
        return None, again, []
    differences = (copy_differences(copy, tree) if tree else
                   [f'the commit {ref[:12]} cannot be listed (git ls-tree failed)'])
    if differences:
        shutil.rmtree(holder, ignore_errors=True)
        return None, [], differences
    log.append({'step': step_name, 'action': 'verified copy',
                'detail': f'{len(tree)} file(s) of {src} at {ref[:12]} copied into a root-owned build directory, '
                          f'every one equal to the commit'})
    return copy, [], []


def step_nexmark_jar(opts, flink_step):
    step = {'name': 'nexmark-jar', 'actions': []}
    lib = target_lib(opts)
    step['needs_root'] = not writable(lib)
    src = os.path.join(opts['repos_dir'], 'nexmark')
    ref = opts['nexmark_ref']
    state = checkout_state(src)
    step.update(src=src, lib=lib, ref=ref, checkout=state,
                builds_from='a verified copy' if install_is_privileged() else 'the checkout')
    jars = deployed_jar_digests(lib)
    if jars:
        # kept, not rebuilt — the idempotence contract; but never silently: the kept
        # jar's identity is recomputed and the requested ref is named, so an operator
        # passing another --nexmark-ref sees it ignored and learns how to build it
        kept = ', '.join(f'{name} (sha256 {digest or "not readable"})' for name, digest in jars)
        step.update(status='skipped',
                    detail=f'{kept} already in {lib} — kept, not rebuilt; this run asked for '
                           f'nexmark at {ref[:12]}; remove the jar(s) to build another ref '
                           f'(or install another version / prefix)',
                    plan=['# nothing to do'])
        return step
    blockers = []
    if state == 'conflict':
        blockers.append(f'{src} exists and is not a git checkout — it is not cloned over and not removed')
    if not shutil.which('mvn'):
        blockers.append('mvn is not on PATH (flink-velox-build-env-prepare installs maven)')
    if state == 'absent' and not shutil.which('git'):
        blockers.append('git is not on PATH (flink-velox-build-env-prepare installs git)')
    if invoking_user() and not shutil.which('runuser'):
        blockers.append('runuser is not on PATH (needed to run git as the invoking user)')
    jdk = find_jdk()
    if not jdk:
        blockers.append('no JDK found (JAVA_HOME or /usr/lib/jvm/java-17-* / java-1.8.0-openjdk-*)')
    if flink_step['status'] in ('blocked', 'failed'):
        blockers.append(flink_step['detail'])
    reasons = untrusted_reasons(src, ref) if state == 'complete' else []
    if reasons:
        blockers.append(f'the existing checkout at {src} is not used: ' + '; '.join(reasons))
    if blockers:
        fix = (f'move or remove {src} by hand, or point --repos-dir elsewhere' if state == 'conflict' else
               f'pass --nexmark-ref <commit> for the commit the checkout is at, or move it away' if reasons else
               'fix the above, then rerun plan')
        step.update(status='blocked', detail='; '.join(blockers), fix=fix)
        return step
    as_user = as_user_prefix()
    privileged = install_is_privileged()
    plan = []
    if state == 'absent':
        plan.append(' && '.join(as_user + ' '.join(c) for c in clone_commands(f'<tmp>/nexmark', ref))
                    + f'   (retries 3; the checkout then moves into place as {src})')
    if privileged:
        plan.append(f'# JAVA_HOME={jdk} mvn -pl nexmark-flink -am package -DskipTests   (in a root-owned copy of {src}, '
                    f'verified file by file against {ref[:12]}; Maven settings and local repository are root\'s)')
        plan.append(f'# cp nexmark-flink/target/nexmark-flink-*.jar (not -sources) from that copy -> {lib}/')
    else:
        plan.append(f'# {as_user}JAVA_HOME={jdk} mvn -pl nexmark-flink -am package -DskipTests   (in {src})')
        plan.append(f'# cp {src}/nexmark-flink/target/nexmark-flink-*.jar (not -sources) -> {lib}/')
    step.update(status='todo', jdk=jdk,
                detail=f'build nexmark-flink at {ref[:12]} from {src} with {jdk} and copy the jar into {lib}', plan=plan)
    return step


def apply_nexmark_jar(step, opts, log, ctx):
    lib, src, ref = step['lib'], step['src'], step['ref']
    privileged = install_is_privileged()
    user = invoking_user()
    if step['checkout'] == 'absent':
        repos = os.path.dirname(src)
        if not os.path.isdir(repos):
            os.makedirs(repos)
            if user and hand_over(repos):  # the user's own repos directory
                log.append({'step': 'nexmark-jar', 'action': 'created directory', 'detail': f'{repos} for {user[0]}'})
        head, error = None, 'git clone of nexmark did not run'
        for attempt in range(1, 4):
            # every attempt clones into a directory of its own; only that directory is cleaned up
            tmp = tempfile.mkdtemp(prefix='.nexmark-clone-', dir=repos)
            hand_over(tmp)  # root's 0700 directory would be out of reach for the demoted clone
            try:
                dest = os.path.join(tmp, 'nexmark')
                for cmd in clone_commands(dest, ref):
                    proc = run_as(cmd, timeout=3600)
                    if proc.returncode != 0:
                        break
                else:
                    head = git_says(dest, 'rev-parse', 'HEAD')
                    if is_commit(ref) and head != ref:
                        proc = subprocess.CompletedProcess(cmd, 1, '', f'FETCH_HEAD is {head}, expected {ref}')
                    else:
                        os.rename(dest, src)  # into place only once the checkout is complete
                        break
                error = f'git clone of nexmark failed: {last_line(proc.stderr)}'
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            log.append({'step': 'nexmark-jar', 'action': 'clone failed', 'detail': f'attempt {attempt}: {error}'})
            if attempt < 3:
                time.sleep(float(os.environ.get('ENV_PREPARE_RETRY_SLEEP', '5')))
        else:
            step.update(status='failed', detail=f'{error} (3 attempts)')
            log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': step['detail']})
            return False
        log.append({'step': 'nexmark-jar', 'action': 'cloned', 'detail': f'nexmark {head} into {src}'})
    build_dir = src
    if privileged:
        # the same guard as in the plan for a checkout that was already there
        reasons = untrusted_reasons(src, ref)
        if reasons:
            step.update(status='failed', detail=f'{src} is not built: ' + '; '.join(reasons))
            log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': step['detail']})
            return False
        # root installs the jar: it is built from a root-owned copy that was verified against
        # the commit, so neither the checkout's owner nor their ~/.m2 can influence it
        build_dir, again, differences = verified_copy(src, ref, opts['prefix'], log, 'nexmark-jar')
        if not build_dir:
            detail = (f'{src} is not built: ' + '; '.join(again) if again else
                      f'{src} is not what the commit {ref[:12]} holds, not built: '
                      + '; '.join(differences[:5]) + (' …' if len(differences) > 5 else ''))
            step.update(status='failed', detail=detail)
            log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': detail})
            return False
        ctx['build_holder'] = os.path.dirname(build_dir)
    env = dict(os.environ, JAVA_HOME=step['jdk'])
    if user:
        env['HOME'] = pwd.getpwnam('root').pw_dir  # Maven reads root's own ~/.m2, not the invoking user's
    # unprivileged: the user's own build in their own checkout; privileged: root builds the
    # verified copy (run_as would hand it back to the invoking user — exactly what must not happen)
    proc = run(['mvn', '-pl', 'nexmark-flink', '-am', 'package', '-DskipTests'], cwd=build_dir, env=env, timeout=14400)
    if proc.returncode != 0:
        step.update(status='failed', detail='mvn build of nexmark-flink failed: ' + last_line(proc.stdout + proc.stderr))
        log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': step['detail']})
        return False
    jars = [j for j in sorted(glob.glob(os.path.join(build_dir, 'nexmark-flink', 'target', 'nexmark-flink-*.jar')))
            if 'sources' not in os.path.basename(j)]
    if not jars:
        step.update(status='failed', detail=f'no nexmark-flink-*.jar under {build_dir}/nexmark-flink/target after the build')
        log.append({'step': 'nexmark-jar', 'action': 'failed', 'detail': step['detail']})
        return False
    with open(jars[0], 'rb') as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    os.makedirs(lib, exist_ok=True)
    shutil.copy2(jars[0], lib)
    name = os.path.basename(jars[0])
    log.append({'step': 'nexmark-jar', 'action': 'built+deployed',
                'detail': f'{name} (sha256 {digest}) from {src} at {ref[:12]}' + (' via a verified root-owned copy' if privileged else '') + f' -> {lib}'})
    step.update(status='done', detail=f'{name} built from {src} at {ref[:12]} and copied into {lib}')
    return True


def tree_missing(home):
    """Entries of the runnable-tree manifest that are absent (or not executable) under `home`."""
    missing = []
    for rel in TREE_MANIFEST:
        path = os.path.join(home, *rel.split('/'))
        if not os.path.isfile(path) or (rel.endswith('.sh') and not os.access(path, os.X_OK)):
            missing.append(rel)
    if not glob.glob(os.path.join(home, 'queries', 'q*.sql')):
        missing.append('queries/q*.sql')
    return missing


def step_nexmark_tree(opts, jar_step):
    step = {'name': 'nexmark-tree', 'actions': []}
    home = opts['nexmark_home']
    step['needs_root'] = not writable(home)
    src, res = jar_step['src'], os.path.join(jar_step['src'], 'nexmark-flink', 'src', 'main', 'resources')
    step['home'] = home
    missing = tree_missing(home)
    if not missing:
        step.update(status='skipped', detail=f'runnable tree at {home} (complete)', plan=['# nothing to do'])
        return step
    step['missing'] = missing
    if jar_step['status'] == 'blocked':
        step.update(status='blocked', detail=jar_step['detail'], fix=jar_step.get('fix'))
        return step
    # only when no build is planned does the tree depend on a source checkout that is already there
    if jar_step['status'] == 'skipped' and not all(os.path.isdir(os.path.join(res, d)) for d in TREE_RESOURCES):
        clone = ' && '.join(' '.join(c) for c in clone_commands(src, opts['nexmark_ref']))
        step.update(status='blocked', detail=f'nexmark source resources not found at {res} — the tree cannot be completed from them',
                    fix=clone)
        return step
    privileged = install_is_privileged()
    plan = [f'mkdir -p {home}',
            f'# copy what is missing ({len(missing)} manifest entr{"y" if len(missing) == 1 else "ies"}: {", ".join(missing)}) '
            f'and the rest of {{bin,conf,queries,queries-cep}} from {res}' + (' — a root-owned copy verified against the commit'
                                                                             if privileged else '') + '; existing files are kept',
            f'mkdir -p {home}/{{lib,log,data}}',
            f'# cp {jar_step["lib"]}/nexmark-flink-*.jar -> {home}/lib/   (when not there yet)']
    step.update(status='todo', res=res, src=src, ref=opts['nexmark_ref'],
                detail=f'complete the runnable tree at {home} (missing {len(missing)}: {", ".join(missing)}) from {res}', plan=plan)
    return step


def copy_tree(src, dst):
    """Copy the files of `src` that `dst` does not have yet; returns how many. Plain
    recursion instead of copytree(dirs_exist_ok=True): that needs Python 3.8."""
    copied = 0
    os.makedirs(dst, exist_ok=True)
    for entry in sorted(os.listdir(src)):
        s, d = os.path.join(src, entry), os.path.join(dst, entry)
        if os.path.isdir(s) and not os.path.islink(s):
            os.makedirs(d, exist_ok=True)
            copied += copy_tree(s, d)
        elif not os.path.lexists(d):
            shutil.copy2(s, d)
            copied += 1
    return copied


def apply_nexmark_tree(step, opts, log, ctx):
    home = step['home']
    res = step['res']
    if install_is_privileged() and 'build_holder' not in ctx:
        # the jar was deployed earlier; the files root copies now come from a verified copy too
        reasons = untrusted_reasons(step['src'], step['ref'])
        copy, again, differences = ((None, reasons, []) if reasons else
                                    verified_copy(step['src'], step['ref'], opts['prefix'], log, 'nexmark-tree'))
        if not copy:
            problems = reasons or again
            detail = (f'{step["src"]} is not used: ' + '; '.join(problems) if problems else
                      f'{step["src"]} is not what the commit {step["ref"][:12]} holds, not used: '
                      + '; '.join(differences[:5]) + (' …' if len(differences) > 5 else ''))
            step.update(status='failed', detail=detail)
            log.append({'step': 'nexmark-tree', 'action': 'failed', 'detail': detail})
            return False
        ctx['build_holder'] = os.path.dirname(copy)
    if 'build_holder' in ctx:
        res = os.path.join(ctx['build_holder'], 'src', 'nexmark-flink', 'src', 'main', 'resources')
    missing_dirs = [d for d in TREE_RESOURCES if not os.path.isdir(os.path.join(res, d))]
    if missing_dirs:
        step.update(status='failed', detail=f'{res} lacks {", ".join(missing_dirs)} — not the nexmark-flink resources layout')
        log.append({'step': 'nexmark-tree', 'action': 'failed', 'detail': step['detail']})
        return False
    fresh = not os.path.isdir(home)
    os.makedirs(home, exist_ok=True)
    copied = sum(copy_tree(os.path.join(res, d), os.path.join(home, d)) for d in TREE_RESOURCES)
    for d in ('lib', 'log', 'data'):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    jar = nexmark_jar_in(target_lib(opts))
    if jar and not os.path.exists(os.path.join(home, 'lib', jar)):
        shutil.copy2(os.path.join(target_lib(opts), jar), os.path.join(home, 'lib', jar))
    still = tree_missing(home)
    log.append({'step': 'nexmark-tree', 'action': 'laid out' if fresh else 'completed',
                'detail': f'{copied} file(s) from {res}{{/bin,/conf,/queries,/queries-cep}} -> {home}' + (f' (jar {jar})' if jar else '')})
    if still:
        step.update(status='failed', detail=f'{home} still lacks {", ".join(still)} after copying from {res}')
        log.append({'step': 'nexmark-tree', 'action': 'failed', 'detail': step['detail']})
        return False
    step.update(status='done', detail=f'runnable tree at {home}')
    return True


# --- verify ------------------------------------------------------------------
ENV_CHECKS = (('flink-dist', 'flink-velox-flink-dist-check', 'flink_dist_check.py'),
              ('nexmark', 'flink-velox-nexmark-check', 'nexmark_check.py'))


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
    with_nexmark = not opts['no_nexmark']
    env = dict(os.environ, FLINK_HOME=opts['prefix'] + '/flink', NEXMARK_HOME=opts['nexmark_home'],
               ENV_CHECK_SYSROOT=SYSROOT)
    items = []
    for name, skill, script in ENV_CHECKS:
        if name == 'nexmark' and not with_nexmark:
            items.append({'name': name, 'skill': skill, 'status': 'disabled', 'detail': '--no-nexmark: not checked'})
            continue
        path = checker_path(skill, script)
        if path is None:
            items.append({'name': name, 'skill': skill, 'status': 'missing',
                          'detail': f'{skill} is not installed next to this skill'})
            continue
        try:
            proc = run([sys.executable, path, '--json'], timeout=600, env=env)
        except Exception as exc:  # the checker did not start or did not finish
            items.append({'name': name, 'skill': skill, 'status': 'fail', 'detail': f'{skill} did not run: {exc_text(exc)}'})
            continue
        try:
            payload = json.loads(proc.stdout)
            r = payload[next(k for k in payload if k.endswith('_result'))]
            items.append({'name': name, 'skill': skill, 'status': 'ok' if r.get('success') else 'fail',
                          'detail': f'{len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                          'missing': r.get('missing') or []})
        except (ValueError, StopIteration, TypeError):
            items.append({'name': name, 'skill': skill, 'status': 'fail',
                          'detail': f'{skill} produced no result (exit {proc.returncode})'})
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
    apply_cmd = command_line(opts, 'apply', yes=True)
    blocked = [s for s in steps if s['status'] == 'blocked']
    if blocked:
        return blocked[0].get('fix') or f'fix the {blocked[0]["name"]} step, then rerun plan'
    todo = [s for s in steps if s['status'] == 'todo']
    if command == 'plan':
        if not todo:
            return 'ready: the Flink runtime' + ('' if opts['no_nexmark'] else ' and nexmark') + \
                   f' are installed (verify with: {command_line(opts, "verify")})'
        root_needed = [s['name'] for s in todo if s['needs_root']]
        return ('as root: ' if root_needed else '') + f'review the plan above, then run {apply_cmd}'
    if command == 'verify':
        return ''
    failed = [s for s in steps if s['status'] == 'failed']
    if failed:
        return f'fix the {failed[0]["name"]} step ({failed[0]["detail"]}), then rerun plan and {apply_cmd} — it is idempotent'
    return f'{len([s for s in steps if s["status"] == "done"])} step(s) done, nothing left to do; run {command_line(opts, "verify")}'


def base_result(command, steps, changes, opts, success, error, next_step, checks=None):
    r = {'success': success, 'checked_at': now(), 'command': command, 'version': opts['version'],
         'prefix': opts['prefix'], 'flink_home': opts['prefix'] + '/flink',
         'nexmark_home': None if opts['no_nexmark'] else opts['nexmark_home'],
         'steps': steps, 'changes': changes,
         'root_steps': [s['name'] for s in steps if s.get('needs_root')], 'next_step': next_step, 'error_message': error}
    if checks is not None:
        r['checks'] = checks
    return {KEY: r}


def version_refusal(command, opts):
    return base_result(command, [], [], opts, False, f'refused: Flink {opts["version"]} is not on the 1.19 line',
                       'pick a 1.19.x version (major.minor 1.19; the stack is built and verified on it)'), 2


def moving_ref_refusal(command, opts):
    return base_result(command, [], [], opts, False,
                       f'refused: --nexmark-ref {opts["nexmark_ref"]} is not a commit — a privileged install '
                       '(root) builds only a commit pinned by --nexmark-ref, never a moving tag / branch',
                       'pass --nexmark-ref <commit> (40 hex digits) — a tag / branch is only for a user '
                       'installing into a prefix they can write themselves'), 2


def build_plan(opts):
    if not flink_version_ok(opts['version']):
        return version_refusal('plan', opts)
    if ref_must_be_a_commit(opts):
        return moving_ref_refusal('plan', opts)
    steps = build_steps(opts)
    return base_result('plan', steps, [], opts, True, '', next_step_for('plan', steps, opts)), 0


def apply_all(opts, yes):
    if not flink_version_ok(opts['version']):
        return version_refusal('apply', opts)
    if ref_must_be_a_commit(opts):
        return moving_ref_refusal('apply', opts)
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
        refusals.append('steps ' + ', '.join(root_steps) + ' need root (not writable): '
                        + command_line(opts, 'apply', yes=True, sudo=True))
    if refusals:
        return base_result('apply', steps, [], opts, False, 'refused: ' + '; '.join(refusals), '; '.join(refusals)), 2
    log = []
    ctx = {}  # what lives across the steps: the root-owned build directory of a privileged install
    appliers = {'flink': apply_flink, 'nexmark-jar': apply_nexmark_jar, 'nexmark-tree': apply_nexmark_tree}
    try:
        for step in steps:
            if step['status'] == 'skipped':
                log.append({'step': step['name'], 'action': 'skipped', 'detail': step['detail']})
                continue
            try:
                ok = appliers[step['name']](step, opts, log, ctx)
            except Exception as exc:  # a command that cannot run or times out is this step's failure, not a crash
                step.update(status='failed', detail=f'{step["detail"]}; {exc_text(exc)}')
                log.append({'step': step['name'], 'action': 'failed', 'detail': exc_text(exc)})
                ok = False
            if not ok:
                break
    finally:
        if ctx.get('build_holder'):
            shutil.rmtree(ctx['build_holder'], ignore_errors=True)  # this run's own build directory
    failed = [s for s in steps if s['status'] == 'failed']
    return base_result('apply', steps, log, opts, not failed,
                       f'{failed[0]["name"]} failed' if failed else '',
                       next_step_for('apply', steps, opts)), 1 if failed else 0


def build_verify(opts):
    checks = verify(opts)
    failing = [c for c in checks if c['status'] == 'fail']
    missing = [c for c in checks if c['status'] == 'missing']
    error = ''
    if failing:
        error += f'{len(failing)} check(s) failing'
    if missing:
        error += '; ' if error else ''
        error += f'{len(missing)} check(s) not run (sibling skills not installed): ' + ', '.join(c['skill'] for c in missing)
    next_step = ''
    if missing or failing:
        next_step = ''
        if missing:
            next_step += f'install the missing check skill(s) ({", ".join(c["skill"] for c in missing)})'
            if failing:
                next_step += ' and fix the failing check(s) above'
        else:
            next_step += 'fix the failing check(s) above'
        next_step += f', then rerun {command_line(opts, "verify")}'
    r = base_result('verify', [], [], opts, not error, error, next_step, checks)
    return r, 0 if not error else 1


def aborted(exc, command, opts):
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'version': opts.get('version'),
                  'prefix': opts.get('prefix'), 'flink_home': None, 'nexmark_home': None, 'steps': [], 'changes': [],
                  'root_steps': [], 'next_step': 'fix the tool error first (see error_message), then rerun',
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
        print(f'runtime_install.py: {problem}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w' if overwrite else 'x', encoding='utf-8') as fh:
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
            lines.append(f"  {c['status'].upper():<9}{c['name']}: {c['detail']}")
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
    parser.add_argument('--version', help='Flink version, 1.19 line only (default 1.19.2)')
    parser.add_argument('--prefix', help='where flink-<version>/ and the flink symlink live (default /opt)')
    parser.add_argument('--nexmark-home', help='runnable nexmark tree (default /opt/nexmark)')
    parser.add_argument('--repos-dir', help='directory for the nexmark source checkout (default: $GFV_REPOS_DIR, then ~/repos)')
    parser.add_argument('--mirror', help='mirror base for the Flink tgz (sha512 always from archive.apache.org)')
    parser.add_argument('--nexmark-ref', help='nexmark commit to build (default: the pinned commit '
                                              f'{NEXMARK_PINNED_COMMIT}); a tag / branch only for a user installing '
                                              'into a prefix they can write themselves — a privileged install refuses it')
    parser.add_argument('--no-nexmark', action='store_true', help='install only the Flink runtime')
    parser.add_argument('--yes', action='store_true', help='apply: the record that the user saw the plan and consented')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing regular file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    explicit = {flag: getattr(args, flag) for flag in ('version', 'prefix', 'nexmark_home', 'repos_dir', 'mirror', 'nexmark_ref')
                if getattr(args, flag) is not None}
    opts = {'version': args.version or '1.19.2',
            'prefix': args.prefix or sys_path('/opt'),
            'nexmark_home': args.nexmark_home or sys_path('/opt/nexmark'),
            'mirror': args.mirror or DEFAULT_MIRROR,
            'nexmark_ref': args.nexmark_ref or NEXMARK_PINNED_COMMIT,
            'no_nexmark': args.no_nexmark,
            'repos_dir': args.repos_dir or os.environ.get('GFV_REPOS_DIR') or os.path.join(user_home(), 'repos'),
            'explicit': explicit}
    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs: a bad --out must not change the machine
        if problem:
            print(f'runtime_install.py: {problem}', file=sys.stderr)
            result, code = base_result(args.command, [], [], opts, False, 'refused: ' + problem, 'fix --out, then rerun'), 2
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
            return code
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
