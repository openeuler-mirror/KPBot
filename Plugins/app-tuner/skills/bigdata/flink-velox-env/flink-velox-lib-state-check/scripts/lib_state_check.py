#!/usr/bin/env python3
"""lib_state_check.py — read-only check of what sits in the Flink lib/ directory.

Classifies the lib/ of the Flink installation (FLINK_HOME, a flink on PATH,
/opt/flink, /usr/local/flink) as one of three builds by its jars: native (no
gluten/velox jar), gfv (the complete GlutenFlink set: planner + runtime + loader, velox4j
and the runtime dependencies the build deploys), or partial (some of it — half a
stack fails at class-loading). Several versions of one artifact are reported.
--expect says which build the caller wants in lib (any by default; native for a
native-side run; gfv for the GlutenFlink build), and anything else is a failure with the
action that gets there. The script classifies only: it moves no jar, starts or
stops nothing, writes nowhere except the optional --out file.

Usage:
  lib_state_check.py [--expect any|native|gfv] [--json] [--out FILE [--overwrite]]

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

Exit codes: 0 lib holds the expected build (warnings allowed); 1 it does not, the
set is incomplete, or no installation was found; 2 check error (JSON in the full
contract shape, error_message "check aborted: ..."), a refused --out, or an
invalid command line (usage on stderr, no JSON).

Test seam, used by the tier-A case only: ENV_CHECK_SYSROOT prefixes every absolute
system path (/opt/flink, /usr/local/flink, ...).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import stat
import sys

KEY = 'flink_velox_lib_state_check_result'

SYSROOT = os.environ.get('ENV_CHECK_SYSROOT', '')

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')

# A jar named gluten* or velox* means the GlutenFlink build (the same criterion as the
# lib gate of flink-velox-e2e-verify).
GFV_JAR_RE = re.compile(r'gluten|velox', re.I)
GLUTEN_ARTIFACTS = ('gluten-flink-planner', 'gluten-flink-runtime', 'gluten-flink-loader')
# What the GlutenFlink build deploys into lib/ besides its own jars (artifactId only:
# versions move with the stack, the names do not).
RUNTIME_DEPS = ('guava', 'failureaccess', 'arrow-c-data', 'arrow-format', 'arrow-memory-core',
                'arrow-memory-unsafe', 'arrow-vector', 'commons-io', 'flatbuffers-java',
                'jackson-annotations', 'jackson-core', 'jackson-databind',
                'jackson-datatype-jdk8', 'jackson-datatype-jsr310')
HINT_FLINK = ('install the official binary distribution '
              'flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it')


def sys_path(rel):
    return SYSROOT + rel


def is_exec(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def make_item(name, status, detail, fix=None, **extra):
    item = {'name': name, 'status': status, 'detail': detail}
    if fix:
        item['fix'] = fix
    item.update(extra)
    return item


def worst(statuses):
    statuses = list(statuses)
    return max(statuses, key=STATUS_ORDER.index) if statuses else 'ok'


def find_flink_home():
    """(path as given, source) — FLINK_HOME, a flink on PATH, /opt/flink,
    /usr/local/flink; the first that has bin/flink."""
    cands = []
    if os.environ.get('FLINK_HOME'):
        cands.append((os.environ['FLINK_HOME'], 'FLINK_HOME'))
    flink = shutil.which('flink')
    if flink:
        cands.append((os.path.dirname(os.path.dirname(os.path.realpath(flink))), 'PATH'))
    cands += [(sys_path('/opt/flink'), 'default'), (sys_path('/usr/local/flink'), 'default')]
    for cand, source in cands:
        if is_exec(os.path.join(cand, 'bin', 'flink')):
            return cand, source
    return (None, None)


def classify(names):
    """native / gfv / partial from the jar names in lib/."""
    gluten = {a: [n for n in names if n.startswith(a + '-')] for a in GLUTEN_ARTIFACTS}
    velox4j = [n for n in names if n.startswith('velox4j-')]
    deps_missing = [a for a in RUNTIME_DEPS if not any(n.startswith(a + '-') for n in names)]
    duplicates = {a: gluten[a] for a in GLUTEN_ARTIFACTS if len(gluten[a]) > 1}
    if len(velox4j) > 1:
        duplicates['velox4j'] = velox4j
    # the runtime dependencies count too: which of two guava versions gets loaded is
    # decided by classpath order, exactly as for the gluten jars
    for a in RUNTIME_DEPS:
        found = [n for n in names if n.startswith(a + '-')]
        if len(found) > 1:
            duplicates[a] = found
    gfv_jars = [n for n in names if GFV_JAR_RE.search(n)]
    if not gfv_jars:
        build, absent = 'native', []
    elif all(gluten.values()) and velox4j and not deps_missing:
        build, absent = 'gfv', []
    else:
        build = 'partial'
        absent = [a for a in GLUTEN_ARTIFACTS if not gluten[a]] + deps_missing
        if not velox4j:
            absent.insert(0, 'velox4j')
    return {'build': build, 'gfv_jars': gfv_jars, 'gluten_jars': gluten, 'velox4j_jars': velox4j,
            'runtime_deps': {'expected': len(RUNTIME_DEPS), 'missing': deps_missing},
            'duplicates': duplicates, 'missing_parts': absent}


def scan(expect):
    home, source = find_flink_home()
    payload = {'build': None, 'lib': None, 'flink_home': home, 'found_via': source, 'gfv_jars': [],
               'gluten_jars': {}, 'velox4j_jars': [], 'runtime_deps': {'expected': len(RUNTIME_DEPS), 'missing': []},
               'duplicates': {}, 'missing_parts': []}
    if not home:
        return payload, [make_item(
            'home', 'fail',
            'Flink not found: FLINK_HOME is unset or has no bin/flink, no flink on PATH, '
            'nothing under /opt/flink or /usr/local/flink', HINT_FLINK)]
    lib = os.path.join(home, 'lib')
    if not os.path.isdir(lib):
        return payload, [make_item(
            'lib', 'fail', f'{home} has no lib/ directory — not a Flink distribution', HINT_FLINK)]
    names = sorted(n for n in os.listdir(lib) if n.endswith('.jar'))
    payload.update(classify(names), lib=lib)
    build, gfv_jars = payload['build'], payload['gfv_jars']
    items = []
    if build == 'native':
        items.append(make_item(
            'build', 'ok' if expect in ('any', 'native') else 'fail',
            'no gluten/velox jar in lib — the native build (same criterion as the lib gate of '
            'flink-velox-e2e-verify)',
            None if expect in ('any', 'native') else
            'build velox4j and gluten-flink and deploy the complete set into lib, then restart the '
            'cluster (a restart needs the user consent)'))
    elif build == 'gfv':
        items.append(make_item(
            'build', 'ok' if expect in ('any', 'gfv') else 'fail',
            'complete GlutenFlink set: ' + ' '.join(a[len('gluten-flink-'):] for a in GLUTEN_ARTIFACTS)
            + f' + velox4j + {len(RUNTIME_DEPS)} runtime deps',
            None if expect in ('any', 'gfv') else
            'move every gluten/velox jar out of lib and restart the cluster (a restart needs the user consent)'))
    else:
        shown = ', '.join(gfv_jars[:4]) + (' ...' if len(gfv_jars) > 4 else '')
        items.append(make_item(
            'build', 'fail',
            f'an incomplete GlutenFlink set is in lib: missing {", ".join(payload["missing_parts"])} '
            f'({len(gfv_jars)} gluten/velox jar(s): {shown}) — half a stack fails at class-loading',
            'deploy the complete GlutenFlink build into lib, or remove every gluten/velox jar, then restart '
            'the cluster (a restart needs the user consent)'))
    for artifact, jars in sorted(payload['duplicates'].items()):
        items.append(make_item(
            'duplicates', 'warn',
            f'several versions of {artifact} in lib: {", ".join(jars)} — remove the stale ones '
            'and restart the cluster (a restart needs the user consent)'))
    return payload, items


def next_step(expect, payload, items):
    by = {i['name']: i for i in items}
    if not payload['flink_home'] or by.get('lib', {}).get('status') == 'fail':
        return HINT_FLINK + ', then rerun'
    build = payload['build']
    if build == 'partial':
        return ('deploy the complete GlutenFlink build into lib (missing ' + ', '.join(payload['missing_parts'])
                + ') or remove every gluten/velox jar, then restart the cluster (a restart needs the '
                'user consent), then rerun')
    if expect == 'native' and build != 'native':
        return 'move every gluten/velox jar out of lib and restart the cluster (a restart needs the ' \
               'user consent), then rerun'
    if expect == 'gfv' and build != 'gfv':
        return 'build velox4j and gluten-flink and deploy the complete set into lib, then restart ' \
               'the cluster (a restart needs the user consent), then rerun'
    dups = payload['duplicates']
    if dups:
        return ('remove the stale duplicate jars (' + '; '.join(f'{a}: {", ".join(j)}' for a, j in sorted(dups.items()))
                + ') and restart the cluster (a restart needs the user consent), then rerun')
    return f'ready: lib holds the {build} build' + (' as expected' if expect != 'any' else '')


def build_result(expect):
    payload, items = scan(expect)
    missing = [f'{i["name"]}: {i["detail"]}' for i in items if i['status'] == 'fail']
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'expect': expect,
        'status': worst(i['status'] for i in items),
        'lib_state': payload,
        'items': items,
        'missing': missing,
        'next_step': next_step(expect, payload, items),
        'error_message': '' if success else f'{len(missing)} item(s) missing: ' + ', '.join(
            i['name'] for i in items if i['status'] == 'fail'),
    }
    return {KEY: result}, 0 if success else 1


def aborted(exc):
    return {KEY: {
        'success': False,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'expect': None,
        'status': 'fail',
        'lib_state': {'build': None, 'lib': None, 'flink_home': None, 'found_via': None, 'gfv_jars': [],
                      'gluten_jars': {}, 'velox4j_jars': [], 'runtime_deps': {'expected': len(RUNTIME_DEPS), 'missing': []},
                      'duplicates': {}, 'missing_parts': []},
        'items': [],
        'missing': [],
        'next_step': 'fix the check error first (see error_message), then rerun',
        'error_message': f'check aborted: {exc!r}',
    }}


def write_out(path, result, overwrite):
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('lib_state_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'lib_state_check.py: --out already exists, pass --overwrite to replace it: {path}',
              file=sys.stderr)
        return 2
    if overwrite and os.path.exists(target) and not stat.S_ISREG(os.stat(target).st_mode):
        # a fifo (or a directory, socket, device) is never opened: open() on a fifo
        # blocks until a reader shows up and the check would never return
        print(f'lib_state_check.py: --out exists and is not a regular file, refusing to overwrite it: {path}',
              file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'lib_state_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('check aborted'):
        return f"CHECK ERROR: {r['error_message']}\nnext: {r['next_step']}"
    s = r['lib_state']
    lines = []
    if r['success']:
        lines.append(f"lib-state   OK       {s['build']} build in {s['lib']}: " + r['items'][0]['detail'])
        for item in r['items']:
            if item['status'] == 'warn':
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
    else:
        lines.append(f"lib-state   FAIL     {len(r['missing'])} item(s) missing")
        for item in r['items']:
            if item['status'] in ('fail', 'warn'):
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
                if item.get('fix'):
                    lines.append(f"        fix: {item['fix']}")
    lines.append('')
    lines.append(f"READY: lib holds the {s['build']} build" if r['success']
                 else f"NOT READY: {len(r['missing'])} item(s) missing")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of what sits in the Flink lib/ directory '
                                                 '(native / GlutenFlink / partial build) for the Flink + GlutenFlink stack.')
    parser.add_argument('--expect', choices=('any', 'native', 'gfv'), default='any',
                        help='the build lib should hold: any (default), native (no gluten/velox jar), '
                             'gfv (the complete GlutenFlink set)')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project '
                                      'directory; an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    try:
        result, code = build_result(args.expect)
    except Exception as exc:
        result, code = aborted(exc), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
