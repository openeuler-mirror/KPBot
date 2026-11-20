# -*- coding: utf-8 -*-
"""采集端：在宿主机上跑，采指定容器的环境。

* 探针按行返回结果，这边边读边存。某一项卡住被超时掐掉时，之前采到的
  项目全都留着，并在结果里记下是哪一项超时了。
* 不管采集成功与否都会写出 bundle 和人读摘要。
"""
import datetime as dt
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import signal
import tarfile
import threading
import time

from . import probe

SCHEMA = 1
COLLECTOR_VERSION = '1.0.0'
# 下面这些是内部参数，不做成命令行开关：
# PROBE_TIMEOUT 是一次探针的上限，CONTAINER_BUDGET 是一个容器连同重试的上限。
# 它们的作用是「一个卡住的命令不会把整次采集拖死」，不需要使用者关心。
PROBE_TIMEOUT = 300
CONTAINER_BUDGET = PROBE_TIMEOUT + 60


def safe_name(name):
    return re.sub(r'[^a-zA-Z0-9_.\-]+', '_', str(name)).strip('._') or 'target'


def payload_source():
    """探针源码，可以直接喂给 `python3 -`。"""
    here = pathlib.Path(__file__).resolve().parent
    probe_src = (here / 'probe.py').read_text(encoding='utf-8')
    return probe_src + '\nimport sys\nsys.exit(main(sys.argv[1:]))\n'


def _reader(stream, out, lock):
    for raw in iter(stream.readline, b''):
        with lock:
            out.append(raw.decode('utf-8', 'replace'))
    try:
        stream.close()
    except Exception:
        pass


def run_probe(args, payload, config, timeout, sink=None):
    """跑探针，边跑边收行。超时只丢没采完的那一项。

    `sink` 是调用方提供的列表：收到的行会实时 append 进去，所以即使这里
    抛出中断异常，调用方手里也有已经采到的部分。
    """
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as e:
        return [], '无法启动探针: %s: %s' % (type(e).__name__, e), None
    lines = sink if sink is not None else []
    lock = threading.Lock()
    err_chunks = []
    out_thread = threading.Thread(target=_reader, args=(proc.stdout, lines, lock))
    err_thread = threading.Thread(target=_reader, args=(proc.stderr, err_chunks, lock))
    out_thread.daemon = err_thread.daemon = True
    out_thread.start()
    err_thread.start()

    def write_stdin():
        try:
            proc.stdin.write(payload.encode('utf-8'))
            proc.stdin.close()
        except Exception:
            pass

    writer = threading.Thread(target=write_stdin)
    writer.daemon = True
    writer.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
    out_thread.join(timeout=5)
    err_thread.join(timeout=5)
    with lock:
        collected = list(lines)
        stderr = ''.join(err_chunks)[:4000]
    error = None
    if timed_out:
        error = '探针超过 %s 秒未结束，已终止；已采到的项目保留' % timeout
    elif proc.returncode not in (0, None):
        detail = stderr.strip()[:300]
        if not detail:
            detail = '没有错误输出' + ('（通常是容器里没有这个命令）'
                                 if proc.returncode == 127 else '')
        error = '探针退出码 %s: %s' % (proc.returncode, detail)
    del config
    return collected, error, proc.returncode


def parse_lines(lines):
    """JSONL -> ({item: record}, [解析失败的行])"""
    items, broken = {}, []
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except ValueError:
            broken.append(text[:200])
            continue
        if isinstance(record, dict) and record.get('item'):
            items[record['item']] = record
        else:
            broken.append(text[:200])
    return items, broken


def to_inventory(items):
    """把按项的结果汇成一份 inventory，供比较端读取。"""
    def data(name):
        record = items.get(name) or {}
        return record.get('data') or {}

    system = data('system')
    profiles = data('python_profiles').get('profiles') or []
    inventory = {
        'schema': SCHEMA,
        'probe_python': system.get('probe_python'),
        'os_release': system.get('os_release'),
        'uname': system.get('uname') or {},
        'glibc': system.get('glibc') or [],
        'cpu_count': system.get('cpu_count'),
        'cpu_affinity_count': system.get('cpu_affinity_count'),
        'meminfo': system.get('meminfo') or {},
        'loadavg': system.get('loadavg'),
        'kernel': data('kernel'),
        'cgroup': data('cgroup'),
        'tools': data('tools'),
        'npu_smi': {'stdout': data('npu_smi').get('info', ''),
                    'mapping': data('npu_smi').get('mapping'),
                    'path': data('npu_smi').get('path')},
        'lscpu': {'stdout': data('lscpu').get('stdout', '')},
        'numa': data('numa'),
        'cann_driver_version_files': data('ascend_versions').get('files') or {},
        'ascend_home': data('ascend_versions').get('ascend_home'),
        'ascend_dirs': data('ascend_dirs').get('entries') or [],
        'git': dict((path, {'rev': rec.get('head', ''), 'describe': rec.get('describe', ''),
                            'dirty_files': rec.get('dirty_files')})
                    for path, rec in (data('git').get('repos') or {}).items()),
        'processes': data('processes').get('processes') or [],
        'process_scan': {'scanned': data('processes').get('scanned'),
                         'unreadable': data('processes').get('unreadable')},
        'profiles': [_profile_for_compare(p) for p in profiles],
        'items': dict((name, {'status': record.get('status'),
                              'reason': record.get('reason'),
                              'seconds': record.get('seconds')})
                      for name, record in items.items() if not name.startswith('_')),
    }
    return inventory


def _profile_for_compare(profile):
    """比较端读画像用的字段名和探针输出不同，这里做一次映射。"""
    packages = profile.get('packages') or {}
    out = dict(profile)
    out['python_executable_used'] = profile.get('python_executable')
    out['environment_source'] = profile.get('chosen_by')
    out['collection_ok'] = bool(profile.get('ok'))
    out['pip_list'] = [{'name': name, 'version': version}
                       for name, version in sorted(packages.items())]
    return out


DOCKER_LIMIT_KEYS = ('NetworkMode', 'IpcMode', 'PidMode', 'ShmSize', 'Memory',
                     'MemorySwap', 'NanoCpus', 'CpuQuota', 'CpuPeriod', 'CpusetCpus',
                     'CpusetMems', 'Runtime', 'Privileged', 'RestartPolicy', 'Ulimits')


def safe_inspect(raw):
    config = raw.get('Config') or {}
    host_config = raw.get('HostConfig') or {}
    state = raw.get('State') or {}
    env_list = config.get('Env') or []
    env_dict = dict(item.split('=', 1) for item in env_list if '=' in item)
    return {
        'id': raw.get('Id'), 'name': raw.get('Name'), 'created': raw.get('Created'),
        'image_id': raw.get('Image'), 'image_ref': config.get('Image'),
        'state': dict((k, state.get(k)) for k in ('Status', 'Running', 'StartedAt', 'Pid', 'ExitCode')),
        'user': config.get('User'), 'working_dir': config.get('WorkingDir'),
        'entrypoint': config.get('Entrypoint'),
        'cmd': config.get('Cmd'),
        # config_env_relevant 装的是容器定义里的完整环境变量
        'config_env_relevant': env_dict,
        'ports': (raw.get('NetworkSettings') or {}).get('Ports'),
        'mounts': [dict((k, m.get(k)) for k in ('Type', 'Source', 'Destination', 'Mode', 'RW'))
                   for m in (raw.get('Mounts') or [])],
        'limits': dict((k, host_config.get(k)) for k in DOCKER_LIMIT_KEYS),
        'devices': [dict((k, d.get(k)) for k in ('PathOnHost', 'PathInContainer', 'CgroupPermissions'))
                    for d in (host_config.get('Devices') or [])],
        'device_requests': host_config.get('DeviceRequests'),
    }


def probe_pgid(lines):
    """从探针第一行里取它的进程组号。"""
    for raw in lines[:3]:
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if record.get('item') == '_begin':
            return (record.get('data') or {}).get('pgid')
    return None


def kill_in_container(container, pgid, timeout=20):
    """把容器里探针的整个进程组清掉。

    宿主机超时只杀得掉 docker exec 客户端，容器里的探针和它启动的 `npu-smi`
    会继续跑。探针自己也有总时限兜底，这里是主动清理。
    """
    if not pgid:
        return '没有拿到探针进程组号，无法主动清理（探针自身总时限仍会生效）'
    script = ('kill -TERM -%d 2>/dev/null; sleep 1; kill -KILL -%d 2>/dev/null; exit 0'
              % (pgid, pgid))
    _, error = docker_cmd(['exec', container, 'sh', '-c', script], timeout=timeout)
    return error


def docker_cmd(args, timeout=30):
    try:
        proc = subprocess.run(['docker'] + args, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, 'docker %s 超过 %s 秒未结束' % (args[0], timeout)
    except OSError as e:
        return None, '无法执行 docker: %s' % e
    if proc.returncode != 0:
        return None, proc.stderr.decode('utf-8', 'replace').strip()[:400]
    return proc.stdout.decode('utf-8', 'replace'), None


# ---------------------------------------------------------------------------
def collect_container(container, config, timeout, pythons, budget=None, sink=None):
    """容器采集：依次尝试几个 python 可执行名，第一个能跑通的就用它。

    整个容器共用一个总预算，每次尝试只能用剩下的时间，并且每次尝试结束
    都清理容器内的进程组，避免候选名逐个等满超时、在容器里积压卡住的进程。
    """
    payload = payload_source()
    attempts = []
    budget = budget if budget else timeout + 60
    deadline = time.time() + budget
    for python in pythons:
        left = deadline - time.time()
        if left <= 5:
            attempts.append('%s: 容器总预算 %s 秒用完，没有再试' % (python, budget))
            break
        this_timeout = int(min(timeout, left))
        # sink 要直接交给 run_probe 当实时缓冲：中断异常是从 run_probe 里抛出来的，
        # 事后再拷贝就来不及了，调用方会拿到一个空列表。
        if sink is not None:
            del sink[:]
            attempt_lines = sink
        else:
            attempt_lines = []
        lines, error, rc = run_probe(
            ['docker', 'exec', '-i', container, python, '-',
             json.dumps(dict(config, deadline=max(10, this_timeout - 5)), ensure_ascii=False)],
            payload, config, this_timeout, sink=attempt_lines)
        items, broken = parse_lines(lines)
        if error and lines:
            # 探针没有任何输出，说明它没有启动，不需要清理
            cleanup_note = kill_in_container(container, probe_pgid(lines))
            if cleanup_note:
                error += '；容器内清理: %s' % cleanup_note
        if items:
            note = ('%s: %s' % (python, error)) if error else None
            return lines, note, attempts + ([] if not note else [note])
        attempts.append('%s: %s' % (python, error or '没有任何输出'))
    # 六个候选名往往是同一个原因，合并相同原因，避免重复输出
    reasons = {}
    for attempt in attempts:
        name, _, reason = attempt.partition(': ')
        reasons.setdefault(reason, []).append(name)
    summary = '; '.join('%s: %s' % ('/'.join(names), reason)
                        for reason, names in reasons.items())
    return [], '容器里没有可用的 Python（试过 %d 个名字）：%s' % (len(attempts), summary), attempts


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + '\n',
                    encoding='utf-8')


def collect_target(name, folder, lines, probe_error, docker_info, attempts):
    folder.mkdir(parents=True, exist_ok=True)
    if lines:
        (folder / 'raw.jsonl').write_text(''.join(lines), encoding='utf-8')
    items, broken = parse_lines(lines)
    inventory = to_inventory(items)
    status = {
        'probe_error': probe_error,
        'attempts': attempts,
        'broken_lines': broken,
        'completed': sorted(n for n, r in items.items()
                            if r.get('status') == 'ok' and not n.startswith('_')),
        'skipped': dict((n, r.get('reason')) for n, r in items.items()
                        if r.get('status') == 'skip'),
        'failed': dict((n, r.get('reason')) for n, r in items.items()
                       if r.get('status') == 'error'),
        'never_started': [n for n, _ in probe.ITEMS if n not in items],
        'finished': '_end' in items,
    }
    inventory['collection_status'] = status
    if docker_info is not None:
        write_json(folder / 'docker_inspect_safe.json', docker_info)
    write_json(folder / 'inventory.json', inventory)
    return inventory, status


def summary_text(meta, targets):
    lines = ['P/D 环境采集结果（collector v%s, schema %d）' % (COLLECTOR_VERSION, SCHEMA),
             '采集时间: %s' % meta['timestamp_local'],
             '宿主机:   %s' % meta['node'],
             '角色:     %s' % (meta.get('role') or '未指定'),
             '采集对象: %s' % ', '.join(meta['targets']),
             '']
    for name, (inventory, status) in targets.items():
        lines.append('=' * 68)
        lines.append('[%s]' % name)
        if status.get('probe_error'):
            lines.append('  探针问题: %s' % status['probe_error'])
        profiles = inventory.get('profiles') or []
        primary = next((p for p in profiles if p.get('collection_ok')), profiles[0] if profiles else {})
        packages = dict((p['name'].lower().replace('_', '-'), p['version'])
                        for p in (primary.get('pip_list') or []))
        interp = (primary.get('interpreter') or {}).get('version') or ''
        lines.append('  Python:   %s' % (interp.splitlines()[0] if interp.strip() else '未采到'))
        lines.append('  包来源:   %s（共 %d 个包）'
                     % (primary.get('packages_source') or '未采到', len(packages)))
        for key in ('vllm', 'vllm-ascend', 'torch', 'torch-npu', 'transformers',
                    'tokenizers', 'fastokens'):
            if key in packages:
                lines.append('    %-14s %s' % (key, packages[key]))
        versions = inventory.get('cann_driver_version_files') or {}
        lines.append('  版本文件: %d 个%s' % (len(versions),
                     ('，含 ' + ', '.join(sorted(versions)[:2]) + ' 等') if versions else ''))
        npu = (inventory.get('npu_smi') or {}).get('stdout') or ''
        lines.append('  npu-smi:  %s' % ('已采集 %d 行' % len(npu.splitlines()) if npu.strip() else '未采集'))
        procs = inventory.get('processes') or []
        lines.append('  进程:     %d 个推理相关进程' % len(procs))
        if procs:
            lines.append('    %s' % ' '.join(str(a) for a in (procs[0].get('args') or [])[:10])[:160])
        lines.append('  采集项:   成功 %d，跳过 %d，失败 %d，未开始 %d%s'
                     % (len(status['completed']), len(status['skipped']),
                        len(status['failed']), len(status['never_started']),
                        '' if status['finished'] else '（探针未正常结束）'))
        for item, reason in sorted(status['skipped'].items()):
            lines.append('    跳过 %-18s %s' % (item, reason))
        for item, reason in sorted(status['failed'].items()):
            lines.append('    失败 %-18s %s' % (item, reason))
        for item in status['never_started']:
            lines.append('    未开始 %s' % item)
    lines += ['', '=' * 68, '说明:',
              '  * 环境变量和启动参数一律原样记录，不做遮盖，里面可能有口令和 token；',
              '    这份采集包按内部资料保管，对外分享前请自查。',
              '  * 跳过(skip)表示这项在当前环境不适用，不是故障。']
    return '\n'.join(lines) + '\n'


class Interrupted(Exception):
    """收到 Ctrl-C 或 SIGTERM。"""


def _install_interrupt():
    """把中断变成异常，这样 finally 里仍能把已采到的部分写出来。"""
    def handler(signum, frame):
        raise Interrupted('收到信号 %d' % signum)
    installed = []
    for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            installed.append((signum, signal.signal(signum, handler)))
        except (OSError, ValueError, RuntimeError):
            pass
    return installed


def _unique_folder(out, used, name):
    """同名采集对象不能互相覆盖（例如容器就叫 local，或同名写两次）。"""
    base = safe_name(name)
    candidate = base
    index = 2
    while candidate in used:
        candidate = '%s-%d' % (base, index)
        index += 1
    used.add(candidate)
    return out / candidate, candidate


def _acquire_lock(out):
    """同一个输出目录只允许一个 collect 在写。"""
    path = out / '.pdenv.lock'
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None, ('输出目录正在被另一个 collect 使用：%s\n'
                      '如果上一次是异常退出，删掉这个文件再试：%s' % (out, path))
    except OSError as e:
        return None, '无法在输出目录创建锁文件：%s' % e
    try:
        os.write(fd, ('pid %d\n' % os.getpid()).encode())
    finally:
        os.close(fd)
    return path, None


def run(args):
    """采集端的实现（入口 scripts/collect.sh）。返回退出码。"""
    duplicates = [c for c in args.containers if args.containers.count(c) > 1]
    if duplicates:
        print('同一个容器写了多次：%s' % ', '.join(sorted(set(duplicates))), file=sys.stderr)
        return 1
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    try:
        out = (pathlib.Path(args.out).expanduser().resolve() if args.out else
               pathlib.Path('pdenv_%s' % stamp).resolve())
        if out.exists() and any(out.iterdir()):
            print('输出目录不是空的：%s' % out, file=sys.stderr)
            print('请换一个 --out，或先把它清空。', file=sys.stderr)
            return 1
        os.umask(0o077)
        out.mkdir(parents=True, exist_ok=True)
        try:
            out.chmod(0o700)
        except OSError:
            pass
    except OSError as e:
        print('无法准备输出目录 %s：%s' % (args.out, e), file=sys.stderr)
        print('请检查路径是否存在、是否有写权限、磁盘是否已满。', file=sys.stderr)
        return 1
    lock, lock_error = _acquire_lock(out)
    if lock_error:
        print(lock_error, file=sys.stderr)
        return 1

    config = {'python': args.python}
    node = platform.node() or 'unknown'
    meta = {
        'schema': SCHEMA, 'collector_version': COLLECTOR_VERSION,
        'role': args.role, 'node': node,
        'timestamp_utc': stamp,
        'timestamp_local': dt.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z'),
        'targets': [], 'folders': {},
        'host_platform': platform.platform(),
        'interrupted': False,
    }
    targets = {}
    used_folders = set()
    pending = []          # 正在采集的对象，中断时用来保住它已采到的部分
    restore = _install_interrupt()
    print('输出目录: %s' % out, flush=True)

    def record(name, folder_name, inventory, status):
        targets[name] = (inventory, status)
        meta['targets'].append(name)
        meta['folders'][name] = folder_name
        print('  完成：成功 %d 项，跳过 %d 项，失败 %d 项%s'
              % (len(status['completed']), len(status['skipped']), len(status['failed']),
                 '，' + str(status['probe_error']) if status['probe_error'] else ''), flush=True)

    try:
        for container in args.containers:
            print('采集容器 %s …' % container, flush=True)
            folder, folder_name = _unique_folder(out, used_folders, container)
            raw, error = docker_cmd(['inspect', container], timeout=30)
            if error:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'ERROR.txt').write_text('docker inspect 失败: %s\n' % error,
                                                  encoding='utf-8')
                print('  跳过：docker inspect 失败: %s' % error, file=sys.stderr)
                record(container, folder_name, {'profiles': []},
                       {'probe_error': 'docker inspect 失败: ' + error,
                        'completed': [], 'skipped': {}, 'failed': {},
                        'never_started': [n for n, _ in probe.ITEMS],
                        'finished': False, 'attempts': [], 'broken_lines': []})
                continue
            docker_info, running = None, False
            try:
                parsed = json.loads(raw)
                docker_info = safe_inspect(parsed[0])
                running = bool((parsed[0].get('State') or {}).get('Running'))
            except (ValueError, IndexError, KeyError) as e:
                docker_info = {'parse_error': str(e)}
            lines, error, attempts = [], None, []
            if running:
                inflight = {'name': container, 'folder': folder, 'folder_name': folder_name,
                            'lines': [], 'docker': docker_info}
                pending.append(inflight)
                lines, error, attempts = collect_container(
                    container, config, PROBE_TIMEOUT, _pythons(args.python),
                    CONTAINER_BUDGET, sink=inflight['lines'])
                pending.pop()
            else:
                error = '容器不在运行中，只采到了 docker inspect'
            inventory, status = collect_target(container, folder, lines, error,
                                               docker_info, attempts)
            record(container, folder_name, inventory, status)
    except Interrupted as e:
        meta['interrupted'] = True
        meta['interrupt_reason'] = str(e)
        print('\n被中断（%s）：正在把已采到的部分写出来 …' % e, file=sys.stderr, flush=True)
        for item in pending:
            # 先把容器里的探针清掉，再把这个对象已采到的几项写出来
            note = kill_in_container(item['name'], probe_pgid(item['lines']))
            inventory, status = collect_target(
                item['name'], item['folder'], item['lines'],
                '采集被中断' + ('；容器内清理: %s' % note if note else '；容器内探针已清理'),
                item.get('docker'), [])
            targets[item['name']] = (inventory, status)
            meta['targets'].append(item['name'])
            meta['folders'][item['name']] = item['folder_name']
            print('  %s：已采到 %d 项，已写出' % (item['name'], len(status['completed'])),
                  file=sys.stderr)
    except OSError as e:
        meta['write_error'] = str(e)
        print('\n写文件出错：%s（磁盘满或权限不足？）' % e, file=sys.stderr)
    finally:
        for signum, old in restore:
            try:
                signal.signal(signum, old)
            except (OSError, ValueError, RuntimeError):
                pass
        code = _finish(out, meta, targets, args, lock)
    return code


def write_report(out, meta):
    """采完就地生成一份可以直接打开的报告。

    只采一个容器也要能看结果，不必非得 compare 两份。
    多个容器时同一张表里并排显示，并标出哪些项不一样。
    """
    from . import bundle as bundle_mod, diff, fields, report
    if not meta.get('targets'):
        return None
    try:
        data = bundle_mod.load_bundle('本次采集', out, out)
        entries = []
        for container in data['containers']:
            entry = fields.build_entry(container['container'], data, container)
            entries.append(entry)
        if not entries:
            return None
        matrix = diff.matrix_rows(entries)
        html = report.html_report(entries, matrix, [], [], meta['timestamp_local'])
        path = out / '环境报告.html'
        path.write_text(html, encoding='utf-8')
        return path
    except Exception as e:      # 报告生成失败不该让采集结果作废
        print('生成报告失败（采集结果仍然可用）: %s: %s' % (type(e).__name__, e),
              file=sys.stderr)
        return None


def _finish(out, meta, targets, args, lock):
    """不管是正常结束还是被中断，meta、摘要、报告、打包都要写出来。"""
    try:
        write_json(out / 'collection_meta.json', meta)
        (out / 'SUMMARY.txt').write_text(summary_text(meta, targets), encoding='utf-8')
    except OSError as e:
        print('写 meta 或摘要失败：%s' % e, file=sys.stderr)
    html = write_report(out, meta)
    if html:
        print('报告: %s' % html)
    archive = pathlib.Path(str(out) + '.tar.gz')
    try:
        with tarfile.open(str(archive), 'w:gz') as tar:
            tar.add(str(out), arcname=out.name)
        print('打包: %s' % archive)
    except OSError as e:
        print('打包失败（目录仍然可用）: %s' % e, file=sys.stderr)
    if lock is not None:
        try:
            os.unlink(str(lock))
        except OSError:
            pass
    print('摘要: %s' % (out / 'SUMMARY.txt'))
    usable = [name for name, (_, status) in targets.items() if status['completed']]
    if meta.get('interrupted'):
        print('已中断：%d 个对象留下了数据，目录和摘要都已写出。' % len(usable), file=sys.stderr)
        return 130
    if not usable:
        print('没有任何采集对象成功，请看 SUMMARY.txt 里的原因。', file=sys.stderr)
        return 3
    return 0


def _pythons(explicit):
    order = [explicit] if explicit else []
    order += ['python3', 'python', 'python3.12', 'python3.11', 'python3.10', 'python3.9']
    seen, out = set(), []
    for item in order:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
