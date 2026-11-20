"""Keep app-tuner subskill registries consistent with REQUIRED_SUBSKILLS."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "Plugins/app-tuner/skills/kpbot-app-tuner"

# These subskills are intentionally not part of the coverage pass:
# - io-memory-network-bottleneck-analysis is a pre-screen
# - database-workload-analysis is nested under application-config-optimization
# - vllm-ascend-profiling is nested under accelerator-optimization
# - ceph-optimization hands OS-layer findings to os-optimization
NON_COVERAGE_SUBSKILLS = {
    "io-memory-network-bottleneck-analysis",
    "database-workload-analysis",
    "vllm-ascend-profiling",
    "ceph-optimization",
}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AppTunerSubskillRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = load_module("app_tuner_validator", SKILL_DIR / "scripts/validate_skill_quality.py")
        cls.tasks = load_module("app_tuner_task_builder", SKILL_DIR / "scripts/create_subagent_tasks.py")

    def test_every_required_subskill_is_in_task_catalog(self):
        missing = sorted(set(self.validator.REQUIRED_SUBSKILLS) - set(self.tasks.SUBSKILL_CATALOG))
        self.assertEqual(missing, [])

    def test_coverage_set_matches_required_subskills(self):
        expected = sorted(set(self.validator.REQUIRED_SUBSKILLS) - NON_COVERAGE_SUBSKILLS)
        self.assertEqual(sorted(self.tasks.MAIN_OPTIMIZATION_SUBSKILLS), expected)

    def test_every_coverage_subskill_is_in_task_catalog(self):
        missing = sorted(set(self.tasks.MAIN_OPTIMIZATION_SUBSKILLS) - set(self.tasks.SUBSKILL_CATALOG))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
