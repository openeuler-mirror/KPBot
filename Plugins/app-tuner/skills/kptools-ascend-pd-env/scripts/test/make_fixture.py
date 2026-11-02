#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造假的采集包，给测试用。不连网、不依赖 docker。

    make_fixture.py <目录> <容器名>[,<容器名>...]

造出来的包是最小但完整的形态：collection_meta.json + 每个容器一个
inventory.json / docker_inspect_safe.json。

第一个容器是基准，后面的容器带一组固定的差异，用来验证对比端认不认得出来：

  * CANN 版本           8.1.RC1 -> 8.2.RC1        （同时带出「版本文件: …」一项）
  * torch-npu 包版本    2.5.1.post1 -> 2.5.1.post3
  * 容器内存限制        1 GiB -> 2 GiB
  * HCCL_EXEC_TIMEOUT   只有后面的容器有，用来造出「仅 X 有」

按名字还能造出几种特殊情形：

  * `-alt`       一律按带差异处理，这样单独一个容器的采集包也能当作「另一台机器」来比
  * `-nopkg`     不带包列表，对应「包列表未采到」
  * `-noproc`    processes 这一项采集失败，启动参数和进程环境变量都采不到
  * `-noprofile` python_profiles 这一项采集失败，进程环境变量和包列表都采不到
  * `-skipproc`  processes 这一项被跳过（不是失败），对应的项按真正的单边存在处理
  * `-failed`    只写 ERROR.txt、不写 inventory.json，对应整份采集失败
"""
import json
import pathlib
import sys

BASE_CANN = 'Version=8.1.RC1\n'
VARIANT_CANN = 'Version=8.2.RC1\n'
TOOLKIT_VERSION_FILE = '/usr/local/Ascend/ascend-toolkit/latest/version.info'
BASE_MEMORY = 1073741824
VARIANT_MEMORY = 2147483648


PROBE_ITEMS = ('system', 'kernel', 'cgroup', 'tools', 'npu_smi', 'lscpu', 'numa',
               'ascend_versions', 'ascend_dirs', 'git', 'processes', 'python_profiles')


def collection_status(failed=None, skipped=None):
    broken = set(failed or {}) | set(skipped or {})
    return {
        'probe_error': None, 'attempts': [], 'broken_lines': [],
        'completed': [i for i in PROBE_ITEMS if i not in broken],
        'skipped': dict(skipped or {}),
        'failed': dict(failed or {}),
        'never_started': [], 'finished': True,
    }


def inventory(variant=False, with_packages=True, processes_state='ok',
              profiles_state='ok'):
    interp = {'executable': '/usr/bin/python3', 'version': '3.11.2 (main) [GCC]',
              'prefix': '/usr', 'base_prefix': '/usr', 'path': ['/usr/lib/python3.11'],
              'sitepackages': ['/usr/lib/python3/dist-packages']}
    env = {'HCCL_BUFFSIZE': '200', 'ASCEND_RT_VISIBLE_DEVICES': '0'}
    if variant:
        env['HCCL_EXEC_TIMEOUT'] = '1800'
    if processes_state == 'failed':
        # 读不到进程就同样读不到进程的环境变量
        env = {}
    packages = [{'name': 'vllm', 'version': '0.9.1'},
                {'name': 'torch', 'version': '2.5.1'},
                {'name': 'torch_npu',
                 'version': '2.5.1.post3' if variant else '2.5.1.post1'},
                {'name': 'example-extra', 'version': '1.2.3'}]
    processes = [{'pid': 7, 'comm': 'VLLM::EngineCore', 'exe': '/usr/bin/python3',
                  'cwd': '/workspace',
                  'args': ['python3', '-m', 'vllm.entrypoints.openai.api_server',
                           '--max-num-seqs', '256'],
                  'env': {'HCCL_BUFFSIZE': '200'}}]
    if processes_state == 'failed':
        processes, status = [], collection_status(failed={'processes': '超过 15 秒未结束'})
    elif processes_state == 'skipped':
        processes, status = [], collection_status(skipped={'processes': '没有 /proc（不是 Linux）'})
    elif profiles_state == 'failed':
        status = collection_status(failed={'python_profiles': '超过 20 秒未结束'})
    else:
        status = collection_status()
    profile = {
        'pid': 7, 'exe': '/usr/bin/python3', 'python_executable_used': '/usr/bin/python3',
        'chosen_by': 'process_argv', 'environment_source': 'runtime_process',
        'interpreter': interp, 'collection_ok': True,
        'pip_list': packages if with_packages else [],
        'pip_freeze': None,
        'environment_relevant': env,
        'module_locations': {'vllm': {'origin': '/opt/vllm/__init__.py', 'locations': []}},
    }
    return {
        'probe_python': {'executable': '/usr/bin/python3', 'version': '3.11.2'},
        'os_release': 'PRETTY_NAME="openEuler 24.03 LTS"\nNAME="openEuler"\n',
        'uname': {'system': 'Linux', 'release': '5.10.0', 'machine': 'aarch64'},
        'glibc': ['glibc', '2.38'], 'cpu_count': 192,
        'npu_smi': {'returncode': 0, 'stdout': ''},
        'lscpu': {'returncode': 0, 'stdout': 'Architecture:  aarch64\nCPU(s):  192\n'},
        'cann_driver_version_files': {
            TOOLKIT_VERSION_FILE: VARIANT_CANN if variant else BASE_CANN},
        'git': {},
        'processes': processes,
        'collection_status': status,
        'profiles': [] if profiles_state == 'failed' else [profile],
        'selection': {'explicit_python': None, 'explicit_env_names': []},
    }


def docker_inspect(variant=False):
    return {
        'image_ref': 'ascend/vllm:latest', 'image_id': 'sha256:aaa',
        'state': {'Status': 'running', 'Running': True},
        'limits': {'NetworkMode': 'host', 'IpcMode': 'host', 'ShmSize': 68719476736,
                   'CpusetCpus': '0-95', 'Runtime': 'runc',
                   'Memory': VARIANT_MEMORY if variant else BASE_MEMORY},
        'config_env_relevant': {'VLLM_USE_V1': '1'},
        'mounts': [], 'devices': [],
    }


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    root = pathlib.Path(sys.argv[1])
    containers = [c for c in sys.argv[2].split(',') if c]
    root.mkdir(parents=True, exist_ok=True)
    (root / 'collection_meta.json').write_text(json.dumps({
        'schema': 1, 'collector_version': 'fixture', 'role': 'P', 'node': root.name,
        'timestamp_utc': '20261024T000000Z',
        'timestamp_local': '2026-10-24 08:00:00 +0800',
        'targets': containers,
        'folders': dict((c, c) for c in containers), 'interrupted': False,
    }, ensure_ascii=False), encoding='utf-8')
    for index, container in enumerate(containers):
        variant = index > 0 or container.endswith('-alt')
        folder = root / container
        folder.mkdir(parents=True, exist_ok=True)
        if container.endswith('-failed'):
            (folder / 'ERROR.txt').write_text(
                '容器里没有可用的 Python\n', encoding='utf-8')
        else:
            processes_state, profiles_state = 'ok', 'ok'
            if container.endswith('-noproc'):
                processes_state = 'failed'
            elif container.endswith('-skipproc'):
                processes_state = 'skipped'
            elif container.endswith('-noprofile'):
                profiles_state = 'failed'
            (folder / 'inventory.json').write_text(
                json.dumps(inventory(variant, not container.endswith('-nopkg'),
                                     processes_state, profiles_state),
                           ensure_ascii=False), encoding='utf-8')
        (folder / 'docker_inspect_safe.json').write_text(
            json.dumps(docker_inspect(variant), ensure_ascii=False), encoding='utf-8')
    print(root)
    return 0


if __name__ == '__main__':
    sys.exit(main())
