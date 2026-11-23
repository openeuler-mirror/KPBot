#!/usr/bin/env python3
"""stack_build.py — build velox4j and gluten-flink from the GFV source checkouts and
deploy the GFV build into the Flink lib/ directory.

Five steps (the build steps always rebuild — sources may have changed):
  backup        every gluten*/velox4j* jar and runtime dependency already in lib/
                is moved to <lib>.bak/<timestamp>/ (copied, then removed from lib)
                so lib never holds two versions of one artifact after the deploy
  velox4j       mvn clean install -DskipTests in the velox4j checkout, built against
                the workspace velox checkout (VELOX4J_VELOX_SOURCE_DIR); the
                velox4j-*.jar from target/ goes into lib/
  gluten        the nexmark-flink jar (when present in lib/) is installed into the
                local Maven repository first (gluten-flink depends on it), then
                mvn clean install -DskipTests -pl planner,loader,runtime in
                gluten-flink; planner / runtime / loader jars go into lib/
  runtime-deps  the 14 third-party jars the GFV jars need at runtime (guava, arrow,
                jackson, flatbuffers, commons-io) copied from ~/.m2/repository into
                lib/; missing ones are reported, not fatal
  pin-jdk       conf/flink-conf.yaml gets env.java.home: <build JDK> only when the
                key is absent (gluten jars carry Java 17 bytecode); a different
                existing value is left untouched and warned about

Jar names are matched by glob, never hard-coded versions. Three subcommands, the
same skeleton as every skill of the install family: plan (default) prints the
exact commands and touches nothing; apply --yes executes (stops at the first
failing step, records every change); verify runs the flink-velox-env
lib-state-check with --expect gfv.

Usage:
  stack_build.py [plan]    [--repos-dir DIR] [--flink-home DIR] [--only velox4j|gluten|all] [--jobs N] [--json] [--out FILE]
  stack_build.py apply --yes [the same options]
  stack_build.py verify

<repos-dir>: --repos-dir, else $GFV_REPOS_DIR, else ~/repos (a checkout can be
pinned with $VELOX_HOME / $VELOX4J_HOME / $GLUTEN_HOME). Flink: $FLINK_HOME, a
flink on PATH, /opt/flink, /usr/local/flink. JDK 17: $JAVA_HOME, the java on PATH,
/usr/lib/jvm/java-17-*. Root is required when lib/ is not writable by the current user.

Exit codes: plan 0 / 2 usage; apply 0 every step done, 1 a step failed (the report
says what was done — lib/ keeps the previous set under lib.bak/), 2 refused (no
--yes, not root for root steps, a blocked prerequisite); verify 0 / 1 / 2 as the
check says.

Test seams, used by the tier-A case only: ENV_PREPARE_SYSROOT, ENV_PREPARE_HOME,
ENV_PREPARE_EUID, ENV_PREPARE_PREFIX_WRITABLE=0, ENV_PREPARE_ENV_FAMILY_DIR.
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
import zipfile

KEY = 'flink_velox_stack_build_result'

SYSROOT = os.environ.get('ENV_PREPARE_SYSROOT', '')
HOME = os.environ.get('ENV_PREPARE_HOME') or os.path.expanduser('~')

REPOS = (('velox', 'VELOX_HOME', ('CMakeLists.txt', 'scripts')),
         ('velox4j', 'VELOX4J_HOME', ('pom.xml',)),
         ('gluten', 'GLUTEN_HOME', ('gluten-flink/pom.xml',)))
# group:artifact:version the GFV jars need at runtime beyond Flink's own lib/
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


# --- prerequisites ----------------------------------------------------------
def locate_repos_dir(explicit):
    return explicit or os.environ.get('GFV_REPOS_DIR') or os.path.join(HOME, 'repos')


def find_checkouts(repos_dir):
    found = {}
    for name, env, markers in REPOS:
        path = os.environ.get(env) or os.path.join(repos_dir, name)
        absent = [m for m in markers if not os.path.exists(os.path.join(path, *m.split('/')))]
        found[name] = {'path': os.path.realpath(path) if not absent else path, 'ok': not absent,
                       'missing': absent, 'pinned_via': env if os.environ.get(env) else None}
    return found


def find_jdk17():
    jh = os.environ.get('JAVA_HOME') or ''
    if os.access(os.path.join(jh, 'bin', 'javac'), os.X_OK) and jdk_major(jh) == 17:
        return jh, 'JAVA_HOME'
    java = shutil.which('java')
    if java:
        home = os.path.dirname(os.path.dirname(os.path.realpath(java)))
        if os.access(os.path.join(home, 'bin', 'javac'), os.X_OK) and jdk_major(home) == 17:
            return home, 'PATH'
    for cand in sorted(glob.glob(sys_path('/usr/lib/jvm/java-17-*'))):
        if os.access(os.path.join(cand, 'bin', 'javac'), os.X_OK):
            return cand, '/usr/lib/jvm'
    return None, None


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


def find_flink_home():
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


def prereqs(repos_dir):
    p = {'checkouts': find_checkouts(repos_dir), 'jdk': None, 'jdk_via': None, 'mvn': bool(shutil.which('mvn')),
         'flink_home': None, 'flink_via': None, 'lib': None, 'problems': []}
    p['jdk'], p['jdk_via'] = find_jdk17()
    p['flink_home'], p['flink_via'] = find_flink_home()
    for name, c in p['checkouts'].items():
        if not c['ok']:
            p['problems'].append(f"no usable {name} checkout at {c['path']} (missing {', '.join(c['missing'])}); "
                                 f"flink-velox-build-env-prepare clones it, or set {REPOS[[r[0] for r in REPOS].index(name)][1]}")
    if not p['jdk']:
        p['problems'].append('no JDK 17 (JAVA_HOME, the java on PATH, /usr/lib/jvm/java-17-*); flink-velox-build-env-prepare installs it')
    if not p['mvn']:
        p['problems'].append('mvn is not on PATH; flink-velox-build-env-prepare installs maven')
    if not p['flink_home']:
        p['problems'].append('no Flink installation (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink); '
                             'flink-velox-runtime-install installs it')
    else:
        p['lib'] = os.path.join(p['flink_home'], 'lib')
    return p


# --- steps ------------------------------------------------------------------
def lib_jars(lib, pattern):
    return sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, pattern)))


def dep_spec(dep):
    g, a, v = dep.split(':')
    return g, a, v, os.path.join(HOME, '.m2', 'repository', g.replace('.', '/'), a, v, f'{a}-{v}.jar')


def build_steps(p, only, jobs):
    lib = p['lib']
    steps = []
    existing = lib_jars(lib, '*.jar') if lib and os.path.isdir(lib) else []
    old_gfv = [j for j in existing if GFV_JAR_RE.match(j)]
    old_deps = []
    for dep in RUNTIME_DEPS:
        g, a, v, _ = dep_spec(dep)
        old_deps += [j for j in existing if j.startswith(a + '-')]
    backup_list = sorted(set(old_gfv + old_deps))
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    steps.append({'name': 'backup', 'needs_root': not writable(lib), 'actions': [],
                  'status': 'todo' if backup_list else 'skipped',
                  'detail': f'{len(backup_list)} jar(s) in lib would be overwritten: {", ".join(backup_list)}' if backup_list
                  else 'lib holds no GFV jar or runtime dependency yet',
                  'plan': [f'mkdir -p {lib}.bak/{stamp}']
                  + [f'cp {lib}/{" ".join(backup_list)} {lib}.bak/{stamp}/   # then removed from lib']
                  if backup_list else ['# nothing to back up'],
                  'backup_dir': os.path.join(lib + '.bak', stamp), 'backup_list': backup_list, 'lib': lib})
    cores = os.cpu_count() or 1
    jobs4j = min(8, jobs or cores)
    velox4j = p['checkouts']['velox4j']['path']
    if only in ('all', 'velox4j'):
        steps.append({'name': 'velox4j', 'needs_root': not writable(lib), 'actions': [], 'status': 'todo',
                      'detail': f'mvn clean install -DskipTests in {velox4j} (against {p["checkouts"]["velox"]["path"]}, JDK {p["jdk"]}); velox4j-*.jar -> lib',
                      'plan': [f'# JAVA_HOME={p["jdk"]} VELOX4J_VELOX_SOURCE_DIR={p["checkouts"]["velox"]["path"]} VELOX4J_BUILD_JOBS={jobs4j}',
                               f'mvn clean install -DskipTests {" ".join(MVN_COMMON)}   (in {velox4j})',
                               f'cp {velox4j}/target/velox4j-*.jar {lib}/'],
                      'repo': velox4j, 'jobs': jobs4j})
    if only in ('all', 'gluten'):
        gluten = os.path.join(p['checkouts']['gluten']['path'], 'gluten-flink')
        nexmark = lib_jars(lib, '*nexmark*.jar') if lib and os.path.isdir(lib) else []
        plan = []
        if nexmark:
            plan += [f'# install nexmark-flink into ~/.m2 first (gluten-flink depends on it):',
                     f'#   unzip {lib}/{nexmark[0]} META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml -> <tmp>',
                     f'#   mvn install:install-file -Dfile={lib}/{nexmark[0]} -DpomFile=<tmp>/pom.xml']
        else:
            plan.append('# no nexmark jar in lib: the nexmark install-file step is skipped (gluten-flink may fail to resolve it)')
        plan += [f'# JAVA_HOME={p["jdk"]}',
                 f'mvn clean install -DskipTests {" ".join(MVN_COMMON)} -pl planner,loader,runtime   (in {gluten})',
                 f'cp {gluten}/planner/target/gluten-flink-planner-*.jar {gluten}/runtime/target/gluten-flink-runtime-*.jar '
                 f'{gluten}/loader/target/gluten-flink-loader-*.jar {lib}/']
        steps.append({'name': 'gluten', 'needs_root': not writable(lib), 'actions': [], 'status': 'todo',
                      'detail': f'mvn clean install -DskipTests -pl planner,loader,runtime in {gluten} (JDK {p["jdk"]}); 3 jars -> lib'
                      + (f'; nexmark {nexmark[0]} installed into ~/.m2 first' if nexmark else '; no nexmark jar in lib'),
                      'plan': plan, 'repo': gluten, 'nexmark_jar': os.path.join(lib, nexmark[0]) if nexmark else None})
    if only == 'all':
        found, missing = [], []
        for dep in RUNTIME_DEPS:
            g, a, v, path = dep_spec(dep)
            (found if os.path.isfile(path) else missing).append(dep)
        plan = [f'cp {dep_spec(d)[3]} {lib}/' for d in found] if found else []
        if missing:
            plan.append(f'# not in ~/.m2 (the builds above populate it; flink may fail to load the GFV jars): {", ".join(missing)}')
        steps.append({'name': 'runtime-deps', 'needs_root': not writable(lib), 'actions': [],
                      'status': 'todo' if found else 'skipped',
                      'detail': f'{len(found)} of {len(RUNTIME_DEPS)} runtime dependencies from ~/.m2 -> lib'
                      + (f'; {len(missing)} not in ~/.m2 (warned, not fatal)' if missing else ''),
                      'plan': plan or ['# nothing to copy'], 'found': found, 'missing': missing})
        conf = os.path.join(p['flink_home'], 'conf', 'flink-conf.yaml')
        cur = None
        try:
            with open(conf, encoding='utf-8', errors='replace') as fh:
                m = re.search(r'^env\.java\.home:\s*(\S+)', fh.read(), re.M)
                cur = m.group(1) if m else None
        except OSError:
            cur = None
        if cur is None:
            steps.append({'name': 'pin-jdk', 'needs_root': not writable(os.path.dirname(conf)), 'actions': [], 'status': 'todo',
                          'detail': f'conf has no env.java.home: append env.java.home: {p["jdk"]} (gluten jars carry Java 17 bytecode)',
                          'plan': [f'echo "env.java.home: {p["jdk"]}" >> {conf}'], 'conf': conf, 'action': 'append'})
        elif os.path.realpath(cur) != os.path.realpath(p['jdk']):
            steps.append({'name': 'pin-jdk', 'needs_root': not writable(os.path.dirname(conf)), 'actions': [], 'status': 'skipped',
                          'detail': f'conf pins env.java.home to {cur} but this build uses {p["jdk"]}; left untouched (WARNING)',
                          'plan': ['# left untouched — the configured value belongs to the user'], 'conf': conf, 'action': 'warn'})
        else:
            steps.append({'name': 'pin-jdk', 'needs_root': False, 'actions': [], 'status': 'skipped',
                          'detail': f'conf already pins env.java.home to {cur}', 'plan': ['# nothing to do'], 'conf': conf, 'action': 'none'})
    return steps


# --- apply ------------------------------------------------------------------
def apply_backup(step, p, log):
    """The previous set moves to lib.bak/<stamp>/ (copied, then removed from lib)
    so lib never holds two versions of one artifact after the deploy."""
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
    log.append({'step': 'backup', 'action': 'backed up',
                'detail': f'{len(step["backup_list"])} jar(s) moved out of lib -> {step["backup_dir"]}'})
    step['status'] = 'done'
    return True


def mvn_build(repo, args, jdk, env_extra, timeout=14400):
    env = dict(os.environ, JAVA_HOME=jdk, PATH=os.path.join(jdk, 'bin') + os.pathsep + os.environ.get('PATH', ''), **env_extra)
    return run(['mvn', *args, *MVN_COMMON], cwd=repo, env=env, timeout=timeout)


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
    log.append({'step': 'velox4j', 'action': 'built+deployed', 'detail': f'{os.path.basename(jars[0])} -> {p["lib"]}'})
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
        import tempfile
        tmp = tempfile.mkdtemp(prefix='stack-build-')
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
            shutil.rmtree(tmp, ignore_errors=True)
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
    log.append({'step': 'gluten', 'action': 'built+deployed', 'detail': ', '.join(deployed) + f' -> {p["lib"]}'})
    step.update(status='done', detail=', '.join(deployed) + ' built and deployed')
    return True


def apply_runtime_deps(step, p, log):
    if step['status'] == 'skipped':
        log.append({'step': 'runtime-deps', 'action': 'skipped', 'detail': step['detail']})
        return True
    copied = []
    for dep in step['found']:
        g, a, v, path = dep_spec(dep)
        shutil.copy2(path, p['lib'])
        copied.append(os.path.basename(path))
    log.append({'step': 'runtime-deps', 'action': 'deployed', 'detail': f'{len(copied)} jar(s) -> {p["lib"]}'})
    if step['missing']:
        log.append({'step': 'runtime-deps', 'action': 'warned', 'detail': 'not in ~/.m2: ' + ', '.join(step['missing'])})
    step.update(status='done', detail=f'{len(copied)} runtime dependencies deployed; {len(step["missing"])} missing in ~/.m2')
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


def blocked_steps(p):
    """One blocked step standing for every missing prerequisite."""
    return [{'name': 'prereqs', 'needs_root': False, 'actions': [], 'status': 'blocked',
             'detail': '; '.join(p['problems']), 'fix': 'fix the above (flink-velox-build-env-prepare / flink-velox-runtime-install), then rerun plan',
             'plan': []}]


def build_plan(repos_dir, only, jobs):
    p = prereqs(repos_dir)
    if p['problems']:
        steps = blocked_steps(p)
        return base_result('plan', p, steps, [], True, '', steps[0]['fix']), 0
    steps = build_steps(p, only, jobs)
    todo = [s for s in steps if s['status'] == 'todo']
    root_needed = [s['name'] for s in todo if s['needs_root']]
    nxt = ('as root: ' if root_needed else '') + 'review the plan above, then run stack_build.py apply --yes (the cluster must be restarted afterwards: flink-velox-cluster-switch)'
    return base_result('plan', p, steps, [], True, '', nxt), 0


def apply_all(repos_dir, only, jobs, yes):
    p = prereqs(repos_dir)
    if p['problems']:
        steps = blocked_steps(p)
        msg = 'the prereqs step is blocked: ' + steps[0]['detail']
        return base_result('apply', p, steps, [], False, 'refused: ' + msg, msg), 2
    steps = build_steps(p, only, jobs)
    refusals = []
    if not yes:
        refusals.append('apply needs --yes (show the user the plan first; --yes is the record of their consent)')
    root_steps = [s['name'] for s in steps if s['status'] == 'todo' and s['needs_root']]
    if root_steps and euid() != 0:
        refusals.append('steps ' + ', '.join(root_steps) + ' need root (lib not writable): run "sudo python3 stack_build.py apply --yes"')
    if refusals:
        return base_result('apply', p, steps, [], False, 'refused: ' + '; '.join(refusals), '; '.join(refusals)), 2
    log = []
    appliers = {'backup': apply_backup, 'velox4j': apply_velox4j, 'gluten': apply_gluten,
                'runtime-deps': apply_runtime_deps, 'pin-jdk': apply_pin_jdk}
    for step in steps:
        if step['status'] == 'skipped':
            log.append({'step': step['name'], 'action': 'skipped', 'detail': step['detail']})
            continue
        if not appliers[step['name']](step, p, log):
            break
    failed = [s for s in steps if s['status'] == 'failed']
    done = [s for s in steps if s['status'] == 'done']
    nxt = (f'fix the {failed[0]["name"]} step ({failed[0]["detail"]}); the previous lib set is kept under {p["lib"]}.bak/, '
           'then rerun plan and apply' if failed else
           f'{len(done)} step(s) done; restart the cluster so it loads the new build (flink-velox-cluster-switch, needs consent), then run stack_build.py verify')
    return base_result('apply', p, steps, log, not failed, f'{failed[0]["name"]} failed' if failed else '', nxt), 1 if failed else 0


def verify(p_only):
    family = os.environ.get('ENV_PREPARE_ENV_FAMILY_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                             '..', '..', '..', 'flink-velox-env')
    script = os.path.realpath(family) + '/flink-velox-lib-state-check/scripts/lib_state_check.py'
    checks = []
    if not os.path.isfile(script):
        checks.append({'name': 'lib-state', 'status': 'skipped', 'detail': 'flink-velox-lib-state-check is not installed next to this skill'})
    else:
        proc = run([sys.executable, script, '--json', '--expect', 'gfv'], timeout=600)
        try:
            payload = json.loads(proc.stdout)
            r = payload[next(k for k in payload if k.endswith('_result'))]
            checks.append({'name': 'lib-state', 'status': 'ok' if r.get('success') else 'fail',
                           'detail': f'build={r.get("lib_state", {}).get("build")}; {len(r.get("missing") or [])} item(s) missing; next: {r.get("next_step")}',
                           'missing': r.get('missing') or []})
        except (ValueError, StopIteration, TypeError):
            checks.append({'name': 'lib-state', 'status': 'fail', 'detail': f'lib-state-check produced no result (exit {proc.returncode})'})
    failing = [c for c in checks if c['status'] == 'fail']
    p = prereqs(locate_repos_dir(None))
    return base_result('verify', p, [], [], not failing, '' if not failing else f'{len(failing)} check(s) failing',
                       '' if not failing else 'rerun stack_build.py plan', checks), 0 if not failing else 1


def aborted(exc, command):
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'repos': {}, 'jdk': None, 'flink_home': None,
                  'lib': None, 'steps': [], 'changes': [], 'root_steps': [],
                  'next_step': 'fix the tool error first (see error_message), then rerun',
                  'error_message': f'tool aborted: {exc!r}'}}


def write_out(path, result, overwrite):
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('stack_build.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'stack_build.py: --out already exists, pass --overwrite to replace it: {path}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:
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
            lines.append(f"  {c['status'].upper():<7}{c['name']}: {c['detail']}")
        lines.append('')
        lines.append('VERIFIED: lib holds the complete GFV build' if r['success'] else f"NOT VERIFIED: {r['error_message']}")
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
        lines.append('APPLIED: GFV build deployed into lib' if r['success'] else f"NOT APPLIED: {r['error_message']}; later steps not run")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build velox4j and gluten-flink and deploy the GFV build into the Flink lib/ '
                                                 'for the Flink + GlutenFlink stack. plan by default; apply needs --yes.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--repos-dir', help='directory for velox/ velox4j/ gluten/ (default: $GFV_REPOS_DIR, then ~/repos)')
    parser.add_argument('--only', default='all', choices=('all', 'velox4j', 'gluten'), help='build one component only')
    parser.add_argument('--jobs', type=int, help='velox4j native build parallelism (default: min(8, cores))')
    parser.add_argument('--yes', action='store_true', help='apply: the record that the user saw the plan and consented')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    repos_dir = locate_repos_dir(args.repos_dir)
    try:
        if args.command == 'plan':
            result, code = build_plan(repos_dir, args.only, args.jobs)
        elif args.command == 'apply':
            result, code = apply_all(repos_dir, args.only, args.jobs, args.yes)
        else:
            result, code = verify(args.only)
    except Exception as exc:
        result, code = aborted(exc, args.command), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
