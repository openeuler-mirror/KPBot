"""Turn one collected container into comparable, flat (category, name) -> value items.

Every extractor is defensive: a missing or malformed piece yields the MISSING
marker instead of raising, so one absent field never aborts the comparison.
"""
import json
import os
import re

MISSING = '未采集'
NOT_REPORTED = '未报告'
NOT_SET = '未设置'
FAILED = '采集失败'
MISSING_MARKERS = (MISSING, NOT_REPORTED, NOT_SET, FAILED, '')

CATEGORY_BASE = '基础信息'
CATEGORY_CORE = '核心包'
CATEGORY_OTHER = '其他包'
CATEGORY_ENV = '进程环境变量'
CATEGORY_DOCKER_ENV = 'Docker配置环境变量'
CATEGORY_ARGS = '启动参数'

CATEGORY_ORDER = [CATEGORY_BASE, CATEGORY_ARGS, CATEGORY_CORE,
                  CATEGORY_OTHER, CATEGORY_ENV, CATEGORY_DOCKER_ENV]

CORE_PACKAGES = [
    'vllm', 'vllm-ascend', 'torch', 'torch-npu', 'torchvision', 'transformers',
    'tokenizers', 'fastokens', 'numpy', 'mooncake', 'mooncake-transfer-engine',
    'safetensors', 'accelerate', 'triton', 'ray', 'uvicorn', 'fastapi',
    'pydantic', 'msgpack', 'pyzmq', 'grpcio', 'sentencepiece',
    'huggingface-hub', 'flash-attn', 'vllm-flash-attn',
]

# Items whose difference plausibly changes throughput or latency. Used to sort
# and highlight the report; it never hides anything.
PERF_PATTERNS = [
    re.compile(r'^(vllm|vllm-ascend|torch|torch-npu|transformers|tokenizers|fastokens|mooncake)'),
    re.compile(r'^(HCCL_|HCCN_|ASCEND_|ACL_|ATB_|LCCL_|MC2|TASK_QUEUE_ENABLE|NPU_)'),
    re.compile(r'^(VLLM_|OMP_|MKL_|OPENBLAS_|KMP_|GOMP_|CPU_AFFINITY|MALLOC_|'
               r'TOKENIZERS_PARALLELISM|LD_PRELOAD|COMBINED_ENABLE|INF_NAN_MODE)'),
    re.compile(r'tensor.parallel|data.parallel|pipeline.parallel|expert.parallel|'
               r'max.num.seqs|max.num.batched|max.model.len|block.size|'
               r'memory.utilization|chunked.prefill|prefix.caching|quantization|'
               r'kv.transfer|kv.cache|num.scheduler.steps|cuda.graph|graph.capture|'
               r'speculative|swap.space|dtype|enforce.eager|compilation', re.I),
    re.compile(r'^(cgroup: (?:cpu\.max|cpuset\.cpus|cpuset\.cpus\.effective|'
               r'memory\.max|cpu\.cfs_quota_us|cpu\.cfs_period_us|memory\.limit_in_bytes)|'
               r'内核参数: (?:transparent_hugepage|numa_balancing|cpu0_governor|nr_hugepages)|'
               r'CPU核数限制|CPU内存节点|进程扫描)', re.U),
    re.compile(r'^(CPU数量|可用CPU数量|CPU集合|CPU配额|容器内存限制|共享内存大小|'
               r'CANN版本|固件版本|驱动版本|NPU芯片数量|IPC模式|网络模式|'
               r'Docker Runtime|NUMA|透明大页)', re.U),
]


# 整类取值都来自同一个采集项的类别：这个采集项失败时，该类别在本环境里
# 既不能判相同也不能判「单边存在」。只列由单个采集项完整提供数据的类别。
# 一个类别可以有多个来源，取并集：进程环境变量要靠 processes 找到进程，
# 再靠 python_profiles 读出它的环境变量，两项任一失败都采不到。
CATEGORY_SOURCE_ITEMS = {
    'processes': (CATEGORY_ARGS, CATEGORY_ENV),
    'python_profiles': (CATEGORY_ENV,),
}


def unreliable_categories(payload, packages_ok):
    """本环境里因采集失败而无法比较的类别。

    跳过（skip）表示这项在当前环境不适用，不是失败，不计入。
    """
    status = payload.get('collection_status') or {}
    broken = set(status.get('failed') or {})
    broken.update(status.get('never_started') or [])
    out = set()
    for item, categories in CATEGORY_SOURCE_ITEMS.items():
        if item in broken:
            out.update(categories)
    if not packages_ok:
        out.update((CATEGORY_CORE, CATEGORY_OTHER))
    return out


def is_perf_relevant(category, name):
    if category in (CATEGORY_CORE, CATEGORY_ARGS):
        return True
    return any(p.search(str(name)) for p in PERF_PATTERNS)


def is_missing(value):
    return str(value).strip() in MISSING_MARKERS


def as_text(value):
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, tuple, dict)):
        if not value:
            return MISSING
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value).strip()
    return text if text else MISSING


def first_line(value):
    """First non-empty line, or MISSING. Never raises on an empty or
    whitespace-only string."""
    lines = [l.strip() for l in str(value or '').splitlines() if l.strip()]
    return lines[0] if lines else MISSING


# ---------------------------------------------------------------------------
# Version files
# ---------------------------------------------------------------------------
_VERSION_KEYS = re.compile(
    r'^\s*(?:Version|version|FullVersion|full_version|innerversion|'
    r'runtime_running_version|compiler_running_version|Innerversion|package_version)'
    r'\s*[:=]\s*(.+?)\s*$', re.M)


def parse_version_file(content):
    """Pull a version out of an Ascend version.info/version.cfg body.

    Every key form is accepted and bracketed value lists are unwrapped, so a
    machine whose version.cfg only holds `runtime_running_version=[...]` still
    yields a version.
    """
    text = str(content or '')
    for match in _VERSION_KEYS.finditer(text):
        value = match.group(1).strip()
        # runtime_running_version=[7.0:8.0.RC1] -> 8.0.RC1
        bracket = re.fullmatch(r'\[(.+)\]', value)
        if bracket:
            parts = [p for p in re.split(r'[:,]', bracket.group(1)) if p.strip()]
            value = parts[-1].strip() if parts else value
        if value:
            return value[:160]
    return None


def cann_version(payload):
    """Best-effort CANN toolkit version, with the file it came from."""
    files = payload.get('cann_driver_version_files') or {}
    if not files:
        return MISSING, MISSING

    def rank(path):
        p = path.lower()
        return (0 if 'ascend-toolkit' in p else 1 if 'cann' in p else 2,
                0 if p.endswith('version.info') else 1,
                path)

    unparsed = None
    for path in sorted(files, key=rank):
        version = parse_version_file(files[path])
        if version:
            return version, path
        if unparsed is None and str(files[path] or '').strip():
            unparsed = path
    if unparsed:
        return '已发现版本文件但未能解析', unparsed
    return MISSING, MISSING


def driver_version(payload):
    files = payload.get('cann_driver_version_files') or {}
    for path in sorted(files):
        if 'driver' in path.lower():
            version = parse_version_file(files[path])
            if version:
                return version
    return MISSING


# ---------------------------------------------------------------------------
# npu-smi
# ---------------------------------------------------------------------------
def parse_npu_smi(text):
    """Extract chip count, firmware and driver version from `npu-smi info`."""
    out = {}
    body = str(text or '')
    if not body.strip():
        return out
    ver = re.search(r'npu-smi\s+([0-9][\w.\-]*)', body)
    if ver:
        out['npu-smi版本'] = ver.group(1)
    fw = re.search(r'Version\s*:\s*([0-9][\w.\-]*)', body)
    if fw:
        out['驱动版本'] = fw.group(1)
    chips = set()
    for line in body.splitlines():
        # | 0     910B3   | OK   | 93.9  40   0  / 0  |
        m = re.match(r'^\s*\|\s*(\d+)\s+(\S+)\s*\|', line)
        if m:
            chips.add((m.group(1), m.group(2)))
    if chips:
        out['NPU芯片数量'] = str(len(chips))
        out['NPU型号'] = ', '.join(sorted({c[1] for c in chips}))
    return out


def parse_lscpu(text):
    out = {}
    wanted = {
        'Architecture': '架构', 'CPU(s)': 'CPU数量', 'Model name': 'CPU型号',
        'NUMA node(s)': 'NUMA节点数', 'Thread(s) per core': '每核线程数',
        'Core(s) per socket': '每路核心数', 'Socket(s)': 'CPU路数',
        'CPU max MHz': 'CPU最大频率', 'L3 cache': 'L3缓存',
    }
    for line in str(text or '').splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        label = wanted.get(key.strip())
        if label:
            out[label] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Serving process and its arguments
# ---------------------------------------------------------------------------
SHELLS = ('sh', 'bash', 'dash', 'ash', 'zsh', 'ksh', 'busybox', 'env', 'tini', 'docker-init')


def _is_shell_wrapper(p):
    """`sh -c "... vllm serve ..."` 这种外层 shell 也会命中关键词，但它的参数
    是一整串命令，不是引擎的真实 argv，排序上要让它排在真进程后面。"""
    argv = p.get('args') or []
    argv0 = str(argv[0]) if argv else ''
    comm = str(p.get('comm') or '')
    return (os.path.basename(argv0) in SHELLS or comm in SHELLS)


def pick_serving_process(payload):
    """The process whose arguments describe how the engine was started."""
    procs = payload.get('processes') or []

    def rank(p):
        text = str(p.get('comm', '')) + ' ' + ' '.join(str(a) for a in (p.get('args') or []))
        if re.search(r'enginecore', text, re.I):
            base = 0
        elif re.search(r'api[._-]?server|vllm[ _-]?serve|vllm\.entrypoints', text, re.I):
            base = 1
        elif re.search(r'vllm', text, re.I):
            base = 2
        else:
            return 3
        return base + (10 if _is_shell_wrapper(p) else 0)

    ranked = sorted(procs, key=lambda p: (rank(p), p.get('pid') or 0))
    for p in ranked:
        if rank(p) != 3:
            return p
    return ranked[0] if ranked else None


def parse_launch_args(args):
    """argv list -> {flag: value}. `--k v`, `--k=v` and bare flags all work.

    Launch arguments are compared item by item, so a P/D pair differing only
    in --tensor-parallel-size or --max-num-seqs shows up as a difference.
    """
    out = {}
    if isinstance(args, str):
        args = args.split()
    items = [str(a) for a in (args or [])]
    i = 0
    seen_serve = False
    while i < len(items):
        token = items[i]
        if token.startswith('--') and len(token) > 2:
            name = token[2:]
            if '=' in name:
                name, value = name.split('=', 1)
            elif i + 1 < len(items) and not items[i + 1].startswith('-'):
                value = items[i + 1]
                i += 1
            else:
                value = 'true'
            key = '--' + name
            out[key] = (out[key] + ' | ' + value) if key in out else value
        elif token in ('serve', 'bench', 'chat', 'complete'):
            seen_serve = True
            out['子命令'] = token
        elif seen_serve and '模型' not in out and not token.startswith('-'):
            out['模型'] = token
        i += 1
    return out


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------
def normalize_package(name):
    return re.sub(r'[-_.]+', '-', str(name)).lower()


def package_map(profile):
    out = {}
    for item in profile.get('pip_list') or []:
        if isinstance(item, dict) and item.get('name') and item.get('version') is not None:
            out[normalize_package(item['name'])] = str(item['version'])
    if not out:
        # Fall back to pip freeze when `pip list --format=json` failed but
        # freeze succeeded.
        for line in str(profile.get('pip_freeze') or '').splitlines():
            m = re.match(r'^([A-Za-z0-9_.\-]+)==([^\s;]+)', line.strip())
            if m:
                out[normalize_package(m.group(1))] = m.group(2)
    return out


def primary_profile(payload):
    profiles = payload.get('profiles') or []
    for profile in profiles:
        if profile.get('collection_ok'):
            return profile
    return profiles[0] if profiles else {}


# ---------------------------------------------------------------------------
# Entry assembly
# ---------------------------------------------------------------------------
def build_entry(label, bundle, container):
    """One comparable environment: identity, status and its items."""
    meta = bundle['meta']
    payload = container['inventory'] or {}
    docker = container['docker'] or {}
    profile = primary_profile(payload)
    packages = package_map(profile) if payload else {}
    # 「列包跑通了但一个包都没有」和「列不出来」要分开：采集端会记下
    # packages_source，有它就说明列包命令确实跑通了（空环境也算采到）。
    if profile.get('packages_source'):
        packages_ok = True
    else:
        packages_ok = bool(packages)
    status = payload.get('collection_status') or {}
    error = container.get('error')
    if not error and status and not status.get('completed'):
        error = status.get('probe_error') or '探针没有采到任何项目'
    entry = {
        'label': label,
        'id': label,
        'source': bundle['source'],
        'role': meta.get('role') or MISSING,
        'node': meta.get('node') or MISSING,
        'container': container['container'],
        'collected_at': meta.get('timestamp_utc') or MISSING,
        'error': error,
        'profile_ok': bool(profile.get('collection_ok')),
        'packages_ok': packages_ok,
        'profile_source': profile.get('environment_source') or MISSING,
        'unreliable_categories': unreliable_categories(payload, packages_ok),
        'items': {},
    }
    if error:
        return entry
    items = entry['items']

    def put(category, name, value):
        items[(category, str(name))] = as_text(value)

    # --- base ---------------------------------------------------------
    interp = profile.get('interpreter') or {}
    uname = payload.get('uname') or {}
    libc = payload.get('glibc') or []
    cann, cann_file = cann_version(payload)
    put(CATEGORY_BASE, 'Python版本', first_line(interp.get('version')))
    put(CATEGORY_BASE, 'Python解释器', profile.get('python_executable_used'))
    put(CATEGORY_BASE, 'Python解释器选择依据', profile.get('chosen_by'))
    put(CATEGORY_BASE, 'Python画像来源', profile.get('environment_source'))
    put(CATEGORY_BASE, 'Python画像状态', '成功' if profile.get('collection_ok') else '失败')
    put(CATEGORY_BASE, 'sys.prefix', interp.get('prefix'))
    put(CATEGORY_BASE, 'sys.base_prefix', interp.get('base_prefix'))
    put(CATEGORY_BASE, 'site-packages', interp.get('sitepackages'))
    put(CATEGORY_BASE, '容器操作系统', _os_name(payload))
    put(CATEGORY_BASE, '内核版本', uname.get('release'))
    put(CATEGORY_BASE, '架构', uname.get('machine'))
    put(CATEGORY_BASE, 'glibc', ' '.join(str(x) for x in libc if x) if isinstance(libc, (list, tuple)) else libc)
    put(CATEGORY_BASE, '可用CPU数量', payload.get('cpu_count'))
    put(CATEGORY_BASE, 'CANN版本', cann)
    put(CATEGORY_BASE, 'CANN版本文件', cann_file)
    put(CATEGORY_BASE, '驱动版本(版本文件)', driver_version(payload))
    for name, value in sorted((payload.get('cann_driver_version_files') or {}).items()):
        put(CATEGORY_BASE, '版本文件: ' + name, parse_version_file(value) or first_line(value))
    for path, rec in sorted((payload.get('git') or {}).items()):
        # `git rev-parse --show-toplevel HEAD` prints the path then the commit.
        lines = [l for l in str((rec or {}).get('rev') or '').splitlines() if l.strip()]
        put(CATEGORY_BASE, 'git HEAD: ' + path,
            (lines[-1][:12] if lines else MISSING) + ' / ' + as_text((rec or {}).get('describe')))
    for name, loc in sorted((profile.get('module_locations') or {}).items()):
        if isinstance(loc, dict):
            put(CATEGORY_BASE, '模块位置: ' + name, loc.get('origin') or MISSING)
        else:
            put(CATEGORY_BASE, '模块位置: ' + name, MISSING)
    # docker inspect
    state = docker.get('state') or {}
    limits = docker.get('limits') or {}
    put(CATEGORY_BASE, '镜像引用', docker.get('image_ref'))
    put(CATEGORY_BASE, '镜像ID', docker.get('image_id'))
    put(CATEGORY_BASE, '容器状态', state.get('Status'))
    put(CATEGORY_BASE, '容器用户', docker.get('user'))
    put(CATEGORY_BASE, '容器工作目录', docker.get('working_dir'))
    put(CATEGORY_BASE, '容器Entrypoint', docker.get('entrypoint'))
    put(CATEGORY_BASE, '容器Cmd', docker.get('cmd'))
    put(CATEGORY_BASE, '网络模式', limits.get('NetworkMode'))
    put(CATEGORY_BASE, 'IPC模式', limits.get('IpcMode'))
    put(CATEGORY_BASE, 'PID模式', limits.get('PidMode'))
    put(CATEGORY_BASE, '共享内存大小(字节)', limits.get('ShmSize'))
    put(CATEGORY_BASE, '容器内存限制(字节)', limits.get('Memory'))
    put(CATEGORY_BASE, 'CPU配额', _quota(limits.get('CpuQuota')))
    put(CATEGORY_BASE, 'CPU周期', limits.get('CpuPeriod'))
    # docker run --cpus 设的是 NanoCpus，和 CpuQuota 是两个口径，都要比
    put(CATEGORY_BASE, 'CPU核数限制(--cpus)', _nano_cpus(limits.get('NanoCpus')))
    put(CATEGORY_BASE, 'CPU内存节点(CpusetMems)', limits.get('CpusetMems'))
    put(CATEGORY_BASE, 'CPU集合', limits.get('CpusetCpus'))
    put(CATEGORY_BASE, 'Docker Runtime', limits.get('Runtime'))
    put(CATEGORY_BASE, '特权模式', limits.get('Privileged'))
    put(CATEGORY_BASE, '容器挂载', docker.get('mounts'))
    put(CATEGORY_BASE, '容器设备数量', len(docker.get('devices') or []) or MISSING)
    put(CATEGORY_BASE, '容器设备', docker.get('devices'))

    # --- 内核与 cgroup 项（采不到时自动跳过）--------------------------
    for name, value in sorted((payload.get('kernel') or {}).items()):
        put(CATEGORY_BASE, '内核参数: ' + name, value)
    for name, value in sorted((payload.get('cgroup') or {}).items()):
        put(CATEGORY_BASE, 'cgroup: ' + name, value)
    for name, value in sorted((payload.get('meminfo') or {}).items()):
        put(CATEGORY_BASE, '内存: ' + name, value)
    for name, value in sorted((payload.get('tools') or {}).items()):
        put(CATEGORY_BASE, '命令位置: ' + name, value or '未找到')
    if payload.get('cpu_affinity_count') is not None:
        put(CATEGORY_BASE, '可用CPU数量(亲和性)', payload.get('cpu_affinity_count'))
    if payload.get('ascend_home'):
        put(CATEGORY_BASE, 'ASCEND_HOME_PATH', payload.get('ascend_home'))
    if payload.get('numa'):
        put(CATEGORY_BASE, 'numactl --hardware', first_line((payload['numa'] or {}).get('hardware')))
    if status:
        put(CATEGORY_BASE, '采集项成功数', len(status.get('completed') or []))
        put(CATEGORY_BASE, '采集项失败',
            ', '.join(sorted(status.get('failed') or {})) or '无')
        put(CATEGORY_BASE, '采集项跳过',
            ', '.join(sorted(status.get('skipped') or {})) or '无')
        put(CATEGORY_BASE, '探针问题', status.get('probe_error') or '无')
    scan = payload.get('process_scan') or {}
    if scan.get('scanned') is not None:
        put(CATEGORY_BASE, '进程扫描(可读/不可读)',
            '%s / %s' % (scan.get('scanned'), scan.get('unreadable')))
    if meta.get('interrupted'):
        put(CATEGORY_BASE, '采集是否被中断', '是：' + str(meta.get('interrupt_reason') or ''))
    if meta.get('reconstructed'):
        put(CATEGORY_BASE, '采集包完整性', '缺少 collection_meta.json，已按目录重建')
    if profile.get('packages_source'):
        put(CATEGORY_BASE, '包列表来源', profile.get('packages_source'))

    # --- container-side npu-smi / lscpu -------------------------------
    for name, value in parse_npu_smi((payload.get('npu_smi') or {}).get('stdout')).items():
        put(CATEGORY_BASE, name, value)
    for name, value in parse_lscpu((payload.get('lscpu') or {}).get('stdout')).items():
        put(CATEGORY_BASE, 'lscpu ' + name, value)

    # --- launch args --------------------------------------------------
    proc = pick_serving_process(payload)
    if proc:
        put(CATEGORY_ARGS, '进程命令(完整)', ' '.join(str(a) for a in (proc.get('args') or [])))
        put(CATEGORY_ARGS, '进程名', proc.get('comm'))
        put(CATEGORY_ARGS, '进程可执行文件', proc.get('exe'))
        for name, value in sorted(parse_launch_args(proc.get('args')).items()):
            put(CATEGORY_ARGS, name, value)
    else:
        put(CATEGORY_ARGS, '进程命令(完整)', MISSING)

    # --- packages -----------------------------------------------------
    for name in CORE_PACKAGES:
        if entry['packages_ok']:
            put(CATEGORY_CORE, name, packages.get(name, NOT_REPORTED))
        else:
            put(CATEGORY_CORE, name, FAILED)
    for name, version in packages.items():
        if name not in set(CORE_PACKAGES):
            put(CATEGORY_OTHER, name, version)

    # --- environment --------------------------------------------------
    for name, value in (profile.get('environment_relevant') or {}).items():
        put(CATEGORY_ENV, name, value)
    for name, value in (docker.get('config_env_relevant') or {}).items():
        put(CATEGORY_DOCKER_ENV, name, value)
    return entry


def _quota(value):
    """CpuQuota 的 0 和 -1 都表示没限制，直接显示 0 容易误读。"""
    if value in (None, ''):
        return MISSING
    try:
        number = int(value)
    except (TypeError, ValueError):
        return value
    return '未限制(%s)' % number if number <= 0 else str(number)


def _nano_cpus(value):
    if value in (None, '', 0):
        return '未限制' if value == 0 else MISSING
    try:
        return '%.3g 核 (%s)' % (int(value) / 1e9, value)
    except (TypeError, ValueError):
        return str(value)


def _os_name(payload):
    raw = payload.get('os_release') or ''
    for key in ('PRETTY_NAME', 'NAME'):
        m = re.search(r'^' + key + r'="?(.*?)"?$', raw, re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return MISSING
