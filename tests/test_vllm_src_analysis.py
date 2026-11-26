"""kptools-vllm-src-analysis 的合成 git 仓库契约测试。

不依赖真实 vllm 仓库、网络或 NPU：用临时 git 仓库构造 4 个 tag，
验证版本登记、四态时间线、源码锚点、release notes 与提交归类。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "Plugins/app-tuner/skills/kptools-vllm-src-analysis/scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


versions = _load("versions")
timeline = _load("timeline")
anchors = _load("anchors")
release_notes = _load("release_notes")
digest = _load("digest")


def _git(repo, *args, date="2026-01-01T00:00:00"):
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.com",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.com",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def _write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _matrix(pairs):
    lines = [
        "# Versioning Policy",
        "",
        "| vLLM Ascend | vLLM | Python | Stable CANN | PyTorch/torch_npu | Triton Ascend | Mooncake |",
        "|---|---|---|---|---|---|---|",
    ]
    for version, vllm in pairs:
        lines.append(f"| {version} | {vllm} | >= 3.10 | 9.2.0 | 2.10.0 / 2.10.0 | 3.2.2 | N/A |")
    return "\n".join(lines) + "\n"


def _notes(entries):
    lines = ["# Release Notes", ""]
    for version, date, sections in entries:
        lines.append(f"## {version} - {date}")
        lines.append("")
        for name, bullets in sections.items():
            lines.append(f"### {name}")
            lines.append("")
            for bullet in bullets:
                lines.append(f"- {bullet}")
            lines.append("")
    return "\n".join(lines) + "\n"


def make_repo(base):
    repo = Path(base) / "vllm-ascend"
    repo.mkdir()
    _git(repo, "init", "-q")

    _write(repo, "vllm_ascend/worker/model_runner_v1.py",
           "class NPUModelRunner:\n    def execute_model(self):\n        pass\n")
    _write(repo, "vllm_ascend/worker/worker.py",
           "class Worker:\n    def busy_loop(self):\n        pass\n")
    _write(repo, "vllm_ascend/envs.py", "# model runner v2 not available yet\n")
    _write(repo, "docs/source/community/versioning_policy.md", _matrix([("v0.1.0rc1", "v0.1.0")]))
    _write(repo, "docs/source/user_guide/release_notes.md", _notes([
        ("v0.1.0rc1", "2026.01.01", {"Features": ["Initial release. [#1](https://x/pull/1)"]}),
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init", date="2026-01-01T00:00:00")
    _git(repo, "tag", "v0.1.0rc1")

    _write(repo, "vllm_ascend/worker/v2/model_runner.py",
           "class NPUModelRunner:\n    def execute_model(self):\n        pass\n\n    def sample(self):\n        pass\n")
    _write(repo, "vllm_ascend/envs.py", "VLLM_USE_V2_MODEL_RUNNER = False\n")
    _write(repo, "docs/source/community/versioning_policy.md",
           _matrix([("v0.1.0rc1", "v0.1.0"), ("v0.2.0rc1", "v0.2.0")]))
    _write(repo, "docs/source/user_guide/release_notes.md", _notes([
        ("v0.1.0rc1", "2026.01.01", {"Features": ["Initial release. [#1](https://x/pull/1)"]}),
        ("v0.2.0rc1", "2026.02.01", {
            "Features": ["Added Model Runner V2 experimental path. [#2](https://x/pull/2)"],
            "Performance": ["Improved MoE NPU throughput. [#3](https://x/pull/3)"],
        }),
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add model runner v2 and moe kernel for npu",
         date="2026-02-01T00:00:00")
    _git(repo, "tag", "v0.2.0rc1")

    _write(repo, "vllm_ascend/envs.py", "VLLM_USE_V2_MODEL_RUNNER = True\n")
    _write(repo, "docs/source/community/versioning_policy.md",
           _matrix([("v0.1.0rc1", "v0.1.0"), ("v0.2.0rc1", "v0.2.0"), ("v0.3.0rc1", "v0.3.0")]))
    _write(repo, "docs/source/user_guide/release_notes.md", _notes([
        ("v0.1.0rc1", "2026.01.01", {"Features": ["Initial release. [#1](https://x/pull/1)"]}),
        ("v0.2.0rc1", "2026.02.01", {"Features": ["Added Model Runner V2. [#2](https://x/pull/2)"]}),
        ("v0.3.0rc1", "2026.03.01", {"Performance": ["Enabled V2 by default. [#4](https://x/pull/4)"]}),
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "perf: enable v2 model runner default and improve throughput",
         date="2026-03-01T00:00:00")
    _git(repo, "tag", "v0.3.0rc1")

    (repo / "vllm_ascend/worker/v2/model_runner.py").unlink()
    _write(repo, "vllm_ascend/envs.py", "# model runner v2 removed\n")
    _write(repo, "docs/source/community/versioning_policy.md",
           _matrix([("v0.3.0rc1", "v0.3.0"), ("v0.4.0rc1", "v0.4.0")]))
    _write(repo, "docs/source/user_guide/release_notes.md", _notes([
        ("v0.3.0rc1", "2026.03.01", {"Performance": ["Enabled V2 by default. [#4](https://x/pull/4)"]}),
        ("v0.4.0rc1", "2026.04.01", {"Deprecation": ["Removed experimental V2. [#5](https://x/pull/5)"]}),
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "refactor: remove v2 model runner", date="2026-04-01T00:00:00")
    _git(repo, "tag", "v0.4.0rc1")
    return repo


class VllmSrcAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vllm-src-fixture-")
        cls.repo = make_repo(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_versions_registry(self):
        result = versions.collect(self.repo)
        rows = {r["version"]: r for r in result["versions"]}
        self.assertEqual(list(rows), ["v0.1.0rc1", "v0.2.0rc1", "v0.3.0rc1", "v0.4.0rc1"])
        self.assertEqual(
            [rows[v]["model_runner"]["v2"] for v in rows], [False, True, True, False]
        )
        self.assertEqual(rows["v0.2.0rc1"]["paired_vllm_ref"], "v0.2.0")
        self.assertEqual(rows["v0.2.0rc1"]["pairing_basis"], "versioning_policy matrix")
        self.assertEqual(rows["v0.3.0rc1"]["compat"]["cann"], "9.2.0")

    def test_versions_online_gap(self):
        missing = versions.diff_online(
            ["v0.1.0rc1", "v0.2.0rc1"], ["v0.2.0rc1", "v0.30.0"]
        )
        self.assertEqual(missing, ["v0.30.0"])

    def test_versions_deterministic(self):
        self.assertEqual(versions.collect(self.repo), versions.collect(self.repo))

    def test_versions_error(self):
        with self.assertRaises(RuntimeError):
            versions.collect(str(Path(self.tmp) / "does-not-exist"))

    def test_timeline_path(self):
        result = timeline.analyze(self.repo, path="vllm_ascend/worker/v2/model_runner.py")
        states = result["states"]
        self.assertEqual(states["first_release"], "v0.2.0rc1")
        self.assertEqual(states["removed"], "v0.4.0rc1")
        self.assertEqual(result["confidence"], "high")
        self.assertIsNotNone(states["introduced_code"])

    def test_post_release_tag_ordering(self):
        self.assertGreater(versions.semkey("v0.23.0.post1"), versions.semkey("v0.23.0"))
        self.assertLess(versions.semkey("v0.23.0rc1"), versions.semkey("v0.23.0"))
        self.assertIsNotNone(timeline.semkey("v0.23.0.post2"))

    def test_invalid_default_regex_raises(self):
        with self.assertRaises(RuntimeError):
            timeline.analyze(
                self.repo, symbol="VLLM_USE_V2_MODEL_RUNNER",
                scope="vllm_ascend", default_regex="(",
            )

    def test_timeline_symbol_default(self):
        result = timeline.analyze(
            self.repo,
            symbol="VLLM_USE_V2_MODEL_RUNNER",
            scope="vllm_ascend",
            default_regex=r"VLLM_USE_V2_MODEL_RUNNER[[:space:]]*=[[:space:]]*True",
        )
        states = result["states"]
        self.assertEqual(states["first_release"], "v0.2.0rc1")
        self.assertEqual(states["default_on"], "v0.3.0rc1")
        self.assertEqual(states["removed"], "v0.4.0rc1")

    def test_anchors_scan(self):
        result = anchors.scan(self.repo, "v0.2.0rc1")
        paths = {f["path"]: f for f in result["files"]}
        target = paths["vllm_ascend/worker/v2/model_runner.py"]
        self.assertEqual(target["classes"][0]["name"], "NPUModelRunner")
        self.assertEqual(target["classes"][0]["line"], 1)
        self.assertEqual(target["classes"][0]["methods"][0]["name"], "execute_model")
        self.assertEqual(target["classes"][0]["methods"][0]["line"], 2)

    def test_anchors_diff(self):
        base = anchors.scan(self.repo, "v0.1.0rc1")
        cand = anchors.scan(self.repo, "v0.2.0rc1")
        diff = anchors.diff_scans(base, cand)
        self.assertIn("vllm_ascend/worker/v2/model_runner.py", diff["added_files"])
        names = {(s["kind"], s["name"]) for s in diff["added_symbols"]}
        self.assertIn(("class", "NPUModelRunner"), names)

    def test_release_notes_sections(self):
        result = release_notes.load(self.repo, "v0.3.0rc1", ["Performance"])
        self.assertTrue(result["found"])
        self.assertEqual(result["matched_by"], "exact")
        self.assertEqual([s["name"] for s in result["sections"]], ["Performance"])
        bullet = result["sections"][0]["bullets"][0]
        self.assertEqual(bullet["prs"][0]["pr"], "4")
        self.assertEqual(result["evidence_level"], "claimed_in_release_notes")

    def test_release_notes_missing(self):
        result = release_notes.load(self.repo, "v9.9.9rc1")
        self.assertFalse(result["found"])
        self.assertIn("v0.3.0rc1", result["available_versions"])

    def test_release_notes_full_version_not_prefix_matched(self):
        result = release_notes.load(self.repo, "v0.3.0")
        self.assertFalse(result["found"])
        self.assertTrue(any("--online" in d for d in result["degradations"]))

        partial = release_notes.load(self.repo, "v0.3")
        self.assertTrue(partial["found"])
        self.assertEqual(partial["version"], "v0.3.0rc1")
        self.assertEqual(partial["matched_by"], "prefix")

    def test_release_notes_parse_chinese_blockquote(self):
        text = (
            "# Release Notes\n\n## v0.30.0 - 2026.10.29 ¶\n\n"
            "> **注意：** 本版本仍默认使用 v1。\n"
            "> 计划在下个版本全面切换。\n\n"
            "### 性能 ¶\n\n- 优化了 X。 [#9](https://x/pull/9)\n\n"
            "### 已知问题 ¶\n\n- 默认仍使用 MRV1。\n"
        )
        result = release_notes.parse_notes(text, "v0.30.0", ["性能"])
        self.assertTrue(result["found"])
        self.assertEqual(result["version"], "v0.30.0")
        self.assertEqual(result["date"], "2026.10.29")
        self.assertEqual([s["name"] for s in result["sections"]], ["Performance"])
        self.assertIn("仍默认使用 v1", result["notes"][0])
        self.assertIn("全面切换", result["notes"][1])
        self.assertEqual(result["sections"][0]["bullets"][0]["prs"][0]["pr"], "9")

    def test_release_notes_sections_fuzzy_and_empty(self):
        text = (
            "## v0.30.0 - 2026.10.29\n\n"
            "### Deprecation and Configuration Changes\n\n"
            "- MRV1 will be removed next version. [#7](https://x/pull/7)\n\n"
            "### Performance\n\n- faster. [#8](https://x/pull/8)\n"
        )
        by_alias = release_notes.parse_notes(text, "v0.30.0", ["Deprecation"])
        self.assertTrue(by_alias["found"])
        self.assertEqual([s["name"] for s in by_alias["sections"]], ["Deprecation"])

        drifted = text.replace(
            "Deprecation and Configuration Changes", "Deprecation & Breaking Changes"
        )
        by_drift = release_notes.parse_notes(drifted, "v0.30.0", ["Deprecation"])
        self.assertEqual(len(by_drift["sections"]), 1)

        empty = release_notes.parse_notes(text, "v0.30.0", ["Highlights"])
        self.assertEqual(empty["sections"], [])
        self.assertTrue(any("未命中" in d for d in empty["degradations"]))

    def test_release_notes_online_file_url(self):
        path = Path(self.tmp) / "online.md"
        path.write_text(
            "## v0.30.0 - 2026.10.29\n\n> Notice: defaults to v1.\n\n"
            "### Performance\n\n- faster. [#1](https://x/pull/1)\n",
            encoding="utf-8",
        )
        result = release_notes.load(
            None, "v0.30.0", ["Performance"], online=True, url=path.as_uri()
        )
        self.assertEqual(result["source_kind"], "online")
        self.assertTrue(result["found"])
        self.assertEqual(result["counts"]["bullets"], 1)
        self.assertIn("defaults to v1", result["notes"][0])

    def test_digest(self):
        result = digest.digest(self.repo, "v0.1.0rc1", "v0.3.0rc1")
        self.assertGreaterEqual(result["total_commits"], 2)
        subs = result["summary"]["by_subsystem"]
        self.assertIn("model_runner_v2", subs)
        self.assertIn("moe", subs)
        self.assertIn("throughput", result["summary"]["by_perf_dim"])
        self.assertEqual(result["analysis_level"], "inferred_from_commit_subject")

    def test_json_output_contract(self):
        out = Path(self.tmp) / "versions.json"
        rc = versions.main(["--ascend-repo", str(self.repo), "--json", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(data["versions"]), 4)


if __name__ == "__main__":
    unittest.main()
