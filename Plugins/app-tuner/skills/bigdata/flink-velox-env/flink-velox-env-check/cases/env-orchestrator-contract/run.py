#!/usr/bin/env python3
"""env-orchestrator-contract — tier-A case for flink-velox-env-check.

Drives scripts/env_check.py as a child process against a stub check family
laid out in a scratch directory (ENV_PREPARE_ENV_FAMILY_DIR carries it): nine
stub scripts print canned <check>_result JSON with per-check switches (ok /
fail / warn / crash / timeout / grandchild / no-result / not-an-object) and
record the argv and locator env they were invoked with — nothing from the real
machine is in play, and the stubs are children of the child (the grandchild
switch even leaves a sleep 999 behind, to prove the timeout takes the whole
process group down). One test reads the real source tree only (paths and
frontmatter, nothing executed).

Pins: goal=build runs all nine checks and aggregates the six groups in order
with the family-wide missing roll-up; goal=native records the four build-side
checks disabled (neither fail nor missing; the run still succeeds), passes
--goal native to jdk and --expect native to lib-state (goal=build pins
nothing — the build target accepts either side); --repos-dir reaches
source-repos as --repos-dir and cpp-deps as GFV_REPOS_DIR plus --velox-dir
<repos-dir>/velox (both pinned to the one checkout, ahead of any VELOX_HOME),
and nothing is pinned when it is left to discovery; an explicit --flink-home
without a runnable bin/flink or --nexmark-home without a runnable
bin/run_query.sh is refused (exit 2, stderr, no JSON) before any check runs,
while a valid one passes the gate and reaches the checks; the unique
next_step follows the group priority build_env → flink → java → lib →
cluster (repos in the build-preparation tier right behind build_env) with
missing beating fail inside a group and the fail branch quoting the group's
first failed check's own next_step verbatim; all green says the goal is
ready; a crashed, timed-out or resultless check fails itself and the run,
its manual re-check line repeating the exact invocation this run made
(interpreter, absolute script path, every locator flag and env — so a
recheck lands on the same installation) and running exactly as printed; a
timeout kills the whole process group, the stub's spawned grandchild
included; a sibling script that is not there reports NOT VERIFIED and exits
1 (never a pass); a warn aggregates into the groups without touching the
next_step; the key set is constant — the result, every group, every check in
every status, the aborted shape included (a sub result that is not an object
aborts the tool); the --out guards (outside the project / symlink / existing
without --overwrite / a fifo even with --overwrite, validated before any
check runs); and the real tree holding all ten skill directories with
frontmatter names matching them.

Writes results/result.json; exit 0 iff all tests pass.
"""
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / 'scripts' / 'env_check.py'
RESULTS = CASE_DIR / 'results'
SCRATCH = RESULTS / 'scratch'
REAL_FAMILY = CASE_DIR.parents[2]  # the source-tree flink-velox-env/ directory

KEY = 'flink_velox_env_check_result'

RESULT_KEYS = {'success', 'command', 'goal', 'checked_at', 'flink_home', 'nexmark_home',
               'repos_dir', 'timeout', 'groups', 'missing', 'next_step', 'error_message'}
CHECK_KEYS = {'name', 'skill', 'status', 'success', 'detail', 'missing', 'next_step'}

# the contract this case pins independently of the implementation
FAMILY_CHECKS = (
    ('build_env', 'toolchain', 'flink-velox-toolchain-check', 'toolchain_check.py'),
    ('build_env', 'build-tools', 'flink-velox-build-tools-check', 'build_tools_check.py'),
    ('build_env', 'cpp-deps', 'flink-velox-cpp-deps-check', 'cpp_deps_check.py'),
    ('repos', 'source-repos', 'flink-velox-source-repos-check', 'source_repos_check.py'),
    ('java', 'jdk', 'flink-velox-jdk-check', 'jdk_check.py'),
    ('flink', 'flink-dist', 'flink-velox-flink-dist-check', 'flink_dist_check.py'),
    ('lib', 'lib-state', 'flink-velox-lib-state-check', 'lib_state_check.py'),
    ('cluster', 'cluster', 'flink-velox-cluster-check', 'cluster_check.py'),
    ('cluster', 'nexmark', 'flink-velox-nexmark-check', 'nexmark_check.py'),
)
GROUP_ORDER = ('build_env', 'repos', 'java', 'flink', 'lib', 'cluster')

STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    # tier-A stub for one flink-velox-env check: prints a canned <x>_result JSON
    # (ok / fail / warn), prints nothing usable, prints a result that is not an
    # object, crashes, hangs, or hangs behind a spawned grandchild (sleep 999 —
    # the timeout must take the whole process group down, grandchild included);
    # records argv, the locator env it saw and the grandchild pid.
    import json
    import os
    import subprocess
    import sys
    import time

    x = os.path.basename(os.path.realpath(__file__))[:-len('_check.py')]
    mode = os.environ.get('ENV_CHECK_STUB_MODE_' + x.upper()) or os.environ.get('ENV_CHECK_STUB_MODE', 'ok')
    grandchild = None
    if mode == 'grandchild':
        grandchild = subprocess.Popen(['sleep', '999'])
    log = os.environ.get('ENV_CHECK_STUB_ARGV_LOG')
    if log:
        with open(log, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'x': x, 'argv': sys.argv[1:],
                                 'FLINK_HOME': os.environ.get('FLINK_HOME'),
                                 'NEXMARK_HOME': os.environ.get('NEXMARK_HOME'),
                                 'GFV_REPOS_DIR': os.environ.get('GFV_REPOS_DIR'),
                                 'grandchild_pid': grandchild.pid if grandchild else None}) + '\\n')
    if mode == 'crash':
        sys.stderr.write('stub %s: boom\\n' % x)
        sys.exit(3)
    if mode in ('timeout', 'grandchild'):
        time.sleep(30 if mode == 'timeout' else 999)
        sys.exit(0)
    if mode == 'noresult':
        sys.stdout.write('stub %s: not json at all\\n' % x)
        sys.exit(0)
    if mode == 'nodict':
        print(json.dumps({'flink_velox_%s_check_result' % x: 'not an object'}))
        sys.exit(0)
    fail, warn = mode == 'fail', mode == 'warn'
    result = {'success': not fail,
              'checked_at': '2026-11-26T00:00:00+08:00',
              'status': 'fail' if fail else ('warn' if warn else 'ok'),
              'items': ([{'name': 'probe', 'status': 'fail', 'detail': '%s: something is missing (stub)' % x}]
                        if fail else ([{'name': 'duplicates', 'status': 'warn',
                                        'detail': 'stub warning for %s' % x}] if warn else
                                       [{'name': 'probe', 'status': 'ok', 'detail': 'stub ok for %s' % x}])),
              'missing': (['%s: something is missing (stub)' % x] if fail else []),
              'next_step': os.environ.get('ENV_CHECK_STUB_NEXT_' + x.upper())
                            or ('fix the %s finding (stub work order), then rerun' % x),
              'error_message': ('1 item(s) missing: probe' if fail else '')}
    print(json.dumps({'flink_velox_%s_check_result' % x: result}))
    sys.exit(1 if fail else 0)
    ''')


def proc_state(pid):
    """The /proc state letter of a pid, or None once it is gone (reaped)."""
    try:
        with open('/proc/%d/stat' % pid, encoding='utf-8') as fh:
            data = fh.read()
    except OSError:
        return None
    tail = data.rsplit(')', 1)[1].split()
    return tail[0] if tail else '?'


def kill_if_alive(pid):
    """A stub grandchild must never outlive the test, even when a fix regresses."""
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class OrchestratorCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if RESULTS.exists():
            (RESULTS / 'result.json').write_text(
                json.dumps({'success': True, 'case': 'env-orchestrator-contract'}, indent=2),
                encoding='utf-8')

    def setUp(self):
        self.base = SCRATCH / self.id().split('.')[-1]
        self.project = self.base / 'project'
        self.family = self.base / 'family'
        self.capture = self.base / 'argv.log'
        self.project.mkdir(parents=True, exist_ok=True)

    # --- the stub family and the tool ---------------------------------------
    def build_family(self, drop=()):
        shutil.rmtree(self.family, ignore_errors=True)  # a rebuild starts clean, drop included
        for _group, _name, skill, script in FAMILY_CHECKS:
            if script in drop:
                continue
            path = self.family / skill / 'scripts' / script
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(STUB, encoding='utf-8')
        return self.family

    def env(self, switches=None):
        e = {k: v for k, v in os.environ.items()
             if k not in ('FLINK_HOME', 'NEXMARK_HOME', 'GFV_REPOS_DIR',
                          'ENV_PREPARE_ENV_FAMILY_DIR') and not k.startswith('ENV_CHECK_STUB')}
        e['ENV_PREPARE_ENV_FAMILY_DIR'] = str(self.family)
        e['ENV_CHECK_STUB_ARGV_LOG'] = str(self.capture)
        e.update(switches or {})
        return e

    def run_tool(self, args, switches=None, cwd=None, as_json=True, timeout=180):
        call = [sys.executable, str(SCRIPT)] + (['--json'] if as_json else []) + args
        proc = subprocess.run(call, cwd=str(cwd or self.project), env=self.env(switches),
                              capture_output=True, text=True, timeout=timeout)
        payload = None
        if as_json:
            try:
                payload = json.loads(proc.stdout)
            except ValueError:
                pass
        return proc, payload

    def check(self, payload, name):
        return next(c for g in payload[KEY]['groups'] for c in g['checks'] if c['name'] == name)

    def calls(self):
        if not self.capture.exists():
            return []
        return [json.loads(line) for line in
                self.capture.read_text(encoding='utf-8').splitlines() if line.strip()]

    def call_for(self, x):
        return next((c for c in self.calls() if c['x'] == x), None)

    def stub_path(self, skill):
        """The real path checker_path would resolve for one stub sibling."""
        script = next(sc for _g, _n, s, sc in FAMILY_CHECKS if s == skill)
        return os.path.realpath(str(self.family / skill / 'scripts' / script))

    def make_flink_home(self):
        """A flink home that passes the explicit-path gate: a runnable bin/flink."""
        fh = self.base / 'flink-home'
        (fh / 'bin').mkdir(parents=True, exist_ok=True)
        (fh / 'bin' / 'flink').write_text('#!/bin/sh\n', encoding='utf-8')
        (fh / 'bin' / 'flink').chmod(0o755)
        return fh

    # --- goal=build: all nine, six groups -------------------------------------
    def test_goal_build_runs_all_nine_checks_in_six_groups(self):
        self.build_family()
        proc, payload = self.run_tool(['plan', '--goal', 'build'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = payload[KEY]
        self.assertTrue(r['success'])
        self.assertEqual(r['command'], 'plan')
        self.assertEqual(r['goal'], 'build')
        self.assertEqual([g['name'] for g in r['groups']], list(GROUP_ORDER))
        self.assertEqual([c['name'] for g in r['groups'] for c in g['checks']],
                         [name for _g, name, _s, _sc in FAMILY_CHECKS])
        self.assertEqual([c['status'] for g in r['groups'] for c in g['checks']], ['ok'] * 9)
        self.assertEqual(r['missing'], [])
        self.assertEqual(r['error_message'], '')
        self.assertEqual(r['next_step'], 'goal build is ready: all checks pass')
        # every check ran, once, with --json only — jdk additionally carries the goal
        self.assertEqual(sorted(c['x'] for c in self.calls()),
                         sorted(script[:-len('_check.py')] for _g, _n, _s, script in FAMILY_CHECKS))
        self.assertTrue(all(c['argv'] == ['--json'] or
                            (c['x'] == 'jdk' and c['argv'] == ['--json', '--goal', 'build'])
                            for c in self.calls()))
        # the human report lists the groups and the READY verdict
        human, _ = self.run_tool(['--goal', 'build'], as_json=False)
        self.assertEqual(human.returncode, 0)
        order = [m.start() for m in re.finditer(r'^(build_env|repos|java|flink|lib|cluster):',
                                                 human.stdout, re.M)]
        self.assertEqual(len(order), 6, human.stdout)
        self.assertIn('READY: goal build, all checks pass', human.stdout)

    # --- goal=native: four disabled, five run ----------------------------------
    def test_goal_native_disables_build_side_and_still_succeeds(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'native'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = payload[KEY]
        self.assertTrue(r['success'])  # disabled is neither fail nor missing
        self.assertEqual(r['error_message'], '')
        disabled = {'toolchain', 'build-tools', 'cpp-deps', 'source-repos'}
        for group in r['groups']:
            for c in group['checks']:
                if c['name'] in disabled:
                    self.assertEqual(c['status'], 'disabled', c)
                    self.assertIsNone(c['success'])
                    self.assertEqual(c['detail'], 'goal=native does not need it')
                else:
                    self.assertEqual(c['status'], 'ok', c)
        # the five that ran: jdk under the native goal, lib-state pinned to the
        # native build, the build side never invoked
        self.assertEqual(sorted(c['x'] for c in self.calls()),
                         ['cluster', 'flink_dist', 'jdk', 'lib_state', 'nexmark'])
        self.assertEqual(self.call_for('jdk')['argv'], ['--json', '--goal', 'native'])
        self.assertEqual(self.call_for('lib_state')['argv'], ['--json', '--expect', 'native'])
        self.assertEqual(r['next_step'], 'goal native is ready: all checks pass')

    # --- goal=native pins lib-state to the native build -----------------------
    def test_goal_native_pins_lib_state_to_the_native_build(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'native'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # the native goal runs on the native build: lib-state is told so, and a
        # GlutenFlink side becomes a fail whose work order goes back to the
        # switch step instead of a READY verdict
        self.assertEqual(self.call_for('lib_state')['argv'], ['--json', '--expect', 'native'])
        self.assertTrue(payload[KEY]['success'])

    def test_goal_build_pins_nothing_on_lib_state(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # the build target accepts either side: lib-state keeps its own default (any)
        self.assertEqual(self.call_for('lib_state')['argv'], ['--json'])
        self.assertTrue(payload[KEY]['success'])

    # --- the unique next_step: first failing group by priority ----------------
    def test_next_step_takes_the_first_failing_group_by_priority(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build'], switches={
            'ENV_CHECK_STUB_MODE_BUILD_TOOLS': 'fail',
            'ENV_CHECK_STUB_NEXT_BUILD_TOOLS': 'BUILD-TOOLS WORK ORDER',
            'ENV_CHECK_STUB_MODE_FLINK_DIST': 'fail',
            'ENV_CHECK_STUB_NEXT_FLINK_DIST': 'FLINK-DIST WORK ORDER',
            'ENV_CHECK_STUB_MODE_CLUSTER': 'fail',
            'ENV_CHECK_STUB_NEXT_CLUSTER': 'CLUSTER WORK ORDER'})
        self.assertEqual(proc.returncode, 1)
        r = payload[KEY]
        self.assertFalse(r['success'])
        # build_env fails, flink fails, cluster fails — build_env's first fail wins
        self.assertEqual(r['next_step'], 'BUILD-TOOLS WORK ORDER')
        self.assertIn('build_tools: something is missing (stub)', r['missing'])
        self.assertIn('flink_dist: something is missing (stub)', r['missing'])
        self.assertIn('cluster: something is missing (stub)', r['missing'])
        self.assertEqual(r['error_message'], '3 check(s) failing')
        # build_env green: the flink group's own first fail takes over
        proc, payload = self.run_tool(['--goal', 'build'], switches={
            'ENV_CHECK_STUB_MODE_FLINK_DIST': 'fail',
            'ENV_CHECK_STUB_NEXT_FLINK_DIST': 'FLINK-DIST WORK ORDER',
            'ENV_CHECK_STUB_MODE_CLUSTER': 'fail'})
        self.assertEqual(payload[KEY]['next_step'], 'FLINK-DIST WORK ORDER')

    def test_priority_walks_flink_java_lib_cluster_in_order(self):
        self.build_family()
        # flink beats java even when java also fails
        proc, payload = self.run_tool(['--goal', 'build'], switches={
            'ENV_CHECK_STUB_MODE_FLINK_DIST': 'fail', 'ENV_CHECK_STUB_NEXT_FLINK_DIST': 'FLINK WORK ORDER',
            'ENV_CHECK_STUB_MODE_JDK': 'fail', 'ENV_CHECK_STUB_NEXT_JDK': 'JAVA WORK ORDER'})
        self.assertEqual(payload[KEY]['next_step'], 'FLINK WORK ORDER')
        # lib beats cluster; a green flink is skipped over
        proc, payload = self.run_tool(['--goal', 'build'], switches={
            'ENV_CHECK_STUB_MODE_LIB_STATE': 'fail', 'ENV_CHECK_STUB_NEXT_LIB_STATE': 'LIB WORK ORDER',
            'ENV_CHECK_STUB_MODE_CLUSTER': 'fail', 'ENV_CHECK_STUB_NEXT_CLUSTER': 'CLUSTER WORK ORDER'})
        self.assertEqual(payload[KEY]['next_step'], 'LIB WORK ORDER')
        # repos sits in the build-preparation tier: its fail is the work order when build_env is green
        proc, payload = self.run_tool(['--goal', 'build'], switches={
            'ENV_CHECK_STUB_MODE_SOURCE_REPOS': 'fail', 'ENV_CHECK_STUB_NEXT_SOURCE_REPOS': 'REPOS WORK ORDER',
            'ENV_CHECK_STUB_MODE_JDK': 'fail', 'ENV_CHECK_STUB_NEXT_JDK': 'JAVA WORK ORDER'})
        self.assertEqual(payload[KEY]['next_step'], 'REPOS WORK ORDER')

    # --- crash / timeout / no result -------------------------------------------
    def test_crashed_and_timed_out_checks_fail_the_run(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build'],
                                      switches={'ENV_CHECK_STUB_MODE_JDK': 'crash'})
        self.assertEqual(proc.returncode, 1)
        c = self.check(payload, 'jdk')
        self.assertEqual(c['status'], 'fail')
        self.assertEqual(c['detail'], 'flink-velox-jdk-check produced no result (exit 3)')
        self.assertIn("see the check's own error: python3", c['next_step'])
        self.assertIn('jdk_check.py', c['next_step'])
        self.assertIn('--goal build', c['next_step'])
        self.assertFalse(payload[KEY]['success'])
        self.assertEqual(payload[KEY]['next_step'], c['next_step'])  # java is the first failing group
        # not json at all: the same shape of failure
        proc, payload = self.run_tool(['--goal', 'build'],
                                      switches={'ENV_CHECK_STUB_MODE_NEXMARK': 'noresult'})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.check(payload, 'nexmark')['detail'],
                         'flink-velox-nexmark-check produced no result (exit 0)')
        # a check that hangs is cut off at the timeout and fails
        proc, payload = self.run_tool(['--goal', 'build', '--timeout', '2'],
                                      switches={'ENV_CHECK_STUB_MODE_CLUSTER': 'timeout'})
        self.assertEqual(proc.returncode, 1)
        c = self.check(payload, 'cluster')
        self.assertEqual(c['status'], 'fail')
        self.assertEqual(c['detail'], 'flink-velox-cluster-check did not finish within 2s')
        self.assertIn('cluster_check.py', c['next_step'])

    def test_timeout_kills_the_whole_process_group_grandchildren_included(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build', '--timeout', '2'],
                                      switches={'ENV_CHECK_STUB_MODE_CLUSTER': 'grandchild'},
                                      timeout=60)
        self.assertEqual(proc.returncode, 1)
        c = self.check(payload, 'cluster')
        self.assertEqual(c['status'], 'fail')
        self.assertEqual(c['detail'], 'flink-velox-cluster-check did not finish within 2s')
        # the stub hung behind a sleep 999 grandchild: the group kill must take
        # it down too (gone from /proc, or a zombie waiting to be reaped) —
        # killing the direct child alone would leave it orphaned
        gcpid = self.call_for('cluster')['grandchild_pid']
        self.assertIsNotNone(gcpid)
        self.addCleanup(kill_if_alive, gcpid)
        deadline = time.time() + 10
        state = proc_state(gcpid)
        while state not in (None, 'Z'):
            self.assertLess(time.time(), deadline,
                            'grandchild %d still alive (%s) after the group kill' % (gcpid, state))
            time.sleep(0.2)
            state = proc_state(gcpid)

    # --- a missing sibling is never a pass --------------------------------------
    def test_missing_sibling_reports_not_verified_exit_1(self):
        self.build_family(drop=('cpp_deps_check.py',))
        proc, payload = self.run_tool(['--goal', 'build'])
        self.assertEqual(proc.returncode, 1)
        r = payload[KEY]
        self.assertFalse(r['success'])
        c = self.check(payload, 'cpp-deps')
        self.assertEqual(c['status'], 'missing')
        self.assertFalse(c['success'])
        self.assertEqual(c['detail'], 'flink-velox-cpp-deps-check is not installed next to this skill')
        self.assertEqual(r['error_message'],
                         '1 check(s) not run (sibling skills not installed): flink-velox-cpp-deps-check')
        self.assertEqual(r['next_step'],
                         'install the missing check skill(s) (flink-velox-cpp-deps-check), '
                         'then rerun python3 %s --goal build' % SCRIPT)
        self.assertIsNone(self.call_for('cpp_deps'))
        # even a failing check elsewhere does not outrank the missing one in build_env
        proc, payload = self.run_tool(['--goal', 'build'], switches={
            'ENV_CHECK_STUB_MODE_FLINK_DIST': 'fail', 'ENV_CHECK_STUB_NEXT_FLINK_DIST': 'FLINK WORK ORDER'})
        self.assertEqual(payload[KEY]['next_step'],
                         'install the missing check skill(s) (flink-velox-cpp-deps-check), '
                         'then rerun python3 %s --goal build' % SCRIPT)
        human, _ = self.run_tool(['--goal', 'build'], as_json=False)
        self.assertIn('NOT VERIFIED: 1 check(s) not run (sibling skills not installed): '
                      'flink-velox-cpp-deps-check', human.stdout)

    def test_all_green_is_ready(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build'])
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(payload[KEY]['success'])
        self.assertEqual(payload[KEY]['next_step'], 'goal build is ready: all checks pass')

    # --- the key set is constant, aborted included -----------------------------
    def test_key_set_constant_including_abort(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build'])
        self.assertEqual(set(payload[KEY]), RESULT_KEYS)
        for group in payload[KEY]['groups']:
            self.assertEqual(set(group), {'name', 'checks'})
            for c in group['checks']:
                self.assertEqual(set(c), CHECK_KEYS, c)
        # every status variant in one run: ok, warn, fail, disabled, missing
        # (nexmark is not build-side, so goal=native still runs it — a dropped
        # stub there is a missing check, not a disabled one)
        self.build_family(drop=('nexmark_check.py',))
        proc, payload = self.run_tool(['--goal', 'native'], switches={
            'ENV_CHECK_STUB_MODE_JDK': 'fail', 'ENV_CHECK_STUB_MODE_LIB_STATE': 'warn'})
        self.assertEqual(set(payload[KEY]), RESULT_KEYS)
        statuses = set()
        for group in payload[KEY]['groups']:
            self.assertEqual(set(group), {'name', 'checks'})
            for c in group['checks']:
                self.assertEqual(set(c), CHECK_KEYS, c)
                statuses.add(c['status'])
        self.assertEqual(statuses, {'ok', 'warn', 'fail', 'disabled', 'missing'})
        # a sub result that is not an object aborts the tool with the same keys
        proc, payload = self.run_tool(['--goal', 'build'],
                                      switches={'ENV_CHECK_STUB_MODE_JDK': 'nodict'})
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(payload[KEY]['error_message'].startswith('tool aborted'))
        self.assertEqual(set(payload[KEY]), RESULT_KEYS)
        self.assertEqual(payload[KEY]['groups'], [])
        self.assertEqual(payload[KEY]['missing'], [])
        self.assertFalse(payload[KEY]['success'])
        human, _ = self.run_tool(['--goal', 'build'], as_json=False,
                                 switches={'ENV_CHECK_STUB_MODE_JDK': 'nodict'})
        self.assertIn('TOOL ERROR: tool aborted:', human.stdout)

    # --- --out guards, checked before anything runs ----------------------------
    def test_out_guards(self):
        self.build_family()
        outside = self.base / 'outside.json'
        outside.write_text('precious\n', encoding='utf-8')
        for out_args in (['--out', str(outside)],                   # outside the project
                         ['--out', str(outside), '--overwrite']):   # still outside
            proc, payload = self.run_tool(['--goal', 'build'] + out_args)
            self.assertEqual(proc.returncode, 2, out_args)
            self.assertIn('must be a regular path inside the project directory', proc.stderr)
            self.assertEqual(proc.stdout, '')  # nothing was checked, so nothing to print
            self.assertFalse(self.capture.exists())
        self.assertEqual(outside.read_text(encoding='utf-8'), 'precious\n')
        # a symlink inside the project is never written through
        target = self.base / 'victim.json'
        target.write_text('precious\n', encoding='utf-8')
        link = self.project / 'link.json'
        link.symlink_to(target)
        proc, _ = self.run_tool(['--goal', 'build', '--out', 'link.json'])
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(os.path.islink(str(link)))
        self.assertEqual(target.read_text(encoding='utf-8'), 'precious\n')
        # an existing file is kept without --overwrite
        kept = self.project / 'kept.json'
        kept.write_text('keep me\n', encoding='utf-8')
        proc, _ = self.run_tool(['--goal', 'build', '--out', 'kept.json'])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(kept.read_text(encoding='utf-8'), 'keep me\n')
        # a fifo is never opened even with --overwrite
        fifo = self.project / 'fifo.json'
        os.mkfifo(str(fifo))
        proc, _ = self.run_tool(['--goal', 'build', '--out', 'fifo.json', '--overwrite'])
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(stat.S_ISFIFO(os.stat(str(fifo)).st_mode))
        # a fresh regular path is written with the same contract
        proc, payload = self.run_tool(['--goal', 'build', '--out', 'result.json'])
        self.assertEqual(proc.returncode, 0)
        written = json.loads((self.project / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(set(written[KEY]), RESULT_KEYS)
        self.assertTrue(written[KEY]['success'])

    # --- the locator pass-through ----------------------------------------------
    def test_locator_passthrough(self):
        self.build_family()
        fh = self.make_flink_home()
        nh = self.base / 'nexmark-home'
        (nh / 'bin').mkdir(parents=True, exist_ok=True)
        runq = nh / 'bin' / 'run_query.sh'
        runq.write_text('#!/bin/sh\n', encoding='utf-8')
        runq.chmod(0o755)
        proc, payload = self.run_tool(['--goal', 'build', '--flink-home', str(fh),
                                       '--nexmark-home', str(nh), '--repos-dir', '/rd'])
        self.assertEqual(proc.returncode, 0)
        saw = {c['x']: c for c in self.calls()}
        for x in ('flink_dist', 'jdk', 'lib_state', 'cluster', 'nexmark'):
            self.assertEqual(saw[x]['FLINK_HOME'], str(fh), x)
        self.assertEqual(saw['nexmark']['NEXMARK_HOME'], str(nh))
        self.assertEqual(saw['source_repos']['argv'], ['--json', '--repos-dir', '/rd'])
        self.assertEqual(saw['source_repos']['FLINK_HOME'], None)  # it does not take one
        self.assertEqual(saw['cpp_deps']['GFV_REPOS_DIR'], '/rd')
        self.assertEqual(saw['cpp_deps']['argv'], ['--json', '--velox-dir', '/rd/velox'])
        self.assertEqual(saw['jdk']['argv'], ['--json', '--goal', 'build'])
        for x in ('toolchain', 'build_tools'):  # no locator reaches these
            self.assertEqual(saw[x]['argv'], ['--json'])
            self.assertIsNone(saw[x]['FLINK_HOME'])
            self.assertIsNone(saw[x]['NEXMARK_HOME'])
            self.assertIsNone(saw[x]['GFV_REPOS_DIR'])
        for x in ('flink_dist', 'lib_state', 'cluster'):  # nexmark's tree is nexmark's only
            self.assertIsNone(saw[x]['NEXMARK_HOME'])
        self.assertEqual(payload[KEY]['flink_home'], str(fh))
        self.assertEqual(payload[KEY]['nexmark_home'], str(nh))
        self.assertEqual(payload[KEY]['repos_dir'], '/rd')
        # left to discovery: nothing is injected and each check sees the ambient env as-is
        self.capture.unlink()
        proc, payload = self.run_tool(['--goal', 'build'])
        self.assertEqual(proc.returncode, 0)
        saw = {c['x']: c for c in self.calls()}
        self.assertTrue(all(c['argv'] == ['--json'] or
                            (c['x'] == 'jdk' and c['argv'] == ['--json', '--goal', 'build'])
                            for c in saw.values()))
        self.assertTrue(all(c['FLINK_HOME'] is None and c['NEXMARK_HOME'] is None
                            and c['GFV_REPOS_DIR'] is None for c in saw.values()))
        self.assertEqual((payload[KEY]['flink_home'], payload[KEY]['nexmark_home'],
                          payload[KEY]['repos_dir']), (None, None, None))

    # --- --repos-dir pins cpp-deps to the same checkout -----------------------
    def test_repos_dir_pins_cpp_deps_to_the_same_velox_checkout(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build', '--repos-dir', '/rd'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        call = self.call_for('cpp_deps')
        # --velox-dir (the checkout cpp-deps itself probes) and GFV_REPOS_DIR (its
        # discovery root) both sit inside the one repos-dir the user gave —
        # ahead of any VELOX_HOME, the same checkout source-repos inspects
        self.assertEqual(call['argv'], ['--json', '--velox-dir', '/rd/velox'])
        self.assertEqual(call['GFV_REPOS_DIR'], '/rd')
        self.assertEqual(self.call_for('source_repos')['argv'], ['--json', '--repos-dir', '/rd'])
        # left to discovery: nothing is pinned
        self.capture.unlink()
        proc, payload = self.run_tool(['--goal', 'build'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.call_for('cpp_deps')['argv'], ['--json'])

    # --- an explicit path is the only target ----------------------------------
    def test_invalid_explicit_homes_are_refused_before_any_check_runs(self):
        self.build_family()
        flink_home = self.base / 'flink-home'
        (flink_home / 'bin').mkdir(parents=True, exist_ok=True)
        (flink_home / 'bin' / 'flink').write_text('#!/bin/sh\n', encoding='utf-8')
        (flink_home / 'bin' / 'flink').chmod(0o644)  # there, but not runnable
        for args in (['--flink-home', str(self.base / 'nope')],      # no such home
                     ['--flink-home', str(flink_home)],              # bin/flink not runnable
                     ['--nexmark-home', str(self.base / 'nope')]):    # no run_query.sh
            proc, payload = self.run_tool(['--goal', 'build'] + args)
            self.assertEqual(proc.returncode, 2, (args, proc.stderr))
            self.assertIsNone(payload, args)  # refused on stderr, no JSON verdict
            self.assertIn('has no bin/', proc.stderr)
        self.assertFalse(self.capture.exists())  # nothing ran: no check fell back anywhere
        # a runnable bin/flink passes the gate and reaches the checks as FLINK_HOME,
        # the report still carrying the directory the user gave
        (flink_home / 'bin' / 'flink').chmod(0o755)
        proc, payload = self.run_tool(['--goal', 'build', '--flink-home', str(flink_home)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload[KEY]['flink_home'], str(flink_home))
        for x in ('flink_dist', 'jdk', 'lib_state', 'cluster', 'nexmark'):
            self.assertEqual(self.call_for(x)['FLINK_HOME'], str(flink_home), x)

    # --- warn aggregates without blocking --------------------------------------
    def test_warn_aggregates_without_blocking_next_step(self):
        self.build_family()
        proc, payload = self.run_tool(['--goal', 'build'],
                                      switches={'ENV_CHECK_STUB_MODE_LIB_STATE': 'warn'})
        self.assertEqual(proc.returncode, 0)
        c = self.check(payload, 'lib-state')
        self.assertEqual(c['status'], 'warn')
        self.assertTrue(c['success'])
        self.assertIn('warning: duplicates: stub warning for lib_state', c['detail'])
        self.assertTrue(payload[KEY]['success'])
        self.assertEqual(payload[KEY]['next_step'], 'goal build is ready: all checks pass')
        human, _ = self.run_tool(['--goal', 'build'], as_json=False,
                                 switches={'ENV_CHECK_STUB_MODE_LIB_STATE': 'warn'})
        self.assertTrue(any('WARN' in ln and 'lib-state' in ln and 'stub warning for lib_state' in ln
                            for ln in human.stdout.splitlines()), human.stdout)
        self.assertIn('READY: goal build, all checks pass', human.stdout)

    # --- the suggested commands run as printed ----------------------------------
    def test_suggested_commands_run_as_printed(self):
        self.build_family(drop=('cpp_deps_check.py',))
        proc, payload = self.run_tool(['--goal', 'build'])
        cmd = re.search(r', then rerun (python3 .+)$', payload[KEY]['next_step'])
        self.assertIsNotNone(cmd, payload[KEY]['next_step'])
        self.build_family()  # the sibling restored: the rerun must find the family complete
        rerun = subprocess.run(['bash', '-c', cmd.group(1)], cwd=str(self.base),
                               env=self.env(), capture_output=True, text=True, timeout=180)
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)  # family complete again
        # a crashed check's next step points at the sibling script itself
        proc, payload = self.run_tool(['--goal', 'build'],
                                      switches={'ENV_CHECK_STUB_MODE_JDK': 'crash'})
        cmd = re.search(r"error: (python3 .+)$", self.check(payload, 'jdk')['next_step'])
        self.assertIsNotNone(cmd)
        byhand = subprocess.run(['bash', '-c', cmd.group(1)], cwd=str(self.base),
                                env=self.env(), capture_output=True, text=True, timeout=180)
        self.assertEqual(byhand.returncode, 0, byhand.stdout + byhand.stderr)
        self.assertIn('flink_velox_jdk_check_result', byhand.stdout)

    # --- the manual re-check repeats the exact invocation ----------------------
    def test_manual_recheck_commands_repeat_the_exact_invocation(self):
        self.build_family()
        fh = self.make_flink_home()
        proc, payload = self.run_tool(
            ['--goal', 'build', '--flink-home', str(fh), '--repos-dir', '/rd', '--timeout', '2'],
            switches={'ENV_CHECK_STUB_MODE_SOURCE_REPOS': 'timeout',
                      'ENV_CHECK_STUB_MODE_CPP_DEPS': 'crash',
                      'ENV_CHECK_STUB_MODE_FLINK_DIST': 'crash'})
        self.assertEqual(proc.returncode, 1)
        # the hung source-repos: the line to run by hand is the one this run made
        cmd = re.search(r'hangs: (.+)$', self.check(payload, 'source-repos')['next_step']).group(1)
        self.assertEqual(cmd, 'python3 %s --json --repos-dir /rd'
                         % shlex.quote(self.stub_path('flink-velox-source-repos-check')))
        # a crashed check: the locator env prefixes and every flag it actually got
        cmd = re.search(r'error: (.+)$', self.check(payload, 'cpp-deps')['next_step']).group(1)
        self.assertEqual(cmd, 'GFV_REPOS_DIR=/rd python3 %s --json --velox-dir /rd/velox'
                         % shlex.quote(self.stub_path('flink-velox-cpp-deps-check')))
        cmd = re.search(r'error: (.+)$', self.check(payload, 'flink-dist')['next_step']).group(1)
        self.assertEqual(cmd, 'FLINK_HOME=%s python3 %s --json'
                         % (shlex.quote(str(fh)), shlex.quote(self.stub_path('flink-velox-flink-dist-check'))))
        # and each line runs exactly as printed, landing on the same locators
        for x in ('source-repos', 'cpp-deps', 'flink-dist'):
            cmd = re.search(r'(?:hangs|error): (.+)$', self.check(payload, x)['next_step']).group(1)
            rerun = subprocess.run(['bash', '-c', cmd], cwd=str(self.base), env=self.env(),
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
            self.assertIn('flink_velox_%s_check_result' % x.replace('-', '_'), rerun.stdout)
        # the rechecks saw the same locators the first pass pinned
        last = {}
        for c in self.calls():
            last[c['x']] = c
        self.assertEqual(last['source_repos']['argv'], ['--json', '--repos-dir', '/rd'])
        self.assertEqual(last['cpp_deps']['argv'], ['--json', '--velox-dir', '/rd/velox'])
        self.assertEqual(last['cpp_deps']['GFV_REPOS_DIR'], '/rd')
        self.assertEqual(last['flink_dist']['FLINK_HOME'], str(fh))

    # --- the real tree: ten skills, names matching directories ------------------
    def test_real_family_integrity(self):
        # reads the real source tree only — no stubs, nothing executed
        expected = {skill: script for _g, _n, skill, script in FAMILY_CHECKS}
        expected['flink-velox-env-check'] = 'env_check.py'
        for skill, script in expected.items():
            d = REAL_FAMILY / skill
            self.assertTrue(d.is_dir(), d)
            self.assertTrue((d / 'SKILL.md').is_file(), skill)
            self.assertTrue((d / 'scripts' / script).is_file(), '%s/scripts/%s' % (skill, script))
            text = (d / 'SKILL.md').read_text(encoding='utf-8')
            m = re.search(r'^name:\s*(\S+)', text, re.M)
            self.assertTrue(m, skill)
            self.assertEqual(m.group(1), skill)
        listed = sorted(p.name for p in REAL_FAMILY.iterdir() if p.is_dir())
        self.assertEqual(listed, sorted(expected))


if __name__ == '__main__':
    unittest.main()
