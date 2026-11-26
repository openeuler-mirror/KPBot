"""Contract checks for the faiss-memory-latency skill."""

import importlib.util
import pathlib
import unittest

HELPER = pathlib.Path(__file__).with_name("faiss_skill_checks.py")
SPEC = importlib.util.spec_from_file_location("faiss_skill_checks", HELPER)
assert SPEC is not None and SPEC.loader is not None
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class FaissMemoryLatencyTests(unittest.TestCase):
    SKILL = "faiss-memory-latency"

    def test_contract(self):
        checks.assert_skill_contract(self, self.SKILL)

    def test_single_responsibility_scope(self):
        text = checks.skill_text(self.SKILL)
        self.assertIn("预取", text)
        self.assertIn("带宽", text)


if __name__ == "__main__":
    unittest.main()
