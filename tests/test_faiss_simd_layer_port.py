"""Contract checks for the faiss-simd-layer-port skill."""

import importlib.util
import pathlib
import unittest

HELPER = pathlib.Path(__file__).with_name("faiss_skill_checks.py")
SPEC = importlib.util.spec_from_file_location("faiss_skill_checks", HELPER)
assert SPEC is not None and SPEC.loader is not None
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class FaissSimdLayerPortTests(unittest.TestCase):
    SKILL = "faiss-simd-layer-port"

    def test_contract(self):
        checks.assert_skill_contract(self, self.SKILL)

    def test_single_responsibility_scope(self):
        text = checks.skill_text(self.SKILL)
        self.assertIn("定宽", text)
        self.assertIn("分派", text)
        self.assertIn("门控", text)
        self.assertIn("faiss-simd-optimization", text)


if __name__ == "__main__":
    unittest.main()
