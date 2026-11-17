"""Regression coverage for PR 138: report correctness and evaluation isolation."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CASE = ROOT / "cases/agent-offline-boundary"
sys.path.insert(0, str(SCRIPTS))
import private_io
import session_view


def load(name):
    spec = importlib.util.spec_from_file_location(name, CASE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def supervisor():
    with patch.dict(sys.modules, {"seed": load("seed"), "verify": load("verify")}):
        return load("supervisor")


def container_test_driver():
    spec = importlib.util.spec_from_file_location(
        "container_tests", Path(__file__).with_name("run_container_tests.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReportReviewTests(unittest.TestCase):
    def test_total_and_comparable_denominators(self):
        requests = [{"tok": 600, "tok_attr": True, "kind": "first", "tok_new": None},
                    {"tok": 400, "tok_attr": True, "kind": "append_user", "tok_new": 100,
                     "template_lost": 40},
                    {"tok": 1000, "tok_attr": True, "kind": "first", "tok_new": None}]
        stats = session_view.fleet_stats([{"id": "s", "n_req": 3, "reqs": requests}])
        self.assertEqual(stats["tok_total"], 2000)
        self.assertEqual(stats["new_tot"], 400)
        self.assertEqual(stats["new_share"], 0.25)
        self.assertEqual(stats["template_lost_share"], 0.1)
        self.assertEqual(stats["template_lost_share_total"], 0.02)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            session_view.print_fleet(stats)
        self.assertIn("全部 token 的 2.0%", output.getvalue())
        self.assertNotIn("全部 token 的 10.0%", output.getvalue())
        self.assertIsNone(session_view.fleet_stats([])["template_lost_share_total"])

    def test_message_markdown_table_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            load("seed").seed(root, SCRIPTS)
            for mode in ("new", "full", "none"):
                out = root / mode
                run = subprocess.run([sys.executable, str(SCRIPTS / "session_view.py"),
                                      str(root / "input/data"), "--out", str(out), "--md", mode],
                                     capture_output=True, timeout=20)
                self.assertEqual(run.returncode, 0, run.stderr)
                tables = 0
                for document in out.glob("session_*.md"):
                    lines = document.read_text().splitlines()
                    for index, line in enumerate(lines):
                        if line.startswith("| # | role | tool |"):
                            widths = [len(re.split(r"(?<!\\)\|", lines[index + offset])) - 2
                                      for offset in (0, 1, 2)]
                            self.assertEqual(widths, [10, 10, 10])
                            tables += 1
                self.assertEqual(tables, 4)

    def test_private_outputs_reject_public_dirs_links_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "reports"
            private_io.prepare_output_dir(out)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
            artifact = out / "report.html"
            with private_io.private_open(artifact) as stream:
                stream.write("private")
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                private_io.private_open(artifact)
            victim = root / "victim"
            victim.write_text("unchanged")
            (out / "linked.html").symlink_to(victim)
            with self.assertRaises(OSError):
                private_io.private_open(out / "linked.html")
            self.assertEqual(victim.read_text(), "unchanged")
            linked = root / "linked-dir"
            linked.symlink_to(out, target_is_directory=True)
            with self.assertRaises(OSError):
                private_io.prepare_output_dir(linked)
            os.chmod(out, 0o755)
            with self.assertRaises(PermissionError):
                private_io.prepare_output_dir(out)
            os.chmod(out, 0o700)
            with patch.object(private_io.os, "geteuid", return_value=out.stat().st_uid + 1):
                with self.assertRaises(PermissionError):
                    private_io.prepare_output_dir(out)

    def test_all_analysis_artifacts_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            load("seed").seed(root, SCRIPTS)
            for name in ("analyze", "session_view"):
                out = root / name
                args = [sys.executable, str(SCRIPTS / (name + ".py")), str(root / "input/data"), "--out", str(out)]
                if name == "session_view":
                    args.append("--include-text")
                run = subprocess.run(args, capture_output=True, timeout=20,
                                     preexec_fn=lambda: os.umask(0))
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
                for artifact in out.iterdir():
                    self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600, str(artifact))
            log = root / "empty.log"
            log.write_text("")
            joined = root / "joined"
            run = subprocess.run([sys.executable, str(SCRIPTS / "join_vllm_log.py"),
                                  "--requests", str(root / "analyze/requests.csv"), "--p-log", str(log),
                                  "--out", str(joined)], capture_output=True, timeout=20,
                                 preexec_fn=lambda: os.umask(0))
            self.assertEqual(run.returncode, 0, run.stderr)
            for artifact in joined.iterdir():
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600, str(artifact))
            corpus = root / "corpus.jsonl"
            run = subprocess.run([sys.executable, str(SCRIPTS / "export_corpus.py"),
                                  str(root / "input/data"), "--format", "requests", "--out", str(corpus)],
                                 capture_output=True, timeout=20, preexec_fn=lambda: os.umask(0))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(stat.S_IMODE(corpus.stat().st_mode), 0o600)


class AgentReviewTests(unittest.TestCase):
    def test_shared_container_security_includes_home_ownership_capability(self):
        args = container_test_driver().security_args()
        self.assertIn("--cap-drop=ALL", args)
        self.assertIn("--cap-add=CHOWN", args)
        self.assertIn("--security-opt=no-new-privileges", args)
        self.assertNotIn("--privileged", args)

    def test_policy_has_no_unrestricted_shell_or_credential_read(self):
        policy = supervisor().permissions()["permission"]
        self.assertEqual(policy["bash"]["*"], "deny")
        self.assertEqual(policy["read"]["*/auth.json"], "deny")
        self.assertEqual(policy["external_directory"], "deny")
        self.assertEqual(policy["webfetch"], "deny")
        allowed = [command for command, value in policy["bash"].items() if value == "allow"]
        self.assertEqual(len(allowed), 2)
        for command in allowed:
            self.assertNotIn("*", command)
            self.assertNotIn(";", command)
            self.assertTrue(command.endswith(("/workspace/opt/analysis", "/workspace/opt/sessions")))
            self.assertIn("/workspace/tools/scripts/", command)
            self.assertNotIn(".opencode/skills", command)

    def test_snapshot_rejects_symlinks_hardlinks_and_special_files(self):
        module = supervisor()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "opt"
            source.mkdir()
            (source / "report").write_text("valid")
            module.snapshot_file(source, Path("report"), root / "accepted", os.getuid())
            self.assertEqual((root / "accepted").read_text(), "valid")
            (source / "link").symlink_to(root / "accepted")
            with self.assertRaises(OSError):
                module.snapshot_file(source, Path("link"), root / "bad", os.getuid())
            (source / "dir").symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                module.snapshot_file(source, Path("dir/accepted"), root / "bad", os.getuid())
            os.link(source / "report", source / "hard")
            with self.assertRaises(ValueError):
                module.snapshot_file(source, Path("hard"), root / "bad", os.getuid())
            os.mkfifo(source / "fifo")
            with self.assertRaises(ValueError):
                module.snapshot_file(source, Path("fifo"), root / "bad", os.getuid())

    def test_stale_passing_result_is_removed_before_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "results").mkdir()
            result = root / "results/result.json"
            result.write_text('{"success": true}')
            (root / "baseline.json").write_text("invalid")
            with self.assertRaises(ValueError):
                load("verify").verify(root)
            self.assertFalse(result.exists())

    def test_runner_rejects_untrusted_paths_hash_and_docker_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / ".local/bin/opencode"
            auth = root / ".local/share/opencode/auth.json"
            binary.parent.mkdir(parents=True)
            auth.parent.mkdir(parents=True)
            binary.write_bytes(b"\x7fELFfixture")
            binary.chmod(0o700)
            auth.write_text("{}")
            env = {**os.environ, "HOME": str(root), "OPENCODE_BIN": str(binary),
                   "OPENCODE_AUTH": str(auth), "DOCKER_RUN_ARGS": ""}
            overrides = ({"OPENCODE_BIN": str(root / "missing")},
                         {"OPENCODE_BIN": str(auth)}, {"OPENCODE_AUTH": str(binary)},
                         {"OPENCODE_SHA256": "wrong"}, {"DOCKER_RUN_ARGS": "--privileged"},
                         {"DOCKER_RUN_ARGS": "--volume=/tmp:/host"},
                         {"DOCKER_RUN_ARGS": "--network=host"},
                         {"DOCKER_RUN_ARGS": "--cpus=1 --entrypoint=sh"})
            for override in overrides:
                with self.subTest(override=override):
                    out = root / "output"
                    run = subprocess.run(["bash", str(CASE / "run_ab.sh"), "unused", str(out)],
                                         env={**env, **override}, capture_output=True, timeout=10)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertFalse(out.exists())

    def test_runner_exports_results_without_host_write_mount_and_propagates_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / ".local/bin/opencode"
            auth = root / ".local/share/opencode/auth.json"
            shim = root / "shims"
            for parent in (binary.parent, auth.parent, shim):
                parent.mkdir(parents=True)
            binary.write_bytes(b"\x7fELFfixture")
            binary.chmod(0o700)
            auth.write_text("{}")
            log = root / "docker.jsonl"
            docker = shim / "docker"
            docker.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['FAKE_LOG'],'a') as out: out.write(json.dumps(args)+'\\n')
if args[0]=='wait': print(os.environ.get('FAKE_EXIT','0'))
if args[0]=='cp':
    Path(args[-1],'result.json').write_text('{"success":true}')
''')
            docker.chmod(0o700)
            env = {**os.environ, "HOME": str(root), "OPENCODE_BIN": str(binary),
                   "OPENCODE_AUTH": str(auth), "DOCKER_RUN_ARGS": "--network=none --cpus=1",
                   "OPENCODE_SHA256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                   "FAKE_LOG": str(log), "PATH": str(shim) + os.pathsep + os.environ["PATH"]}
            for exit_code in ("0", "1"):
                run = subprocess.run(["bash", str(CASE / "run_ab.sh"), "fixture-image", str(root / exit_code)],
                                     env={**env, "FAKE_EXIT": exit_code}, capture_output=True, timeout=15)
                self.assertEqual(run.returncode, int(exit_code), run.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            creates = [call for call in calls if call[0] == "create"]
            self.assertEqual(len(creates), 4)
            for call in creates:
                security = container_test_driver().security_args()
                start = call.index("--cap-drop=ALL")
                self.assertEqual(call[start:start + len(security)], security)
                mounts = [call[index + 1] for index, arg in enumerate(call) if arg == "--mount"]
                self.assertEqual(len(mounts), 2)
                self.assertTrue(all(value.endswith(",readonly") for value in mounts))
                self.assertFalse(any("/workspace/results" in value for value in mounts))
                self.assertTrue(any("dst=/run/secrets/opencode-auth.json" in value for value in mounts))
            self.assertEqual(len([call for call in calls if call[0] == "rm"]), 4)

    @unittest.skipUnless(shutil.which("sha256sum"), "requires sha256sum")
    def test_pristine_check_rejects_modified_grader(self):
        module = supervisor()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grader = root / "verify.py"
            grader.write_text("trusted")
            digest = hashlib.sha256(grader.read_bytes()).hexdigest()
            (root / "pristine.sha256").write_text(digest + "  " + str(grader) + "\n")
            with patch.object(module, "HARNESS", root):
                module.pristine()
                grader.write_text("tampered")
                with self.assertRaises(subprocess.CalledProcessError):
                    module.pristine()

    @unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                         "requires a disposable root Linux container")
    def test_supervisor_pipeline_with_unprivileged_stub_client(self):
        module = supervisor()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            harness = root / "harness"
            shutil.copytree(CASE, harness)
            harness.chmod(0o700)
            grader = harness / "verify.py"
            digest = hashlib.sha256(grader.read_bytes()).hexdigest()
            (harness / "pristine.sha256").write_text(digest + "  " + str(grader) + "\n")
            secrets = root / "secrets"
            secrets.mkdir(mode=0o700)
            auth = secrets / "auth.json"
            auth.write_text("{}")
            auth.chmod(0o600)
            client = root / "opencode"
            client.write_text("#!" + sys.executable + "\n" + '''import os,subprocess,sys,json
from pathlib import Path
assert os.getuid() == 65534
r=Path.cwd()
try:
    (r/'results/result.json').write_text('{"success":true}')
except PermissionError:
    pass
else:
    raise SystemExit('agent could forge the result')
subprocess.run([sys.executable, str(r/'tools/scripts/analyze.py'),
                str(r/'input/data'), '--out', str(r/'opt/analysis')], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(r/'opt/REPORT.md').write_text('Synthetic report: four requests, two sessions; tokenizer and logs unavailable.')
mode=sys.argv[sys.argv.index('-m')+1].split('/')[-1]
assert (r/'.opencode/skills/kptools-prompt-capture/SKILL.md').exists() == (mode == 'with')
if mode == 'with':
    print(json.dumps({'type':'tool_use','part':{'tool':'skill','input':{'name':'kptools-prompt-capture'}}}))
''')
            client.chmod(0o555)
            user = types.SimpleNamespace(pw_uid=65534, pw_gid=65534)
            hashes = []
            policies = []
            for mode in ("with", "without"):
                with self.subTest(mode=mode):
                    workspace = root / mode
                    workspace.mkdir(mode=0o755)
                    (workspace / "opt").mkdir(mode=0o700)
                    os.chown(workspace / "opt", 65534, 65534)
                    with patch.multiple(module, ROOT=workspace, HARNESS=harness, STAGING=ROOT,
                                        OPENCODE=str(client), AUTH=auth):
                        with patch.object(module.pwd, "getpwnam", return_value=user), patch.dict(
                                os.environ, {"SKILL_MODE": mode, "MODEL": "mock/" + mode}):
                            self.assertEqual(module.main(), 0)
                    result = json.loads((workspace / "results/result.json").read_text())
                    self.assertTrue(result["success"])
                    self.assertTrue(result["inputs_unchanged"])
                    self.assertEqual(result["skill_assembled"], mode == "with")
                    guidance = workspace / ".opencode/skills/kptools-prompt-capture"
                    for name in ("scripts", "tests", "cases", "evals"):
                        self.assertFalse((guidance / name).exists())
                    self.assertEqual((workspace / "results/result.json").stat().st_uid, 0)
                    self.assertEqual((workspace / "opt/REPORT.md").stat().st_uid, 65534)
                    tools = workspace / "tools"
                    self.assertFalse((tools / "SKILL.md").exists())
                    self.assertFalse((tools / "references").exists())
                    files = [p for p in tools.rglob("*") if p.is_file()]
                    hashes.append({str(p.relative_to(tools)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in files})
                    for tool in files:
                        self.assertEqual(tool.stat().st_uid, 0)
                        self.assertEqual(stat.S_IMODE(tool.stat().st_mode) & 0o222, 0)
                    policies.append((workspace / "opencode.json").read_text())
            self.assertEqual(hashes[0], hashes[1])
            self.assertEqual(policies[0], policies[1])

    @unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                         "requires a disposable root Linux container")
    def test_os_permissions_protect_grader_results_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            grader = root / "verify.py"
            grader.write_text("trusted")
            grader.chmod(0o500)
            results = root / "results"
            results.mkdir(mode=0o700)
            secrets = root / "secrets"
            secrets.mkdir(mode=0o700)
            (secrets / "auth.json").write_text("fake key")
            opt = root / "opt"
            opt.mkdir(mode=0o700)
            os.chown(opt, 65534, 65534)

            def drop():
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)

            code = '''from pathlib import Path
import sys
r=Path(sys.argv[1])
for p,mode in [(r/'verify.py','w'),(r/'results/result.json','w'),(r/'secrets/auth.json','r')]:
    try:
        with p.open(mode): pass
    except PermissionError: continue
    raise SystemExit('protected path was accessible')
(r/'opt/report.md').write_text('allowed')
'''
            run = subprocess.run([sys.executable, "-c", code, str(root)], preexec_fn=drop,
                                 capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(grader.read_text(), "trusted")
            self.assertFalse((results / "result.json").exists())
            self.assertEqual((opt / "report.md").read_text(), "allowed")


if __name__ == "__main__":
    unittest.main()
