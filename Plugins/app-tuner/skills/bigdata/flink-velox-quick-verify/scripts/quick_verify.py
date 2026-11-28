#!/usr/bin/env python3
"""quick_verify.py — single-side quick semantic verification of Flink SQL
against a Python reference model (SKILL.md §执行步骤/§输出 carries the
full behavioral spec).

One SQL job per scenario goes to whatever build the cluster currently runs
(native Flink or GlutenFlink), the print-sink changelog is captured from
the TM .out logs, folded to the final result set, and compared — exact
multiset equality — with the scenario's reference model; a mismatch is
attributed per column so a divergence names the offending expression.

The invariants this implementation is pinned on (tests in cases/
quick-contract): an incomplete scenario FAILs by name, never silently
vanishes; a scenario whose sql client hangs, times out or loses REST
cancels its leftover job(s) best-effort and an unconfirmed cancel skips
the remaining scenarios (two print streams must not mix into one
capture); a TM log replaced or truncated mid-run is detected by file
identity (inode/mtime/size/boundary line), read from the top, and
noted; only the folded final state is
compared (intermediate changelog updates are not independently checked);
the plan is fetched on BOTH sides and gated symmetrically on engine fields
only — gluten nodes required on the GlutenFlink side, forbidden on the
native side (a cluster still running the GlutenFlink planner must not
report native green), and an unread plan fails either side.

plan (the default) probes and prints, submits and writes nothing (--out
aside). run needs --yes. Exit codes: plan 0 / 2 refused or aborted; run 0
all pass, 1 a scenario failed, 2 refused (a blocked prerequisite, run
without --yes, a bad --out) or a tool abort (the result keeps the full key
set with the observation slots empty).
"""
import argparse
import datetime
import glob
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

KEY = 'flink_velox_quick_verify_result'
SCRIPT = os.path.realpath(__file__)
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIO_ROOT = os.path.join(SKILL_DIR, 'scenarios')
PLACEHOLDER = '{{INPUT_DIR}}'
DEFAULT_TIMEOUT = 600
POLL = 0.5
FIRST_JOB_CAP = 90
REST_FAIL_MAX = 3
TERMINAL_STATES = ('FINISHED', 'FAILED', 'CANCELED', 'SUSPENDED')
GLUTEN_JARS = ('gluten', 'velox')
CHANGLOG_FLAG = re.compile(r'[-+][IUD]\[')
EXPECTED_ROW = re.compile(r'^\+I\[(.*)\]$')
OUT_SCOPE = ('--out must be a regular path inside the project directory (the working directory), '
             'not a symlink or a path outside it')
EVIDENCE_SCOPE = ('the evidence path must be a regular path inside the project directory '
                  '(the working directory), not a symlink or a path outside it')

# REST talks to the cluster directly, never through a proxy: a set
# http_proxy would swallow a localhost REST address and make a reachable
# cluster look dead (same policy as the flink-velox-env family).
REST_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


class RestError(Exception):
    pass


def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with REST_OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, ValueError) as exc:
        raise RestError(f'{url}: {exc}') from exc


def http_patch(url, timeout=10):
    """PATCH that only needs the 2xx — anything else raises RestError."""
    req = urllib.request.Request(url, method='PATCH',
                                 headers={'Accept': 'application/json'})
    try:
        with REST_OPENER.open(req, timeout=timeout) as resp:
            resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, ValueError) as exc:
        raise RestError(f'{url}: {exc}') from exc


def normalize_url(url):
    url = str(url).strip().rstrip('/')
    if not re.match(r'^https?://', url, re.I):
        url = 'http://' + url
    return url


def load_flink_conf(flink_home):
    """Flatten conf/config.yaml (nested maps) and conf/flink-conf.yaml
    (flat dotted keys), no YAML dependency; the latter wins. Scalars."""
    conf = {}
    for name in ('config.yaml', 'flink-conf.yaml'):
        path = os.path.join(flink_home, 'conf', name)
        try:
            fh = open(path, encoding='utf-8', errors='replace')
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


def resolve_rest_url(flink_home):
    env = os.environ.get('FLINK_REST_URL')
    if env:
        return normalize_url(env)
    if flink_home:
        conf = load_flink_conf(flink_home)
        address = conf.get('rest.address') or 'localhost'
        port = str(conf.get('rest.port') or '8081')
        return normalize_url(f'{address}:{port}')
    return 'http://localhost:8081'


def rest_alive(base, timeout=3):
    try:
        http_get_json(f'{base}/jobs/overview', timeout=timeout)
        return True
    except RestError:
        return False


def gluten_mention(node):
    """GlutenFlink node by engine fields (operator/operator_strategy/input
    ship_strategy/exchange) — the description embeds user SQL and never counts."""
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
    """velox4j mentions in the TM .out logs — diagnostic stack evidence."""
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


# --- scenarios ----------------------------------------------------------------

def scenario_dirs():
    """Every scenario directory, complete or not — an incomplete one fails
    loudly in plan/run, it never silently vanishes (zero-scenario all-pass
    is impossible)."""
    if not os.path.isdir(SCENARIO_ROOT):
        return []
    return [entry for entry in sorted(os.listdir(SCENARIO_ROOT))
            if os.path.isdir(os.path.join(SCENARIO_ROOT, entry))]


def scenario_paths(name):
    base = os.path.join(SCENARIO_ROOT, name)
    return {'root': base,
            'case_sql': os.path.join(base, 'case.sql'),
            'input_dir': os.path.join(base, 'input'),
            'reference': os.path.join(base, 'reference.py'),
            'readme': os.path.join(base, 'README.md')}


def sink_columns(case_sql_text):
    """Sink column names from the DDL (paren-aware; None → col_1..col_n)."""
    m = re.search(r'CREATE\s+TABLE\s+sink\s*\(', case_sql_text, re.I)
    if not m:
        return None
    depth, start = 1, m.end()
    parts, cur = [], []
    for ch in case_sql_text[start:]:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                parts.append(''.join(cur))
                break
        elif ch == ',' and depth == 1:
            parts.append(''.join(cur))
            cur = []
            continue
        cur.append(ch)
    names = []
    for part in parts:
        token = part.strip().split()[0] if part.strip() else ''
        if token:
            names.append(token)
    return names or None


# --- probing ------------------------------------------------------------------

def find_flink_home(explicit):
    if explicit:
        return explicit
    env = os.environ.get('FLINK_HOME')
    if env:
        return env
    search = os.path.realpath(os.getcwd())
    while search != os.sep:
        probe = os.path.join(search, '.gfvbot', 'env.json')
        if os.path.isfile(probe):
            try:
                with open(probe, encoding='utf-8') as fh:
                    path = json.load(fh).get('stack', {}).get('flink', {}).get('path', '')
                if isinstance(path, str) and path:
                    return path
            except (OSError, ValueError):
                pass
        search = os.path.dirname(search)
    return None


def detect_side(flink_home):
    """Expected build from the lib; either side may run, it is recorded
    (with the plan gates below), never enforced."""
    jars = []
    for jar in sorted(glob.glob(os.path.join(flink_home, 'lib', '*.jar'))):
        name = os.path.basename(jar).lower()
        if any(word in name for word in GLUTEN_JARS):
            jars.append(os.path.basename(jar))
    return ('GlutenFlink', jars) if jars else ('native', [])


def blocked(reason, fix):
    return {'status': 'blocked', 'detail': reason, 'fix': fix}


def probe(opts, names):
    """Everything the run needs, with blocked findings instead of exceptions."""
    findings = []
    state = {'flink_home': opts['flink_home'], 'side': None,
             'side_jars': [], 'rest': None, 'sql_client': None}
    flink_home = opts['flink_home']
    if not flink_home:
        findings.append(blocked(
            'FLINK_HOME is not set and no .gfvbot/env.json (stack.flink.path) '
            'was found upward from the working directory',
            'point --flink-home at the Flink installation (or set FLINK_HOME)'))
        return findings, state
    if not os.path.isdir(flink_home):
        findings.append(blocked('Flink home not found: %s' % flink_home,
                                'point --flink-home at the Flink installation (or set FLINK_HOME)'))
        return findings, state
    client = os.path.join(flink_home, 'bin', 'sql-client.sh')
    if not os.path.isfile(client) or not os.access(client, os.X_OK):
        findings.append(blocked(
            'sql client not found or not executable: %s' % client,
            'check $FLINK_HOME/bin/sql-client.sh; nothing was submitted'))
        return findings, state
    state['sql_client'] = client
    if not os.path.isdir(os.path.join(flink_home, 'lib')):
        findings.append(blocked('no lib directory under %s — cannot tell which '
                                'build this is' % flink_home,
                                'point --flink-home at a complete installation'))
    else:
        state['side'], state['side_jars'] = detect_side(flink_home)
    state['rest'] = resolve_rest_url(flink_home)
    if not rest_alive(state['rest']):
        findings.append(blocked(
            'the cluster REST endpoint %s does not answer' % state['rest'],
            'start the cluster first, then rerun (this skill never starts or '
            'restarts anything itself)'))
    if not names:
        findings.append(blocked(
            'the scenario library is empty: no scenario directory under %s'
            % SCENARIO_ROOT,
            'restore the scenarios shipped with the skill — a run over zero '
            'scenarios says nothing'))
    return findings, state


def scenario_problem(name):
    """(reason, fix) when a scenario cannot run at all (a missing
    case.sql/input/reference.py included), else None."""
    paths = scenario_paths(name)
    if not os.path.isfile(paths['case_sql']):
        return ('%s/case.sql is missing' % name,
                'restore the scenario — an incomplete scenario fails, it is '
                'never silently dropped from the run')
    with open(paths['case_sql'], encoding='utf-8') as fh:
        text = fh.read()
    if PLACEHOLDER not in text:
        return ('%s/case.sql carries no %s path placeholder' % (name, PLACEHOLDER),
                'fix the scenario so the executor can point the source at its input')
    if not os.path.isdir(paths['input_dir']) or not os.listdir(paths['input_dir']):
        return ('%s/input is missing or empty' % name,
                'restore the scenario input CSVs')
    if not os.path.isfile(paths['reference']):
        return ('%s/reference.py is missing' % name,
                'restore the scenario reference model')
    return None


# --- --out / evidence guards --------------------------------------------------

def guarded_real(path, scope):
    """The one path the guard judges AND the write opens: realpath resolves
    symlinks before '..' (kernel semantics) — a lexical fold would let
    'link/../x' escape; real != abspath means a symlink component. Returns
    (real, None) or (None, reason)."""
    real = os.path.realpath(path)  # resolves the existing prefix; tolerates a missing tail
    lexical = os.path.abspath(path)
    if real != lexical:
        # a symlink component was resolved somewhere in the traversal: the
        # family rule refuses to write through links at all (even when the
        # link points back inside the project)
        return None, ('%s: %s resolves to %s — a path with a symlink component is '
                      'never written through' % (scope, path, real))
    if not real.startswith(os.path.realpath(os.getcwd()) + os.sep):
        return None, '%s: %s' % (scope, path)
    return real, None


def out_problem(path, overwrite):
    real, problem = guarded_real(path, OUT_SCOPE)
    if problem:
        return problem
    if os.path.exists(real):
        if not overwrite:
            return '--out already exists, pass --overwrite to replace it: %s' % path
        if not stat.S_ISREG(os.stat(real).st_mode):
            return '--out exists and is not a regular file, refusing to overwrite it: %s' % path
    return None


def write_out(path, result, overwrite):
    problem = out_problem(path, overwrite)
    if problem:
        print('quick_verify.py: %s' % problem, file=sys.stderr)
        return 2
    # open the same path the guard just judged — the resolved real path,
    # never the raw argument (its '..'s and links are already folded away)
    real = os.path.realpath(path)
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print('quick_verify.py: cannot write --out %s: %s' % (path, exc.strerror or exc),
              file=sys.stderr)
        return 2
    return 0


def evidence_problem(path):
    """Why an evidence file cannot be (re)written, or None: symlinks and
    non-regular files (a fifo would block after a whole scenario ran) are
    never written through — the scenario fails."""
    real, problem = guarded_real(path, EVIDENCE_SCOPE)
    if problem:
        return problem
    if os.path.exists(real) and not stat.S_ISREG(os.stat(real).st_mode):
        return 'evidence exists and is not a regular file, refusing to overwrite it: %s' % path
    return None


def evidence_write(path, text):
    problem = evidence_problem(path)
    if problem:
        return problem
    real = os.path.realpath(path)  # judged and opened as the same resolved path
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, 'w', encoding='utf-8') as fh:
            fh.write(text)
    except OSError as exc:
        return 'cannot write %s: %s' % (path, exc.strerror or exc)
    return None


# --- scenario execution -------------------------------------------------------

def snapshot_tm_logs(flink_home):
    """(path, (lines, ino, mtime_ns, size, last-line prefix)) per TM .out —
    the identity the capture later diffs against: a changed inode means
    replaced (rotation); a boundary line that is no longer the old last
    line means truncated/rewritten in place."""
    snap = []
    for path in sorted(glob.glob(os.path.join(flink_home, 'log', '*.out'))):
        try:
            st = os.stat(path)
            lines, last = 0, ''
            with open(path, encoding='utf-8', errors='replace') as fh:
                for lines, last in enumerate(fh, 1):
                    pass
            snap.append((path, (lines, st.st_ino, st.st_mtime_ns,
                                st.st_size, last[:80])))
        except OSError:
            continue
    return snap


def capture_changelog(flink_home, snapshot):
    """Changelog rows appended since the snapshot (flag anywhere in the
    line, prefixes stripped). Returns (rows, errors, warnings): a warning =
    replaced/truncated mid-run → the file is read from the top, the record
    says the capture may be incomplete (not an auto-FAIL)."""
    rows, capture_errors, warnings = [], [], []
    known = {path: ident for path, ident in snapshot}
    files = sorted(glob.glob(os.path.join(flink_home, 'log', '*.out')))
    for path in files:
        ident = known.get(path)
        skip = ident[0] if ident else None
        if ident:
            try:
                st = os.stat(path)
            except OSError:
                continue
            reasons = []
            if st.st_ino != ident[1] or st.st_mtime_ns < ident[2] or st.st_size < ident[3]:
                reasons.append('file identity changed (snapshot: inode %d, %d bytes; '
                               'now: inode %d, %d bytes)' % (ident[1], ident[3],
                                                             st.st_ino, st.st_size))
            if not reasons and skip:  # the boundary line must still be the old last line
                boundary = None
                try:
                    with open(path, encoding='utf-8', errors='replace') as fh:
                        for lineno, line in enumerate(fh, 1):
                            if lineno >= skip:
                                boundary = line
                                break
                except OSError:
                    boundary = None
                if boundary is None or not boundary.startswith(ident[4]):
                    reasons.append('the pre-run boundary line is gone (truncated or '
                                  'rewritten in place)')
            if reasons:
                warnings.append('%s was replaced or truncated during the run: %s — '
                                'read from the top, rows printed before the rewrite '
                                'may be missing' % (path, '; '.join(reasons)))
                skip = 0
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                for lineno, line in enumerate(fh, 1):
                    if skip is not None and lineno <= skip:
                        continue
                    m = CHANGLOG_FLAG.search(line)
                    if not m:
                        continue
                    row = line[m.start():].rstrip()
                    if not row.endswith(']'):
                        capture_errors.append('%s:%d: starts a changelog row '
                                              'but never terminates: %r' % (path, lineno, row))
                        continue
                    rows.append(row)
        except OSError:
            continue
    return rows, capture_errors, warnings


def fold_changelog(rows):
    """Final multiset: +I/+U add, -U/-D withdraw the complete carried row."""
    final, stats, fold_errors = {}, {}, []
    for row in rows:
        flag, payload = row[:2], row[3:-1]
        stats[flag] = stats.get(flag, 0) + 1
        if flag in ('+I', '+U'):
            final[payload] = final.get(payload, 0) + 1
        else:
            if final.get(payload, 0) <= 0:
                fold_errors.append('%s withdraws a row never present: [%s]' % (flag, payload))
                continue
            final[payload] -= 1
            if final[payload] == 0:
                del final[payload]
    return final, stats, fold_errors


def run_reference(paths, timeout=120):
    """expected(input_dir) as a subprocess (no shared code). (rows, problem)."""
    argv = [sys.executable, paths['reference'], 'expected', paths['input_dir']]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, 'the reference model did not answer within %ss' % timeout
    except OSError as exc:
        return None, 'the reference model did not start: %s' % exc
    if proc.returncode != 0:
        return None, 'the reference model exited %d: %s' % (
            proc.returncode, (proc.stderr or '').strip()[:400])
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        return None, 'the reference model printed no JSON'
    if not isinstance(rows, list) or any(not isinstance(r, str) for r in rows):
        return None, 'the reference model output is not a list of row strings'
    bad = [r for r in rows if not EXPECTED_ROW.match(r)]
    if bad:
        return None, ('the reference model returned %d row(s) outside the +I[...] '
                      'final-row contract (v1 scenarios are final-state result sets)' % len(bad))
    return rows, None


def multiset_diff(a, b):
    """Rows of a not covered by b (multiset subtraction)."""
    return {row: n - b.get(row, 0) for row, n in a.items() if n > b.get(row, 0)}


def attribute(expected, captured, columns):
    """Per-column attribution: differing rows paired by first column (best
    effort, most equal cells), unpairable rows counted separately."""
    missing = multiset_diff(expected, captured)
    extra = multiset_diff(captured, expected)
    missing_instances = sorted(row for row, n in missing.items() for _ in range(n))
    extra_instances = [row for row, n in extra.items() for _ in range(n)]

    def cells(payload):
        return payload.split(', ')

    width = max([len(cells(r)) for r in missing_instances + extra_instances] or [0])
    names = columns if columns and len(columns) >= width else \
        ['col_%d' % (i + 1) for i in range(width)]
    per_column = {n: {'column': n, 'mismatches': 0, 'samples': []} for n in names[:width]}
    used = [False] * len(extra_instances)
    unpaired_missing = 0
    for mrow in missing_instances:
        mcells = cells(mrow)
        candidates = [i for i, erow in enumerate(extra_instances)
                      if not used[i] and cells(erow)[0] == mcells[0]]
        if not candidates:
            unpaired_missing += 1
            continue
        best = max(candidates,
                   key=lambda i: sum(a == b for a, b in
                                     zip(mcells, cells(extra_instances[i]))))
        used[best] = True
        ecells = cells(extra_instances[best])
        for i, name in enumerate(names[:width]):
            mval = mcells[i] if i < len(mcells) else ''
            eval_ = ecells[i] if i < len(ecells) else ''
            if mval != eval_:
                per_column[name]['mismatches'] += 1
                if len(per_column[name]['samples']) < 3:
                    per_column[name]['samples'].append(
                        {'expected': mval, 'actual': eval_})
    diffs = [d for d in per_column.values() if d['mismatches']]
    return diffs, unpaired_missing, used.count(False)


def wait_jobs(rest, before, timeout):
    """Follow the new jobs to a terminal state. (jobs, problem, code) —
    jobs carries the last known {jid,name,state} even on the timeout /
    rest-unreachable failures (the caller must be able to cancel what may
    still run)."""

    def snapshot():
        return [{'jid': jid, 'name': entry.get('name'),
                 'state': entry.get('state', 'UNKNOWN')}
                for jid, entry in sorted(new_jobs.items())]

    def overview_or_none():
        try:
            return http_get_json(f'{rest}/jobs/overview'), None
        except RestError as exc:
            return None, exc

    poll, failures = POLL, 0
    deadline_first = time.monotonic() + min(timeout, FIRST_JOB_CAP)
    new_jobs = {}
    while True:
        data, exc = overview_or_none()
        if data is None:
            failures += 1
            if failures >= REST_FAIL_MAX:
                return snapshot(), ('REST %s became unreachable after submission: %s — '
                                    'job state unknown; find the job on the WebUI first, '
                                    'do not restart the cluster while its state is unknown'
                                    % (rest, exc)), 'rest-unreachable'
        else:
            failures = 0
            current = {j['jid']: j for j in data.get('jobs', [])
                       if isinstance(j, dict) and j.get('jid') and j['jid'] not in before}
            if current:
                new_jobs = current
                break
            if time.monotonic() >= deadline_first:
                return ([], 'no new job appeared on %s within %gs — the INSERT failed '
                        'before submission (see the submit log)'
                        % (rest, min(timeout, FIRST_JOB_CAP)), 'no-new-job')
        time.sleep(poll)
    deadline = time.monotonic() + timeout
    stable = 0
    while True:
        data, exc = overview_or_none()
        if data is None:
            failures += 1
            if failures >= REST_FAIL_MAX:
                return snapshot(), ('REST %s became unreachable after submission: %s — '
                                    'job state unknown; do not restart the cluster over a '
                                    'running job' % (rest, exc)), 'rest-unreachable'
        else:
            failures = 0
            for job in data.get('jobs', []):
                if isinstance(job, dict) and job.get('jid') in new_jobs:
                    new_jobs[job['jid']] = job
            states = {jid: entry.get('state', 'UNKNOWN') for jid, entry in new_jobs.items()}
            if all(s in TERMINAL_STATES for s in states.values()):
                stable += 1
                if stable >= 2:  # guard against REST visibility lag
                    break
            else:
                stable = 0
                if time.monotonic() >= deadline:
                    return snapshot(), ('the job(s) did not reach a terminal state '
                                        'within %gs (still %s) — do not restart the '
                                        'cluster over a running job'
                                        % (timeout, states)), 'timeout'
        time.sleep(poll)
    return snapshot(), None, None


def settle_leftover(rest, jobs):
    """Best-effort cancel of a scenario's leftover job(s) (performance-verify
    pattern: PATCH /jobs/<jid>/cancel, confirm terminal over REST; never
    fatal). Returns (confirmed, note, jobs)."""
    if not jobs:
        return False, ('no job id could be read — the submitted job may still '
                       'be running'), jobs
    label = ', '.join(j['jid'] for j in jobs)
    try:
        for job in jobs:
            http_patch(f'{rest}/jobs/{job["jid"]}/cancel')
    except RestError as exc:
        return False, ('could not cancel %s: %s' % (label, exc)), jobs
    for _ in range(12):  # ~6s: the cancel must stick before the next submit
        try:
            data = http_get_json(f'{rest}/jobs/overview')
        except RestError:
            data = None
        if isinstance(data, dict):
            states = {j.get('jid'): j.get('state') for j in data.get('jobs', [])
                      if isinstance(j, dict)}
            if all(states.get(j['jid']) in TERMINAL_STATES for j in jobs):
                settled = ', '.join('%s %s' % (j['jid'], states[j['jid']]) for j in jobs)
                return True, 'leftover job(s) cancelled over REST: %s' % settled, jobs
        time.sleep(POLL)
    return False, ('the cancel was sent but %s did not reach a terminal state '
                   'over REST' % label), jobs


def hang_leftovers(rest, before):
    """(jobs, note) — what a sql client that hit the subprocess timeout may
    have left behind: the jobs submitted after `before` that are not in a
    terminal state (a FINISHED one needs no cancel). note is non-None when
    the overview could not be read — then which jobs ran is unknown, so
    nothing may be assumed cancelled."""
    try:
        data = http_get_json(f'{rest}/jobs/overview')
    except RestError as exc:
        return None, ('the REST overview could not be read to tell which jobs '
                      'the timed-out client submitted: %s' % exc)
    jobs = [j for j in data.get('jobs', [])
            if isinstance(j, dict) and j.get('jid') not in before
            and j.get('state') not in TERMINAL_STATES]
    return jobs, None


def plan_gluten_nodes(rest, jobs):
    """gluten node count per job from the plan. (counts, errors) — an
    unread plan must never fake a green gate."""
    counts, errors = {}, {}
    for job in jobs:
        nodes = []
        try:
            plan = http_get_json(f'{rest}/jobs/{job["jid"]}/plan') or {}
            inner = plan.get('plan')
            if isinstance(inner, dict):
                plan = inner
            if isinstance(plan.get('nodes'), list):
                nodes = plan['nodes']
        except RestError as exc:
            errors[job['jid']] = str(exc)
        counts[job['jid']] = sum(1 for n in nodes if gluten_mention(n))
    return counts, errors


def run_scenario(name, opts, state, leftover=None):
    """The five steps for one scenario; returns the record. On a hung
    client, timeout or REST loss the dict `leftover` (when given) carries
    {'confirmed', 'note', 'jobs'} so the runner can stop before the next
    submit — two print streams would mix into one capture."""
    ev = opts['evidence_dir']
    paths = scenario_paths(name)
    rec = {'name': name, 'status': 'pending', 'side': state['side'],
           'rows_expected': None, 'rows_captured': None, 'column_diffs': [],
           'unpaired_missing': None, 'unpaired_extra': None,
           'capture': os.path.join(ev, name + '.out'),
           'submit_log': os.path.join(ev, name + '.submit.log'),
           'gluten_nodes': None, 'detail': ''}

    def fail(detail):
        rec['status'] = 'fail'
        rec['detail'] = detail
        print('  [%s] FAIL: %s' % (name, detail), file=sys.stderr)
        return rec

    # ---- step 1: gate — the scenario must be complete, the cluster must
    # answer and every evidence path must be writable.
    problem = scenario_problem(name)
    if problem:
        return fail('%s; %s' % problem)
    rest = state['rest']
    if not rest_alive(rest):
        return fail('REST %s unreachable — nothing was submitted' % rest)
    for path in (rec['capture'], rec['submit_log'],
                 os.path.join(ev, name + '.submit.sql'),
                 os.path.join(ev, name + '.job.json')):
        problem = evidence_problem(path)
        if problem:
            return fail(problem)

    # ---- step 2: submit, tracked to FINISHED over REST
    with open(paths['case_sql'], encoding='utf-8') as fh:
        case_text = fh.read()
    if PLACEHOLDER not in case_text:
        return fail('case.sql carries no %s placeholder' % PLACEHOLDER)
    submit_sql = os.path.join(ev, name + '.submit.sql')
    problem = evidence_write(submit_sql, case_text.replace(PLACEHOLDER, paths['input_dir']))
    if problem:
        return fail(problem)
    try:
        before = {jid for jid in
                  (j.get('jid') for j in
                   http_get_json(f'{rest}/jobs/overview').get('jobs', [])
                   if isinstance(j, dict))}
    except RestError as exc:
        return fail('REST %s unreachable before submit — nothing was submitted: %s'
                    % (rest, exc))
    tm_snapshot = snapshot_tm_logs(state['flink_home'])
    with open(rec['submit_log'], 'w', encoding='utf-8') as log_fh:
        try:
            proc = subprocess.run([state['sql_client'], '-f', submit_sql],
                                  stdout=log_fh, stderr=subprocess.STDOUT,
                                  timeout=max(opts['timeout'] + 60, 120))
            submit_rc = proc.returncode
        except subprocess.TimeoutExpired:
            window = max(opts['timeout'] + 60, 120)
            # a hung client leaves the job state unknown — the leftover
            # protocol owns the window's jobs from here (settle_leftover
            # below is the same path a REST loss takes)
            leftovers, problem = hang_leftovers(rest, before)
            confirmed = False
            if problem is None:
                if leftovers:
                    confirmed, problem, leftovers = settle_leftover(rest, leftovers)
                else:
                    confirmed = True
                    problem = 'no job it submitted is still running over REST'
            if leftover is not None:
                leftover.update(confirmed=confirmed, note=problem,
                                jobs=leftovers or [])
            return fail('the sql client did not return within %ss — the job '
                         'state is unknown; %s' % (window, problem))
        except OSError as exc:
            return fail('the sql client did not start: %s' % exc)
    jobs, wait_problem, wait_code = wait_jobs(rest, before, opts['timeout'])
    # failure priority (the e2e family order): REST went away > not FINISHED
    # in time > client did not start > client exited non-zero > wait findings
    if wait_code in ('rest-unreachable', 'timeout'):
        confirmed, note, ljobs = settle_leftover(rest, jobs)
        if leftover is not None:
            leftover.update(confirmed=confirmed, note=note, jobs=ljobs)
        return fail('%s; %s' % (wait_problem, note))
    if submit_rc in (126, 127):
        return fail('the sql client did not start (exit %d — command missing or not '
                     'executable); see %s'
                     % (submit_rc, rec['submit_log']))
    if submit_rc != 0:
        return fail('the sql client started but exited %d — see %s'
                    % (submit_rc, rec['submit_log']))
    if wait_problem is not None:
        return fail(wait_problem)
    if not all(j['state'] == 'FINISHED' for j in jobs):
        bad = '; '.join('%s %s' % (j['jid'], j['state']) for j in jobs)
        return fail('the job(s) did not finish: %s' % bad)
    problem = evidence_write(os.path.join(ev, name + '.job.json'),
                             json.dumps({'scenarios': [name], 'jobs': jobs,
                                        'plan_gates_checked': True},
                                        ensure_ascii=False, indent=2) + '\n')
    if problem:
        return fail('the FINISHED record could not be written: %s — a scenario '
                    'whose evidence is not on disk cannot pass' % problem)

    # ---- plan gates, both sides: gluten nodes required on the GlutenFlink
    # side (a fallback fakes nothing), forbidden on the native side (the lib
    # alone must not decide which planner ran); unread plans fake nothing.
    counts, plan_errors = plan_gluten_nodes(rest, jobs)
    rec['gluten_nodes'] = sum(counts.values())
    if state['side'] == 'GlutenFlink':
        fallen = [jid for jid, n in counts.items() if n == 0]
        if fallen or plan_errors:
            notes = []
            if fallen:
                tm = tm_velox4j_lines(state['flink_home'])
                notes.append('no gluten nodes in the plan of %s (%s)' % (
                    ', '.join(fallen),
                    'velox4j lines ARE present in the TM logs — the cluster loaded '
                    'the GlutenFlink stack but this SQL fell back to native operators'
                    if tm else
                    'no velox4j lines in the TM logs either — the cluster is '
                    'probably not the GlutenFlink build'))
            if plan_errors:
                notes.append('the plan of %s could not be fetched: %s'
                             % (', '.join(sorted(plan_errors)),
                                plan_errors[sorted(plan_errors)[0]]))
            return fail('GlutenFlink side but %s' % '; '.join(notes))
    elif plan_errors:
        return fail('the plan of %s could not be fetched: %s — refusing to pass '
                    'on an unread plan' % (', '.join(sorted(plan_errors)),
                                            plan_errors[sorted(plan_errors)[0]]))
    else:
        rose = {jid: n for jid, n in counts.items() if n > 0}
        if rose:
            return fail('native side (from the lib) but the plan of %s carries %d '
                        'gluten node(s) — the cluster still runs the GlutenFlink '
                        'planner; removing the jars is not a stack switch, restart '
                        'on the native build first'
                        % (', '.join(sorted(rose)), sum(rose.values())))

    # ---- step 3: capture (snapshot diff; the flag may sit behind prefixes)
    rows, capture_errors, capture_warnings = capture_changelog(state['flink_home'], tm_snapshot)
    note = ('; capture may be incomplete: %s' % ' '.join(capture_warnings)
            if capture_warnings else '')
    if capture_errors:
        return fail('capture error: %s (the capture is truncated or corrupt — '
                    'not a pass)%s' % (capture_errors[0], note))
    problem = evidence_write(rec['capture'], ''.join(r + '\n' for r in rows))
    if problem:
        return fail(problem)
    final, stats, fold_errors = fold_changelog(rows)
    rec['rows_captured'] = sum(final.values())
    if not rows:
        return fail('empty capture: 0 changelog rows despite FINISHED — not a '
                    'pass (see %s)%s' % (rec['submit_log'], note))
    if fold_errors:
        return fail('fold error: %s (a retraction of a row never present — the '
                    'changelog does not reconstruct a result set)' % fold_errors[0])

    # ---- step 4: reference model as a subprocess (shares no code here)
    expected_rows, problem = run_reference(paths)
    if expected_rows is None:
        return fail('reference model broken: %s (this is a failure of the '
                     'reference implementation, not of the cluster side)' % problem)
    rec['rows_expected'] = len(expected_rows)
    expected = {}
    for row in expected_rows:
        payload = EXPECTED_ROW.match(row).group(1)
        expected[payload] = expected.get(payload, 0) + 1

    # ---- step 5: compare against the reference model
    if final == expected:
        rec['status'] = 'pass'
        rec['detail'] = ('%d row(s), exact multiset match with the reference '
                         'model (%d changelog line(s), flags %s)%s'
                         % (sum(final.values()), len(rows),
                            ', '.join('%s=%d' % kv for kv in sorted(stats.items())), note))
        return rec
    diffs, unpaired_missing, unpaired_extra = attribute(expected, final,
                                                         sink_columns(case_text))
    rec['column_diffs'] = diffs
    rec['unpaired_missing'] = unpaired_missing
    rec['unpaired_extra'] = unpaired_extra
    parts = ['result set mismatch: %d expected row(s), %d captured row(s)'
             % (rec['rows_expected'], rec['rows_captured'])]
    if unpaired_missing or unpaired_extra:
        parts.append('%d expected row(s) / %d captured row(s) could not be paired '
                     'by the first column' % (unpaired_missing, unpaired_extra))
    if note:
        parts.append(note.lstrip('; '))
    return fail('; '.join(parts))


# --- result assembly ----------------------------------------------------------

def command_line(opts, sub, yes=False):
    """The copy-paste command for the reviewed options."""
    parts = ['python3', shlex.quote(SCRIPT), sub] + (['--yes'] if yes else [])
    if opts['all']:
        parts.append('--all')
    elif opts['scenarios']:
        parts += ['--scenario ' + shlex.quote(s) for s in opts['scenarios']]
    for flag in ('flink_home', 'timeout'):
        if flag in opts['explicit']:
            parts += ['--%s' % flag.replace('_', '-'), shlex.quote(str(opts['explicit'][flag]))]
    return ' '.join(parts)


def base_result(command, opts, state, scenarios, success, next_step, error):
    return {KEY: {'success': success, 'checked_at': now(), 'command': command,
                  'side': state.get('side'), 'flink_home': state.get('flink_home'),
                  'rest': state.get('rest'), 'evidence_dir': opts['evidence_dir'],
                  'timeout': opts['timeout'], 'scenarios': scenarios,
                  'next_step': next_step, 'error_message': error}}


def scenario_record(name, status, detail):
    """A result record with the full stable key set."""
    return {'name': name, 'status': status, 'side': None,
            'rows_expected': None, 'rows_captured': None, 'column_diffs': [],
            'unpaired_missing': None, 'unpaired_extra': None,
            'capture': None, 'submit_log': None, 'gluten_nodes': None,
            'detail': detail}


def skipped_records(names, detail):
    return [scenario_record(n, 'skipped', detail) for n in names]


def next_step_for(opts, scenarios, side):
    failed = [s for s in scenarios if s['status'] == 'fail']
    if failed:
        return ('fix the failed scenario(s) %s — the column_diffs name the offending '
                'columns, the captures are in %s; then rerun %s'
                % (', '.join(s['name'] for s in failed), opts['evidence_dir'],
                   command_line(opts, 'run', yes=True)))
    return ('all %d scenario(s) pass on the %s side — the quick semantic check is '
            'green; rerun after the next stack or code change (this skill never '
            'switches the stack itself)'
            % (len(scenarios), side))


def build_plan(opts, names):
    """plan probes and prints; an incomplete scenario fails here by name —
    never silently vanishing from the list."""
    findings, state = probe(opts, names)
    scenarios, incomplete = [], []
    for name in names:
        problem = scenario_problem(name)
        if problem:
            scenarios.append(scenario_record(name, 'fail', '%s; %s' % problem))
            incomplete.append(name)
        else:
            scenarios.append(scenario_record(name, 'skipped', 'planned; plan mode '
                                             'submits nothing and writes nothing '
                                             '(--out aside)'))
    errors = [f['detail'] for f in findings]
    fixes = [f['fix'] for f in findings]
    if incomplete:
        errors.append('incomplete scenario(s): %s — case.sql/input/reference.py '
                      'are all required' % ', '.join(incomplete))
        fixes.append('restore the missing case.sql/input/reference.py of %s, then '
                     'rerun plan' % ', '.join(incomplete))
    error = 'refused: ' + '; '.join(errors) if errors else ''
    result = base_result('plan', opts, state, scenarios, not errors,
                         ('; '.join(fixes) if errors else
                          'review the plan above, then run %s'
                          % command_line(opts, 'run', yes=True)), error)
    preview = []
    if not findings:
        preview.append('side: %s (from %s/lib%s)'
                       % (state['side'], state['flink_home'],
                          ', GlutenFlink jars: ' + ', '.join(state['side_jars'])
                          if state['side_jars'] else ''))
        preview.append('rest: %s   submit client: %s'
                       % (state['rest'], state['sql_client']))
        preview.append('evidence refreshes in place under %s '
                      '(<scenario>.submit.sql/.submit.log/.job.json/.out)'
                      % opts['evidence_dir'])
        for name in names:
            if name in incomplete:
                continue
            paths = scenario_paths(name)
            preview.append('%s: input %s; submit %s -f %s/%s.submit.sql; '
                           'reference %s expected %s; capture %s/%s.out'
                           % (name, paths['input_dir'], state['sql_client'],
                              opts['evidence_dir'], name, sys.executable,
                              paths['input_dir'], opts['evidence_dir'], name))
    return result, 0 if not errors else 2, preview


def run_all(opts, yes, names):
    findings, state = probe(opts, names)
    if findings:
        error = 'refused: ' + '; '.join(f['detail'] for f in findings)
        scenarios = skipped_records(names, error)
        result = base_result('run', opts, state, scenarios, False,
                             '; '.join(f['fix'] for f in findings), error)
        return result, 2
    if not yes:
        note = 'run needs --yes (show the user the plan first; --yes is the record ' \
               'of their consent)'
        result = base_result('run', opts, state,
                             skipped_records(names, 'refused: ' + note), False,
                             'plan first, then add --yes', 'refused: ' + note)
        return result, 2
    scenarios = []
    leftover_block = None
    for name in names:
        if leftover_block:
            scenarios.append(scenario_record(name, 'skipped', leftover_block))
            continue
        holder = {}
        scenarios.append(run_scenario(name, opts, state, holder))
        leftover = holder or None
        if leftover and not leftover['confirmed']:
            jids = (', '.join(j['jid'] for j in leftover['jobs'])
                    if leftover['jobs'] else 'id unknown')
            leftover_block = ('skipped: the previous scenario %s left job %s running '
                             'and its cancellation could not be confirmed (%s) — a '
                             'submit now would mix two print streams into one '
                             'capture; find and stop the job on the WebUI, then rerun'
                             % (name, jids, leftover['note']))
    failed = [s for s in scenarios if s['status'] == 'fail']
    error = ('%d scenario(s) failed: %s' % (len(failed), ', '.join(s['name'] for s in failed))
             if failed else '')
    result = base_result('run', opts, state, scenarios, not failed,
                         next_step_for(opts, scenarios, state['side']), error)
    return result, 1 if failed else 0


def aborted(exc, command, opts, names):
    state = {'side': None, 'flink_home': opts['flink_home'], 'rest': None}
    return base_result(command, opts, state, [], False,
                       'fix the tool error first (see error_message), then rerun',
                       'tool aborted: %r' % exc)


# --- rendering ----------------------------------------------------------------

def render(result, preview=None):
    r = result[KEY]
    if r['error_message'].startswith('tool aborted'):
        return 'TOOL ERROR: %s\nnext: %s' % (r['error_message'], r['next_step'])
    lines = ['quick-verify %s  (single-side semantic check against the reference model)'
             % r['command'],
             '  flink %s  (side: %s)' % (r['flink_home'], r['side'])]
    if r.get('rest'):
        lines.append('  rest %s' % r['rest'])
    if r['error_message'].startswith('refused'):
        lines.append('  REFUSED  %s' % r['error_message'][len('refused: '):])
    for line in (preview or []):
        lines.append('  %s' % line)
    if r['command'] == 'plan' and not r['error_message'].startswith('refused'):
        lines.append('  PLAN: %d scenario(s) — nothing runs in plan mode' % len(r['scenarios']))
    if r['command'] == 'plan':
        # an incomplete scenario fails in plan too — never silently dropped
        lines += ['  FAIL     %-16s %s' % (s['name'], s['detail'])
                  for s in r['scenarios'] if s['status'] == 'fail']
    if r['command'] == 'run':
        for s in r['scenarios']:
            if s['status'] == 'pass':
                lines.append('  PASS     %-16s rows %d/%d  %s'
                             % (s['name'], s['rows_captured'], s['rows_expected'],
                                s['detail']))
            elif s['status'] == 'fail':
                lines.append('  FAIL     %-16s %s' % (s['name'], s['detail']))
                for d in s['column_diffs']:
                    sample = d['samples'][0] if d['samples'] else {}
                    lines.append('    column %-10s %d mismatch(es), e.g. expected=%s actual=%s'
                                 % (d['column'], d['mismatches'],
                                    sample.get('expected', '?'), sample.get('actual', '?')))
                if s.get('unpaired_missing') or s.get('unpaired_extra'):
                    lines.append('    unpaired rows: %d expected-only / %d captured-only'
                                 % (s['unpaired_missing'], s['unpaired_extra']))
            else:
                lines.append('  SKIPPED  %-16s %s' % (s['name'], s['detail']))
        total, passed = len(r['scenarios']), sum(1 for s in r['scenarios']
                                                 if s['status'] == 'pass')
        lines.append('QUICK VERIFY on the %s side: %d/%d scenario(s) PASS'
                     % (r['side'], passed, total))
        if not r['success']:
            lines.append('NOT GREEN: %s' % (r['error_message'] or 'see the failed scenario(s) above'))
    lines.append('next: %s' % r['next_step'])
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Single-side quick semantic verification: run Flink SQL '
                    'scenarios against the cluster currently up, capture the '
                    'print-sink changelog and compare it with the Python '
                    'reference models. plan by default; run needs --yes.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'run'))
    parser.add_argument('--scenario', action='append', metavar='NAME',
                        help='one scenario to run (repeatable); default: all')
    parser.add_argument('--all', action='store_true', help='run every scenario')
    parser.add_argument('--flink-home', help='the Flink installation '
                                             '(default: $FLINK_HOME, then .gfvbot/env.json)')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT,
                        help='seconds to wait for FINISHED per scenario (default %d)'
                             % DEFAULT_TIMEOUT)
    parser.add_argument('--yes', action='store_true',
                        help='run: the record that the user saw the plan and consented')
    parser.add_argument('--out', help='also write the JSON result to this file '
                                      '(inside the project; an existing file is kept '
                                      'unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true',
                        help='let --out replace an existing regular file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)

    available = scenario_dirs()
    if args.scenario and args.all:
        print('quick_verify.py: --scenario and --all are mutually exclusive', file=sys.stderr)
        return 2
    names = list(args.scenario or [])
    unknown = [n for n in names if n not in available]
    if unknown:
        print('quick_verify.py: unknown scenario(s): %s — available: %s'
              % (', '.join(unknown), ', '.join(available) or 'none'), file=sys.stderr)
        return 2
    if not names:
        names = list(available)

    opts = {'flink_home': find_flink_home(args.flink_home),
            'scenarios': [n for n in names if n in available] or names,
            'all': not args.scenario,
            'timeout': args.timeout,
            'evidence_dir': os.path.join(os.getcwd(), 'e2e', 'quick'),
            'explicit': {flag: getattr(args, flag) for flag in ('flink_home', 'timeout')
                         if getattr(args, flag) is not None}}

    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs
        if problem:
            print('quick_verify.py: %s' % problem, file=sys.stderr)
            return 2
        # an --out that WILL be one of the run's evidence files must be
        # refused up front: the scenario writes its capture first, so the
        # report would fail at the end (or --overwrite would destroy the
        # capture) — this covers files that do not exist yet.
        real_out = os.path.realpath(args.out)
        for name in names:
            for piece in ('.submit.sql', '.submit.log', '.job.json', '.out'):
                target = os.path.join(opts['evidence_dir'], name + piece)
                if os.path.realpath(target) == real_out:
                    print('quick_verify.py: --out collides with an evidence path of '
                          'scenario %s (%s) — the run writes its capture and submit '
                          'records there; choose another --out' % (name, target),
                          file=sys.stderr)
                    return 2

    preview = None
    try:
        if args.command == 'plan':
            result, code, preview = build_plan(opts, names)
        else:
            result, code = run_all(opts, args.yes, names)
    except KeyboardInterrupt as exc:
        result, code = aborted(exc, args.command, opts, names), 2
    except Exception as exc:
        result, code = aborted(exc, args.command, opts, names), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
          else render(result, preview))
    return code


if __name__ == '__main__':
    sys.exit(main())
