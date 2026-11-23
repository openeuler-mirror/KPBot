"""Test fixed endpoint, archive-like names, and split outcomes with a Docker stub."""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[2]
CHILD = pathlib.Path('subskills/collect-environment/scripts/collect_role.sh')
CONTAINER_ID = 'a' * 64


class CollectRole(unittest.TestCase):
    def test_one_verified_endpoint_and_partial_results(self):
        with tempfile.TemporaryDirectory(prefix='pdenv-collect-') as tmp:
            root = pathlib.Path(tmp)
            wrapper = root / CHILD
            wrapper.parent.mkdir(parents=True)
            shutil.copyfile(str(SKILL / CHILD), str(wrapper))
            scripts = root / 'scripts'
            scripts.mkdir()
            (root / 'docker').write_text(
                '#!/bin/bash\nset -eu\n'
                'test -z "${DOCKER_CONTEXT+x}"\n'
                'test "$DOCKER_HOST" = "$EXPECTED_ENDPOINT"\n'
                'printf "%s\\n" "$*" >> "$LOG"\n'
                'case "$1" in\n'
                'info) printf "node-fixture\\n" ;;\n'
                'container) printf "' + CONTAINER_ID + ' /%s\\n" "${@: -1}" ;;\n'
                'version) printf "test\\n" ;;\n'
                '*) exit 1 ;;\n'
                'esac\n', encoding='utf-8')
            (root / 'docker').chmod(0o700)
            (scripts / 'discover.sh').write_text('docker version\n', encoding='utf-8')
            (scripts / 'collect.sh').write_text(
                'set -eu\n'
                'test "$1" = "' + CONTAINER_ID + '"\n'
                'test "$DOCKER_HOST" = "$EXPECTED_ENDPOINT"\n'
                'mkdir -p "$5/采集"\n'
                'printf "%s\\n" "$3" > "$5/采集/SUMMARY.txt"\n'
                'if [ "$3" = D ]; then exit 130; fi\n'
                'touch "$5/采集.tar.gz"\n', encoding='utf-8')
            endpoint, log = root / 'd.sock', root / 'calls'
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                       DOCKER_HOST='tcp://unrelated:2375', DOCKER_CONTEXT='remote',
                       EXPECTED_ENDPOINT='unix://' + str(endpoint), LOG=str(log))
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(endpoint))
                def run(node, name, role, out):
                    return subprocess.run(['bash', str(wrapper), str(endpoint), node, name, role, out],
                                          cwd=str(root), env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, encoding='utf-8')
                rejected = run('wrong-node', 'other', 'P', 'no-out')
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(log.read_text().splitlines(), ["info --format {{.Name}}"])
                p = run('node-fixture', 'vllm-p.tgz', 'P', 'p-out')
                d = run('node-fixture', 'vllm-d', 'D', 'd-out')
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('vllm-p.tgz (' + CONTAINER_ID + ')', p.stdout)
            self.assertEqual(d.returncode, 130, d.stderr)
            self.assertTrue((root / 'p-out/采集.tar.gz').exists())
            self.assertTrue((root / 'd-out/采集/SUMMARY.txt').exists())
            self.assertFalse((root / 'd-out/采集.tar.gz').exists())
            fixture = SKILL / CHILD.parent.parent / 'evals/fixtures/partial-collection.json'
            with fixture.open(encoding='utf-8') as stream:
                outcomes = json.load(stream)
            self.assertEqual((outcomes['P']['exit_code'], outcomes['D']['exit_code']), (0, 130))
            self.assertTrue(outcomes['D']['never_started'])


if __name__ == '__main__':
    unittest.main()
