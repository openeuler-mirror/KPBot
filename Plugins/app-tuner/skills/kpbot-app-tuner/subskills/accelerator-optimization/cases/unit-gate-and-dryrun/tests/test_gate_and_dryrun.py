#!/usr/bin/env python3
"""Tier-A unit tests for the accelerator-optimization evidence gate and
dry-run behavior.

Strategy: drive the real scripts with controlled inputs only — synthetic
snapshot_metadata.json fixtures and env-var overrides (OPENSSL_DIR /
NGX_PREFIX / NGINX_CONF / ENGINE_DIR / ASF_DIR / SSL_CERT all point into a
temp tree). No mock tools are needed for the gate logic itself; device-level
probes degrade into fallback_notes on any host.

Guarded regressions:
  1. evidence-gate exit-code contract: missing/unparseable metadata -> 4,
     parseable-but-mismatch (run_id / status / snapshot_time) -> 3, valid
     -> 0 with hpre_offload_evidence.json written (SKILL.md documented
     "exit 3" for every failure until this contract was pinned down)
  2. unbound mode (no --evidence-dir): stdout JSON, status unbound, exit 0
  3. enable_uadk_offload.sh stays read-only without --execute: dry-run
     banners, no config mutation, no .bak artifacts
  4. SKILL.md keeps documenting the exit 3 / exit 4 / unbound contract
     (doc-script drift guard)
  5. ssl_engine is planned as exactly one main-context directive; nested
     (http/server) directives are removed
  6. quota > throughput and unsafe --quota/--requests/--pid/--url inputs are
     rejected before any write; scripts contain no `bash -c` string execution,
     no StrictHostKeyChecking=no, and no pgrep self-match pattern
  7. binding mode requires started_at + target_identity; missing snapshot_time
     and malformed timestamps fail closed
  8. evidence writes are atomic: a read-only evidence dir exits 1 and leaves
     the previous file untouched
  9. OpenSSL "[ unavailable ]" is not reported as available
 10. verification attribution: failed handshakes cannot PASS; a shared-counter
     delta outside the handshake tolerance is INSUFFICIENT
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.environ.get("SKILL_DIR") or os.path.abspath(
    os.path.join(TESTS_DIR, "..", "..", "..")
)
COLLECT = os.path.join(SKILL_DIR, "scripts", "collect_hpre_evidence.sh")
ENABLE = os.path.join(SKILL_DIR, "scripts", "enable_uadk_offload.sh")
VERIFY = os.path.join(SKILL_DIR, "scripts", "verify_hpre_offload.sh")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")

RUN_ID = "run-gate-001"
STARTED_AT = "2026-11-23T08:00:00+00:00"
SNAPSHOT_OK = "2026-11-23T09:00:00+00:00"
SNAPSHOT_STALE = "2026-11-23T07:00:00+00:00"


class CollectEnvFixture:
    """Temp tree that makes device/env probes deterministic."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="acc-gate-")
        self.evidence_dir = os.path.join(self.root, "evidence")
        os.makedirs(self.evidence_dir, exist_ok=True)
        self.target_path = os.path.join(self.root, "target.json")
        self.target_identity = {"instance_id": "inst-1", "hostname": "host-a"}
        with open(self.target_path, "w", encoding="utf-8") as fh:
            json.dump(self.target_identity, fh)
        self.env = dict(os.environ)
        self.env.update(
            {
                "OPENSSL_DIR": os.path.join(self.root, "openssl"),   # absent binary
                "NGX_PREFIX": os.path.join(self.root, "nginx"),
                "NGINX_CONF": os.path.join(self.root, "nginx", "conf", "nginx.conf"),
                "ENGINE_DIR": os.path.join(self.root, "engines-1.1"),
                "ASF_DIR": os.path.join(self.root, "asf_conf"),
                "SSL_CERT": os.path.join(self.root, "cert.pem"),     # absent
            }
        )
        os.makedirs(os.path.dirname(self.env["NGINX_CONF"]), exist_ok=True)
        with open(self.env["NGINX_CONF"], "w", encoding="utf-8") as fh:
            fh.write(
                "worker_processes 4;\n"
                "events { worker_connections 1024; }\n"
                "http {\n"
                "    server { listen 10001 ssl; }\n"
                "}\n"
            )

    def write_meta(self, **fields):
        meta = {
            "current_run_id": RUN_ID,
            "current_evidence_status": "current",
            "snapshot_time": SNAPSHOT_OK,
            "target_identity": self.target_identity,
        }
        meta.update(fields)
        for key, value in fields.items():
            if value is None:
                meta.pop(key, None)
        with open(
            os.path.join(self.evidence_dir, "snapshot_metadata.json"), "w",
            encoding="utf-8",
        ) as fh:
            json.dump(meta, fh)

    def run_collect(self, *extra_args):
        argv = [
            "bash", COLLECT,
            "--evidence-dir", self.evidence_dir,
            "--current-run-id", RUN_ID,
            "--current-run-started-at", STARTED_AT,
            "--target-identity", self.target_path,
        ] + list(extra_args)
        return self.run_collect_args(*argv[2:])

    def run_collect_args(self, *args):
        argv = ["bash", COLLECT] + list(args)
        return subprocess.run(
            argv, capture_output=True, text=True, env=self.env, timeout=120
        )

    def write_target(self, target):
        with open(self.target_path, "w", encoding="utf-8") as fh:
            json.dump(target, fh)

    def evidence_path(self):
        return os.path.join(self.evidence_dir, "hpre_offload_evidence.json")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class EvidenceGateTest(unittest.TestCase):
    def setUp(self):
        self.fx = CollectEnvFixture()

    def tearDown(self):
        self.fx.cleanup()

    def _assert_no_evidence_written(self):
        self.assertFalse(os.path.exists(self.fx.evidence_path()))

    def test_missing_metadata_exits_4(self):
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 4)
        self.assertIn("snapshot_metadata.json", proc.stderr)
        self._assert_no_evidence_written()

    def test_unparseable_metadata_exits_4(self):
        with open(
            os.path.join(self.fx.evidence_dir, "snapshot_metadata.json"), "w"
        ) as fh:
            fh.write("{not json")
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 4)
        self._assert_no_evidence_written()

    def test_run_id_mismatch_exits_3(self):
        self.fx.write_meta(current_run_id="run-OTHER")
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("mismatch", proc.stderr)
        self._assert_no_evidence_written()

    def test_stale_evidence_status_exits_3(self):
        self.fx.write_meta(current_evidence_status="stale")
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("current", proc.stderr)
        self._assert_no_evidence_written()

    def test_snapshot_older_than_run_exits_3(self):
        self.fx.write_meta(snapshot_time=SNAPSHOT_STALE)
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("snapshot_time", proc.stderr)
        self._assert_no_evidence_written()

    def test_missing_started_at_exits_2(self):
        self.fx.write_meta()
        proc = self.fx.run_collect_args(
            "--evidence-dir", self.fx.evidence_dir,
            "--current-run-id", RUN_ID,
            "--target-identity", self.fx.target_path,
        )
        self.assertEqual(proc.returncode, 2)
        self._assert_no_evidence_written()

    def test_missing_target_identity_exits_2(self):
        self.fx.write_meta()
        proc = self.fx.run_collect_args(
            "--evidence-dir", self.fx.evidence_dir,
            "--current-run-id", RUN_ID,
            "--current-run-started-at", STARTED_AT,
        )
        self.assertEqual(proc.returncode, 2)
        self._assert_no_evidence_written()

    def test_missing_snapshot_time_exits_3(self):
        self.fx.write_meta(snapshot_time=None)
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("snapshot_time", proc.stderr)
        self._assert_no_evidence_written()

    def test_invalid_started_at_exits_3(self):
        self.fx.write_meta()
        proc = self.fx.run_collect_args(
            "--evidence-dir", self.fx.evidence_dir,
            "--current-run-id", RUN_ID,
            "--current-run-started-at", "not-a-timestamp",
            "--target-identity", self.fx.target_path,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("started_at", proc.stderr)
        self._assert_no_evidence_written()

    def test_target_identity_mismatch_exits_3(self):
        self.fx.write_meta()
        self.fx.write_target({"instance_id": "other", "hostname": "host-b"})
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("target identity", proc.stderr)
        self._assert_no_evidence_written()

    def test_write_failure_exits_nonzero_and_preserves_old_file(self):
        if os.geteuid() == 0:
            self.skipTest("permission failure is not enforceable as root")
        self.fx.write_meta()
        path = self.fx.evidence_path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("OLD\n")
        os.chmod(self.fx.evidence_dir, 0o500)
        try:
            proc = self.fx.run_collect()
        finally:
            os.chmod(self.fx.evidence_dir, 0o700)
        self.assertEqual(proc.returncode, 1)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "OLD\n")

    def test_valid_gate_writes_evidence(self):
        self.fx.write_meta()
        proc = self.fx.run_collect()
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        self.assertTrue(os.path.exists(self.fx.evidence_path()))
        data = json.load(open(self.fx.evidence_path(), encoding="utf-8"))
        self.assertEqual(data["schema"], "hpre_offload_evidence.v1")
        self.assertEqual(data["current_run_id"], RUN_ID)
        self.assertEqual(data["current_evidence_status"], "current")
        # NGINX_CONF fixture is parsed into the evidence
        self.assertEqual(data["nginx"]["worker_processes"], "4")
        self.assertIsInstance(data["fallback_notes"], list)

    def test_engine_unavailable_output_is_not_available(self):
        bindir = os.path.join(self.fx.env["OPENSSL_DIR"], "bin")
        os.makedirs(bindir, exist_ok=True)
        ossl = os.path.join(bindir, "openssl")
        with open(ossl, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
            fh.write("if [ \"$1\" = engine ]; then printf ' [ unavailable ]\\n'; exit 0; fi\n")
            fh.write("exit 0\n")
        os.chmod(ossl, 0o755)
        proc = subprocess.run(
            ["bash", COLLECT], capture_output=True, text=True,
            env=self.fx.env, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        data = json.loads(proc.stdout)
        self.assertEqual(data["openssl_engine_test"]["uadk_engine"], "unavailable")
        self.assertEqual(data["openssl_engine_test"]["e_sse"], "unavailable")

    def test_invalid_pid_exits_2(self):
        self.fx.write_meta()
        proc = self.fx.run_collect("--pid", "8x; id")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--pid", proc.stderr)
        self._assert_no_evidence_written()

    def test_unbound_mode_stdout_json(self):
        argv = ["bash", COLLECT]  # no --evidence-dir at all
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=self.fx.env, timeout=120
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["current_evidence_status"], "unbound")
        self.assertIsNone(data["snapshot_metadata_path"])

    def test_unknown_option_exits_2(self):
        proc = self.fx.run_collect("--bogus-flag")
        self.assertEqual(proc.returncode, 2)


class EnableDryRunTest(unittest.TestCase):
    def setUp(self):
        self.fx = CollectEnvFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_dry_run_makes_no_changes(self):
        conf = self.fx.env["NGINX_CONF"]
        before = hashlib.sha256(open(conf, "rb").read()).hexdigest()
        proc = subprocess.run(
            ["bash", ENABLE], capture_output=True, text=True,
            env=self.fx.env, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        out = proc.stdout + proc.stderr
        self.assertIn("mode: dry-run", out)
        self.assertIn("[dry-run]", out)
        # nothing modified: conf unchanged, no .bak artifacts next to it
        after = hashlib.sha256(open(conf, "rb").read()).hexdigest()
        self.assertEqual(before, after)
        baks = [
            f for f in os.listdir(os.path.dirname(conf))
            if ".bak" in f
        ]
        self.assertEqual(baks, [])
        # dry-run output must mention the approval gate for real execution
        self.assertIn("--approved-change-id", out)

    def test_unknown_option_exits_2(self):
        proc = subprocess.run(
            ["bash", ENABLE, "--bogus-flag"], capture_output=True, text=True,
            env=self.fx.env, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)

    def test_dry_run_plans_main_context_ssl_engine(self):
        proc = subprocess.run(
            ["bash", ENABLE], capture_output=True, text=True,
            env=self.fx.env, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        out = proc.stdout + proc.stderr
        # ssl_engine is a core (main-context) directive; never insert into http{}
        self.assertIn("main-context 'ssl_engine e_sse;'", out)
        self.assertNotIn("inside http {} block", out)

    def test_force_plans_engine_cnf_backup(self):
        cnf = os.path.join(self.fx.env["NGX_PREFIX"], "conf", "openssl_sse_uadk.cnf")
        os.makedirs(os.path.dirname(cnf), exist_ok=True)
        with open(cnf, "w", encoding="utf-8") as fh:
            fh.write("openssl_conf=old\n")
        proc = subprocess.run(
            ["bash", ENABLE, "--force"], capture_output=True, text=True,
            env=self.fx.env, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        out = proc.stdout + proc.stderr
        self.assertIn("backup:", out)
        self.assertIn("openssl_sse_uadk.cnf.bak.", out)
        with open(cnf, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "openssl_conf=old\n")

    def test_quota_above_throughput_exits_2(self):
        proc = subprocess.run(
            ["bash", ENABLE, "--quota", "5000OPS", "--throughput", "2000OPS"],
            capture_output=True, text=True, env=self.fx.env, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("exceeds throughput", proc.stderr)

    def test_invalid_capacity_exits_2(self):
        proc = subprocess.run(
            ["bash", ENABLE, "--quota", "5000OPS; id"],
            capture_output=True, text=True, env=self.fx.env, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)


    def _write_quota_fixture(self, throughput_sn, scheduler_sns):
        asf = self.fx.env["ASF_DIR"]
        os.makedirs(os.path.join(asf, "dev_metadata"), exist_ok=True)
        scheduler = {"scheduler": [
            {"SN": sn, "sched_dev": [{"name": "uadk_engine", "processor": "hpre", "quota": "450OPS"}]}
            for sn in scheduler_sns
        ]}
        metadata = {"processor": [{"name": "hpre", "algorithm": [
            {"SN": sn, "throughput": value} for sn, value in throughput_sn.items()
        ]}]}
        with open(os.path.join(asf, "scheduler.json"), "w", encoding="utf-8") as fh:
            json.dump(scheduler, fh)
        with open(os.path.join(asf, "dev_metadata", "uadk_engine.json"), "w", encoding="utf-8") as fh:
            json.dump(metadata, fh)

    def test_only_quota_validates_the_new_value(self):
        self._write_quota_fixture({"RSA": "2000OPS"}, ["RSA"])
        proc = subprocess.run(
            ["bash", ENABLE, "--only-quota", "--quota", "5000OPS"],
            capture_output=True, text=True, env=self.fx.env, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("exceeds throughput", proc.stderr)

    def test_all_quotas_validates_each_algorithm(self):
        self._write_quota_fixture({"RSA": "2000OPS", "SM2": "100OPS"}, ["RSA", "SM2"])
        proc = subprocess.run(
            ["bash", ENABLE, "--only-quota", "--all-quotas", "--quota", "450OPS"],
            capture_output=True, text=True, env=self.fx.env, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("exceeds throughput", proc.stderr)

    def test_all_quotas_requires_matching_throughput(self):
        self._write_quota_fixture({"RSA": "2000OPS"}, ["RSA", "SM2"])
        proc = subprocess.run(
            ["bash", ENABLE, "--only-quota", "--all-quotas", "--quota", "450OPS"],
            capture_output=True, text=True, env=self.fx.env, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no throughput", proc.stderr)


class VerifyUsageTest(unittest.TestCase):
    def test_help_exits_0(self):
        proc = subprocess.run(
            ["bash", VERIFY, "--help"], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage", (proc.stdout + proc.stderr).lower())

    def test_unknown_option_exits_2(self):
        proc = subprocess.run(
            ["bash", VERIFY, "--bogus-flag"], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 2)

    def test_rejects_non_numeric_requests(self):
        proc = subprocess.run(
            ["bash", VERIFY, "--requests", "50; id"], capture_output=True,
            text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--requests", proc.stderr)

    def test_rejects_unsafe_url(self):
        proc = subprocess.run(
            ["bash", VERIFY, "--url", "https://127.0.0.1:10001/x'; id; #"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--url", proc.stderr)

    def test_rejects_http_url(self):
        proc = subprocess.run(
            ["bash", VERIFY, "--url", "http://127.0.0.1:10001/index.html"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("https://", proc.stderr)

    def test_rejects_too_many_concurrency(self):
        proc = subprocess.run(
            ["bash", VERIFY, "--concurrency", "500"], capture_output=True,
            text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--concurrency", proc.stderr)

    def test_rejects_non_numeric_pid(self):
        proc = subprocess.run(
            ["bash", VERIFY, "--pid", "1; id"], capture_output=True,
            text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--pid", proc.stderr)


class VerifyLogicTest(unittest.TestCase):
    """Drive the counter/attribution logic with fake regs, maps and curl."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="acc-verify-")
        self.debugfs = os.path.join(self.root, "hisi_hpre")
        regdir = os.path.join(self.debugfs, "hpre0", "qm")
        os.makedirs(regdir)
        self.reg = os.path.join(regdir, "regs")
        with open(self.reg, "w", encoding="utf-8") as fh:
            fh.write("QM_DFX_SEND_SQE_TO_ACC_CNT 0x0\n")
        self.proc = os.path.join(self.root, "proc")
        os.makedirs(os.path.join(self.proc, "123"))
        with open(os.path.join(self.proc, "123", "maps"), "w", encoding="utf-8") as fh:
            fh.write("7f00-7f01 r-xp 00000000 00:00 0 /usr/lib64/engines-1.1/e_sse.so\n")
        self.bin = os.path.join(self.root, "curl")
        with open(self.bin, "w", encoding="utf-8") as fh:
            fh.write("""#!/bin/sh
raw=$(cat "$FAKE_REG")
hex=$(printf '%s\\n' "$raw" | awk '/QM_DFX_SEND_SQE_TO_ACC_CNT/{for(i=1;i<=NF;i++) if($i ~ /^0x[0-9a-fA-F]+$/) {print $i; exit}}')
step=${FAKE_STEP:-1}
if [ "$step" -gt 0 ]; then
    printf 'QM_DFX_SEND_SQE_TO_ACC_CNT 0x%x\\n' $((hex + step)) > "$FAKE_REG"
fi
printf '%s\\n' "${FAKE_STATUS:-200}"
""")
        os.chmod(self.bin, 0o755)
        self.env = dict(os.environ)
        self.env.update({
            "HPRE_DEBUGFS_ROOT": self.debugfs,
            "PROC_ROOT": self.proc,
            "CURL_BIN": self.bin,
            "FAKE_REG": self.reg,
        })

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_verify(self, **extra_env):
        env = dict(self.env)
        env.update({k: str(v) for k, v in extra_env.items()})
        return subprocess.run(
            ["bash", VERIFY, "--url", "https://127.0.0.1:10001/index.html",
             "--requests", "1", "--concurrency", "1", "--pid", "123", "--json"],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_pass_requires_aligned_successful_handshake(self):
        proc = self.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "PASS")
        self.assertEqual(data["handshakes_success"], 1)
        self.assertEqual(data["delta"], 1)
        self.assertEqual(data["offload_mode"], "asf")

    def test_failed_handshakes_cannot_pass(self):
        proc = self.run_verify(FAKE_STATUS=500, FAKE_STEP=0)
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "INSUFFICIENT")
        self.assertEqual(data["attribution"], "no_successful_handshakes")
        self.assertEqual(data["handshakes_success"], 0)

    def test_shared_counter_noise_is_insufficient(self):
        proc = self.run_verify(FAKE_STEP=5)
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "INSUFFICIENT")
        self.assertEqual(data["attribution"], "sqe_exceeds_handshake_attribution")


class ScriptSafetyTest(unittest.TestCase):
    """Guard the injection/self-match fixes against regression."""

    def test_no_shell_string_execution_or_insecure_ssh(self):
        for path in (ENABLE, VERIFY):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("bash -c", text, path)
            self.assertNotIn("StrictHostKeyChecking=no", text, path)

    def test_pgrep_pattern_cannot_match_wrapper(self):
        with open(VERIFY, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("'nginx: worker'", text)
        self.assertIn("'nginx: worke[r]'", text)


class SkillDocContractTest(unittest.TestCase):
    """Guard the SKILL.md <-> script exit-code contract against drift."""

    def test_skill_md_documents_exit_contract(self):
        with open(SKILL_MD, encoding="utf-8") as fh:
            text = fh.read()
        # the precise contract: 4 for missing/unparseable, 3 for mismatch,
        # unbound mode documented, script declared as source of truth
        self.assertIn("exit 4", text)
        self.assertIn("exit 3", text)
        self.assertIn("unbound", text)
        self.assertIn("真源声明", text)


if __name__ == "__main__":
    unittest.main()
