#!/usr/bin/env python3
"""install-contract — tier-A case for flink-velox-runtime-install.

Drives scripts/runtime_install.py (plan / apply / verify) against a fake machine
laid out in a scratch directory — a sysroot holding /opt (the install prefix) and a
JDK, a fake HOME, a fake PATH whose curl / git / mvn are sh stubs driven by files
(a download fixture directory with a real tgz + sha512, a mirror-failure switch, a
clone-failure switch, an mvn-failure switch, a call log), real sha512sum / tar, and
a fake flink-velox-env family directory with stub check scripts — with nothing from
the real machine in play; the script's seams ENV_PREPARE_SYSROOT / ENV_PREPARE_HOME
/ ENV_PREPARE_EUID / ENV_PREPARE_PREFIX_WRITABLE / ENV_PREPARE_ENV_FAMILY_DIR carry
the fake. Pins the plan (exact commands, skipped / blocked steps, the 1.19 line),
the apply contract (--yes required, root required when the prefix is not writable,
mirror-then-archive fallback, sha512 verification, symlink, minimal conf appended
only to an all-comment file, nexmark clone / mvn / jar copy / tree layout, stop at
the first failing step with a change log, idempotent rerun), verify aggregation,
the JSON shape (also for a crash), --out safety and that plan touches nothing.

Writes results/result.json; exit 0 iff all tests pass.
"""
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "runtime_install.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_runtime_install_result"
VERSION = "1.19.2"
TGZ = f"flink-{VERSION}-bin-scala_2.12.tgz"
MIRROR = "https://mirrors.huaweicloud.com/apache/flink"
ARCHIVE = "https://archive.apache.org/dist/flink"
NEXMARK_REPO = "https://github.com/nexmark/nexmark.git"
MINIMAL_CONF_FIRST = "jobmanager.rpc.address: localhost"
MINIMAL_CONF_LAST = "taskmanager.numberOfTaskSlots: 8"

CURL_STUB = textwrap.dedent('''\
    # fake curl: -fsSL --retry N --max-time T -o OUT URL -> copies <basename> from $FAKE_DL_DIR
    out=""; url=""
    while [ $# -gt 0 ]; do
      [ "$1" = "-o" ] && { out="$2"; shift 2; continue; }
      case "$1" in -*) shift 2 ;; *) url="$1"; shift ;; esac
    done
    echo "$url" >> "$FAKE_LOG"
    [ "${FAKE_CURL_FAIL_ALL:-}" = 1 ] && { echo "curl: (22) The requested URL returned error: 404" >&2; exit 22; }
    case "$url" in *huaweicloud*) [ "${FAKE_CURL_MIRROR_FAIL:-}" = 1 ] && { echo "curl: (22) 404" >&2; exit 22; } ;; esac
    cp "$FAKE_DL_DIR/$(basename "$url")" "$out" || { echo "curl: (37) no fixture" >&2; exit 37; }
    ''')
GIT_STUB = textwrap.dedent('''\
    # fake git: clone --depth 1 <url> <dest> lays down a nexmark checkout with its resources
    echo "$@" >> "$FAKE_LOG"
    [ "$1" = "clone" ] || exit 0
    shift; [ "$1" = "--depth" ] && shift 2
    url="$1"; dest="$2"
    [ "${FAKE_GIT_CLONE_FAIL:-}" = nexmark ] && { echo "fatal: unable to access '$url'" >&2; exit 128; }
    res="$dest/nexmark-flink/src/main/resources"
    mkdir -p "$dest/.git" "$res/bin" "$res/conf" "$res/queries" "$res/queries-cep"
    echo "ref: refs/heads/master" > "$dest/.git/HEAD"
    printf '#!/bin/sh\\necho run_query\\n' > "$res/bin/run_query.sh"; chmod 755 "$res/bin/run_query.sh"
    : > "$res/conf/nexmark.yaml"; : > "$res/queries/q0.sql"; : > "$res/queries-cep/q0.sql"
    ''')
MVN_STUB = textwrap.dedent('''\
    # fake mvn: -pl nexmark-flink -am package -DskipTests -> target jars in the cwd
    echo "mvn $* JAVA_HOME=$JAVA_HOME" >> "$FAKE_LOG"
    [ "${FAKE_MVN_FAIL:-}" = 1 ] && { echo "[ERROR] Failed to execute goal" >&2; exit 1; }
    mkdir -p nexmark-flink/target
    : > nexmark-flink/target/nexmark-flink-0.3-SNAPSHOT.jar
    : > nexmark-flink/target/nexmark-flink-0.3-SNAPSHOT-sources.jar
    ''')
CHECK_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    name = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ok = os.environ.get("FAKE_CHECK_FAIL") != name
    key = "flink_velox_" + name.replace("flink-velox-", "").replace("-", "_") + "_result"
    print(json.dumps({key: {"success": ok, "missing": [] if ok else ["x: missing"], "next_step": "stub",
                            "flink_home_seen": os.environ.get("FLINK_HOME")}}))
    sys.exit(0 if ok else 1)
    ''')


def load_module():
    spec = importlib.util.spec_from_file_location("install_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMachine:
    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.dl = self.root / "downloads"
        self.repos = self.home / "repos"
        for d in (self.bin, self.home, self.dl, self.sys / "usr" / "lib" / "jvm" / "java-17-fake" / "bin"):
            d.mkdir(parents=True)
        self.log = self.root / "log"
        self.jdk = self.sys / "usr" / "lib" / "jvm" / "java-17-fake"
        self.script(self.jdk / "bin" / "javac")
        self.script(self.jdk / "bin" / "java")
        self.script(self.bin / "curl", CURL_STUB)
        self.script(self.bin / "git", GIT_STUB)
        self.script(self.bin / "mvn", MVN_STUB)
        self.family = self.root / "flink-velox-env"
        self.prefix = self.sys / "opt"
        self.prefix.mkdir(parents=True)  # exists and is writable: no root needed in the fake
        self.nexmark_home = self.sys / "opt" / "nexmark"
        self.fixture_tgz(comments_only=True)

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body="exit 0\n"):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def fixture_tgz(self, version=VERSION, comments_only=True, name=TGZ, sha_name=None, bad_sha=False):
        """A real tgz holding a stock-looking distribution + its sha512 file."""
        staging = self.root / "staging"
        dist = staging / f"flink-{version}"
        for f in ("bin/flink", "bin/start-cluster.sh", "bin/stop-cluster.sh", "bin/sql-client.sh"):
            self.script(dist / f)
        self.write(dist / "conf" / "flink-conf.yaml",
                   ("# a stock file: every line is a comment\n" if comments_only else "rest.port: 8081\n"))
        self.write(dist / "lib" / f"flink-dist-{version}.jar", "jar\n")
        path = self.dl / name
        with tarfile.open(path, "w:gz") as tf:
            tf.add(dist, arcname=f"flink-{version}")
        digest = hashlib.sha512(path.read_bytes()).hexdigest()
        if bad_sha:
            digest = "0" * 128
        self.write(self.dl / (sha_name or name + ".sha512"), f"{digest}  {name}\n")
        shutil.rmtree(staging)
        return path

    def installed_flink(self, conf="rest.port: 8081\n"):
        dist = self.prefix / f"flink-{VERSION}"
        for f in ("bin/flink", "bin/start-cluster.sh"):
            self.script(dist / f)
        self.write(dist / "conf" / "flink-conf.yaml", conf)
        self.write(dist / "lib" / f"flink-dist-{VERSION}.jar", "jar\n")
        (self.prefix / "flink").symlink_to(dist, target_is_directory=True)
        return dist

    def fake_family(self):
        for skill, script in (("flink-velox-flink-dist-check", "flink_dist_check.py"),
                              ("flink-velox-nexmark-check", "nexmark_check.py")):
            self.script(self.family / skill / "scripts" / script, CHECK_STUB)

    def env(self, **overrides):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys),
               "ENV_PREPARE_HOME": str(self.home), "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family),
               "ENV_PREPARE_RETRY_SLEEP": "0", "HOME": str(self.home), "LC_ALL": "C",
               "FAKE_LOG": str(self.log), "FAKE_DL_DIR": str(self.dl)}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def args(self):
        return ["--prefix", str(self.prefix), "--nexmark-home", str(self.nexmark_home),
                "--repos-dir", str(self.repos)]

    def snapshot(self):
        seen = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            base = Path(dirpath)
            for name in dirnames:
                seen[str(base / name)] = ("d", (base / name).lstat().st_mode)
            for name in filenames:
                st = (base / name).lstat()
                seen[str(base / name)] = ("f", st.st_mode, st.st_size, st.st_mtime_ns)
        return seen


class InstallCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def run_tool(self, *args, env=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args, *self.m.args()], capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env, cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=120)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def step(self, result, name):
        return next(s for s in result["steps"] if s["name"] == name)

    def log(self):
        return self.m.log.read_text(encoding="utf-8") if self.m.log.exists() else ""

    # -- plan -----------------------------------------------------------------
    def test_plan_on_a_bare_machine(self):
        code, out, err, r = self.run_tool("--json")
        self.assertEqual((code, r["success"], r["command"], r["version"]), (0, True, "plan", VERSION))
        self.assertEqual((r["flink_home"], r["nexmark_home"]), (str(self.m.prefix / "flink"), str(self.m.nexmark_home)))
        flink, jar, tree = (self.step(r, n) for n in ("flink", "nexmark-jar", "nexmark-tree"))
        self.assertEqual([(s["status"], s["needs_root"]) for s in r["steps"]],
                         [("todo", False), ("todo", False), ("todo", False)])  # the fake prefix is writable
        self.assertEqual(flink["pending"], ["download+unpack", "symlink", "minimal-conf"])
        self.assertEqual(flink["plan"][0], f"# {TGZ}: try {MIRROR}/flink-{VERSION}/{TGZ}, then {ARCHIVE}/flink-{VERSION}/{TGZ}")
        self.assertEqual(flink["plan"][3], "(cd <tmp> && sha512sum -c " + TGZ + ".sha512)")
        self.assertEqual(flink["plan"][4], f"tar -xzf <tmp>/{TGZ} -C {self.m.prefix}")
        self.assertEqual(flink["plan"][5], f"ln -sfn {self.m.prefix}/flink-{VERSION} {self.m.prefix}/flink")
        self.assertEqual(jar["plan"][0], f"git clone --depth 1 {NEXMARK_REPO} {self.m.repos / 'nexmark'}   (retries 3)")
        self.assertEqual(jar["plan"][1], f"# JAVA_HOME={self.m.jdk} mvn -pl nexmark-flink -am package -DskipTests   (in {self.m.repos / 'nexmark'})")
        self.assertEqual(jar["plan"][2], f"# cp {self.m.repos / 'nexmark'}/nexmark-flink/target/nexmark-flink-*.jar (not -sources) -> {self.m.prefix / 'flink/lib'}/")
        self.assertEqual(tree["plan"][1], f"cp -R {self.m.repos / 'nexmark/nexmark-flink/src/main/resources'}/{{bin,conf,queries,queries-cep}} {self.m.nexmark_home}/")
        self.assertEqual(r["root_steps"], [])
        self.assertEqual(r["next_step"], "review the plan above, then run runtime_install.py apply --yes")
        _, out, _, _ = self.run_tool()
        self.assertIn(f"runtime-install plan  (Flink {VERSION} -> {self.m.prefix / 'flink'}, nexmark {self.m.nexmark_home})", out)
        self.assertIn("  flink        TODO    Flink 1.19.2: download+unpack, symlink, minimal-conf into", out)
        self.assertIn("        curl -fsSL --retry 3 --max-time 3600 -o <tmp>/" + TGZ + " <url>", out)
        self.assertIn("  nexmark-tree TODO    lay out the runnable tree at", out)
        self.assertIn("PLAN: 3 step(s) to apply", out)
        self.assertEqual(err, "")

    def test_plan_when_everything_is_installed(self):
        self.m.installed_flink()
        self.m.write(self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "jar\n")
        self.m.script(self.m.nexmark_home / "bin" / "run_query.sh")
        code, out, _, r = self.run_tool("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([s["status"] for s in r["steps"]], ["skipped"] * 3)
        self.assertEqual(self.step(r, "flink")["detail"], f"Flink {VERSION} at {self.m.prefix / 'flink-1.19.2'} (via {self.m.prefix / 'flink'}); conf already set")
        self.assertEqual(r["next_step"], "ready: the Flink runtime and nexmark are installed (verify with: runtime_install.py verify)")
        self.assertIn("PLAN: nothing to do", self.run_tool()[1])

    def test_plan_dist_present_but_link_and_conf_missing(self):
        self.m.installed_flink(conf="# all comments\n")
        (self.m.prefix / "flink").unlink()
        _, _, _, r = self.run_tool("--json")
        flink = self.step(r, "flink")
        self.assertEqual(flink["pending"], ["symlink", "minimal-conf"])
        self.assertEqual(flink["plan"][0], f"# {self.m.prefix}/flink-{VERSION} already installed")

    def test_plan_honours_the_1_19_line_only(self):
        self.m.fixture_tgz(version="1.19.3", name="flink-1.19.3-bin-scala_2.12.tgz")
        _, _, _, r = self.run_tool("--json", "--version", "1.19.3")
        self.assertEqual((r["success"], r["version"]), (True, "1.19.3"))
        code, out, _, r = self.run_tool("--json", "--version", "1.18.0")
        self.assertEqual((code, r["success"], r["error_message"]), (2, False, "refused: Flink 1.18.0 is not on the 1.19 line"))
        self.assertIn("pick a 1.19.x version", r["next_step"])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--version", "1.18.0")
        self.assertEqual(code, 2)

    def test_plan_blocked_without_mvn_or_jdk(self):
        (self.m.bin / "mvn").unlink()
        # a PATH without the system directories hides the real mvn (the plan runs no stubs)
        _, out, _, r = self.run_tool("--json", env=self.m.env(PATH=str(self.m.bin)))
        jar = self.step(r, "nexmark-jar")
        self.assertEqual(jar["status"], "blocked")
        self.assertIn("mvn is not on PATH (flink-velox-build-env-prepare installs maven)", jar["detail"])
        self.assertEqual(self.step(r, "nexmark-tree")["status"], "blocked")
        self.assertIn("PLAN: 1 step(s) to apply (2 step(s) blocked — see the fix: lines)", self.run_tool(env=self.m.env(PATH=str(self.m.bin)))[1])
        shutil.rmtree(self.m.jdk)
        _, _, _, r = self.run_tool("--json", env=self.m.env(PATH=str(self.m.bin)))
        self.assertIn("no JDK found (JAVA_HOME or /usr/lib/jvm/java-17-* / java-1.8.0-openjdk-*)",
                      self.step(r, "nexmark-jar")["detail"])

    def test_plan_touches_nothing(self):
        before = self.m.snapshot()
        self.run_tool("--json")
        self.assertEqual(self.m.snapshot(), before)

    # -- apply ----------------------------------------------------------------
    def test_apply_refuses_without_yes(self):
        code, out, _, r = self.run_tool("--json", "apply")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertTrue(r["error_message"].startswith("refused: apply needs --yes"))
        self.assertFalse((self.m.prefix / f"flink-{VERSION}").exists())
        self.assertEqual(self.log(), "")
        self.assertIn("REFUSED: apply needs --yes", self.run_tool("apply")[1])

    def test_apply_refuses_when_prefix_needs_root(self):
        env = self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0")
        _, _, _, r = self.run_tool("--json", env=env)
        self.assertEqual([s["needs_root"] for s in r["steps"]], [True, True, True])
        self.assertEqual(r["next_step"], "as root: review the plan above, then run runtime_install.py apply --yes")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn('steps flink, nexmark-jar, nexmark-tree need root (not writable): run "sudo python3 runtime_install.py apply --yes"',
                      r["error_message"])
        self.assertEqual(self.log(), "")

    def test_apply_refuses_blocked_steps(self):
        shutil.rmtree(self.m.jdk)
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("no JDK found", r["error_message"])

    def test_apply_full_run_then_idempotent_rerun(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("flink", "done"), ("nexmark-jar", "done"), ("nexmark-tree", "done")])
        dist = self.m.prefix / f"flink-{VERSION}"
        self.assertTrue(os.access(dist / "bin" / "flink", os.X_OK))
        self.assertEqual(os.path.realpath(self.m.prefix / "flink"), str(dist))
        conf = (dist / "conf" / "flink-conf.yaml").read_text(encoding="utf-8")
        self.assertTrue(conf.startswith("# a stock file: every line is a comment\n"))
        self.assertIn(MINIMAL_CONF_FIRST, conf)
        self.assertTrue(conf.rstrip().endswith(MINIMAL_CONF_LAST))
        lib = self.m.prefix / "flink" / "lib"
        self.assertEqual(sorted(p.name for p in lib.glob("nexmark*")), ["nexmark-flink-0.3-SNAPSHOT.jar"])
        home = self.m.nexmark_home
        self.assertEqual(sorted(d.name for d in home.iterdir()), ["bin", "conf", "data", "lib", "log", "queries", "queries-cep"])
        self.assertTrue(os.access(home / "bin" / "run_query.sh", os.X_OK))
        self.assertTrue((home / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())
        self.assertEqual([c["action"] for c in r["changes"]], ["downloaded+unpacked", "symlinked", "configured",
                                                               "cloned", "built+deployed", "laid out"])
        self.assertIn(f"{TGZ} from {MIRROR}/flink-{VERSION}/{TGZ} (sha512 verified)", r["changes"][0]["detail"])
        self.assertEqual(r["next_step"], "3 step(s) done, nothing left to do; run runtime_install.py verify")
        log = self.log()
        self.assertIn(f"{MIRROR}/flink-{VERSION}/{TGZ}\n{ARCHIVE}/flink-{VERSION}/{TGZ}.sha512\n", log)
        self.assertIn(f"clone --depth 1 {NEXMARK_REPO}", log)
        self.assertIn(f"mvn -pl nexmark-flink -am package -DskipTests JAVA_HOME={self.m.jdk}", log)
        report = load_module().render({KEY: r})
        self.assertIn("APPLIED: all steps done or already satisfied", report)
        self.assertIn("    - nexmark-tree: laid out — bin, conf, queries, queries-cep + lib/log/data at", report)
        # rerun: everything skipped, curl / git / mvn never called again
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, [s["status"] for s in r["steps"]], [c["action"] for c in r["changes"]]),
                         (0, ["skipped"] * 3, ["skipped"] * 3))
        self.assertEqual(self.log(), log)

    def test_apply_falls_back_to_the_archive_when_the_mirror_fails(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_CURL_MIRROR_FAIL="1"))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertIn(f"{MIRROR}/flink-{VERSION}/{TGZ}\n{ARCHIVE}/flink-{VERSION}/{TGZ}\n", self.log())
        self.assertIn(f"{ARCHIVE}/flink-{VERSION}/{TGZ} (sha512 verified)", r["changes"][0]["detail"])

    def test_apply_stops_on_a_checksum_mismatch(self):
        self.m.fixture_tgz(bad_sha=True)
        code, out, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (1, False))
        flink = self.step(r, "flink")
        self.assertEqual(flint_detail := flink["detail"], flint_detail)
        self.assertIn(f"sha512 mismatch for {TGZ}:", flink["detail"])
        self.assertFalse((self.m.prefix / f"flink-{VERSION}").exists())
        self.assertEqual(self.step(r, "nexmark-jar")["status"], "todo")  # later steps not run
        self.assertNotIn("clone", self.log())
        self.assertEqual((r["error_message"], [c["action"] for c in r["changes"]]), ("flink failed", ["failed"]))
        self.assertIn("NOT APPLIED: flink failed; later steps not run", self.run_tool("apply", "--yes")[1])

    def test_apply_stops_when_no_source_answers(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_CURL_FAIL_ALL="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.step(r, "flink")["detail"], f"download of {TGZ} failed from the mirror and from {ARCHIVE}")

    def test_apply_never_replaces_a_real_directory_at_the_link_path(self):
        self.m.installed_flink()
        (self.m.prefix / "flink").unlink()
        (self.m.prefix / "flink").mkdir()  # a real directory where the symlink should go
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("exists and is not a symlink — not replaced", self.step(r, "flink")["detail"])
        self.assertTrue((self.m.prefix / "flink").is_dir())

    def test_apply_stops_at_a_failed_nexmark_clone_then_resumes(self):
        env = self.m.env(FAKE_GIT_CLONE_FAIL="nexmark")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]][1:], [("nexmark-jar", "failed"), ("nexmark-tree", "todo")])
        self.assertIn("git clone of nexmark failed after 3 attempts: fatal: unable to access", self.step(r, "nexmark-jar")["detail"])
        self.assertFalse((self.m.repos / "nexmark").exists())
        self.assertEqual(self.log().count(f"clone --depth 1 {NEXMARK_REPO}"), 3)
        self.assertFalse(self.m.nexmark_home.exists())
        self.assertIn("fix the nexmark-jar step", r["next_step"])
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, [s["status"] for s in r["steps"]]), (0, ["skipped", "done", "done"]))

    def test_apply_stops_at_a_failed_mvn_build(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_MVN_FAIL="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("mvn build of nexmark-flink failed: [ERROR] Failed to execute goal", self.step(r, "nexmark-jar")["detail"])
        self.assertEqual([c["action"] for c in r["changes"]], ["downloaded+unpacked", "symlinked", "configured", "cloned", "failed"])
        self.assertFalse((self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())
        self.assertFalse(self.m.nexmark_home.exists())
        # the checkout is kept: the rerun builds without cloning again
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, self.step(r, "nexmark-jar")["status"]), (0, "done"))
        self.assertEqual(self.log().count(f"clone --depth 1 {NEXMARK_REPO}"), 1)

    def test_tree_blocked_when_jar_deployed_but_no_source(self):
        self.m.installed_flink()
        self.m.write(self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "jar\n")
        _, _, _, r = self.run_tool("--json")
        tree = self.step(r, "nexmark-tree")
        self.assertEqual(tree["status"], "blocked")
        self.assertIn("nexmark source resources not found at", tree["detail"])
        self.assertEqual(tree["fix"], f"git clone --depth 1 {NEXMARK_REPO} {self.m.repos / 'nexmark'}")
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["error_message"][:9]), (2, "refused: "))

    def test_no_nexmark_limits_every_step(self):
        _, _, _, r = self.run_tool("--json", "--no-nexmark")
        self.assertEqual([s["name"] for s in r["steps"]], ["flink"])
        self.assertIsNone(r["nexmark_home"])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--no-nexmark")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertFalse((self.m.repos / "nexmark").exists())
        self.assertNotIn("clone", self.log())
        _, _, _, r = self.run_tool("--json", "verify", "--no-nexmark")
        self.assertEqual([c["status"] for c in r["checks"]][1] if r["checks"] else None, "skipped")

    # -- verify ---------------------------------------------------------------
    def test_verify_aggregates_the_env_family_checks(self):
        self.m.fake_family()
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"], r["command"]), (0, True, "verify"))
        self.assertEqual([(c["name"], c["status"]) for c in r["checks"]], [("flink-dist", "ok"), ("nexmark", "ok")])
        self.assertIn("VERIFIED: Flink runtime and nexmark checks pass", self.run_tool("verify")[1])
        code, _, _, r = self.run_tool("--json", "verify", env=self.m.env(FAKE_CHECK_FAIL="flink-velox-nexmark-check"))
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "1 check(s) failing"))
        self.assertEqual(next(c for c in r["checks"] if c["name"] == "nexmark")["detail"], "1 item(s) missing; next: stub")

    def test_verify_reports_missing_siblings(self):
        _, _, _, r = self.run_tool("--json", "verify")
        self.assertEqual([c["status"] for c in r["checks"]], ["skipped", "skipped"])

    # -- shape, --out, usage, crash -------------------------------------------
    def test_json_contract_shape_and_crash_shape(self):
        _, _, _, plan = self.run_tool("--json")
        _, _, _, applied = self.run_tool("--json", "apply", "--yes")
        _, _, _, verified = self.run_tool("--json", "verify")
        for r in (plan, applied, verified):
            self.assertEqual(set(r), {"success", "checked_at", "command", "version", "prefix", "flink_home", "nexmark_home",
                                      "steps", "changes", "root_steps", "next_step", "error_message"} | ({"checks"} if r is verified else set()))
        self.assertEqual(set(plan["steps"][0]), {"name", "actions", "needs_root", "dist", "link", "tgz", "pending", "status", "detail", "plan"})
        module = load_module()

        def boom(opts):
            raise RuntimeError("probe exploded")

        module.step_flink = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json", *self.m.args()])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["command"], a["error_message"][:14]), (2, False, "plan", "tool aborted: "))
        self.assertEqual(set(a), set(plan) - {"checks"})
        self.assertEqual((a["steps"], a["changes"], a["flink_home"]), ([], [], None))

    def test_out_file_matches_stdout_and_is_confined(self):
        archive = self.tmp / "project" / "install.json"
        _, stdout, _, _ = self.run_tool("--json", "--out", str(archive))
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), json.loads(stdout))
        first = archive.read_text(encoding="utf-8")
        code, _, err, _ = self.run_tool("--json", "--out", str(archive))
        self.assertEqual((code, archive.read_text(encoding="utf-8")), (2, first))
        self.assertIn("--overwrite", err)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        code, _, err, _ = self.run_tool("--json", "--out", str(self.tmp / "outside.json"), cwd=str(elsewhere))
        self.assertEqual(code, 2)
        self.assertIn("inside the project directory", err)
        target = self.tmp / "keep.json"
        target.write_text("keep me\n", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(target)
        code, _, err, _ = self.run_tool("--json", "--out", str(link))
        self.assertEqual((code, target.read_text(encoding="utf-8")), (2, "keep me\n"))
        self.assertIn("not a symlink", err)
        self.assertEqual(self.run_tool("--json", "--out", str(archive), "--overwrite")[0], 0)

    def test_invalid_command_is_2_without_json(self):
        code, out, err, r = self.run_tool("install")
        self.assertEqual((code, r), (2, None))
        self.assertIn("usage", err.lower())


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"case": "install-contract", "skill": "flink-velox-runtime-install",
              "success": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (RESULTS / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
