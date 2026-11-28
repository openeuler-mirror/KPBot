#!/usr/bin/env python3
"""nexmark_join.py — run the nexmark Join-family queries (q3/q7/q8/q13/q20/q23,
the fixed set — no --queries override: the join family is the skill's identity)
on the current Flink side at the fixed calibre shared byte for byte with
flink-velox-performance-verify (conf/nexmark-bench.yaml: 1e8 events / 5e7 tps /
blackhole / warmup), and produce the join-focused analysis report instead of a
bare metrics table: join_analysis[] annotates every query with its join type,
sides, key columns and predicate (references/join-table.md restates them for
review), plus the GlutenFlink join pushdown verdict — on the gluten side each
finished cell's plan is fetched over REST and judged by engine-generated fields
only (never the description, which embeds SQL text): no join-related gluten
node means the join fell back to native operators, marked FAILED in the human
report. summary[] averages comparable throughputs per join type (regular q3/q20,
window-join q7/q8, lookup q13, cascade q23 — the mean of per-query means).

The conf discipline: the fixed conf is swapped in as $NEXMARK_HOME/conf/
nexmark.yaml for the duration of a run, parallelism.default and
pipeline.object-reuse (false — the r0 calibre) are set per cell in the config
file Flink actually reads (flink-conf.yaml wins, else config.yaml), and the
originals are restored afterwards — also on failure or interrupt. A backup is
published atomically (same-directory temporary, then renamed in); a kernel
flock on the lock file beside each conf keeps a concurrent run off the same
pair (the fd is held to the verified end of the restore; a crashed holder's
flock evaporates with the process — nothing is ever unlinked); a restore only
counts when the conf reads back byte-identical, with the original's uid/gid
chowned back (copystat copies neither).

Evidence lands in <project>/e2e/perf/join/ (qNN.p<P>.out and *.submit.log,
refreshed in place). Every evidence path carries the --out rule, checked at the
cell start and again at each write, and every write opens the same resolved
realpath the guard judged — never the raw argument: realpath resolves symlinks
before '..' (kernel semantics; a lexical fold cannot see link/../x escaping),
so any symlink component refuses the write (even one pointing back inside the
project), the resolved path must stay inside the project directory, and a
non-regular file is never opened (a fifo would block the write). A cell whose
runner times out or is interrupted (SIGINT/SIGTERM) drops the runner's partial
output as its *.submit.log and has its cluster job cancelled best-effort over
REST, ownership first: the jid the runner's own output named, else the jobs
that appeared since the cell began (a job-set baseline taken at the cell
start), the bare time window only when no baseline could be taken and then
marked imprecise in the detail. Failed and not-run cells keep the full runs[]
key set. Rows off the calibre are marked not comparable — the interpretation
stays with the reader; the pushdown verdict is an observation, not a cell
failure.

REST and slot prechecks are best-effort, never gates (findings land in
warnings[]; --skip-check skips the probes). plan (default) probes and prints
the matrix and touches nothing; run --yes executes the cells and stops at the
first failing one (the confs are restored regardless). Exit codes: 0 done; 1 a
cell failed or the confs were not restored (confs_restored=false); 2 refused
(a blocked prerequisite, a bad --out, a live run lock — a refusal never
touched the confs, confs_restored is true there) or a tool abort
(confs_restored null — a crash cannot know; check by hand).
"""
import argparse
import contextlib
import datetime
import fcntl
import glob
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

KEY = 'flink_velox_nexmark_join_result'
SCRIPT = os.path.realpath(__file__)
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXED_CONF = os.path.join(SKILL_DIR, 'conf', 'nexmark-bench.yaml')

# The calibre this skill pins, shared byte for byte with flink-velox-performance-verify
# (conf/nexmark-bench.yaml is the single source for the runner — the same file).
CALIBRE_EVENTS = 100000000
CALIBRE_TPS = 50000000
# The fixed Join-family query set; there is no --queries override on purpose.
JOIN_QUERIES = ('q3', 'q7', 'q8', 'q13', 'q20', 'q23')

# Join semantics per query, hardcoded from the nexmark query definitions
# (references/join-table.md restates the table against the query SQL for
# reviewers). group is the summary bucket the query feeds.
JOIN_INFO = {
    'q3': {'join_type': 'regular join（双流）',
           'sides': 'auction ⋈ person',
           'key_columns': 'auction.seller = person.id',
           'predicate': 'auction.category = 10 AND person.state IN (OR, ID, CA)',
           'group': 'regular'},
    'q7': {'join_type': 'window join + 回联',
           'sides': 'tumble(bid, 10s) 最高价 ⋈ bid',
           'key_columns': 'bid.price = windowed max(price)',
           'predicate': 'bid.dateTime BETWEEN window_end - 10s AND window_end',
           'group': 'window-join'},
    'q8': {'join_type': 'window join（对齐窗）',
           'sides': 'tumble(person, 10s) ⋈ tumble(auction, 10s)',
           'key_columns': 'person.id = auction.seller',
           'predicate': '两侧 tumble 10s 窗口对齐（window_start / window_end 相等）',
           'group': 'window-join'},
    'q13': {'join_type': 'temporal/lookup join',
            'sides': 'bid ⋈ side_input（维表）',
            'key_columns': 'mod(bid.auction, 10000) = side_input.key',
            'predicate': 'FOR SYSTEM_TIME AS OF bid.p_time（proctime 时态联接）',
            'group': 'lookup'},
    'q20': {'join_type': 'inner join（宽投影）',
            'sides': 'bid ⋈ auction',
            'key_columns': 'bid.auction = auction.id',
            'predicate': 'auction.category = 10（过滤）',
            'group': 'regular'},
    'q23': {'join_type': '三流级联 join',
            'sides': 'bid ⋈ person ⋈ auction',
            'key_columns': 'person.id = bid.bidder AND auction.seller = bid.bidder',
            'predicate': '无过滤谓词（纯级联展开）',
            'group': 'cascade'},
}

# The per-join-type summary buckets, in report order: regular avg = mean of the
# q3 / q20 query means, window-join avg = q7 / q8, lookup = q13, cascade = q23.
SUMMARY_GROUPS = (('regular', ('q3', 'q20')),
                  ('window-join', ('q7', 'q8')),
                  ('lookup', ('q13',)),
                  ('cascade', ('q23',)))

CONF_FILES = ('flink-conf.yaml', 'config.yaml')  # Flink 1.19 reads flink-conf.yaml and only it
GLUTEN_JARS = ('gluten', 'velox')  # a jar whose name carries one of these means the GlutenFlink side
REST_KEYS = ('rest.address', 'rest.port')
BACKUP_SUFFIX = '.flink-velox-join.bak'
LOCK_SUFFIX = '.flink-velox-join.lock'
RESULTS_BANNER = 'Nexmark Results'
DEFAULT_PARALLELISM = (1, 4, 8, 16)
DEFAULT_TIMEOUT = 3600  # one cell: warmup plus 1e8 events; a stuck cell must not hang the matrix
PLAN_SKEW_MS = 2000  # the plan window: jobs started since the cell began, minus a little skew


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


# --- probing -----------------------------------------------------------------
def load_flink_conf(conf_path):
    """Flatten the one conf file Flink reads — config.yaml (nested maps) or
    flink-conf.yaml (flat dotted keys) — without a YAML dependency. Scalar leaves
    only. Flink 1.19 ships config.yaml nested ('rest:' / 'port: 8081'); the older
    flink-conf.yaml form is flat ('rest.port: 8081'); both flatten to dotted keys."""
    conf = {}
    if not conf_path or not os.path.isfile(conf_path):
        return conf
    stack = []  # [(indent, dotted prefix)]
    with open(conf_path, encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            line = re.sub(r'(^|\s)#.*$', r'\1', raw.rstrip()).rstrip()
            if not line or line in ('---', '...'):
                continue
            m = re.match(r'^(\s*)([A-Za-z0-9_.-]+):(?:\s+(.*))?$', line)
            if not m:
                continue
            indent, key, value = m.group(1), m.group(2), (m.group(3) or '').strip()
            while stack and stack[-1][0] >= len(indent):
                stack.pop()
            prefix = stack[-1][1] + '.' if stack else ''
            if value:
                conf[prefix + key] = value.strip('\'"')
            else:
                stack.append((len(indent), prefix + key))
    return conf


def read_conf_key(conf, key):
    """The last definition of a conf key, nested config.yaml and flat flink-conf.yaml
    both flattened to dotted keys by load_flink_conf."""
    return load_flink_conf(conf).get(key)


def effective_conf(flink_home):
    """The one config file Flink reads in this installation (flink-conf.yaml wins, no merge)."""
    for name in CONF_FILES:
        path = os.path.join(flink_home, 'conf', name)
        if os.path.isfile(path):
            return path
    return os.path.join(flink_home, 'conf', CONF_FILES[-1])


def foreign_jars(flink_home):
    """The lib jars that make the installation the GlutenFlink side (gluten / velox)."""
    jars = []
    for jar in sorted(glob.glob(os.path.join(flink_home, 'lib', '*.jar'))):
        name = os.path.basename(jar).lower()
        if any(word in name for word in GLUTEN_JARS):
            jars.append(os.path.basename(jar))
    return jars


def side_of(flink_home):
    """('gluten', jars) when the lib holds gluten/velox jars — the GlutenFlink side —
    else ('native', []): the side the matrix runs on is whatever is installed, this
    skill takes no side gate (the join analysis adapts to what it finds)."""
    foreign = foreign_jars(flink_home)
    return ('gluten' if foreign else 'native'), foreign


def slot_count(flink_home, conf):
    """(tms, per_tm, total): standalone-session workers times taskmanager.numberOfTaskSlots."""
    tms = 0
    workers = os.path.join(flink_home, 'conf', 'workers')
    if os.path.isfile(workers):
        try:
            with open(workers, encoding='utf-8', errors='replace') as fh:
                lines = {ln.split('#', 1)[0].strip() for ln in fh}
            tms = len([ln for ln in lines if ln])
        except OSError:
            tms = 0
    tms = max(tms, 1)
    per_tm = read_conf_key(conf, 'taskmanager.numberOfTaskSlots')
    try:
        per_tm = max(int(per_tm), 1)
    except (TypeError, ValueError):
        per_tm = 1
    return tms, per_tm, tms * per_tm


def rest_endpoint(conf):
    """The REST address and port Flink answers on, from the config file it actually reads."""
    address = read_conf_key(conf, REST_KEYS[0]) or 'localhost'
    port = read_conf_key(conf, REST_KEYS[1]) or '8081'
    try:
        port = int(port)
    except ValueError:
        pass
    return address, port


def rest_alive(address, port, timeout=3):
    """True when the cluster REST endpoint answers /overview."""
    conn = None
    try:
        conn = http.client.HTTPConnection(address, port, timeout=timeout)
        conn.request('GET', '/overview')
        resp = conn.getresponse()
        resp.read()
        return 200 <= resp.status < 300
    except OSError:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def rest_call(conn, method, path):
    """One request on an open connection: (status, the parsed JSON body or None)."""
    conn.request(method, path)
    resp = conn.getresponse()
    body = resp.read()
    try:
        return resp.status, json.loads(body.decode('utf-8', errors='replace'))
    except ValueError:
        return resp.status, None


def rest_job_ids(address, port, timeout=3):
    """The jids the cluster already knows (any state) — the baseline the cell's own
    submissions are diffed against, taken right before the runner starts — or
    None when REST could not answer (no baseline: imprecise fallback)."""
    conn = None
    try:
        conn = http.client.HTTPConnection(address, port, timeout=timeout)
        status, overview = rest_call(conn, 'GET', '/jobs/overview')
        if not 200 <= status < 300 or not isinstance(overview, dict):
            return None
        return {str(job.get('jid')) for job in overview.get('jobs') or []
                if isinstance(job, dict) and job.get('jid')}
    except (OSError, http.client.HTTPException):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def submitted_jid(text):
    """The jid the runner's own output names ('Job has been submitted with ID
    <jid>' — the Flink CLI line), or None: the precise ownership the cancel
    prefers over any window heuristic."""
    m = re.search(r'Job has been submitted with (?:ID|JobID) ([0-9a-f]{32})', text or '')
    return m.group(1) if m else None


def cancel_running_jobs(address, port, since_ms, before=None, jids=(), timeout=5):
    """Best-effort cancel of the jobs a dead cell left behind, ownership first so
    nobody else's job is touched: the jids the runner output named cancel
    exactly those ('submitted-jids'); else the jobs that appeared since the cell
    began — not in the before baseline, the /jobs/overview snapshot taken at the
    cell start — and are still RUNNING ('new-since-start'); only without a
    baseline the bare time window remains, marked imprecise ('time-window').
    Returns (cancelled, problem, how). REST trouble is reported, never fatal —
    the cell has already failed on the timeout or the interrupt."""
    how = 'time-window' if before is None else 'new-since-start'
    wanted = [jid for jid in jids if jid]
    if wanted:
        how = 'submitted-jids'
    conn = None
    cancelled, problem = [], None
    try:
        conn = http.client.HTTPConnection(address, port, timeout=timeout)
        if how == 'submitted-jids':
            overview = {}
            status, data = rest_call(conn, 'GET', '/jobs/overview')
            if 200 <= status < 300 and isinstance(data, dict):
                overview = {str(job.get('jid')): job for job in data.get('jobs') or []
                            if isinstance(job, dict) and job.get('jid')}
            for jid in wanted:
                job = overview.get(jid, {})
                if job.get('state') == 'FINISHED':
                    continue  # already gone: nothing to cancel, not an error
                status, _ = rest_call(conn, 'PATCH', '/jobs/%s/cancel' % jid)
                if 200 <= status < 300:
                    cancelled.append('%s (%s)' % (jid, job.get('name') or 'submitted by this cell'))
                else:
                    problem = problem or 'REST answered %s for %s' % (status, jid)
            return cancelled, problem, how
        status, overview = rest_call(conn, 'GET', '/jobs/overview')
        if not 200 <= status < 300 or not isinstance(overview, dict):
            return [], 'REST /jobs/overview answered %s' % status, how
        for job in overview.get('jobs') or []:
            if job.get('state') != 'RUNNING':
                continue
            try:
                if int(job.get('start-time')) < since_ms:
                    continue
            except (TypeError, ValueError):
                continue
            jid = str(job.get('jid') or '')
            if before is not None and jid in before:
                continue  # the cluster knew this job before the cell began: not this cell's
            status, _ = rest_call(conn, 'PATCH', '/jobs/%s/cancel' % jid)
            if 200 <= status < 300:
                cancelled.append('%s (%s)' % (jid, job.get('name') or '?'))
            else:
                problem = problem or 'REST answered %s for %s' % (status, jid)
    except (OSError, http.client.HTTPException) as exc:
        return cancelled, problem or 'REST %s:%s: %s' % (address, port, exc), how
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return cancelled, problem, how


def cancel_outcome_note(cancelled, problem, how):
    """The sentences a cancel outcome adds to a cell's detail: what was cancelled
    and how ownership was established, or why it failed — never silent."""
    if problem:
        return ('; the cluster job could not be cancelled over REST (%s) — it may '
                'still be running, cancel it by hand (the web UI or "flink cancel")' % problem)
    if cancelled:
        if how == 'submitted-jids':
            return ('; cancelled the job(s) this cell submitted over REST (jid parsed from '
                    'the runner output): %s' % ', '.join(cancelled))
        if how == 'new-since-start':
            return ('; cancelled the cluster job(s) that appeared since this cell began '
                    '(job-set baseline): %s' % ', '.join(cancelled))
        return ('; cancel by time window (imprecise — no job-set baseline could be taken, '
                'RUNNING jobs started since the cell began): %s' % ', '.join(cancelled))
    return '; no RUNNING cluster job found for this cell'


# --- the join pushdown verdict ------------------------------------------------
def gluten_mention(node):
    """True when a plan node's engine-generated fields identify it as a Gluten node:
    the operator name, operator_strategy, or an input's ship strategy / exchange
    mentions "gluten" (case-insensitive). Observed engine markers include the ship
    strategy "GlutenHASH" and operator names like "Gluten cal". The "description"
    field is deliberately excluded — it embeds user SQL text (table and column
    names), and a table named gluten_orders must not mark a node any more than a
    native plan with clean operators should be rejected."""
    if not isinstance(node, dict):
        return False
    texts = [str(node.get(k) or '')
             for k in ('operator', 'operator_strategy')]
    for inp in node.get('inputs') or []:
        if isinstance(inp, dict):
            texts += [str(inp.get(k) or '')
                      for k in ('ship_strategy', 'exchange')]
    return any('gluten' in t.lower() for t in texts)


def gluten_join_node(node):
    """A node that is both gluten (engine fields, per gluten_mention) and
    join-shaped: 'join' appears in the operator name or the operator strategy —
    engine fields only again, never the description. A plan whose only gluten
    nodes are calc/filter shapes means the join itself fell back to native
    operators: not accelerated, the report marks it FAILED."""
    if not gluten_mention(node):
        return False
    texts = [str(node.get(k) or '')
             for k in ('operator', 'operator_strategy')]
    return any('join' in t.lower() for t in texts)


def fetch_join_plan_evidence(address, port, since_ms, attributed, timeout=5):
    """Best-effort plan collection for one cell on the GlutenFlink side: every
    cluster job that started inside the cell's window (its start, minus a little
    skew) and has not already been attributed to an earlier cell (a job belongs
    to exactly one cell) gets its plan fetched and counted — gluten nodes and
    the join-related subset. Returns (entries, problem): entries as {'jid',
    'name', 'plan_fetched', 'gluten_nodes', 'join_gluten_nodes'} and why
    nothing could be collected when nothing was. REST trouble is never fatal —
    the cell has already succeeded or failed on its own."""
    conn = None
    entries, problem = [], None
    try:
        conn = http.client.HTTPConnection(address, port, timeout=timeout)
        status, overview = rest_call(conn, 'GET', '/jobs/overview')
        if not 200 <= status < 300 or not isinstance(overview, dict):
            return [], 'REST /jobs/overview answered %s' % status
        window = []
        for job in overview.get('jobs') or []:
            try:
                start = int(job.get('start-time'))
            except (TypeError, ValueError):
                continue
            if start < since_ms:
                continue
            jid = str(job.get('jid') or '')
            if not jid or jid in attributed:
                continue
            window.append((start, jid, str(job.get('name') or '?')))
        for _, jid, name in sorted(window):
            nodes, fetched = [], False
            status2, plan = rest_call(conn, 'GET', '/jobs/%s/plan' % jid)
            if 200 <= status2 < 300 and isinstance(plan, dict):
                inner = plan.get('plan')  # the REST API wraps the plan
                if isinstance(inner, dict):
                    plan = inner
                maybe = plan.get('nodes')
                if isinstance(maybe, list):
                    nodes, fetched = maybe, True
            if not fetched:
                problem = problem or ('the plan of %s could not be fetched '
                                      '(REST answered %s)' % (jid, status2))
            attributed.add(jid)
            entries.append({'jid': jid, 'name': name, 'plan_fetched': fetched,
                            'gluten_nodes': (sum(1 for n in nodes if gluten_mention(n))
                                             if fetched else None),
                            'join_gluten_nodes': (sum(1 for n in nodes if gluten_join_node(n))
                                                  if fetched else None)})
        if not entries:
            problem = 'no cluster job started since the cell began'
    except (OSError, http.client.HTTPException) as exc:
        return entries, problem or 'REST %s:%s: %s' % (address, port, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return entries, problem


def attach_plan_evidence(record, rest, since_ms, attributed, side):
    """Collect the plan evidence into a cell record — only on the GlutenFlink side
    (the native side has no gluten nodes by construction, the verdict there is
    not applicable and no REST round-trip is spent)."""
    if side != 'gluten':
        return
    entries, problem = fetch_join_plan_evidence(rest['address'], rest['port'], since_ms, attributed)
    fetched = [e for e in entries if e['plan_fetched']]
    record['plan_checked'] = bool(fetched)
    record['plan_jids'] = [e['jid'] for e in entries]
    record['gluten_nodes'] = sum(e['gluten_nodes'] for e in fetched) if fetched else None
    record['join_gluten_nodes'] = sum(e['join_gluten_nodes'] for e in fetched) if fetched else None
    record['plan_problem'] = problem if not fetched else None


def fixed_conf_text(address, port):
    """The fixed benchmark conf with the cluster's own REST endpoint in it."""
    with open(FIXED_CONF, encoding='utf-8') as fh:
        text = fh.read()
    text = re.sub(r'(?m)^flink\.rest\.address: .*$',
                  'flink.rest.address: %s' % address, text)
    text = re.sub(r'(?m)^flink\.rest\.port: .*$',
                  'flink.rest.port: %d' % port, text)
    return text


def strip_rest_lines(text):
    return re.sub(r'(?m)^flink\.rest\.(address|port): .*$\n?', '', text)


# --- planning ----------------------------------------------------------------
def parse_list(value, pattern, what):
    items, bad = [], []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue
        if re.match(pattern, part):
            items.append(part)
        else:
            bad.append(part)
    if bad or not items:
        raise ValueError('%s must be a non-empty comma-separated list, got: %s' % (what, value))
    return items


def command_line(opts, sub, yes=False):
    """The command for the options that were actually reviewed, ready to copy: interpreter and
    the path of this script included (it is neither on PATH nor in the working directory).
    Root is never suggested: who may write the confs is the user's to arrange (the installing
    owner, or an admin adjusting owner/permissions), and a privileged rerun would need its own
    plan -> consent round anyway — no --yes is ever pre-filled for another identity."""
    parts = ['python3', shlex.quote(SCRIPT), sub] + (['--yes'] if yes else [])
    for flag in ('flink-home', 'nexmark-home', 'parallelism', 'timeout'):
        key = flag.replace('-', '_')
        if key in opts['explicit']:
            parts += ['--%s' % flag, shlex.quote(str(opts['explicit'][key]))]
    if opts['skip_check']:
        parts.append('--skip-check')
    return ' '.join(parts)


def blocked(reason, fix):
    return {'status': 'blocked', 'detail': reason, 'fix': fix}


def warn(reason, fix):
    """A best-effort finding: REST and slot prechecks never gate a join matrix —
    they land in warnings[] and the cells speak for themselves."""
    return {'status': 'warn', 'detail': reason, 'fix': fix}


def probe(opts):
    """Everything the matrix needs: blocked findings refuse, warn findings never do
    (the join calibre: checks best-effort, no hard REST / slot gate — REST jitter
    must not mis-block a join run). --skip-check skips the two probes entirely."""
    findings, warnings = [], []
    flink_home, nexmark_home = opts['flink_home'], opts['nexmark_home']
    side, foreign = side_of(flink_home)
    state = {'side': side, 'gluten_jars': foreign, 'flink_home': flink_home,
             'nexmark_home': nexmark_home, 'effective_conf': None, 'rest': None,
             'slots': None}

    if not os.path.isdir(flink_home):
        findings.append(blocked('Flink home not found: %s' % flink_home,
                                'point --flink-home at the Flink installation (or set FLINK_HOME)'))
        return findings, warnings, state
    if not os.path.isfile(os.path.join(flink_home, 'bin', 'flink')):
        findings.append(blocked('bin/flink is missing under %s' % flink_home,
                                'is this a Flink installation?'))

    conf = effective_conf(flink_home)
    state['effective_conf'] = conf
    if not os.path.isfile(conf):
        findings.append(blocked('no config file under %s/conf (flink-conf.yaml / config.yaml)' % flink_home,
                                'install Flink or point --flink-home elsewhere'))
    conf_link = link_problem(conf)  # a conf and its backup path must both be regular files
    if conf_link:
        findings.append(blocked(*conf_link))

    if os.path.isfile(conf):
        if not os.access(conf, os.W_OK):
            findings.append(blocked('cannot write %s' % conf,
                                    'run as a user who can write it (e.g. the installing owner), '
                                    'or have an admin adjust the conf owner/permissions, then rerun'))
        address, port = rest_endpoint(conf)
        state['rest'] = {'address': address, 'port': port}
        tms, per_tm, total = slot_count(flink_home, conf)
        state['slots'] = {'tms': tms, 'per_tm': per_tm, 'total': total}
        if not opts['skip_check']:
            if total < max(opts['parallelism_int']):
                warnings.append(warn(
                    'cluster has %d slot(s) (%d workers x %d), less than the largest requested '
                    'parallelism %d' % (total, tms, per_tm, max(opts['parallelism_int'])),
                    'cells above the slot count will fail or starve; lower --parallelism, or raise '
                    'taskmanager.numberOfTaskSlots and restart the cluster first'))
            if not rest_alive(address, port):
                warnings.append(warn(
                    'the cluster REST endpoint %s:%s does not answer' % (address, port),
                    'start the Flink cluster first — the cells will fail without it, and the '
                    'join pushdown verdict needs REST for the plans'))

    runner = os.path.join(nexmark_home, 'bin', 'run_query.sh')
    if not os.path.isfile(runner):
        findings.append(blocked('no nexmark runner under %s/bin' % nexmark_home,
                                'install the runnable nexmark tree (flink-velox-runtime-install), '
                                'or point --nexmark-home at it'))
    else:
        nexmark_conf = os.path.join(nexmark_home, 'conf', 'nexmark.yaml')
        nexmark_link = link_problem(nexmark_conf)
        if not os.path.isfile(nexmark_conf):
            findings.append(blocked('no conf/nexmark.yaml under %s' % nexmark_home,
                                    'complete the nexmark tree first'))
        elif nexmark_link:
            findings.append(blocked(*nexmark_link))
        elif not os.access(nexmark_conf, os.W_OK):
            findings.append(blocked('cannot write %s' % nexmark_conf,
                                    'run as a user who can write it (e.g. the installing owner), '
                                    'or have an admin adjust the conf owner/permissions, then rerun'))
        elif leftover_state(nexmark_conf) == 'gone':
            findings.append(blocked(
                '%s holds a benchmark conf and the original backup is gone '
                '(a previous run crashed without restoring)' % nexmark_conf,
                'restore conf/nexmark.yaml by hand, then rerun plan'))
    return findings, warnings, state


def cells(opts):
    """The matrix in execution order: every fixed join query at each parallelism."""
    return [(query, par) for query in JOIN_QUERIES for par in opts['parallelism_int']]


def plan(opts):
    findings, warnings, state = probe(opts)
    matrix = [] if findings else cells(opts)
    preview = []
    if matrix:
        rest = state['rest']
        preview = ['queries: %s (fixed join family — no --queries override)'
                   % ', '.join(JOIN_QUERIES),
                   'join types: regular (q3, q20) / window-join (q7, q8) / lookup (q13) / cascade (q23)',
                   'side: %s%s' % (state['side'],
                                   ' — the join pushdown verdict is judged from the plans'
                                   if state['side'] == 'gluten' else
                                   ' — the pushdown verdict does not apply (the baseline side)'),
                   '%s: parallelism.default and pipeline.object-reuse (false) set per cell, '
                   'restored afterwards' % os.path.basename(state['effective_conf']),
                   'conf/nexmark.yaml: swapped in as a whole by this skill\'s fixed conf '
                   '(1e8 events, 5e7 tps, REST %s:%s), restored afterwards'
                   % (rest['address'], rest['port']),
                   'evidence: %s/qNN.p<P>.out and *.submit.log' % opts['evidence_dir']]
    return findings, warnings, matrix, preview, state


# --- conf management ---------------------------------------------------------
def backup_path(path):
    return path + BACKUP_SUFFIX


def lock_path(path):
    return path + LOCK_SUFFIX


def take_lock(path, changes):
    """The run lock beside a conf for the whole leftover-repair + swap + restore
    window: a kernel flock on the lock file (LOCK_EX|LOCK_NB, the fd held until
    the restore is verified). Kernel arbitration means no check-then-unlink
    race: a crashed holder's flock evaporates with the process and the next run
    truncates and rewrites the file (the pid text is diagnostics, never the
    lock; the file itself is never unlinked). A lock that is a symlink is
    refused. plan never locks."""
    lock = lock_path(path)
    if os.path.islink(lock):
        return None, ('%s is a symbolic link; the run lock is never taken through a link' % lock,
                      'check the link by hand and remove it, then rerun')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        return None, ('cannot open the run lock %s: %s' % (lock, exc.strerror or exc),
                      'check the lock path, then rerun')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None, ('another run holds the lock: %s' % lock,
                      'wait for it to finish and restore the confs, then rerun')
    try:
        os.ftruncate(fd, 0)
        os.write(fd, ('%d %s\n' % (os.getpid(), now())).encode('utf-8'))
    except OSError:
        pass  # the holder text is diagnostics; the flock itself is the lock
    changes.append({'action': 'lock taken',
                    'detail': '%s (pid %d, kernel flock held until the restore is verified)' % (lock, os.getpid())})
    return fd, None


@contextlib.contextmanager
def _no_interrupts():
    """A critical section for undoing or restoring the confs: SIGINT/SIGTERM are
    held off until it completes, so a second interrupt cannot land between the two
    files and turn a true 'restored' into a guess. A signal that arrives here is
    dropped in favour of finishing the restore."""
    held = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            held[sig] = signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    try:
        yield
    finally:
        for sig, handler in held.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass


def link_problem(path):
    """Why nothing may be swapped at this conf path, or None. A conf that is a symbolic link
    is never written through: the swap would edit the link's target and the restore would
    replace the link itself, leaving the target modified and the link destroyed. A backup
    path that is a link is never renamed into place either: the conf path would become a
    link to an arbitrary file. Such states are refused for the user to sort out by hand."""
    if os.path.islink(path):
        return ('%s is a symbolic link; this skill swaps and restores the conf itself, '
                'never through a link' % path,
                'use a regular file, or have the conf owner swap it by hand, then rerun plan')
    bak = backup_path(path)
    if os.path.islink(bak):
        return ('%s is a symbolic link; it is never renamed into place' % bak,
                'check the link by hand and remove it, then rerun plan')
    return None


def leftover_state(path):
    """The state a conf is in after a crashed run: 'gone' (the file holds our conf and
    the backup is lost — not recoverable, refuse), 'backup' (the original is in the
    .bak, put back on the next run) or None."""
    bak = backup_path(path)
    if os.path.isfile(bak):
        return 'backup'
    if os.path.isfile(path) and file_matches_skill_conf(path):
        return 'gone'
    return None


def repair_leftover(path):
    """Put the original back when a previous run left its backup behind. Probing never
    calls this: plan touches nothing, the repair happens in run before anything else.
    A backup that is a symbolic link is never renamed — run_all refuses such a state
    before the repair loop; this guard keeps the rename safe on its own."""
    bak = backup_path(path)
    if os.path.isfile(bak) and not os.path.islink(bak):
        os.replace(bak, path)
        return True
    return False


def file_matches_skill_conf(path):
    """True when the file already holds this skill's benchmark conf (modulo the REST endpoint)."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except OSError:
        return False
    return strip_rest_lines(text) == strip_rest_lines(fixed_conf_text('X', 0))


def backup_file(path, bak):
    """Copy a conf to its backup, atomically: the copy is completed in a
    same-directory temporary (<bak>.tmp-<pid>), flushed to disk and only then renamed
    into place — an interrupt or a full disk mid-copy can never leave a half backup
    behind that the next run would restore over the good conf. A backup that
    somehow already exists (a directory included) is still an error and not a
    silent replacement (shutil.copy2 would happily copy into a directory there and
    the restore would then quietly skip — the user's conf would be the one lost)."""
    if os.path.lexists(bak):
        raise FileExistsError('backup already exists: %s' % bak)
    tmp = '%s.tmp-%d' % (bak, os.getpid())
    try:
        with open(path, 'rb') as src, open(tmp, 'xb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, bak)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    shutil.copystat(path, bak)


def set_conf_key_lines(lines, key, value):
    """Set one conf key in the file's own form and return the new lines: a flat
    'a.b: v' line is replaced in place; a nested 'a:' map with a 'b:' leaf is edited
    at the leaf (same form the file already uses, no duplicate key); a key that is
    nowhere in the file is appended as one flat dotted line (the install family's
    pin-jdk precedent, verified on a live cluster)."""
    flat = re.compile(r'^%s:(\s.*)?$' % re.escape(key))
    for i, raw in enumerate(lines):
        if flat.match(raw.rstrip('\n')):
            lines[i] = '%s: %s\n' % (key, value)
            return lines
    stack = []  # [(indent, dotted prefix)] — the same walk load_flink_conf does
    for i, raw in enumerate(lines):
        line = re.sub(r'(^|\s)#.*$', r'\1', raw.rstrip('\n')).rstrip()
        if not line or line in ('---', '...'):
            continue
        m = re.match(r'^(\s*)([A-Za-z0-9_.-]+):(?:\s+(.*))?$', line)
        if not m:
            continue
        indent, name, val = m.group(1), m.group(2), (m.group(3) or '').strip()
        while stack and stack[-1][0] >= len(indent):
            stack.pop()
        prefix = stack[-1][1] + '.' if stack else ''
        if val:
            if prefix + name == key:
                lines[i] = '%s%s: %s\n' % (indent, name, value)
                return lines
        else:
            stack.append((len(indent), prefix + name))
    lines.append('%s: %s\n' % (key, value))
    return lines


class ConfSwap:
    """Swap the two confs for the duration of a run and restore them afterwards,
    whatever happens. Leftovers of a previous crashed run are repaired first by
    run_all (under the run lock, before entering). A restore only counts when the
    conf reads back byte-identical to its backup; anything else lands in
    not_restored and the receipt says so."""

    def __init__(self, flink_conf, nexmark_conf, address, port, changes):
        self.flink_conf, self.nexmark_conf = flink_conf, nexmark_conf
        self.text = fixed_conf_text(address, port)
        self.changes = changes
        self.entered = False
        self.not_restored = []
        self.owners = {}  # path -> (uid, gid) the original file carried at backup time

    def __enter__(self):
        backed_up = []
        try:
            for path in (self.flink_conf, self.nexmark_conf):
                info = os.stat(path)  # the owner the restore chowns back to:
                self.owners[path] = (info.st_uid, info.st_gid)  # copystat copies neither
                backup_file(path, backup_path(path))
                backed_up.append(path)
                self.changes.append({'action': 'backed up',
                                     'detail': '%s -> %s' % (path, backup_path(path))})
            with open(self.nexmark_conf, 'w', encoding='utf-8') as fh:
                fh.write(self.text)
        except BaseException:  # a crash half-way in: undo the backups already taken
            # (nothing has been swapped yet), the undo itself uninterruptible
            with _no_interrupts():
                for path in backed_up:
                    self._put_back(path, record=False)
            raise
        self.changes.append({'action': 'swapped in',
                             'detail': 'the fixed benchmark conf -> %s' % self.nexmark_conf})
        self.entered = True
        return self

    def set_flink(self, parallelism):
        self._set_key('parallelism.default', str(parallelism))
        self._set_key('pipeline.object-reuse', 'false')  # the r0 calibre, comparable
                                                          # across this skill and
                                                          # flink-velox-performance-verify
    def _set_key(self, key, value):
        with open(self.flink_conf, encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        lines = set_conf_key_lines(lines, key, value)
        with open(self.flink_conf, 'w', encoding='utf-8') as fh:
            fh.writelines(lines)

    def _put_back(self, path, record=True):
        """One restore step, verified: read the backup, move it into place, read the
        conf back — only a byte-identical conf counts as restored. Then the
        original's uid/gid are chowned back (copystat copies neither owner nor
        group; an admin-run would otherwise leave the conf admin-owned and the
        service account could lose access) — a chown failure never blocks the
        restore, the outcome lands in changes. A missing backup (consumed by
        something else) or a failed verify leaves the conf swapped and named in
        not_restored."""
        bak = backup_path(path)
        if not os.path.isfile(bak):
            self.not_restored.append(path)
            return
        try:
            with open(bak, 'rb') as fh:
                original = fh.read()
            os.replace(bak, path)
            with open(path, 'rb') as fh:
                back = fh.read()
        except OSError:
            self.not_restored.append(path)
            return
        if back != original:
            self.not_restored.append(path)
            return
        if record:
            self.changes.append({'action': 'restored', 'detail': '%s -> %s' % (bak, path)})
        owner = self.owners.get(path)
        if owner:
            try:
                os.chown(path, owner[0], owner[1])
                self.changes.append({'action': 'owner restored',
                                     'detail': '%s (uid %d, gid %d — copystat copies no owner)'
                                     % (path, owner[0], owner[1])})
            except OSError as exc:
                self.changes.append({'action': 'owner NOT restored',
                                     'detail': '%s: %s — the service account may lose access to '
                                     'the conf; chown it back by hand' % (path, exc.strerror or exc)})

    def __exit__(self, *exc):
        if not self.entered:
            return False
        with _no_interrupts():  # the restore, once started, runs to the end
            for path in (self.flink_conf, self.nexmark_conf):
                self._put_back(path)
        return False


# --- running one cell --------------------------------------------------------
def parse_number(text):
    """A table cell as a number: '100,000,000' -> 100000000, '595.598' -> 595.598,
    '167.9 K/s' -> 167900.0, '9.22 E/s' -> 9.22e18; None when the cell is not a number
    (the runner prints human units, and a per-cores cell can be bogus)."""
    m = re.match(r'^([\d,]+(?:\.\d+)?)\s*([KMGTPE]?)(?:/s)?$', text.strip())
    if not m:
        return None
    try:
        value = float(m.group(1).replace(',', ''))
    except ValueError:
        return None
    factor = {'K': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12, 'P': 1e15, 'E': 1e18}.get(m.group(2), 1)
    return value * factor


def parse_results_table(log_text):
    """The rows of the "Nexmark Results" summary: query, events, cores, time(s), throughput.
    The runner prints comma-grouped integers and unit-bearing cells ('167.9 K/s'); a 'Total'
    row and cells that carry no number (cores=0 makes per-cores nonsense) are kept as None."""
    rows, inside = [], False
    for line in log_text.splitlines():
        if RESULTS_BANNER in line:
            inside = True
            continue
        if inside and line.lstrip().startswith('|'):
            fields = [c.strip() for c in line.strip().strip('|').split('|')]
            if len(fields) >= 6 and fields[0] not in ('Query', 'Total'):
                events = parse_number(fields[1])
                cores = parse_number(fields[2])
                time_s = parse_number(fields[3])
                throughput = parse_number(fields[5])
                if events is None or time_s is None:
                    continue  # a row without the two numbers that make the comparison
                rows.append({'query': fields[0], 'events_num': int(events), 'cores': cores,
                             'time_s': time_s, 'throughput': throughput,
                             'throughput_per_cores': parse_number(fields[6]) if len(fields) > 6 else None})
    return rows


def evidence_paths(evidence_dir, query, par):
    tag = '%s.p%d' % (query, par)
    return (os.path.join(evidence_dir, tag + '.out'),
            os.path.join(evidence_dir, tag + '.submit.log'))


EVIDENCE_SCOPE = ('the evidence path must be a regular path inside the project directory '
                  '(the working directory), not a symlink or a path outside it')


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


def evidence_problem(path):
    """Why an evidence file may not be written, or None — the --out rule, per file
    at every check (cell start and each write): the path is judged and written as
    the same resolved realpath (kernel semantics — symlinks resolve before '..',
    a lexical fold cannot see link/../x escaping), so any symlink component
    refuses the write (even one pointing back inside the project) and the
    resolved path must stay inside the project directory; a non-regular file
    is never opened (a fifo would block the write)."""
    real, problem = guarded_real(path, EVIDENCE_SCOPE)
    if problem:
        return problem
    if os.path.lexists(real):
        if not stat.S_ISREG(os.lstat(real).st_mode):
            return '%s exists and is not a regular file (a fifo would block the write)' % path
    return None


def run_cell(record, opts, swap, rest, attributed):
    """Submit one cell to the cluster and capture its table. Returns True when the cell is done."""
    query, par = record['query'], record['parallelism']
    side = opts['side']
    table_path, log_path = evidence_paths(opts['evidence_dir'], query, par)
    record.update(table=table_path, submit_log=log_path)
    problems = [p for p in (evidence_problem(table_path), evidence_problem(log_path)) if p]
    if problems:  # never write through a link, never open a fifo: the cell fails before the runner
        record.update(status='failed',
                      detail='; '.join(problems)
                      + ' — replace it with a regular file (or remove it), then rerun')
        return False
    swap.set_flink(par)
    record['flink'] = {'parallelism.default': par, 'pipeline.object-reuse': False}
    runner = os.path.join(opts['nexmark_home'], 'bin', 'run_query.sh')
    # the job-set baseline: whatever the cluster already knows when this cell begins.
    # The cell's own submissions are the jobs that appear on top of it — the cancel
    # after a timeout or an interrupt touches those and only those (ownership);
    # a REST that cannot answer it leaves the cancel the imprecise time window.
    before = rest_job_ids(rest['address'], rest['port'])
    started = time.monotonic()
    started_wall = time.time()
    try:
        proc = subprocess.run([runner, 'oa', query], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=opts['timeout'], env=dict(os.environ, FLINK_HOME=opts['flink_home']))
    except subprocess.TimeoutExpired as exc:
        # the runner is dead but its output so far is evidence too: drop it as this cell's
        # submit.log (partial is better than nothing) and point the detail at what landed;
        # the job it submitted may live on — cancel what belongs to this cell (the jid its
        # own output named, else the jobs that appeared since it began), and say how
        # that went either way
        detail = 'the runner did not finish within %ss' % opts['timeout']
        partial_problem = evidence_problem(log_path)  # the log is the only evidence written here
        if exc.output and not partial_problem:
            # written as the resolved path the guard just judged — never the raw
            # argument (its '..'s and links are already folded away)
            real_log = os.path.realpath(log_path)
            os.makedirs(os.path.dirname(real_log), exist_ok=True)
            with open(real_log, 'wb') as fh:
                fh.write(exc.output)
            detail = '%s — see %s' % (detail, log_path)
        elif exc.output:
            detail = ('%s (the partial runner output was not written: %s)'
                      % (detail, partial_problem))
        else:
            detail = '%s (no runner output captured before the timeout)' % detail
        cancelled, problem, how = cancel_running_jobs(
            rest['address'], rest['port'], int(started_wall * 1000) - PLAN_SKEW_MS,
            before=before, jids=(submitted_jid((exc.output or b'').decode('utf-8', errors='replace')),))
        detail = detail + cancel_outcome_note(cancelled, problem, how)
        # the job's plan is still fetchable after the cancel — the pushdown verdict
        # for this cell counts even though the throughput does not
        attach_plan_evidence(record, rest, int(started_wall * 1000) - PLAN_SKEW_MS, attributed, side)
        record.update(status='failed', duration_s=round(time.monotonic() - started, 1),
                      detail=detail)
        return False
    except KeyboardInterrupt:  # SIGINT / SIGTERM: this cell is interrupted, the confs still restore
        # symmetric with the timeout path: the runner's cluster job may live on past
        # the killed runner — cancel what belongs to this cell and record the outcome
        # (the runner was killed, its output is gone; ownership comes from the baseline)
        cancelled, problem, how = cancel_running_jobs(
            rest['address'], rest['port'], int(started_wall * 1000) - PLAN_SKEW_MS, before=before)
        record.update(status='failed', duration_s=round(time.monotonic() - started, 1),
                      detail='interrupted (SIGINT / SIGTERM)' + cancel_outcome_note(cancelled, problem, how))
        raise
    except OSError as exc:
        record.update(status='failed', duration_s=round(time.monotonic() - started, 1),
                      detail='the runner did not start: %s' % (exc.strerror or exc))
        return False
    record.update(duration_s=round(time.monotonic() - started, 1), runner_exit=proc.returncode)
    attach_plan_evidence(record, rest, int(started_wall * 1000) - PLAN_SKEW_MS, attributed, side)
    problems = [p for p in (evidence_problem(table_path), evidence_problem(log_path)) if p]
    if problems:  # the paths were fine at the cell start: re-checked at the write —
        # a path swapped to a link / fifo mid-cell fails here instead of being written through
        record.update(status='failed',
                      detail='; '.join(problems)
                      + ' — the evidence path changed under the cell; replace it with a '
                        'regular file (or remove it), then rerun')
        return False
    # written as the resolved paths the guard just judged — never the raw
    # arguments (their '..'s and links are already folded away)
    real_log = os.path.realpath(log_path)
    real_table = os.path.realpath(table_path)
    os.makedirs(os.path.dirname(real_log), exist_ok=True)
    with open(real_log, 'wb') as fh:
        fh.write(proc.stdout)
    text = proc.stdout.decode('utf-8', errors='replace')
    rows = parse_results_table(text)
    with open(real_table, 'w', encoding='utf-8') as fh:
        kept = [ln for ln in text.splitlines()
                if RESULTS_BANNER in ln or (rows and ln.lstrip().startswith(('+', '|')))]
        fh.write('\n'.join(kept) + '\n')
    if proc.returncode != 0:
        record.update(status='failed', detail='the runner exited %d — see %s' % (proc.returncode, log_path))
        return False
    if not rows:
        record.update(status='failed',
                      detail='the runner succeeded but printed no "%s" table (the job most likely '
                             'never reached FINISHED) — see %s' % (RESULTS_BANNER, log_path))
        return False
    mine = [r for r in rows if r['query'] == query]
    if not mine:
        record.update(status='failed',
                      detail='the results table holds no row for %s — see %s' % (query, log_path))
        return False
    row = mine[0]
    record['metrics'] = row
    missing = []
    if row['events_num'] != CALIBRE_EVENTS:
        missing.append('events differ from the calibre')
    if not row['time_s'] or row['time_s'] <= 0:
        missing.append('time is not a positive number')
    if row['throughput'] is None:
        missing.append('throughput missing from the table')
    record['comparable'] = not missing  # a row off the calibre or without its key
    record['status'] = 'done'           # metrics is not a comparable measurement
    record['detail'] = ('%d events in %ss, %s events/s'
                        % (row['events_num'], row['time_s'], row['throughput'])
                        + ('' if record['comparable']
                           else ' — %s, not comparable' % ', '.join(missing)))
    return True


# --- the join analysis -------------------------------------------------------
def build_join_analysis(runs, side):
    """join_analysis[]: one entry per fixed query. The static semantics (join_type /
    sides / key_columns / predicate) come from the nexmark query definitions; the
    throughput is the mean over the query's comparable cells; the pushdown verdict
    is judged from the plans collected on the GlutenFlink side — any join-related
    gluten node means pushed down, none among checked plans means fell back (the
    FAILED mark of the human report), no plans collected means unknown, and the
    native side does not apply by construction."""
    analysis = []
    for query in JOIN_QUERIES:
        info = JOIN_INFO[query]
        cells = [r for r in runs if r['query'] == query]
        done = [r for r in cells if r['status'] == 'done']
        comparable = [r for r in done
                      if r.get('comparable') and (r.get('metrics') or {}).get('throughput') is not None]
        values = [r['metrics']['throughput'] for r in comparable]
        throughput_avg = sum(values) / len(values) if values else None
        checked = [r for r in cells if r.get('plan_checked')]
        joiny = [r for r in checked if (r.get('join_gluten_nodes') or 0) > 0]
        attempted = any(r['status'] != 'pending' for r in cells)
        if side == 'native':
            pushdown, detail = 'not_applicable', ('native side: gluten pushdown does not apply '
                                                  '(the baseline run)')
        elif not attempted:
            pushdown, detail = 'not_run', ('not run yet' if not runs else
                                           'the matrix stopped before this query')
        elif joiny:
            pushdown, detail = 'yes', ('join-related gluten nodes in the plan of %d of %d '
                                       'checked cell(s)' % (len(joiny), len(checked)))
        elif checked:
            pushdown, detail = 'no', ('no join-related gluten node in the plan — the join fell '
                                      'back to native operators (not accelerated)')
        else:
            problems = [r.get('plan_problem') for r in cells if r.get('plan_problem')]
            pushdown, detail = 'unknown', (('the plan could not be fetched over REST (%s)'
                                            % problems[0]) if problems else
                                           'no plan was checked for this query')
        analysis.append({'query': query, 'join_type': info['join_type'], 'sides': info['sides'],
                         'key_columns': info['key_columns'], 'predicate': info['predicate'],
                         'group': info['group'], 'throughput_avg': throughput_avg,
                         'cells_done': len(done), 'cells_comparable': len(comparable),
                         'pushdown': pushdown, 'pushdown_detail': detail})
    return analysis


def build_summary(analysis):
    """summary[]: per join type, the mean of the per-query means (regular = q3 / q20,
    window-join = q7 / q8, lookup = q13, cascade = q23) — queries without a
    comparable cell contribute nothing; a group without any stays None."""
    by_query = {a['query']: a for a in analysis}
    out = []
    for group, queries in SUMMARY_GROUPS:
        values = [by_query[q]['throughput_avg']
                  for q in queries if by_query[q]['throughput_avg'] is not None]
        out.append({'group': group, 'queries': list(queries),
                    'avg': (sum(values) / len(values)) if values else None,
                    'cell_count': sum(by_query[q]['cells_comparable'] for q in queries)})
    return out


def render_throughput(value):
    """A throughput in K/s for the human report ('n/a' when there is none)."""
    return 'n/a' if value is None else '%.1f K/s' % (value / 1000.0)


# --- assembly ----------------------------------------------------------------
def base_result(command, opts, state, runs, analysis, summary, changes, warnings,
                success, error, next_step, confs_restored=None):
    r = {'success': success, 'checked_at': now(), 'command': command,
         'side': state.get('side'), 'gluten_jars': state.get('gluten_jars'),
         'flink_home': opts['flink_home'], 'nexmark_home': opts['nexmark_home'],
         'effective_conf': state.get('effective_conf'), 'rest': state.get('rest'),
         'slots': state.get('slots'), 'calibre': {'events': CALIBRE_EVENTS, 'tps': CALIBRE_TPS},
         'queries': list(JOIN_QUERIES), 'parallelism': opts['parallelism_int'],
         'timeout': opts['timeout'], 'skip_check': opts['skip_check'],
         'evidence_dir': opts['evidence_dir'], 'runs': runs, 'join_analysis': analysis,
         'summary': summary, 'changes': changes, 'warnings': warnings,
         'next_step': next_step, 'error_message': error}
    if confs_restored is not None:
        r['confs_restored'] = confs_restored
    return {KEY: r}


def refused(command, opts, state, findings, warnings):
    """The refusal shape: nothing was touched, so the confs on disk are the
    originals — confs_restored is true (present, not missing: a run-command
    consumer may read it on every branch)."""
    error = 'refused: ' + '; '.join(f['detail'] for f in findings)
    return base_result(command, opts, state, [], [], [], [], warnings, False, error,
                       '; '.join(f['fix'] for f in findings) or 'fix the findings above, then rerun plan',
                       confs_restored=True)


def next_step_for(command, findings, matrix, opts, runs=None):
    if findings:
        return '; '.join(f['fix'] for f in findings)
    run_cmd = command_line(opts, 'run', yes=True)
    if command == 'plan':
        return 'review the plan above, then run %s' % run_cmd
    failed = [r for r in (runs or []) if r['status'] == 'failed']
    if failed:
        return 'fix the failed cell above (%s), then rerun %s — the matrix resumes' % (
            failed[0]['detail'], run_cmd)
    return ('%d cell(s) done; the metrics are in %s and the join analysis is in this result'
            % (len(matrix), opts['evidence_dir']))


def build_plan(opts):
    findings, warnings, matrix, preview, state = plan(opts)
    analysis = build_join_analysis([], state['side'])
    result = base_result('plan', opts, state, [], analysis, build_summary(analysis),
                         [], warnings, not findings,
                         'refused: ' + '; '.join(f['detail'] for f in findings) if findings else '',
                         next_step_for('plan', findings, matrix, opts))
    return result, 0 if not findings else 2, preview


def run_all(opts, yes):
    findings, warnings, state = probe(opts)
    if findings:
        return refused('run', opts, state, findings, warnings), 2
    if not yes:
        return refused('run', opts, state,
                       [blocked('run needs --yes (show the user the plan first; '
                                '--yes is the record of their consent)', 'plan first, then add --yes')],
                       warnings), 2
    opts['side'] = state['side']  # run_cell reads it off the options record

    matrix = cells(opts)
    # every run entry carries the full runs[] key set from the start (join-table.md §6):
    # a failed or never-attempted cell reports runner_exit=None / metrics=None /
    # comparable=False instead of dropping the keys a consumer expects
    runs = [{'query': q, 'parallelism': p, 'object_reuse': False, 'status': 'pending',
             'runner_exit': None, 'metrics': None, 'comparable': False,
             'gluten_nodes': None, 'join_gluten_nodes': None,
             'plan_checked': False, 'plan_jids': [], 'plan_problem': None}
            for q, p in matrix]
    changes = []
    rest = state['rest']
    attributed = set()  # jids already spoken for by an earlier cell's plan check
    nexmark_conf = os.path.join(opts['nexmark_home'], 'conf', 'nexmark.yaml')
    # probe and run can be a blink apart: re-check the conf and backup paths before anything
    # is touched, so a conf turned into a link in the meantime is still refused untampered
    for path in (state['effective_conf'], nexmark_conf):
        problem = link_problem(path)
        if problem:
            return refused('run', opts, state, [blocked(*problem)], warnings), 2
    # the leftover repair and the whole swap window run under a lock beside each conf:
    # a concurrent run must not restore this run's in-flight backups as leftovers
    locks, lock_refusal = [], None
    for path in (state['effective_conf'], nexmark_conf):
        lock, refusal = take_lock(path, changes)
        if lock is not None:
            locks.append(lock)
        if refusal:
            lock_refusal = blocked(*refusal)
            break
    if lock_refusal:
        for lock in locks:
            try:
                os.close(lock)
            except OSError:
                pass
        return refused('run', opts, state, [lock_refusal], warnings), 2
    interrupted = [None]
    swap = ConfSwap(state['effective_conf'], nexmark_conf, rest['address'], rest['port'], changes)
    try:
        for path in (state['effective_conf'], nexmark_conf):
            if repair_leftover(path):
                changes.append({'action': 'restored',
                                'detail': '%s -> %s (leftover from a crashed run)' % (backup_path(path), path)})
        try:
            with swap:
                try:
                    for record in runs:
                        if not run_cell(record, opts, swap, rest, attributed):
                            break
                except KeyboardInterrupt:
                    interrupted[0] = 'a cell was interrupted (SIGINT / SIGTERM)'
                    for record in runs:
                        if record['status'] == 'pending' and 'detail' not in record:
                            record['detail'] = interrupted[0]
        except KeyboardInterrupt:
            interrupted[0] = interrupted[0] or 'interrupted before the confs were swapped in'
    finally:
        for lock in locks:  # the fds, held until the restore is done and verified:
            try:            # closing them releases the kernel flock
                os.close(lock)
            except OSError:
                pass

    for record in runs:
        if record['status'] == 'pending' and not record.get('detail'):
            record['detail'] = interrupted[0] if interrupted[0] else 'an earlier cell failed; this one was not run'
    failed = [r for r in runs if r['status'] == 'failed']
    not_run = [r for r in runs if r['status'] == 'pending']
    error = ''
    if interrupted[0]:
        error = interrupted[0]
    elif failed:
        error = '%s p=%d failed' % (failed[0]['query'], failed[0]['parallelism'])
    elif not_run:
        error = '%d cell(s) not run' % len(not_run)
    confs_restored = not swap.not_restored  # per the verified state, not the happy path
    if swap.not_restored:
        error = ('%s; ' % error if error else '') + 'confs NOT restored: %s' % ', '.join(swap.not_restored)
    analysis = build_join_analysis(runs, opts['side'])
    next_step = next_step_for('run', [], matrix, opts, runs)
    if swap.not_restored:
        next_step = 'restore the conf(s) named in the error by hand, then rerun plan'
    result = base_result('run', opts, state, runs, analysis, build_summary(analysis),
                         changes, warnings,
                         not failed and not not_run and not interrupted[0] and confs_restored,
                         error, next_step, confs_restored)
    return result, 1 if (failed or interrupted[0] or not_run or not confs_restored) else 0


def aborted(exc, command, opts):
    """The tool-own-crash shape. confs_restored is None, not a guess either way: a
    crash mid-swap may have happened before or after the verified restore and the
    tool cannot tell — the key stays present (the run contract), the value says
    unknown, the next_step sends the operator to check by hand."""
    side = None
    if os.path.isdir(opts.get('flink_home') or ''):
        try:
            side = side_of(opts['flink_home'])[0]
        except Exception:
            side = None
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'side': side,
                  'gluten_jars': None,
                  'flink_home': opts.get('flink_home'), 'nexmark_home': opts.get('nexmark_home'),
                  'effective_conf': None, 'rest': None, 'slots': None,
                  'calibre': {'events': CALIBRE_EVENTS, 'tps': CALIBRE_TPS},
                  'queries': list(JOIN_QUERIES), 'parallelism': opts.get('parallelism_int'),
                  'timeout': opts.get('timeout'), 'skip_check': opts.get('skip_check'),
                  'evidence_dir': opts.get('evidence_dir'), 'runs': [], 'join_analysis': [],
                   'summary': [], 'changes': [], 'warnings': [], 'confs_restored': None,
                   'next_step': 'fix the tool error first (see error_message), then rerun — '
                                'the tool aborted before the restore could be verified: check '
                                'the confs (and any %s files left beside them) by hand' % BACKUP_SUFFIX,
                   'error_message': 'tool aborted: %r' % exc}}


# --- --out -------------------------------------------------------------------
OUT_SCOPE = ('--out must be a regular path inside the project directory (the working directory), '
             'not a symlink or a path outside it')


def out_problem(path, overwrite):
    """Why --out cannot be written, or None. Checked before anything runs and again at the write."""
    real, problem = guarded_real(path, OUT_SCOPE)
    if problem:
        return problem
    if os.path.exists(real):
        if not overwrite:
            return '--out already exists, pass --overwrite to replace it: %s' % path
        if not stat.S_ISREG(os.stat(real).st_mode):  # a fifo would block open() until a reader shows up
            return '--out exists and is not a regular file, refusing to overwrite it: %s' % path
    return None


def write_out(path, result, overwrite):
    problem = out_problem(path, overwrite)
    if problem:
        print('nexmark_join.py: %s' % problem, file=sys.stderr)
        return 2
    # open the same path the guard just judged — the resolved real path,
    # never the raw argument (its '..'s and links are already folded away)
    real = os.path.realpath(path)
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print('nexmark_join.py: cannot write --out %s: %s' % (path, exc.strerror or exc), file=sys.stderr)
        return 2
    return 0


# --- rendering ---------------------------------------------------------------
def render(result, preview=None):
    r = result[KEY]
    if r['error_message'].startswith('tool aborted'):
        return 'TOOL ERROR: %s\nnext: %s' % (r['error_message'], r['next_step'])
    lines = ['nexmark-join %s  (side %s, nexmark 1e8 events / 5e7 tps, blackhole, join family)'
             % (r['command'], r['side']),
             '  flink %s' % r['flink_home']]
    if r.get('rest'):
        lines.append('  rest %s:%s   slots %s' % (r['rest']['address'], r['rest']['port'],
                                                   r['slots']['total'] if r.get('slots') else '?'))
    if r.get('skip_check'):
        lines.append('  precheck skipped (--skip-check: REST / slots not probed)')
    for finding in (r.get('warnings') or []):
        lines.append('  WARN  %s' % finding['detail'])
    if r['error_message'].startswith('refused'):
        lines.append('  REFUSED  %s' % r['error_message'][len('refused: '):])
    for line in (preview or []):
        lines.append('  %s' % line)
    if r['command'] == 'plan':
        n = len(r['queries']) * len(r['parallelism'])
        lines.append('  PLAN: %d cell(s) — %d query x %d parallelism — %s'
                     % (n, len(r['queries']), len(r['parallelism']), r['next_step']))
    else:
        marks = {'done': 'DONE    ', 'failed': 'FAILED  ', 'pending': 'NOT RUN '}
        for run in r['runs']:
            line = '  %s.p%d  %s%s' % (run['query'], run['parallelism'],
                                       marks[run['status']], run.get('detail', ''))
            if run.get('plan_checked') and run['query'] and (run.get('join_gluten_nodes') or 0) > 0:
                line += '  [join gluten nodes: %d]' % run['join_gluten_nodes']
            lines.append(line)
        if r.get('confs_restored') is False:
            lines.append('  confs NOT restored — restore them by hand from the %s files' % BACKUP_SUFFIX)
        elif r.get('confs_restored') is None:
            lines.append('  confs restore status unknown — the tool aborted before it could verify')
        elif r.get('confs_restored') and not r['error_message'].startswith('refused'):
            lines.append('  confs restored')  # a refusal never touched them: not worth a line
        if r['join_analysis']:
            lines.append('  --- join analysis ---')
            pushdown_mark = {'yes': 'pushdown yes', 'no': 'FAILED pushdown no', 'unknown': 'pushdown unknown',
                             'not_applicable': 'pushdown n/a (native side)', 'not_run': 'pushdown not run'}
            for entry in r['join_analysis']:
                lines.append('  %-5s %-22s avg %-12s %s' % (
                    entry['query'], entry['join_type'], render_throughput(entry['throughput_avg']),
                    pushdown_mark[entry['pushdown']]))
        if r['summary']:
            lines.append('  --- join-type summary (mean of query means) ---')
            for entry in r['summary']:
                lines.append('  %-12s avg %-12s (%s)' % (
                    entry['group'] + ' avg', render_throughput(entry['avg']),
                    ', '.join(entry['queries'])))
        lines.append('BENCHMARK: all cells done' if r['success'] else 'NOT DONE: %s' % (
            r['error_message'] or 'see the failed cell above'))
    lines.append('next: %s' % r['next_step'])
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the nexmark Join-family queries '
                                                 '(q3 q7 q8 q13 q20 q23 — fixed set) on the '
                                                 'current Flink side at the fixed calibre '
                                                 '(1e8 events / 5e7 tps / blackhole) and produce '
                                                 'the join-focused analysis report. plan by default; '
                                                 'run needs --yes.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'run'))
    parser.add_argument('--flink-home', help='the Flink installation to benchmark (default: $FLINK_HOME)')
    parser.add_argument('--nexmark-home', help='the runnable nexmark tree (default: $NEXMARK_HOME, then /opt/nexmark)')
    parser.add_argument('--parallelism', help='comma-separated parallelism steps, default 1,4,8,16 '
                                              '(the query set is fixed and takes no override)')
    parser.add_argument('--timeout', type=int, help='per-cell runner timeout in seconds (default %d)' % DEFAULT_TIMEOUT)
    parser.add_argument('--skip-check', action='store_true',
                        help='skip the REST / slot prechecks (they are best-effort warnings anyway; '
                             'this skips the probes entirely)')
    parser.add_argument('--yes', action='store_true', help='run: the record that the user saw the plan and consented')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing regular file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)

    try:
        parallelism = [int(p) for p in parse_list(args.parallelism
                                                 or ','.join(str(n) for n in DEFAULT_PARALLELISM),
                                                 r'^[1-9]\d*$', 'parallelism')]
    except ValueError as exc:
        print('nexmark_join.py: %s' % exc, file=sys.stderr)
        return 2

    opts = {'flink_home': args.flink_home or os.environ.get('FLINK_HOME') or '/usr/local/flink',
            'nexmark_home': args.nexmark_home or os.environ.get('NEXMARK_HOME') or '/opt/nexmark',
            'parallelism_int': parallelism, 'skip_check': args.skip_check,
            'timeout': args.timeout or DEFAULT_TIMEOUT,
            'evidence_dir': os.path.abspath(os.path.join(os.getcwd(), 'e2e', 'perf', 'join')),
            'explicit': {flag: getattr(args, flag) for flag in
                         ('flink_home', 'nexmark_home', 'parallelism', 'timeout')
                         if getattr(args, flag) is not None}}

    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs: a bad --out must not touch the machine
        if problem:
            print('nexmark_join.py: %s' % problem, file=sys.stderr)
            return 2

    def on_term(signum, frame):
        raise KeyboardInterrupt()  # let the finally-blocks restore the confs

    signal.signal(signal.SIGTERM, on_term)

    preview = None
    try:
        if args.command == 'plan':
            result, code, preview = build_plan(opts)
        else:
            result, code = run_all(opts, args.yes)
    except Exception as exc:
        result, code = aborted(exc, args.command, opts), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result, preview))
    return code


if __name__ == '__main__':
    sys.exit(main())
