#!/usr/bin/env python3
"""build-contract — tier-A case for flink-velox-stack-build.

Drives scripts/stack_build.py (plan / apply / verify) against a fake machine laid
out in a scratch directory — three source checkouts, a fake JDK 17, a Flink home
whose lib/ holds an older GFV set plus a real-zip nexmark jar carrying the
nexmark-flink pom, a fake ~/.m2 with some of the 14 runtime dependencies, a fake
PATH whose mvn is an sh stub (creates target jars named by the checkout it runs
in — gluten at a version the script must not hard-code — logs JAVA_HOME and the
velox source dir, can be made to fail per component), and a fake flink-velox-env
family directory with a lib-state-check stub — with nothing from the real machine
in play; the script's seams ENV_PREPARE_SYSROOT / ENV_PREPARE_HOME /
ENV_PREPARE_EUID / ENV_PREPARE_PREFIX_WRITABLE / ENV_PREPARE_ENV_FAMILY_DIR
carry the fake. Pins the plan (exact mvn / cp / conf commands, backup set,
nexmark install-file, runtime deps found vs missing, pin-jdk variants), the apply
contract (--yes required, root required when lib is not writable, stop at the
first failing build with the previous set kept under lib.bak/, idempotent rerun,
--only), verify, the JSON shape (also for a crash), --out safety and that plan
touches nothing.

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "stack_build.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_stack_build_result"
# pinned independently of the script
DEPS = ("com.google.guava:guava:33.4.0-jre", "com.google.guava:failureaccess:1.0.2",
        "org.apache.arrow:arrow-c-data:18.1.0", "org.apache.arrow:arrow-format:18.1.0",
        "org.apache.arrow:arrow-memory-core:18.1.0", "org.apache.arrow:arrow-memory-unsafe:18.1.0",
        "org.apache.arrow:arrow-vector:18.1.0", "commons-io:commons-io:2.18.0",
        "com.google.flatbuffers:flatbuffers-java:24.3.25", "com.fasterxml.jackson.core:jackson-annotations:2.18.0",
        "com.fasterxml.jackson.core:jackson-core:2.18.0", "com.fasterxml.jackson.core:jackson-databind:2.18.0",
        "com.fasterxml.jackson.datatype:jackson-datatype-jdk8:2.18.0",
        "com.fasterxml.jackson.datatype:jackson-datatype-jsr310:2.18.0")
OLD_JARS = ("gluten-flink-runtime-1.8.0-SNAPSHOT.jar", "velox4j-0.0.9-SNAPSHOT.jar", "guava-33.4.0-jre.jar")
NEW_JARS = ("gluten-flink-loader-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar",
            "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar")

MVN_STUB = textwrap.dedent('''\
    # fake mvn: creates the target jars of the checkout it runs in; logs the build env
    name="$(basename "$PWD")"
    echo "mvn $* JAVA_HOME=$JAVA_HOME VELOX4J_VELOX_SOURCE_DIR=${VELOX4J_VELOX_SOURCE_DIR:-} JOBS=${VELOX4J_BUILD_JOBS:-} cwd=$PWD" >> "$FAKE_LOG"
    case " $* " in
      *" install:install-file "*) exit 0 ;;
    esac
    case "$name" in
      velox4j) [ "${FAKE_MVN_FAIL:-}" = velox4j ] && { echo "[ERROR] velox4j boom" >&2; exit 1; }
               [ "${FAKE_MVN_NO_JAR:-}" = 1 ] && exit 0   # "success" without a product
               mkdir -p target; : > target/velox4j-0.1.0-SNAPSHOT.jar; : > target/velox4j-0.1.0-SNAPSHOT-sources.jar ;;
      gluten-flink) [ "${FAKE_MVN_FAIL:-}" = gluten ] && { echo "[ERROR] gluten boom" >&2; exit 1; }
               mkdir -p planner/target runtime/target loader/target
               : > planner/target/gluten-flink-planner-1.9.0-SNAPSHOT.jar
               : > runtime/target/gluten-flink-runtime-1.9.0-SNAPSHOT.jar
               : > loader/target/gluten-flink-loader-1.9.0-SNAPSHOT.jar ;;
    esac
    exit 0
    ''')
CHECK_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    ok = os.environ.get("FAKE_CHECK_FAIL") != "lib-state"
    print(json.dumps({"flink_velox_lib_state_check_result": {"success": ok, "missing": [] if ok else ["build: partial"],
                                                             "lib_state": {"build": "gfv" if ok else "partial"}, "next_step": "stub"}}))
    sys.exit(0 if ok else 1)
    ''')


def load_module():
    spec = importlib.util.spec_from_file_location("build_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMachine:
    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.repos = self.home / "repos"
        self.log = self.root / "log"
        for d in (self.bin, self.home, self.sys / "usr" / "lib" / "jvm" / "java-17-fake" / "bin"):
            d.mkdir(parents=True)
        self.jdk = self.sys / "usr" / "lib" / "jvm" / "java-17-fake"
        self.write(self.jdk / "release", 'JAVA_VERSION="17.0.18"\n')
        self.script(self.jdk / "bin" / "javac")
        self.script(self.jdk / "bin" / "java")
        self.script(self.bin / "mvn", MVN_STUB)
        self.family = self.root / "flink-velox-env"
        self.flink = self.sys / "opt" / "flink"
        self.lib = self.flink / "lib"
        self.checkouts()
        self.flink_home()

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body="exit 0\n"):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def checkouts(self):
        self.write(self.repos / "velox" / "CMakeLists.txt", "x\n")
        self.write(self.repos / "velox" / "scripts" / "setup-centos9.sh", "x\n")
        self.write(self.repos / "velox4j" / "pom.xml", "x\n")
        self.write(self.repos / "gluten" / "gluten-flink" / "pom.xml", "x\n")

    def flink_home(self, old_jars=OLD_JARS, nexmark=True, conf="rest.port: 8081\n"):
        self.script(self.flink / "bin" / "flink")
        self.write(self.lib / "flink-dist-1.19.2.jar", "jar\n")
        for jar in old_jars:
            self.write(self.lib / jar, "old\n")
        if nexmark:
            with zipfile.ZipFile(self.lib / "nexmark-flink-0.3-SNAPSHOT.jar", "w") as zf:
                zf.writestr("META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml", "<project/>")
        if conf is not None:
            self.write(self.flink / "conf" / "flink-conf.yaml", conf)

    def m2(self, deps):
        for dep in deps:
            g, a, v = dep.split(":")
            self.write(self.home / ".m2" / "repository" / g.replace(".", "/") / a / v / f"{a}-{v}.jar", "jar\n")

    def fake_family(self):
        self.script(self.family / "flink-velox-lib-state-check" / "scripts" / "lib_state_check.py", CHECK_STUB)

    def env(self, **overrides):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys), "ENV_PREPARE_HOME": str(self.home),
               "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family), "HOME": str(self.home), "LC_ALL": "C",
               "FLINK_HOME": str(self.flink), "FAKE_LOG": str(self.log)}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def args(self):
        return ["--repos-dir", str(self.repos)]

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


class BuildCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")
        self.m.m2(DEPS[:10])  # ten of the fourteen in ~/.m2

    def run_tool(self, *args, env=None, cwd=None):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args, *self.m.args()], capture_output=True, text=True,
                              errors="replace", env=self.m.env() if env is None else env,
                              cwd=str(self.tmp if cwd is None else cwd), stdin=subprocess.DEVNULL, timeout=120)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def step(self, result, name):
        return next(s for s in result["steps"] if s["name"] == name)

    def log(self):
        return self.m.log.read_text(encoding="utf-8") if self.m.log.exists() else ""

    # -- plan -----------------------------------------------------------------
    def test_plan_with_an_older_set_in_lib(self):
        code, out, err, r = self.run_tool("--json")
        self.assertEqual((code, r["success"], r["command"]), (0, True, "plan"))
        self.assertEqual(r["repos"], {"velox": str(self.m.repos / "velox"), "velox4j": str(self.m.repos / "velox4j"),
                                      "gluten": str(self.m.repos / "gluten")})
        self.assertEqual((r["jdk"], r["flink_home"], r["lib"]), (str(self.m.jdk), str(self.m.flink), str(self.m.lib)))
        self.assertEqual([s["name"] for s in r["steps"]], ["backup", "velox4j", "gluten", "runtime-deps", "pin-jdk"])
        self.assertTrue(all(s["status"] == "todo" for s in r["steps"]))
        backup = self.step(r, "backup")
        self.assertEqual(backup["backup_list"], sorted(OLD_JARS))
        self.assertEqual(backup["plan"][0], f"mkdir -p {self.m.lib}.bak/" + backup["plan"][0].rsplit("/", 1)[1])
        self.assertIn(f"cp {self.m.lib}/{' '.join(sorted(OLD_JARS))} {self.m.lib}.bak/", backup["plan"][1])
        velox4j = self.step(r, "velox4j")
        self.assertEqual(velox4j["plan"][0], f"# JAVA_HOME={self.m.jdk} VELOX4J_VELOX_SOURCE_DIR={self.m.repos / 'velox'} VELOX4J_BUILD_JOBS={min(8, os.cpu_count())}")
        self.assertEqual(velox4j["plan"][1], f"mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true   (in {self.m.repos / 'velox4j'})")
        self.assertEqual(velox4j["plan"][2], f"cp {self.m.repos / 'velox4j'}/target/velox4j-*.jar {self.m.lib}/")
        gluten = self.step(r, "gluten")
        self.assertEqual(gluten["plan"][1], f"#   unzip {self.m.lib}/nexmark-flink-0.3-SNAPSHOT.jar META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml -> <tmp>")
        self.assertEqual(gluten["plan"][2], f"#   mvn install:install-file -Dfile={self.m.lib}/nexmark-flink-0.3-SNAPSHOT.jar -DpomFile=<tmp>/pom.xml")
        self.assertEqual(gluten["plan"][4], f"mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true -pl planner,loader,runtime   (in {self.m.repos / 'gluten/gluten-flink'})")
        self.assertEqual(gluten["plan"][5], f"cp {self.m.repos / 'gluten/gluten-flink'}/planner/target/gluten-flink-planner-*.jar "
                         f"{self.m.repos / 'gluten/gluten-flink'}/runtime/target/gluten-flink-runtime-*.jar "
                         f"{self.m.repos / 'gluten/gluten-flink'}/loader/target/gluten-flink-loader-*.jar {self.m.lib}/")
        deps = self.step(r, "runtime-deps")
        self.assertEqual((deps["found"], deps["missing"]), (list(DEPS[:10]), list(DEPS[10:])))
        self.assertEqual(deps["plan"][0], f"cp {self.m.home}/.m2/repository/com/google/guava/guava/33.4.0-jre/guava-33.4.0-jre.jar {self.m.lib}/")
        self.assertIn("not in ~/.m2", deps["plan"][-1])
        self.assertIn("jackson-datatype-jsr310:2.18.0", deps["plan"][-1])
        pin = self.step(r, "pin-jdk")
        self.assertEqual((pin["status"], pin["plan"]), ("todo", [f'echo "env.java.home: {self.m.jdk}" >> {self.m.flink / "conf/flink-conf.yaml"}']))
        self.assertEqual(r["root_steps"], [])  # the fake lib is writable: no root needed
        _, out, _, _ = self.run_tool()
        self.assertIn(f"stack-build plan  (velox4j {self.m.repos / 'velox4j'}, gluten {self.m.repos / 'gluten'}, JDK {self.m.jdk}, lib {self.m.lib})", out)
        self.assertIn("  backup        TODO    3 jar(s) in lib would be overwritten: gluten-flink-runtime-1.8.0-SNAPSHOT.jar, guava-33.4.0-jre.jar, velox4j-0.0.9-SNAPSHOT.jar", out)
        self.assertIn("  runtime-deps  TODO    10 of 14 runtime dependencies from ~/.m2 -> lib; 4 not in ~/.m2 (warned, not fatal)", out)
        self.assertIn("PLAN: 5 step(s) to apply", out)
        self.assertIn("next: review the plan above, then run stack_build.py apply --yes (the cluster must be restarted afterwards: flink-velox-cluster-switch)", out)
        self.assertEqual(err, "")

    def test_plan_pin_jdk_variants(self):
        self.m.flink_home(conf=f"env.java.home: {self.m.jdk}\n")  # already pinned to the build JDK
        _, _, _, r = self.run_tool("--json")
        pin = self.step(r, "pin-jdk")
        self.assertEqual((pin["status"], pin["action"]), ("skipped", "none"))
        self.assertIn("already pins", pin["detail"])
        other = self.m.sys / "usr" / "lib" / "jvm" / "java-17-other"
        self.m.write(other / "bin" / "javac", "", 0o755)
        self.m.flink_home(conf=f"env.java.home: {other}\n")  # pinned elsewhere: left alone, warned
        _, _, _, r = self.run_tool("--json")
        pin = self.step(r, "pin-jdk")
        self.assertEqual((pin["status"], pin["action"]), ("skipped", "warn"))
        self.assertIn(f"conf pins env.java.home to {other} but this build uses {self.m.jdk}; left untouched (WARNING)", pin["detail"])

    def test_plan_blocked_prerequisites(self):
        shutil.rmtree(self.m.repos / "gluten" / "gluten-flink")
        _, out, _, r = self.run_tool("--json")
        steps = r["steps"]
        self.assertEqual([(s["name"], s["status"]) for s in steps], [("prereqs", "blocked")])
        self.assertIn(f"no usable gluten checkout at {self.m.repos / 'gluten'} (missing gluten-flink/pom.xml); flink-velox-build-env-prepare clones it, or set GLUTEN_HOME",
                      steps[0]["detail"])
        self.assertIn("fix the above (flink-velox-build-env-prepare / flink-velox-runtime-install), then rerun plan", steps[0]["fix"])
        self.assertIn("  prereqs       BLOCKED no usable gluten checkout", self.run_tool()[1])
        self.m.write(self.m.repos / "gluten" / "gluten-flink" / "pom.xml", "x\n")  # gluten is back
        shutil.rmtree(self.m.jdk)
        (self.m.bin / "mvn").unlink()
        (self.m.flink / "bin" / "flink").unlink()
        _, _, _, r = self.run_tool("--json", env=self.m.env(PATH=str(self.m.bin)))
        self.assertIn("no JDK 17", r["steps"][0]["detail"])
        self.assertIn("mvn is not on PATH", r["steps"][0]["detail"])
        self.assertIn("no Flink installation", r["steps"][0]["detail"])

    def test_plan_touches_nothing(self):
        before = self.m.snapshot()
        self.run_tool("--json")
        self.assertEqual(self.m.snapshot(), before)

    # -- apply ----------------------------------------------------------------
    def test_apply_refuses_without_yes(self):
        code, out, _, r = self.run_tool("--json", "apply")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertTrue(r["error_message"].startswith("refused: apply needs --yes"))
        self.assertEqual(self.log(), "")
        self.assertFalse((self.m.lib.parent / "lib.bak").exists())
        self.assertIn("REFUSED: apply needs --yes", self.run_tool("apply")[1])

    def test_apply_refuses_when_lib_needs_root(self):
        env = self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0")
        _, _, _, r = self.run_tool("--json", env=env)
        self.assertEqual(r["next_step"], "as root: review the plan above, then run stack_build.py apply --yes (the cluster must be restarted afterwards: flink-velox-cluster-switch)")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn('need root (lib not writable): run "sudo python3 stack_build.py apply --yes"', r["error_message"])
        self.assertEqual(self.log(), "")

    def test_apply_refuses_blocked_prerequisites(self):
        (self.m.repos / "velox4j" / "pom.xml").unlink()
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("refused: the prereqs step is blocked: no usable velox4j checkout", r["error_message"])

    def test_apply_full_run(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]],
                         [("backup", "done"), ("velox4j", "done"), ("gluten", "done"), ("runtime-deps", "done"), ("pin-jdk", "done")])
        # the previous set is kept under lib.bak/<stamp>/
        backup_dirs = list((self.m.lib.parent / "lib.bak").iterdir())
        self.assertEqual(len(backup_dirs), 1)
        self.assertEqual(sorted(p.name for p in backup_dirs[0].iterdir()), sorted(OLD_JARS))
        # the new set is in lib with glob-matched names (gluten at a version the script never
        # hard-codes); the old set is gone from lib — it lives in lib.bak
        self.assertEqual(sorted(p.name for p in self.m.lib.glob("gluten*")), sorted(n for n in NEW_JARS if n.startswith("gluten")))
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())
        self.assertFalse((self.m.lib / "velox4j-0.1.0-SNAPSHOT-sources.jar").exists())
        # the old gluten runtime and velox4j are gone; guava was backed up and then
        # deployed again from ~/.m2 by the runtime-deps step (same name, new copy)
        for jar in ("gluten-flink-runtime-1.8.0-SNAPSHOT.jar", "velox4j-0.0.9-SNAPSHOT.jar"):
            self.assertFalse((self.m.lib / jar).exists(), jar)
        self.assertEqual((self.m.lib / "guava-33.4.0-jre.jar").read_text(encoding="utf-8"), "jar\n")
        for dep in DEPS[:10]:
            g, a, v = dep.split(":")
            self.assertTrue((self.m.lib / f"{a}-{v}.jar").exists(), dep)
        for dep in DEPS[10:]:
            g, a, v = dep.split(":")
            self.assertFalse((self.m.lib / f"{a}-{v}.jar").exists(), dep)
        conf = (self.m.flink / "conf" / "flink-conf.yaml").read_text(encoding="utf-8")
        self.assertEqual(conf, f"rest.port: 8081\nenv.java.home: {self.m.jdk}\n")
        # mvn was driven with the build env, the nexmark pom installed first, in the right directories
        log = self.log()
        self.assertIn(f"mvn install:install-file -Dfile={self.m.lib}/nexmark-flink-0.3-SNAPSHOT.jar -DpomFile=", log)
        self.assertIn(f"mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true JAVA_HOME={self.m.jdk} "
                      f"VELOX4J_VELOX_SOURCE_DIR={self.m.repos / 'velox'} JOBS={min(8, os.cpu_count())} cwd={self.m.repos / 'velox4j'}", log)
        self.assertIn(f"cwd={self.m.repos / 'gluten/gluten-flink'}", log)
        self.assertLess(log.index("install:install-file"), log.index("cwd=" + str(self.m.repos / "gluten/gluten-flink")))
        self.assertEqual([c["action"] for c in r["changes"]], ["backed up", "built+deployed", "installed nexmark-flink into ~/.m2",
                                                               "built+deployed", "deployed", "warned", "configured"])
        self.assertIn("3 jar(s) moved out of lib -> " + str(backup_dirs[0]), r["changes"][0]["detail"])
        self.assertIn("not in ~/.m2: " + ", ".join(DEPS[10:]), r["changes"][5]["detail"])
        self.assertEqual(r["next_step"], "5 step(s) done; restart the cluster so it loads the new build (flink-velox-cluster-switch, needs consent), then run stack_build.py verify")
        report = load_module().render({KEY: r})
        self.assertIn("APPLIED: GFV build deployed into lib", report)
        self.assertIn("    - gluten: built+deployed — gluten-flink-planner-1.9.0-SNAPSHOT.jar, gluten-flink-runtime-1.9.0-SNAPSHOT.jar, gluten-flink-loader-1.9.0-SNAPSHOT.jar ->", report)  # module order
        # rerun: rebuilds again (sources may have changed) and backs up the set it deployed before
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(len(list((self.m.lib.parent / "lib.bak").iterdir())), 2)
        self.assertEqual(self.log().count("install:install-file"), 2)

    def test_apply_only_one_component(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--only", "velox4j")
        self.assertEqual((code, [s["name"] for s in r["steps"]]), (0, ["backup", "velox4j"]))
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())
        self.assertFalse(any(p.name.startswith("gluten-flink-planner") for p in self.m.lib.iterdir()))
        self.assertNotIn("cwd=" + str(self.m.repos / "gluten/gluten-flink"), self.log())
        self.assertNotIn("env.java.home", (self.m.flink / "conf" / "flink-conf.yaml").read_text(encoding="utf-8"))
        self.assertTrue(r["next_step"].endswith("then run stack_build.py verify"))
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--only", "gluten")
        self.assertEqual([s["name"] for s in r["steps"]], ["backup", "gluten"])

    def test_gluten_without_nexmark_jar_skips_the_install_file(self):
        for stale in self.m.lib.glob("*nexmark*.jar"):
            stale.unlink()
        _, _, _, r = self.run_tool("--json")
        gluten = self.step(r, "gluten")
        self.assertEqual(gluten["plan"][0], "# no nexmark jar in lib: the nexmark install-file step is skipped (gluten-flink may fail to resolve it)")
        self.assertIn("; no nexmark jar in lib", gluten["detail"])
        self.run_tool("--json", "apply", "--yes")
        self.assertNotIn("install:install-file", self.log())

    def test_apply_stops_at_a_failed_velox4j_build_and_keeps_the_previous_set(self):
        env = self.m.env(FAKE_MVN_FAIL="velox4j")
        code, out, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]][1:], [("velox4j", "failed"), ("gluten", "todo"), ("runtime-deps", "todo"), ("pin-jdk", "todo")])
        self.assertEqual(self.step(r, "velox4j")["detail"], "mvn build of velox4j failed: [ERROR] velox4j boom")
        self.assertEqual((r["error_message"], [c["action"] for c in r["changes"]]), ("velox4j failed", ["backed up", "failed"]))
        self.assertIn("fix the velox4j step (mvn build of velox4j failed: [ERROR] velox4j boom); the previous lib set is kept under "
                      + str(self.m.lib) + ".bak/, then rerun plan and apply", r["next_step"])
        # the backup ran before the build: the old jars are out of lib but kept under lib.bak
        kept = list((self.m.lib.parent / "lib.bak").iterdir())
        self.assertEqual(sorted(p.name for p in kept[0].iterdir()), sorted(OLD_JARS))
        for jar in OLD_JARS:
            self.assertFalse((self.m.lib / jar).exists(), jar)
        self.assertFalse(any(p.name in NEW_JARS for p in self.m.lib.iterdir()))
        self.assertNotIn("env.java.home", (self.m.flink / "conf" / "flink-conf.yaml").read_text(encoding="utf-8"))
        self.assertIn("NOT APPLIED: velox4j failed; later steps not run", self.run_tool("apply", "--yes", env=env)[1])

    def test_apply_fails_when_the_build_produces_no_jar(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_MVN_NO_JAR="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.step(r, "velox4j")["detail"],
                         f"no velox4j-*.jar under {self.m.repos / 'velox4j'}/target after the build")
        self.assertEqual(r["error_message"], "velox4j failed")
        self.assertFalse((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())

    def test_apply_stops_at_a_failed_gluten_build_after_deploying_velox4j(self):
        env = self.m.env(FAKE_MVN_FAIL="gluten")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]][1:3], [("velox4j", "done"), ("gluten", "failed")])
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())
        self.assertFalse(any(p.name.startswith("gluten-flink-planner") for p in self.m.lib.iterdir()))
        self.assertEqual([c["action"] for c in r["changes"]], ["backed up", "built+deployed", "installed nexmark-flink into ~/.m2", "failed"])

    # -- verify ---------------------------------------------------------------
    def test_verify_runs_lib_state_check_with_expect_gfv(self):
        self.m.fake_family()
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"], r["command"]), (0, True, "verify"))
        self.assertEqual(r["checks"], [{"name": "lib-state", "status": "ok", "detail": "build=gfv; 0 item(s) missing; next: stub", "missing": []}])
        self.assertIn("VERIFIED: lib holds the complete GFV build", self.run_tool("verify")[1])
        code, _, _, r = self.run_tool("--json", "verify", env=self.m.env(FAKE_CHECK_FAIL="lib-state"))
        self.assertEqual((code, r["error_message"], r["checks"][0]["detail"]), (1, "1 check(s) failing", "build=partial; 1 item(s) missing; next: stub"))

    def test_verify_reports_missing_sibling(self):
        _, _, _, r = self.run_tool("--json", "verify")
        self.assertEqual(r["checks"][0]["status"], "skipped")

    # -- shape, --out, usage, crash -------------------------------------------
    def test_json_contract_shape_and_crash_shape(self):
        _, _, _, plan = self.run_tool("--json")
        _, _, _, applied = self.run_tool("--json", "apply", "--yes")
        _, _, _, verified = self.run_tool("--json", "verify")
        for r in (plan, applied, verified):
            self.assertEqual(set(r), {"success", "checked_at", "command", "repos", "jdk", "flink_home", "lib", "steps", "changes",
                                      "root_steps", "next_step", "error_message"} | ({"checks"} if r is verified else set()))
        module = load_module()

        def boom(repos_dir):
            raise RuntimeError("probe exploded")

        module.prereqs = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json", *self.m.args()])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["command"], a["error_message"][:14]), (2, False, "plan", "tool aborted: "))
        self.assertEqual(set(a), set(plan) - {"checks"})
        self.assertEqual((a["steps"], a["changes"], a["lib"]), ([], [], None))

    def test_out_file_matches_stdout_and_is_confined(self):
        archive = self.tmp / "project" / "build.json"
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
        code, out, err, r = self.run_tool("deploy")
        self.assertEqual((code, r), (2, None))
        self.assertIn("usage", err.lower())


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"case": "build-contract", "skill": "flink-velox-stack-build",
              "success": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (RESULTS / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
