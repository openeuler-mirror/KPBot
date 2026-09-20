"""Install into disposable projects and exercise installed diagnostic helpers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[4]
TESTS = Path(__file__).resolve().parent
REQUIRED = [
    'prompts/stage.design-experiment.md',
    'prompts/stage.validate-experiment.md',
    'references/experiment-contract.md',
    'scripts/experiment_record.py',
    'scripts/experiment_feedback.py',
]

# Explicit module list prevents recursively loading this installation test.
INSTALLED_RUNNER = r'''
import json
from pathlib import Path
import sys
import unittest
import experiment_record
import experiment_feedback
sys.path.insert(0, sys.argv[2])
import test_record
import test_feedback
expected = Path(sys.argv[1]).resolve()
origins = {}
for module, tests in ((experiment_record, test_record),
                      (experiment_feedback, test_feedback)):
    origin = Path(module.__file__).resolve()
    assert origin == expected / (module.__name__ + '.py'), (origin, expected)
    assert tests.SCRIPT.resolve() == origin, (tests.SCRIPT, origin)
    origins[module.__name__] = str(origin)
print('Installed helper origins: ' + json.dumps(origins), flush=True)
suite = unittest.defaultTestLoader.loadTestsFromNames(['test_record', 'test_feedback'])
assert suite.countTestCases() > 0
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
'''


class PlatformTests(unittest.TestCase):
    def test_installed_helpers(self):
        for tool in ('claude', 'opencode'):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory(
                prefix=f'kpbot-feedback-{tool}-'
            ) as temporary:
                project = Path(temporary)
                installed = project / f'.{tool}' / 'skills' / 'kpbot-code-optimizer'
                subprocess.run(
                    ['bash', str(REPO / 'install.sh'), 'project', tool, str(project)],
                    check=True, capture_output=True, text=True,
                )
                for relative in REQUIRED:
                    self.assertTrue((installed / relative).is_file(), relative)
                scripts = installed / 'scripts'
                env = dict(os.environ, PYTHONPATH=str(scripts), PYTHONDONTWRITEBYTECODE='1')
                try:
                    result = subprocess.run(
                        [sys.executable, '-c', INSTALLED_RUNNER, str(scripts), str(TESTS)],
                        cwd=project, env=env, check=True, capture_output=True, text=True,
                    )
                except subprocess.CalledProcessError as error:
                    self.fail(f'{tool} installed suite failed:\n{error.stdout}\n{error.stderr}')
                print(f'\n{tool}: {result.stdout.strip()}\n{result.stderr.strip()}', flush=True)


if __name__ == '__main__':
    unittest.main()
