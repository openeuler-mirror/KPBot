#!/usr/bin/env python3
"""bench-contract — tier-A case for flink-velox-performance-verify.

Drives scripts/performance_verify.py (plan / run) against a fake machine laid out
in a scratch directory: a fake Flink home (bin/flink, conf/config.yaml — the one
Flink reads on this line — conf/workers, lib with the dist and nexmark jars), a
fake nexmark tree whose bin/run_query.sh is a stub recording the confs it sees
and printing a canned "Nexmark Results" table (with switches for exit-failure,
no-table, wrong-query-row, small events and sleep), and a real local HTTP stub
answering /overview so the cluster liveness probe meets a live endpoint. The
script only ever runs in a child process with explicit --flink-home /
--nexmark-home and FLINK_HOME / NEXMARK_HOME overridden — nothing from the real
machine is in play, and the runner stub is a child of that child.

Pins: plan prints the matrix and the conf preview and touches nothing (byte
identity of both confs); the native-side gate (gluten/velox jars in lib refuse
with the cluster-switch fix); slot / REST / runner / query-set refusals; the
effective-conf rule (flink-conf.yaml wins over config.yaml, no merge); run
needing --yes; the full run contract (nexmark conf swapped in as a whole with
this cluster's REST endpoint derived from the conf Flink reads, parallelism.default
and pipeline.object-reuse set per cell, evidence tree + metrics parsing, events off
the calibre flagged not comparable, a throughput column that parses to None (n/a)
or a non-positive time flagged not comparable with the reason in the detail, r0 and
r1 with --object-reuse, the first failure stopping the matrix with the rest NOT RUN,
the runner's missing table counting as failure); a timed-out cell dropping the
runner's partial output as its *.submit.log (or saying there was none), cancelling
the cluster jobs still RUNNING since the cell started over the stub REST (and
recording a refused or absent cancel as-is, leaving older jobs alone), and every
failed cell — timeout, start failure, interrupt — carrying the full runs[] key set
with runner_exit/metrics/comparable as None/None/False; the evidence files guarded
per file like --out (a symlinked .out is never written through, a fifo at the
.submit.log fails the cell before the runner even starts); the unwritable-conf fix
leading with least privilege (installing owner / admin adjusts permissions —
no root command assembled anywhere, no --yes pre-filled); confs and backup paths
that are symbolic links refused by plan and run alike, the link and its target
left untouched; the run lock beside each conf (a live holder refuses the run, a
dead holder's stale lock is replaced and recorded, plan never locks); the backup
published atomically (a mid-copy failure under RLIMIT_FSIZE leaves no .bak and no
temporary behind, confs untouched); the confs restored after success, failure and
SIGTERM — byte-identical and confs_restored telling the truth (a restore whose
backup was consumed reports confs NOT restored with the path, the swapped conf
left in place), repaired first when a previous crashed run left its backups, with
a refusal when the original is gone; --out and the evidence directory refused
outside the project / as symlinks / as an existing file without --overwrite / as
a fifo even with --overwrite; the crash result carrying the full key set with tool
aborted; and the suggested command running as printed from another directory.

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
SCRIPT = ROOT / 'scripts' / 'performance_verify.py'
RESULTS = CASE_DIR / 'results'
SCRATCH = RESULTS / 'scratch'

KEY = 'flink_velox_performance_verify_result'
BAK = '.flink-velox-perf.bak'
LOCK = '.flink-velox-perf.lock'
SLOTS_PER_TM = 8

RESULT_KEYS = {'success', 'checked_at', 'command', 'side', 'flink_home', 'nexmark_home',
               'effective_conf', 'rest', 'slots', 'calibre', 'queries', 'parallelism',
               'object_reuse', 'timeout', 'evidence_dir', 'runs', 'changes',
               'confs_restored', 'next_step', 'error_message'}

RUNNER_STUB = textwrap.dedent('''\
    #!/usr/bin/env bash
    # fake run_query.sh: records the confs it runs under, then prints a canned
    # "Nexmark Results" table; switches (RUNNER_FAIL / RUNNER_SLEEP / RUNNER_NO_TABLE /
    # RUNNER_WRONG_QUERY / RUNNER_SMALL_EVENTS / RUNNER_STEAL_BAK /
    # RUNNER_NA_THROUGHPUT / RUNNER_ZERO_TIME) come through the environment
    n="$(cat "$CAPTURE/count" 2>/dev/null || echo 0)"; n=$((n + 1)); echo "$n" > "$CAPTURE/count"
    cp "$NEXMARK_HOME/conf/nexmark.yaml" "$CAPTURE/nexmark.$n.yaml"
    cp "$FLINK_HOME/conf/config.yaml" "$CAPTURE/flinkconf.$n.yaml" 2>/dev/null || true
    {
      echo "--- call $n: category=$1 query=$q"
      grep -E '^(parallelism.default|pipeline.object-reuse):' "$FLINK_HOME/conf/config.yaml"
    } >> "$CAPTURE/calls.log"
    echo "$n" > "$CAPTURE/started"
    [ "${RUNNER_STEAL_BAK:-}" = 1 ] && mv "$NEXMARK_HOME/conf/nexmark.yaml.flink-velox-perf.bak" "$CAPTURE/stolen.bak" 2>/dev/null || true
    [ "${RUNNER_FAIL:-}" = 1 ] && { echo "runner: boom" >&2; exit 7; }
    if [ "${RUNNER_SLEEP:-}" = 1 ]; then
      [ "${RUNNER_QUIET:-}" = 1 ] || echo "runner: job submitted, stub output before the long sleep"
      sleep 300
    fi
    [ "${RUNNER_NO_TABLE:-}" = 1 ] && exit 0
    if [ "${RUNNER_SMALL_EVENTS:-}" = 1 ]; then EV=1000; else EV=100000000; fi
    if [ "${RUNNER_WRONG_QUERY:-}" = 1 ]; then ROW="q99"; else ROW="$2"; fi
    if [ "${RUNNER_ZERO_TIME:-}" = 1 ]; then TS="0.000"; else TS="42.500"; fi
    if [ "${RUNNER_NA_THROUGHPUT:-}" = 1 ]; then TP="n/a"; else TP="2352941.2"; fi
    echo "-------------------------------- Nexmark Results --------------------------------"
    echo ""
    echo "+-------+------------+-------+---------+-----------------+------------+-------------------+"
    echo "| Query | Events Num | Cores | Time(s) | Cores * Time(s) | Throughput | Throughput/Cores |"
    echo "+-------+------------+-------+---------+-----------------+------------+-------------------+"
    printf "| %-5s | %-10d | %-5d | %-7s | %-15.3f | %-10s | %-17.1f |\\n" "$ROW" "$EV" 8 "$TS" 340.000 "$TP" 294117.6
    echo "+-------+------------+-------+---------+-----------------+------------+-------------------+"
    exit 0
    ''')


class RestStub(threading.Thread):
    """A live /overview endpoint for the liveness probe, plus the job side the
    timeout-cancel path talks to: GET /jobs/overview lists the registered jobs and
    PATCH /jobs/<jid>/cancel cancels one (a job can be registered to refuse, the way
    a real cluster answers 5xx)."""

    jobs = {}
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

    def __init__(self):
        super().__init__(daemon=True)

        def reply(handler, code, body):
            handler.send_response(code)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        def do_get(handler):
            if handler.path == '/jobs/overview':
                with RestStub.guard:
                    jobs = [{k: v for k, v in job.items() if k != 'fail_cancel'}
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
    """The child environment: no FLINK_HOME / NEXMARK_HOME / RUNNER_* from this machine."""
    env = {k: v for k, v in os.environ.items()
           if k not in ('FLINK_HOME', 'NEXMARK_HOME') and not k.startswith('RUNNER_')}
    env.update(extra)
    return env


class BenchCase(unittest.TestCase):
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
        if RESULTS.exists():
            (RESULTS / 'result.json').write_text(
                json.dumps({'success': True, 'case': 'bench-contract'}, indent=2), encoding='utf-8')

    def setUp(self):
        RestStub.reset()
        self.base = SCRATCH / self.id().split('.')[-1]
        self.machine = self.build_machine()

    # --- the fake machine ----------------------------------------------------
    def build_machine(self, slots=SLOTS_PER_TM):
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
            'jobmanager.rpc.address: localhost\n'
            'rest.address: 127.0.0.1\n'
            'rest.port: %d\n'
            'taskmanager.numberOfTaskSlots: %d\n'
            'parallelism.default: 1\n'
            'pipeline.object-reuse: false\n' % (self.rest.port, slots), encoding='utf-8')
        (flink / 'conf' / 'workers').write_text('localhost\n', encoding='utf-8')
        for jar in ('flink-dist-1.19.2.jar', 'nexmark-flink-1.0.jar'):
            (flink / 'lib' / jar).write_text('fake jar\n', encoding='utf-8')
        (nexmark / 'conf' / 'nexmark.yaml').write_text(
            'nexmark.workload.suite.100m.events.num: 10000\n'
            'nexmark.workload.suite.100m.tps: 2000\n'
            'nexmark.workload.suite.100m.queries: "q0,q1,q2"\n'
            'flink.rest.address: localhost\n'
            'flink.rest.port: 8188\n', encoding='utf-8')
        runner = nexmark / 'bin' / 'run_query.sh'
        runner.write_text(RUNNER_STUB, encoding='utf-8')
        os.chmod(str(runner), 0o755)
        return {'flink': flink, 'nexmark': nexmark, 'project': project,
                'capture': capture, 'flink_conf': flink / 'conf' / 'config.yaml',
                'nexmark_conf': nexmark / 'conf' / 'nexmark.yaml'}

    def env(self, switches=None):
        e = tool_env(FLINK_HOME=str(self.machine['flink']),
                     NEXMARK_HOME=str(self.machine['nexmark']),
                     CAPTURE=str(self.machine['capture']),
                     CALL_LOG=str(self.machine['capture'] / 'calls.log'))
        e.update(switches or {})
        return e

    def run_tool(self, args, switches=None, cwd=None, timeout=120, as_json=True):
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

    def common_args(self, extra=None):
        return (['--flink-home', str(self.machine['flink']),
                 '--nexmark-home', str(self.machine['nexmark'])] + (extra or []))

    def snapshot(self, *paths):
        return {str(p): p.read_bytes() for p in paths}

    def no_backups(self):
        return [p for p in self.base.rglob('*' + BAK) if p.is_file()]

    def no_locks(self):
        return [p for p in self.base.rglob('*' + LOCK) if p.is_file()]

    def no_tmps(self):
        return [p for p in self.base.rglob('*.tmp-*')]

    def evidence(self, name):
        return self.machine['project'] / 'e2e' / 'perf' / 'native' / name

    # --- plan ----------------------------------------------------------------
    def test_plan_prints_matrix_and_touches_nothing(self):
        before = self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf'])
        args = ['plan', '--queries', 'q0', '--parallelism', '1'] + self.common_args()
        proc, payload = self.run_tool(args)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(payload[KEY]['success'])
        self.assertEqual(set(payload[KEY]), RESULT_KEYS - {'confs_restored'}, payload[KEY])
        self.assertEqual(payload[KEY]['rest']['port'], self.rest.port)
        self.assertEqual(payload[KEY]['slots'], {'tms': 1, 'per_tm': SLOTS_PER_TM, 'total': SLOTS_PER_TM})
        self.assertIn('run --yes', payload[KEY]['next_step'])
        self.assertIn(str(SCRIPT), payload[KEY]['next_step'])
        self.assertIn('--flink-home', payload[KEY]['next_step'])
        # the human-readable plan carries the conf preview and the matrix size
        rendered, _ = self.run_tool(args, as_json=False)
        self.assertEqual(rendered.returncode, 0)
        self.assertIn('swapped in', rendered.stdout)
        self.assertIn('restored afterwards', rendered.stdout)
        self.assertIn('PLAN: 1 cell(s)', rendered.stdout)
        self.assertEqual(before, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertEqual(self.no_backups(), [])

    def test_refuses_gluten_side_with_cluster_switch_fix(self):
        (self.machine['flink'] / 'lib' / 'gluten-flink.jar').write_text('g', encoding='utf-8')
        (self.machine['flink'] / 'lib' / 'velox4j-1.0.jar').write_text('v', encoding='utf-8')
        before = self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf'])
        proc, payload = self.run_tool(['plan'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(payload[KEY]['success'])
        self.assertIn('native side only', payload[KEY]['error_message'])
        self.assertIn('flink-velox-cluster-switch --to native', payload[KEY]['next_step'])
        self.assertEqual(before, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertFalse((self.machine['capture'] / 'started').exists())

    def test_refuses_when_parallelism_exceeds_slots(self):
        self.machine['flink_conf'].write_text(
            'rest.address: 127.0.0.1\nrest.port: %d\ntaskmanager.numberOfTaskSlots: 2\n' % self.rest.port,
            encoding='utf-8')
        proc, payload = self.run_tool(['plan', '--parallelism', '4'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('slot', payload[KEY]['error_message'])
        self.assertIn('lower --parallelism', payload[KEY]['next_step'])

    def test_refuses_when_rest_endpoint_dead(self):
        dead = self.base / 'dead-port'
        dead.mkdir(parents=True, exist_ok=True)
        self.machine['flink_conf'].write_text(
            'rest.address: 127.0.0.1\nrest.port: 1\ntaskmanager.numberOfTaskSlots: 8\n', encoding='utf-8')
        proc, payload = self.run_tool(['plan'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('REST', payload[KEY]['error_message'])
        self.assertIn('start the Flink cluster', payload[KEY]['next_step'])

    def test_refuses_when_nexmark_runner_missing(self):
        os.unlink(str(self.machine['nexmark'] / 'bin' / 'run_query.sh'))
        proc, payload = self.run_tool(['plan'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('nexmark runner', payload[KEY]['error_message'])
        self.assertIn('flink-velox-runtime-install', payload[KEY]['next_step'])

    def test_refuses_query_outside_the_fixed_suite(self):
        proc, payload = self.run_tool(['plan', '--queries', 'q6'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('not in the fixed suite', payload[KEY]['error_message'])
        self.assertIn('q23', payload[KEY]['next_step'])

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
        # config.yaml-only machine: flink-conf.yaml's port 1 is the one that counts → refused
        other = self.build_machine()
        (other['flink'] / 'conf' / 'flink-conf.yaml').write_text(
            'rest.address: 127.0.0.1\nrest.port: 1\n', encoding='utf-8')
        proc2 = subprocess.run(
            [sys.executable, str(SCRIPT), '--json', 'plan', '--parallelism', '1',
             '--flink-home', str(other['flink']), '--nexmark-home', str(other['nexmark'])],
            cwd=str(other['project']), env=self.env(), capture_output=True, text=True, timeout=120)
        payload2 = json.loads(proc2.stdout)
        self.assertEqual(proc2.returncode, 2)  # flink-conf.yaml says port 1: nothing answers there
        self.assertIn('REST', payload2[KEY]['error_message'])

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
        proc, payload = self.run_tool(['run', '--yes', '--queries', 'q0', '--parallelism', '1',
                                       '--out', 'result.json'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = payload[KEY]
        self.assertTrue(r['success'])
        self.assertTrue(r['confs_restored'])
        self.assertEqual(len(r['runs']), 1)
        run = r['runs'][0]
        self.assertEqual((run['query'], run['parallelism'], run['object_reuse']), ('q0', 1, False))
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['runner_exit'], 0)
        self.assertEqual(run['metrics']['events_num'], 100000000)
        self.assertEqual(run['metrics']['throughput'], 2352941.2)
        self.assertEqual(run['metrics']['time_s'], 42.5)
        self.assertTrue(run['comparable'])
        # evidence: the table and the full runner log
        table = self.evidence('q0.p1.r0.out').read_text(encoding='utf-8')
        self.assertIn('Nexmark Results', table)
        self.assertIn('| q0', table)
        self.assertIn('Nexmark Results',
                     self.evidence('q0.p1.r0.submit.log').read_text(encoding='utf-8'))
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

    def test_run_r0_and_r1_with_object_reuse(self):
        proc, payload = self.run_tool(['run', '--yes', '--queries', 'q0', '--parallelism', '1',
                                       '--object-reuse'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        r = payload[KEY]
        self.assertEqual([(x['query'], x['parallelism'], x['object_reuse']) for x in r['runs']],
                         [('q0', 1, False), ('q0', 1, True)])
        self.assertTrue(self.evidence('q0.p1.r0.out').exists())
        self.assertTrue(self.evidence('q0.p1.r1.out').exists())
        calls = (self.machine['capture'] / 'calls.log').read_text(encoding='utf-8')
        self.assertIn('pipeline.object-reuse: false', calls)
        self.assertIn('pipeline.object-reuse: true', calls)

    def test_first_failure_stops_the_matrix(self):
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--queries', 'q0,q1', '--parallelism', '1']
                                      + self.common_args(), switches={'RUNNER_FAIL': '1'})
        self.assertEqual(proc.returncode, 1)
        r = payload[KEY]
        self.assertFalse(r['success'])
        self.assertEqual(r['runs'][0]['status'], 'failed')
        self.assertIn('exited 7', r['runs'][0]['detail'])
        self.assertEqual(r['runs'][1]['status'], 'pending')
        self.assertIn('not run', r['runs'][1]['detail'])
        self.assertFalse(self.evidence('q1.p1.r0.out').exists())
        self.assertTrue(r['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertIn('matrix resumes', r['next_step'])

    def test_runner_timeout_fails_the_cell(self):
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                      + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('did not finish within 2s', run['detail'])
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])

    def test_timeout_cell_drops_partial_output_and_full_keys(self):
        # the runner is killed at the timeout, but what it printed until then is evidence
        # too: it lands as this cell's submit.log and the detail points at it
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                      + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        log = self.evidence('q0.p1.r0.submit.log')
        self.assertIn('stub output before the long sleep', log.read_text(encoding='utf-8'))
        self.assertIn('see %s' % log, run['detail'])
        self.assertFalse(self.evidence('q0.p1.r0.out').exists())  # no metrics table for a killed runner

    def test_timeout_cell_without_output_says_so(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                       + self.common_args(),
                                       switches={'RUNNER_SLEEP': '1', 'RUNNER_QUIET': '1'})
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('no runner output captured', run['detail'])
        self.assertNotIn('see ', run['detail'])
        self.assertFalse(self.evidence('q0.p1.r0.submit.log').exists())

    def test_timeout_cell_cancels_the_running_cluster_job(self):
        # the job the runner submitted lives on after the runner is killed: the
        # timeout branch cancels what is still RUNNING since the cell started
        RestStub.add_job('job-1', start_ms=int(time.time() * 1000) + 10000)  # safely after the cell
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                      + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('cancelled the cluster job(s) over REST: job-1 (nexmark job-1)', run['detail'])
        self.assertEqual(RestStub.jobs['job-1']['state'], 'CANCELED')

    def test_timeout_cancel_refusals_and_old_jobs_left_alone(self):
        # a job from before the cell is not ours to kill; one the REST refuses to
        # cancel is reported as still running, with the hand-cancel hint
        RestStub.add_job('old-job', start_ms=int(time.time() * 1000) - 3600000)
        RestStub.add_job('stuck-job', start_ms=int(time.time() * 1000) + 10000, fail_cancel=True)
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1', '--timeout', '2']
                                      + self.common_args(), switches={'RUNNER_SLEEP': '1'})
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('could not be cancelled over REST (REST answered 500 for stuck-job)', run['detail'])
        self.assertIn('cancel it by hand', run['detail'])
        self.assertEqual(RestStub.jobs['old-job']['state'], 'RUNNING')
        self.assertEqual(RestStub.jobs['stuck-job']['state'], 'RUNNING')

    # --- evidence-file guards (the --out rule, per file) ------------------------
    def test_evidence_symlink_is_not_written_through(self):
        victim = self.base / 'victim.out'
        victim.write_text('precious evidence from an earlier tool\n', encoding='utf-8')
        out = self.evidence('q0.p1.r0.out')
        os.makedirs(str(out.parent), exist_ok=True)
        out.symlink_to(victim)
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('q0.p1.r0.out is a symbolic link', run['detail'])
        self.assertIn('never written through a link', run['detail'])
        self.assertTrue(os.path.islink(str(out)))  # the link itself survives
        self.assertEqual(victim.read_bytes(), b'precious evidence from an earlier tool\n')
        self.assertFalse((self.machine['capture'] / 'started').exists())  # the runner never ran
        self.assertFalse(self.evidence('q0.p1.r0.submit.log').exists())
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])

    def test_evidence_fifo_fails_the_cell_without_hanging(self):
        # a fifo at the submit.log would block the write after a whole cell's
        # benchmark: the cell fails on the guard instead, before the runner starts
        fifo = self.evidence('q0.p1.r0.submit.log')
        os.makedirs(str(fifo.parent), exist_ok=True)
        os.mkfifo(str(fifo))
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('q0.p1.r0.submit.log exists and is not a regular file', run['detail'])
        self.assertIn('fifo', run['detail'])
        self.assertTrue(stat.S_ISFIFO(os.stat(str(fifo)).st_mode))  # the fifo survives
        self.assertFalse((self.machine['capture'] / 'started').exists())
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())

    def test_failed_cells_carry_the_full_key_set(self):
        # runs[] keys are constant whatever happened to the cell (bench-table.md §6):
        # a timed-out and a not-run cell report the same keys, values nulled out
        proc, payload = self.run_tool(['run', '--yes', '--queries', 'q0,q1', '--parallelism', '1',
                                       '--timeout', '2'] + self.common_args(),
                                      switches={'RUNNER_SLEEP': '1'})
        self.assertEqual(proc.returncode, 1)
        timeout_cell, not_run_cell = payload[KEY]['runs']
        for run in (timeout_cell, not_run_cell):
            for key in ('runner_exit', 'metrics', 'comparable', 'detail'):
                self.assertIn(key, run)
            self.assertIsNone(run['runner_exit'])
            self.assertIsNone(run['metrics'])
            self.assertFalse(run['comparable'])
        for key in ('table', 'submit_log'):  # the attempted cell knows its evidence paths too
            self.assertIn(key, timeout_cell)

    def test_runner_start_failure_cell_keeps_the_key_set(self):
        # a runner that cannot be executed (EACCES even for root: no x bit) fails the
        # cell without any output to drop — the keys are still all there
        os.chmod(str(self.machine['nexmark'] / 'bin' / 'run_query.sh'), 0o644)
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 1)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('did not start', run['detail'])
        self.assertIsNone(run['runner_exit'])
        self.assertIsNone(run['metrics'])
        self.assertFalse(run['comparable'])
        self.assertFalse(self.evidence('q0.p1.r0.submit.log').exists())
        self.assertFalse((self.machine['capture'] / 'started').exists())

    def test_nested_config_yaml_reads_and_writes_in_its_own_form(self):
        # the machine this skill was built on: Flink 1.19's config.yaml with nested
        # maps (rest: / port:, parallelism: / default:) and no object-reuse key
        original = (
            'rest:\n  address: 127.0.0.1\n  port: %d\n'
            'taskmanager:\n  numberOfTaskSlots: 8\n'
            'parallelism:\n  default: 4\n' % self.rest.port)
        self.machine['flink_conf'].write_text(original, encoding='utf-8')
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload[KEY]['rest']['port'], self.rest.port)
        self.assertEqual(payload[KEY]['slots']['per_tm'], 8)
        self.assertTrue(payload[KEY]['effective_conf'].endswith('config.yaml'))
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        captured = (self.machine['capture'] / 'flinkconf.1.yaml').read_text(encoding='utf-8')
        self.assertIn('\n  default: 1\n', captured)  # the nested leaf, edited in place
        self.assertNotIn('parallelism.default:', captured)  # no flat duplicate
        self.assertIn('pipeline.object-reuse: false', captured)  # absent key appended flat
        self.assertIn('  port: %d\n' % self.rest.port, captured)  # the rest block untouched
        self.assertEqual(original, self.machine['flink_conf'].read_text(encoding='utf-8'))
        self.assertEqual(self.no_backups(), [])

    def test_real_table_format_with_commas_units_and_total_row(self):
        # the table the runner actually prints (captured from a live run): comma-grouped
        # integers, unit-bearing cells, a Total row, and a per-cores cell that carries
        # no number (cores=0). Only the table lines change what the stub prints.
        old = ('printf "| %-5s | %-10d | %-5d | %-7s | %-15.3f | %-10s | %-17.1f |\\n" '
               '"$ROW" "$EV" 8 "$TS" 340.000 "$TP" 294117.6\n')
        new = ('printf "|%-5s |%-15s |%-7s |%-9s |%-15s |%-14s |%-17s|\\n" '
               '"$ROW" "100,000,000" "0" "42.598" "0.000" "235.9 K/s" "n/a"\n'
               'printf "|%-5s |%-15s |%-7s |%-9s |%-15s |%-14s |%-17s|\\n" '
               '"Total" "100,000,000" "0.000" "42.598" "0.000" "235.9 K/s" "n/a"\n')
        stub = RUNNER_STUB.replace(old, new)
        assert stub != RUNNER_STUB, 'the stub table line to replace was not found'
        runner = self.machine['nexmark'] / 'bin' / 'run_query.sh'
        runner.write_text(stub, encoding='utf-8')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['metrics']['events_num'], 100000000)
        self.assertEqual(run['metrics']['time_s'], 42.598)
        self.assertEqual(run['metrics']['throughput'], 235900.0)
        self.assertEqual(run['metrics']['cores'], 0)
        self.assertIsNone(run['metrics']['throughput_per_cores'])
        self.assertTrue(run['comparable'])

    def test_runner_success_without_table_is_failure(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_NO_TABLE': '1'})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('FINISHED', payload[KEY]['runs'][0]['detail'])

    def test_table_row_for_another_query_is_failure(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_WRONG_QUERY': '1'})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no row for q0', payload[KEY]['runs'][0]['detail'])

    def test_events_off_calibre_not_comparable(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_SMALL_EVENTS': '1'})
        self.assertEqual(proc.returncode, 0)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'done')
        self.assertFalse(run['comparable'])
        self.assertIn('not comparable', run['detail'])

    def test_na_throughput_is_not_comparable(self):
        # events at the calibre but the throughput column prints n/a: the row is
        # done and kept, yet without its key metric it is no comparable measurement
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_NA_THROUGHPUT': '1'})
        self.assertEqual(proc.returncode, 0)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['metrics']['events_num'], 100000000)
        self.assertIsNone(run['metrics']['throughput'])
        self.assertFalse(run['comparable'])
        self.assertIn('throughput missing from the table', run['detail'])
        self.assertIn('not comparable', run['detail'])

    def test_zero_time_is_not_comparable(self):
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args(),
                                      switches={'RUNNER_ZERO_TIME': '1'})
        self.assertEqual(proc.returncode, 0)
        run = payload[KEY]['runs'][0]
        self.assertEqual(run['status'], 'done')
        self.assertFalse(run['comparable'])
        self.assertIn('time is not a positive number', run['detail'])

    def test_sigterm_restores_the_confs(self):
        original_nexmark = self.machine['nexmark_conf'].read_bytes()
        original_flink = self.machine['flink_conf'].read_bytes()
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), '--json', 'run', '--yes', '--queries', 'q0,q1', '--parallelism', '1']
            + self.common_args(), cwd=str(self.machine['project']), env=self.env({'RUNNER_SLEEP': '1'}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not (self.machine['capture'] / 'started').exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue((self.machine['capture'] / 'started').exists(), 'the runner stub was never invoked')
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(out)
        r = payload[KEY]
        self.assertFalse(r['success'])
        self.assertEqual(r['runs'][0]['status'], 'failed')
        self.assertIn('interrupted', r['runs'][0]['detail'])
        self.assertIsNone(r['runs'][0]['runner_exit'])  # interrupted cell: full key set too
        self.assertIsNone(r['runs'][0]['metrics'])
        self.assertFalse(r['runs'][0]['comparable'])
        self.assertTrue(r['confs_restored'])
        # restored to the byte (both confs), the locks released, no backups left
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(original_flink, self.machine['flink_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])
        self.assertEqual(self.no_locks(), [])

    # --- the run lock: two runs, one installation ------------------------------
    def test_live_lock_refuses_the_run(self):
        # pid 1 is always alive: a lock naming it is another run in flight
        lock = self.machine['flink_conf'].parent / ('config.yaml' + LOCK)
        lock.write_text('1 2026-11-26T00:00:00\n', encoding='utf-8')
        original = self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf'])
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('another run (pid 1) holds the lock', payload[KEY]['error_message'])
        self.assertIn('wait for it to finish', payload[KEY]['next_step'])
        self.assertEqual(original, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertEqual(self.no_backups(), [])
        self.assertFalse((self.machine['capture'] / 'started').exists())
        self.assertEqual(lock.read_text(encoding='utf-8'), '1 2026-11-26T00:00:00\n')  # not touched
        # plan is read-only: it neither locks nor cares about the lock
        proc, payload = self.run_tool(['plan', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)

    def test_stale_lock_is_replaced_and_recorded(self):
        # 4194304 is one past pid_max on 64-bit Linux: a holder that cannot exist
        lock = self.machine['nexmark_conf'].parent / ('nexmark.yaml' + LOCK)
        lock.write_text('4194304 2026-11-25T00:00:00\n', encoding='utf-8')
        proc, payload = self.run_tool(['run', '--yes', '--parallelism', '1'] + self.common_args())
        self.assertEqual(proc.returncode, 0)
        details = ' '.join(c['detail'] for c in payload[KEY]['changes'])
        self.assertIn('stale lock replaced', ' '.join(c['action'] for c in payload[KEY]['changes']))
        self.assertIn('4194304 not alive', details)
        self.assertTrue(payload[KEY]['confs_restored'])
        self.assertEqual(self.no_locks(), [])  # the replacement lock was released again

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
                               capture_output=True, text=True, timeout=120, preexec_fn=cap)
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(payload[KEY]['error_message'].startswith('tool aborted'))
        self.assertFalse(payload[KEY]['confs_restored'])
        self.assertEqual(self.no_backups(), [])   # no .bak — complete or absent, never half
        self.assertEqual(self.no_tmps(), [])      # and no temporary left behind
        self.assertEqual(original, self.snapshot(self.machine['flink_conf'], self.machine['nexmark_conf']))
        self.assertEqual(self.no_locks(), [])
        self.assertFalse((self.machine['capture'] / 'started').exists())

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
        self.assertEqual(self.no_locks(), [])

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

    def test_refuses_symlinked_flink_conf(self):
        real = self.make_link(self.machine['flink_conf'], 'real-config.yaml')
        before = real.read_bytes()
        for args in (['plan'], ['run', '--yes']):
            proc, payload = self.run_tool(args + ['--parallelism', '1'] + self.common_args())
            self.assertEqual(proc.returncode, 2, args)
            self.assertIn('config.yaml is a symbolic link', payload[KEY]['error_message'])
            self.assertIn('never through a link', payload[KEY]['error_message'])
            self.assertIn('regular file', payload[KEY]['next_step'])
            self.assert_conf_untouched(self.machine['flink_conf'], real, before)
            self.assertFalse((self.machine['capture'] / 'started').exists())

    def test_refuses_symlinked_nexmark_conf(self):
        real = self.make_link(self.machine['nexmark_conf'], 'real-nexmark.yaml')
        before = real.read_bytes()
        for args in (['plan'], ['run', '--yes']):
            proc, payload = self.run_tool(args + ['--parallelism', '1'] + self.common_args())
            self.assertEqual(proc.returncode, 2, args)
            self.assertIn('nexmark.yaml is a symbolic link', payload[KEY]['error_message'])
            self.assert_conf_untouched(self.machine['nexmark_conf'], real, before)
            self.assertFalse((self.machine['capture'] / 'started').exists())

    def test_refuses_symlinked_backup_and_never_moves_it(self):
        # a .bak that is a link would be renamed onto the conf path and then written
        # through: refused instead, the link stays a link pointing where it pointed
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

    def test_refuses_symlinked_flink_backup(self):
        victim = self.base / 'precious-flink.yaml'
        victim.write_text('precious flink content\n', encoding='utf-8')
        bak = self.machine['flink_conf'].parent / ('config.yaml' + BAK)
        bak.symlink_to(victim)
        original = self.machine['flink_conf'].read_bytes()
        for args in (['plan'], ['run', '--yes']):
            proc, payload = self.run_tool(args + ['--parallelism', '1'] + self.common_args())
            self.assertEqual(proc.returncode, 2, args)
            self.assertIn(BAK + ' is a symbolic link', payload[KEY]['error_message'])
            self.assertTrue(os.path.islink(str(bak)))
            self.assertEqual(victim.read_bytes(), b'precious flink content\n')
            self.assertEqual(original, self.machine['flink_conf'].read_bytes())

    # --- least-privilege fix hints ------------------------------------------------
    def test_unwritable_conf_fix_is_minimal_privilege(self):
        # who may write the confs is the user's to arrange: the refusal says installing
        # owner / admin-adjusted permissions — no root command, no pre-filled --yes
        for conf in (self.machine['flink_conf'], self.machine['nexmark_conf']):
            os.chmod(str(conf), 0o444)
        argv = [sys.executable, str(SCRIPT), '--json', 'plan', '--parallelism', '1'] + self.common_args()
        if os.geteuid() == 0:  # root writes through 0444: run the child as nobody
            if not shutil.which('setpriv'):
                self.skipTest('setpriv unavailable to drop privileges as root')
            argv = ['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups'] + argv
        proc = subprocess.run(argv, cwd=str(self.machine['project']), env=self.env(),
                              capture_output=True, text=True, timeout=120)
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

    # --- --out and the evidence directory --------------------------------------
    def test_out_and_evidence_guardrails(self):
        # --parallelism 1: the refusal under test must come from --out, not from the slot gate
        guarded = [
            (['run', '--yes', '--parallelism', '1', '--out', '../outside.json'] + self.common_args(),
             'project directory'),
            (['run', '--yes', '--parallelism', '1', '--out', 'taken.json'] + self.common_args(),
             'already exists'),
            (['plan', '--parallelism', '1', '--evidence-dir', '/tmp/elsewhere'] + self.common_args(),
             'project directory'),
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
        self.assertFalse(r['confs_restored'])
        self.assertEqual(original_nexmark, self.machine['nexmark_conf'].read_bytes())
        self.assertEqual(self.no_backups(), [])

    # --- the suggested command runs as printed ---------------------------------
    def test_the_suggested_command_runs_as_printed(self):
        proc, payload = self.run_tool(['plan', '--queries', 'q0', '--parallelism', '1']
                                      + self.common_args())
        self.assertEqual(proc.returncode, 0)
        marker = 'then run '
        self.assertIn(marker, payload[KEY]['next_step'])
        command = payload[KEY]['next_step'].split(marker, 1)[1].strip()
        self.assertIn('--flink-home', command)
        other = self.base / 'project-b'
        other.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(['bash', '-c', command], cwd=str(other), env=self.env(),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((other / 'e2e' / 'perf' / 'native' / 'q0.p1.r0.out').exists())

    def test_parallelism_must_be_positive_integers(self):
        proc, payload = self.run_tool(['plan', '--parallelism', '0,x'] + self.common_args())
        self.assertEqual(proc.returncode, 2)
        self.assertIn('parallelism', proc.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
