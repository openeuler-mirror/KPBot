#!/usr/bin/env python3
"""stack_build.py — build velox4j and gluten-flink from the GlutenFlink source checkouts and
deploy the GlutenFlink build into the Flink lib/ directory.

Five steps (the build steps always rebuild — sources may have changed):
  backup        only what this build replaces is moved to <lib>.bak/<timestamp>/
                (copied, then removed from lib): with --only velox4j the velox4j jar,
                with --only gluten the gluten jars, with both the whole GlutenFlink set and
                the runtime dependencies — so lib never holds two versions of one
                rebuilt artifact, and what is not rebuilt stays where it is
  velox4j       mvn clean install -DskipTests in the velox4j checkout, built against
                the workspace velox checkout (VELOX4J_VELOX_SOURCE_DIR); the
                velox4j-*.jar from target/ goes into lib/
  gluten        the nexmark-flink jar (when present in lib/) is installed into the
                local Maven repository first (gluten-flink depends on it), then
                mvn clean install -DskipTests -pl planner,loader,runtime in
                gluten-flink; planner / runtime / loader jars go into lib/
  runtime-deps  the 14 third-party jars the GlutenFlink jars need at runtime (guava, arrow,
                jackson, flatbuffers, commons-io) copied from ~/.m2/repository into
                lib/ — resolved AFTER the builds (they populate ~/.m2); one still
                missing fails the step: lib is incomplete and the cluster must not
                be restarted onto it
  pin-jdk       the config file Flink actually reads (flink-conf.yaml when it exists,
                else config.yaml) gets env.java.home: <build JDK> only when the key is
                absent (gluten jars carry Java 17 bytecode); a different existing
                value is left untouched and warned about

Every build records where it comes from: the checkout's origin, its commit and
whether it carries local modifications are in the plan and in the change log.
Jar names are matched by glob, never hard-coded versions. Run through sudo, the
Maven builds still run as the invoking user (SUDO_USER): root is used for the
backup and the deploy into lib/ only. Three subcommands, the same skeleton as
every skill of the install family: plan (default) prints the exact commands and
touches nothing; apply --yes executes (stops at the first failing step, records
every change); verify runs the flink-velox-env lib-state-check with --expect gfv.

Usage:
  stack_build.py [plan]    [--repos-dir DIR] [--flink-home DIR] [--only velox4j|gluten|all] [--jobs N] [--json] [--out FILE]
  stack_build.py apply --yes [the same options]
  stack_build.py verify [the same options]

<repos-dir>: --repos-dir, else $GFV_REPOS_DIR, else ~/repos of the invoking user (a
checkout can be pinned with $VELOX_HOME / $VELOX4J_HOME / $GLUTEN_HOME). Flink:
--flink-home (then nothing else), else $FLINK_HOME, a flink on PATH, /opt/flink,
/usr/local/flink. JDK 17: $JAVA_HOME, the java on PATH, /usr/lib/jvm/java-17-* —
each candidate's real major version is checked. Root is required when lib/ is not
writable by the current user.

Prerequisites follow --only — a checkout no step of the requested build reads does not
block: all three for all, velox and velox4j for --only velox4j (the native .so compiles
against the velox source), gluten for --only gluten. And gluten-flink resolves velox4j
from ~/.m2 (a SNAPSHOT no remote repo serves), so a gluten-only build needs velox4j built
before — the artifact named by the velox4j.version property of gluten-flink/pom.xml,
checked in the invoking user's ~/.m2 (a full build installs it in the same run, before
the gluten step; JDK, mvn, Flink and runuser are always checked).

Exit codes: plan 0 / 2 usage or refused; apply 0 every step done, 1 a step failed
(the report says what was done — the replaced set is kept under lib.bak/), 2
refused (no --yes, not root for root steps, a blocked prerequisite, a bad --out);
verify 0 every check ok, 1 a check failed or the sibling check skill is not
installed (NOT VERIFIED), 2 usage or refused (an explicit --flink-home that is
not a Flink installation — the sibling check never falls back to another Flink).

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT, ENV_PREPARE_HOME,
ENV_PREPARE_EUID, ENV_PREPARE_PREFIX_WRITABLE=0, ENV_PREPARE_ENV_FAMILY_DIR.
"""
import argparse
import datetime
import glob
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
import zipfile

KEY = 'flink_velox_stack_build_result'
# The suggested commands carry this script's own path: they run as printed, from any directory.
SCRIPT = os.path.realpath(__file__)

SYSROOT = os.environ.get('ENV_PREPARE_SYSROOT', '')
HOME = os.environ.get('ENV_PREPARE_HOME') or os.path.expanduser('~')

REPOS = (('velox', 'VELOX_HOME', ('CMakeLists.txt', 'scripts')),
         ('velox4j', 'VELOX4J_HOME', ('pom.xml',)),
         ('gluten', 'GLUTEN_HOME', ('gluten-flink/pom.xml',)))
# Which checkouts the requested build reads — the checkout prerequisites follow --only
# (JDK, mvn, Flink and runuser are always checked, whatever --only says).
ONLY_REPOS = {'all': ('velox', 'velox4j', 'gluten'),
              'velox4j': ('velox', 'velox4j'),  # velox4j's native .so compiles against the velox source
              'gluten': ('gluten',)}            # the gluten build reads no other checkout
# gluten-flink's planner / runtime depend on velox4j (provided scope) and resolve it from the
# local Maven repository — a SNAPSHOT no remote repo serves, so it is in ~/.m2 only after the
# velox4j checkout has been built once. The version is the velox4j.version property of
# gluten-flink/pom.xml; the group is pinned here like the RUNTIME_DEPS coordinates below.
GLUTEN_VELOX4J_GROUP = 'io.github.zhztheplayer'
# group:artifact:version the GlutenFlink jars need at runtime beyond Flink's own lib/
RUNTIME_DEPS = (
    'com.google.guava:guava:33.4.0-jre', 'com.google.guava:failureaccess:1.0.2',
    'org.apache.arrow:arrow-c-data:18.1.0', 'org.apache.arrow:arrow-format:18.1.0',
    'org.apache.arrow:arrow-memory-core:18.1.0', 'org.apache.arrow:arrow-memory-unsafe:18.1.0',
    'org.apache.arrow:arrow-vector:18.1.0', 'commons-io:commons-io:2.18.0',
    'com.google.flatbuffers:flatbuffers-java:24.3.25',
    'com.fasterxml.jackson.core:jackson-annotations:2.18.0', 'com.fasterxml.jackson.core:jackson-core:2.18.0',
    'com.fasterxml.jackson.core:jackson-databind:2.18.0',
    'com.fasterxml.jackson.datatype:jackson-datatype-jdk8:2.18.0',
    'com.fasterxml.jackson.datatype:jackson-datatype-jsr310:2.18.0',
)
GFV_JAR_RE = re.compile(r'^(gluten|velox4j)', re.I)
MVN_COMMON = ['-Dgpg.skip', '-Dgpg.skip=true', '-Dmaven.javadoc.skip=true']
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


def exc_text(exc):
    return f'{type(exc).__name__}: {exc}'


def writable(path):
    if os.environ.get('ENV_PREPARE_PREFIX_WRITABLE') == '0':
        return False
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return os.access(path, os.W_OK)


def invoking_user():
    """(name, home) of the user behind sudo, or None. The Maven builds never need
    root: they run as that user, root is kept for the backup and the deploy."""
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
    """A build command: as the invoking user when we are root through sudo.
    `runuser -u <user> -- cmd` keeps the environment and switches HOME to that
    user's, so ~/.m2 is theirs (util-linux 2.39)."""
    user = invoking_user()
    if not user:
        return run(cmd, timeout=timeout, env=env, cwd=cwd)
    return run(['runuser', '-u', user[0], '--', *cmd], timeout=timeout, env=env, cwd=cwd)


# --- prerequisites ----------------------------------------------------------
def locate_repos_dir(explicit):
    return explicit or os.environ.get('GFV_REPOS_DIR') or os.path.join(user_home(), 'repos')


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
    """Through sudo as the invoking user: root never runs git inside their checkout (a
    repository's config can make git run a program, core.fsmonitor for one)."""
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


def find_checkouts(repos_dir):
    found = {}
    for name, env, markers in REPOS:
        path = os.environ.get(env) or os.path.join(repos_dir, name)
        absent = [m for m in markers if not os.path.exists(os.path.join(path, *m.split('/')))]
        found[name] = {'path': os.path.realpath(path) if not absent else path, 'ok': not absent,
                       'missing': absent, 'pinned_via': env if os.environ.get(env) else None}
    return found


def gluten_velox4j_version(gluten_path):
    """The velox4j version gluten-flink's planner / runtime resolve — a property of its pom.
    None when the pom cannot be read or says nothing: then the gluten-only prerequisite
    below cannot be verified and the plan says so instead of hoping."""
    try:
        with open(os.path.join(gluten_path, 'gluten-flink', 'pom.xml'), encoding='utf-8', errors='replace') as fh:
            m = re.search(r'<velox4j\.version>\s*([^<]+?)\s*</velox4j\.version>', fh.read())
    except OSError:
        return None
    return m.group(1) if m else None


def jdk_major(home):
    try:
        with open(os.path.join(home, 'release'), encoding='utf-8', errors='replace') as fh:
            m = re.search(r'^JAVA_VERSION="([^"]+)"', fh.read(), re.M)
        if m:
            v = m.group(1)
            return int(v.split('.')[1]) if v.startswith('1.') else int(v.split('.')[0])
    except OSError:
        pass
    return None


def usable_jdk17(cand):
    """The candidate really is a JDK 17: its release file says so (a directory named
    java-17-* may hold something else)."""
    return bool(cand) and os.access(os.path.join(cand, 'bin', 'javac'), os.X_OK) and jdk_major(cand) == 17


def find_jdk17():
    jh = os.environ.get('JAVA_HOME') or ''
    if usable_jdk17(jh):
        return jh, 'JAVA_HOME'
    java = shutil.which('java')
    if java:
        home = os.path.dirname(os.path.dirname(os.path.realpath(java)))
        if usable_jdk17(home):
            return home, 'PATH'
    for cand in sorted(glob.glob(sys_path('/usr/lib/jvm/java-17-*'))):
        if usable_jdk17(cand):
            return cand, '/usr/lib/jvm'
    return None, None


def find_flink_home(explicit):
    """(home, source). An explicit --flink-home is the only candidate."""
    if explicit:
        cand = os.path.abspath(os.path.expanduser(explicit))
        return (cand, '--flink-home') if os.access(os.path.join(cand, 'bin', 'flink'), os.X_OK) else (None, '--flink-home')
    cands = []
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


def effective_conf(flink_home):
    """The one config file Flink reads in this installation (flink-conf.yaml wins over config.yaml)."""
    for name in CONF_FILES:
        path = os.path.join(flink_home, 'conf', name)
        if os.path.isfile(path):
            return path
    return os.path.join(flink_home, 'conf', CONF_FILES[-1])


def prereqs(repos_dir, flink_home=None, only='all'):
    p = {'checkouts': find_checkouts(repos_dir), 'jdk': None, 'jdk_via': None, 'mvn': bool(shutil.which('mvn')),
         'flink_home': None, 'flink_via': None, 'lib': None, 'problems': []}
    p['jdk'], p['jdk_via'] = find_jdk17()
    p['flink_home'], p['flink_via'] = find_flink_home(flink_home)
    required = ONLY_REPOS[only]
    for name, c in p['checkouts'].items():
        if not c['ok']:
            if name in required:  # a checkout no step of this build reads is not its blocker
                p['problems'].append(f"no usable {name} checkout at {c['path']} (missing {', '.join(c['missing'])}); "
                                     f"flink-velox-build-env-prepare clones it, or set {REPOS[[r[0] for r in REPOS].index(name)][1]}")
        else:
            c['source'] = source_of(c['path'])
    if only == 'gluten' and p['checkouts']['gluten']['ok']:
        # gluten-flink resolves velox4j from ~/.m2, not from any checkout: a gluten-only build
        # needs velox4j built before (a full build installs it in the same run, before this step)
        pom = os.path.join(p['checkouts']['gluten']['path'], 'gluten-flink', 'pom.xml')
        version = gluten_velox4j_version(p['checkouts']['gluten']['path'])
        if version is None:
            p['problems'].append(f'cannot read the velox4j version from {pom} (gluten-flink resolves velox4j '
                                 'from ~/.m2); update this skill to match the gluten-flink pom')
        elif not os.path.isfile(dep_spec(f'{GLUTEN_VELOX4J_GROUP}:velox4j:{version}')[3]):
            p['problems'].append(f'velox4j {version} is not in ~/.m2 (gluten-flink resolves it from there); '
                                  'build it first with --only velox4j, or run the full build')
    if not p['jdk']:
        p['problems'].append('no JDK 17 (JAVA_HOME, the java on PATH, /usr/lib/jvm/java-17-*); flink-velox-build-env-prepare installs it')
    if not p['mvn']:
        p['problems'].append('mvn is not on PATH; flink-velox-build-env-prepare installs maven')
    if flink_home and not p['flink_home']:
        p['problems'].append(f'--flink-home {flink_home} is not a Flink installation (no bin/flink)')
    elif not p['flink_home']:
        p['problems'].append('no Flink installation (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink); '
                             'flink-velox-runtime-install installs it')
    else:
        p['lib'] = os.path.join(p['flink_home'], 'lib')
    if invoking_user() and not shutil.which('runuser'):
        p['problems'].append('runuser is not on PATH (needed to run the Maven builds as the invoking user)')
    return p


# --- steps ------------------------------------------------------------------
def lib_jars(lib, pattern):
    return sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, pattern)))


def dep_spec(dep):
    g, a, v = dep.split(':')
    return g, a, v, os.path.join(user_home(), '.m2', 'repository', g.replace('.', '/'), a, v, f'{a}-{v}.jar')


def resolve_deps():
    found, missing = [], []
    for dep in RUNTIME_DEPS:
        (found if os.path.isfile(dep_spec(dep)[3]) else missing).append(dep)
    return found, missing


def backup_scope(only):
    """Which artifacts this build replaces — and so moves out of lib first."""
    return {'velox4j': ('velox4j',), 'gluten': ('gluten',), 'all': ('gluten', 'velox4j', 'deps')}[only]


def build_steps(p, only, jobs):
    lib = p['lib']
    steps = []
    existing = lib_jars(lib, '*.jar') if lib and os.path.isdir(lib) else []
    scope = backup_scope(only)
    replaced = [j for j in existing if GFV_JAR_RE.match(j) and any(j.lower().startswith(s) for s in scope if s != 'deps')]
    if 'deps' in scope:
        for dep in RUNTIME_DEPS:
            replaced += [j for j in existing if j.startswith(dep_spec(dep)[1] + '-')]
    backup_list = sorted(set(replaced))
    kept = [j for j in existing if GFV_JAR_RE.match(j) and j not in backup_list]
    note = (f'; not rebuilt, left in lib: {", ".join(kept)}' if kept else '') + \
           ('; the runtime dependencies stay in lib' if only != 'all' else '')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    steps.append({'name': 'backup', 'needs_root': not writable(lib), 'actions': [],
                  'status': 'todo' if backup_list else 'skipped',
                  'detail': (f'{len(backup_list)} jar(s) in lib would be overwritten by this build: {", ".join(backup_list)}'
                             if backup_list else 'lib holds nothing this build replaces') + note,
                  'plan': [f'mkdir -p {lib}.bak/{stamp}']
                  + [f'cp {lib}/{" ".join(backup_list)} {lib}.bak/{stamp}/   # then removed from lib']
                  if backup_list else ['# nothing to back up'],
                  'backup_dir': os.path.join(lib + '.bak', stamp), 'backup_list': backup_list, 'lib': lib})
    cores = os.cpu_count() or 1
    jobs4j = min(8, jobs or cores)
    as_user = as_user_prefix()
    velox4j = p['checkouts']['velox4j']['path']
    if only in ('all', 'velox4j'):
        src4j, srcvelox = p['checkouts']['velox4j']['source'], p['checkouts']['velox']['source']
        steps.append({'name': 'velox4j', 'needs_root': not writable(lib), 'actions': [], 'status': 'todo',
                      'detail': f'mvn clean install -DskipTests in {velox4j} (against {p["checkouts"]["velox"]["path"]}, JDK {p["jdk"]}); '
                                f'velox4j-*.jar -> lib; source {source_text(src4j)} / velox {source_text(srcvelox)}',
                      'plan': [f'# JAVA_HOME={p["jdk"]} VELOX4J_VELOX_SOURCE_DIR={p["checkouts"]["velox"]["path"]} VELOX4J_BUILD_JOBS={jobs4j}',
                               f'{as_user}mvn clean install -DskipTests {" ".join(MVN_COMMON)}   (in {velox4j})',
                               f'cp {velox4j}/target/velox4j-*.jar {lib}/'],
                      'repo': velox4j, 'jobs': jobs4j, 'source': src4j, 'velox_source': srcvelox})
    if only in ('all', 'gluten'):
        gluten = os.path.join(p['checkouts']['gluten']['path'], 'gluten-flink')
        src = p['checkouts']['gluten']['source']
        nexmark = lib_jars(lib, '*nexmark*.jar') if lib and os.path.isdir(lib) else []
        plan = []
        if nexmark:
            plan += [f'# install nexmark-flink into ~/.m2 first (gluten-flink depends on it):',
                     f'#   unzip {lib}/{nexmark[0]} META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml -> <tmp>',
                     f'#   {as_user}mvn install:install-file -Dfile={lib}/{nexmark[0]} -DpomFile=<tmp>/pom.xml']
        else:
            plan.append('# no nexmark jar in lib: the nexmark install-file step is skipped (gluten-flink may fail to resolve it)')
        plan += [f'# JAVA_HOME={p["jdk"]}',
                 f'{as_user}mvn clean install -DskipTests -pl planner,loader,runtime {" ".join(MVN_COMMON)}   (in {gluten})',
                 f'cp {gluten}/planner/target/gluten-flink-planner-*.jar {gluten}/runtime/target/gluten-flink-runtime-*.jar '
                 f'{gluten}/loader/target/gluten-flink-loader-*.jar {lib}/']
        steps.append({'name': 'gluten', 'needs_root': not writable(lib), 'actions': [], 'status': 'todo',
                      'detail': f'mvn clean install -DskipTests -pl planner,loader,runtime in {gluten} (JDK {p["jdk"]}); 3 jars -> lib'
                                + (f'; nexmark {nexmark[0]} installed into ~/.m2 first' if nexmark else '; no nexmark jar in lib')
                                + f'; source {source_text(src)}',
                      'plan': plan, 'repo': gluten, 'source': src,
                      'nexmark_jar': os.path.join(lib, nexmark[0]) if nexmark else None})
    if only == 'all':
        found, missing = resolve_deps()  # a snapshot: resolved again after the builds
        plan = [f'cp {dep_spec(d)[3]} {lib}/' for d in found] if found else []
        if missing:
            plan.append(f'# not in ~/.m2 now (the builds above populate it; still missing after them = this step fails): {", ".join(missing)}')
        steps.append({'name': 'runtime-deps', 'needs_root': not writable(lib), 'actions': [], 'status': 'todo',
                      'detail': f'{len(found)} of {len(RUNTIME_DEPS)} runtime dependencies in ~/.m2 now, re-checked after the builds -> lib'
                                + (f'; {len(missing)} not there yet' if missing else ''),
                      'plan': plan or ['# nothing in ~/.m2 yet — re-checked after the builds'], 'found': found, 'missing': missing})
        conf = effective_conf(p['flink_home'])
        cur = None
        try:
            with open(conf, encoding='utf-8', errors='replace') as fh:
                m = re.search(r'^env\.java\.home:\s*(\S+)', fh.read(), re.M)
                cur = m.group(1) if m else None
        except OSError:
            cur = None
        if cur is None:
            steps.append({'name': 'pin-jdk', 'needs_root': not writable(os.path.dirname(conf)), 'actions': [], 'status': 'todo',
                          'detail': f'{conf} has no env.java.home: append env.java.home: {p["jdk"]} (gluten jars carry Java 17 bytecode)',
                          'plan': [f'echo "env.java.home: {p["jdk"]}" >> {conf}'], 'conf': conf, 'action': 'append'})
        elif os.path.realpath(cur) != os.path.realpath(p['jdk']):
            steps.append({'name': 'pin-jdk', 'needs_root': not writable(os.path.dirname(conf)), 'actions': [], 'status': 'skipped',
                          'detail': f'{conf} pins env.java.home to {cur} but this build uses {p["jdk"]}; left untouched (WARNING)',
                          'plan': ['# left untouched — the configured value belongs to the user'], 'conf': conf, 'action': 'warn'})
        else:
            steps.append({'name': 'pin-jdk', 'needs_root': False, 'actions': [], 'status': 'skipped',
                          'detail': f'{conf} already pins env.java.home to {cur}', 'plan': ['# nothing to do'], 'conf': conf, 'action': 'none'})
    return steps


# --- apply ------------------------------------------------------------------
def apply_backup(step, p, log):
    """What this build replaces moves to lib.bak/<stamp>/ (copied, then removed from
    lib) so lib never holds two versions of one rebuilt artifact after the deploy."""
    target = step['backup_dir']
    n = 0
    while os.path.exists(target):  # a rerun within the same second gets its own directory
        n += 1
        target = f"{step['backup_dir']}-{n}"
    step['backup_dir'] = target
    os.makedirs(target)
    for jar in step['backup_list']:
        shutil.copy2(os.path.join(step['lib'], jar), step['backup_dir'])
        os.unlink(os.path.join(step['lib'], jar))
        log.append({'step': 'backup', 'action': 'backed up', 'detail': f'{step["lib"]}/{jar} -> {step["backup_dir"]}/{jar}'})
    step['status'] = 'done'
    return True


def mvn_build(repo, args, jdk, env_extra, timeout=14400):
    env = dict(os.environ, JAVA_HOME=jdk, PATH=os.path.join(jdk, 'bin') + os.pathsep + os.environ.get('PATH', ''), **env_extra)
    return run_as(['mvn', *args, *MVN_COMMON], cwd=repo, env=env, timeout=timeout)


def apply_velox4j(step, p, log):
    env_extra = {'VELOX4J_VELOX_SOURCE_DIR': p['checkouts']['velox']['path'], 'VELOX4J_BUILD_JOBS': str(step['jobs'])}
    proc = mvn_build(step['repo'], ['clean', 'install', '-DskipTests'], p['jdk'], env_extra)
    if proc.returncode != 0:
        step.update(status='failed', detail='mvn build of velox4j failed: ' + last_line(proc.stdout + proc.stderr))
        log.append({'step': 'velox4j', 'action': 'failed', 'detail': step['detail']})
        return False
    jars = sorted(glob.glob(os.path.join(step['repo'], 'target', 'velox4j-*.jar')))
    jars = [j for j in jars if 'sources' not in os.path.basename(j) and 'javadoc' not in os.path.basename(j)]
    if not jars:
        step.update(status='failed', detail=f'no velox4j-*.jar under {step["repo"]}/target after the build')
        log.append({'step': 'velox4j', 'action': 'failed', 'detail': step['detail']})
        return False
    shutil.copy2(jars[0], p['lib'])
    log.append({'step': 'velox4j', 'action': 'built+deployed',
                'detail': f'{os.path.basename(jars[0])} -> {p["lib"]} (source: {source_text(step["source"])})'})
    step.update(status='done', detail=f'{os.path.basename(jars[0])} built and deployed')
    return True


def apply_gluten(step, p, log):
    if step.get('nexmark_jar'):
        try:
            with zipfile.ZipFile(step['nexmark_jar']) as zf:
                pom = zf.read('META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml')
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            step.update(status='failed', detail=f'nexmark jar {step["nexmark_jar"]} has no nexmark-flink pom.xml: {exc}')
            log.append({'step': 'gluten', 'action': 'failed', 'detail': step['detail']})
            return False
        tmp = tempfile.mkdtemp(prefix='stack-build-')
        hand_over(tmp)  # the demoted Maven run has to read the pom from inside it
        try:
            pom_path = os.path.join(tmp, 'pom.xml')
            with open(pom_path, 'wb') as fh:
                fh.write(pom)
            proc = mvn_build(tmp, ['install:install-file', f'-Dfile={step["nexmark_jar"]}', f'-DpomFile={pom_path}'], p['jdk'], {})
            if proc.returncode != 0:
                step.update(status='failed', detail='mvn install:install-file of nexmark-flink failed: ' + last_line(proc.stdout + proc.stderr))
                log.append({'step': 'gluten', 'action': 'failed', 'detail': step['detail']})
                return False
            log.append({'step': 'gluten', 'action': 'installed nexmark-flink into ~/.m2', 'detail': os.path.basename(step['nexmark_jar'])})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # this run's own temp directory
    proc = mvn_build(step['repo'], ['clean', 'install', '-DskipTests', '-pl', 'planner,loader,runtime'], p['jdk'], {})
    if proc.returncode != 0:
        step.update(status='failed', detail='mvn build of gluten-flink failed: ' + last_line(proc.stdout + proc.stderr))
        log.append({'step': 'gluten', 'action': 'failed', 'detail': step['detail']})
        return False
    deployed = []
    for module in ('planner', 'runtime', 'loader'):
        jars = [j for j in sorted(glob.glob(os.path.join(step['repo'], module, 'target', f'gluten-flink-{module}-*.jar')))
                if 'sources' not in os.path.basename(j) and 'javadoc' not in os.path.basename(j)]
        if not jars:
            step.update(status='failed', detail=f'no gluten-flink-{module}-*.jar under {step["repo"]}/{module}/target after the build')
            log.append({'step': 'gluten', 'action': 'failed', 'detail': step['detail']})
            return False
        shutil.copy2(jars[0], p['lib'])
        deployed.append(os.path.basename(jars[0]))
    log.append({'step': 'gluten', 'action': 'built+deployed',
                'detail': ', '.join(deployed) + f' -> {p["lib"]} (source: {source_text(step["source"])})'})
    step.update(status='done', detail=', '.join(deployed) + ' built and deployed')
    return True


def apply_runtime_deps(step, p, log):
    """Resolved here, after the builds: they are what populates ~/.m2."""
    found, missing = resolve_deps()
    step.update(found=found, missing=missing)
    for dep in found:
        shutil.copy2(dep_spec(dep)[3], p['lib'])
    log.append({'step': 'runtime-deps', 'action': 'deployed',
                'detail': f'{len(found)} runtime dependencies from {os.path.join(user_home(), ".m2", "repository")} -> {p["lib"]}'})
    if missing:
        # a GlutenFlink set without its runtime dependencies does not load: not "done with a warning"
        step.update(status='failed',
                    detail=f'{len(missing)} of {len(RUNTIME_DEPS)} runtime dependencies are not in ~/.m2 after the builds: {", ".join(missing)}'
                           ' — lib is incomplete')
        log.append({'step': 'runtime-deps', 'action': 'failed', 'detail': 'not in ~/.m2 after the builds: ' + ', '.join(missing)})
        return False
    step.update(status='done', detail=f'{len(found)} runtime dependencies deployed')
    return True


def apply_pin_jdk(step, p, log):
    if step['action'] == 'append':
        os.makedirs(os.path.dirname(step['conf']), exist_ok=True)
        with open(step['conf'], 'a', encoding='utf-8') as fh:
            fh.write(f'env.java.home: {p["jdk"]}\n')
        log.append({'step': 'pin-jdk', 'action': 'configured', 'detail': f'env.java.home: {p["jdk"]} appended to {step["conf"]}'})
        step['status'] = 'done'
    else:
        log.append({'step': 'pin-jdk', 'action': 'skipped', 'detail': step['detail']})
    return True


# --- assembly ---------------------------------------------------------------
def base_result(command, p, steps, changes, success, error, next_step, checks=None):
    r = {'success': success, 'checked_at': now(), 'command': command,
         'repos': {k: v['path'] for k, v in p['checkouts'].items()}, 'jdk': p['jdk'], 'flink_home': p['flink_home'],
         'lib': p['lib'], 'steps': steps, 'changes': changes,
         'root_steps': [s['name'] for s in steps if s.get('needs_root')], 'next_step': next_step, 'error_message': error}
    if checks is not None:
        r['checks'] = checks
    return {KEY: r}


def command_line(opts, sub, yes=False, sudo=False):
    """The command for the options that were actually reviewed, ready to copy: interpreter and
    the path of this script included (it is neither on PATH nor in the working directory)."""
    parts = ['sudo'] if sudo else []
    parts += ['python3', shlex.quote(SCRIPT), sub] + (['--yes'] if yes else [])
    for flag in ('repos_dir', 'only', 'jobs', 'flink_home'):
        if flag in opts['explicit']:
            parts += [f'--{flag.replace("_", "-")}', shlex.quote(str(opts[flag]))]
    return ' '.join(parts)


def blocked_steps(p):
    """One blocked step standing for every missing prerequisite."""
    return [{'name': 'prereqs', 'needs_root': False, 'actions': [], 'status': 'blocked',
             'detail': '; '.join(p['problems']), 'fix': 'fix the above (flink-velox-build-env-prepare / flink-velox-runtime-install), then rerun plan',
             'plan': []}]


def restart_note(opts):
    return (f'restart the cluster so it loads the new build (flink-velox-cluster-switch, needs consent), '
            f'then run {command_line(opts, "verify")}')


def build_plan(opts):
    p = prereqs(opts['repos_dir'], opts['flink_home'], opts['only'])
    if p['problems']:
        steps = blocked_steps(p)
        return base_result('plan', p, steps, [], True, '', steps[0]['fix']), 0
    steps = build_steps(p, opts['only'], opts['jobs'])
    todo = [s for s in steps if s['status'] == 'todo']
    root_needed = [s['name'] for s in todo if s['needs_root']]
    nxt = ('as root: ' if root_needed else '') + f'review the plan above, then run {command_line(opts, "apply", yes=True)} (the cluster must be restarted afterwards: flink-velox-cluster-switch)'
    return base_result('plan', p, steps, [], True, '', nxt), 0


def apply_all(opts, yes):
    p = prereqs(opts['repos_dir'], opts['flink_home'], opts['only'])
    if p['problems']:
        steps = blocked_steps(p)
        msg = 'the prereqs step is blocked: ' + steps[0]['detail']
        return base_result('apply', p, steps, [], False, 'refused: ' + msg, msg), 2
    steps = build_steps(p, opts['only'], opts['jobs'])
    refusals = []
    if not yes:
        refusals.append('apply needs --yes (show the user the plan first; --yes is the record of their consent)')
    root_steps = [s['name'] for s in steps if s['status'] == 'todo' and s['needs_root']]
    if root_steps and euid() != 0:
        refusals.append('steps ' + ', '.join(root_steps) + ' need root (lib not writable): '
                        + command_line(opts, 'apply', yes=True, sudo=True))
    if refusals:
        return base_result('apply', p, steps, [], False, 'refused: ' + '; '.join(refusals), '; '.join(refusals)), 2
    log = []
    appliers = {'backup': apply_backup, 'velox4j': apply_velox4j, 'gluten': apply_gluten,
                'runtime-deps': apply_runtime_deps, 'pin-jdk': apply_pin_jdk}
    for step in steps:
        if step['status'] == 'skipped':
            log.append({'step': step['name'], 'action': 'skipped', 'detail': step['detail']})
            continue
        try:
            ok = appliers[step['name']](step, p, log)
        except Exception as exc:  # a command that cannot run or times out is this step's failure, not a crash
            step.update(status='failed', detail=f'{step["detail"]}; {exc_text(exc)}')
            log.append({'step': step['name'], 'action': 'failed', 'detail': exc_text(exc)})
            ok = False
        if not ok:
            break
    failed = [s for s in steps if s['status'] == 'failed']
    done = [s for s in steps if s['status'] == 'done']
    apply_cmd = command_line(opts, 'apply', yes=True)
    if failed and failed[0]['name'] == 'runtime-deps':
        nxt = (f'do NOT restart the cluster: lib holds the GlutenFlink jars without all their runtime dependencies ({failed[0]["detail"]}); '
               f'populate ~/.m2 (rerun the builds) or fix the table in references/build-table.md, then rerun plan and {apply_cmd}')
    elif failed:
        nxt = (f'fix the {failed[0]["name"]} step ({failed[0]["detail"]}); the replaced set is kept under {p["lib"]}.bak/, '
               f'then rerun plan and {apply_cmd}')
    else:
        nxt = f'{len(done)} step(s) done; ' + restart_note(opts)
    return base_result('apply', p, steps, log, not failed, f'{failed[0]["name"]} failed' if failed else '', nxt), 1 if failed else 0


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
    p = prereqs(opts['repos_dir'], opts['flink_home'], opts['only'])
    flink_refusal = next((pr for pr in p['problems'] if pr.startswith('--flink-home')), None)
    if flink_refusal:
        # an explicit --flink-home is the only candidate — refused like apply, never handed to
        # the sibling check as an empty FLINK_HOME: the sibling would look at PATH, /opt/flink,
        # /usr/local/flink and could report another installation VERIFIED
        return base_result('verify', p, [], [], False, 'refused: ' + flink_refusal,
                           'point --flink-home at the Flink installation to verify (bin/flink inside), then rerun'), 2
    skill, script = 'flink-velox-lib-state-check', 'lib_state_check.py'
    path = checker_path(skill, script)
    checks = []
    if path is None:
        checks.append({'name': 'lib-state', 'skill': skill, 'status': 'missing',
                       'detail': f'{skill} is not installed next to this skill'})
    else:
        env = dict(os.environ, FLINK_HOME=p['flink_home'] or '', ENV_CHECK_SYSROOT=SYSROOT)
        try:
            proc = run([sys.executable, path, '--json', '--expect', 'gfv'], timeout=600, env=env)
        except Exception as exc:  # the checker did not start or did not finish
            checks.append({'name': 'lib-state', 'skill': skill, 'status': 'fail', 'detail': f'{skill} did not run: {exc_text(exc)}'})
            proc = None
        if proc is not None:
            try:
                payload = json.loads(proc.stdout)
                r = payload[next(k for k in payload if k.endswith('_result'))]
                checks.append({'name': 'lib-state', 'skill': skill, 'status': 'ok' if r.get('success') else 'fail',
                               'detail': f'build={r.get("lib_state", {}).get("build")}; {len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                               'missing': r.get('missing') or []})
            except (ValueError, StopIteration, TypeError):
                checks.append({'name': 'lib-state', 'skill': skill, 'status': 'fail',
                               'detail': f'{skill} produced no result (exit {proc.returncode})'})
    failing = [c for c in checks if c['status'] == 'fail']
    missing = [c for c in checks if c['status'] == 'missing']
    error = ''
    if failing:
        error += f'{len(failing)} check(s) failing'
    if missing:
        error += '; ' if error else ''
        error += f'{len(missing)} check(s) not run (sibling skills not installed): ' + ', '.join(c['skill'] for c in missing)
    next_step = ''
    if missing:
        next_step = f'install the missing check skill(s) ({", ".join(c["skill"] for c in missing)})'
        if failing:
            next_step += ' and fix the failing check(s) above'
    elif failing:
        next_step = 'fix the failing check(s) above'
    if next_step:
        next_step += f', then rerun {command_line(opts, "verify")}'
    return base_result('verify', p, [], [], not error, error, next_step, checks), 0 if not error else 1


def aborted(exc, command):
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'repos': {}, 'jdk': None, 'flink_home': None,
                  'lib': None, 'steps': [], 'changes': [], 'root_steps': [],
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
        print(f'stack_build.py: {problem}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'stack_build.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    lines = [f"stack-build {r['command']}  (velox4j {r['repos'].get('velox4j')}, gluten {r['repos'].get('gluten')}, JDK {r['jdk']}, lib {r['lib']})"]
    if r['command'] == 'verify':
        for c in r.get('checks', []):
            lines.append(f"  {c['status'].upper():<9}{c['name']}: {c['detail']}")
        lines.append('')
        if r['success']:
            lines.append('VERIFIED: lib holds the complete GlutenFlink build')
        elif r['error_message'].startswith('refused'):
            lines.append('REFUSED: ' + r['error_message'][len('refused: '):])
        else:
            lines.append(f"NOT VERIFIED: {r['error_message']}")
        lines.append(f"next: {r['next_step']}")
        return '\n'.join(line.rstrip() for line in lines)
    if r['error_message'].startswith('tool aborted'):
        return f"TOOL ERROR: {r['error_message']}\nnext: {r['next_step']}"
    for step in r['steps']:
        mark = {'todo': 'TODO    ', 'skipped': 'SKIP    ', 'done': 'DONE    ', 'failed': 'FAILED  ', 'blocked': 'BLOCKED '}[step['status']]
        root = '  # needs root' if step.get('needs_root') and step['status'] in ('todo', 'done') else ''
        lines.append(f"  {step['name']:<14}{mark}{step['detail']}{root}")
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
        lines.append('APPLIED: GlutenFlink build deployed into lib' if r['success'] else f"NOT APPLIED: {r['error_message']}; later steps not run")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build velox4j and gluten-flink and deploy the GlutenFlink build into the Flink lib/ '
                                                 'for the Flink + GlutenFlink stack. plan by default; apply needs --yes.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--repos-dir', help='directory for velox/ velox4j/ gluten/ (default: $GFV_REPOS_DIR, then ~/repos)')
    parser.add_argument('--flink-home', help='the Flink installation to deploy into — the only candidate when given '
                                             '(default: $FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink)')
    parser.add_argument('--only', choices=('all', 'velox4j', 'gluten'), help='build one component only (default: all)')
    parser.add_argument('--jobs', type=int, help='velox4j native build parallelism (default: min(8, cores))')
    parser.add_argument('--yes', action='store_true', help='apply: the record that the user saw the plan and consented')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing regular file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    explicit = {flag: getattr(args, flag) for flag in ('repos_dir', 'flink_home', 'only', 'jobs') if getattr(args, flag) is not None}
    opts = {'repos_dir': locate_repos_dir(args.repos_dir), 'flink_home': args.flink_home,
            'only': args.only or 'all', 'jobs': args.jobs, 'explicit': explicit}
    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs: a bad --out must not change the machine
        if problem:
            print(f'stack_build.py: {problem}', file=sys.stderr)
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
            result, code = verify(opts)
    except Exception as exc:
        result, code = aborted(exc, args.command), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
