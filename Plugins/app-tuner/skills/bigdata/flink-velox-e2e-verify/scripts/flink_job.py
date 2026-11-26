#!/usr/bin/env python3
"""flink_job.py — REST helpers around a Flink SQL submission.

Subcommands:
  rest-url    Print the REST base URL for the cluster.
  snapshot    Print the job ids currently on the cluster, one per line.
  tms-local   Refuse (exit 2) unless every TaskManager runs on this
              machine — the print-sink capture reads only the local TM
              logs, so a remote TM would leave that partition's rows
              invisible to the comparison.
  wait        Follow the jobs that appeared after --before until every one
              reaches a terminal state, then check the plan against --mode.

rest-url resolution order: --rest argument, FLINK_REST_URL env variable,
rest.address / rest.port from $FLINK_HOME/conf/config.yaml (nested "rest:"
block) or conf/flink-conf.yaml (flat "rest.address:" lines — wins when both
exist), then http://localhost:8081. The sql client submits to the address in
the same conf file, so by default the client and this tool talk to one
cluster; an FLINK_REST_URL that differs from the conf only moves the observer
and ends in "no new job appeared".

Every REST call uses a no-proxy opener (ProxyHandler({})): environment
proxies (http_proxy etc.) are ignored on purpose, so a localhost or
intranet cluster address is never handed to a corporate proxy.

wait exit codes (aligned with the GlutenFlink flink-velox-e2e-test
conventions):
  0  every new job FINISHED and the plan matches the mode
  1  no new job appeared, or a job ended FAILED/CANCELED/SUSPENDED, or the
      plan does not match the mode, or the tool aborted on an unexpected
      error (the --out JSON then says so)
  2  REST unreachable (3 consecutive failed polls), or --out cannot be
      written (refused before anything runs, and re-checked at the write)
  3  timeout. Waiting for FINISHED gets --timeout seconds; waiting for the
      first new job to appear has its own cap (--first-job-timeout, default
      min(timeout, 90)) so a failed INSERT does not eat the whole budget.

tms-local exit codes:
  0  every registered TaskManager runs on this machine (or none is
      registered — the submission gates catch a cluster that cannot run)
  1  the /taskmanagers payload had an unexpected shape (tool error)
  2  REST unreachable, a TaskManager off this machine, or one whose host
      cannot be determined (refused — an unknown host may be remote)

On exit the --out JSON holds: rest/mode, per-job {jid, name, state,
duration_ms, gluten_nodes, plan_available, exception}, all_finished,
stack_check, tm_velox4j_lines (cluster-level GlutenFlink-load evidence from
TaskManager .out logs — diagnostic only), verdict, error_message, next_step —
the same key set on a normal exit and on a tool abort (verdict
"tool-aborted", observation slots empty, error_message "tool aborted: ...").
--out must be a regular path inside the project directory (the working
directory), not a symlink or a path outside it; an existing regular file is
only replaced with --overwrite.
"""
import argparse
import glob
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

TERMINAL_STATES = ('FINISHED', 'FAILED', 'CANCELED', 'SUSPENDED')
DEFAULT_REST_URL = 'http://localhost:8081'
OUT_SCOPE = ('--out must be a regular path inside the project directory (the working directory), '
             'not a symlink or a path outside it')


class RestError(Exception):
    """A REST call failed (network or decoding)."""


# REST talks to the cluster directly, never through a proxy: the default
# opener installs ProxyHandler(getproxies()), so a set http_proxy would
# swallow the localhost REST address and a pre-submit snapshot would judge
# a reachable cluster unreachable (same policy as flink-velox-env's
# cluster_check.py). Only http_get_json uses urllib in this file.
REST_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with REST_OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, ValueError) as exc:
        raise RestError(f'{url}: {exc}') from exc


def normalize_url(url):
    url = str(url).strip().rstrip('/')
    if not re.match(r'^https?://', url, re.I):
        url = 'http://' + url
    return url


def load_flink_conf(flink_home):
    """Flatten conf/config.yaml (nested maps) and conf/flink-conf.yaml (flat
    dotted keys) into one dict, without a YAML dependency — flink-conf.yaml
    is read last, so its keys win when both files exist. Scalar leaves
    only — lists and inline maps are skipped; the keys this tool needs
    (rest.address / rest.port) always live in simple key: value pairs."""
    conf = {}
    for name in ('config.yaml', 'flink-conf.yaml'):
        path = os.path.join(flink_home, 'conf', name)
        try:
            fh = open(path, encoding='utf-8', errors='replace')
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
                    continue  # list item, inline map, or anything unusual
                indent, key, value = m.group(1), m.group(2), (m.group(3) or '').strip()
                while stack and stack[-1][0] >= len(indent):
                    stack.pop()
                prefix = f'{stack[-1][1]}.' if stack else ''
                if value:
                    conf[prefix + key] = value.strip('\'"')
                else:
                    stack.append((len(indent), prefix + key))
    return conf


def resolve_rest_url(rest_arg, flink_home):
    if rest_arg:
        return normalize_url(rest_arg)
    env = os.environ.get('FLINK_REST_URL')
    if env:
        return normalize_url(env)
    if flink_home:
        conf = load_flink_conf(flink_home)
        address = conf.get('rest.address') or 'localhost'
        port = str(conf.get('rest.port') or '8081')
        return normalize_url(f'{address}:{port}')
    return DEFAULT_REST_URL


def gluten_mention(node):
    """True when a plan node's engine-generated fields identify it as a
    Gluten node: the operator name, operator_strategy, or an input's
    ship strategy / exchange mentions "gluten" (case-insensitive).
    Observed engine markers include the ship strategy "GlutenHASH" and
    operator names like "Gluten cal". The "description" field is
    deliberately excluded — it embeds user SQL text (table and column
    names), and a table named gluten_orders must not mark a node any
    more than a native plan with clean operators should be rejected."""
    if not isinstance(node, dict):
        return False
    texts = [str(node.get(k) or '')
             for k in ('operator', 'operator_strategy')]
    for inp in node.get('inputs') or []:
        if isinstance(inp, dict):
            texts += [str(inp.get(k) or '')
                      for k in ('ship_strategy', 'exchange')]
    return any('gluten' in t.lower() for t in texts)


def tm_velox4j_lines(flink_home):
    """Count lines mentioning velox4j in TaskManager .out logs — evidence
    the GlutenFlink stack was loaded when the cluster started. Diagnostic
    only: it proves nothing about whether any particular SQL used gluten
    operators (that is what the plan check is for). None when no TM log
    exists."""
    if not flink_home:
        return None
    total, seen = 0, False
    for path in sorted(glob.glob(os.path.join(flink_home, 'log', '*.out'))):
        base = os.path.basename(path)
        if 'taskexecutor' not in base and 'taskmanager' not in base:
            continue
        seen = True
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                total += sum(1 for line in fh if 'velox4j' in line.lower())
        except OSError:
            continue
    return total if seen else None


def tm_hint():
    """One-line TaskManager liveness hint for REST-unreachable errors."""
    try:
        proc = subprocess.run(['jps', '-lm'], capture_output=True,
                              text=True, timeout=5)
    except Exception:
        return ''
    alive = 'org.apache.flink.runtime.taskexecutor.TaskManagerRunner' in (proc.stdout or '')
    return (f' (jps: TaskManagerRunner {"alive" if alive else "not listed"}'
            ' — the TaskManager may be dead)')


LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')


def local_hostnames():
    """Every hostname that means "this machine" for the single-node guard."""
    names = set(LOCAL_HOSTS)
    for getter in (socket.gethostname, socket.getfqdn):
        try:
            name = getter()
        except OSError:
            continue
        if name:
            names.add(name.lower())
    return names


def tm_host(entry):
    """The host a /taskmanagers entry runs on. Flink 1.19 serves no
    dedicated host field — the host comes from the ActorRef "path"
    (akka://flink@host:port/...). Empty when neither yields a host."""
    if not isinstance(entry, dict):
        return ''
    host = str(entry.get('host') or '').strip().lower()
    if host:
        return host
    m = re.match(r'^[A-Za-z][\w+.-]*://[^@]*@([^:/]+)',
                 str(entry.get('path') or ''))
    return m.group(1).lower() if m else ''


def cmd_tms_local(args):
    base = normalize_url(args.rest)
    try:
        data = http_get_json(f'{base}/taskmanagers')
    except RestError as exc:
        print(f'REST unreachable: {exc}', file=sys.stderr)
        return 2
    tms = data.get('taskmanagers') if isinstance(data, dict) else None
    if not isinstance(tms, list):
        print('unexpected /taskmanagers payload — not a list of entries',
              file=sys.stderr)
        return 1
    local = local_hostnames()
    remote, unknown = [], []
    for entry in tms:
        host = tm_host(entry)
        if not host:
            unknown.append(str(entry.get('id') if isinstance(entry, dict)
                                else entry))
        elif host not in local:
            remote.append(host)
    if remote or unknown:
        detail = (f'not on this machine: {", ".join(remote)}' if remote
                  else f'host cannot be determined (may be remote): '
                       f'{", ".join(unknown)}')
        print(f'refusing: TaskManager(s) {detail}', file=sys.stderr)
        print('  the print-sink capture reads only this machine\'s '
              'TaskManager logs — on a distributed cluster the remote '
              'partitions\' rows would be invisible and a false MATCH '
              'could pass', file=sys.stderr)
        return 2
    if not tms:
        print('no TaskManagers registered — the submission gates will '
              'catch a cluster that cannot run the job')
        return 0
    hosts = ', '.join(sorted({tm_host(e) for e in tms}))
    print(f'all {len(tms)} TaskManagers are on this machine: {hosts}')
    return 0


def cmd_rest_url(args):
    print(resolve_rest_url(args.rest, args.flink_home))
    return 0


def cmd_snapshot(args):
    base = normalize_url(args.rest)
    try:
        data = http_get_json(f'{base}/jobs/overview')
    except RestError as exc:
        print(f'REST unreachable: {exc}', file=sys.stderr)
        return 2
    for job in data.get('jobs', []):
        if isinstance(job, dict):
            jid = job.get('jid') or job.get('id')
            if jid:
                print(jid)
    return 0


# --- --out ---------------------------------------------------------------------

def out_problem(path, overwrite):
    """Why --out cannot be written, or None. Checked before anything runs and again at the write."""
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(os.path.realpath(os.getcwd()) + os.sep):
        return '%s: %s' % (OUT_SCOPE, path)
    if os.path.exists(target):
        if not overwrite:
            return '--out already exists, pass --overwrite to replace it: %s' % path
        if not stat.S_ISREG(os.stat(target).st_mode):  # a fifo would block open() until a reader shows up
            return '--out exists and is not a regular file, refusing to overwrite it: %s' % path
    return None


def write_out(path, payload, overwrite):
    problem = out_problem(path, overwrite)
    if problem:
        print('flink_job.py: %s' % problem, file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print('flink_job.py: cannot write --out %s: %s' % (path, exc.strerror or exc), file=sys.stderr)
        return 2
    return 0


def cmd_wait(args):
    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs: a bad --out must not touch the machine
        if problem:
            print('flink_job.py: %s' % problem, file=sys.stderr)
            return 2
    try:
        return wait_jobs(args)
    except Exception as exc:  # unexpected: same key set as a normal result, observation slots empty
        payload = {'rest': normalize_url(args.rest), 'mode': args.mode, 'jobs': [],
                   'all_finished': False, 'stack_check': 'not-run',
                   'tm_velox4j_lines': None, 'verdict': 'tool-aborted',
                   'error_message': 'tool aborted: %r' % exc,
                   'next_step': 'fix the tool error first (see error_message), then rerun this side'}
        print('ERROR: ' + payload['error_message'], file=sys.stderr)
        if args.out:
            write_out(args.out, payload, args.overwrite)
        return 1


def wait_jobs(args):
    base = normalize_url(args.rest)
    before = set((args.before or '').split())
    poll = max(float(args.poll), 0.05)
    timeout = float(args.timeout)
    first_cap = (float(args.first_job_timeout) if args.first_job_timeout
                 else min(timeout, 90))

    def finish(verdict, jobs, stack_check, code, message=''):
        payload = {
            'rest': base,
            'mode': args.mode,
            'jobs': jobs,
            'all_finished': bool(jobs) and all(
                j['state'] == 'FINISHED' for j in jobs),
            'stack_check': stack_check,
            'tm_velox4j_lines': tm_velox4j_lines(args.flink_home),
            'verdict': verdict,
            'error_message': message,
            'next_step': '',
        }
        if message:
            print(message, file=sys.stderr)
        if args.out:
            code = max(code, write_out(args.out, payload, args.overwrite))
        return code

    def collect(new_jobs):
        """Fetch per-job details; return (report entries, summary lines)."""
        jobs_out, lines = [], []
        for jid in sorted(new_jobs):
            entry = new_jobs[jid]
            state = entry.get('state', 'UNKNOWN')
            try:
                detail = http_get_json(f'{base}/jobs/{jid}') or {}
            except RestError as exc:
                print(f'WARN: job detail for {jid}: {exc}', file=sys.stderr)
                detail = {}
            nodes = []
            try:
                plan = http_get_json(f'{base}/jobs/{jid}/plan') or {}
                inner = plan.get('plan')  # the REST API wraps the plan
                if isinstance(inner, dict):
                    plan = inner
                maybe = plan.get('nodes')
                if isinstance(maybe, list):
                    nodes = maybe
            except RestError as exc:
                print(f'WARN: job plan for {jid}: {exc}', file=sys.stderr)
            exception = ''
            if state != 'FINISHED':
                try:
                    exc_data = http_get_json(f'{base}/jobs/{jid}/exceptions') or {}
                    exception = str(exc_data.get('root-exception') or '')[:2000]
                except RestError:
                    pass
            jobs_out.append({
                'jid': jid,
                'name': entry.get('name'),
                'state': state,
                'duration_ms': detail.get('duration'),
                'gluten_nodes': sum(1 for n in nodes if gluten_mention(n)),
                'plan_available': bool(nodes),
                'exception': exception,
            })
            lines.append(f"job {jid} {state} "
                         f"duration={detail.get('duration')}ms "
                         f"gluten_nodes={jobs_out[-1]['gluten_nodes']}")
        return jobs_out, lines

    # Phase 1: wait for the submission to show up (own budget, like the
    # GlutenFlink e2e-test helper — a failed INSERT must not eat the whole
    # timeout).
    failures = 0
    first_deadline = time.monotonic() + first_cap
    new_jobs = {}
    while True:
        try:
            data = http_get_json(f'{base}/jobs/overview')
            failures = 0
        except RestError as exc:
            failures += 1
            if failures >= 3:
                return finish('rest-unreachable', [], 'not-run', 2,
                              f'ERROR: REST unreachable: {exc}{tm_hint()}')
            time.sleep(poll)
            continue
        current = {j['jid']: j for j in data.get('jobs', [])
                   if isinstance(j, dict) and j.get('jid')
                   and j['jid'] not in before}
        if current:
            new_jobs = current
            break
        if time.monotonic() >= first_deadline:
            return finish('no-new-job', [], 'not-run', 1,
                          f'ERROR: no new job appeared on {base} within '
                          f'{first_cap:g}s — the INSERT failed before '
                          'submission (see the submit log)')
        time.sleep(poll)

    # Phase 2: follow every new job to a terminal state; two consecutive
    # terminal polls guard against a REST visibility lag making us call a
    # still-submitted batch complete too early.
    deadline = time.monotonic() + timeout
    stable = 0
    while True:
        try:
            data = http_get_json(f'{base}/jobs/overview')
            failures = 0
        except RestError as exc:
            failures += 1
            if failures >= 3:
                jobs, _ = collect(new_jobs)
                return finish('rest-unreachable', jobs, 'not-run', 2,
                              f'ERROR: REST unreachable: {exc}{tm_hint()}')
            time.sleep(poll)
            continue
        for j in data.get('jobs', []):
            if isinstance(j, dict) and j.get('jid') in new_jobs:
                new_jobs[j['jid']] = j
        states = {jid: entry.get('state', 'UNKNOWN')
                  for jid, entry in new_jobs.items()}
        if all(s in TERMINAL_STATES for s in states.values()):
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
            if time.monotonic() >= deadline:
                jobs, _ = collect(new_jobs)
                return finish('timeout', jobs, 'not-run', 3,
                              f'ERROR: jobs not terminal after {timeout:g}s: '
                              f'{states} — the job may still be RUNNING; '
                              'cancel it or keep waiting, and do not restart '
                              'the cluster over a running job')
        time.sleep(poll)

    jobs, lines = collect(new_jobs)
    for line in lines:
        print(line)
    if not all(j['state'] == 'FINISHED' for j in jobs):
        bad = '; '.join(f"{j['jid']} {j['state']}"
                        for j in jobs if j['state'] != 'FINISHED')
        root = next((j['exception'] for j in jobs if j['exception']), '')
        return finish('failed', jobs, 'not-run', 1,
                      f'ERROR: job(s) did not finish: {bad}'
                      + (f' — root exception: {root[:400]}' if root else ''))

    if args.skip_plan_check:
        return finish('ok', jobs, 'skipped', 0)
    if any(not j['plan_available'] for j in jobs):
        return finish('plan-mismatch', jobs, 'failed: plan unavailable', 1,
                      'ERROR: job plan could not be fetched — cannot confirm '
                      'which stack ran')
    if args.mode == 'gfv':
        glutenless = [j['jid'] for j in jobs if j['gluten_nodes'] == 0]
        if glutenless:
            tm = tm_velox4j_lines(args.flink_home)
            if tm:
                note = ('velox4j lines ARE present in the TM logs — the '
                        'cluster loaded the GlutenFlink stack but this SQL '
                        'fell back to native operators')
            else:
                note = ('no velox4j lines in the TM logs either — the '
                        'cluster is probably not the GlutenFlink build')
            return finish(
                'plan-mismatch', jobs, 'failed: no gluten nodes in plan', 1,
                f'ERROR: gfv mode but no gluten nodes in the plan of '
                f'{", ".join(glutenless)} ({note}); a fallen-back case '
                'compares native against native and proves nothing')
    else:
        gluteny = [j['jid'] for j in jobs if j['gluten_nodes'] > 0]
        if gluteny:
            return finish(
                'plan-mismatch', jobs, 'failed: gluten nodes in plan', 1,
                f'ERROR: native mode but the plan of {", ".join(gluteny)} '
                'mentions gluten — the cluster still runs the GlutenFlink '
                'planner; removing the jars is not enough, restart on the '
                'native build')
    return finish('ok', jobs, 'ok', 0)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='REST helpers around a Flink SQL submission.')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('rest-url', help='print the resolved REST base URL')
    p.add_argument('--flink-home', default=os.environ.get('FLINK_HOME'))
    p.add_argument('--rest', default=None,
                   help='explicit URL (else FLINK_REST_URL, else conf, '
                        'else http://localhost:8081)')
    p.set_defaults(func=cmd_rest_url)

    p = sub.add_parser('snapshot', help='print current job ids')
    p.add_argument('--rest', required=True)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser('tms-local',
                       help='refuse unless every TaskManager runs on '
                            'this machine (the print-sink capture is '
                            'local-only)')
    p.add_argument('--rest', required=True)
    p.set_defaults(func=cmd_tms_local)

    p = sub.add_parser('wait', help='follow new jobs to a terminal state, '
                                    'then check the plan against the mode')
    p.add_argument('--rest', required=True)
    p.add_argument('--before', required=True,
                   help='job ids that existed before the submission '
                        '(whitespace separated)')
    p.add_argument('--mode', required=True, choices=('native', 'gfv'))
    p.add_argument('--flink-home', default=os.environ.get('FLINK_HOME'))
    p.add_argument('--out', default=None,
                   help='write the job/verdict JSON here (inside the project '
                        'directory; an existing file is kept unless '
                        '--overwrite)')
    p.add_argument('--overwrite', action='store_true',
                   help='let --out replace an existing regular file')
    p.add_argument('--timeout', type=float, default=600,
                   help='seconds to wait for FINISHED (default 600)')
    p.add_argument('--poll', type=float, default=1,
                   help='poll interval seconds (default 1)')
    p.add_argument('--first-job-timeout', type=float, default=None,
                   help='seconds to wait for the first new job to appear '
                        '(default min(timeout, 90))')
    p.add_argument('--skip-plan-check', action='store_true',
                   help='do not compare the plan against the mode')
    p.set_defaults(func=cmd_wait)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
