#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造假的采集包，给测试用。不连网、不依赖 docker。

    make_fixture.py <目录> <容器名>[,<容器名>...]

造出来的包是最小但完整的形态：collection_meta.json + 每个容器一个
inventory.json / docker_inspect_safe.json。
"""
import json
import pathlib
import sys


def inventory(container):
    interp = {'executable': '/usr/bin/python3', 'version': '3.11.2 (main) [GCC]',
              'prefix': '/usr', 'base_prefix': '/usr', 'path': ['/usr/lib/python3.11'],
              'sitepackages': ['/usr/lib/python3/dist-packages']}
    profile = {
        'pid': 7, 'exe': '/usr/bin/python3', 'python_executable_used': '/usr/bin/python3',
        'chosen_by': 'process_argv', 'environment_source': 'runtime_process',
        'interpreter': interp, 'collection_ok': True,
        'pip_list': [{'name': 'vllm', 'version': '0.9.1'},
                     {'name': 'torch', 'version': '2.5.1'},
                     {'name': 'torch_npu', 'version': '2.5.1.post1'}],
        'pip_freeze': None,
        'environment_relevant': {'HCCL_BUFFSIZE': '200', 'ASCEND_RT_VISIBLE_DEVICES': '0'},
        'module_locations': {'vllm': {'origin': '/opt/vllm/__init__.py', 'locations': []}},
    }
    return {
        'probe_python': {'executable': '/usr/bin/python3', 'version': '3.11.2'},
        'os_release': 'PRETTY_NAME="openEuler 24.03 LTS"\nNAME="openEuler"\n',
        'uname': {'system': 'Linux', 'release': '5.10.0', 'machine': 'aarch64'},
        'glibc': ['glibc', '2.38'], 'cpu_count': 192,
        'npu_smi': {'returncode': 0, 'stdout': ''},
        'lscpu': {'returncode': 0, 'stdout': 'Architecture:  aarch64\nCPU(s):  192\n'},
        'cann_driver_version_files': {},
        'git': {},
        'processes': [{'pid': 7, 'comm': 'VLLM::EngineCore', 'exe': '/usr/bin/python3',
                       'cwd': '/workspace',
                       'args': ['python3', '-m', 'vllm.entrypoints.openai.api_server',
                                '--max-num-seqs', '256'],
                       'env': {'HCCL_BUFFSIZE': '200'}}],
        'profiles': [profile],
        'selection': {'explicit_python': None, 'explicit_env_names': []},
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
    for container in containers:
        folder = root / container
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'inventory.json').write_text(
            json.dumps(inventory(container), ensure_ascii=False), encoding='utf-8')
        (folder / 'docker_inspect_safe.json').write_text(json.dumps({
            'image_ref': 'ascend/vllm:latest', 'image_id': 'sha256:aaa',
            'state': {'Status': 'running', 'Running': True},
            'limits': {'NetworkMode': 'host', 'IpcMode': 'host', 'ShmSize': 68719476736,
                       'CpusetCpus': '0-95', 'Runtime': 'runc'},
            'config_env_relevant': {'VLLM_USE_V1': '1'},
            'mounts': [], 'devices': [],
        }, ensure_ascii=False), encoding='utf-8')
    print(root)
    return 0


if __name__ == '__main__':
    sys.exit(main())
