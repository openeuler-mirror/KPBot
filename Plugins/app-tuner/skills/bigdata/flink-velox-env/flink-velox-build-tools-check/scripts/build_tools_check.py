#!/usr/bin/env python3
"""build_tools_check.py — read-only check of the build-side tools the GlutenFlink stack needs.

Which tools: ninja, ccache, autoconf, automake, libtool, flex, bison, python3
and patchelf — looked up on PATH and asked for --version. The script classifies
only: it installs nothing, changes nothing, writes nowhere except the optional
--out file.

Usage:
  build_tools_check.py [--json] [--out FILE [--overwrite]]

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

Exit codes: 0 every tool present; 1 at least one missing; 2 check error (JSON in
the full contract shape, error_message "check aborted: ..."), a refused --out, or
an invalid command line (usage on stderr, no JSON).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

KEY = 'flink_velox_build_tools_check_result'

# tool -> openEuler package; only ninja differs from the tool name.
TOOLS = (('ninja', 'ninja-build'), ('ccache', 'ccache'), ('autoconf', 'autoconf'),
         ('automake', 'automake'), ('libtool', 'libtool'), ('flex', 'flex'), ('bison', 'bison'),
         ('python3', 'python3'), ('patchelf', 'patchelf'))

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')


def run_argv(argv, timeout=15):
    """Run a fixed argv (never through a shell); first output line or ''."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ''
    lines = (proc.stdout or '').splitlines() + (proc.stderr or '').splitlines()
    return lines[0].strip() if lines else ''


def probe_tool(name):
    """(path, version): the version is the first x.y.z on the tool's --version
    line ("ccache version 4.8.3" -> 4.8.3), else that whole line."""
    path = shutil.which(name)
    if not path:
        return None, ''
    line = run_argv([path, '--version'])
    m = re.search(r'\d+(?:\.\d+)+', line)
    return path, (m.group(0) if m else line)


def make_item(name, status, detail, fix=None, **extra):
    item = {'name': name, 'status': status, 'detail': detail}
    if fix:
        item['fix'] = fix
    item.update(extra)
    return item


def worst(statuses):
    statuses = list(statuses)
    return max(statuses, key=STATUS_ORDER.index) if statuses else 'ok'


def scan():
    tools, items = {}, []
    for name, package in TOOLS:
        path, version = probe_tool(name)
        tools[name] = {'path': path, 'version': version or None, 'package': package}
        if path:
            items.append(make_item(name, 'ok', f'{version or "version unknown"} ({path})',
                                   version=version or None, package=package))
        else:
            items.append(make_item(name, 'fail', 'not found on PATH',
                                   f'system package (openEuler name): {package}', package=package))
    missing = [item['name'] for item in items if item['status'] == 'fail']
    return tools, items, missing


def next_step(missing, tools):
    if not missing:
        return 'ready: the build tools are complete'
    return 'install the missing build tools (dnf install ' \
        + ' '.join(tools[m]['package'] for m in missing) + '), then rerun'


def build_result():
    tools, items, missing = scan()
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': worst(item['status'] for item in items),
        'tools': tools,
        'items': items,
        'missing': [f'{m}: not found on PATH' for m in missing],
        'next_step': next_step(missing, tools),
        'error_message': '' if success else f'{len(missing)} tool(s) missing: ' + ', '.join(missing),
    }
    return {KEY: result}, 0 if success else 1


def aborted(exc):
    """A check crash in the full contract shape: every documented key exists, so a
    consumer never trips over a missing field while handling the failure."""
    return {KEY: {
        'success': False,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': 'fail',
        'tools': {},
        'items': [],
        'missing': [],
        'next_step': 'fix the check error first (see error_message), then rerun',
        'error_message': f'check aborted: {exc!r}',
    }}


def write_out(path, result, overwrite):
    """The --out archive stays inside the project (the working directory), is never a
    symlink, and is never truncated without --overwrite. Returns the exit code to use
    when the write is refused (0 when written); the verdict itself is kept either way."""
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('build_tools_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'build_tools_check.py: --out already exists, pass --overwrite to replace it: {path}',
              file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:  # 'x' never follows a symlink
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:  # the archive is a side output: say so, keep the verdict
        print(f'build_tools_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    lines = []
    if r['error_message'].startswith('check aborted'):
        lines.append(f"CHECK ERROR: {r['error_message']}")
        lines.append(f"next: {r['next_step']}")
        return '\n'.join(lines)
    if r['success']:
        lines.append('build-tools OK       all %d present: ' % len(r['items']) + ', '.join(
            f'{i["name"]} {i["version"] or "version unknown"}' for i in r['items']))
    else:
        lines.append(f'build-tools FAIL     {len(r["missing"])} of {len(r["items"])} missing')
        for item in r['items']:
            if item['status'] != 'ok':
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
                if item.get('fix'):
                    lines.append(f"        fix: {item['fix']}")
    lines.append('')
    lines.append('READY: the build tools are complete' if r['success']
                 else f"NOT READY: {len(r['missing'])} tool(s) missing")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the build tools '
                                                 '(git cmake gcc g++ mvn) for the Flink + GlutenFlink stack.')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project '
                                      'directory; an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    try:
        result, code = build_result()
    except Exception as exc:  # a check bug must never be reported as "present"
        result, code = aborted(exc), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))  # a refused write is 2, the verdict still prints
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
