#!/usr/bin/env python3
"""cluster_check.py — read-only check of the Flink cluster of this installation.

Answers: are the daemons of THIS installation running (attributed by the
flink-dist jar on their classpath, read from /proc), which build did they load
(the gluten/velox jars on that classpath), does lib/ still match what they
loaded (the GFV jars on disk vs loaded; any jar replaced after the earliest
daemon started), and what does REST say (overview only; environment proxies are
ignored so a localhost address is never rerouted). Structural problems fail: not
running, a JobManager without TaskManagers (or the reverse), daemons that
disagree on the build, lib/ changed since the start. REST oddities and running
jobs warn. The script classifies only: it starts or stops nothing, runs no
Flink script, writes nowhere except the optional --out file.

Usage:
  cluster_check.py [--json] [--out FILE [--overwrite]]

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

Exit codes: 0 the cluster is up, consistent and answers (warnings allowed);
1 a structural problem (see above) or no Flink installation; 2 check error
(JSON in the full contract shape, error_message "check aborted: ..."), a
refused --out, or an invalid command line (usage on stderr, no JSON).

Test seams, used by the tier-A case only: ENV_CHECK_SYSROOT prefixes every
absolute system path (/opt/flink, ...); ENV_CHECK_PROC_ROOT replaces /proc.
"""
import argparse
import datetime
import glob
import http.client
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request

KEY = 'flink_velox_cluster_check_result'

SYSROOT = os.environ.get('ENV_CHECK_SYSROOT', '')
PROC_ROOT = os.environ.get('ENV_CHECK_PROC_ROOT', '/proc')

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')

DEFAULT_REST_URL = 'http://localhost:8081'
JM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.entrypoint\.\w+Entrypoint$')
TM_ENTRY_RE = re.compile(r'^org\.apache\.flink\.runtime\.taskexecutor\.TaskManagerRunner$')
GFV_JAR_RE = re.compile(r'gluten|velox', re.I)
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


# --- the installation and its conf -----------------------------------------
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


def load_flink_conf(flink_home):
    conf = {}
    for name in ('config.yaml', 'flink-conf.yaml'):
        try:
            fh = open(os.path.join(flink_home, 'conf', name), encoding='utf-8', errors='replace')
        except OSError:
            continue
        stack = []
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
    return url if re.match(r'^https?://', url, re.I) else 'http://' + url


def resolve_rest_url(conf):
    env = os.environ.get('FLINK_REST_URL')
    if env:
        return normalize_url(env)
    return normalize_url(f"{conf.get('rest.address') or 'localhost'}:{conf.get('rest.port') or '8081'}")


def dist_version(home):
    names = sorted(os.path.basename(j) for j in glob.glob(os.path.join(home, 'lib', 'flink-dist*.jar')))
    m = re.search(r'-(\d+\.\d+\.\d+)', names[0]) if names else None
    return (m.group(1) if m else None), names


def rest_overview(rest_url):
    """GET <rest>/overview, or None when nothing answers. Environment proxies
    are ignored on purpose so a localhost address is never rerouted."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(rest_url + '/overview', headers={'Accept': 'application/json'})
        with opener.open(req, timeout=3) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None


# --- the daemons -----------------------------------------------------------
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


def proc_start_epoch(pid, boot_time):
    """Start time of a pid from /proc/<pid>/stat field 22 (clock ticks since
    boot) plus /proc/stat btime."""
    try:
        with open(os.path.join(PROC_ROOT, str(pid), 'stat')) as fh:
            line = fh.read()
        fields = line[line.rindex(')') + 2:].split()
        if boot_time is None:
            return None
        return boot_time + int(fields[19]) / float(os.sysconf('SC_CLK_TCK') or 100)
    except (OSError, ValueError, IndexError):
        return None


def scan_flink_processes(flink_home):
    """(daemons of this installation: [{pid, role, gfv_jars, started}], how many
    Flink daemons of OTHER installations run)."""
    procs, others = [], 0
    real_lib = os.path.realpath(os.path.join(flink_home, 'lib'))
    try:
        with open(os.path.join(PROC_ROOT, 'stat'), encoding='utf-8', errors='replace') as fh:
            boot_time = int(re.search(r'^btime (\d+)', fh.read(), re.M).group(1))
    except (OSError, AttributeError, ValueError):
        boot_time = None
    try:
        pids = sorted(e for e in os.listdir(PROC_ROOT) if e.isdigit())
    except OSError:
        return procs, others
    for pid in pids:
        argv = read_cmdline(pid)
        entry = next((t for t in argv if JM_ENTRY_RE.match(t) or TM_ENTRY_RE.match(t)), None)
        if not entry:
            continue
        dist = [p for p in classpath_of(argv) if os.path.basename(p).startswith('flink-dist')]
        if not any(os.path.dirname(os.path.realpath(p)) == real_lib for p in dist):
            others += 1
            continue
        procs.append({'pid': int(pid), 'role': 'jobmanager' if JM_ENTRY_RE.match(entry) else 'taskmanager',
                      'gfv_jars': sorted(os.path.basename(p) for p in classpath_of(argv)
                                         if GFV_JAR_RE.search(os.path.basename(p))),
                      'started': proc_start_epoch(pid, boot_time)})
    return procs, others


def started_at(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().strftime('%Y-%m-%d %H:%M:%S') if epoch else None


def scan():
    home, source = find_flink_home()
    payload = {'flink_home': home, 'found_via': source, 'running': False, 'jobmanagers': [], 'taskmanagers': [],
               'other_flink_daemons': 0, 'loaded_build': None, 'loaded_gfv_jars': [], 'lib_gfv_jars': [],
               'lib_version': None, 'earliest_start': None, 'jars_replaced_after_start': [],
               'consistent_with_lib': None, 'rest_url': None, 'rest_reachable': False, 'rest_version': None,
               'rest_taskmanagers': None, 'rest_jobs_running': None}
    if not home:
        payload['rest_url'] = DEFAULT_REST_URL
        return payload, [make_item(
            'home', 'fail',
            'Flink not found: FLINK_HOME is unset or has no bin/flink, no flink on PATH, nothing under '
            '/opt/flink or /usr/local/flink — running daemons cannot be attributed to an installation',
            HINT_FLINK)]
    conf = load_flink_conf(home)
    payload['rest_url'] = resolve_rest_url(conf)
    version, _ = dist_version(home)
    payload['lib_version'] = version
    lib = os.path.join(home, 'lib')
    payload['lib_gfv_jars'] = sorted(os.path.basename(j) for j in glob.glob(os.path.join(lib, '*.jar'))
                                     if GFV_JAR_RE.search(os.path.basename(j)))
    daemons, others = scan_flink_processes(home)
    payload['other_flink_daemons'] = others
    for d in daemons:
        payload['jobmanagers' if d['role'] == 'jobmanager' else 'taskmanagers'].append(
            {'pid': d['pid'], 'gfv_jars': d['gfv_jars'], 'started': started_at(d['started']), 'started_epoch': d['started']})
    jm, tm = payload['jobmanagers'], payload['taskmanagers']
    running = bool(jm or tm)
    payload['running'] = running
    items = []
    if others:
        items.append(make_item(
            'other-clusters', 'info',
            f'{others} Flink daemon(s) of other installations run on this machine — a WebUI or REST port '
            f'other than {payload["rest_url"]} is theirs'))
    if jm and tm:
        items.append(make_item('processes', 'ok', f'JobManager {jm[0]["pid"]} and {len(tm)} TaskManager(s) '
                                                  'of this installation'))
    elif jm:
        items.append(make_item(
            'processes', 'fail', f'JobManager {jm[0]["pid"]} runs but no TaskManager of this installation '
            'does — jobs cannot start',
            'start the TaskManagers (or restart the cluster cleanly; a restart needs the user consent)'))
    elif tm:
        items.append(make_item(
            'processes', 'fail', f'{len(tm)} TaskManager(s) of this installation run without a JobManager — '
            'leftover processes',
            'stop the leftover TaskManagers and restart the cluster cleanly (a restart needs the user consent)'))
    else:
        items.append(make_item('processes', 'fail', 'not running: no JobManager and no TaskManager of this '
                                                  'installation',
                                'start the cluster (starting or restarting needs the user consent)'))
    if running:
        daemons_all = jm + tm
        per = ['gfv' if d['gfv_jars'] else 'native' for d in daemons_all]
        payload['loaded_build'] = per[0] if len(set(per)) == 1 else 'mixed'
        loaded = sorted({j for d in daemons_all for j in d['gfv_jars']})
        payload['loaded_gfv_jars'] = loaded
        if payload['loaded_build'] == 'mixed':
            items.append(make_item(
                'loaded-build', 'fail',
                'the daemons disagree: some carry gluten/velox jars on their classpath, some do not — '
                'restart the cluster as a whole (a restart needs the user consent)'))
        else:
            items.append(make_item(
                'loaded-build', 'ok',
                'every daemon carries ' + (', '.join(loaded) if loaded
                                           else 'no gluten/velox jar on its classpath (native build)')))
        starts = [d['started_epoch'] for d in daemons_all if d['started_epoch']]
        earliest = min(starts) if starts else None
        payload['earliest_start'] = started_at(earliest)
        replaced = []
        if earliest:
            for jar in sorted(glob.glob(os.path.join(lib, '*.jar'))):
                try:
                    if os.stat(jar).st_mtime > earliest:
                        replaced.append(os.path.basename(jar))
                except OSError:
                    continue
        payload['jars_replaced_after_start'] = replaced
        if loaded != payload['lib_gfv_jars'] or replaced:
            what = []
            if loaded != payload['lib_gfv_jars']:
                what.append(f'on disk {", ".join(payload["lib_gfv_jars"]) or "none"}; '
                            f'loaded {", ".join(loaded) or "none"}')
            if replaced:
                what.append('replaced after the start: ' + ', '.join(replaced[:5]) + (' ...' if len(replaced) > 5 else ''))
            payload['consistent_with_lib'] = False
            items.append(make_item(
                'lib-vs-cluster', 'fail',
                'lib/ changed since the cluster started (' + '; '.join(what) + ') — the cluster still runs '
                'what it loaded',
                'restart the cluster so it loads what is in lib now (a restart needs the user consent)'))
        else:
            payload['consistent_with_lib'] = True
            items.append(make_item(
                'lib-vs-cluster', 'ok', f'lib/ is unchanged since the cluster started ({payload["earliest_start"]})'))
        for d in daemons_all:
            d.pop('started_epoch', None)
    else:
        payload['consistent_with_lib'] = None
    overview = rest_overview(payload['rest_url'])
    if overview is not None:
        payload.update(rest_reachable=True, rest_version=str(overview.get('flink-version') or '') or None,
                       rest_taskmanagers=overview.get('taskmanagers'), rest_jobs_running=overview.get('jobs-running'))
        summary = (f'REST {payload["rest_url"]} answers: Flink {payload["rest_version"] or "?"}, '
                   f'{payload["rest_taskmanagers"]} TaskManager(s), {payload["rest_jobs_running"]} job(s) running')
        if not running:
            items.append(make_item(
                'rest', 'warn', summary + ' — but no daemon of this installation runs here: it is another '
                'installation, a container, or a remote rest.address; it is not this cluster'))
        else:
            items.append(make_item('rest', 'ok', summary))
            if payload['rest_version'] and version and payload['rest_version'] != version:
                items.append(make_item(
                    'rest', 'warn', f'the REST endpoint reports Flink {payload["rest_version"]} while {home} '
                    f'is {version} — it is not this installation'))
            if isinstance(payload['rest_taskmanagers'], int) and payload['rest_taskmanagers'] < len(tm):
                items.append(make_item(
                    'rest', 'warn', f'{len(tm)} TaskManager(s) of this installation run but REST lists '
                    f'{payload["rest_taskmanagers"]} — still registering, or registered with another JobManager'))
            if payload['rest_jobs_running']:
                items.append(make_item(
                    'jobs', 'warn', f'{payload["rest_jobs_running"]} job(s) are running — a restart would '
                    'interrupt them'))
    elif running:
        items.append(make_item(
            'rest', 'warn', f'REST {payload["rest_url"]} does not answer although the daemons are up — still '
            'starting, or rest.address/rest.port differ'))
    else:
        items.append(make_item('rest', 'info', f'REST {payload["rest_url"]} does not answer (not running)'))
    return payload, items


def next_step(payload, items):
    by = {}
    for i in items:
        by.setdefault(i['name'], i)
    if not payload['flink_home']:
        return HINT_FLINK + ', then rerun'
    if not payload['running']:
        return by['processes']['fix'] + ', then rerun'  # the same wording as the item's fix
    proc = by.get('processes', {})
    if proc.get('status') == 'fail':
        return proc['fix'] + ', then rerun'
    if by.get('loaded-build', {}).get('status') == 'fail':
        return 'restart the cluster as a whole so every daemon loads the same build (a restart needs the ' \
               'user consent), then rerun'
    if by.get('lib-vs-cluster', {}).get('status') == 'fail':
        return 'restart the cluster so it loads what is in lib now (a restart needs the user consent), then rerun'
    warnings = [i['name'] for i in items if i['status'] == 'warn']
    return f"ready: the cluster runs the {payload['loaded_build']} build that is in lib" \
        + (' (warnings above)' if warnings else '')


def build_result():
    payload, items = scan()
    missing = [f'{i["name"]}: {i["detail"]}' for i in items if i['status'] == 'fail']
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': worst(i['status'] for i in items),
        'cluster': payload,
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
        'cluster': {'flink_home': None, 'found_via': None, 'running': False, 'jobmanagers': [], 'taskmanagers': [],
                    'other_flink_daemons': 0, 'loaded_build': None, 'loaded_gfv_jars': [], 'lib_gfv_jars': [],
                    'lib_version': None, 'earliest_start': None, 'jars_replaced_after_start': [],
                    'consistent_with_lib': None, 'rest_url': None, 'rest_reachable': False, 'rest_version': None,
                    'rest_taskmanagers': None, 'rest_jobs_running': None},
        'items': [],
        'missing': [],
        'next_step': 'fix the check error first (see error_message), then rerun',
        'error_message': f'check aborted: {exc!r}',
    }}


def write_out(path, result, overwrite):
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('cluster_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'cluster_check.py: --out already exists, pass --overwrite to replace it: {path}', file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print(f'cluster_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('check aborted'):
        return f"CHECK ERROR: {r['error_message']}\nnext: {r['next_step']}"
    lines = []
    oks = [i for i in r['items'] if i['status'] == 'ok']
    if r['success']:
        lines.append('cluster     OK       ' + '; '.join(f'{i["name"]}: {i["detail"]}' for i in oks))
    else:
        lines.append(f"cluster     FAIL     {len(r['missing'])} item(s) missing")
    for item in r['items']:
        if item['status'] in ('fail', 'warn'):
            lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
            if item.get('fix'):
                lines.append(f"        fix: {item['fix']}")
        elif item['status'] == 'info':
            lines.append(f"  INFO  {item['name']}: {item['detail']}")
    lines.append('')
    lines.append('READY: the cluster runs the build that is in lib' if r['success']
                 else f"NOT READY: {len(r['missing'])} item(s) missing")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the Flink cluster of this installation '
                                                 '(daemons, loaded build, lib consistency, REST) for the '
                                                 'Flink + GlutenFlink stack.')
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
