#!/usr/bin/env python3
"""flink_dist_check.py — read-only check of the Flink installation the GFV stack runs on.

Locates the installation (FLINK_HOME, a flink on PATH, /opt/flink, /usr/local/flink;
the first with bin/flink), checks that the stock scripts and exactly one
lib/flink-dist*.jar are there, reads the version (jar name, else MANIFEST.MF) and
holds it to the 1.19 line (major.minor only — the stack is built and verified on
1.19.x, the patch level is free), notes a missing conf file, and resolves the REST
URL the same way the sibling skills do (FLINK_REST_URL, conf rest.address/rest.port,
stock localhost:8081). The script classifies only: it installs nothing, starts or
stops nothing, writes nowhere except the optional --out file.

Usage:
  flink_dist_check.py [--json] [--out FILE [--overwrite]]

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

Exit codes: 0 every item ok (warnings allowed); 1 at least one fail; 2 check error
(JSON in the full contract shape, error_message "check aborted: ..."), a refused
--out, or an invalid command line (usage on stderr, no JSON).

Test seam, used by the tier-A case only: ENV_CHECK_SYSROOT prefixes every absolute
system path (/opt/flink, /usr/local/flink, ...).
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import sys
import zipfile

KEY = 'flink_velox_flink_dist_check_result'

SYSROOT = os.environ.get('ENV_CHECK_SYSROOT', '')

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')

# The stack is built and verified on the 1.19 line; the patch level is free.
FLINK_LINE = (1, 19)
FLINK_FILES = ('bin/flink', 'bin/start-cluster.sh', 'bin/stop-cluster.sh', 'bin/sql-client.sh')
HINT_FLINK = ('install the official binary distribution '
              'flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it')
# Flink's stock REST endpoint; conf rest.address/rest.port override it.
DEFAULT_REST_URL = 'http://localhost:8081'


def sys_path(rel):
    """Absolute system path under the (optional) sysroot prefix."""
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
        # a flink on PATH is <home>/bin/flink, often reached through a symlink
        cands.append((os.path.dirname(os.path.dirname(os.path.realpath(flink))), 'PATH'))
    cands += [(sys_path('/opt/flink'), 'default'), (sys_path('/usr/local/flink'), 'default')]
    for cand, source in cands:
        if is_exec(os.path.join(cand, 'bin', 'flink')):
            return cand, source
    return (None, None)


def load_flink_conf(flink_home):
    """Flatten conf/config.yaml (nested maps) and conf/flink-conf.yaml (flat dotted
    keys) into one dict, without a YAML dependency — the same reading the sibling
    skills apply, so all of them derive the same REST URL. Scalar leaves only."""
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


def normalize_url(url):
    url = str(url).strip().rstrip('/')
    if not re.match(r'^https?://', url, re.I):
        url = 'http://' + url
    return url


def resolve_rest_url(conf):
    """$FLINK_REST_URL, then conf rest.address/rest.port, then the stock
    localhost:8081 — the order every skill of the family applies."""
    env = os.environ.get('FLINK_REST_URL')
    if env:
        return normalize_url(env)
    address = conf.get('rest.address') or 'localhost'
    port = str(conf.get('rest.port') or '8081')
    return normalize_url(f'{address}:{port}')


def dist_version(home):
    """(version, jar name, where the version came from, every flink-dist jar name
    in name order). The stock distribution names the jar flink-dist-<version>.jar;
    a renamed one still carries its version as Implementation-Version in
    META-INF/MANIFEST.MF. Read from the first jar in name order — the order
    Flink's own config.sh lists lib/*.jar."""
    names = [os.path.basename(j)
             for j in sorted(glob.glob(os.path.join(home, 'lib', 'flink-dist*.jar')))]
    if not names:
        return None, None, None, []
    name = names[0]
    m = re.search(r'-(\d+\.\d+\.\d+)', name)
    if m:
        return m.group(1), name, f'lib/{name}', names
    try:
        with zipfile.ZipFile(os.path.join(home, 'lib', name)) as zf:
            manifest = zf.read('META-INF/MANIFEST.MF').decode('utf-8', 'replace')
        m = re.search(r'^Implementation-Version:\s*(\S+)', manifest, re.M)
    except (OSError, KeyError, zipfile.BadZipFile):
        m = None
    return (m.group(1) if m else None), name, f'lib/{name} MANIFEST.MF', names


def version_line_ok(version):
    m = re.match(r'^(\d+)\.(\d+)\.', version or '')
    return bool(m) and (int(m.group(1)), int(m.group(2))) == FLINK_LINE


def scan():
    home, source = find_flink_home()
    payload = {'path': home, 'found_via': source, 'version': None, 'on_1_19_line': None,
               'version_source': None, 'dist_jars': [], 'rest_url': None, 'conf': {}}
    if not home:
        payload['rest_url'] = DEFAULT_REST_URL
        return payload, [make_item(
            'home', 'fail',
            'Flink not found: FLINK_HOME is unset or has no bin/flink, no flink on PATH, '
            'nothing under /opt/flink or /usr/local/flink', HINT_FLINK)]
    conf = load_flink_conf(home)
    payload['rest_url'] = resolve_rest_url(conf)
    payload['conf'] = {k: conf[k] for k in ('rest.address', 'rest.port', 'env.java.home') if k in conf}
    items = [make_item('home', 'ok', f'Flink {payload["version"] or "?"} at {home} (via {source})')]
    absent = [f for f in FLINK_FILES if not is_exec(os.path.join(home, *f.split('/')))]
    version, dist_jar, version_source, dist_jars = dist_version(home)
    if not dist_jar:
        absent.append('lib/flink-dist-*.jar')
    payload.update(version=version, version_source=version_source, dist_jars=dist_jars)
    items[0]['detail'] = (f'Flink {version or "?"} at {home} (via {source}'
                          + (f'; version from {version_source}' if version_source else '') + ')')
    if absent:
        items.append(make_item('files', 'fail', f'incomplete installation, missing {", ".join(absent)}',
                               'unpack the distribution again, or point FLINK_HOME at a complete one'))
    if len(dist_jars) > 1:
        # config.sh of the distribution: "Multiple flink-dist*.jar found ... Please resolve."
        items.append(make_item(
            'dist-jars', 'fail',
            f'several flink-dist jars in lib: {", ".join(dist_jars)} — Flink\'s scripts refuse to '
            'start with more than one (the version above is read from the first in name order)',
            'keep only the flink-dist jar of the installed version in lib, then restart the cluster '
            '(a restart needs the user consent)'))
    if not version:
        items.append(make_item(
            'version', 'fail',
            'cannot determine the Flink version: lib/flink-dist*.jar has no version in its '
            'name nor an Implementation-Version in its MANIFEST.MF', HINT_FLINK))
    elif not version_line_ok(version):
        items.append(make_item(
            'version', 'fail',
            f'Flink {version} is not on the 1.19 line — the stack is built and verified against '
            '1.19.x only', HINT_FLINK))
    else:
        payload['on_1_19_line'] = True
    if not any(os.path.isfile(os.path.join(home, 'conf', n))
               for n in ('config.yaml', 'flink-conf.yaml')):
        items.append(make_item('conf', 'warn', 'neither conf/config.yaml nor conf/flink-conf.yaml '
                                              'exists — the stock distribution ships one'))
    return payload, items


def next_step(payload, items):
    by_name = {i['name']: i for i in items}
    if not payload['path']:
        return HINT_FLINK + ', then rerun'
    if by_name.get('files', {}).get('status') == 'fail':
        return 'unpack the Flink distribution again (or point FLINK_HOME at a complete one), then rerun'
    if by_name.get('dist-jars', {}).get('status') == 'fail':
        return 'keep only the flink-dist jar of the installed version in lib, then restart the ' \
               'cluster (a restart needs the user consent), then rerun'
    if not payload['version']:
        return 'reinstall the Flink 1.19.x runtime (its flink-dist jar carries no readable version), then rerun'
    if not payload['on_1_19_line']:
        return f'install the Flink 1.19.x runtime instead (this installation is {payload["version"]}), then rerun'
    warnings = [i for i in items if i['status'] == 'warn']
    return f'ready: Flink {payload["version"]} at {payload["path"]}' + (' (warnings above)' if warnings else '')


def build_result():
    payload, items = scan()
    missing = [f'{i["name"]}: {i["detail"]}' for i in items if i['status'] == 'fail']
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': worst(i['status'] for i in items),
        'flink': payload,
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
        'flink': {'path': None, 'found_via': None, 'version': None, 'on_1_19_line': None,
                  'version_source': None, 'dist_jars': [], 'rest_url': None, 'conf': {}},
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
        print('flink_dist_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'flink_dist_check.py: --out already exists, pass --overwrite to replace it: {path}',
              file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:  # 'x' never follows a symlink
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'flink_dist_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('check aborted'):
        return f"CHECK ERROR: {r['error_message']}\nnext: {r['next_step']}"
    f = r['flink']
    lines = []
    if r['success']:
        lines.append(f"flink-dist  OK       {r['items'][0]['detail']}; REST {f['rest_url']}")
        for item in r['items']:  # a warning never blocks, but must be seen
            if item['status'] == 'warn':
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
    else:
        lines.append(f"flink-dist  FAIL     {len(r['missing'])} item(s) missing")
        for item in r['items']:
            if item['status'] in ('fail', 'warn'):
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
                if item.get('fix'):
                    lines.append(f"        fix: {item['fix']}")
    lines.append('')
    lines.append(f"READY: Flink {f['version']} at {f['path']}" if r['success'] and f['on_1_19_line']
                 else (f"READY (no version line check): {f['path']}" if r['success']
                       else f"NOT READY: {len(r['missing'])} item(s) missing"))
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the Flink installation '
                                                 '(files, flink-dist jar, 1.19 line, conf) for the '
                                                 'Flink + GlutenFlink stack.')
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
