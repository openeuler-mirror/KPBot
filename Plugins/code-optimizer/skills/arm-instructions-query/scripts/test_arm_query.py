#!/usr/bin/env python3
"""Regression tests for the pipeline-level ARM query wrapper."""

from __future__ import annotations

import sys
import unittest
import contextlib
import io
import argparse
from pathlib import Path

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import arm_query  # noqa: E402


class ArmQueryFamilyTests(unittest.TestCase):
    def titles_for_family(self, family: str) -> set[str]:
        return {record.get("title", "") for record in arm_query.instruction_records_for_family(family)}

    def test_plain_sve_excludes_sve2_only_instruction(self) -> None:
        self.assertNotIn("EOR3", self.titles_for_family("sve"))

    def test_sve2_includes_sve2_only_instruction(self) -> None:
        self.assertIn("EOR3", self.titles_for_family("sve2"))

    def test_plain_sve_keeps_shared_baseline_instruction(self) -> None:
        self.assertIn("ABS", self.titles_for_family("sve"))

    def test_intrinsic_search_uses_wrapper_relevance(self) -> None:
        original = arm_query.acle_query.search_score

        def fail_if_called(entry: dict, pattern_lower: str) -> int:
            raise AssertionError("acle_query search scorer should not be called")

        arm_query.acle_query.search_score = fail_if_called
        try:
            args = argparse.Namespace(
                data_dir=str(arm_query.acle_query.DATA_DIR),
                keyword="vadd",
                family="neon",
                limit=1,
                json=True,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                result = arm_query.command_intrinsic_search(args)
        finally:
            arm_query.acle_query.search_score = original

        self.assertEqual(result, 0)

    def test_intrinsic_keyword_rejects_empty_input(self) -> None:
        entry = {
            "name": "vaddq_f32",
            "description": "Add floating-point vectors.",
            "expanded_names": ["vaddq_f32"],
        }

        self.assertFalse(arm_query.intrinsic_matches_keyword(entry, ""))
        self.assertFalse(arm_query.intrinsic_matches_keyword(entry, "   "))
        self.assertFalse(arm_query.intrinsic_matches_keyword(entry, None))
        self.assertEqual(arm_query.intrinsic_relevance(entry, ""), 0)

    def test_intrinsic_keyword_treats_special_chars_as_plain_text(self) -> None:
        entry = {
            "name": "svtbl_u8",
            "description": "Table lookup [byte] operation.",
            "expanded_names": [],
        }

        self.assertTrue(arm_query.intrinsic_matches_keyword(entry, "[byte]"))
        self.assertFalse(arm_query.intrinsic_matches_keyword(entry, "(byte)"))


if __name__ == "__main__":
    unittest.main()
