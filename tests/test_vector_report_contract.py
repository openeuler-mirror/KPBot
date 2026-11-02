"""Check that the dependency-free UT/ST validator never accepts schema-invalid reports."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest

try:
    import jsonschema
except ImportError:
    jsonschema = None

SKILL = Path(__file__).resolve().parents[1] / "Plugins/code-optimizer/skills/vector-ut-st-validation"
SPEC = importlib.util.spec_from_file_location("vector_report_validator", SKILL / "scripts/validate_test_report.py")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def report_fixture():
    return {
        "schema_version": "vector-test-report.v1", "subject": "fixture",
        "baseline_commit": "a" * 40, "candidate_commit": "b" * 40,
        "feature_switch": {"name": "FEATURE", "off_tested": True, "on_tested": True},
        "platform": {"arch": "aarch64", "cpu": "fixture", "os": "fixture", "target_hardware_tested": True},
        "build": {"compiler": "cc", "flags": "-O2"},
        "suites": {name: {"executed": 1, "passed": 1, "failed": 0, "skipped": 0} for name in ("ut", "st")},
        "risk_checks": {name: {"status": "pass", "evidence": ["test.log"], "rationale": ""}
                        for name in ("boundary", "concurrency", "fallback", "feature_off")},
        "raw_artifacts": ["test.log"], "verdict": "READY",
    }


class VectorReportContractTests(unittest.TestCase):
    def test_valid_report_round_trips(self):
        report = report_fixture()
        self.assertEqual(VALIDATOR.validate(report), [])
        if jsonschema is not None:
            schema = json.loads((SKILL / "references/test-report.schema.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(report)

    def test_boolean_test_counters_are_not_execution_evidence(self):
        for name in ("ut", "st"):
            report = report_fixture()
            report["suites"][name].update(executed=True, passed=True, failed=False, skipped=False)
            self.assertTrue(VALIDATOR.validate(report))

    def test_required_risk_fields_cannot_be_omitted(self):
        for field in ("status", "evidence", "rationale"):
            report = report_fixture()
            del report["risk_checks"]["boundary"][field]
            self.assertTrue(VALIDATOR.validate(report), field)

    def test_unknown_fields_follow_published_schema(self):
        for target in (None, "feature_switch"):
            report = report_fixture()
            (report if target is None else report[target])["typo_field"] = 1
            self.assertTrue(VALIDATOR.validate(report))

    def test_extra_risk_checks_are_validated(self):
        for check in (None, {"status": "pass", "evidence": [], "rationale": ""},
                      {"status": "fail", "evidence": ["test.log"], "rationale": "failure"}):
            report = report_fixture()
            report["risk_checks"]["extra"] = check
            self.assertTrue(VALIDATOR.validate(report))

    def test_malformed_enums_return_errors(self):
        report = report_fixture()
        report["verdict"] = []
        report["risk_checks"]["boundary"]["status"] = {}
        self.assertTrue(VALIDATOR.validate(report))

    def test_custom_acceptance_implies_schema_acceptance(self):
        if jsonschema is None:
            self.skipTest("Install jsonschema to check published schema parity")
        schema = json.loads((SKILL / "references/test-report.schema.json").read_text(encoding="utf-8"))
        schema_validator = jsonschema.Draft202012Validator(schema)
        paths = [(), ("feature_switch",), ("platform",), ("build",), ("suites",),
                 ("suites", "ut"), ("risk_checks",), ("risk_checks", "boundary")]
        variants = [report_fixture()]
        for path in paths:
            original = report_fixture()
            target = original
            for key in path:
                target = target[key]
            for field in target:
                for value in (None, [], {}, "", True, 0):
                    report = copy.deepcopy(original)
                    current = report
                    for key in path:
                        current = current[key]
                    current[field] = value
                    variants.append(report)
                report = copy.deepcopy(original)
                current = report
                for key in path:
                    current = current[key]
                del current[field]
                variants.append(report)
        for index, report in enumerate(variants):
            with self.subTest(variant=index):
                errors = VALIDATOR.validate(report)
                if not errors:
                    self.assertTrue(schema_validator.is_valid(report), report)


if __name__ == "__main__":
    unittest.main()
