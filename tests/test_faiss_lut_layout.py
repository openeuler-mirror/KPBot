"""Contract checks for the faiss-lut-layout skill."""

import importlib.util
import pathlib
import unittest

HELPER = pathlib.Path(__file__).with_name("faiss_skill_checks.py")
SPEC = importlib.util.spec_from_file_location("faiss_skill_checks", HELPER)
assert SPEC is not None and SPEC.loader is not None
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class FaissLutLayoutTests(unittest.TestCase):
    SKILL = "faiss-lut-layout"

    def test_contract(self):
        checks.assert_skill_contract(self, self.SKILL)

    def test_single_responsibility_scope(self):
        text = checks.skill_text(self.SKILL)
        self.assertIn("布局", text)
        self.assertIn("摊销", text)
        self.assertIn("足迹", text)


if __name__ == "__main__":
    unittest.main()
