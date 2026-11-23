#!/usr/bin/env python3
"""source_repos_check.py — read-only check of the three source checkouts the GFV stack is built from.

velox, velox4j and gluten: each must exist, be usable (the marker files of a
complete checkout are there), and report its branch and HEAD through git. The
branch is compared with the baseline the stack is built on; being elsewhere is
reported, not failed. The script classifies only: it clones nothing, fetches
nothing, writes nowhere except the optional --out file.

Usage:
  source_repos_check.py [--repos-dir DIR] [--json] [--out FILE [--overwrite]]

The checkouts: --repos-dir, else $GFV_REPOS_DIR, else a repos/ directory holding
at least one of them, walked upward from the working directory (bounded by
ENV_CHECK_WALK_ROOT when that tier-A seam is set). A single checkout can be
pinned with $VELOX_HOME / $VELOX4J_HOME / $GLUTEN_HOME.

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

Exit codes: 0 every checkout present and usable (warnings allowed); 1 at least one
missing or unusable; 2 check error (JSON in the full contract shape, error_message
"check aborted: ..."), a refused --out, or an invalid command line.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

KEY = 'flink_velox_source_repos_check_result'

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')

# name, upstream, baseline branch, files that make the checkout usable.
REPOS = (
    ('velox', 'https://github.com/bigo-sg/velox.git', 'gluten-20260829', ('CMakeLists.txt', 'scripts')),
    ('velox4j', 'https://github.com/bigo-sg/velox4j.git', 'gluten-20260829', ('pom.xml',)),
    ('gluten', 'https://github.com/apache/gluten.git', 'main', ('gluten-flink/pom.xml',)),
)
REPO_ENV = {'velox': 'VELOX_HOME', 'velox4j': 'VELOX4J_HOME', 'gluten': 'GLUTEN_HOME'}


def make_item(name, status, detail, fix=None, **extra):
    item = {'name': name, 'status': status, 'detail': detail}
    if fix:
        item['fix'] = fix
    item.update(extra)
    return item


def worst(statuses):
    statuses = list(statuses)
    return max(statuses, key=STATUS_ORDER.index) if statuses else 'ok'


def locate_repos_root(explicit):
    """(root, how): --repos-dir, $GFV_REPOS_DIR, then a repos/ directory holding at
    least one of the checkouts, walked upward from the working directory and
    bounded by ENV_CHECK_WALK_ROOT when set (a tier-A seam; default: the root)."""
    for cand, how in ((explicit, '--repos-dir'), (os.environ.get('GFV_REPOS_DIR'), 'GFV_REPOS_DIR')):
        if cand and os.path.isdir(cand) and any(
                os.path.isdir(os.path.join(cand, name)) for name, _, _, _ in REPOS):
            return os.path.realpath(cand), how
    here = os.getcwd()
    boundary = os.path.realpath(os.environ.get('ENV_CHECK_WALK_ROOT') or '/')
    while True:
        cand = os.path.join(here, 'repos')
        if os.path.isdir(cand) and any(os.path.isdir(os.path.join(cand, name)) for name, _, _, _ in REPOS):
            return os.path.realpath(cand), 'repos/ walked up from the working directory'
        parent = os.path.dirname(here)
        if here == boundary or parent == here:
            return None, None
        here = parent


def git_line(repo, *args):
    """One line of git output for the checkout, or None (no git, not a checkout, failure)."""
    git = shutil.which('git')
    if not git:
        return None
    try:
        proc = subprocess.run([git, '-C', repo, *args], capture_output=True, text=True,
                              errors='replace', timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def scan(explicit_root):
    root, how = locate_repos_root(explicit_root)
    payload = {'root': root, 'root_via': how, 'checkouts': {}}
    items = []
    for name, url, branch, markers in REPOS:
        pinned = os.environ.get(REPO_ENV[name])
        path = pinned or (os.path.join(root, name) if root else None)
        entry = {'path': path, 'branch': None, 'head': None, 'origin': None,
                 'baseline_branch': branch, 'on_baseline': None, 'pinned_via': REPO_ENV[name] if pinned else None}
        if not path or not os.path.isdir(path):
            shown = path or f'<repos dir>/{name}'
            entry['error'] = 'missing'
            where = f' into {root}' if root else ' (pass --repos-dir or set GFV_REPOS_DIR)'
            items.append(make_item(
                name, 'fail', f'no {name} checkout at {shown}',
                f'git clone -b {branch} {url}{where}, or set {REPO_ENV[name]}'))
        else:
            entry['path'] = os.path.realpath(path)
            absent = [m for m in markers if not os.path.exists(os.path.join(path, *m.split('/')))]
            if absent:
                entry['error'] = 'unusable'
                items.append(make_item(
                    name, 'fail', f'{path} is not a usable {name} checkout (missing {", ".join(absent)})',
                    f'git clone -b {branch} {url} afresh, or point --repos-dir (or {REPO_ENV[name]}) '
                    f'at a complete checkout'))
            else:
                entry['branch'] = git_line(path, 'rev-parse', '--abbrev-ref', 'HEAD')
                entry['head'] = git_line(path, 'rev-parse', '--short', 'HEAD')
                entry['origin'] = git_line(path, 'remote', 'get-url', 'origin')
                if entry['head']:
                    entry['on_baseline'] = entry['branch'] == branch
                    note = '' if entry['on_baseline'] else f'; baseline branch is {branch}'
                    items.append(make_item(
                        name, 'ok', f'{entry["branch"]} @ {entry["head"]} ({path}){note}', on_baseline=entry['on_baseline']))
                else:
                    where = 'git is not on PATH' if not shutil.which('git') else 'not a git checkout'
                    items.append(make_item(name, 'warn', f'{path}: {where}, branch and HEAD not read'))
        payload['checkouts'][name] = entry
    return payload, items


def next_step(payload, items):
    missing = [i for i in items if i['status'] == 'fail' and payload['checkouts'][i['name']]['error'] == 'missing']
    unusable = [i for i in items if i['status'] == 'fail' and payload['checkouts'][i['name']]['error'] == 'unusable']
    if missing:
        clones = '; '.join(f"git clone -b {payload['checkouts'][i['name']]['baseline_branch']} {url}"
                           for i in missing for n, url, _, _ in REPOS if n == i['name'])
        where = f" into {payload['root']}" if payload['root'] else ' (pass --repos-dir or set GFV_REPOS_DIR)'
        return f'clone the missing checkouts ({clones}){where}, then rerun'
    if unusable:
        return ('re-clone the unusable checkouts ('
                + ', '.join(i['name'] for i in unusable) + '), then rerun')
    warnings = [i for i in items if i['status'] == 'warn']
    off = [n for n, e in payload['checkouts'].items() if e.get('on_baseline') is False]
    note = ('; warnings above' if warnings else '') + (f"; not on the baseline branch: {', '.join(off)}" if off else '')
    return f"ready: the three source checkouts are in place at {payload['root']}{note}" if payload['root'] \
        else 'ready: the three source checkouts are in place' + note


def build_result(explicit_root):
    payload, items = scan(explicit_root)
    missing = [f'{i["name"]}: {i["detail"]}' for i in items if i['status'] == 'fail']
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': worst(i['status'] for i in items),
        'root': payload['root'],
        'root_via': payload['root_via'],
        'checkouts': payload['checkouts'],
        'items': items,
        'missing': missing,
        'next_step': next_step(payload, items),
        'error_message': '' if success else f'{len(missing)} item(s) missing: ' + ', '.join(
            i['name'] for i in items if i['status'] == 'fail'),
    }
    return {KEY: result}, 0 if success else 1


def aborted(exc):
    """A check crash in the full contract shape: every documented key exists."""
    return {KEY: {
        'success': False,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': 'fail',
        'root': None,
        'root_via': None,
        'checkouts': {},
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
        print('source_repos_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'source_repos_check.py: --out already exists, pass --overwrite to replace it: {path}',
              file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:  # 'x' never follows a symlink
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'source_repos_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('check aborted'):
        return f"CHECK ERROR: {r['error_message']}\nnext: {r['next_step']}"
    lines = []
    if r['success']:
        lines.append('source-repos OK       ' + '; '.join(f'{i["name"]} {i["detail"]}' for i in r['items']))
        for item in r['items']:  # a warning never blocks, but must be seen
            if item['status'] == 'warn':
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
    else:
        lines.append(f"source-repos FAIL     {len(r['missing'])} item(s) missing")
        for item in r['items']:
            if item['status'] in ('fail', 'warn'):
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
                if item.get('fix'):
                    lines.append(f"        fix: {item['fix']}")
    lines.append('')
    lines.append(f"READY: the three source checkouts are in place at {r['root']}" if r['success'] and r['root']
                 else ('READY: the three source checkouts are in place' if r['success']
                       else f"NOT READY: {len(r['missing'])} item(s) missing"))
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the velox / velox4j / gluten source '
                                                 'checkouts for the Flink + GlutenFlink stack.')
    parser.add_argument('--repos-dir', help='directory holding velox/ velox4j/ gluten/ (default: '
                                            '$GFV_REPOS_DIR, then repos/ walked upward from here)')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project '
                                      'directory; an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    try:
        result, code = build_result(args.repos_dir)
    except Exception as exc:  # a check bug must never be reported as "present"
        result, code = aborted(exc), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))  # a refused write is 2, the verdict still prints
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
