#!/usr/bin/env python3
"""Tests for register allocation selection."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SELECT_SCRIPT = SCRIPT_DIR / "select_register_allocation.py"


def run_select(*args: str) -> dict[str, object]:
    """Run select_register_allocation.py and return its JSON payload."""

    completed = subprocess.run(
        [sys.executable, str(SELECT_SCRIPT), *args, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


class RegisterAllocationDecisionTests(unittest.TestCase):
    """Coverage for the two-stage register allocation policy."""

    def test_neon_intrinsics_avoids_pressure_one_shape_by_default(self) -> None:
        payload = run_select(
            "--isa",
            "neon",
            "--dtype",
            "float32",
            "--n",
            "4",
            "--codegen-style",
            "intrinsics",
        )

        selected = payload["selected_register_allocation"]
        self.assertEqual(selected["shape"], "20x4")
        self.assertNotEqual(selected["shape"], "24x4")
        self.assertLess(selected["pressure_ratio"], 1.0)

    def test_inline_asm_can_select_high_risk_with_verification(self) -> None:
        payload = run_select(
            "--isa",
            "neon",
            "--dtype",
            "float32",
            "--n",
            "4",
            "--codegen-style",
            "inline_asm",
        )

        selected = payload["selected_register_allocation"]
        self.assertEqual(selected["shape"], "24x4")
        self.assertEqual(selected["spill_risk"], "high")
        self.assertTrue(payload["verification_required"])
        self.assertIn("scan clobber list", " ".join(payload["verification_actions"]))

    def test_sve_outputs_fallback_chain_and_underutilization_flags(self) -> None:
        payload = run_select(
            "--isa",
            "sve",
            "--dtype",
            "float32",
            "--n",
            "8",
            "--vector-bits",
            "256",
        )

        selected = payload["selected_register_allocation"]
        self.assertEqual(selected["shape"], "16x8")
        self.assertGreater(len(payload["fallback_register_allocations"]), 0)
        candidates = payload["candidate_register_allocations"]
        low_candidate = next(candidate for candidate in candidates if candidate["shape"] == "4x8")
        self.assertTrue(low_candidate["underutilization_risk"])

    def test_gpr_overbudget_rejects_candidates(self) -> None:
        payload = run_select(
            "--isa",
            "neon",
            "--dtype",
            "float32",
            "--n",
            "4",
            "--reserve-general-regs",
            "30",
        )

        self.assertIsNone(payload["selected_register_allocation"])
        self.assertFalse(payload["register_allocation_plan"]["success"])
        first_candidate = payload["candidate_register_allocations"][0]
        self.assertFalse(first_candidate["eligible"])
        self.assertIn("gpr budget exceeded", " ".join(first_candidate["rejection_reasons"]))

    def test_shape_candidates_restrict_search_space(self) -> None:
        payload = run_select(
            "--isa",
            "neon",
            "--dtype",
            "float32",
            "--shape-candidates",
            "8x8,12x8,16x8",
        )

        shapes = [candidate["shape"] for candidate in payload["candidate_register_allocations"]]
        self.assertEqual(shapes, ["8x8", "12x8", "16x8"])

    def test_sme_za_requires_inline_asm_without_abi_runtime(self) -> None:
        intrinsics_payload = run_select(
            "--isa",
            "sme",
            "--dtype",
            "float32",
            "--shape-candidates",
            "8x8",
            "--uses-za-tile",
            "--codegen-style",
            "intrinsics",
        )
        self.assertIsNone(intrinsics_payload["selected_register_allocation"])
        rejection_text = " ".join(
            intrinsics_payload["candidate_register_allocations"][0]["rejection_reasons"]
        )
        self.assertIn("SME ZA intrinsics require verified SME ABI runtime support", rejection_text)

        asm_payload = run_select(
            "--isa",
            "sme",
            "--dtype",
            "float32",
            "--shape-candidates",
            "8x8",
            "--uses-za-tile",
            "--codegen-style",
            "inline_asm",
        )
        selected = asm_payload["selected_register_allocation"]
        class_budgets = selected["register_class_budgets"]
        self.assertIn("za_tile", class_budgets)
        self.assertGreater(class_budgets["za_tile"]["needed"], 0)
        self.assertIn("__arm_tpidr2", " ".join(asm_payload["verification_actions"]))

    def test_legacy_cli_payload_fields_remain_present(self) -> None:
        payload = run_select("--isa", "sve", "--dtype", "float32", "--n", "8")

        self.assertIn("register_allocation_plan", payload)
        self.assertIn("candidate_register_allocations", payload)
        self.assertIn("selected_register_allocation", payload)
        self.assertIn("underutilization_risk", payload)
        self.assertIn("verification_required", payload)


if __name__ == "__main__":
    unittest.main()
