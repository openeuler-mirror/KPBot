#!/usr/bin/env python3
"""env_check.py — read-only orchestrator over the flink-velox-env check family.

One command answers "what is this machine still missing for the goal":
--goal build (compile and run the GlutenFlink stack) runs all nine sibling
checks; --goal native (run the native side only) records the four build-side
checks disabled and runs the other five. Each check is delegated to its sibling
skill's script as a subprocess (python3 <script> --json, per-check timeout)
and its result — success / items / missing / next_step — is aggregated into
six groups (build_env / repos / java / flink / lib / cluster). The report
carries exactly one next_step: the first failing or missing group in the
priority order build_env → flink → java → lib → cluster (the repos group sits
in the build-preparation tier with build_env — both route their work orders to
flink-velox-build-env-prepare); a missing group says "install the missing check
skill(s)", a failing one quotes its first failed check's own next_step
verbatim — the work order for the installing family. A sibling script that is
not there is never a pass: the run reports NOT VERIFIED and exits 1.

Locators are passed through to the checks that need them and otherwise left to
each check's own discovery: --flink-home reaches flink-dist / jdk / cluster /
lib-state / nexmark as FLINK_HOME, --nexmark-home reaches nexmark as
NEXMARK_HOME, --repos-dir reaches source-repos as --repos-dir (its own CLI
form) and cpp-deps as GFV_REPOS_DIR plus --velox-dir <repos-dir>/velox (the
two checks pinned to the same checkout, ahead of any VELOX_HOME), --goal
reaches jdk as its own --goal (build wants 17, native accepts 8/11/17) and,
for the native goal, lib-state as --expect native (the native goal runs on
the native build; a GlutenFlink side is a mismatch handed back to the switch
step — the build goal accepts either side and pins nothing). An explicit
locator is the only target: --flink-home without a runnable bin/flink or
--nexmark-home without a runnable bin/run_query.sh is refused (exit 2)
before anything runs, so no check can fall back to PATH or the default
locations and report some other installation READY. A check that hangs is
killed as a whole process group (its own session, SIGKILL to the group), so
the java/mvn/flink grandchildren it spawned go with it instead of lingering
as orphans; the manual re-check commands next_step suggests repeat the exact
invocation this run made — interpreter, absolute script path, every locator
flag and env — never a bare command that could succeed against another
installation. This skill is read-only: it installs nothing, fixes nothing,
restarts nothing, and writes nowhere except the optional --out file.

Usage:
  env_check.py [plan] --goal {build,native} [--flink-home DIR] [--nexmark-home DIR]
                      [--repos-dir DIR] [--timeout SEC] [--json] [--out FILE [--overwrite]]

Exit codes: 0 every executed check ok (warnings allowed) and none missing;
1 at least one check failing or a sibling skill missing (NOT VERIFIED);
2 tool error (JSON in the full contract shape, error_message "tool aborted: ..."),
a refused --out, a refused explicit locator (--flink-home without bin/flink,
--nexmark-home without bin/run_query.sh), or an invalid command line (usage on
stderr, no JSON).
"""
import argparse
import datetime
import json
import os
import shlex
import signal
import stat
import subprocess
import sys

KEY = 'flink_velox_env_check_result'
SCRIPT = os.path.realpath(__file__)

DEFAULT_TIMEOUT = 120  # one check; a stuck check must not hang the whole run

# The six groups in report order. The next_step priority walks a different
# order: the build-preparation side first (build_env, then repos — both route
# their work orders to flink-velox-build-env-prepare), then flink, java, lib,
# cluster — the order the environment is usually fixed in.
GROUPS = ('build_env', 'repos', 'java', 'flink', 'lib', 'cluster')
PRIORITY = ('build_env', 'repos', 'flink', 'java', 'lib', 'cluster')

# (group, check name, sibling skill, script, locator needs)
#   F  FLINK_HOME env, from --flink-home
#   N  NEXMARK_HOME env, from --nexmark-home
#   R  --repos-dir argv, source-repos' own CLI form
#   V  --velox-dir argv, from --repos-dir (pins cpp-deps to the same checkout,
#      ahead of any VELOX_HOME)
#   G  GFV_REPOS_DIR env, from --repos-dir (cpp-deps' velox checkout root)
#   J  --goal argv, jdk's own goal switch
#   E  --expect native argv, from the goal (lib-state only: the native goal
#      runs on the native build; the build goal accepts either side)
CHECKS = (
    ('build_env', 'toolchain', 'flink-velox-toolchain-check', 'toolchain_check.py', ''),
    ('build_env', 'build-tools', 'flink-velox-build-tools-check', 'build_tools_check.py', ''),
    ('build_env', 'cpp-deps', 'flink-velox-cpp-deps-check', 'cpp_deps_check.py', 'GV'),
    ('repos', 'source-repos', 'flink-velox-source-repos-check', 'source_repos_check.py', 'R'),
    ('java', 'jdk', 'flink-velox-jdk-check', 'jdk_check.py', 'FJ'),
    ('flink', 'flink-dist', 'flink-velox-flink-dist-check', 'flink_dist_check.py', 'F'),
    ('lib', 'lib-state', 'flink-velox-lib-state-check', 'lib_state_check.py', 'FE'),
    ('cluster', 'cluster', 'flink-velox-cluster-check', 'cluster_check.py', 'F'),
    ('cluster', 'nexmark', 'flink-velox-nexmark-check', 'nexmark_check.py', 'FN'),
)

# goal=native does not build anything: the build-preparation groups are not run.
NATIVE_SKIP_GROUPS = ('build_env', 'repos')


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def checker_path(skill, script):
    """Where a sibling check skill lives. An explicit family dir
    (ENV_PREPARE_ENV_FAMILY_DIR) is authoritative — nothing else is searched;
    otherwise the skill is looked up next to this skill's directory
    (<family>/<check>/scripts/<script> in the source tree — the same relative
    shape the flat install produces, where every skill sits one level under the
    skills root, so one candidate covers both layouts)."""
    here = os.path.dirname(os.path.abspath(__file__))
    family = os.environ.get('ENV_PREPARE_ENV_FAMILY_DIR')
    cands = [os.path.join(family, skill, 'scripts', script)] if family else [
        os.path.join(here, '..', '..', skill, 'scripts', script)]
    return next((os.path.realpath(c) for c in cands if os.path.isfile(c)), None)


def command_line(opts):
    """This tool's own rerun line, interpreter and absolute script path included
    (it is neither on PATH nor in the working directory); the locators the user
    pinned are kept, the ones left to discovery stay left to it."""
    parts = ['python3', shlex.quote(SCRIPT), '--goal', opts['goal']]
    for flag in ('flink-home', 'nexmark-home', 'repos-dir'):
        value = opts[flag.replace('-', '_')]
        if value is not None:
            parts += ['--%s' % flag, shlex.quote(value)]
    if opts['explicit']['timeout'] is not None:
        parts += ['--timeout', str(opts['timeout'])]
    return ' '.join(parts)


def is_exec(path):
    """A runnable file — the family's own criterion for an installation."""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def bad_locator(opts):
    """Why an explicit locator is refused, or None. An explicit path is the
    only target (the family rule): --flink-home without a runnable bin/flink
    or --nexmark-home without a runnable bin/run_query.sh fails right here,
    before anything runs — a check left to fall back to PATH or the default
    locations could report some other installation READY."""
    if opts['flink_home'] is not None and not is_exec(os.path.join(opts['flink_home'], 'bin', 'flink')):
        return ('--flink-home %s has no bin/flink — an explicit home is the only target, '
                'fix the path or leave the checks to their own discovery' % opts['flink_home'])
    if opts['nexmark_home'] is not None \
            and not is_exec(os.path.join(opts['nexmark_home'], 'bin', 'run_query.sh')):
        return ('--nexmark-home %s has no bin/run_query.sh — an explicit home is the only target, '
                'fix the path or leave the check to its own discovery' % opts['nexmark_home'])
    return None


def manual_command(argv, env_inject):
    """The line that reruns one check by hand exactly as this run invoked it:
    the locator env this run pinned, then the interpreter, the absolute
    script path and every flag the check actually got — so a recheck lands on
    the same installation the hung or crashed check was looking at, never on
    whatever PATH or the default locations would offer."""
    parts = ['%s=%s' % (k, shlex.quote(v)) for k, v in env_inject.items()]
    parts += ['python3'] + [shlex.quote(a) for a in argv[1:]]
    return ' '.join(parts)


def kill_group(proc):
    """Take a hung check down whole: it runs in its own session, and the
    group signal reaches the java/mvn/flink grandchildren it spawned —
    killing the direct child alone would leave them orphaned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def blank_check(entry, status, success, detail, next_step):
    return {'name': entry[1], 'skill': entry[2], 'status': status, 'success': success,
            'detail': detail, 'missing': [], 'next_step': next_step}


def run_check(entry, opts):
    """One delegated check: locate the sibling script, run it as a subprocess
    with --json in its own session (so a hung check can be killed as a whole
    process group), take success / items / missing / next_step from its first
    *_result key. A sibling that is not installed is 'missing' (never a
    pass); one that crashes, times out or prints no result is a 'fail' with
    the reason in the detail and this run's exact command line — locators
    included — in the next_step."""
    group, name, skill, script, needs = entry
    if opts['goal'] == 'native' and group in NATIVE_SKIP_GROUPS:
        return blank_check(entry, 'disabled', None, 'goal=native does not need it', '')
    path = checker_path(skill, script)
    if path is None:
        return blank_check(entry, 'missing', False,
                           '%s is not installed next to this skill' % skill,
                           'install the missing check skill(s) (%s), then rerun %s'
                           % (skill, command_line(opts)))
    argv = [sys.executable, path, '--json']
    if 'R' in needs and opts['repos_dir'] is not None:
        argv += ['--repos-dir', opts['repos_dir']]
    if 'V' in needs and opts['repos_dir'] is not None:
        argv += ['--velox-dir', os.path.join(opts['repos_dir'], 'velox')]
    if 'J' in needs:
        argv += ['--goal', opts['goal']]
    if 'E' in needs and opts['goal'] == 'native':
        argv += ['--expect', 'native']
    env_inject = {}
    if 'F' in needs and opts['flink_home'] is not None:
        env_inject['FLINK_HOME'] = opts['flink_home']
    if 'N' in needs and opts['nexmark_home'] is not None:
        env_inject['NEXMARK_HOME'] = opts['nexmark_home']
    if 'G' in needs and opts['repos_dir'] is not None:
        env_inject['GFV_REPOS_DIR'] = opts['repos_dir']
    env = dict(os.environ)
    env.update(env_inject)
    byhand = manual_command(argv, env_inject)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, start_new_session=True)
    except OSError as exc:
        return blank_check(entry, 'fail', False,
                           '%s did not run: %s' % (skill, exc.strerror or exc),
                           "see the check's own error: %s" % byhand)
    try:
        out, _err = proc.communicate(timeout=opts['timeout'])
    except subprocess.TimeoutExpired:
        kill_group(proc)
        proc.communicate()  # reap; the group is dead, so the pipes close
        return blank_check(entry, 'fail', False,
                           '%s did not finish within %ss' % (skill, opts['timeout']),
                           'see where it hangs: %s' % byhand)
    try:
        payload = json.loads(out)
        r = payload[next(k for k in payload if k.endswith('_result'))]
        missing = r.get('missing') or []
        warns = [i for i in (r.get('items') or [])
                 if isinstance(i, dict) and i.get('status') == 'warn']
        ok = bool(r.get('success'))
        status = 'fail' if not ok else ('warn' if r.get('status') == 'warn' else 'ok')
        if not ok:
            detail = r.get('error_message') or 'the check reported failure'
        elif warns:
            detail = 'warning: ' + '; '.join('%s: %s' % (i.get('name') or 'item',
                                                        i.get('detail') or '') for i in warns)
        else:
            detail = r.get('next_step') or ''
        # a result that is not an object is left to raise: the tool aborts
        # rather than guess what a malformed sibling result meant
        return {'name': name, 'skill': skill, 'status': status, 'success': ok,
                'detail': detail,
                'missing': list(missing) if isinstance(missing, list) else [],
                'next_step': r.get('next_step') or ''}
    except (ValueError, StopIteration, TypeError):
        return blank_check(entry, 'fail', False,
                           '%s produced no result (exit %s)' % (skill, proc.returncode),
                           "see the check's own error: %s" % byhand)


def next_step_for(groups, goal, cmd):
    """The one next_step: the first group in priority order with a fail or a
    missing check decides. Missing wins inside a group (an unrun check is not
    a diagnosis) and names every missing skill of the run; a fail quotes the
    group's first failed check's own next_step verbatim. Warnings never
    intercept; all green says the goal is ready."""
    flat = [c for g in groups for c in g['checks']]
    missing = [c for c in flat if c['status'] == 'missing']
    for gname in PRIORITY:
        checks = next(g['checks'] for g in groups if g['name'] == gname)
        if any(c['status'] == 'missing' for c in checks):
            return 'install the missing check skill(s) (%s), then rerun %s' % (
                ', '.join(c['skill'] for c in missing), cmd)
        fails = [c for c in checks if c['status'] == 'fail']
        if fails:
            return fails[0]['next_step'] or ('fix the failing %s check, then rerun %s'
                                             % (fails[0]['skill'], cmd))
    return 'goal %s is ready: all checks pass' % goal


def check_all(opts):
    groups = [{'name': g, 'checks': [run_check(e, opts) for e in CHECKS if e[0] == g]}
              for g in GROUPS]
    flat = [c for g in groups for c in g['checks']]
    failing = [c for c in flat if c['status'] == 'fail']
    missing = [c for c in flat if c['status'] == 'missing']
    missing_items = [m for c in flat for m in c['missing']]
    error = ''
    if failing:
        error = '%d check(s) failing' % len(failing)
    if missing:
        error = ('; ' if error else '') + '%d check(s) not run (sibling skills not installed): %s' \
            % (len(missing), ', '.join(c['skill'] for c in missing))
    result = {KEY: {'success': not failing and not missing,
                    'command': 'plan', 'goal': opts['goal'], 'checked_at': now(),
                    'flink_home': opts['flink_home'], 'nexmark_home': opts['nexmark_home'],
                    'repos_dir': opts['repos_dir'], 'timeout': opts['timeout'],
                    'groups': groups, 'missing': missing_items,
                    'next_step': next_step_for(groups, opts['goal'], command_line(opts)),
                    'error_message': error}}
    return result, 0 if not failing and not missing else 1


def aborted(exc, opts):
    return {KEY: {'success': False, 'command': 'plan', 'goal': opts.get('goal'),
                  'checked_at': now(), 'flink_home': opts.get('flink_home'),
                  'nexmark_home': opts.get('nexmark_home'), 'repos_dir': opts.get('repos_dir'),
                  'timeout': opts.get('timeout'), 'groups': [], 'missing': [],
                  'next_step': 'fix the tool error first (see error_message), then rerun',
                  'error_message': 'tool aborted: %r' % exc}}


# --- --out -------------------------------------------------------------------
OUT_SCOPE = ('--out must be a regular path inside the project directory (the working directory), '
             'not a symlink or a path outside it')


def out_problem(path, overwrite):
    """Why --out cannot be written, or None. Checked before anything runs and
    again at the write."""
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(os.path.realpath(os.getcwd()) + os.sep):
        return '%s: %s' % (OUT_SCOPE, path)
    if os.path.exists(target):
        if not overwrite:
            return '--out already exists, pass --overwrite to replace it: %s' % path
        if not stat.S_ISREG(os.stat(target).st_mode):  # a fifo would block open() until a reader shows up
            return '--out exists and is not a regular file, refusing to overwrite it: %s' % path
    return None


def write_out(path, result, overwrite):
    problem = out_problem(path, overwrite)
    if problem:
        print('env_check.py: %s' % problem, file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print('env_check.py: cannot write --out %s: %s' % (path, exc.strerror or exc), file=sys.stderr)
        return 2
    return 0


# --- rendering ---------------------------------------------------------------
MARKS = {'ok': 'OK       ', 'warn': 'WARN     ', 'fail': 'FAIL     ',
         'disabled': 'DISABLED ', 'missing': 'MISSING  '}


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('tool aborted'):
        return 'TOOL ERROR: %s\nnext: %s' % (r['error_message'], r['next_step'])
    checks = [c for g in r['groups'] for c in g['checks']]
    lines = ['env-check %s  (goal %s; %d check(s) in %d group(s))'
             % (r['command'], r['goal'], len(checks), len(r['groups']))]
    for g in r['groups']:
        lines.append('%s:' % g['name'])
        for c in g['checks']:
            lines.append('  %s %-11s %s' % (MARKS[c['status']], c['name'], c['detail']))
            if c['status'] in ('fail', 'missing'):
                lines.append('        next: %s' % c['next_step'])
    failing = [c for c in checks if c['status'] == 'fail']
    missing = [c for c in checks if c['status'] == 'missing']
    if missing:
        lines.append('NOT VERIFIED: %d check(s) not run (sibling skills not installed): %s'
                     % (len(missing), ', '.join(c['skill'] for c in missing)))
    if failing:
        lines.append('NOT READY: %d check(s) failing' % len(failing))
    if not failing and not missing:
        lines.append('READY: goal %s, all checks pass' % r['goal'])
    lines.append('next: %s' % r['next_step'])
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only orchestrator over the flink-velox-env '
                                                 'check family: one command, one goal, one next_step.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan',),
                        help='this skill only diagnoses; plan (the default) is all there is')
    parser.add_argument('--goal', required=True, choices=('build', 'native'),
                        help='build: compile and run the GlutenFlink stack (all 9 checks); '
                             'native: run the native side only (the 4 build-side checks disabled)')
    parser.add_argument('--flink-home', help='the Flink installation the checks look at '
                                             '(default: $FLINK_HOME when set, else each check\'s own discovery)')
    parser.add_argument('--nexmark-home', help='the runnable nexmark tree for the nexmark check '
                                               '(default: $NEXMARK_HOME when set, else its own discovery)')
    parser.add_argument('--repos-dir', help='directory of the velox/ velox4j/ gluten/ checkouts '
                                            '(default: $GFV_REPOS_DIR when set, else each check\'s own discovery)')
    parser.add_argument('--timeout', type=int, help='per-check timeout in seconds (default %d)' % DEFAULT_TIMEOUT)
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project '
                                      'directory; an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing regular file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)

    if args.timeout is not None and args.timeout <= 0:
        print('env_check.py: --timeout must be a positive number of seconds', file=sys.stderr)
        return 2

    opts = {'goal': args.goal,
            'flink_home': args.flink_home, 'nexmark_home': args.nexmark_home,
            'repos_dir': args.repos_dir, 'timeout': args.timeout or DEFAULT_TIMEOUT,
            'explicit': {'timeout': args.timeout}}

    if args.out:  # before anything runs: a bad --out must not start the checks
        problem = out_problem(args.out, args.overwrite)
        if problem:
            print('env_check.py: %s' % problem, file=sys.stderr)
            return 2

    problem = bad_locator(opts)  # same rule: a bad explicit path starts nothing
    if problem:
        print('env_check.py: %s' % problem, file=sys.stderr)
        return 2

    try:
        result, code = check_all(opts)
    except Exception as exc:  # a tool bug must never be reported as "present"
        result, code = aborted(exc, opts), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
