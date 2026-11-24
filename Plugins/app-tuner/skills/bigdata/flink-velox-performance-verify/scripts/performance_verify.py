#!/usr/bin/env python3
"""performance_verify.py — run the nexmark throughput benchmark on the native Flink side.

This version runs on the native build only (the Flink whose lib holds no gluten /
velox jar): the same nexmark queries, replayed at a fixed calibre, produce the native
baseline that a later GlutenFlink-side run will be compared against. Submitting jobs
to the GlutenFlink side is out of scope here.

The fixed calibre (only numbers taken at the same calibre compare):
  1e8 events, 5e7 tps, blackhole sink (the queries bring their own DDL) and a warmup
  pass, defined once in conf/nexmark-bench.yaml. The file is swapped in as
  $NEXMARK_HOME/conf/nexmark.yaml for the duration of a run and the original is
  restored afterwards — also on failure or interrupt. parallelism.default and
  pipeline.object-reuse are set per cell in the config file Flink actually reads
  (flink-conf.yaml when both exist, else config.yaml) and restored the same way.
  Total slots (workers x taskmanager.numberOfTaskSlots) must cover the largest
  requested parallelism, or the run is refused before anything is touched.

Subcommands, the same skeleton as the flink-velox install family:
  plan  (default)  probe and print the exact matrix and conf changes; touch nothing
  run   --yes      execute the cells, capture each runner's "Nexmark Results" table,
                   stop at the first failing cell (the confs are restored regardless)

Usage:
  performance_verify.py [plan]   [--flink-home DIR] [--nexmark-home DIR]
                                        [--queries q0,q1,...] [--parallelism 1,4,8,16]
                                        [--object-reuse] [--timeout SEC] [--evidence-dir DIR]
                                        [--json] [--out FILE]
  performance_verify.py run --yes [the same options]

Evidence lands in <project>/e2e/perf/native/ by default (the working directory is the
project root): qNN.p<P>.r0.out (the metrics table, object-reuse=false),
qNN.p<P>.r1.out (object-reuse=true, only with --object-reuse) and *.submit.log
beside each — the evidence tree is refreshed in place across runs. Only data, no
verdicts: rows whose events count differs from the requested calibre are marked not
comparable; the interpretation stays with the reader.

Exit codes: plan 0 / 2 refused (a blocked prerequisite, a bad --out, bad options);
run 0 every cell done, 1 a cell failed (the report says which; rerun after fixing,
the matrix resumes), 2 refused; 2 on a tool abort (the confs are still restored).
"""
import argparse
import datetime
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

KEY = 'flink_velox_performance_verify_result'
SCRIPT = os.path.realpath(__file__)
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXED_CONF = os.path.join(SKILL_DIR, 'conf', 'nexmark-bench.yaml')

# The calibre this skill pins; conf/nexmark-bench.yaml is the single source for the runner.
CALIBRE_EVENTS = 100000000
CALIBRE_TPS = 50000000
# Every query the fixed suite defines; --queries must stay inside this list.
SUITE_QUERIES = tuple('q%d' % n for n in range(24) if n != 6)  # q6 is not runnable (upstream)

CONF_FILES = ('flink-conf.yaml', 'config.yaml')  # Flink 1.19 reads flink-conf.yaml and only it
GLUTEN_JARS = ('gluten', 'velox')  # a jar whose name carries one of these means not the native side
REST_KEYS = ('rest.address', 'rest.port')
BACKUP_SUFFIX = '.flink-velox-perf.bak'
RESULTS_BANNER = 'Nexmark Results'
DEFAULT_PARALLELISM = (1, 4, 8, 16)
DEFAULT_TIMEOUT = 3600  # one cell: warmup plus 1e8 events; a stuck cell must not hang the matrix
QUERIES_HELP = 'comma-separated queries, default q0 (the fixed suite: ' + ','.join(SUITE_QUERIES) + ')'


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


# --- probing -----------------------------------------------------------------
def load_flink_conf(conf_path):
    """Flatten the one conf file Flink reads — config.yaml (nested maps) or
    flink-conf.yaml (flat dotted keys) — without a YAML dependency. Scalar leaves
    only. Flink 1.19 ships config.yaml nested ('rest:' / 'port: 8159'); the older
    flink-conf.yaml form is flat ('rest.port: 8159'); both flatten to dotted keys."""
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
    """The lib jars that make the installation not native (gluten / velox / velox4j)."""
    jars = []
    for jar in sorted(glob.glob(os.path.join(flink_home, 'lib', '*.jar'))):
        name = os.path.basename(jar).lower()
        if any(word in name for word in GLUTEN_JARS):
            jars.append(os.path.basename(jar))
    return jars


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


def command_line(opts, sub, yes=False, sudo=False):
    """The command for the options that were actually reviewed, ready to copy: interpreter and
    the path of this script included (it is neither on PATH nor in the working directory)."""
    parts = ['sudo'] if sudo else []
    parts += ['python3', shlex.quote(SCRIPT), sub] + (['--yes'] if yes else [])
    for flag in ('flink-home', 'nexmark-home', 'queries', 'parallelism', 'timeout', 'evidence-dir'):
        key = flag.replace('-', '_')
        if key in opts['explicit']:
            parts += ['--%s' % flag, shlex.quote(str(opts['explicit'][key]))]
    if opts['object_reuse']:
        parts.append('--object-reuse')
    return ' '.join(parts)


def blocked(reason, fix):
    return {'status': 'blocked', 'detail': reason, 'fix': fix}


def probe(opts):
    """Everything the matrix needs, with blocked findings instead of exceptions."""
    findings = []
    flink_home, nexmark_home = opts['flink_home'], opts['nexmark_home']
    state = {'flink_home': flink_home, 'nexmark_home': nexmark_home,
             'effective_conf': None, 'rest': None, 'slots': None}

    if not os.path.isdir(flink_home):
        findings.append(blocked('Flink home not found: %s' % flink_home,
                                'point --flink-home at the Flink installation (or set FLINK_HOME)'))
        return findings, state
    if not os.path.isfile(os.path.join(flink_home, 'bin', 'flink')):
        findings.append(blocked('bin/flink is missing under %s' % flink_home,
                                'is this a Flink installation?'))

    conf = effective_conf(flink_home)
    state['effective_conf'] = conf
    if not os.path.isfile(conf):
        findings.append(blocked('no config file under %s/conf (flink-conf.yaml / config.yaml)' % flink_home,
                                'install Flink or point --flink-home elsewhere'))
    foreign = foreign_jars(flink_home)
    if foreign:
        findings.append(blocked(
            'the lib holds GlutenFlink jars (%s) — this skill runs on the native side only'
            % ', '.join(foreign),
            'switch the cluster to the native side first (flink-velox-cluster-switch --to native; '
            'it restarts the cluster), then rerun plan'))

    if os.path.isfile(conf):
        if not os.access(conf, os.W_OK):
            findings.append(blocked('cannot write %s' % conf,
                                    'run as a user who can, or: %s'
                                    % command_line(opts, 'run', yes=True, sudo=True)))
        address, port = rest_endpoint(conf)
        state['rest'] = {'address': address, 'port': port}
        tms, per_tm, total = slot_count(flink_home, conf)
        state['slots'] = {'tms': tms, 'per_tm': per_tm, 'total': total}
        if total < max(opts['parallelism_int']):
            findings.append(blocked(
                'cluster has %d slot(s) (%d workers x %d), less than the largest requested parallelism %d'
                % (total, tms, per_tm, max(opts['parallelism_int'])),
                'lower --parallelism, or raise taskmanager.numberOfTaskSlots and restart the cluster first'))
        if not rest_alive(address, port):
            findings.append(blocked('the cluster REST endpoint %s:%s does not answer' % (address, port),
                                    'start the Flink cluster first, then rerun plan'))

    runner = os.path.join(nexmark_home, 'bin', 'run_query.sh')
    if not os.path.isfile(runner):
        findings.append(blocked('no nexmark runner under %s/bin' % nexmark_home,
                                'install the runnable nexmark tree (flink-velox-runtime-install), '
                                'or point --nexmark-home at it'))
    else:
        nexmark_conf = os.path.join(nexmark_home, 'conf', 'nexmark.yaml')
        if not os.path.isfile(nexmark_conf):
            findings.append(blocked('no conf/nexmark.yaml under %s' % nexmark_home,
                                    'complete the nexmark tree first'))
        elif not os.access(nexmark_conf, os.W_OK):
            findings.append(blocked('cannot write %s' % nexmark_conf,
                                    'run as a user who can, or: %s'
                                    % command_line(opts, 'run', yes=True, sudo=True)))
        elif leftover_state(nexmark_conf) == 'gone':
            findings.append(blocked(
                '%s holds this skill\'s benchmark conf and the original backup is gone '
                '(a previous run crashed without restoring)' % nexmark_conf,
                'restore conf/nexmark.yaml by hand, then rerun plan'))

    unknown = [q for q in opts['queries'] if q not in SUITE_QUERIES]
    if unknown:
        findings.append(blocked('query %s is not in the fixed suite' % ' '.join(unknown),
                                 'pick from: ' + ', '.join(SUITE_QUERIES)))
    return findings, state


def cells(opts):
    """The matrix in execution order: each query at each parallelism, r0 then r1 (--object-reuse)."""
    out = []
    for query in opts['queries']:
        for par in opts['parallelism_int']:
            out.append((query, par, False))
            if opts['object_reuse']:
                out.append((query, par, True))
    return out


def plan(opts):
    findings, state = probe(opts)
    matrix = [] if findings else cells(opts)
    preview = []
    if matrix:
        rest = state['rest']
        preview = ['%s: parallelism.default and pipeline.object-reuse set per cell, restored afterwards'
                   % os.path.basename(state['effective_conf']),
                   'conf/nexmark.yaml: swapped in as a whole by this skill\'s fixed conf '
                   '(1e8 events, 5e7 tps, REST %s:%s), restored afterwards'
                   % (rest['address'], rest['port']),
                   'evidence: %s/qNN.p<P>.r0.out (+ r1 with --object-reuse) and *.submit.log'
                   % opts['evidence_dir']]
    return findings, matrix, preview, state


# --- conf management ---------------------------------------------------------
def backup_path(path):
    return path + BACKUP_SUFFIX


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
    calls this: plan touches nothing, the repair happens in run before anything else."""
    bak = backup_path(path)
    if os.path.isfile(bak):
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
    """Copy a conf to its backup with an exclusive create: a directory at the backup
    path, or a backup that somehow already exists, is an error and not a silent copy
    into that directory (shutil.copy2 would happily do that and the restore would
    then quietly skip — the user's conf would be the one lost)."""
    with open(path, 'rb') as src, open(bak, 'xb') as dst:
        dst.write(src.read())
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
    """Swap the two confs for the duration of a run and restore them afterwards, whatever happens.
    Leftovers of a previous crashed run are repaired first (the backups are restored on enter)."""

    def __init__(self, flink_conf, nexmark_conf, address, port, changes):
        self.flink_conf, self.nexmark_conf = flink_conf, nexmark_conf
        self.text = fixed_conf_text(address, port)
        self.changes = changes
        self.entered = False

    def __enter__(self):
        backed_up = []
        try:
            for path in (self.flink_conf, self.nexmark_conf):
                backup_file(path, backup_path(path))
                backed_up.append(path)
                self.changes.append({'action': 'backed up',
                                     'detail': '%s -> %s' % (path, backup_path(path))})
            with open(self.nexmark_conf, 'w', encoding='utf-8') as fh:
                fh.write(self.text)
        except Exception:  # a crash half-way in: undo the backups already taken, report nothing swapped
            for path in backed_up:
                os.replace(backup_path(path), path)
            raise
        self.changes.append({'action': 'swapped in',
                             'detail': 'the fixed benchmark conf -> %s' % self.nexmark_conf})
        self.entered = True
        return self

    def set_flink(self, parallelism, object_reuse):
        self._set_key('parallelism.default', str(parallelism))
        self._set_key('pipeline.object-reuse', 'true' if object_reuse else 'false')

    def _set_key(self, key, value):
        with open(self.flink_conf, encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        lines = set_conf_key_lines(lines, key, value)
        with open(self.flink_conf, 'w', encoding='utf-8') as fh:
            fh.writelines(lines)

    def __exit__(self, *exc):
        if not self.entered:
            return False
        for path in (self.flink_conf, self.nexmark_conf):
            bak = backup_path(path)
            if os.path.isfile(bak):
                os.replace(bak, path)
                self.changes.append({'action': 'restored', 'detail': '%s -> %s' % (bak, path)})
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


def evidence_paths(evidence_dir, query, par, reuse):
    tag = '%s.p%d.r%d' % (query, par, 1 if reuse else 0)
    return (os.path.join(evidence_dir, tag + '.out'),
            os.path.join(evidence_dir, tag + '.submit.log'))


def run_cell(record, opts, swap):
    """Submit one cell to the cluster and capture its table. Returns True when the cell is done."""
    query, par, reuse = record['query'], record['parallelism'], record['object_reuse']
    table_path, log_path = evidence_paths(opts['evidence_dir'], query, par, reuse)
    record.update(table=table_path, submit_log=log_path)
    swap.set_flink(par, reuse)
    record['flink'] = {'parallelism.default': par, 'pipeline.object-reuse': reuse}
    runner = os.path.join(opts['nexmark_home'], 'bin', 'run_query.sh')
    started = time.monotonic()
    try:
        proc = subprocess.run([runner, 'oa', query], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=opts['timeout'], env=dict(os.environ, FLINK_HOME=opts['flink_home']))
    except subprocess.TimeoutExpired:
        record.update(status='failed', duration_s=round(time.monotonic() - started, 1),
                      detail='the runner did not finish within %ss' % opts['timeout'])
        return False
    except KeyboardInterrupt:  # SIGINT / SIGTERM: this cell is interrupted, the confs still restore
        record.update(status='failed', duration_s=round(time.monotonic() - started, 1),
                      detail='interrupted (SIGINT / SIGTERM)')
        raise
    except OSError as exc:
        record.update(status='failed', duration_s=round(time.monotonic() - started, 1),
                      detail='the runner did not start: %s' % (exc.strerror or exc))
        return False
    record.update(duration_s=round(time.monotonic() - started, 1), runner_exit=proc.returncode)
    os.makedirs(opts['evidence_dir'], exist_ok=True)
    with open(log_path, 'wb') as fh:
        fh.write(proc.stdout)
    text = proc.stdout.decode('utf-8', errors='replace')
    rows = parse_results_table(text)
    with open(table_path, 'w', encoding='utf-8') as fh:
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
    record['comparable'] = row['events_num'] == CALIBRE_EVENTS
    record['status'] = 'done'
    record['detail'] = ('%d events in %ss, %s events/s'
                        % (row['events_num'], row['time_s'], row['throughput'])
                        + ('' if record['comparable']
                           else ' — events differ from the calibre, not comparable'))
    return True


# --- assembly ----------------------------------------------------------------
def base_result(command, opts, state, runs, changes, success, error, next_step, confs_restored=None):
    r = {'success': success, 'checked_at': now(), 'command': command, 'side': 'native',
         'flink_home': opts['flink_home'], 'nexmark_home': opts['nexmark_home'],
         'effective_conf': state.get('effective_conf'), 'rest': state.get('rest'),
         'slots': state.get('slots'), 'calibre': {'events': CALIBRE_EVENTS, 'tps': CALIBRE_TPS},
         'queries': opts['queries'], 'parallelism': opts['parallelism_int'],
         'object_reuse': opts['object_reuse'], 'timeout': opts['timeout'],
         'evidence_dir': opts['evidence_dir'], 'runs': runs, 'changes': changes,
         'next_step': next_step, 'error_message': error}
    if confs_restored is not None:
        r['confs_restored'] = confs_restored
    return {KEY: r}


def refused(command, opts, state, findings):
    error = 'refused: ' + '; '.join(f['detail'] for f in findings)
    return base_result(command, opts, state, [], [], False, error,
                       '; '.join(f['fix'] for f in findings) or 'fix the findings above, then rerun plan')


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
    return '%d cell(s) done; the metrics are in %s and in this result' % (len(matrix), opts['evidence_dir'])


def build_plan(opts):
    findings, matrix, preview, state = plan(opts)
    result = base_result('plan', opts, state, [], [], not findings,
                         'refused: ' + '; '.join(f['detail'] for f in findings) if findings else '',
                         next_step_for('plan', findings, matrix, opts))
    return result, 0 if not findings else 2, preview


def run_all(opts, yes):
    findings, state = probe(opts)
    if findings:
        return refused('run', opts, state, findings), 2
    if not yes:
        return refused('run', opts, state,
                       [blocked('run needs --yes (show the user the plan first; '
                                '--yes is the record of their consent)', 'plan first, then add --yes')]), 2

    matrix = cells(opts)
    runs = [{'query': q, 'parallelism': p, 'object_reuse': ru, 'status': 'pending'}
            for q, p, ru in matrix]
    changes = []
    rest = state['rest']
    nexmark_conf = os.path.join(opts['nexmark_home'], 'conf', 'nexmark.yaml')
    for path in (state['effective_conf'], nexmark_conf):
        if repair_leftover(path):
            changes.append({'action': 'restored',
                            'detail': '%s -> %s (leftover from a crashed run)' % (backup_path(path), path)})
    interrupted = [None]

    try:
        with ConfSwap(state['effective_conf'], nexmark_conf, rest['address'], rest['port'], changes) as swap:
            try:
                for record in runs:
                    if not run_cell(record, opts, swap):
                        break
            except KeyboardInterrupt:
                interrupted[0] = 'a cell was interrupted (SIGINT / SIGTERM)'
                for record in runs:
                    if record['status'] == 'pending' and 'detail' not in record:
                        record['detail'] = interrupted[0]
    except KeyboardInterrupt:
        interrupted[0] = interrupted[0] or 'interrupted before the confs were swapped in'

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
    result = base_result('run', opts, state, runs, changes, not failed and not not_run and not interrupted[0],
                         error, next_step_for('run', [], matrix, opts, runs), True)
    return result, 1 if (failed or interrupted[0] or not_run) else 0


def aborted(exc, command, opts):
    return {KEY: {'success': False, 'checked_at': now(), 'command': command, 'side': 'native',
                  'flink_home': opts.get('flink_home'), 'nexmark_home': opts.get('nexmark_home'),
                  'effective_conf': None, 'rest': None, 'slots': None,
                  'calibre': {'events': CALIBRE_EVENTS, 'tps': CALIBRE_TPS},
                  'queries': opts.get('queries'), 'parallelism': opts.get('parallelism_int'),
                  'object_reuse': opts.get('object_reuse'), 'timeout': opts.get('timeout'),
                  'evidence_dir': opts.get('evidence_dir'), 'runs': [], 'changes': [],
                  'confs_restored': False,
                  'next_step': 'fix the tool error first (see error_message), then rerun',
                  'error_message': 'tool aborted: %r' % exc}}


# --- --out -------------------------------------------------------------------
OUT_SCOPE = ('--out must be a regular path inside the project directory (the working directory), '
             'not a symlink or a path outside it')


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


def write_out(path, result, overwrite):
    problem = out_problem(path, overwrite)
    if problem:
        print('performance_verify.py: %s' % problem, file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w' if overwrite else 'x', encoding='utf-8') as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        print('performance_verify.py: cannot write --out %s: %s' % (path, exc.strerror or exc), file=sys.stderr)
        return 2
    return 0


# --- rendering ---------------------------------------------------------------
def render(result, preview=None):
    r = result[KEY]
    if r['error_message'].startswith('tool aborted'):
        return 'TOOL ERROR: %s\nnext: %s' % (r['error_message'], r['next_step'])
    lines = ['performance-verify %s  (native side, nexmark 1e8 events / 5e7 tps, blackhole)' % r['command'],
             '  flink %s' % r['flink_home']]
    if r.get('rest'):
        lines.append('  rest %s:%s   slots %s' % (r['rest']['address'], r['rest']['port'],
                                                  r['slots']['total'] if r.get('slots') else '?'))
    if r['error_message'].startswith('refused'):
        lines.append('  REFUSED  %s' % r['error_message'][len('refused: '):])
    for line in (preview or []):
        lines.append('  %s' % line)
    if r['command'] == 'plan':
        n = len(r['queries']) * len(r['parallelism']) * (2 if r['object_reuse'] else 1)
        lines.append('  PLAN: %d cell(s) — %s' % (n, r['next_step']))
    else:
        marks = {'done': 'DONE    ', 'failed': 'FAILED  ', 'pending': 'NOT RUN '}
        for run in r['runs']:
            lines.append('  %s.p%d.r%d  %s%s' % (run['query'], run['parallelism'],
                                                  1 if run['object_reuse'] else 0,
                                                  marks[run['status']], run.get('detail', '')))
        if r.get('confs_restored') is False:
            lines.append('  confs NOT restored — restore them by hand from the %s files' % BACKUP_SUFFIX)
        elif r.get('confs_restored'):
            lines.append('  confs restored')
        lines.append('BENCHMARK: all cells done' if r['success'] else 'NOT DONE: %s' % (
            r['error_message'] or 'see the failed cell above'))
    lines.append('next: %s' % r['next_step'])
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the nexmark throughput benchmark on the native Flink '
                                                 'side (1e8 events / 5e7 tps / blackhole, fixed calibre). '
                                                 'plan by default; run needs --yes.')
    parser.add_argument('command', nargs='?', default='plan', choices=('plan', 'run'))
    parser.add_argument('--flink-home', help='the Flink installation to benchmark (default: $FLINK_HOME)')
    parser.add_argument('--nexmark-home', help='the runnable nexmark tree (default: $NEXMARK_HOME, then /opt/nexmark)')
    parser.add_argument('--queries', help=QUERIES_HELP)
    parser.add_argument('--parallelism', help='comma-separated parallelism steps, default 1,4,8,16')
    parser.add_argument('--object-reuse', action='store_true',
                        help='also run r1 (pipeline.object-reuse=true), the second native baseline')
    parser.add_argument('--timeout', type=int, help='per-cell runner timeout in seconds (default %d)' % DEFAULT_TIMEOUT)
    parser.add_argument('--evidence-dir', help='where qNN.p<P>.rN.out and *.submit.log land '
                                               '(default <working directory>/e2e/perf/native)')
    parser.add_argument('--yes', action='store_true', help='run: the record that the user saw the plan and consented')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project directory; '
                                      'an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing regular file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)

    try:
        queries = parse_list(args.queries or 'q0', r'^q\d+$', 'queries')
        parallelism = [int(p) for p in parse_list(args.parallelism
                                                 or ','.join(str(n) for n in DEFAULT_PARALLELISM),
                                                 r'^[1-9]\d*$', 'parallelism')]
    except ValueError as exc:
        print('performance_verify.py: %s' % exc, file=sys.stderr)
        return 2

    opts = {'flink_home': args.flink_home or os.environ.get('FLINK_HOME') or '/usr/local/flink',
            'nexmark_home': args.nexmark_home or os.environ.get('NEXMARK_HOME') or '/opt/nexmark',
            'queries': queries, 'parallelism_int': parallelism, 'object_reuse': args.object_reuse,
            'timeout': args.timeout or DEFAULT_TIMEOUT,
            'evidence_dir': os.path.abspath(args.evidence_dir or os.path.join(os.getcwd(), 'e2e', 'perf', 'native')),
            'explicit': {flag: getattr(args, flag) for flag in
                        ('flink_home', 'nexmark_home', 'queries', 'parallelism', 'timeout', 'evidence_dir')
                        if getattr(args, flag) is not None}}

    if not os.path.realpath(opts['evidence_dir']).startswith(os.path.realpath(os.getcwd()) + os.sep):
        print('performance_verify.py: the evidence directory must be inside the project directory '
              '(the working directory): %s' % opts['evidence_dir'], file=sys.stderr)
        return 2
    if args.out:
        problem = out_problem(args.out, args.overwrite)  # before anything runs: a bad --out must not touch the machine
        if problem:
            print('performance_verify.py: %s' % problem, file=sys.stderr)
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
