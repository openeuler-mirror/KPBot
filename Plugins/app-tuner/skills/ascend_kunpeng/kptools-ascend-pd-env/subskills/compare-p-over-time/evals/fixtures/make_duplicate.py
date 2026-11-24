"""Create a directory and archive of the same P snapshot for the eval."""
import json
import pathlib
import sys
import tarfile

if __name__ == '__main__':
    work = pathlib.Path(sys.argv[1])
    work.mkdir(mode=0o700)
    source = work / 'p-snapshot'
    (source / 'vllm-p').mkdir(parents=True)
    meta = {'schema': 1, 'node': 'fixture-node', 'targets': ['vllm-p'], 'role': 'P',
            'timestamp_utc': '20261023T000000Z', 'folders': {'vllm-p': 'vllm-p'}}
    (source / 'collection_meta.json').write_text(json.dumps(meta), encoding='utf-8')
    inventory = {'collection_status': {'completed': ['cgroup']},
                 'cgroup': {'cpu.max': '100000 100000'}}
    (source / 'vllm-p/inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
    archive = work / 'p-snapshot.tar.gz'
    with tarfile.open(str(archive), 'w:gz') as output:
        output.add(str(source), arcname=source.name)
    print(source)
    print(archive)
