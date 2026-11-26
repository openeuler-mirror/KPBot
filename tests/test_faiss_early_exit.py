"""Contract checks for the faiss-early-exit skill."""

import importlib.util
import pathlib
import unittest

HELPER = pathlib.Path(__file__).with_name("faiss_skill_checks.py")
SPEC = importlib.util.spec_from_file_location("faiss_skill_checks", HELPER)
assert SPEC is not None and SPEC.loader is not None
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class FaissEarlyExitTests(unittest.TestCase):
    SKILL = "faiss-early-exit"

    def test_contract(self):
        checks.assert_skill_contract(self, self.SKILL)

    def test_single_responsibility_scope(self):
        text = checks.skill_text(self.SKILL)
        self.assertIn("判决", text)
        self.assertIn("召回", text)
        self.assertIn("worst-case", text)


if __name__ == "__main__":
    unittest.main()
