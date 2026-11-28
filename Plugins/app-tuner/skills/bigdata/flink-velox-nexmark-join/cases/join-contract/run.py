#!/usr/bin/env python3
"""join-contract — tier-A case for flink-velox-nexmark-join.

Drives scripts/nexmark_join.py (plan / run) against a fake machine laid out
in a scratch directory: a fake Flink home (bin/flink, conf/config.yaml — the one
Flink reads on this line — conf/workers, lib with the dist and nexmark jars,
optionally gluten/velox jars to make the GlutenFlink side), a fake nexmark tree
whose bin/run_query.sh is a stub recording the confs it sees and printing a
canned "Nexmark Results" table (the row throughput from a per-query, per-call
map; switches for exit-failure, no-table, small-events and sleep), and a real
local HTTP stub answering /overview, /jobs/overview, /jobs/<jid>/plan (the join
pushdown judgement reads engine fields only — the description is a trap on
purpose) and PATCH /jobs/<jid>/cancel. The script only ever runs in a child
process with explicit --flink-home / --nexmark-home and FLINK_HOME / NEXMARK_HOME
overridden — nothing from the real machine is in play, and the runner stub is a
child of that child.

Pins: plan prints the 24-cell matrix and the join family and touches nothing
(byte identity of both confs); the fixed query set takes no --queries override
(argparse refuses, exit 2); the side is reported, never a gate; the REST and
slot prechecks are warnings, never refusals (--skip-check skips the probes
entirely, and a run against a dead REST still completes); the full run contract
(nexmark conf swapped in as a whole with this cluster's REST endpoint,
parallelism.default and object-reuse=false set per cell, evidence tree +
metrics parsing, events off the calibre flagged not comparable, the first
failure stopping the matrix with the rest NOT RUN); a timed-out or interrupted
cell drops the runner's partial output as its *.submit.log and cancels the
cluster jobs it owns — the jid the runner output named, else the jobs that
appeared since the cell began (job-set baseline), the bare time window only as
an imprecise last resort, marked as such; the SIGINT/SIGTERM path cancels the
same way; failed and pending cells carry the full runs[] key set with nulled
values; the join pushdown judgement on the GlutenFlink side (gluten-join nodes
-> yes; clean or calc-only or description trap -> FAILED no; REST refuse ->
unknown; native -> not applicable, no REST spent); the static join semantics
per query; the summary arithmetic (mean of the per-query means, not comparable
cells excluded); the calibre file byte-identical to performance-verify's; the
run lock beside each conf as a kernel flock (two concurrent runs cannot both
enter — the second is refused exit 2; a crashed holder's lock file is taken
over, never unlinked; a lock that is itself a symlink is refused; plan never
locks); the backup published atomically; the restore chowning the original
owner back (copystat copies no uid/gid), outcome in changes; confs restored
after success, failure and SIGTERM — byte-identical, confs_restored telling
the truth (a refusal: true, nothing touched; a tool abort: null, unknowable);
crashed-run leftovers repaired first, a lost original refused; confs, backups
and locks that are symbolic links refused; --out guardrails (judged and written
as the same resolved realpath — kernel semantics, symlinks resolve before '..',
so link/../x cannot escape the project lexically; a symlink component, even
one pointing back inside, is never written through; a plain nested path still
writes); the evidence guard carrying the full --out rule (the same
resolved-path judgment per file — any symlink component refused, the resolved
path must stay inside the project — plus the write-time re-check of a path
swapped to a link or a fifo mid-cell); the crash result carrying the full
key set; the terminology section (GlutenFlink official naming); result.json
reflecting the real unittest outcome (a failing suite writes success=false and
exits 1 — pinned by an injected-failure child run); the suggested command
running as printed from another directory.

Writes results/result.json; exit 0 iff all tests pass.
"""
import http.server
import json
import os
import pathlib
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = pathlib.Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / 'scripts' / 'nexmark_join.py'
PERF_SKILL = ROOT.parent / 'flink-velox-performance-verify'  # the calibre twin
RESULTS = pathlib.Path(os.environ.get('JOIN_CONTRACT_RESULTS', str(CASE_DIR / 'results')))
SCRATCH = RESULTS / 'scratch'

KEY = 'flink_velox_nexmark_join_result'
BAK = '.flink-velox-join.bak'
LOCK = '.flink-velox-join.lock'
PERF_BAK = '.flink-velox-perf.bak'
SLOTS_PER_TM = 8
FIXED_QUERIES = ('q3', 'q7', 'q8', 'q13', 'q20', 'q23')

RESULT_KEYS = {'success', 'checked_at', 'command', 'side', 'gluten_jars', 'flink_home',
               'nexmark_home', 'effective_conf', 'rest', 'slots', 'calibre', 'queries',
               'parallelism', 'timeout', 'skip_check', 'evidence_dir', 'runs', 'join_analysis',
               'summary', 'changes', 'warnings', 'confs_restored', 'next_step', 'error_message'}

RUN_BASE_KEYS = {'query', 'parallelism', 'object_reuse', 'status', 'runner_exit', 'metrics',
                 'comparable', 'gluten_nodes', 'join_gluten_nodes', 'plan_checked', 'plan_jids',
                 'plan_problem'}

RUNNER_STUB = textwrap.dedent('''\
    #!/usr/bin/env bash
    # fake run_query.sh: records the confs it runs under, then prints a canned
    # "Nexmark Results" table; the row throughput comes from RUNNER_TP
    # ("q3=1000000+4000000;q20=3000000" — per query, per call of that query,
    # later calls reuse the last value), switches (RUNNER_FAIL / RUNNER_SLEEP /
    # RUNNER_QUIET / RUNNER_NO_TABLE / RUNNER_STEAL_BAK / RUNNER_SMALL_CALLS /
    # RUNNER_WRONG_QUERY / RUNNER_JID / RUNNER_SWAP_EVIDENCE /
    # RUNNER_FIFO_EVIDENCE) come through the environment
    q="$2"
    n="$(cat "$CAPTURE/count" 2>/dev/null || echo 0)"; n=$((n + 1)); echo "$n" > "$CAPTURE/count"
    qn="$(cat "$CAPTURE/count.$q" 2>/dev/null || echo 0)"; qn=$((qn + 1)); echo "$qn" > "$CAPTURE/count.$q"
    cp "$NEXMARK_HOME/conf/nexmark.yaml" "$CAPTURE/nexmark.$n.yaml"
    cp "$FLINK_HOME/conf/config.yaml" "$CAPTURE/flinkconf.$n.yaml" 2>/dev/null || true
    {
      echo "--- call $n: category=$1 query=$q"
      grep -E '^(parallelism.default|pipeline.object-reuse):' "$FLINK_HOME/conf/config.yaml"
    } >> "$CAPTURE/calls.log"
    echo "$n" > "$CAPTURE/started"
    [ "${RUNNER_STEAL_BAK:-}" = 1 ] && mv "$NEXMARK_HOME/conf/nexmark.yaml.flink-velox-join.bak" "$CAPTURE/stolen.bak" 2>/dev/null || true
    # the job this cell submits: a marker the REST stub only sees from now on
    # (a real job shows up in /jobs/overview only after submission), carrying
    # the plan mode PLAN_MODES picked for this query (default clean)
    mode="clean"
    for pair in ${PLAN_MODES//;/ }; do
      k="${pair%%=*}"
      [ "$k" = "$q" ] && mode="${pair#*=}"
    done
    mkdir -p "$CAPTURE/jobs"
    echo "$(date +%s%3N) $mode" > "$CAPTURE/jobs/job-$n.job"
    [ -n "${RUNNER_JID:-}" ] && echo "Job has been submitted with ID $RUNNER_JID"
    # mid-cell evidence swap (RUNNER_SWAP_EVIDENCE / RUNNER_FIFO_EVIDENCE): the
    # path was clean when the cell started; by the write it is a link / a fifo
    if [ "${RUNNER_SWAP_EVIDENCE:-}" = 1 ] || [ "${RUNNER_FIFO_EVIDENCE:-}" = 1 ]; then
      p="$(grep -E '^parallelism.default:' "$FLINK_HOME/conf/config.yaml" | head -1 | awk '{print $2}')"
      mkdir -p e2e/perf/join
      if [ "${RUNNER_SWAP_EVIDENCE:-}" = 1 ]; then
        rm -f "e2e/perf/join/$q.p$p.out"; ln -s "$CAPTURE/victim.out" "e2e/perf/join/$q.p$p.out"
      else
        rm -f "e2e/perf/join/$q.p$p.submit.log"; mkfifo "e2e/perf/join/$q.p$p.submit.log"
      fi
    fi
    [ "${RUNNER_FAIL:-}" = 1 ] && { echo "runner: boom" >&2; exit 7; }
    if [ "${RUNNER_SLEEP:-}" = 1 ]; then
      [ "${RUNNER_QUIET:-}" = 1 ] || echo "runner: job submitted, stub output before the long sleep"
      sleep 300
    fi
    [ "${RUNNER_NO_TABLE:-}" = 1 ] && exit 0
    small=0
    for c in ${RUNNER_SMALL_CALLS//,/ }; do [ "$c" = "$n" ] && small=1; done
    if [ "$small" = 1 ]; then EV=1000; else EV=100000000; fi
    if [ "${RUNNER_WRONG_QUERY:-}" = 1 ]; then ROW="q99"; else ROW="$q"; fi
    TP=2352941.2
    if [ -n "${RUNNER_TP:-}" ]; then
      for pair in ${RUNNER_TP//;/ }; do
        k="${pair%%=*}"; vals="${pair#*=}"
        if [ "$k" = "$q" ]; then
          i=1
          for v in ${vals//+/ }; do
            [ "$i" = "$qn" ] && TP="$v"
            i=$((i + 1))
          done
          [ "$i" -le "$qn" ] && TP="${vals##*+}"
        fi
      done
    fi
    echo "-------------------------------- Nexmark Results --------------------------------"
    echo ""
    echo "+-------+------------+-------+---------+-----------------+------------+-------------------+"
    echo "| Query | Events Num | Cores | Time(s) | Cores * Time(s) | Throughput | Throughput/Cores |"
    echo "+-------+------------+-------+---------+-----------------+------------+-------------------+"
    printf "| %-5s | %-10d | %-5d | %-7s | %-15.3f | %-10s | %-17.1f |\\n" "$ROW" "$EV" 8 "42.500" 340.000 "$TP" 294117.6
    echo "+-------+------------+-------+---------+-----------------+------------+-------------------+"
    exit 0
    ''')

# plan fixtures for the join pushdown judgement: engine fields only carry the
# signal; descriptions embed SQL-ish text and are traps on purpose
GLUTEN_JOIN_NODES = [
    {'id': 1, 'operator': 'Join', 'operator_strategy': 'Hash Join',
     'description': 'plain text', 'inputs': [{'ship_strategy': 'GlutenHASH', 'exchange': 'forward'}]},
    {'id': 2, 'operator': 'Gluten Join', 'operator_strategy': 'Gluten Hash Join',
     'description': 'plain text'},
]
CLEAN_NODES = [
    {'id': 1, 'operator': 'Join', 'operator_strategy': 'Hash Join',
     'description': 'join over auction and person'},
]
GLUTEN_CALC_ONLY_NODES = [  # gluten present, but only calc shapes: the join fell back
    {'id': 1, 'operator': 'Gluten cal', 'operator_strategy': 'Calc', 'description': 'filter'},
    {'id': 2, 'operator': 'Join', 'operator_strategy': 'Hash Join', 'description': 'native join'},
]
DESC_TRAP_NODES = [  # descriptions mention gluten / join: engine fields stay clean
    {'id': 1, 'operator': 'Join', 'operator_strategy': 'Hash Join',
     'description': 'gluten marker in sql text'},
    {'id': 2, 'operator': 'Gluten cal', 'operator_strategy': 'Calc',
     'description': 'join probe in sql text'},
]
PLAN_FIXTURES = {'gluten-join': GLUTEN_JOIN_NODES, 'clean': CLEAN_NODES,
                 'calc-only': GLUTEN_CALC_ONLY_NODES, 'desc-trap': DESC_TRAP_NODES}


class RestStub(threading.Thread):
    """A live /overview endpoint for the liveness probe, the job side the
    timeout-cancel path talks to (GET /jobs/overview, PATCH /jobs/<jid>/cancel)
    and the plan side the join pushdown judgement reads (GET /jobs/<jid>/plan —
    the REST API wraps the plan in a 'plan' key like the real one). Jobs come
    from two places: an in-memory registry the test registers timeouts with,
    and marker files the runner stub drops as it "submits" (a job shows up in
    /jobs/overview only from its submission on, and only then — the way a real
    cluster behaves); a marker carries '<start_ms> <plan-mode>'."""

    jobs = {}
    capture_dir = None
    refuse_overview = 0  # answer /jobs/overview with 500 this many times (REST jitter)
    guard = threading.Lock()

    @classmethod
    def add_job(cls, jid, state='RUNNING', start_ms=None, fail_cancel=False):
        with cls.guard:
            cls.jobs[jid] = {'jid': jid, 'name': 'nexmark %s' % jid, 'state': state,
                             'start-time': int(time.time() * 1000) if start_ms is None else start_ms,
                             'fail_cancel': fail_cancel}

    @classmethod
    def reset(cls):
        with cls.guard:
            cls.jobs = {}
            cls.refuse_overview = 0

    @classmethod
    def marker_jobs(cls):
        out = []
        if cls.capture_dir:
            folder = pathlib.Path(cls.capture_dir) / 'jobs'
            if folder.is_dir():
                for marker in sorted(folder.glob('*.job')):
                    parts = marker.read_text(encoding='utf-8').split()
                    if len(parts) == 2:
                        out.append({'jid': marker.stem, 'name': 'nexmark %s' % marker.stem,
                                    'state': 'FINISHED', 'start-time': int(parts[0]),
                                    '_mode': parts[1]})
        return out

    @classmethod
    def plan_for(cls, jid):
        with cls.guard:
            if jid in cls.jobs:
                return 200, {'type': 'jobplan', 'plan': {'nodes': []}}
        for job in cls.marker_jobs():
            if job['jid'] == jid:
                if job['_mode'] == 'refuse':
                    return 404, {'errors': ['no plan']}
                return 200, {'type': 'jobplan', 'plan': {'nodes': PLAN_FIXTURES.get(job['_mode'], [])}}
        return 404, {}

    def __init__(self):
        super().__init__(daemon=True)

        def reply(handler, code, body):
            handler.send_response(code)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        def do_get(handler):
            m = re.match(r'^/jobs/([^/]+)/plan$', handler.path)
            if m:
                status, body = RestStub.plan_for(m.group(1))
                reply(handler, status, json.dumps(body).encode('utf-8'))
                return
            if handler.path == '/jobs/overview':
                with RestStub.guard:
                    refuse = RestStub.refuse_overview > 0
                    if refuse:
                        RestStub.refuse_overview -= 1
                if refuse:
                    reply(handler, 500, b'{}')
                    return
                jobs = RestStub.marker_jobs()
                with RestStub.guard:
                    jobs += [{k: v for k, v in job.items() if k != 'fail_cancel'}
                             for job in RestStub.jobs.values()]
                reply(handler, 200, json.dumps({'jobs': jobs}).encode('utf-8'))
            else:  # /overview and anything else: alive and empty
                reply(handler, 200, b'{}')

        def do_patch(handler):
            m = re.match(r'^/jobs/([^/]+)/cancel$', handler.path)
            with RestStub.guard:
                job = RestStub.jobs.get(m.group(1)) if m else None
                if job is None:
                    reply(handler, 404, b'{}')
                elif job.get('fail_cancel'):
                    reply(handler, 500, b'{}')
                else:
                    job['state'] = 'CANCELED'
                    reply(handler, 202, b'{}')

        handler_class = type('ok', (http.server.BaseHTTPRequestHandler,),
                             {'do_GET': do_get, 'do_PATCH': do_patch,
                              'log_message': lambda self, *a: None})
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler_class)
        self.port = self.server.server_address[1]

    def run(self):
        self.server.serve_forever()


def tool_env(**extra):
    """The child environment: no FLINK_HOME / NEXMARK_HOME / RUNNER_* / PLAN_MODES
    from this machine."""
    env = {k: v for k, v in os.environ.items()
           if k not in ('FLINK_HOME', 'NEXMARK_HOME', 'PLAN_MODES')
           and not k.startswith('RUNNER_')}
    env.update(extra)
    return env


class JoinCase(unittest.TestCase):
    rest = None

    @classmethod
    def setUpClass(cls):
        cls.rest = RestStub()
        cls.rest.start()
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if cls.rest:
            cls.rest.server.shutdown()
        # result.json is written by __main__ from the real unittest outcome —
        # tearDownClass only cleans up (a hardcoded success here would make the
        # case.yaml json_field assertion a tautology)

    def setUp(self):
        RestStub.reset()
        self.base = SCRATCH / self.id().split('.')[-1]
        self.machine = self.build_machine()

    # --- the fake machine ----------------------------------------------------
    def build_machine(self, slots=SLOTS_PER_TM, gluten=False):
        m = self.base
        flink = m / 'flink'
        nexmark = m / 'nexmark'
        project = m / 'project'
        capture = m / 'capture'
        for d in (flink / 'bin', flink / 'conf', flink / 'lib',
                  nexmark / 'bin', nexmark / 'conf', project, capture):
            d.mkdir(parents=True, exist_ok=True)
        (flink / 'bin' / 'flink').write_text('#!/bin/sh\n', encoding='utf-8')
        os.chmod(str(flink / 'bin' / 'flink'), 0o755)
        (flink / 'conf' / 'config.yaml').write_text(
            'jobmanager.rpc.address: localhost\nrest.address: 127.0.0.1\nrest.port: %d\n'
            'taskmanager.numberOfTaskSlots: %d\nparallelism.default: 1\n'
            'pipeline.object-reuse: true\n' % (self.rest.port, slots), encoding='utf-8')
        (flink / 'conf' / 'workers').write_text('localhost\n', encoding='utf-8')
        jars = ['flink-dist-1.19.2.jar', 'nexmark-flink-0.3-SNAPSHOT.jar']
        if gluten:
            jars += ['gluten-flink-1.19.2.jar', 'velox4j-1.0.jar']
        for jar in jars:
            (flink / 'lib' / jar).write_text('fake jar\n', encoding='utf-8')
        (nexmark / 'conf' / 'nexmark.yaml').write_text(
            'nexmark.workload.suite.100m.events.num: 10000\n'
            'nexmark.workload.suite.100m.tps: 2000\n'
            'nexmark.workload.suite.100m.queries: "q0,q1,q2"\n'
            'flink.rest.address: localhost\nflink.rest.port: 8188\n' , encoding='utf-8')
        runner = nexmark / 'bin' / 'run_query.sh'
        runner.write_text(RUNNER_STUB, encoding='utf-8')
        os.chmod(str(runner), 0o755)
        RestStub.capture_dir = str(capture)  # the runner's job markers live here
        return {'flink': flink, 'nexmark': nexmark, 'project': project,
                'capture': capture, 'flink_conf': flink / 'conf' / 'config.yaml',
                'nexmark_conf': nexmark / 'conf' / 'nexmark.yaml', 'gluten': gluten}

    def env(self, switches=None):
        e = tool_env(FLINK_HOME=str(self.machine['flink']),
                     NEXMARK_HOME=str(self.machine['nexmark']),
                     CAPTURE=str(self.machine['capture']),
                     CALL_LOG=str(self.machine['capture'] / 'calls.log'))
        e.update(switches or {})
        return e

    def run_tool(self, args, switches=None, cwd=None, timeout=180, as_json=True):
        call = [sys.executable, str(SCRIPT)] + (['--json'] if as_json else []) + args
        proc = subprocess.run(call, cwd=str(cwd or self.machine['project']),
                              env=self.env(switches), capture_output=True, text=True, timeout=timeout)
        payload = None
        if as_json:
            try:
                payload = json.loads(proc.stdout)
            except ValueError:
                pass
        return proc, payload

    def start_tool(self, args, switches=None):
        """The tool as a live child process — for registering cluster jobs while a
        cell is mid-run (a job this cell submits appears on the cluster only after
        the cell's baseline was taken; jobs registered before the start are
        somebody else's)."""
        return subprocess.Popen([sys.executable, str(SCRIPT), '--json'] + args,
                                cwd=str(self.machine['project']),
                                env=self.env(switches), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def wait_for_runner(self, proc, deadline_s=30):
        """Block until the sleeping runner stub of a live tool process has started."""
        deadline = time.time() + deadline_s
        while not (self.machine['capture'] / 'started').exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.machine['capture'] / 'started').exists(),
                        'the runner stub was never invoked')

    def common_args(self, extra=None):
        return (['--flink-home', str(self.machine['flink']),
                 '--nexmark-home', str(self.machine['nexmark'])] + (extra or []))

    def snapshot(self, *paths):
        return {str(p): p.read_bytes() for p in paths}

    def no_backups(self):
        return [p for p in self.base.rglob('*' + BAK) if p.is_file()]

    def held_locks(self):
        """The lock files still flock-held by a live process — the lock is a kernel
        flock, so the FILE persists after a clean run (the next holder truncates
        and rewrites it); released means a probe flock succeeds."""
        import fcntl
        held = []
        for p in self.base.rglob('*' + LOCK):
            if not p.is_file() or p.is_symlink():
                continue
            try:
                fd = os.open(str(p), os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    held.append(p)
                finally:
                    os.close(fd)
            except OSError:
                continue
        return held

    def no_tmps(self):
        return [p for p in self.base.rglob('*.tmp-*')]

    def evidence(self, name):
        return self.machine['project'] / 'e2e' / 'perf' / 'join' / name

    def analysis_of(self, payload, query):
        return next(a for a in payload[KEY]['join_analysis'] if a['query'] == query)

    def summary_of(self, payload, group):
        return next(s for s in payload[KEY]['summary'] if s['group'] == group)

    # --- plan ----------------------------------------------------------------
    def test_plan_prints_matrix_and_touches_nothing(self):
        before = self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf'])
        args = ['plan'] + self.common_args()
        proc, payload = self.run_tool(args)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(payload[KEY]['success'])
        self.assertEqual(set(payload[KEY]), RESULT_KEYS - {'confs_restored'}, payload[KEY])
        self.assertEqual(payload[KEY]['queries'], list(FIXED_QUERIES))
        self.assertEqual(payload[KEY]['parallelism'], [1, 4, 8, 16])
        self.assertEqual(payload[KEY]['rest']['port'], self.rest.port)
        self.assertEqual(payload[KEY]['slots'], {'tms': 1, 'per_tm': SLOTS_PER_TM, 'total': SLOTS_PER_TM})
        self.assertEqual(payload[KEY]['side'], 'native')
        # the 16-step default on an 8-slot machine: a warning, never a refusal
        self.assertEqual(len(payload[KEY]['warnings']), 1)
        self.assertIn('less than the largest requested parallelism 16',
                      payload[KEY]['warnings'][0]['detail'])
        self.assertIn('run --yes', payload[KEY]['next_step'])
        self.assertIn(str(SCRIPT), payload[KEY]['next_step'])
        self.assertIn('--flink-home', payload[KEY]['next_step'])
        # the human-readable plan carries the join family, the conf preview and the matrix size
        rendered, _ = self.run_tool(args, as_json=False)
        self.assertEqual(rendered.returncode, 0)
        self.assertIn('fixed join family', rendered.stdout)
        self.assertIn('swapped in', rendered.stdout)
        self.assertIn('restored afterwards', rendered.stdout)
        self.assertIn('PLAN: 24 cell(s)', rendered.stdout)
        self.assertEqual(before, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertEqual(self.no_backups(), [])

    def test_plan_carries_the_static_join_analysis_skeleton(self):
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual([a['query'] for a in payload[KEY]['join_analysis']], list(FIXED_QUERIES))
        for entry in payload[KEY]['join_analysis']:
            self.assertIsNone(entry['throughput_avg'])
            self.assertEqual(entry['pushdown'], 'not_applicable')  # native side
        self.assertEqual([s['group'] for s in payload[KEY]['summary']],
                         ['regular', 'window-join', 'lookup', 'cascade'])
        for entry in payload[KEY]['summary']:
            self.assertIsNone(entry['avg'])
        # on the GlutenFlink side the skeleton says not_run instead
        self.machine = self.build_machine(gluten=True)
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        for entry in payload[KEY]['join_analysis']:
            self.assertEqual(entry['pushdown'], 'not_run')

    def test_plan_on_gluten_side_reports_the_side_no_gate(self):
        self.machine = self.build_machine(gluten=True)
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload[KEY]['side'], 'gluten')
        self.assertEqual(payload[KEY]['gluten_jars'], ['gluten-flink-1.19.2.jar', 'velox4j-1.0.jar'])
        rendered, _ = self.run_tool(['plan', '--parallelism', '1'] + self.common_args(), as_json=False)
        self.assertIn('pushdown verdict', rendered.stdout)

    def test_rest_and_slot_prechecks_are_warnings_not_gates(self):
        # the join calibre: checks best-effort, no hard REST / slot gate — REST
        # jitter or a thin slot count must not block the matrix
        self.machine['flink_conf'].write_text(
            'rest.address: 127.0.0.1\nrest.port: 1\ntaskmanager.numberOfTaskSlots: 2\n', encoding='utf-8')
        proc, payload = self.run_tool(['plan', '--parallelism', '4'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(payload[KEY]['success'])
        details = ' '.join(w['detail'] for w in payload[KEY]['warnings'])
        self.assertIn('REST', details)
        self.assertIn('2 slot(s)', details)
        self.assertEqual(len(payload[KEY]['warnings']), 2)
        rendered, _ = self.run_tool(['plan', '--parallelism', '4'] + self.common_args(), as_json=False)
        self.assertIn('WARN', rendered.stdout)

    def test_skip_check_skips_the_probes_entirely(self):
        self.machine['flink_conf'].write_text(
            'rest.address: 127.0.0.1\nrest.port: 1\ntaskmanager.numberOfTaskSlots: 2\n', encoding='utf-8')
        proc, payload = self.run_tool(['plan', '--parallelism', '4', '--skip-check'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(payload[KEY]['success'])
        self.assertTrue(payload[KEY]['skip_check'])
        self.assertEqual(payload[KEY]['warnings'], [])
        self.assertEqual(payload[KEY]['rest']['port'], 1)  # still derived: the fixed conf needs it
        rendered, _ = self.run_tool(['plan', '--parallelism', '4', '--skip-check']
                                    + self.common_args(), as_json=False)
        self.assertIn('precheck skipped', rendered.stdout)

    def test_run_completes_with_rest_dead(self):
        # REST jitter must not block a run: the cells speak for themselves
        self.machine['flink_conf'].write_text(
            'rest.address: 127.0.0.1\nrest.port: 1\ntaskmanager.numberOfTaskSlots: 8\n', encoding='utf-8')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(payload[KEY]['success'])
        self.assertEqual(len(payload[KEY]['runs']), 6)
        self.assertIn('REST', ' '.join(w['detail'] for w in payload[KEY]['warnings']))

    def test_fixed_query_set_takes_no_queries_override(self):
        # the join family is the skill's identity: --queries is refused outright
        proc, _ = self.run_tool(['plan', '--queries', 'q0'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('unrecognized arguments: --queries', proc.stderr)
        proc2, _ = self.run_tool(['run', '--yes', '--queries', 'q3,q20'] + self.common_args())
        self.assertEqual(proc2.returncode, 2)
        self.assertIn('unrecognized arguments: --queries', proc2.stderr)
        self.assertFalse((self.machine['capture'] / 'started').exists())

    def test_calibre_file_is_byte_identical_to_performance_verify(self):
        # cross-skill comparability pinned: the conf is a byte copy of the twin's
        ours = (ROOT / 'conf' / 'nexmark-bench.yaml')
        theirs = (PERF_SKILL / 'conf' / 'nexmark-bench.yaml')
        self.assertTrue(theirs.is_file(), 'the performance-verify twin is not beside this skill')
        self.assertEqual(ours.read_bytes(), theirs.read_bytes())

    def test_refuses_missing_prerequisites(self):
        missing = self.base / 'nowhere'
        proc, payload = self.run_tool(['plan', '--flink-home', str(missing),
                                       '--nexmark-home', str(self.machine['nexmark'])])
        self.assertEqual(proc.returncode, 2)
        self.assertIn('Flink home not found', payload[KEY]['error_message'])
        os.unlink(str(self.machine['nexmark'] / 'bin' / 'run_query.sh'))
        proc, payload = self.run_tool(['plan'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('nexmark runner', payload[KEY]['error_message'])
        self.assertIn('flink-velox-runtime-install', payload[KEY]['next_step'])

    def test_effective_conf_precedence(self):
        (self.machine['flink'] / 'conf' / 'flink-conf.yaml').write_text(
            'rest.address: 127.0.0.1\nrest.port: %d\ntaskmanager.numberOfTaskSlots: 4\n' % self.rest.port,
            encoding='utf-8')
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(payload[KEY]['rest']['port'], self.rest.port)
        self.assertTrue(payload[KEY]['effective_conf'].endswith('flink-conf.yaml'),
                        payload[KEY]['effective_conf'])
        self.assertEqual(payload[KEY]['slots']['total'], 4)  # config.yaml's 8 must not leak in
        # config.yaml-only machine: flink-conf.yaml's port 1 is the one that counts -> a warning
        other = self.build_machine()
        (other['flink'] / 'conf' / 'flink-conf.yaml').write_text(
            'rest.address: 127.0.0.1\nrest.port: 1\n', encoding='utf-8')
        proc2 = subprocess.run(
            [sys.executable, str(SCRIPT), '--json', 'plan', '--parallelism', '1',
             '--flink-home', str(other['flink']), '--nexmark-home', str(other['nexmark'])],
            cwd=str(other['project']), env=self.env(), capture_output=True, text=True, timeout=180)
        payload2 = json.loads(proc2.stdout)
        self.assertEqual(proc2.returncode, 0)  # REST dead is a warning, not a refusal
        self.assertIn('REST', ' '.join(w['detail'] for w in payload2[KEY]['warnings']))

    # --- run -----------------------------------------------------------------
    def test_run_needs_yes_and_touches_nothing(self):
        before = self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf'])
        proc, payload = self.run_tool(['run', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('--yes', payload[KEY]['error_message'])
        self.assertEqual(before, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertEqual(self.no_backups(), [])
        self.assertFalse((self.machine['capture'] / 'started').exists())

    def test_run_matrix_captures_and_restores(self):
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        original_flink = self.machine['flink_conf'].read_bytes()
        out = self.machine['project'] / 'result.json'
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1',
                                       '--out', 'result.json'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = payload[KEY]
        self.assertTrue(r['success'])
        self.assertTrue(r['confs_restored'])
        self.assertEqual([(run['query'], run['parallelism']) for run in r['runs']],
                         [(q, 1) for q in FIXED_QUERIES])
        self.assertTrue(all(run['object_reuse'] is False for run in r['runs']))
        run = r['runs'][0]
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['runner_exit'], 0)
        self.assertEqual(run['metrics']['events_num'], 100000000)
        self.assertEqual(run['metrics']['throughput'], 2352941.2)
        self.assertEqual(run['metrics']['time_s'], 42.5)
        self.assertTrue(run['comparable'])
        # evidence: the table and the full runner log
        table = self.evidence('q3.p1.out').read_text(encoding='utf-8')
        self.assertIn('Nexmark Results', table)
        self.assertIn('| q3', table)
        self.assertIn('Nexmark Results',
                      self.evidence('q3.p1.submit.log').read_text(encoding='utf-8'))
        # the stub saw the swapped-in fixed conf with this cluster's REST endpoint
        swapped = (self.machine['capture'] / 'nexmark.1.yaml').read_text(encoding='utf-8')
        self.assertIn('suite.bench.events.num: 100000000', swapped)
        self.assertIn('flink.rest.port: %d' % self.rest.port, swapped)
        self.assertNotIn('suite.100m', swapped)
        calls = (self.machine['capture'] / 'calls.log').read_text(encoding='utf-8')
        self.assertIn('parallelism.default: 1', calls)
        self.assertIn('pipeline.object-reuse: false', calls)
        # both confs restored to the byte, no backups left behind
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(original_flink, self.machine['flink_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])
        actions = [c['action'] for c in r['changes']]
        self.assertEqual(actions.count('backed up'), 2)
        self.assertEqual(actions.count('restored'), 2)
        self.assertIn('swapped in', actions)
        # --out carries the same result
        written = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(written[KEY]['success'], True)

    # --- the join pushdown judgement -------------------------------------------
    def test_gluten_side_judges_pushdown_from_the_plans(self):
        self.machine = self.build_machine(gluten=True)
        # the six cells submit six jobs (marker per call): q3 pushed down, q7
        # clean, q8 gluten but calc-only (the join fell back), q13's plan
        # refused over REST, q20 pushed down, q23 a description trap
        modes = ('q3=gluten-join;q7=clean;q8=calc-only;q13=refuse;q20=gluten-join;q23=desc-trap' )
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'PLAN_MODES': modes})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = payload[KEY]
        self.assertEqual(r['side'], 'gluten')
        by_query = {run['query']: run for run in r['runs']}
        self.assertTrue(by_query['q3']['plan_checked'])
        self.assertEqual(by_query['q3']['gluten_nodes'], 2)
        self.assertEqual(by_query['q3']['join_gluten_nodes'], 2)
        self.assertEqual(by_query['q8']['gluten_nodes'], 1)  # gluten present, join not
        self.assertEqual(by_query['q8']['join_gluten_nodes'], 0)
        self.assertFalse(by_query['q13']['plan_checked'])  # plan REST answered 404
        expected = {'q3': 'yes', 'q7': 'no', 'q8': 'no', 'q13': 'unknown',
                    'q20': 'yes', 'q23': 'no'}
        for query, pushdown in expected.items():
            self.assertEqual(self.analysis_of(payload, query)['pushdown'], pushdown,
                             '%s: %s' % (query, self.analysis_of(payload, query)))
        self.assertIn('fell back to native operators',
                      self.analysis_of(payload, 'q7')['pushdown_detail'])
        self.assertIn('REST', self.analysis_of(payload, 'q13')['pushdown_detail'])
        # a job belongs to exactly one cell: every jid attributed once
        self.assertEqual(sorted(j for run in r['runs'] for j in run['plan_jids']),
                         ['job-1', 'job-2', 'job-3', 'job-4', 'job-5', 'job-6'])
        # the human report marks a fallen-back join FAILED
        rendered, _ = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                    as_json=False, switches={'PLAN_MODES': modes})
        self.assertEqual(rendered.returncode, 0)
        self.assertIn('FAILED pushdown no', rendered.stdout)
        self.assertIn('pushdown yes', rendered.stdout)

    def test_native_side_spends_no_rest_round_trip(self):
        # no jobs registered: the native side never fetches a plan at all
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        for run in payload[KEY]['runs']:
            self.assertFalse(run['plan_checked'])
            self.assertIsNone(run['gluten_nodes'])
        for entry in payload[KEY]['join_analysis']:
            self.assertEqual(entry['pushdown'], 'not_applicable')
        rendered, _ = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                    as_json=False)
        self.assertIn('pushdown n/a (native side)', rendered.stdout)

    def test_gluten_side_with_rest_dead_reports_unknown(self):
        self.machine = self.build_machine(gluten=True)
        self.machine['flink_conf'].write_text(
            'rest.address: 127.0.0.1\nrest.port: 1\ntaskmanager.numberOfTaskSlots: 8\n', encoding='utf-8')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)  # the run itself is fine
        for run in payload[KEY]['runs']:
            self.assertFalse(run['plan_checked'])
            self.assertIn('REST', run['plan_problem'])
        for entry in payload[KEY]['join_analysis']:
            self.assertEqual(entry['pushdown'], 'unknown')
            self.assertIn('REST', entry['pushdown_detail'])

    def test_join_analysis_fields_are_complete(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual([a['query'] for a in payload[KEY]['join_analysis']], list(FIXED_QUERIES))
        for entry in payload[KEY]['join_analysis']:
            for key in ('join_type', 'sides', 'key_columns', 'predicate', 'group',
                        'throughput_avg', 'cells_done', 'cells_comparable',
                        'pushdown', 'pushdown_detail'):
                self.assertIn(key, entry)
            for key in ('join_type', 'sides', 'key_columns', 'predicate', 'group', 'pushdown'):
                self.assertTrue(entry[key], entry)
            self.assertIn(entry['group'], ('regular', 'window-join', 'lookup', 'cascade'))
        # the semantics are the real query's: q3 joins auction to person on the seller key
        q3 = self.analysis_of(payload, 'q3')
        self.assertEqual(q3['join_type'], 'regular join（双流）')
        self.assertIn('auction', q3['sides'])
        self.assertIn('seller', q3['key_columns'])
        self.assertIn('person.id', q3['key_columns'])

    def test_summary_arithmetic_is_mean_of_query_means(self):
        # stubbed throughputs: q3 gets 1e6 (not comparable) then 4e6, q20 3e6
        # twice — the regular average is (4e6 + 3e6) / 2, not the pooled mean
        # over cells; a not-comparable cell drops out of its query's mean
        tp = ('q3=1000000+4000000;q7=800000+800000;q8=1000000+1000000;'
              'q13=500000+500000;q20=3000000+3000000;q23=700000+700000')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1,2',
                                       ] + self.common_args(),
                                      switches={'RUNNER_TP': tp, 'RUNNER_SMALL_CALLS': '1'})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = payload[KEY]
        self.assertEqual(len(r['runs']), 12)  # 6 queries x 2 parallelism steps
        q3_run = [run for run in r['runs'] if run['query'] == 'q3']
        self.assertFalse(q3_run[0]['comparable'])  # small events: dropped from the mean
        self.assertTrue(q3_run[1]['comparable'])
        self.assertEqual(self.analysis_of(payload, 'q3')['throughput_avg'], 4000000.0)
        self.assertEqual(self.analysis_of(payload, 'q20')['throughput_avg'], 3000000.0)
        self.assertEqual(self.analysis_of(payload, 'q7')['throughput_avg'], 800000.0)
        self.assertEqual(self.analysis_of(payload, 'q8')['throughput_avg'], 1000000.0)
        self.assertEqual(self.analysis_of(payload, 'q13')['throughput_avg'], 500000.0)
        self.assertEqual(self.analysis_of(payload, 'q23')['throughput_avg'], 700000.0)
        self.assertEqual(self.summary_of(payload, 'regular'),
                         {'group': 'regular', 'queries': ['q3', 'q20'], 'avg': 3500000.0,
                          'cell_count': 3})
        self.assertEqual(self.summary_of(payload, 'window-join')['avg'], 900000.0)
        self.assertEqual(self.summary_of(payload, 'lookup')['avg'], 500000.0)
        self.assertEqual(self.summary_of(payload, 'cascade')['avg'], 700000.0)
        self.assertEqual(self.summary_of(payload, 'window-join')['cell_count'], 4)
        # the human report carries the four summary lines
        rendered, _ = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                    as_json=False, switches={'RUNNER_TP': tp})
        self.assertIn('regular avg', rendered.stdout)
        self.assertIn('window-join avg', rendered.stdout)
        self.assertIn('lookup avg', rendered.stdout)
        self.assertIn('cascade avg', rendered.stdout)

    def test_first_failure_stops_the_matrix(self):
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_FAIL': '1'})
        self.assertEqual(proc.returncode, 1)
        r = payload[KEY]
        self.assertFalse(r['success'])
        self.assertEqual(r['runs'][0]['status'], 'failed')
        self.assertIn('exited 7', r['runs'][0]['detail'])
        self.assertEqual(r['runs'][1]['status'], 'pending')
        self.assertIn('not run', r['runs'][1]['detail'])
        self.assertFalse(self.evidence('q7.p1.out').exists())
        self.assertTrue(r['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertIn('matrix resumes', r['next_step'])
        # a query whose cells never ran reports not_run (on the side where the
        # verdict applies at all)
        self.machine = self.build_machine(gluten=True)
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1']
                                      + self.common_args(), switches={'RUNNER_FAIL': '1'})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.analysis_of(payload, 'q23')['pushdown'], 'not_run')

    def test_runner_timeout_fails_the_cell_and_cancels_over_rest(self):
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        # the job this cell submits appears only once the cell is mid-run (a real
        # cluster only knows a job from its submission on) — registered live
        proc = self.start_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                               + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.wait_for_runner(proc)
        RestStub.add_job('job-1', start_ms=int(time.time() * 1000) + 10000)
        out, _ = proc.communicate(timeout=120)
        payload = json.loads(out)
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('did not finish within 2s', run['detail'])
        self.assertIn('cancelled the cluster job(s) that appeared since this cell began '
                      '(job-set baseline): job-1 (nexmark job-1)', run['detail'])
        self.assertEqual(RestStub.jobs['job-1']['state'], 'CANCELED')
        # partial output lands as the cell's submit.log and the detail points at it
        log = self.evidence('q3.p1.submit.log')
        self.assertIn('stub output before the long sleep', log.read_text(encoding='utf-8'))
        self.assertIn('see %s' % log, run['detail'])
        self.assertFalse(self.evidence('q3.p1.out').exists())
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])
        self.assertEqual(self.held_locks(), [])

    def test_timeout_cancel_is_ownership_scoped(self):
        # ownership first, three grades: (a) the jobs that appeared since the cell
        # began — a job the cluster already knew (somebody else's, even inside the
        # time window) is never touched; (b) the jid the runner's own output named —
        # exactly that job, decoys around it spared; (c) no baseline could be taken
        # (REST jittered at the cell start) — the bare time window, marked imprecise
        future = lambda: int(time.time() * 1000) + 10000  # noqa: E731
        with self.subTest(how='new-since-start (the baseline difference)'):
            RestStub.add_job('someone-elses-job', start_ms=future())
            RestStub.add_job('old-job', start_ms=int(time.time() * 1000) - 3600000)
            proc = self.start_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                   + self.common_args(), switches={'RUNNER_SLEEP': '1'})
            self.wait_for_runner(proc)
            RestStub.add_job('job-1', start_ms=future())
            out, _ = proc.communicate(timeout=120)
            run = json.loads(out)[KEY]['runs'][0]
            self.assertIn('appeared since this cell began', run['detail'])
            self.assertIn('job-1', run['detail'])
            for not_ours in ('someone-elses-job', 'old-job'):
                self.assertNotIn(not_ours, run['detail'])
                self.assertEqual(RestStub.jobs[not_ours]['state'], 'RUNNING')
            self.assertEqual(RestStub.jobs['job-1']['state'], 'CANCELED')
        with self.subTest(how='submitted-jids (parsed from the runner output)'):
            submitted = 'f' * 32
            proc = self.start_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                   + self.common_args(),
                                   switches={'RUNNER_SLEEP': '1', 'RUNNER_JID': submitted})
            self.wait_for_runner(proc)
            RestStub.add_job(submitted, start_ms=future())
            RestStub.add_job('decoy-job', start_ms=future())
            out, _ = proc.communicate(timeout=120)
            run = json.loads(out)[KEY]['runs'][0]
            self.assertIn('cancelled the job(s) this cell submitted over REST (jid parsed '
                          'from the runner output): %s (nexmark %s)' % (submitted, submitted),
                          run['detail'])
            self.assertNotIn('decoy-job', run['detail'])
            self.assertEqual(RestStub.jobs[submitted]['state'], 'CANCELED')
            self.assertEqual(RestStub.jobs['decoy-job']['state'], 'RUNNING')
        with self.subTest(how='time-window (no baseline, imprecise fallback)'):
            RestStub.refuse_overview = 1  # the cell's own baseline call is the first overview
            proc = self.start_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                   + self.common_args(), switches={'RUNNER_SLEEP': '1'})
            self.wait_for_runner(proc)
            RestStub.add_job('job-1', start_ms=future())
            out, _ = proc.communicate(timeout=120)
            run = json.loads(out)[KEY]['runs'][0]
            self.assertIn('cancel by time window (imprecise', run['detail'])
            self.assertIn('job-1', run['detail'])
            self.assertEqual(RestStub.jobs['job-1']['state'], 'CANCELED')

    def test_timeout_cancel_refusals_and_old_jobs_left_alone(self):
        RestStub.add_job('old-job', start_ms=int(time.time() * 1000) - 3600000)
        proc = self.start_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                               + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.wait_for_runner(proc)
        RestStub.add_job('stuck-job', start_ms=int(time.time() * 1000) + 10000, fail_cancel=True)
        out, _ = proc.communicate(timeout=120)
        payload = json.loads(out)
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('could not be cancelled over REST (REST answered 500 for stuck-job)', run['detail'])
        self.assertIn('cancel it by hand', run['detail'])
        self.assertEqual(RestStub.jobs['old-job']['state'], 'RUNNING')
        self.assertEqual(RestStub.jobs['stuck-job']['state'], 'RUNNING')

    def test_failed_and_pending_cells_keep_the_full_key_set(self):
        # runs[] keys are constant whatever happened to the cell (join-table.md §8):
        # a timed-out and a not-run cell report the same keys, values nulled out
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                      + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.assertEqual(proc.returncode, 1)
        timeout_cell, not_run_cell = payload[KEY]['runs'][0], payload[KEY]['runs'][1]
        for run in (timeout_cell, not_run_cell):
            self.assertTrue(RUN_BASE_KEYS <= set(run), sorted(set(RUN_BASE_KEYS) - set(run)))
            self.assertIsNone(run['runner_exit'])
            self.assertIsNone(run['metrics'])
            self.assertIsNone(run['gluten_nodes'])
            self.assertIsNone(run['join_gluten_nodes'])
            self.assertFalse(run['comparable'])
            self.assertFalse(run['plan_checked'])
        for key in ('table', 'submit_log'):  # the attempted cell knows its evidence paths too
            self.assertIn(key, timeout_cell)

    def test_runner_success_without_table_is_failure(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_NO_TABLE': '1'})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('FINISHED', payload[KEY]['runs'][0]['detail'])

    def test_events_off_calibre_not_comparable(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_SMALL_CALLS': '1'})
        self.assertEqual(proc.returncode, 0)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'done')
        self.assertFalse(run['comparable'])
        self.assertIn('not comparable', run['detail'])
        # and out of the summary: the query mean drops the cell, the group
        # average is the mean of the query means that remain
        self.assertIsNone(self.analysis_of(payload, 'q3')['throughput_avg'])
        self.assertEqual(self.summary_of(payload, 'regular')['avg'], 2352941.2)  # q20 alone
        self.assertEqual(self.summary_of(payload, 'regular')['cell_count'], 1)

    # --- evidence-file guards (the --out rule, per file) ------------------------
    def test_evidence_symlink_is_not_written_through(self):
        victim = self.base / 'victim.out'
        victim.write_text('precious evidence from an earlier tool\n', encoding='utf-8')
        out = self.evidence('q3.p1.out')
        os.makedirs(str(out.parent), exist_ok=True)
        out.symlink_to(victim)
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        # the same resolved-path judgment as --out: real != abspath means a
        # symlink component, never written through (even pointing back inside)
        self.assertIn('q3.p1.out resolves to', run['detail'])
        self.assertIn('a path with a symlink component is never written through', run['detail'])
        self.assertTrue(os.path.islink(str(out)))  # the link itself survives
        self.assertEqual(victim.read_bytes(), b'precious evidence from an earlier tool\n')
        self.assertFalse((self.machine['capture'] / 'started').exists())  # the runner never ran
        self.assertFalse(self.evidence('q3.p1.submit.log').exists())
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])

    def test_evidence_fifo_fails_the_cell_without_hanging(self):
        # a fifo at the submit.log would block the write after a whole cell's
        # benchmark: the cell fails on the guard instead, before the runner starts
        fifo = self.evidence('q3.p1.submit.log')
        os.makedirs(str(fifo.parent), exist_ok=True)
        os.mkfifo(str(fifo))
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('q3.p1.submit.log exists and is not a regular file', run['detail'])
        self.assertIn('fifo', run['detail'])
        self.assertTrue(stat.S_ISFIFO(os.stat(str(fifo)).st_mode))  # the fifo survives
        self.assertFalse((self.machine['capture'] / 'started').exists())
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())

    def test_evidence_parent_dir_linking_outside_the_project_is_refused(self):
        # the resolved-path rule, --out's own: e2e itself a symlink to a directory
        # outside the project makes every evidence path under it resolve outside
        # — the final component being clean changes nothing, and the symlink
        # component alone refuses the write (even before the containment looks);
        # without the rule the whole cell's table would land on the other side
        # of the link
        outside = self.base / 'outside-evidence'
        join_outside = outside / 'perf' / 'join'
        join_outside.mkdir(parents=True)
        victim = join_outside / 'q3.p1.out'
        victim.write_text('an earlier tool\'s evidence\n', encoding='utf-8')
        (self.machine['project'] / 'e2e').symlink_to(outside)  # the whole tree links out
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('q3.p1.out resolves to', run['detail'])
        self.assertIn('a path with a symlink component is never written through', run['detail'])
        self.assertTrue(os.path.islink(str(self.machine['project'] / 'e2e')))  # untouched
        self.assertEqual(victim.read_text(encoding='utf-8'), 'an earlier tool\'s evidence\n')
        self.assertFalse((self.machine['capture'] / 'started').exists())  # the runner never ran
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())

    def test_evidence_swapped_mid_cell_is_not_written_through(self):
        # the paths were clean when the cell started; the runner swapped them to a
        # symlink / a fifo before finishing — the write-time re-check refuses
        # instead of writing a whole cell's benchmark through the link / blocking
        for switch, target in (('RUNNER_SWAP_EVIDENCE', 'q3.p1.out'),
                               ('RUNNER_FIFO_EVIDENCE', 'q3.p1.submit.log')):
            with self.subTest(switch=switch):
                shutil.rmtree(self.base, ignore_errors=True)  # a clean machine per subTest
                self.machine = self.build_machine()
                if switch == 'RUNNER_SWAP_EVIDENCE':
                    (self.machine['capture'] / 'victim.out').write_text('precious\n', encoding='utf-8')
                proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1']
                                              + self.common_args(), switches={switch: '1'})
                self.assertEqual(proc.returncode, 1)
                run = payload[KEY]['runs'][0]
                self.assertEqual(run['status'], 'failed')
                self.assertIn('changed under the cell', run['detail'])
                if switch == 'RUNNER_SWAP_EVIDENCE':
                    self.assertIn('resolves to', run['detail'])  # the swapped-in link, by the resolved-path rule
                    self.assertIn('never written through', run['detail'])
                    self.assertEqual((self.machine['capture'] / 'victim.out').read_text(
                        encoding='utf-8'), 'precious\n')  # not written through
                    self.assertTrue(os.path.islink(str(self.evidence(target))))
                else:
                    self.assertIn('%s exists and is not a regular file' % target, run['detail'])
                    self.assertTrue(stat.S_ISFIFO(os.stat(str(self.evidence(target))).st_mode))
                if switch == 'RUNNER_SWAP_EVIDENCE':
                    self.assertFalse(self.evidence('q3.p1.submit.log').exists())  # neither file written
                else:
                    self.assertFalse(self.evidence('q3.p1.out').exists())  # the fifo is never opened
                self.assertTrue(payload[KEY]['confs_restored'])

    def test_sigterm_restores_the_confs_and_cancels_the_cell_jobs(self):
        # symmetric with the timeout path: an interrupted cell's cluster job may
        # live on past the killed runner — it is cancelled too, and the outcome
        # recorded in the detail either way
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        original_flink = self.machine['flink_conf'].read_bytes()
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), '--json', 'run', '--yes', '--parallelism', '1']
            + self.common_args(), cwd=str(self.machine['project']),
            env=self.env({'RUNNER_SLEEP': '1'}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for_runner(proc)
        RestStub.add_job('job-1', start_ms=int(time.time() * 1000) + 10000)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(out)
        r = payload[KEY]
        self.assertFalse(r['success'])
        self.assertEqual(r['runs'][0]['status'], 'failed')
        self.assertIn('interrupted', r['runs'][0]['detail'])
        self.assertIn('cancelled the cluster job(s) that appeared since this cell began',
                      r['runs'][0]['detail'])
        self.assertEqual(RestStub.jobs['job-1']['state'], 'CANCELED')
        self.assertIsNone(r['runs'][0]['runner_exit'])  # interrupted cell: full key set too
        self.assertIsNone(r['runs'][0]['metrics'])
        self.assertFalse(r['runs'][0]['comparable'])
        self.assertTrue(r['confs_restored'])
        # restored to the byte (both confs), the locks released, no backups left
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(original_flink, self.machine['flink_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])
        self.assertEqual(self.held_locks(), [])

    # --- the run lock: two runs, one installation ------------------------------
    def test_a_live_flock_refuses_a_second_concurrent_run(self):
        # the lock is a kernel flock, not file content: two runs started at the same
        # moment cannot both pass (the check-then-unlink race is gone by design —
        # nobody ever unlinks the other's lock, the kernel arbitrates)
        first = self.start_tool(['run', '--yes', '--parallelism', '1', '--timeout', '8']
                                + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.wait_for_runner(first)
        original = self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf'])
        second, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(second.returncode, 2)
        self.assertIn('another run holds the lock', payload[KEY]['error_message'])
        self.assertIn('wait for it to finish', payload[KEY]['next_step'])
        self.assertEqual(original, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertEqual(payload[KEY]['changes'], [])  # the refusal did nothing at all
        # the first run is unaffected by the refused second one and finishes on its own
        out, _ = first.communicate(timeout=120)
        self.assertEqual(first.returncode, 1)  # its cell timed out — confs still restored
        first_payload = json.loads(out)
        self.assertTrue(first_payload[KEY]['confs_restored'])
        self.assertEqual(self.held_locks(), [])
        # plan is read-only: it neither locks nor cares about a held lock
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)

    def test_stale_lock_file_from_a_crashed_run_is_taken_over(self):
        # a crashed holder's flock evaporates with the process; the file it left
        # behind (any text in it, or none — a writer killed between create and
        # write) is simply truncated and rewritten by the next holder
        for content in ('4194304 2026-11-25T00:00:00\n', ''):  # dead pid, then an empty file
            with self.subTest(lock_content=repr(content)):
                self.machine = self.build_machine()
                lock = self.machine['flink_conf'].parent / ('config.yaml' + LOCK)
                lock.write_text(content, encoding='utf-8')
                proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1']
                                              + self.common_args())
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertTrue(payload[KEY]['confs_restored'])
                actions = ' '.join(c['action'] for c in payload[KEY]['changes'])
                self.assertIn('lock taken', actions)
                self.assertNotIn('stale lock replaced', actions)  # nothing is ever unlinked
                taken = ' '.join(c['detail'] for c in payload[KEY]['changes']
                                 if c['action'] == 'lock taken')
                self.assertRegex(taken, r'\(pid \d+, kernel flock held')
                self.assertTrue(lock.is_file())  # the file persists after release — by design
                self.assertNotIn('4194304', lock.read_text(encoding='utf-8'))
                self.assertEqual(self.held_locks(), [])  # released when the run ended

    def test_a_symlinked_lock_file_is_refused(self):
        victim = self.base / 'victim.lock'
        victim.write_text('precious\n', encoding='utf-8')
        lock = self.machine['flink_conf'].parent / ('config.yaml' + LOCK)
        lock.symlink_to(victim)
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('run lock is never taken through a link', payload[KEY]['error_message'])
        self.assertTrue(os.path.islink(str(lock)))  # untouched, never written through
        self.assertEqual(victim.read_text(encoding='utf-8'), 'precious\n')
        self.assertEqual(self.no_backups(), [])
        self.assertFalse((self.machine['capture'] / 'started').exists())

    # --- leftovers and lost originals -----------------------------------------
    def test_leftover_backup_is_repaired_first(self):
        original = b'nexmark.workload.suite.user.events.num: 5\n'
        self.machine['nexmark_conf'].write_bytes(original)
        (self.machine['nexmark'] / 'conf' / ('nexmark.yaml' + BAK)).write_bytes(original)
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertIn('leftover from a crashed run',
                      ' '.join(c['detail'] for c in payload[KEY]['changes']))
        self.assertEqual(original, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])

    def test_lost_original_is_refused(self):
        fixed = (ROOT / 'conf' / 'nexmark-bench.yaml').read_text(encoding='utf-8')
        self.machine['nexmark_conf'].write_text(fixed, encoding='utf-8')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('benchmark conf and the original backup is gone', payload[KEY]['error_message'])
        self.assertIn('restore conf/nexmark.yaml by hand', payload[KEY]['next_step'])
        self.assertEqual(self.no_backups(), [])

    def test_performance_verify_leftover_is_also_refused(self):
        # the two skills share the fixed conf byte for byte: a crashed
        # performance-verify run leaves the same text — this skill refuses
        # instead of claiming it (it only ever restores its own .bak)
        fixed = (PERF_SKILL / 'conf' / 'nexmark-bench.yaml').read_text(encoding='utf-8')
        self.machine['nexmark_conf'].write_text(fixed, encoding='utf-8')
        (self.machine['nexmark'] / 'conf' / ('nexmark.yaml' + PERF_BAK)).write_bytes(b'user original\n')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('original backup is gone', payload[KEY]['error_message'])
        # the twin's backup is left exactly where it was, untouched
        self.assertEqual((self.machine['nexmark'] / 'conf' / ('nexmark.yaml' + PERF_BAK)).read_bytes(),
                         b'user original\n')
        self.assertFalse((self.machine['nexmark_conf'].parent / ('nexmark.yaml' + BAK)).exists())

    # --- confs_restored tells the truth ------------------------------------------
    def test_missing_backup_reports_confs_not_restored(self):
        # the runner hides the nexmark backup mid-cell (another consumer of the .bak):
        # the flink conf restores, the nexmark conf stays swapped and the receipt
        # says exactly that instead of a happy confs_restored=true
        original_flink = self.machine['flink_conf'].read_bytes()
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_STEAL_BAK': '1'})
        self.assertEqual(proc.returncode, 1)
        r = payload[KEY]
        self.assertFalse(r['success'])
        self.assertFalse(r['confs_restored'])
        self.assertIn('confs NOT restored: %s' % self.machine['nexmark_conf'], r['error_message'])
        self.assertIn('restore the conf(s) named in the error', r['next_step'])
        self.assertEqual(original_flink, self.machine['flink_conf'].read_bytes())
        self.assertNotEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertIn('suite.bench.events.num: 100000000',
                      self.machine['nexmark_conf'].read_text(encoding='utf-8'))
        stolen = self.machine['capture'] / 'stolen.bak'  # moved, not deleted: recoverable
        self.assertEqual(stolen.read_bytes(), original_nexmark)
        self.assertEqual(self.no_backups(), [])
        self.assertEqual(self.held_locks(), [])

    # --- the restore gives the file its owner back -------------------------------
    def test_restore_chowns_the_original_owner_back(self):
        # copystat copies neither owner nor group: an admin-run would otherwise
        # leave the conf owned by the admin and the service account could lose
        # access. The uid/gid seen at backup time are chowned back after the
        # byte-verified restore, and the receipt says so.
        root = os.geteuid() == 0  # only root can hand a file to another uid
        if root:
            os.chown(str(self.machine['nexmark_conf']), 65534, 65534)  # nobody:nogroup
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        if root:
            info = os.stat(str(self.machine['nexmark_conf']))
            self.assertEqual((info.st_uid, info.st_gid), (65534, 65534))  # the owner came back
        owners = [c for c in payload[KEY]['changes'] if c['action'] == 'owner restored']
        self.assertEqual(len(owners), 2)  # both confs, and never a failure entry
        self.assertIn('uid', ' '.join(c['detail'] for c in owners))
        self.assertFalse([c for c in payload[KEY]['changes']
                          if c['action'] == 'owner NOT restored'])

    # --- backup atomicity --------------------------------------------------------
    def test_backup_is_atomic_when_the_write_fails(self):
        # RLIMIT_FSIZE breaks the backup copy mid-write (EFBIG): a half .bak must not
        # survive it — the next run would restore it over the good conf
        padded = self.machine['flink_conf'].read_text(encoding='utf-8') + '# ' + ('padding ' * 300) + '\n'
        self.machine['flink_conf'].write_text(padded, encoding='utf-8')
        original = self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf'])

        def cap():
            resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
        proc = subprocess.run([sys.executable, str(SCRIPT), '--json', 'run', '--yes',
                               '--parallelism', '1'] + self.common_args(),
                              cwd=str(self.machine['project']), env=self.env(),
                              capture_output=True, text=True, timeout=180, preexec_fn=cap)
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(payload[KEY]['error_message'].startswith('tool aborted'))
        # a crash cannot know the restore state: null, not a guess either way
        self.assertIsNone(payload[KEY]['confs_restored'])
        self.assertIn('aborted before the restore could be verified', payload[KEY]['next_step'])
        self.assertEqual(self.no_backups(), [])   # no .bak — complete or absent, never half
        self.assertEqual(self.no_tmps(), [])      # and no temporary left behind
        self.assertEqual(original, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertEqual(self.held_locks(), [])
        self.assertFalse((self.machine['capture'] / 'started').exists())

    # --- symlink guards ----------------------------------------------------------
    def make_link(self, path, real_name):
        """Turn a regular conf into a symlink to a real file next to it (Kubernetes /
        shared-config shape); returns the real file's path."""
        real = path.parent / real_name
        os.rename(str(path), str(real))
        path.symlink_to(real)
        return real

    def assert_conf_untouched(self, conf, real, before):
        self.assertTrue(os.path.islink(str(conf)), 'the symlink itself must survive')
        self.assertEqual(before, real.read_bytes())
        self.assertEqual(self.no_backups(), [])

    def test_refuses_symlinked_confs(self):
        for conf_key in ('flink_conf', 'nexmark_conf'):
            machine = self.build_machine()
            real = self.make_link(machine[conf_key], 'real-%s.yaml' % conf_key)
            before = real.read_bytes()
            for args in (['plan'], ['run', '--yes']):
                proc, payload = self.run_tool(args + ['--parallelism', '1', '--flink-home',
                                                      str(machine['flink']), '--nexmark-home',
                                                      str(machine['nexmark'])],
                                              switches={'CAPTURE': str(machine['capture'])})
                self.assertEqual(proc.returncode, 2, (conf_key, args))
                self.assertIn('is a symbolic link', payload[KEY]['error_message'])
                self.assertIn('never through a link', payload[KEY]['error_message'])
                self.assertIn('regular file', payload[KEY]['next_step'])
            self.assertTrue(os.path.islink(str(machine[conf_key])))
            self.assertEqual(before, real.read_bytes())
            self.assertFalse((machine['capture'] / 'started').exists())

    def test_refuses_symlinked_backup_and_never_moves_it(self):
        victim = self.base / 'precious.yaml'
        victim.write_text('precious content\n', encoding='utf-8')
        bak = self.machine['nexmark'] / 'conf' / ('nexmark.yaml' + BAK)
        bak.symlink_to(victim)
        original = self.machine['nexmark_conf'].read_bytes()
        for args in (['plan'], ['run', '--yes']):
            proc, payload = self.run_tool(args + ['--parallelism', '1'] + self.common_args())
            self.assertEqual(proc.returncode, 2, args)
            self.assertIn(BAK + ' is a symbolic link', payload[KEY]['error_message'])
            self.assertIn('remove it', payload[KEY]['next_step'])
            self.assertTrue(os.path.islink(str(bak)))
            self.assertEqual(victim.read_bytes(), b'precious content\n')
            self.assertEqual(original, self.machine['nexmark_conf'].read_bytes())
            self.assertFalse((self.machine['capture'] / 'started').exists())

    # --- least-privilege fix hints ------------------------------------------------
    def test_unwritable_conf_fix_is_minimal_privilege(self):
        for conf in (self.machine['flink_conf'], self.machine['nexmark_conf']):
            os.chmod(str(conf), 0o444)
        argv = [sys.executable, str(SCRIPT), '--json', 'plan', '--parallelism', '1'] + self.common_args()
        if os.geteuid() == 0:  # root writes through 0444: run the child as nobody
            if not shutil.which('setpriv'):
                self.skipTest('setpriv unavailable to drop privileges as root')
            argv = ['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups'] + argv
        proc = subprocess.run(argv, cwd=str(self.machine['project']), env=self.env(),
                              capture_output=True, text=True, timeout=180)
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('cannot write', payload[KEY]['error_message'])
        self.assertIn('installing owner', payload[KEY]['next_step'])
        self.assertIn('admin', payload[KEY]['next_step'])
        self.assertNotIn('sudo', payload[KEY]['next_step'])
        self.assertNotIn('--yes', payload[KEY]['next_step'])
        self.assertFalse((self.machine['capture'] / 'started').exists())

    def test_the_script_assembles_no_root_command(self):
        # the whole tool stays free of privilege escalation: no sudo anywhere, so no
        # fix hint can ever hand out a root rerun with --yes already in it
        self.assertNotIn('sudo', SCRIPT.read_text(encoding='utf-8'))

    def test_the_plan_window_comes_from_the_plan_skew_constant(self):
        # PLAN_SKEW_MS is the single adjustment point for the skew allowance: the
        # three window computations reference it, no bare 'minus 2 seconds' magic
        # number hides beside the constant
        script = SCRIPT.read_text(encoding='utf-8')
        self.assertNotIn('started_wall - 2', script)
        self.assertGreaterEqual(script.count('PLAN_SKEW_MS'), 4)  # the definition + the uses

    # --- terminology --------------------------------------------------------------
    def test_terminology_official_name_and_no_bare_abbreviation(self):
        skill_md = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('## 术语', skill_md)
        self.assertIn('GlutenFlink', skill_md)  # the official name is spelled out
        for doc in (skill_md, (ROOT / 'references' / 'join-table.md').read_text(encoding='utf-8')):
            self.assertIsNone(re.search(r'\bGFV\b', doc), 'the bare abbreviation must not appear')
        # the judgement never reads the description: engine fields only
        script = SCRIPT.read_text(encoding='utf-8')
        self.assertIn('gluten_mention', script)
        self.assertIn('deliberately excluded', script)

    # --- --out -----------------------------------------------------------------
    def test_out_guardrails(self):
        # --parallelism 1: the refusal under test must come from --out, not from a gate
        guarded = [
            (['run', '--yes', '--parallelism', '1', '--out', '../outside.json'] + self.common_args(),
             'project directory'),
            (['run', '--yes', '--parallelism', '1', '--out', 'taken.json'] + self.common_args(),
             'already exists'),
        ]
        (self.machine['project'] / 'taken.json').write_text('x', encoding='utf-8')
        for args, expected in guarded:
            proc, payload = self.run_tool(args)
            self.assertEqual(proc.returncode, 2, args)
            self.assertIn(expected, proc.stderr + proc.stdout)
        # a fifo is refused even with --overwrite
        fifo = self.machine['project'] / 'fifo.json'
        os.mkfifo(str(fifo))
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--out', 'fifo.json',
                                       '--overwrite'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('not a regular file', proc.stderr + proc.stdout)
        # a symlinked --out is refused
        link = self.machine['project'] / 'link.json'
        (self.machine['project'] / 'real.json').write_text('x', encoding='utf-8')
        link.symlink_to(self.machine['project'] / 'real.json')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--out', 'link.json',
                                       '--overwrite'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        # nothing ran while refusing
        self.assertFalse((self.machine['capture'] / 'started').exists())
        self.assertEqual(self.no_backups(), [])

    def test_out_link_dotdot_escape_refused(self):
        # a lexical fold sees link/../x as <cwd>/x and would pass, but the
        # kernel resolves the link first: the write would land in the link
        # target's parent (the reviewer's PoC — --out link/../escaped.json).
        # The judge and the open share one resolved realpath, so the refusal
        # fires before anything runs; the link points at a dedicated directory
        # outside the project, so the kernel landing spot is <base>/
        # escaped.json exactly — and one level further out is watched too:
        # neither may be created or truncated.
        target = self.base / 'outside-target'
        target.mkdir()
        escape = self.base / 'escaped.json'
        further = self.base.parent / 'escaped.json'
        (self.machine['project'] / 'link').symlink_to(target)
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1',
                                       '--out', 'link/../escaped.json'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('a path with a symlink component is never written through',
                      proc.stderr + proc.stdout)
        self.assertFalse((self.machine['capture'] / 'started').exists())  # the runner never ran
        self.assertFalse(escape.exists())  # the kernel landing spot: no file
        self.assertFalse(further.exists())  # one level further out: none either
        self.assertEqual(self.no_backups(), [])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())  # never touched

    def test_out_normal_nested_path_still_writes(self):
        # regression for the unified judge/open path: an honest nested --out
        # whose directory does not exist yet (realpath tolerates a missing
        # tail) is created and written as before — the resolved-path open
        # changes nothing for the clean case
        out = self.machine['project'] / 'reports' / 'result.json'
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1',
                                       '--out', 'reports/result.json'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        written = json.loads(out.read_text(encoding='utf-8'))
        self.assertTrue(written[KEY]['success'])
        self.assertEqual(written[KEY]['runs'][0]['status'], 'done')
        self.assertTrue(self.evidence('q3.p1.out').exists())  # the run itself completed too

    # --- crash shape -----------------------------------------------------------
    def test_tool_abort_result_has_the_full_key_set(self):
        # a directory at the flink-conf backup path makes the backup crash half-way in:
        # nothing has been swapped yet, and the result still carries every key
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        os.makedirs(str(self.machine['flink_conf']) + BAK)
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        r = payload[KEY]
        self.assertEqual(set(r), RESULT_KEYS, r)
        self.assertTrue(r['error_message'].startswith('tool aborted'))
        # the crash cannot tell whether the confs were restored: null (unknown),
        # never a guessed false — and the next_step says to check by hand
        self.assertIsNone(r['confs_restored'])
        self.assertIn('aborted before the restore could be verified', r['next_step'])
        self.assertEqual(r['runs'], [])
        self.assertEqual(r['join_analysis'], [])
        self.assertEqual(r['summary'], [])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])

    def test_refused_runs_carry_confs_restored_true(self):
        # the run contract holds on the refusal branches too: nothing was touched,
        # so the confs on disk are the originals — confs_restored true (present,
        # not missing); plan keeps omitting the key
        refusals = [
            ['run', '--parallelism', '1'] + self.common_args(),  # no --yes
            ['run', '--yes', '--flink-home', str(self.base / 'nowhere'),
             '--nexmark-home', str(self.machine['nexmark'])],  # missing prerequisite
        ]
        for args in refusals:
            proc, payload = self.run_tool(args)
            self.assertEqual(proc.returncode, 2, args)
            r = payload[KEY]
            self.assertIn('confs_restored', r, args)
            self.assertTrue(r['confs_restored'], args)
        # a live lock refusal: a second run while the first holds the flock
        first = self.start_tool(['run', '--yes', '--parallelism', '1', '--timeout', '8']
                                + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.wait_for_runner(first)
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('another run holds the lock', payload[KEY]['error_message'])
        self.assertTrue(payload[KEY]['confs_restored'])  # never touched: originals
        first.communicate(timeout=120)
        # and plan omits the key (it never touches the confs at all)
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn('confs_restored', payload[KEY])

    # --- the suggested command runs as printed ---------------------------------
    def test_the_suggested_command_runs_as_printed(self):
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        marker = 'then run '
        self.assertIn(marker, payload[KEY]['next_step'])
        command = payload[KEY]['next_step'].split(marker, 1)[1].strip()
        self.assertIn('--flink-home', command)
        other = self.base / 'project-b'
        other.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(['bash', '-c', command], cwd=str(other), env=self.env(),
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((other / 'e2e' / 'perf' / 'join' / 'q3.p1.out').exists())

    def test_parallelism_must_be_positive_integers(self):
        proc, payload = self.run_tool(['plan', '--parallelism', '0,x'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('parallelism', proc.stderr)

    def test_command_includes_skip_check_when_used(self):
        proc, payload = self.run_tool(['plan', '--parallelism', '1', '--skip-check']
                                      + self.common_args())
        self.assertEqual(proc.returncode, 0)
        self.assertIn('--skip-check', payload[KEY]['next_step'])

    # --- result.json is the real verdict, never a constant ----------------------
    @unittest.skipUnless(os.environ.get('JOIN_CONTRACT_INJECT_FAILURE') == '1',
                         'failure injection: only the result.json wiring self-test runs this')
    def test_injected_failure_flips_the_result_json(self):
        self.fail('injected failure — this run must land success=false in result.json '
                  'and exit 1, never a hardcoded success')

    def test_result_json_reflects_the_real_outcome(self):
        # the judgment harness pins itself: with a failing test injected, the child
        # run's result.json must say success=false and exit 1 — the case.yaml
        # `json_field: success expect true` then guards the real wiring, not a
        # constant (the child writes its result.json in an isolated results dir)
        child_results = self.base / 'child-results'
        env = dict(os.environ, JOIN_CONTRACT_INJECT_FAILURE='1',
                   JOIN_CONTRACT_RESULTS=str(child_results))
        proc = subprocess.run([sys.executable, str(CASE_DIR / 'run.py'),
                               'JoinCase.test_injected_failure_flips_the_result_json'],
                              cwd=str(self.machine['project']), env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        written = json.loads((child_results / 'result.json').read_text(encoding='utf-8'))
        self.assertIs(written['success'], False)
        self.assertEqual(written['failures'], 1)
        self.assertIn('injected failure', written['first_failure'])


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    module = sys.modules[__name__]
    loader = unittest.defaultTestLoader
    names = sys.argv[1:]
    suite = (loader.loadTestsFromNames(names, module) if names
             else loader.loadTestsFromModule(module))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    first_failure = None
    if result.failures or result.errors:
        _, text = (result.failures + result.errors)[0]
        first_failure = str(text).strip().splitlines()[-1]
    report = {
        'case': 'join-contract',
        'skill': 'flink-velox-nexmark-join',
        'success': result.wasSuccessful(),
        'tests': result.testsRun,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'first_failure': first_failure,
    }
    (RESULTS / 'result.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
