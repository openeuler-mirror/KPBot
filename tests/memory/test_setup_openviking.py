"""Development-helper layout and progress tests; no downloads or services."""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools/memory/setup-openviking.py"


def load(path=SCRIPT):
    spec = importlib.util.spec_from_file_location("memory_setup_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenVikingSetupTests(unittest.TestCase):
    def test_actual_checkout_is_accepted_without_writes(self):
        module = load()
        self.assertEqual(module.REPO, REPO)
        self.assertEqual(module.RUNTIME, REPO / ".runtime/openviking")
        self.assertIsNone(module.require_checkout())

    def test_versioned_ignore_protects_fresh_and_existing_runtime_without_helper(self):
        current_ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
        for upgrading in (False, True):
            with self.subTest(upgrading=upgrading), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                # No personal/global ignore rules or helper invocation may mask
                # a missing versioned runtime exclusion in a fresh checkout.
                env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
                for key in tuple(env):
                    if key.startswith("GIT_") and key not in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"):
                        env.pop(key)

                def git(*arguments):
                    return subprocess.run(
                        ["git", "-c", "core.excludesFile=" + os.devnull, *arguments],
                        cwd=root, env=env, check=True, capture_output=True, text=True,
                    )

                git("-c", "init.templateDir=", "init", "--quiet")
                exclude = root / ".git/info/exclude"
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text("# No local exclusions\n", encoding="utf-8")
                ignore = root / ".gitignore"
                if upgrading:
                    # Existing checkout: runtime was created while the old
                    # tracked ignore rule was present; pulling replaces it.
                    ignore.write_text("/.runtime/\n", encoding="utf-8")
                    git("add", ".gitignore")
                artifacts = (
                    ".runtime/openviking/ov.conf",
                    ".runtime/openviking/config-user-01.json",
                    ".runtime/openviking/models/embedding.gguf",
                    ".runtime/openviking/models/embedding.gguf.part",
                    ".runtime/openviking/data/record.json",
                )
                for relative in artifacts:
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("synthetic fixture, not a real credential\n", encoding="utf-8")
                ignore.write_text(current_ignore, encoding="utf-8")
                (root / "source.py").write_text("# source remains visible\n", encoding="utf-8")
                for relative in artifacts:
                    match = git("check-ignore", "-v", "--", relative).stdout
                    self.assertIn(".gitignore:", match)
                    self.assertIn(":/.runtime/\t", match)
                visible = git("status", "--porcelain", "--untracked-files=all").stdout
                self.assertNotIn(".runtime/", visible)
                self.assertIn("source.py", visible)
                git("add", "-A")
                self.assertEqual(
                    set(git("diff", "--cached", "--name-only").stdout.splitlines()),
                    {".gitignore", "source.py"},
                )
                self.assertNotIn("/.runtime/", exclude.read_text(encoding="utf-8"))

    def test_runtime_exclusion_is_local_and_idempotent(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exclude = root / ".git/info/exclude"
            exclude.parent.mkdir(parents=True)
            exclude.write_text("# user's rule\n/custom/", encoding="utf-8")
            result = subprocess.CompletedProcess([], 0, stdout=".git/info/exclude\n")
            with mock.patch.object(module, "REPO", root), \
                    mock.patch.object(module.subprocess, "run", return_value=result):
                module.ensure_local_exclusion()
                module.ensure_local_exclusion()
            content = exclude.read_text(encoding="utf-8")
            self.assertEqual(content.count("/.runtime/"), 1)
            self.assertIn("/custom/\n", content)
            self.assertFalse((root / ".gitignore").exists())

    def test_runtime_exclusion_refuses_symlink_without_overwriting_target(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "owned"
            target.write_text("unchanged")
            exclude = root / ".git/info/exclude"
            exclude.parent.mkdir(parents=True)
            exclude.symlink_to(target)
            result = subprocess.CompletedProcess([], 0, stdout=".git/info/exclude\n")
            with mock.patch.object(module, "REPO", root), \
                    mock.patch.object(module.subprocess, "run", return_value=result):
                with self.assertRaises(module.SetupError):
                    module.ensure_local_exclusion()
            self.assertEqual(target.read_text(), "unchanged")

    def test_installed_layout_refused_before_runtime_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copied = root / "project with spaces/.claude/skills/kpbot-memory/scripts/setup-openviking.py"
            copied.parent.mkdir(parents=True)
            shutil.copyfile(SCRIPT, copied)
            module = load(copied)
            # Checkout markers cannot make an installed skill path acceptable.
            (module.REPO / ".git").mkdir()
            (module.REPO / "install.sh").touch()
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(module.main(["verify"]), 1)
            self.assertIn("source checkout", stderr.getvalue())
            self.assertIn("configure", stderr.getvalue())
            self.assertFalse((module.REPO / ".runtime").exists())
            self.assertEqual(stdout.getvalue(), "")

    def test_progress_reports_elapsed_waiting_and_stops(self):
        module = load()
        waited = module.threading.Event()
        stderr = io.StringIO()
        original_report = module.report

        def report(message):
            original_report(message)
            if "仍在执行" in message:
                waited.set()

        before_threads = set(module.threading.enumerate())
        with mock.patch.object(module, "report", side_effect=report), \
                contextlib.redirect_stderr(stderr):
            with module.progress("安装依赖", interval=0.01):
                self.assertTrue(waited.wait(1), "heartbeat was not emitted")
        self.assertIn("已等待", stderr.getvalue())
        self.assertIn("完成，耗时", stderr.getvalue())
        self.assertEqual(set(module.threading.enumerate()), before_threads)

    def test_failed_stage_does_not_report_success(self):
        module = load()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(module.SetupError):
                with module.progress("核验"):
                    raise module.SetupError("test failure")
        self.assertIn("未完成", stderr.getvalue())
        self.assertNotIn("test failure", stderr.getvalue())

    def test_download_reports_verified_byte_total_without_network(self):
        module = load()
        payload = b"test model"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            stderr = io.StringIO()
            with mock.patch.object(module, "RUNTIME", root), \
                    mock.patch.object(module, "checked", side_effect=lambda path: path), \
                    mock.patch.object(module, "MODEL_SIZE", len(payload)), \
                    mock.patch.object(module, "MODEL_SHA256", hashlib.sha256(payload).hexdigest()), \
                    mock.patch.object(module.request, "urlopen", return_value=io.BytesIO(payload)), \
                    contextlib.redirect_stderr(stderr):
                model = module.download_model()
            self.assertEqual(model.read_bytes(), payload)
            self.assertIn(f"{len(payload)} / {len(payload)} 字节", stderr.getvalue())
            self.assertIn("100%", stderr.getvalue())
            self.assertIn("还需校验 SHA256", stderr.getvalue())
            self.assertEqual(list((root / "models").glob("*.part")), [])

    def test_dependency_failure_has_safe_next_action(self):
        module = load()
        stderr = io.StringIO()
        failure = subprocess.CalledProcessError(1, ["uv", "TOP-SECRET-ARG"])
        with mock.patch.object(module, "require_checkout"), \
                mock.patch.object(module, "prepare_directories"), \
                mock.patch.object(module, "runtime_lock", return_value=contextlib.nullcontext()), \
                mock.patch.object(module, "install_dependencies", side_effect=failure), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(module.main(["setup"]), 1)
        self.assertIn("inspect the tool output", stderr.getvalue())
        self.assertNotIn("TOP-SECRET", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
