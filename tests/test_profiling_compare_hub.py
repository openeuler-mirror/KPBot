"""kptools-ascend-profiling-compare-hub 的契约测试（stub provider）。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "Plugins/app-tuner/skills/kptools-ascend-profiling-compare-hub"
SCRIPT = SKILL / "scripts/hub.py"

SPEC = importlib.util.spec_from_file_location("hub_mod", SCRIPT)
hub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hub)

PROVIDERS = {p["skill"]: p["script"] for p in hub.PROVIDERS}

STUB = '''#!/usr/bin/env python3
import argparse, json, sys, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("cand", nargs="?")
ap.add_argument("--json", dest="out_json")
ap.add_argument("--out-html", dest="out_html")
ap.add_argument("--base-steps")
ap.add_argument("--cand-steps")
ap.add_argument("--min-delta-ms", type=float)
a = ap.parse_args()
MODE = {mode!r}
if MODE == "fail":
    sys.exit(2)
if MODE == "sleep":
    time.sleep(5)
if MODE == "bad":
    Path(a.out_json).write_text("{{bad json", encoding="utf-8")
    sys.exit(0)
if MODE == "crash":
    raise RuntimeError("stub crash")
if MODE == "abscheck":
    if not Path(a.base).is_absolute():
        sys.exit(9)
    if a.cand and not Path(a.cand).is_absolute():
        sys.exit(9)
env = {{
  "skill": {skill!r},
  "schema_version": 1,
  "mode": "pair" if a.cand else "single",
  "inputs": {{"base": a.base, "cand": a.cand, "steps": None}},
  "data_quality": {{"sidecars": ["stub.csv"], "degraded": [], "confidence": "high"}},
  "metrics": [{{"name": {metric!r}, "unit": "ms", "base": 2.0, "cand": 1.0,
               "delta": 1.0, "delta_pct": 50.0, "verdict": "improved",
               "noise_band": 0.1, "evidence": "stub"}}],
  "per_step": [{{"step": 0, "base": 2.0, "cand": 1.0, "delta": 1.0}}],
  "summary": "stub summary: " + {skill!r},
  "notes": [],
}}
if MODE == "summary_not_str":
    env["summary"] = 123
if MODE == "empty_metrics":
    env["metrics"] = []
if MODE == "require_steps":
    if not (a.base_steps and a.cand_steps):
        sys.exit(7)
if MODE == "metric_no_evidence":
    env["metrics"][0].pop("evidence", None)
if MODE == "string_metric":
    env["metrics"][0]["base"] = "<script>alert(1)</script>"
Path(a.out_json).write_text(json.dumps(env), encoding="utf-8")
Path(a.out_html).write_text("<html>stub</html>", encoding="utf-8")
'''


def make_stub(root, skill, script, mode="ok", metric="step_time_ms"):
    d = root / skill / Path(script).parent
    d.mkdir(parents=True, exist_ok=True)
    path = root / skill / script
    path.write_text(STUB.format(mode=mode, skill=skill, metric=metric), encoding="utf-8")
    return path


def all_stubs(root, mode="ok"):
    for skill, script in PROVIDERS.items():
        metric = "step_time_ms" if skill == "kptools-ascend-profiling-compare" else "x_metric"
        make_stub(root, skill, script, mode=mode, metric=metric)


def run_hub(args, timeout=60, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT)] + args,
                          capture_output=True, text=True, timeout=timeout, cwd=cwd)


class OutputGuardTests(unittest.TestCase):
    def test_rejects_same_output_file(self):
        with tempfile.TemporaryDirectory() as d:
            same = Path(d) / "same.html"
            run = run_hub(["--out-html", str(same), "--json", str(same), "a", "b"])
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(same.exists())


class AggregationTests(unittest.TestCase):
    def test_all_providers_ok(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            out_json, out_html = d / "r.json", d / "r.html"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(out_html), "cap_a", "cap_b"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(hub.validate_env(env), [])
            self.assertEqual(env["skill"], "kptools-ascend-profiling-compare-hub")
            self.assertEqual(env["mode"], "pair")
            self.assertEqual([m["name"] for m in env["metrics"]], ["step_time_ms"])
            self.assertIn("kptools-ascend-profiling-compare /", env["metrics"][0]["evidence"])
            self.assertEqual(len(env["per_step"]), 1)
            self.assertEqual(env["data_quality"]["confidence"], "high")
            self.assertEqual(env["inputs"]["steps"], "auto")
            total = len(hub.PROVIDERS)
            self.assertIn(f"provider {total - 1}/{total}", " ".join(env["notes"]))
            html = out_html.read_text(encoding="utf-8")
            self.assertIn("章节执行矩阵", html)
            self.assertIn("调用栈差异", html)

    def test_relative_workdir_is_absolutized(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            out_json, out_html = d / "r.json", d / "r.html"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(out_html), "--workdir", "out", "a", "b"],
                          cwd=str(d))
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            total = len(hub.PROVIDERS)
            self.assertIn(f"provider {total - 1}/{total}", " ".join(env["notes"]))
            workdir = d / "out"
            self.assertTrue(workdir.is_dir())
            self.assertTrue(any(workdir.rglob("kptools-ascend-profiling-compare*.json")))

    def test_missing_skill_is_skipped_report_complete(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            make_stub(skills, "kptools-ascend-profiling-compare", "scripts/compare.py")
            out_json, out_html = d / "r.json", d / "r.html"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(out_html), "a", "b"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertTrue(any("not_installed" in x for x in env["data_quality"]["degraded"]))
            self.assertTrue(out_html.exists())

    def test_no_overview_uses_status_metric(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            make_stub(skills, "kptools-ascend-profiling-compare-opstats", "scripts/opstats.py")
            out_json = d / "r.json"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(d / "r.html"), "a", "b"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["metrics"][0]["name"], "hub_chapter_ok")
            self.assertEqual(env["data_quality"]["confidence"], "medium")
            self.assertIn("总览不可用", env["summary"])
            self.assertEqual(hub.validate_env(env), [])

    def test_only_filter(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            out_json = d / "r.json"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(d / "r.html"), "a", "b", "--only", "op_"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertIn("1/1", " ".join(env["notes"]))
            self.assertTrue(any("--only" in n for n in env["notes"]))


class ContractValidationTests(unittest.TestCase):
    """合法 JSON 但不符合 A.1 的子 skill 输出必须判 failed，且不污染 hub 输出。"""

    def _run_with_bad_provider(self, mode):
        d = Path(tempfile.mkdtemp())
        skills = d / "skills"
        all_stubs(skills)
        make_stub(skills, "kptools-ascend-profiling-compare-opstats",
                  "scripts/opstats.py", mode=mode)
        out_json, out_html = d / "r.json", d / "r.html"
        run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                       "--out-html", str(out_html), "a", "b"])
        self.assertEqual(run.returncode, 0, run.stderr)
        env = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(any("A.1 不合规" in x for x in env["data_quality"]["degraded"]))
        self.assertEqual(hub.validate_env(env), [])
        return env

    def test_summary_not_str_failed(self):
        self._run_with_bad_provider("summary_not_str")

    def test_empty_metrics_failed(self):
        self._run_with_bad_provider("empty_metrics")

    def test_metric_missing_evidence_failed(self):
        self._run_with_bad_provider("metric_no_evidence")


class ChildNotesRenderTests(unittest.TestCase):
    def test_render_uses_child_env_notes(self):
        css = Path("/nonexistent.css")
        env = {"skill": "hub", "schema_version": 1, "mode": "pair",
               "inputs": {"base": "a", "cand": "b", "steps": "auto"},
               "data_quality": {"sidecars": [], "degraded": [], "confidence": "high"},
               "metrics": [], "per_step": [], "summary": "ok", "notes": []}
        child = {"skill": "s", "chapter": "章节", "intro": "", "status": "ok",
                 "reason": "", "required": False, "html": None,
                 "env": {"notes": ["child-only-note"], "metrics": [], "per_step": [],
                         "summary": "ok", "inputs": {"base": "a", "cand": "b"},
                         "data_quality": {"sidecars": [], "degraded": [], "confidence": "high"}}}
        html = hub.render_html(env, [child], css)
        self.assertIn("child-only-note", html)


class ValidatorMarkerTests(unittest.TestCase):
    """validate_env 与 A.1 真源 envelope.validate 口径一致。"""

    def _base(self):
        return {
            "skill": "x", "schema_version": 1, "mode": "pair", "inputs": {},
            "data_quality": {"sidecars": [], "degraded": [], "confidence": "high"},
            "metrics": [{"name": "a", "unit": "ms", "base": 1, "cand": 2, "delta": 1,
                         "delta_pct": 1, "verdict": "improved", "noise_band": 0.1,
                         "evidence": "e"}],
            "per_step": [], "summary": "ok", "notes": [],
        }

    def test_full_envelope_passes(self):
        self.assertEqual(hub.validate_env(self._base()), [])

    def test_non_object_envelope_is_rejected_without_crash(self):
        # provider 输出合法 JSON 数组 [] 时不能抛 AttributeError 拖垮整个编排。
        self.assertEqual(hub.validate_env([]), ["信封必须是 JSON 对象"])
        self.assertTrue(hub.validate_env([1, 2]))

    def test_bad_schema_version_rejected(self):
        env = self._base()
        env["schema_version"] = 2
        self.assertTrue(hub.validate_env(env))

    def test_bad_verdict_rejected(self):
        env = self._base()
        env["metrics"][0]["verdict"] = "WEIRD"
        self.assertTrue(hub.validate_env(env))

    def test_missing_data_quality_subfield_rejected(self):
        env = self._base()
        del env["data_quality"]["sidecars"]
        self.assertTrue(hub.validate_env(env))


class FailureIsolationTests(unittest.TestCase):
    def _run(self, mode, extra=None, timeout="300"):
        d = Path(tempfile.mkdtemp())
        skills = d / "skills"
        all_stubs(skills)
        make_stub(skills, "kptools-ascend-profiling-compare-opstats",
                  "scripts/opstats.py", mode=mode)
        out_json, out_html = d / "r.json", d / "r.html"
        args = ["--skills-root", str(skills), "--json", str(out_json),
                "--out-html", str(out_html), "a", "b", "--timeout", timeout]
        run = run_hub(args + (extra or []))
        return d, run, out_json, out_html

    def test_nonzero_exit_marked_failed(self):
        d, run, out_json, out_html = self._run("fail")
        self.assertEqual(run.returncode, 0, run.stderr)
        env = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(any("rc=2" in x for x in env["data_quality"]["degraded"]))
        self.assertTrue(out_html.exists())

    def test_bad_json_marked_failed(self):
        d, run, out_json, out_html = self._run("bad")
        env = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(any("JSON 解析失败" in x for x in env["data_quality"]["degraded"]))

    def test_crash_marked_failed(self):
        d, run, out_json, out_html = self._run("crash")
        env = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(any("rc=1" in x for x in env["data_quality"]["degraded"]))

    def test_timeout_marked_failed(self):
        d, run, out_json, out_html = self._run("sleep", timeout="1")
        env = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(any("timeout" in x for x in env["data_quality"]["degraded"]))
        self.assertTrue(out_html.exists())


class PathTests(unittest.TestCase):
    def test_relative_inputs_are_absolutized(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            make_stub(skills, "kptools-ascend-profiling-compare",
                      "scripts/compare.py", mode="abscheck")
            out_json = d / "r.json"
            run = subprocess.run(
                [sys.executable, str(SCRIPT), "rel_cap_a", "rel_cap_b",
                 "--skills-root", str(skills), "--json", str(out_json),
                 "--out-html", str(d / "r.html")],
                capture_output=True, text=True, timeout=60, cwd=str(d))
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertFalse(any("rc=9" in x for x in env["data_quality"]["degraded"]))
            self.assertEqual(env["metrics"][0]["name"], "step_time_ms")


class ModeTests(unittest.TestCase):
    def test_single_capture_skips_pair_providers(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            out_json = d / "r.json"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(d / "r.html"), "only_one"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(env["mode"], "single")
            self.assertTrue(any("needs_two_captures" in x for x in env["data_quality"]["degraded"]))
            self.assertTrue(any("needs_step_groups" in x for x in env["data_quality"]["degraded"]))

    def test_steps_flags_forwarded_to_overview(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            make_stub(skills, "kptools-ascend-profiling-compare", "scripts/compare.py",
                      mode="require_steps", metric="step_time_ms")
            out_json = d / "r.json"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(d / "r.html"), "a", "b",
                           "--base-steps", "0,1", "--cand-steps", "5,6"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertIn("step_time_ms", [m["name"] for m in env["metrics"]])

    def test_string_metric_value_rejected_and_not_rendered(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            make_stub(skills, "kptools-ascend-profiling-compare", "scripts/compare.py",
                      mode="string_metric", metric="step_time_ms")
            out_json, out_html = d / "r.json", d / "r.html"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(out_html), "a", "b"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertTrue(any("非数值" in x for x in env["data_quality"]["degraded"]))
            html = out_html.read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", html)

    def test_render_escapes_non_numeric_metric_values(self):
        env = {"inputs": {"base": "b", "cand": None, "steps": None},
               "data_quality": {"degraded": [], "sidecars": [], "confidence": "high"},
               "summary": "ok", "metrics": [], "per_step": [], "notes": []}
        row = {"chapter": "c", "skill": "s", "status": "ok", "reason": "", "html": None,
               "intro": "i",
               "env": {"metrics": [{"name": "m", "unit": "ms", "base": "<b>x</b>",
                                    "cand": None, "delta": 1.0, "delta_pct": None,
                                    "verdict": "info", "noise_band": None, "evidence": "e"}],
                       "summary": "s", "data_quality": {"confidence": "high"}}}
        html = hub.render_html(env, [row], Path("/nonexistent.css"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)

    def test_filter_accepts_short_prefix_and_full_name(self):
        stack = next(p for p in hub.PROVIDERS if p["skill"].endswith("-stack"))
        for token in ("stack", "stack_", "kptools-ascend-profiling-compare-stack"):
            self.assertTrue(hub._match_filter(stack, {token}), token)

    def test_skip_short_name_skips_chapter(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            base, cand, work = d / "cap_a", d / "cap_b", d / "work"
            for p in (base, cand, work):
                p.mkdir()
            rows = hub.orchestrate(str(base), str(cand), skills, set(), {"stack"}, {}, work, 30)
            by_skill = {r["skill"]: r for r in rows}
            stack = by_skill["kptools-ascend-profiling-compare-stack"]
            self.assertEqual(stack["status"], "skipped")
            self.assertEqual(stack["reason"], "user_skipped")

    def test_unmatched_filter_warns(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            out_json = d / "r.json"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(d / "r.html"), "a", "b", "--skip", "stak"])
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("未匹配任何章节", run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertTrue(any("未匹配任何章节" in n for n in env["notes"]))

    def test_both_mode_single_capture_runs_both_chapters(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            base = d / "cap_a"
            base.mkdir()
            work = d / "work"
            work.mkdir()
            rows = hub.orchestrate(str(base), None, skills, [], [], {}, work, 30)
            by_skill = {r["skill"]: r for r in rows}
            for skill in (
                "kptools-ascend-profiling-stream",
                "kptools-ascend-profiling-compare-bubble",
                "kptools-ascend-profiling-compare-free",
            ):
                self.assertEqual(by_skill[skill]["status"], "ok", by_skill[skill].get("reason"))
                child = json.loads((work / f"{skill}.json").read_text(encoding="utf-8"))
                self.assertEqual(child["mode"], "single")
                self.assertIsNone(child["inputs"]["cand"])
            overview = by_skill["kptools-ascend-profiling-compare"]
            self.assertEqual(overview["status"], "skipped")
            self.assertEqual(overview["reason"], "needs_two_captures")

    def test_build_cmd_never_includes_none_cand(self):
        both = next(p for p in hub.PROVIDERS if p["mode"] == "both")
        cmd = hub._build_cmd(Path("/x"), both, "/base", None, {}, Path("/j"), Path("/h"))
        self.assertNotIn(None, cmd)
        self.assertIn("/base", cmd)

    def test_steps_with_groups_runs_on_pair_input(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            out_json = d / "r.json"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(d / "r.html"), "a", "b",
                           "--base-steps", "0,1", "--cand-steps", "5,6"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            total = len(hub.PROVIDERS)
            self.assertIn(f"provider {total}/{total}", " ".join(env["notes"]))
            html = (d / "r.html").read_text(encoding="utf-8")
            self.assertIn("使用 base 采集", html)

    def test_steps_with_groups_runs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            skills = d / "skills"
            all_stubs(skills)
            out_json = d / "r.json"
            run = run_hub(["--skills-root", str(skills), "--json", str(out_json),
                           "--out-html", str(d / "r.html"), "only_one",
                           "--base-steps", "0,1", "--cand-steps", "5,6"])
            self.assertEqual(run.returncode, 0, run.stderr)
            env = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertFalse(any("needs_step_groups" in x for x in env["data_quality"]["degraded"]))


class SkillIntegrityTests(unittest.TestCase):
    def test_frontmatter_and_evals(self):
        import re

        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(fm)
        self.assertIn(f"name: {SKILL.name}", fm.group(1))
        self.assertRegex(fm.group(1), r"description:\s*\S")
        evals = json.loads((SKILL / "evals/eval_queries.json").read_text(encoding="utf-8"))
        qs = evals["queries"]
        self.assertGreaterEqual(len(qs), 10)
        self.assertGreaterEqual(sum(1 for q in qs if q["should_trigger"]), 6)
        self.assertGreaterEqual(sum(1 for q in qs if not q["should_trigger"]), 4)
        self.assertTrue((SKILL / "assets/report.css").is_file())


if __name__ == "__main__":
    unittest.main()
