"""Exercise published Faiss producers and validators, with and without jsonschema."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    import jsonschema
except ImportError:
    jsonschema = None

SKILLS = Path(__file__).resolve().parents[1] / "Plugins/code-optimizer/skills"
WORKFLOW = SKILLS / "faiss-optimize/workflows"


def state_fixture():
    return {
        "schema_version": "kpbot-faiss-opt-run.v1",
        "run_id": "contract-test",
        "repo": "/source",
        "source": {"baseline_commit": "a" * 40, "baseline_id": "fixture-build"},
        "contract": {"algorithm": "ivfpq"},
        "stages": {str(i): {"status": "pending"} for i in range(8)},
        "selected_candidate": {},
        "result": {"candidate_commit": {"status": "pending"}},
        "decisions": [],
    }


def handoff_fixture():
    return {
        "schema_version": "kpbot-faiss-handoff.v1",
        "run_id": "contract-test",
        "handoff_type": "review-result",
        "produced_by": "faiss-reviewer",
        "payload": {"summary": "Independent review complete", "artifacts": ["review.md"]},
        "review": {"verdict": "REJECT"},
    }


class FaissContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kpbot-faiss-contract-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def cli(self, script, *args, fallback=False, env=None):
        child_env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        if fallback:
            child_env.pop("PYTHONPATH", None)
        child_env.update(env or {})
        return subprocess.run(
            [sys.executable, *(["-S"] if fallback else []), str(script), *map(str, args)],
            env=child_env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )

    def check_document(self, kind, document, valid, fallback=False):
        path = self.root / (kind + ".json")
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        result = self.cli(WORKFLOW / "scripts" / ("validate_" + kind + ".py"), path,
                          fallback=fallback)
        self.assertEqual(result.returncode, 0 if valid else 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def initialize(self, source, label="release", env=None):
        run = self.root / label
        result = self.cli(
            WORKFLOW / "scripts/init_run.py", "--algorithm", "ivfpq", "--run-id", "contract-test",
            "--repo", source, "--run-dir", run,
            env=dict({"GIT_CEILING_DIRECTORIES": str(self.root)}, **(env or {})),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((run / "state.json").read_text(encoding="utf-8"))
        for fallback in (False, True):
            self.check_document("state", state, True, fallback)
        if jsonschema is not None:
            schema = json.loads((WORKFLOW / "references/state.schema.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(state)
        return state

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "user.name=Contract Fixture",
             "-c", "user.email=fixture@example.invalid", *map(str, args)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )

    def test_release_source_round_trips_without_git(self):
        source = self.root / "源码"
        source.mkdir()
        state = self.initialize(source)
        self.assertEqual(state["source"]["baseline_commit"], "unknown")

    def test_state_artifact_is_utf8_even_with_legacy_locale(self):
        source = self.root / "源码"
        source.mkdir()
        self.initialize(source, env={"PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"})

    def test_unborn_repository_round_trips(self):
        source = self.root / "empty-repo"
        self.assertEqual(self.git("init", source).returncode, 0)
        self.assertEqual(self.initialize(source)["source"]["baseline_commit"], "unknown")

    def test_missing_git_executable_round_trips(self):
        source = self.root / "no-git"
        source.mkdir()
        self.assertEqual(self.initialize(source, env={"PATH": ""})["source"]["baseline_commit"], "unknown")

    def test_git_commit_identity_round_trips_for_both_object_formats(self):
        for format_name in ("sha1", "sha256"):
            with self.subTest(format=format_name):
                source = self.root / format_name
                result = self.git("init", "--object-format=" + format_name, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.git("-C", source, "commit", "--allow-empty", "-m", "fixture")
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = self.git("-C", source, "rev-parse", "HEAD").stdout.strip()
                self.assertEqual(self.initialize(source, format_name + "-run")["source"]["baseline_commit"], expected)

    def test_invalid_baseline_identity_is_rejected_in_both_modes(self):
        for value in ("", "not-a-commit", "<commit>", None, [], "f" * 65):
            for fallback in (False, True):
                with self.subTest(value=value, fallback=fallback):
                    state = state_fixture()
                    state["source"]["baseline_commit"] = value
                    self.check_document("state", state, False, fallback)

    def test_only_one_stage_can_be_active(self):
        for fallback in (False, True):
            for second in ("running", "blocked", "failed"):
                with self.subTest(fallback=fallback, second=second):
                    state = state_fixture()
                    state["stages"]["0"]["status"] = "running"
                    state["stages"]["1"] = {"status": second, "blocker_reason": "fixture"}
                    self.check_document("state", state, False, fallback)

    def test_terminal_state_requires_a_verdict(self):
        for fallback in (False, True):
            state = state_fixture()
            for stage in state["stages"].values():
                stage["status"] = "completed"
            state["selected_candidate"] = {"candidate_id": "c01"}
            self.check_document("state", state, False, fallback)
            state["result"] = {"verdict": "REJECT", "candidate_commit": {"status": "not_applicable"}}
            self.check_document("state", state, True, fallback)

    def test_unknown_commit_does_not_waive_cp2_source_identity(self):
        for fallback in (False, True):
            state = state_fixture()
            state["source"]["baseline_commit"] = "unknown"
            for stage in ("0", "1", "2"):
                state["stages"][stage]["status"] = "completed"
            for baseline_id in ("", "unknown", " TBD ", " <baseline> "):
                with self.subTest(fallback=fallback, baseline_id=baseline_id):
                    state["source"]["baseline_id"] = baseline_id
                    self.check_document("state", state, False, fallback)
            state["source"]["baseline_id"] = "release-archive-sha256-with-build-manifest"
            self.check_document("state", state, True, fallback)

    def test_committed_identity_supports_sha256(self):
        for fallback in (False, True):
            state = state_fixture()
            for stage in state["stages"].values():
                stage["status"] = "completed"
            state["selected_candidate"] = {"candidate_id": "c01"}
            state["result"] = {"verdict": "ACCEPT", "candidate_commit": {
                "status": "committed", "sha": "b" * 64, "committed_by": "coordinator"}}
            self.check_document("state", state, True, fallback)

    def test_review_result_requires_structured_verdict(self):
        for fallback in (False, True):
            handoff = handoff_fixture()
            self.check_document("handoff", handoff, True, fallback)
            del handoff["review"]
            self.check_document("handoff", handoff, False, fallback)
            if jsonschema is not None:
                schema = json.loads((WORKFLOW / "references/handoff.schema.json").read_text(encoding="utf-8"))
                self.assertFalse(jsonschema.Draft202012Validator(schema).is_valid(handoff))

    def test_artifact_paths_are_portable_and_stay_relative(self):
        for kind, fixture, location in (("state", state_fixture, ("stages", "0")),
                                         ("handoff", handoff_fixture, ("payload",))):
            for fallback in (False, True):
                for path in ("../escape", r"..\escape", "/tmp/absolute", r"C:\outside", r"C:outside", r"\\host\share"):
                    with self.subTest(kind=kind, fallback=fallback, path=path):
                        document = fixture()
                        target = document
                        for key in location:
                            target = target[key]
                        target["artifacts"] = [path]
                        self.check_document(kind, document, False, fallback)
                document = fixture()
                target = document
                for key in location:
                    target = target[key]
                target["artifacts"] = ["logs/result.json", r"logs\result.json"]
                self.check_document(kind, document, True, fallback)

    def test_invalid_document_shapes_fail_without_tracebacks(self):
        malformed_states = [[], {**state_fixture(), "stages": []},
                            {**state_fixture(), "source": None},
                            {**state_fixture(), "result": {"candidate_commit": []}}]
        malformed_handoffs = [[], {**handoff_fixture(), "payload": []},
                              {**handoff_fixture(), "payload": {"summary": "x", "artifacts": None}}]
        for kind, documents in (("state", malformed_states), ("handoff", malformed_handoffs)):
            for fallback in (False, True):
                for document in documents:
                    with self.subTest(kind=kind, fallback=fallback, document=document):
                        self.check_document(kind, document, False, fallback)


if __name__ == "__main__":
    unittest.main()
