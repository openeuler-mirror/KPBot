#!/usr/bin/env python3
"""Check the installed replay tools on a disposable checkout."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TRACE = "example__issue-1.replay.json"
TOKEN = "private-fixture-value"


class BenchmarkCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def command(self, *args, ok=True, env=None):
        run = subprocess.run(
            args, cwd=self.root, env=env, capture_output=True, text=True
        )
        self.assertEqual(run.returncode == 0, ok, run.stderr or run.stdout)
        return run

    def plan(self, *args, **kwargs):
        return self.command(
            sys.executable, SCRIPTS / "plan_oversub.py", *args, **kwargs
        )

    def checkout(self):
        self.command("git", "init", "-q")
        traces = self.root / "artifacts/traces/replays"
        traces.mkdir(parents=True)
        payload = json.dumps({"trajectory": [{"action": "echo ok"}]})
        (traces / TRACE).write_text(payload)
        for i in range(54):
            (traces / f"fixture-{i}.replay.json").write_text(payload)
        (traces.parent / "image-map.json").write_text(
            json.dumps(
                {p.name: "template-" + p.stem for p in traces.iterdir()}
            )
        )
        presets = self.root / "config/common"
        presets.mkdir(parents=True)
        base = {
            "workflow_type": "replay",
            "aenv": {"env": {"E2B_API_KEY": TOKEN}},
            "sandbox": {},
            "replay": {},
            "test": {},
            "report": {},
        }
        for name in ("replay", "replay-ephemeral"):
            (presets / f"{name}.yaml").write_text(yaml.safe_dump(base))
        self.command("git", "add", ".")
        self.command(
            "git",
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "preset fixtures",
        )

    def prepare(self, *args, ok=True):
        return self.command(
            sys.executable,
            SCRIPTS / "prepare_replay.py",
            "--api-url",
            "http://control.invalid",
            "--mem-total-gib",
            "1536",
            *args,
            ok=ok,
        )

    def test_fixed_replay_smoke_and_lifecycle(self):
        self.checkout()
        (self.root / "config/common/replay.yaml").write_text(
            "uncommitted edit"
        )
        (self.root / "artifacts/traces/replays/extra.replay.json").write_text(
            "{}"
        )
        endpoint = ("--sandbox-url", "http://data.invalid")
        folder = Path(self.prepare(*endpoint).stdout.strip())
        smoke = Path(
            self.prepare("--smoke-trace", TRACE, *endpoint).stdout.strip()
        )
        self.assertEqual(len(list((folder / "replays").iterdir())), 55)
        self.assertEqual(
            (folder / "benchmark-commit.txt").read_text().strip(),
            self.command("git", "rev-parse", "HEAD").stdout.strip(),
        )
        for name, count, delay in (
            ("full", 384, 1),
            ("smoke", 1, 0.02),
            ("lifecycle-base", 384, 1),
        ):
            directory = smoke if name == "smoke" else folder
            text = (directory / f"{name}.yaml").read_text()
            self.assertNotIn(TOKEN, text)
            config = yaml.safe_load(text)
            replay = config["replay"]
            self.assertEqual(
                config["aenv"]["env"]["E2B_DOMAIN"], "data.invalid"
            )
            self.assertEqual(config["sandbox"]["total_count"], count)
            self.assertEqual(config["test"]["round_size"], count)
            self.assertEqual(
                (replay["running_concurrency"], replay["delay_scale"]),
                (count, delay),
            )
            self.assertTrue(Path(replay["template_manifest"]).is_file())
            if name == "lifecycle-base":
                self.assertEqual(replay["mode"], "lifecycle")
            else:
                self.assertEqual(
                    (
                        replay["mode"],
                        replay["multiplier"],
                        config["test"]["benchmark_mode"],
                    ),
                    ("exec_ephemeral", 1, "fixed"),
                )
            if name == "smoke":
                self.assertEqual(
                    list(
                        json.loads(
                            Path(replay["template_manifest"]).read_text()
                        )
                    ),
                    [TRACE],
                )
        self.assertEqual(
            (self.root / "config/common/replay.yaml").read_text(),
            "uncommitted edit",
        )

    def test_incomplete_dataset_accepts_only_selected_smoke(self):
        self.checkout()
        (self.root / "artifacts/traces/replays/fixture-0.replay.json").unlink()
        self.prepare(ok=False)
        self.assertEqual(list(self.root.glob("agent-benchmark.*")), [])
        smoke = Path(self.prepare("--smoke-trace", TRACE).stdout.strip())
        self.assertEqual(
            [p.name for p in smoke.glob("*.yaml")], ["smoke.yaml"]
        )

    def test_unsafe_or_missing_trace_creates_no_run(self):
        self.checkout()
        for trace in (
            "../outside.replay.json",
            "*.replay.json",
            "missing.replay.json",
        ):
            self.prepare("--smoke-trace", trace, ok=False)
        self.assertEqual(list(self.root.glob("agent-benchmark.*")), [])

    def test_unreadable_or_empty_selected_replay_cleans_up(self):
        self.checkout()
        source = self.root / "artifacts/traces/replays" / TRACE
        for text in ("not JSON", "{}", '{"trajectory":[{"action":"submit"}]}'):
            source.write_text(text)
            self.prepare("--smoke-trace", TRACE, ok=False)
            self.assertEqual(list(self.root.glob("agent-benchmark.*")), [])

    def test_bad_template_map_creates_no_run(self):
        self.checkout()
        manifest = self.root / "artifacts/traces/image-map.json"
        for value in (
            "invalid JSON",
            "{}",
            "[]",
            *[json.dumps({TRACE: alias}) for alias in (None, 42, "", "   ")],
            json.dumps({"other.replay.json": "valid"}),
        ):
            with self.subTest(value=value):
                manifest.write_text(value)
                self.prepare("--smoke-trace", TRACE, ok=False)
                self.assertEqual(list(self.root.glob("agent-benchmark.*")), [])

    def test_case_list_and_custom_directory_use_memory_sized_fleet(self):
        self.checkout()
        dataset = self.root / "custom suite"
        dataset.mkdir()
        names = [f"case-{i}.replay.json" for i in range(5)]
        for name in names:
            (dataset / name).write_text(
                json.dumps({"trajectory": [{"action": "echo ok"}]})
            )
        manifest = dataset / "templates.json"
        manifest.write_text(json.dumps({name: "shared" for name in names}))
        subset = self.root / "cases.txt"
        subset.write_text("# selected cases\n\n" + "\n".join(names[1:3]))
        args = (
            "--trajectory-dir",
            dataset,
            "--template-manifest",
            manifest,
            "--ratio",
            "2",
        )
        for expected, extra in (
            (names, ()),
            (names[1:3], ("--case-list", subset)),
        ):
            run = Path(self.prepare(*args, *extra).stdout.strip())
            prepared = json.loads((run / "plan.json").read_text())
            self.assertEqual(prepared["case_count"], len(expected))
            self.assertEqual(prepared["trials"][0]["sandbox_count"], 768)
            for label in ("full", "lifecycle-base"):
                c = yaml.safe_load((run / f"{label}.yaml").read_text())
                self.assertEqual(
                    (c["sandbox"]["total_count"], c["test"]["round_size"]),
                    (768, 768),
                )
                self.assertEqual(
                    set(
                        json.loads(
                            Path(c["replay"]["template_manifest"]).read_text()
                        )
                    ),
                    set(expected),
                )
                self.assertEqual(
                    {
                        p.name
                        for p in Path(c["replay"]["trajectory_dir"]).glob(
                            "*.replay.json"
                        )
                    },
                    set(expected),
                )
            sweep = run / "sweep"
            self.plan(
                "--mem-total-gib",
                "4",
                "1",
                "3",
                "--base-config",
                run / "lifecycle-base.yaml",
                "--output-dir",
                sweep,
            )
            for n in (1, 3):
                c = yaml.safe_load(
                    (sweep / f"1-to-{n}/trial.yaml").read_text()
                )
                self.assertEqual(c["sandbox"]["total_count"], n)
                self.assertNotIn("trajectory_limit", c["replay"])
                self.assertEqual(
                    len(
                        json.loads(
                            Path(c["replay"]["template_manifest"]).read_text()
                        )
                    ),
                    len(expected),
                )

    def test_empty_duplicate_and_missing_selection(self):
        self.checkout()
        subset = self.root / "cases.txt"
        for content in ("# none", TRACE + "\n" + TRACE, "missing.replay.json"):
            subset.write_text(content)
            self.prepare("--case-list", subset, ok=False)
            self.assertEqual(list(self.root.glob("agent-benchmark.*")), [])

    def test_capacity_counts(self):
        plan = json.loads(
            self.plan("--mem-total-gib", "1536", "1", "2", "3").stdout
        )
        self.assertEqual(
            [trial["sandbox_count"] for trial in plan["trials"]],
            [384, 768, 1152],
        )
        plan = json.loads(
            self.plan("--mem-total-kib", "1578474656", "3").stdout
        )
        self.assertEqual(plan["baseline_count"], 376)
        self.assertEqual(
            plan["trials"][0],
            {"ratio": 3, "sandbox_count": 1129, "native_count": 1128},
        )

    def test_trial_files_match_plan_and_isolate_instance_ids(self):
        self.checkout()
        run = Path(self.prepare().stdout.strip())
        for name, slots in (("lifecycle-base", 1152), ("full", 384)):
            sweep = run / name
            plan = json.loads(
                self.plan(
                    "--mem-total-gib",
                    "1536",
                    "1",
                    "3",
                    "--base-config",
                    run / f"{name}.yaml",
                    "--output-dir",
                    sweep,
                ).stdout
            )
            self.assertEqual(
                plan, json.loads((sweep / "plan.json").read_text())
            )
            for trial in plan["trials"]:
                config = yaml.safe_load(Path(trial["config"]).read_text())
                self.assertEqual(
                    config["sandbox"]["total_count"], trial["sandbox_count"]
                )
                self.assertEqual(
                    config["test"]["round_size"], trial["sandbox_count"]
                )
                self.assertEqual(
                    config["replay"]["running_concurrency"],
                    slots if trial["ratio"] == 3 else 384,
                )
                self.assertNotIn("trajectory_limit", config["replay"])
                self.assertEqual(
                    config["report"]["output_dir"], trial["results_dir"]
                )
                self.assertEqual(
                    Path(config["aenv"]["sandbox_ids_file"]).parent,
                    Path(trial["config"]).parent,
                )

    def test_refuses_overwrite_redirection_and_inline_credentials(self):
        base = self.root / "base.yaml"
        base.write_text("workflow_type: replay\n")
        args = (
            "1",
            "--mem-total-gib",
            "8",
            "--base-config",
            base,
            "--output-dir",
        )
        keep = self.root / "existing"
        keep.mkdir()
        (keep / "plan.json").write_text("keep")
        self.plan(*args, keep, ok=False)
        self.assertEqual((keep / "plan.json").read_text(), "keep")
        link = self.root / "new-link"
        link.symlink_to(self.root / "outside")
        self.plan(*args, link, ok=False)
        self.assertFalse((self.root / "outside").exists())
        base.write_text(
            yaml.safe_dump(
                {
                    "workflow_type": "replay",
                    "aenv": {"env": {"E2B_API_KEY": TOKEN}},
                }
            )
        )
        output = self.plan(*args, self.root / "invalid", ok=False)
        self.assertNotIn(TOKEN, output.stdout + output.stderr)
        self.assertFalse((self.root / "invalid").exists())

    def test_host_probe_receives_no_driver_credentials(self):
        probe = self.root / "dmidecode"
        probe.write_text(
            '#!/bin/sh\n'
            '[ -z "${E2B_API_KEY+x}" ] || exit 1\n'
            'printf "Size: 16 GB\\n"\n'
        )
        probe.chmod(0o755)
        env = dict(
            os.environ,
            PATH=str(self.root) + os.pathsep + os.environ["PATH"],
            E2B_API_KEY=TOKEN,
        )
        plan = json.loads(self.plan("1", env=env).stdout)
        self.assertEqual(
            (plan["memory_source"], plan["baseline_count"]),
            ("dmidecode:type17", 4),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
