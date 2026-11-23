#!/usr/bin/env python3
"""nexmark_check.py — read-only check of the nexmark benchmark pieces for the GFV stack.

Two things make a nexmark run possible: the nexmark-flink jar in the Flink lib/
(nexmark has no release artifact — it is built from github.com/nexmark/nexmark),
and a runnable nexmark tree (bin/run_query.sh plus the query/resource files) under
$NEXMARK_HOME, /usr/local/nexmark or /opt/nexmark. The script reports both; the
check is ready only when both are there. It classifies only: it builds nothing,
copies nothing, writes nowhere except the optional --out file.

Usage:
  nexmark_check.py [--json] [--out FILE [--overwrite]]

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

Exit codes: 0 jar and runnable tree present; 1 either missing (or no Flink
installation to look into); 2 check error (JSON in the full contract shape,
error_message "check aborted: ..."), a refused --out, or an invalid command
line (usage on stderr, no JSON).

Test seam, used by the tier-A case only: ENV_CHECK_SYSROOT prefixes every
absolute system path (/opt/flink, /usr/local/nexmark, ...).
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import sys

KEY = 'flink_velox_nexmark_check_result'

SYSROOT = os.environ.get('ENV_CHECK_SYSROOT', '')

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')
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


def scan():
    home, source = find_flink_home()
    payload = {'flink_home': home, 'found_via': source, 'jar': None, 'jars': [],
               'home': None, 'home_via': None}
    if not home:
        return payload, [make_item(
            'flink', 'fail',
            'Flink not found: FLINK_HOME is unset or has no bin/flink, no flink on PATH, '
            'nothing under /opt/flink or /usr/local/flink — the nexmark jar is looked for in its lib/',
            HINT_FLINK)]
    lib = os.path.join(home, 'lib')
    jars = sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, '*nexmark*.jar'))) \
        if os.path.isdir(lib) else []
    payload['jars'] = jars
    payload['jar'] = jars[0] if jars else None
    homes = [('$NEXMARK_HOME', os.environ.get('NEXMARK_HOME')),
             ('default', sys_path('/usr/local/nexmark')), ('default', sys_path('/opt/nexmark'))]
    home_via, tree = None, None
    for how, cand in homes:
        if cand and is_exec(os.path.join(cand, 'bin', 'run_query.sh')):
            home_via, tree = how, cand
            break
    payload.update(home=tree, home_via=home_via)
    items = []
    if jars:
        items.append(make_item('jar', 'ok', f'{jars[0]} in lib'))
        if len(jars) > 1:
            items.append(make_item('jar', 'warn', f'several nexmark jars in lib: {", ".join(jars)} — '
                                 'the first in name order is used; remove the stale ones'))
    else:
        items.append(make_item(
            'jar', 'fail', f'no nexmark jar in {lib}',
            'build nexmark-flink from https://github.com/nexmark/nexmark '
            '(mvn -pl nexmark-flink -am package -DskipTests) and copy the jar into lib'))
    if tree:
        items.append(make_item('home', 'ok', f'runnable tree at {tree} (via {home_via})'))
    else:
        items.append(make_item(
            'home', 'fail',
            'no runnable nexmark tree (bin/run_query.sh) under $NEXMARK_HOME, /usr/local/nexmark '
            'or /opt/nexmark',
            'copy nexmark-flink/src/main/resources of the nexmark checkout as the runnable tree '
            'and point NEXMARK_HOME at it'))
    return payload, items


def next_step(payload, items):
    by = {i['name']: i for i in items}
    if not payload['flink_home']:
        return HINT_FLINK + ', then rerun'
    todos = []
    if by['jar']['status'] == 'fail':
        todos.append('build the nexmark-flink jar (mvn -pl nexmark-flink -am package -DskipTests) and copy it into lib')
    if by['home']['status'] == 'fail':
        todos.append('lay out the runnable nexmark tree (nexmark-flink/src/main/resources) and point NEXMARK_HOME at it')
    if todos:
        return 'nexmark is not ready: ' + ' and '.join(todos) + ', then rerun'
    extra = [i for i in items if i['status'] == 'warn']
    return 'ready: nexmark jar and runnable tree are in place' + ('; warnings above' if extra else '')


def build_result():
    payload, items = scan()
    missing = [f'{i["name"]}: {i["detail"]}' for i in items if i['status'] == 'fail']
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': worst(i['status'] for i in items),
        'nexmark': payload,
        'items': items,
        'missing': missing,
        'next_step': next_step(payload, items),
        'error_message': '' if success else f'{len(missing)} item(s) missing: ' + ', '.join(
            i['name'] for i in items if i['status'] == 'fail'),
    }
    return {KEY: result}, 0 if success else 1


def aborted(exc):
    return {KEY: {
        'success': False,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': 'fail',
        'nexmark': {'flink_home': None, 'found_via': None, 'jar': None, 'jars': [], 'home': None, 'home_via': None},
        'items': [],
        'missing': [],
        'next_step': 'fix the check error first (see error_message), then rerun',
        'error_message': f'check aborted: {exc!r}',
    }}


def write_out(path, result, overwrite):
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('nexmark_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'nexmark_check.py: --out already exists, pass --overwrite to replace it: {path}',
              file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'nexmark_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('check aborted'):
        return f"CHECK ERROR: {r['error_message']}\nnext: {r['next_step']}"
    lines = []
    if r['success']:
        lines.append('nexmark     OK       ' + '; '.join(f'{i["name"]}: {i["detail"]}' for i in r['items'] if i['status'] == 'ok'))
        for item in r['items']:
            if item['status'] == 'warn':
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
    else:
        lines.append(f"nexmark     FAIL     {len(r['missing'])} item(s) missing")
        for item in r['items']:
            if item['status'] in ('fail', 'warn'):
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
                if item.get('fix'):
                    lines.append(f"        fix: {item['fix']}")
    lines.append('')
    lines.append('READY: nexmark jar and runnable tree are in place' if r['success']
                 else f"NOT READY: {len(r['missing'])} item(s) missing")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the nexmark jar and runnable tree '
                                                 'for the Flink + GlutenFlink stack.')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project '
                                      'directory; an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    try:
        result, code = build_result()
    except Exception as exc:
        result, code = aborted(exc), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
