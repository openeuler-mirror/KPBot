#!/usr/bin/env python3
"""Run the kernel workflow contract in disposable directories, without root."""

import argparse
from email.message import EmailMessage
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

CASE = Path(__file__).resolve().parent
SCRIPTS = CASE.parents[1] / "scripts"
ORIGINAL_ID = "78f11b68-1170-4b09-a4dc-06f9d4a48932"
CANDIDATE_ID = "00d3396f-ff85-445a-b27e-b3e061c99b0e"


class Workflow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def command(self, *args, code=0, env=None):
        result = subprocess.run([str(a) for a in args], cwd=self.tmp,
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return result.stdout

    def git(self, repo, *args):
        return self.command("git", "-C", repo, "-c", "user.name=Kernel Test",
                            "-c", "user.email=fixture@example.invalid",
                            "-c", "commit.gpgsign=false", *args).strip()

    def write(self, name, body):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def prepare(self, repo, run_name, *patches, hashes=(), **kwargs):
        run = self.tmp / run_name
        args = [sys.executable, SCRIPTS / "prepare_source.py",
                "--repo", repo, "--base", "driver-base", "--run", run]
        for patch in patches:
            args.extend(["--patch", patch])
        for value in hashes:
            args.extend(["--patch-sha256", value])
        self.command(*args, **kwargs)
        return json.loads((run / "source.json").read_text())

    def check(self, *args, code=0):
        output = self.command(sys.executable, SCRIPTS / "check_kernel.py", *args, code=code)
        return json.loads(output)

    def series(self):
        repo = self.tmp / "driver tree"
        repo.mkdir()
        self.git(repo, "init", "-q")
        driver = repo / "disk.c"
        driver.write_text("int queue_limit = 16;\n")
        self.git(repo, "add", "disk.c")
        self.git(repo, "commit", "-qm", "base")
        self.git(repo, "tag", "driver-base")
        diffs = []
        for number in (32, 64):
            driver.write_text(f"int queue_limit = {number};\n")
            self.git(repo, "commit", "-qam", f"Use limit {number}")
            patch = self.command("git", "-C", repo, "diff", "HEAD~", "HEAD")
            diffs.append(self.write(f"{number}.diff", patch))
        return repo, diffs

    def test_ordered_mailbox_and_dirty_caller(self):
        repo, _ = self.series()
        mailbox = self.write("series.mbox", self.command(
            "git", "-C", repo, "format-patch", "--stdout", "driver-base..HEAD"))
        expected_tree = self.git(repo, "rev-parse", "HEAD^{tree}")
        driver = repo / "disk.c"
        driver.write_text("staged local change\n")
        self.git(repo, "add", "disk.c")
        driver.write_text("unstaged local change\n")
        self.write("driver tree/untracked", "keep\n")
        before = (self.git(repo, "status", "--porcelain"), self.git(repo, "write-tree"))
        foreign_index = self.tmp / "outside.index"
        record = self.prepare(repo, "new run", mailbox,
                              env=dict(os.environ, GIT_INDEX_FILE=str(foreign_index)))
        self.assertEqual(record["status"], "prepared")
        self.assertEqual(record["patched_tree"], expected_tree)
        self.assertEqual(record["patches"][0]["parts"], 2)
        self.assertEqual((self.tmp / "new run/source/disk.c").read_text(), "int queue_limit = 64;\n")
        self.assertEqual(before, (self.git(repo, "status", "--porcelain"), self.git(repo, "write-tree")))
        self.assertFalse(foreign_index.exists())

    def test_plain_series_and_unmodified_tag(self):
        repo, diffs = self.series()
        record = self.prepare(repo, "patched", *diffs)
        self.assertEqual(record["patched_tree"], self.git(repo, "rev-parse", "HEAD^{tree}"))
        baseline = self.prepare(repo, "unchanged")
        self.assertEqual(baseline["patched_tree"], self.git(repo, "rev-parse", "driver-base^{tree}"))
        self.assertEqual(baseline["patches"], [])

    def test_mail_archive_with_cover_and_mime_message(self):
        repo, diffs = self.series()
        mail_dir = self.tmp / "mail"
        self.git(repo, "format-patch", "--cover-letter", "--numbered-files",
                 "-o", mail_dir, "driver-base..HEAD")
        mail_files = sorted(mail_dir.iterdir())
        self.assertEqual([path.name for path in mail_files], ["0", "1", "2"])

        separator = "From mboxrd@z Thu Jan  1 00:00:00 1970\n"
        messages = []
        for path in mail_files:
            with path.open(encoding="utf-8") as mail:
                self.assertTrue(mail.readline().startswith("From "), path.name)
                # mboxrd quotes body lines beginning with From or an already quoted From.
                body = re.sub(r"(?m)^(>*From )", r">\1", mail.read())
            messages.append(separator + body)

        archive_path = self.write("driver.mbox", "".join(messages))
        self.assertTrue(archive_path.read_bytes().startswith(separator.encode("ascii")))
        archived = self.prepare(repo, "archive", archive_path)
        self.assertEqual(archived["patched_tree"], self.git(repo, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(archived["patches"][0]["parts"], 2)
        cover_path = self.write("cover.mbox", messages[0])
        rejected = self.prepare(repo, "cover only", cover_path, code=1)
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["patches"][0]["parts"], 0)

        message = EmailMessage()
        message["From"] = "Kernel Test <fixture@example.invalid>"
        message["Subject"] = "[PATCH 1/2] update queue limit"
        message.set_content(diffs[0].read_text(), cte="base64")
        self.write("driver.eml", message.as_string())
        raw = self.prepare(repo, "raw email", self.tmp / "driver.eml", diffs[1])
        self.assertEqual(raw["patched_tree"], archived["patched_tree"])

    def test_failed_series_keeps_evidence_and_rejects_retry_in_place(self):
        repo, diffs = self.series()
        failed = self.prepare(repo, "review", *reversed(diffs), code=1)
        self.assertEqual((failed["status"], failed["current_patch"]), ("failed", "64.diff"))
        self.assertFalse((self.tmp / "review/source").exists())
        self.assertEqual(self.prepare(repo, "review", *diffs, code=1), failed)
        html = self.write("download.patch", "<html>sign in required</html>\n")
        self.assertEqual(self.prepare(repo, "download", html, code=1)["status"], "failed")

    def test_trusted_patch_digest_and_local_git_scope(self):
        repo, diffs = self.series()
        checksums = [hashlib.sha256(path.read_bytes()).hexdigest() for path in diffs]
        index = self.git(repo, "write-tree")
        worktrees = self.git(repo, "worktree", "list", "--porcelain")
        failed = self.prepare(repo, "bad digest", *diffs, hashes=[checksums[0], "0" * 64], code=1)
        self.assertEqual((failed["status"], failed["actual_sha256"]), ("failed", checksums[1]))
        self.assertFalse((self.tmp / "bad digest/source").exists())
        self.assertEqual(self.git(repo, "worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(self.git(repo, "write-tree"), index)

        marker = self.tmp / "unexpected"
        hook = repo / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\n")
        hook.chmod(0o755)
        # A local Git wrapper observes which environment reaches every Git command.
        real_git = shlex.quote(shutil.which("git"))
        wrapper = self.write("bin/git", "#!/bin/sh\n"
                             f'if [ -n "$KPBOT_FIXTURE_TOKEN" ]; then touch {shlex.quote(str(marker))}; fi\n'
                             f'exec {real_git} "$@"\n')
        wrapper.chmod(0o755)
        env = dict(os.environ, PATH=str(wrapper.parent) + os.pathsep + os.environ["PATH"],
                   KPBOT_FIXTURE_TOKEN="synthetic-token")
        good = self.prepare(repo, "checked", *diffs, hashes=checksums, env=env)
        self.assertEqual([item["sha256"] for item in good["patches"]], checksums)
        self.assertFalse(marker.exists())

    def test_resolved_config_and_unavailable_options(self):
        selections = self.write("request", "CONFIG_BLOCK=y\nCONFIG_LOOP=m\n# CONFIG_DEBUG is not set\nCONFIG_NR=48\n")
        check = self.check("config", "--config", selections, "--expect", selections)
        self.assertEqual(len(check["checks"]), 4)
        unknown = self.write("absent", "# CONFIG_MISSING is not set\n")
        wrong = self.check("config", "--config", selections, "--expect", unknown, code=1)
        self.assertIsNone(wrong["checks"]["CONFIG_MISSING"]["actual"])
        dropped = self.write("changed", "CONFIG_NR=24\n")
        self.assertEqual(self.check("config", "--config", selections, "--expect", dropped,
                                    code=1)["checks"]["CONFIG_NR"]["actual"], "48")
        self.assertIn("conflicting", self.check("config", "--config", selections, "--expect",
                                               selections, "--expect", dropped, code=1)["error"])

    def test_guest_boot_and_fallback_config(self):
        self.write("capture/proc/sys/kernel/osrelease", "6.12-driver\n")
        self.write("capture/proc/sys/kernel/random/boot_id", CANDIDATE_ID + "\n")
        self.write("capture/proc/cmdline",
                   'console=ttyS0 queue_policy=balanced dev.label="disk A" -- init_only\n')
        config = self.write("capture/boot/config-6.12-driver", "CONFIG_STORAGE=y\n")
        fragment = self.write("request", "CONFIG_STORAGE=y\n")
        notes = self.tmp / "capture/sys/kernel/notes"
        notes.parent.mkdir(parents=True)
        notes.write_bytes(struct.pack("<III", 4, 4, 3) + b"GNU\0\x12\x34\xab\xcd")
        args = ["boot", "--root", self.tmp / "capture", "--release", "6.12-driver"]
        full = self.check(*args, "--expect", fragment, "--has-arg", "queue_policy=balanced",
                          "--has-arg", "dev.label=disk A", "--lacks-arg", "init_only",
                          "--lacks-arg", "queue_policy=fast", "--build-id", "1234abcd",
                          "--previous-boot-id", ORIGINAL_ID)
        self.assertEqual((full["status"], full["boot_id"]), ("matched", CANDIDATE_ID))
        self.assertEqual(full["config_source"], "boot/config-6.12-driver")
        (self.tmp / "capture/proc/config.gz").write_bytes(gzip.compress(config.read_bytes()))
        config.unlink()
        observed = self.check(*args, "--expect", fragment)
        self.assertEqual((observed["status"], observed["config_source"]), ("matched", "proc/config.gz"))
        self.assertFalse(self.check(*args, "--previous-boot-id", CANDIDATE_ID, code=1)["checks"]["new_boot"]["actual"])
        self.assertFalse(self.check(*args, "--has-arg", "balanced", code=1)["checks"]["cmdline:balanced"]["actual"])
        self.assertEqual(self.check(*args, "--build-id", "ab00", code=1)["status"], "unverified")
        self.write("capture/proc/sys/kernel/osrelease", "6.6-old\n")
        self.assertEqual(self.check(*args, code=1)["checks"]["kernel_release"]["actual"], "6.6-old")

    def handoff(self, record, request=None, code=0):
        path = self.write("run/switch-result.json", json.dumps(record))
        args = ["--request", self.write("request.json", json.dumps(request))] if request is not None else []
        out = self.command(sys.executable, SCRIPTS / "verify_handoff.py", path, *args, code=code)
        return json.loads(out)

    def guest_example(self):
        guide = (SCRIPTS.parent / "references/handoff.md").read_text()
        record = json.loads(re.search(r"```json\n(.*?)\n```", guide, re.S)[1])
        for path in record["evidence_sources"].values():
            self.write("run/" + path, "synthetic probe output\n")
        self.write("run/selected-image.log", record["findings"]["next_boot"] + "\n")
        return record

    def observe(self, record, *args, code=0):
        release = record["findings"]["release"]
        self.write("capture/proc/sys/kernel/osrelease", release + "\n")
        self.write("capture/proc/sys/kernel/random/boot_id", CANDIDATE_ID + "\n")
        boot = self.check("boot", "--root", self.tmp / "capture", "--release", release,
                          "--run-id", record["run_id"], "--target-id", record["target"]["id"],
                          *args, code=code)
        self.write("run/boot-check.json", json.dumps(boot))

    def test_guest_receipt_from_guide_and_blocked_trial_after_restore(self):
        record = self.guest_example()
        self.observe(record)
        request = {"contract": "agentenv-kernel-switch-request/v1", "run_id": record["run_id"],
                   "target": dict(record["target"]), "goal": "trial", "release": record["findings"]["release"]}
        self.assertEqual(self.handoff(record, request)["status"], "completed")
        for changed in ({"goal": "restore"}, {"release": "old-kernel"}, {"run_id": "other-run"},
                        {"probes": ["pause_resume"]}):
            self.assertEqual(self.handoff(record, dict(request, **changed), code=2)["status"], "invalid")
        record["target"]["id"] = "other-sandbox"
        self.assertEqual(self.handoff(record, code=2)["status"], "invalid")
        record["target"]["id"] = "sandbox-demo"
        self.observe(record, "--previous-boot-id", CANDIDATE_ID, code=1)
        self.assertEqual(self.handoff(record, code=2)["status"], "invalid")
        record.update(status="blocked", phase="requested", required_evidence=["cold boot of the candidate"])
        blocked = self.handoff(record, code=1)
        (self.tmp / "run").rename(self.tmp / "trial")
        record = self.guest_example()
        record.update(goal="restore", phase="restored")
        record["findings"]["release"] = "6.6-original"
        self.observe(record)
        self.assertEqual(self.handoff(record)["phase"], "restored")
        old_result = self.command(sys.executable, SCRIPTS / "verify_handoff.py",
                                  self.tmp / "trial/switch-result.json", code=1)
        self.assertEqual(json.loads(old_result), blocked)

    def test_task_pins_the_build_id_for_a_shared_release(self):
        record = self.guest_example()
        findings = record["findings"]
        findings.update(previous_release=findings["release"], build_id="1234abcd")
        notes = self.write("capture/sys/kernel/notes", "")
        notes.write_bytes(struct.pack("<III", 4, 4, 3) + b"GNU\0\x12\x34\xab\xcd")
        self.observe(record, "--build-id", "1234abcd")
        request = {"contract": "agentenv-kernel-switch-request/v1", "run_id": record["run_id"],
                   "target": record["target"], "goal": "trial", "release": findings["release"]}
        self.assertEqual(self.handoff(record, dict(request, build_id="eeeeeeee"), code=2)["status"], "invalid")
        request["build_id"] = "1234ABCD"
        self.assertEqual(self.handoff(record, request)["status"], "completed")
        findings["build_id"] = "eeeeeeee"
        self.assertEqual(self.handoff(record, request, code=2)["status"], "invalid")
        del findings["build_id"]
        self.assertEqual(self.handoff(record, request)["status"], "completed")
        self.observe(record)
        self.assertEqual(self.handoff(record, request, code=2)["status"], "invalid")

    def test_build_receipt_hashes_the_delivered_artifact(self):
        repo, diffs = self.series()
        self.write("run/kernelrelease", "6.12-driver\n")
        source = self.prepare(repo, "run/input", *diffs)
        self.assertEqual(source["status"], "prepared")
        image = self.tmp / "run/fixture.img"
        image.write_bytes((self.tmp / "run/input/source/disk.c").read_bytes())
        record = {"contract": "agentenv-kernel-switch/v1", "run_id": "build-01",
                  "target": {"layer": "guest", "id": "driver-build"}, "goal": "build",
                  "phase": "built", "status": "completed", "required_evidence": [],
                  "findings": {"release": "6.12-driver", "artifact_sha256": hashlib.sha256(image.read_bytes()).hexdigest()},
                  "evidence_sources": {"kernelrelease": "kernelrelease", "artifact": "fixture.img",
                                       "source": "input/source.json"}}
        self.assertEqual(self.handoff(record)["status"], "completed")
        request = {"contract": "agentenv-kernel-switch-request/v1", "run_id": record["run_id"],
                   "target": record["target"], "goal": "build", "artifact_sha256": "0" * 64}
        self.assertEqual(self.handoff(record, request, code=2)["status"], "invalid")
        request["artifact_sha256"] = record["findings"]["artifact_sha256"].upper()
        self.assertEqual(self.handoff(record, request)["status"], "completed")
        image.write_bytes(b"other build")
        self.assertEqual(self.handoff(record, code=2)["status"], "invalid")

    def test_host_build_id_and_runtime_probe_failures(self):
        record = self.guest_example()
        record["target"] = {"layer": "host", "id": "host-demo"}
        record["findings"]["previous_release"] = record["findings"]["release"]
        del record["findings"]["probe_exit_codes"]["cold_start"]
        notes = self.write("capture/sys/kernel/notes", "")
        notes.write_bytes(struct.pack("<III", 4, 4, 3) + b"GNU\0\x12\x34\xab\xcd")
        self.observe(record, "--previous-boot-id", ORIGINAL_ID, "--build-id", "1234abcd")
        self.assertEqual(self.handoff(record, code=2)["status"], "invalid")
        record["findings"]["build_id"] = "1234abcd"
        self.assertEqual(self.handoff(record)["status"], "completed")
        record["goal"] = "runtime"
        record["findings"].update(previous_value="16", trial_value="32", effective_value="32")
        record["findings"]["probe_exit_codes"] = {"mechanism": 1, "workload": 0}
        record["evidence_sources"]["mechanism"] = self.write("run/value.log", "16 -> 32\n").name
        self.assertEqual(self.handoff(record, code=2)["status"], "invalid")
        record["findings"]["probe_exit_codes"]["mechanism"] = 0
        self.assertEqual(self.handoff(record)["status"], "completed")

    def test_outside_capture_and_report_references(self):
        secret = "synthetic-private-content"
        sentinel = self.write("unrelated/note", secret)
        before = hashlib.sha256(sentinel.read_bytes()).digest()
        capture = self.tmp / "capture"
        proc = capture / "proc/sys/kernel"
        proc.mkdir(parents=True)
        (proc / "osrelease").symlink_to(sentinel)
        check = self.check("boot", "--root", capture, "--release", "6.12", code=1)
        self.assertIn("leaves the selected directory", check["error"])
        self.assertNotIn(secret, json.dumps(check))
        record = self.guest_example()
        log = self.tmp / "run/envd.log"
        log.unlink()
        log.symlink_to(sentinel)
        result = self.handoff(record, code=2)
        self.assertIn("leaves the selected directory", result["error"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(hashlib.sha256(sentinel.read_bytes()).digest(), before)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=CASE / "results",
                        help="case-owned output directory (default: cases/workflow-contract/results)")
    output_dir = parser.parse_args().results_dir
    if output_dir.is_symlink() or (output_dir / "result.json").is_symlink():
        parser.exit(2, "expected a case-owned directory and result file\n")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Workflow)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output_dir.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output_dir, mode="w", delete=False) as stream:
        json.dump({"success": result.wasSuccessful(), "tests_run": result.testsRun}, stream)
        stream.write("\n")
    Path(stream.name).replace(output_dir / "result.json")
    sys.exit(0 if result.wasSuccessful() else 1)
