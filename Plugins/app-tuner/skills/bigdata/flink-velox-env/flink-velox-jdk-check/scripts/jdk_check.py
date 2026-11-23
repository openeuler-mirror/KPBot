#!/usr/bin/env python3
"""jdk_check.py — read-only check of the Java the Flink + GlutenFlink (GFV) stack needs.

Three questions, one pass: is there a JDK 17 on the machine (the build needs one);
is JAVA_HOME what the build scripts should take (they read it first); which Java do
the Flink daemons run on, and which would a restart pick (conf env.java.home, then
JAVA_HOME, then java on PATH — Flink's own resolution order). goal=build (default)
needs Java 17 everywhere because the GFV jars carry Java 17 bytecode; goal=native
accepts Java 8, 11 or 17 (what Flink 1.19 itself runs on).
The script classifies only: it installs nothing, starts or stops nothing, and writes
nowhere except the optional --out file.

Usage:
  jdk_check.py [--goal build|native] [--json] [--out FILE [--overwrite]]

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

The Flink installation is located like the other checks: FLINK_HOME, a flink on
PATH, /opt/flink, /usr/local/flink. Without one, conf env.java.home cannot be read
and running daemons cannot be attributed — reported as a warning, not a failure.

Exit codes: 0 every item ok (warnings allowed); 1 at least one fail; 2 check error
(JSON in the full contract shape, error_message "check aborted: ..."), a refused
--out, or an invalid command line (usage on stderr, no JSON).

Test seams, used by the tier-A case only: ENV_CHECK_SYSROOT prefixes every absolute
system path (/usr/lib/jvm, /opt/flink, ...); ENV_CHECK_PROC_ROOT replaces /proc for
the daemon scan.
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

KEY = 'flink_velox_jdk_check_result'

SYSROOT = os.environ.get('ENV_CHECK_SYSROOT', '')
PROC_ROOT = os.environ.get('ENV_CHECK_PROC_ROOT', '/proc')

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')

JM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.entrypoint\.\w+Entrypoint$')
TM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.taskexecutor\.TaskManagerRunner$')


def sys_path(rel):
    """Absolute system path under the (optional) sysroot prefix."""
    return SYSROOT + rel


def is_exec(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def run_argv(argv, timeout=15):
    """Run a fixed argv (never through a shell); first output line or ''."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ''
    lines = (proc.stdout or '').splitlines() + (proc.stderr or '').splitlines()
    return lines[0].strip() if lines else ''


def java_major(version):
    parts = re.split(r'[._\-]', version or '')
    if not parts or not parts[0].isdigit():
        return None
    if parts[0] == '1':
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return int(parts[0])


def jdk_version(home):
    """(version string, major) from <home>/release, else `java -version`."""
    try:
        with open(os.path.join(home, 'release'), encoding='utf-8', errors='replace') as fh:
            m = re.search(r'^JAVA_VERSION="([^"]+)"', fh.read(), re.M)
        if m:
            return m.group(1), java_major(m.group(1))
    except OSError:
        pass
    line = run_argv([os.path.join(home, 'bin', 'java'), '-version'], timeout=20)
    m = re.search(r'version "([^"]+)"', line)
    return (m.group(1), java_major(m.group(1))) if m else (None, None)


def is_jdk(home):
    return is_exec(os.path.join(home or '', 'bin', 'javac'))


def make_item(name, status, detail, fix=None, **extra):
    item = {'name': name, 'status': status, 'detail': detail}
    if fix:
        item['fix'] = fix
    item.update(extra)
    return item


def worst(statuses):
    statuses = list(statuses)
    return max(statuses, key=STATUS_ORDER.index) if statuses else 'ok'


# --- the JDKs on the machine ------------------------------------------------
def find_jdk17():
    """(home, version, source): JAVA_HOME, the java on PATH, then /usr/lib/jvm."""
    jh = os.environ.get('JAVA_HOME') or ''
    if is_jdk(jh):
        version, major = jdk_version(jh)
        if major == 17:
            return jh, version, 'JAVA_HOME'
    java = shutil.which('java')
    if java:
        home = os.path.dirname(os.path.dirname(os.path.realpath(java)))
        if is_jdk(home):
            version, major = jdk_version(home)
            if major == 17:
                return home, version, 'PATH'
    for cand in sorted(glob.glob(sys_path('/usr/lib/jvm/java-17-*'))):
        if is_jdk(cand):
            return cand, jdk_version(cand)[0], '/usr/lib/jvm'
    return None, None, None


def find_jdk8():
    for pattern in ('java-1.8.0-openjdk-*', 'java-8-openjdk-*'):
        for cand in sorted(glob.glob(sys_path('/usr/lib/jvm/' + pattern))):
            if is_jdk(cand):
                return cand
    return None


def scan_jdks():
    """JDK 17 for the build, and JAVA_HOME as the build scripts read it: they take
    JAVA_HOME first and fall back to a JDK 17 found on the machine, so a JAVA_HOME
    that is not a JDK 17 breaks the build even when a JDK 17 exists."""
    home17, version17, source17 = find_jdk17()
    java_home = os.environ.get('JAVA_HOME') or None
    payload = {'jdk17': {'path': home17, 'version': version17, 'found_via': source17},
               'jdk8': find_jdk8(), 'java_home': java_home}
    items = []
    if home17:
        items.append(make_item('jdk17', 'ok', f'{version17 or "?"} at {home17} (via {source17})'))
    else:
        items.append(make_item(
            'jdk17', 'fail',
            'no JDK 17: JAVA_HOME is unset or not a JDK 17, no JDK 17 behind the java on PATH, '
            'nothing under /usr/lib/jvm/java-17-*',
            'install a JDK 17 (java-17-openjdk-devel on openEuler/CentOS)'))
    if not java_home:
        tail = f'; the build falls back to the JDK 17 found at {home17}' if home17 else ''
        items.append(make_item('JAVA_HOME', 'warn', 'not set' + tail))
    elif not is_jdk(java_home):
        items.append(make_item(
            'JAVA_HOME', 'fail', f'{java_home} has no bin/javac — the build scripts take JAVA_HOME '
            'first and would use it', 'point JAVA_HOME at a JDK 17'))
    else:
        version, major = jdk_version(java_home)
        if major == 17:
            items.append(make_item('JAVA_HOME', 'ok', f'{version or "?"} at {java_home}'))
        else:
            fix = (f'point JAVA_HOME at the JDK 17 at {home17}' if home17
                   else 'install a JDK 17 and point JAVA_HOME at it')
            items.append(make_item(
                'JAVA_HOME', 'fail',
                f'JAVA_HOME is JDK {major or "?"} ({java_home}); the build scripts take JAVA_HOME '
                'first while gluten-flink is compiled as Java 17', fix))
    return payload, items


# --- the Flink installation and its conf ------------------------------------
def find_flink_home():
    """(path as given, source) — FLINK_HOME, a flink on PATH, /opt/flink,
    /usr/local/flink; the first that has bin/flink."""
    cands = []
    if os.environ.get('FLINK_HOME'):
        cands.append((os.environ['FLINK_HOME'], 'FLINK_HOME'))
    flink = shutil.which('flink')
    if flink:
        # a flink on PATH is <home>/bin/flink, often reached through a symlink
        cands.append((os.path.dirname(os.path.dirname(os.path.realpath(flink))), 'PATH'))
    cands += [(sys_path('/opt/flink'), 'default'), (sys_path('/usr/local/flink'), 'default')]
    for cand, source in cands:
        if is_exec(os.path.join(cand, 'bin', 'flink')):
            return cand, source
    return (None, None)


def load_flink_conf(flink_home):
    """Flatten conf/config.yaml (nested maps) and conf/flink-conf.yaml (flat dotted
    keys) into one dict, without a YAML dependency. Scalar leaves only."""
    conf = {}
    for name in ('config.yaml', 'flink-conf.yaml'):
        try:
            fh = open(os.path.join(flink_home, 'conf', name), encoding='utf-8', errors='replace')
        except OSError:
            continue
        stack = []  # [(indent, dotted prefix)]
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


def running_daemon_java(flink_home):
    """(java home, pid, role) of a Flink daemon of THIS installation — its classpath
    names lib/flink-dist under this installation; None when none runs or no
    installation is known."""
    if not flink_home:
        return None
    real_lib = os.path.realpath(os.path.join(flink_home, 'lib'))
    try:
        pids = sorted(e for e in os.listdir(PROC_ROOT) if e.isdigit())
    except OSError:
        return None
    for pid in pids:
        argv = read_cmdline(pid)
        entry = next((t for t in argv if JM_ENTRY_RE.match(t) or TM_ENTRY_RE.match(t)), None)
        if not entry:
            continue
        dist = [p for p in classpath_of(argv) if os.path.basename(p).startswith('flink-dist')]
        if not any(os.path.dirname(os.path.realpath(p)) == real_lib for p in dist):
            continue
        java = os.path.dirname(os.path.dirname(os.path.realpath(argv[0]))) if argv else None
        return java, int(pid), ('jobmanager' if JM_ENTRY_RE.match(entry) else 'taskmanager')
    return None


def describe_java(home, source):
    entry = {'path': home, 'found_via': source}
    if home and is_exec(os.path.join(home, 'bin', 'java')):
        entry['version'], entry['major'] = jdk_version(home)
    else:
        entry['version'], entry['major'] = None, None
    return entry


def scan_daemon_java(goal, flink_home, conf):
    """The Java the Flink daemons use and the one a restart would pick (Flink's
    bash-java-utils.sh order: conf env.java.home, JAVA_HOME, java on PATH)."""
    if conf.get('env.java.home'):
        source, home = 'flink conf env.java.home', conf['env.java.home']
    elif os.environ.get('JAVA_HOME'):
        source, home = 'JAVA_HOME', os.environ['JAVA_HOME']
    else:
        java = shutil.which('java')
        source, home = 'PATH', (os.path.dirname(os.path.dirname(os.path.realpath(java)))
                                if java else None)
    startup = describe_java(home, source)
    found = running_daemon_java(flink_home)
    running = describe_java(found[0], f'running process (pid {found[1]}, {found[2]})') if found else None
    payload = {'cluster_java': startup, 'running_java': running}
    gfv_needed = goal == 'build'
    items = []
    if running and os.path.realpath(running['path'] or '') == os.path.realpath(startup['path'] or ''):
        # the daemons run on what a restart would pick: one verdict
        startup['found_via'] += '; the running daemons use it'
        entries = [('java', startup)]
    else:
        entries = ([('running', running)] if running else []) + [('startup', startup)]
    for name, entry in entries:
        path, version, major = entry['path'], entry['version'], entry['major']
        where = f' ({path}, via {entry["found_via"]})'
        if not path:
            items.append(make_item(
                name, 'fail',
                'no java for the Flink daemons: neither env.java.home, JAVA_HOME nor a java on '
                'PATH resolves to one', 'set env.java.home in the Flink conf (or JAVA_HOME)'))
        elif major is None:
            items.append(make_item(
                name, 'fail',
                f'{path} (via {entry["found_via"]}) has no bin/java or its version cannot be read',
                'set env.java.home in the Flink conf (or JAVA_HOME) to a JDK'))
        elif gfv_needed and major != 17:
            items.append(make_item(
                name, 'fail',
                f'Java {version}{where}: the gluten jars carry Java 17 bytecode and fail to load '
                'on it (UnsupportedClassVersionError)',
                'set env.java.home in the Flink conf to a JDK 17 and restart the cluster '
                '(a restart needs the user consent)'))
        else:
            items.append(make_item(name, 'ok', f'Java {version}{where}'))
    return payload, items


def next_step(jdk_payload, daemon_items):
    if not jdk_payload['jdk17']['path']:
        return 'install a JDK 17 (java-17-openjdk-devel on openEuler) and point JAVA_HOME at it, then rerun'
    home17 = jdk_payload['jdk17']['path']
    java_home = jdk_payload['java_home']
    if java_home and (not is_jdk(java_home) or jdk_version(java_home)[1] != 17):
        return f'point JAVA_HOME at the JDK 17 at {home17}, then rerun'
    for item in daemon_items:
        if item['status'] != 'fail':
            continue
        if 'Java 17 bytecode' in item['detail']:
            return 'set env.java.home in the Flink conf to a JDK 17 and restart the cluster ' \
                   '(a restart needs the user consent), then rerun'
        return 'set env.java.home in the Flink conf (or JAVA_HOME) to a JDK, then rerun'
    warnings = [i for i in daemon_items if i['status'] == 'warn']
    return 'ready: the Java for the Flink daemons is in order' + (' (warnings above)' if warnings else '')


def build_result(goal):
    jdks, jdk_items = scan_jdks()
    home, source = find_flink_home()
    items = list(jdk_items)
    if not home:
        items.append(make_item(
            'flink', 'warn',
            'Flink installation not found (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink): '
            'conf env.java.home not read, running daemons not attributed'))
        conf = {}
    else:
        conf = load_flink_conf(home)
    daemons, daemon_items = scan_daemon_java(goal, home, conf)
    items += daemon_items
    missing = [f'{i["name"]}: {i["detail"]}' for i in items if i['status'] == 'fail']
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'goal': goal,
        'status': worst(i['status'] for i in items),
        'jdk17': jdks['jdk17'],
        'jdk8': jdks['jdk8'],
        'java_home': jdks['java_home'],
        'flink_home': {'path': home, 'found_via': source},
        'cluster_java': daemons['cluster_java'],
        'running_java': daemons['running_java'],
        'items': items,
        'missing': missing,
        'next_step': next_step(jdks, daemon_items),
        'error_message': '' if success else f'{len(missing)} item(s) missing: ' + ', '.join(
            i['name'] for i in items if i['status'] == 'fail'),
    }
    return {KEY: result}, 0 if success else 1


def aborted(exc):
    """A check crash in the full contract shape: every documented key exists."""
    return {KEY: {
        'success': False,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'goal': None,
        'status': 'fail',
        'jdk17': {'path': None, 'version': None, 'found_via': None},
        'jdk8': None,
        'java_home': None,
        'flink_home': {'path': None, 'found_via': None},
        'cluster_java': {'path': None, 'version': None, 'major': None, 'found_via': None},
        'running_java': None,
        'items': [],
        'missing': [],
        'next_step': 'fix the check error first (see error_message), then rerun',
        'error_message': f'check aborted: {exc!r}',
    }}


def write_out(path, result, overwrite):
    """The --out archive stays inside the project (the working directory), is never a
    symlink, and is never truncated without --overwrite. Returns the exit code to use
    when the write is refused (0 when written)."""
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('jdk_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'jdk_check.py: --out already exists, pass --overwrite to replace it: {path}',
              file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:  # 'x' never follows a symlink
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'jdk_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('check aborted'):
        return f"CHECK ERROR: {r['error_message']}\nnext: {r['next_step']}"
    lines = []
    if r['success']:
        lines.append(f"jdk         OK       JDK {r['jdk17']['version'] or '?'} at {r['jdk17']['path']} "
                     f"(via {r['jdk17']['found_via']}); JAVA_HOME "
                     f"{'ok' if any(i['name'] == 'JAVA_HOME' and i['status'] == 'ok' for i in r['items']) else 'not set'}; "
                     + '; '.join(f"{i['name']}: {i['detail']}" for i in r['items']
                                 if i['name'] in ('java', 'running', 'startup')))
        for item in r['items']:  # a warning never blocks, but must be seen
            if item['status'] == 'warn':
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
    else:
        lines.append(f"jdk         FAIL     {len(r['missing'])} item(s) missing")
        for item in r['items']:
            if item['status'] in ('fail', 'warn'):
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
                if item.get('fix'):
                    lines.append(f"        fix: {item['fix']}")
    lines.append('')
    lines.append('READY: the Java for the Flink daemons is in order' if r['success']
                 else f"NOT READY for goal '{r['goal']}': {len(r['missing'])} item(s) missing")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the Java (JDK 17, JAVA_HOME, '
                                                 'the Flink daemons Java) for the Flink + GlutenFlink stack.')
    parser.add_argument('--goal', choices=('build', 'native'), default='build',
                        help='build: Java 17 everywhere (default); native: Java 8, 11 or 17 accepted')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project '
                                      'directory; an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    try:
        result, code = build_result(args.goal)
    except Exception as exc:  # a check bug must never be reported as "present"
        result, code = aborted(exc), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))  # a refused write is 2, the verdict still prints
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
