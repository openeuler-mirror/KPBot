# -*- coding: utf-8 -*-
"""只读环境探针：在一台机器或一个容器里采集环境信息。

采集端把这个文件的源码用 `docker exec -i 容器 python3 -` 送进容器执行。

两点设计都是为了「一项失败不影响其他项」：

1. 每一项单独计时、单独超时、单独捕获异常。采不到就记原因，继续下一项。
2. 结果按行输出（每项一个 JSON 对象）。采集端即使中途不得不杀掉探针，
   在那之前已经完成的项目全都保得住。
"""
import glob
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import time

SCHEMA = 1
# 单项超时：一个命令卡住时，只影响这一项，其他项照常采。不做成命令行开关。
TIMEOUTS = {
    'cmd': 15,        # 一般命令
    'npu_smi': 25,    # npu-smi 在卡多的机器上慢
    'python': 20,     # 解释器自省
    'packages': 60,   # 列包
    'git': 10,
}
MAX_PROCESSES = 40   # 上限 40，避免进程数多的机器上结果过大
MAX_PROFILES = 4     # 最多给几个 Python 环境做画像


class Skip(Exception):
    """这一项在当前环境里不适用，不算失败。"""


# 正在跑的子进程，用于中断和超时时统一清理
_CHILDREN = set()


def kill_tree(proc):
    """杀掉一个子进程及它的整个进程组，先 TERM 再 KILL。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (OSError, AttributeError):
            try:
                proc.kill()
            except OSError:
                pass
            return
        if sig is signal.SIGTERM:
            for _ in range(20):
                if proc.poll() is not None:
                    return
                time.sleep(0.05)


def kill_all_children():
    for proc in list(_CHILDREN):
        if proc.poll() is None:
            kill_tree(proc)
        _CHILDREN.discard(proc)


def _on_deadline(signum, frame):
    """总时限到了：清掉子进程，报一行，然后立刻退出。

    采集端那边超时只能杀掉宿主机上的 docker exec 客户端，杀不到容器里的
    探针。所以探针自己也要有总时限，宿主机那边中断了它也会自行退出。
    """
    kill_all_children()
    try:
        sys.stdout.write(json.dumps(
            {'item': '_deadline', 'status': 'error',
             'reason': '探针到达自身总时限，已清理子进程并退出'},
            ensure_ascii=False) + '\n')
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(4)


def _on_signal(signum, frame):
    kill_all_children()
    try:
        sys.stdout.write(json.dumps(
            {'item': '_interrupted', 'status': 'error',
             'reason': '探针收到信号 %d，已清理子进程并退出' % signum},
            ensure_ascii=False) + '\n')
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(5)


def install_guards(deadline_seconds):
    """自成进程组 + 总时限 + 信号处理。进不去也不影响采集。"""
    notes = []
    try:
        os.setsid()
    except (OSError, AttributeError):
        notes.append('setsid 失败（已是进程组组长或平台不支持）')
    for signum in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGINT', None),
                   getattr(signal, 'SIGHUP', None), getattr(signal, 'SIGPIPE', None)):
        if signum is None:
            continue
        try:
            signal.signal(signum, _on_signal)
        except (OSError, ValueError, RuntimeError):
            pass
    if deadline_seconds and hasattr(signal, 'SIGALRM'):
        try:
            signal.signal(signal.SIGALRM, _on_deadline)
            signal.alarm(int(deadline_seconds))
        except (OSError, ValueError, RuntimeError):
            notes.append('设不了 SIGALRM 总时限')
    elif deadline_seconds:
        notes.append('平台不支持 SIGALRM，没有总时限')
    return notes


def _timeout(cfg, key):
    del cfg
    return TIMEOUTS[key]


def run(args, timeout, env=None, cwd=None, limit=200000):
    """执行命令，从不抛异常。返回 rc/stdout/stderr 或 error。

    子进程放进自己的进程组（start_new_session），超时时杀整组：只杀直接
    子进程的话，`npu-smi` 这类 sh 包装被杀后，里面的命令会变成孤儿留在容器里。
    """
    started = time.time()
    kwargs = {}
    if hasattr(os, 'setsid'):
        kwargs['start_new_session'] = True
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, env=env, cwd=cwd, **kwargs)
    except OSError as e:
        return {'error': '%s: %s' % (type(e).__name__, e), 'seconds': 0.0}
    _CHILDREN.add(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = b'', b''
            return {'error': '超过 %s 秒未结束，已终止（含子进程）' % timeout,
                    'seconds': round(time.time() - started, 3),
                    'stdout': out.decode('utf-8', 'replace')[:limit]}
    finally:
        _CHILDREN.discard(proc)
    return {'rc': proc.returncode,
            'stdout': out.decode('utf-8', 'replace')[:limit],
            'stderr': err.decode('utf-8', 'replace')[:4000],
            'seconds': round(time.time() - started, 3)}


def need_ok(result, what):
    if 'error' in result:
        raise RuntimeError('%s: %s' % (what, result['error']))
    if result['rc'] != 0:
        reason = (result.get('stderr') or result.get('stdout') or '').strip()
        raise RuntimeError('%s 返回 %s: %s' % (what, result['rc'], reason[:300]))
    return result['stdout']


def read_text(path, limit=20000):
    try:
        with open(path, 'rb') as f:
            return f.read(limit).decode('utf-8', 'replace')
    except (OSError, IOError):
        return None


def which(name, path=None):
    for folder in (path or os.environ.get('PATH', '')).split(os.pathsep):
        candidate = os.path.join(folder, name)
        if folder and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# ---------------------------------------------------------------------------
# 各采集项
# ---------------------------------------------------------------------------
MEM_KEYS = ('MemTotal', 'MemAvailable', 'SwapTotal', 'HugePages_Total',
            'HugePages_Free', 'Hugepagesize')


def item_system(cfg):
    uname = platform.uname()
    data = {
        'hostname': socket.gethostname(),
        'uname': {'system': uname[0], 'node': uname[1], 'release': uname[2],
                  'version': uname[3], 'machine': uname[4]},
        'cpu_count': os.cpu_count(),
        'probe_python': {'executable': sys.executable, 'version': sys.version},
    }
    if hasattr(os, 'sched_getaffinity'):
        try:
            data['cpu_affinity_count'] = len(os.sched_getaffinity(0))
        except OSError:
            pass
    libc = platform.libc_ver()
    data['glibc'] = [x for x in libc if x]
    data['os_release'] = read_text('/etc/os-release', 4000)
    meminfo = read_text('/proc/meminfo', 8000) or ''
    data['meminfo'] = dict((k, v.strip()) for k, v in
                           re.findall(r'^(\w+):\s*(.+)$', meminfo, re.M) if k in MEM_KEYS)
    loadavg = read_text('/proc/loadavg', 200)
    data['loadavg'] = loadavg.split()[:3] if loadavg else None
    return data


def item_kernel(cfg):
    """和吞吐有关的内核开关。"""
    paths = {
        'transparent_hugepage': '/sys/kernel/mm/transparent_hugepage/enabled',
        'numa_balancing': '/proc/sys/kernel/numa_balancing',
        'cpu0_governor': '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor',
        'swappiness': '/proc/sys/vm/swappiness',
        'overcommit_memory': '/proc/sys/vm/overcommit_memory',
        'nr_hugepages': '/proc/sys/vm/nr_hugepages',
        'somaxconn': '/proc/sys/net/core/somaxconn',
    }
    data = {}
    for name, path in paths.items():
        value = read_text(path, 500)
        if value is not None:
            data[name] = value.strip()
    if not data:
        raise Skip('读不到 /proc/sys 或 /sys（不是 Linux，或被容器挡住）')
    return data


def item_cgroup(cfg):
    """容器实际拿到的 CPU 和内存配额。"""
    data = {}
    cgroup = read_text('/proc/self/cgroup', 2000)
    if cgroup:
        data['self_cgroup'] = cgroup.strip()
    files = {
        'cpu.max': '/sys/fs/cgroup/cpu.max',
        'cpuset.cpus.effective': '/sys/fs/cgroup/cpuset.cpus.effective',
        'memory.max': '/sys/fs/cgroup/memory.max',
        'pids.max': '/sys/fs/cgroup/pids.max',
        'cpu.cfs_quota_us': '/sys/fs/cgroup/cpu/cpu.cfs_quota_us',
        'cpu.cfs_period_us': '/sys/fs/cgroup/cpu/cpu.cfs_period_us',
        'cpuset.cpus': '/sys/fs/cgroup/cpuset/cpuset.cpus',
        'memory.limit_in_bytes': '/sys/fs/cgroup/memory/memory.limit_in_bytes',
    }
    for name, path in files.items():
        value = read_text(path, 500)
        if value is not None:
            data[name] = value.strip()
    if not data:
        # ulimit 在哪都读得到，不能靠它说明「采到了 cgroup」
        raise Skip('读不到 cgroup 信息（不是 Linux，或 /sys/fs/cgroup 没挂进来）')
    try:
        import resource
        data['ulimit_nofile'] = list(resource.getrlimit(resource.RLIMIT_NOFILE))
        data['ulimit_memlock'] = list(resource.getrlimit(resource.RLIMIT_MEMLOCK))
    except (ImportError, ValueError, OSError):
        pass
    return data


TOOLS = ('python3', 'python', 'pip', 'pip3', 'git', 'npu-smi', 'hccn_tool',
         'lscpu', 'numactl', 'ldd', 'docker')


def item_tools(cfg):
    return dict((name, which(name)) for name in TOOLS)


def item_npu_smi(cfg):
    exe = which('npu-smi')
    if not exe:
        for candidate in ('/usr/local/sbin/npu-smi', '/usr/local/bin/npu-smi',
                          '/usr/bin/npu-smi'):
            if os.path.isfile(candidate):
                exe = candidate
                break
    if not exe:
        raise Skip('没有 npu-smi（不是昇腾机器，或没有映射进容器）')
    timeout = _timeout(cfg, 'npu_smi')
    data = {'path': exe, 'info': need_ok(run([exe, 'info'], timeout), 'npu-smi info')}
    # 下面两项是加分项，失败不影响 info
    mapping = run([exe, 'info', '-m'], timeout)
    if 'error' not in mapping and mapping['rc'] == 0:
        data['mapping'] = mapping['stdout']
    else:
        data['mapping_error'] = mapping.get('error') or mapping.get('stderr', '')[:200]
    return data


def item_lscpu(cfg):
    exe = which('lscpu')
    if not exe:
        raise Skip('没有 lscpu')
    return {'stdout': need_ok(run([exe], _timeout(cfg, 'cmd')), 'lscpu')}


def item_numa(cfg):
    exe = which('numactl')
    if not exe:
        raise Skip('没有 numactl')
    return {'hardware': need_ok(run([exe, '--hardware'], _timeout(cfg, 'cmd')), 'numactl')}


ASCEND_VERSION_GLOBS = (
    '/usr/local/Ascend/ascend-toolkit/latest/version.*',
    '/usr/local/Ascend/ascend-toolkit/*/version.*',
    '/usr/local/Ascend/driver/version.*',
    '/usr/local/Ascend/firmware/version.*',
    '/usr/local/Ascend/nnal/atb/latest/version.*',
    '/usr/local/Ascend/nnae/latest/version.*',
    '/usr/local/Ascend/opp/version.*',
    '/usr/local/Ascend/*/version.info',
    '/etc/ascend_install.info',
    '/etc/ascend_*_install.info',
)


def item_ascend_versions(cfg):
    """CANN / 驱动 / 固件版本文件。

    路径用通配匹配，装在非默认目录或多版本并存时也能采到。
    """
    found = {}
    for pattern in ASCEND_VERSION_GLOBS:
        for path in sorted(glob.glob(pattern))[:40]:
            if path in found or not os.path.isfile(path):
                continue
            content = read_text(path, 5000)
            if content is not None:
                found[path] = content
    if not found:
        raise Skip('没找到昇腾版本文件（不是昇腾机器，或装在非默认目录）')
    home = os.environ.get('ASCEND_HOME_PATH') or os.environ.get('ASCEND_TOOLKIT_HOME')
    return {'files': found, 'ascend_home': home}


def item_ascend_dirs(cfg):
    if not os.path.isdir('/usr/local/Ascend'):
        raise Skip('没有 /usr/local/Ascend')
    entries = []
    try:
        for name in sorted(os.listdir('/usr/local/Ascend'))[:60]:
            full = os.path.join('/usr/local/Ascend', name)
            entries.append({'name': name, 'is_dir': os.path.isdir(full),
                            'link': os.readlink(full) if os.path.islink(full) else None})
    except OSError as e:
        raise RuntimeError('列目录失败: %s' % e)
    return {'entries': entries}


GIT_ROOTS = ('/workspace', '/opt', '/app', '/data', '/home', '/root', '/usr/local/src')
GIT_NAMES = ('vllm', 'vllm-ascend', 'vllm_ascend', 'fastokens', 'mooncake',
             'transformers', 'torch_npu', 'MindSpeed', 'ms-swift')


def item_git(cfg):
    """感兴趣的几个仓库的 commit。按目录名找，不做全盘扫描。"""
    exe = which('git')
    if not exe:
        raise Skip('没有 git')
    candidates = []
    for root in GIT_ROOTS:
        for name in GIT_NAMES:
            path = os.path.join(root, name)
            if os.path.isdir(os.path.join(path, '.git')):
                candidates.append(path)
    if not candidates:
        raise Skip('在 %s 下没找到关心的 git 仓库' % ', '.join(GIT_ROOTS)[:200])
    data = {}
    timeout = _timeout(cfg, 'git')
    # `git status` / `describe --dirty` 会刷新 .git/index，和只读采集的要求冲突。
    # --no-optional-locks + GIT_OPTIONAL_LOCKS=0 让 git 不写索引、不建 index.lock。
    readonly = [exe, '--no-optional-locks']
    genv = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    for path in sorted(set(candidates))[:12]:
        record = {}
        head = run(readonly + ['-C', path, 'rev-parse', 'HEAD'], timeout, env=genv)
        if 'error' not in head and head['rc'] == 0:
            record['head'] = head['stdout'].strip()[:40]
        else:
            record['error'] = head.get('error') or head.get('stderr', '')[:200]
        # 实测（Apple Git 2.54）：`describe --dirty` 即使加了 --no-optional-locks
        # 和 GIT_OPTIONAL_LOCKS=0 也会刷新 .git/index；`status --porcelain` 不会。
        # 所以不让 git 判断脏不脏，自己按 status 的结果拼出 `-dirty` 后缀，
        # 既保持 describe 的惯用格式，又不写入任何文件。
        status = run(readonly + ['-C', path, 'status', '--porcelain'], timeout, env=genv)
        dirty = None
        if 'error' not in status and status['rc'] == 0:
            changed = [l for l in status['stdout'].splitlines() if l.strip()]
            record['dirty_files'] = len(changed)
            dirty = bool(changed)
        # 不加 --tags：带标签的 describe 会和不带标签的采集包产生假差异
        describe = run(readonly + ['-C', path, 'describe', '--always'], timeout, env=genv)
        if 'error' not in describe and describe['rc'] == 0:
            text = describe['stdout'].strip()[:120]
            record['describe'] = (text + '-dirty') if dirty else text
        data[path] = record
    return {'repos': data}


PROCESS_PATTERN = re.compile(
    r'enginecore|vllm|api[._-]?server|uvicorn|ray::|raylet|gcs_server|mooncake|'
    r'torchrun|mindie|llm[._-]?server', re.I)


def item_processes(cfg):
    """所在环境中和推理服务有关的进程：启动参数 + 环境变量。

    采这些是为了说清「这套服务是怎么起来的」。启动参数和环境变量原样记录。
    """
    proc_dir = cfg.get('proc_root') or '/proc'
    if not os.path.isdir(proc_dir):
        raise Skip('没有 %s（不是 Linux）' % proc_dir)
    mine = os.getpid()
    found, scanned, denied = [], 0, 0
    for name in sorted(os.listdir(proc_dir), key=lambda x: int(x) if x.isdigit() else 1 << 30):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == mine:
            continue
        base = os.path.join(proc_dir, name)
        raw = read_text(os.path.join(base, 'cmdline'), 60000)
        if raw is None:
            denied += 1
            continue
        scanned += 1
        argv = [part for part in raw.split('\0') if part]
        if not argv:
            continue
        comm = (read_text(os.path.join(base, 'comm'), 200) or '').strip()
        if not PROCESS_PATTERN.search(comm + ' ' + ' '.join(argv[:16])):
            continue
        record = {'pid': pid, 'comm': comm, 'args': argv[:80]}
        for key, target in (('exe', 'exe'), ('cwd', 'cwd')):
            try:
                record[target] = os.readlink(os.path.join(base, key))
            except OSError:
                record[target] = None
        environ = _read_environ(os.path.join(base, 'environ'))
        record['env'] = dict(environ)
        record['_env'] = environ          # 仅用于挑解释器，不写进结果
        found.append(record)
        if len(found) >= MAX_PROCESSES:
            break
    if not found:
        raise Skip('没有匹配到推理相关进程（扫描 %d 个，%d 个读不到）' % (scanned, denied))
    return {'processes': found, 'scanned': scanned, 'unreadable': denied}


def _read_environ(path):
    raw = read_text(path, 200000)
    if raw is None:
        return {}
    out = {}
    for chunk in raw.split('\0'):
        if '=' in chunk:
            key, value = chunk.split('=', 1)
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Python 画像
# ---------------------------------------------------------------------------
INTROSPECT = (
    'import json,sys\n'
    'd={"executable":sys.executable,"version":sys.version,"prefix":sys.prefix,'
    '"base_prefix":getattr(sys,"base_prefix",sys.prefix),"path":sys.path[:40]}\n'
    'try:\n'
    '    import site; d["sitepackages"]=site.getsitepackages()\n'
    'except Exception as e: d["sitepackages_error"]=str(e)\n'
    'print(json.dumps(d))\n')

# 不依赖 pip 的列包方式：uv 建的无 pip 环境靠 `pip list` 列不出来。
LIST_PACKAGES = (
    'import json,sys\n'
    'out={}\n'
    'try:\n'
    '    from importlib import metadata\n'
    'except ImportError:\n'
    '    metadata=None\n'
    'if metadata is None:\n'
    '    sys.stderr.write("no importlib.metadata (Python < 3.8)")\n'
    '    raise SystemExit(3)\n'
    'if metadata is not None:\n'
    '    for dist in metadata.distributions():\n'
    '        try:\n'
    '            name=dist.metadata["Name"]; ver=dist.version\n'
    '        except Exception:\n'
    '            continue\n'
    '        if name and name not in out: out[name]=ver\n'
    'print(json.dumps({"source":"importlib.metadata","packages":out}))\n')

MODULE_ORIGINS = (
    'import json,importlib.util\n'
    'names=%s\n'
    'out={}\n'
    'for n in names:\n'
    '    try:\n'
    '        spec=importlib.util.find_spec(n)\n'
    '    except Exception as e:\n'
    '        out[n]={"error":str(e)[:200]}; continue\n'
    '    out[n]=None if spec is None else {"origin":spec.origin,'
    '"locations":list(spec.submodule_search_locations or [])[:6]}\n'
    'print(json.dumps(out))\n')

MODULES = ['vllm', 'vllm_ascend', 'torch', 'torch_npu', 'transformers', 'tokenizers',
           'fastokens', 'numpy', 'ray', 'mooncake', 'acl', 'torchair']


def _interpreter_candidates(cfg, processes):
    """挑出要做画像的解释器，按可信度排序，去重。"""
    seen, out = set(), []

    def add(path, source, env, cwd=None):
        if not path or path in seen:
            return
        seen.add(path)
        out.append({'python': path, 'source': source, 'env': env, 'cwd': cwd})

    if cfg.get('python'):
        add(cfg['python'], 'explicit_python', dict(os.environ))
    for proc in processes or []:
        env = proc.get('_env') or {}
        exe = proc.get('exe') or ''
        if 'python' not in os.path.basename(exe).lower() and '/pypy' not in exe.lower():
            continue
        venv = env.get('VIRTUAL_ENV')
        cwd = proc.get('cwd') if proc.get('cwd') and os.path.isdir(proc.get('cwd') or '') else None
        if venv:
            candidate = os.path.join(venv, 'bin', 'python')
            if os.path.isfile(candidate):
                add(candidate, 'process_virtual_env', env, cwd)
                continue
        conda = env.get('CONDA_PREFIX')
        if conda:
            candidate = os.path.join(conda, 'bin', 'python')
            if os.path.isfile(candidate):
                add(candidate, 'process_conda_prefix', env, cwd)
                continue
        # argv[0] 要优先于 /proc/PID/exe：venv 里的 bin/python 只是个指向真
        # 解释器的链接，exe 读出来是被指向的那个，于是 pyvenv.cfg 失效，列包
        # 列的是系统环境而不是 venv。直接用 uv/venv 的 python 启动、没有
        # export VIRTUAL_ENV 时就会这样。
        argv0 = (proc.get('args') or [None])[0]
        if (argv0 and os.path.isabs(argv0) and 'python' in os.path.basename(argv0).lower()
                and os.path.isfile(argv0) and os.access(argv0, os.X_OK)):
            add(argv0, 'process_argv0', env, cwd)
            continue
        if os.path.isfile(exe):
            add(exe, 'process_exe', env, cwd)
    if not out:
        add(sys.executable, 'probe_python', dict(os.environ))
    return out[:MAX_PROFILES]


def _profile_one(cfg, candidate):
    env = dict(candidate.get('env') or os.environ)
    env.update(cfg.get('extra_env') or {})
    python = candidate['python']
    cwd = candidate.get('cwd')
    # environment_relevant 装的是进程实际生效的完整环境变量
    profile = {'python_executable': python, 'chosen_by': candidate['source'],
               'environment_relevant': dict(env)}
    introspect = run([python, '-c', INTROSPECT], _timeout(cfg, 'python'), env=env, cwd=cwd)
    if 'error' not in introspect and introspect['rc'] == 0:
        try:
            profile['interpreter'] = json.loads(introspect['stdout'])
        except ValueError:
            profile['interpreter_error'] = '输出不是 JSON: ' + introspect['stdout'][:300]
    else:
        profile['interpreter_error'] = (introspect.get('error')
                                        or introspect.get('stderr', '')[:300])
    packages, source, errors = {}, None, []
    attempts = (
        ('importlib.metadata', [python, '-c', LIST_PACKAGES], _parse_metadata),
        ('pip list', [python, '-m', 'pip', 'list', '--format=json'], _parse_pip_list),
        ('pip freeze', [python, '-m', 'pip', 'freeze', '--all'], _parse_freeze),
    )
    for name, args, parser in attempts:
        result = run(args, _timeout(cfg, 'packages'), env=env, cwd=cwd)
        if 'error' in result or result['rc'] != 0:
            errors.append('%s: %s' % (name, (result.get('error')
                                             or result.get('stderr', '')).strip()[:200]))
            continue
        try:
            parsed = parser(result['stdout'])
        except ValueError as e:
            errors.append('%s: 输出无法解析: %s' % (name, e))
            continue
        # 命令跑通且能解析就采纳，哪怕是空的 —— 空环境确实可能一个包都没有，
        # 这和「列不出来」不是一回事。
        packages, source = parsed, name
        if not parsed:
            errors.append('%s: 跑通了但没有列出任何包（环境可能确实是空的）' % name)
        break
    profile['packages'] = packages
    profile['packages_source'] = source
    if errors:
        profile['packages_errors'] = errors
    origins = run([python, '-c', MODULE_ORIGINS % json.dumps(MODULES)],
                  _timeout(cfg, 'python'), env=env, cwd=cwd)
    if 'error' not in origins and origins['rc'] == 0:
        try:
            profile['module_locations'] = json.loads(origins['stdout'])
        except ValueError:
            profile['module_locations_error'] = origins['stdout'][:300]
    else:
        profile['module_locations_error'] = (origins.get('error')
                                             or origins.get('stderr', '')[:300])
    # 「采集成功」= 解释器自省成功，且列包命令跑通（包数可以是 0）
    profile['ok'] = bool(profile.get('interpreter')) and source is not None
    return profile


def _parse_metadata(text):
    data = json.loads(text)
    return dict(data.get('packages') or {})


def _parse_pip_list(text):
    return dict((item['name'], item['version']) for item in json.loads(text)
                if isinstance(item, dict) and item.get('name'))


def _parse_freeze(text):
    out = {}
    for line in str(text).splitlines():
        match = re.match(r'^([A-Za-z0-9_.\-]+)==([^\s;]+)', line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


def item_python_profiles(cfg, processes=None):
    candidates = _interpreter_candidates(cfg, processes)
    profiles = [_profile_one(cfg, candidate) for candidate in candidates]
    if not profiles:
        raise Skip('没有可用的 Python 解释器')
    return {'profiles': profiles}


# ---------------------------------------------------------------------------
# 驱动
# ---------------------------------------------------------------------------
ITEMS = (
    ('system', item_system),
    ('kernel', item_kernel),
    ('cgroup', item_cgroup),
    ('tools', item_tools),
    ('npu_smi', item_npu_smi),
    ('lscpu', item_lscpu),
    ('numa', item_numa),
    ('ascend_versions', item_ascend_versions),
    ('ascend_dirs', item_ascend_dirs),
    ('git', item_git),
    ('processes', item_processes),
    ('python_profiles', item_python_profiles),
)


def collect(cfg, emit):
    """依次采集每一项，每项结束立刻 emit 一行。

    emit 失败不应影响采集，所以调用方自己保证 emit 不抛异常。
    """
    processes = None
    for name, func in ITEMS:
        started = time.time()
        try:
            if name == 'python_profiles':
                data = func(cfg, processes)
            else:
                data = func(cfg)
            if name == 'processes':
                processes = data.get('processes')
                data = {'processes': [dict((k, v) for k, v in p.items() if k != '_env')
                                      for p in processes],
                        'scanned': data.get('scanned'), 'unreadable': data.get('unreadable')}
            emit({'item': name, 'status': 'ok', 'seconds': round(time.time() - started, 3),
                  'data': data})
        except Skip as e:
            emit({'item': name, 'status': 'skip', 'seconds': round(time.time() - started, 3),
                  'reason': str(e)})
        except Exception as e:      # 任何一项的任何异常都只影响这一项
            emit({'item': name, 'status': 'error', 'seconds': round(time.time() - started, 3),
                  'reason': '%s: %s' % (type(e).__name__, str(e)[:500])})


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = json.loads(argv[0]) if argv else {}

    def emit(record):
        try:
            sys.stdout.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
            sys.stdout.flush()
        except Exception:
            pass

    notes = install_guards(cfg.get('deadline'))
    # pid/pgid 要第一行就报出去：采集端超时后靠它在容器里把整组清掉
    emit({'item': '_begin', 'status': 'ok',
          'data': {'schema': SCHEMA, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                   'probe_python': sys.version.split()[0],
                   'pid': os.getpid(),
                   'pgid': _own_pgid(),
                   'deadline': cfg.get('deadline'),
                   'guard_notes': notes}})
    try:
        collect(cfg, emit)
    finally:
        kill_all_children()
        if hasattr(signal, 'alarm'):
            try:
                signal.alarm(0)
            except (OSError, ValueError):
                pass
    emit({'item': '_end', 'status': 'ok', 'data': {'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S')}})
    return 0


def _own_pgid():
    try:
        return os.getpgid(0)
    except (OSError, AttributeError):
        return None


if __name__ == '__main__':
    sys.exit(main())
