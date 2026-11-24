#!/usr/bin/env python3
"""build-contract — tier-A case for flink-velox-stack-build.

Drives scripts/stack_build.py (plan / apply / verify) against a fake machine laid
out in a scratch directory — three source checkouts, a fake JDK 17, a Flink home
whose lib/ holds an older GlutenFlink set plus a real-zip nexmark jar carrying the
nexmark-flink pom, a fake ~/.m2 with the 14 runtime dependencies, a fake PATH whose
mvn / git / runuser are sh stubs (mvn creates target jars named by the checkout it
runs in — gluten at a version the script must not hard-code — logs JAVA_HOME and
the velox source dir, can be made to fail per component, to "succeed" without a
product, or to populate ~/.m2 the way a first build does; git answers read-only
questions about a checkout; runuser records, then runs the command as that user
when the case runs as root — a real uid switch — and as-is otherwise) and a fake
flink-velox-env family directory with a lib-state-check stub (or, where the
sibling's own FLINK_HOME fallback matters, the real lib-state-check copied in)
— with nothing from the real machine in play: the script only ever runs in a
child process that carries
the fake environment (ENV_PREPARE_SYSROOT / ENV_PREPARE_HOME / ENV_PREPARE_EUID /
ENV_PREPARE_PREFIX_WRITABLE / ENV_PREPARE_ENV_FAMILY_DIR), and the commands that
are made to fail are broken inside that child process too. Pins the plan (exact
mvn / cp / conf commands, the backup set scoped to what is rebuilt, nexmark
install-file, runtime deps, pin-jdk variants and the config file Flink reads, the
source of every build on record), the checkout prerequisites following --only
(a gluten-only build plans without the velox / velox4j checkouts and a
velox4j-only one without gluten — no checkout no step of the requested build
reads blocks it, the full build still wants all three; and a gluten-only build
resolving velox4j from ~/.m2, blocked when the artifact the gluten-flink pom
names is missing or unreadable), the apply contract (--yes required, root required
when lib is not writable, stop at the first failing build with the replaced set
kept under lib.bak/, idempotent rerun, --only leaving what it does not rebuild in
place, a runtime dependency still missing after the builds failing the step, sudo
handing the Maven builds to the invoking user — with a real uid switch when
the case runs as root, so the directory root creates for that build is seen to be
reachable by it — an explicit --flink-home being the only candidate, the suggested
commands running as printed), verify (an explicit --flink-home that is not a Flink
installation refused like apply — the sibling check never falls back to another
Flink on the machine — and a valid explicit target the only one checked), the JSON
shape (also for a crash), --out safety (checked
before anything runs, a fifo is never opened) and that plan touches nothing.

Writes results/result.json; exit 0 iff all tests pass.
"""
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
GIT_HEAD = "0123456789abcdef0123456789abcdef01234567"

MVN_STUB = textwrap.dedent('''\
    # fake mvn: creates the target jars of the checkout it runs in; logs the build env;
    # fails the way Maven does when it cannot read or write where it has to
    cannot_write() { echo "[ERROR] Build failed: cannot write under $PWD" >&2; exit 1; }
    name="$(basename "$PWD")"
    echo "mvn $* JAVA_HOME=$JAVA_HOME VELOX4J_VELOX_SOURCE_DIR=${VELOX4J_VELOX_SOURCE_DIR:-} JOBS=${VELOX4J_BUILD_JOBS:-} cwd=$PWD" >> "$FAKE_LOG"
    case " $* " in
      *" install:install-file "*)
        # Maven has to read the pom from the directory it was started in
        for a in "$@"; do case "$a" in -DpomFile=*) pom="${a#-DpomFile=}" ;; esac; done
        [ -r "$pom" ] || { echo "[ERROR] Non-readable POM: $pom" >&2; exit 1; }
        exit 0 ;;
    esac
    # a first build downloads what ~/.m2 lacks
    if [ "${FAKE_MVN_FILL_M2:-}" = 1 ] && [ "$name" = velox4j ]; then
      for p in com/fasterxml/jackson/core/jackson-core/2.18.0/jackson-core-2.18.0 \\
               com/fasterxml/jackson/core/jackson-databind/2.18.0/jackson-databind-2.18.0 \\
               com/fasterxml/jackson/datatype/jackson-datatype-jdk8/2.18.0/jackson-datatype-jdk8-2.18.0 \\
               com/fasterxml/jackson/datatype/jackson-datatype-jsr310/2.18.0/jackson-datatype-jsr310-2.18.0; do
        mkdir -p "$HOME/.m2/repository/$(dirname "$p")"; : > "$HOME/.m2/repository/$p.jar"
      done
    fi
    case "$name" in
      velox4j) [ "${FAKE_MVN_FAIL:-}" = velox4j ] && { echo "[ERROR] velox4j boom" >&2; exit 1; }
               [ "${FAKE_MVN_NO_JAR:-}" = 1 ] && exit 0   # "success" without a product
               mkdir -p target && : > target/velox4j-0.1.0-SNAPSHOT.jar && : > target/velox4j-0.1.0-SNAPSHOT-sources.jar || cannot_write ;;
      gluten-flink) [ "${FAKE_MVN_FAIL:-}" = gluten ] && { echo "[ERROR] gluten boom" >&2; exit 1; }
               { mkdir -p planner/target runtime/target loader/target \
                 && : > planner/target/gluten-flink-planner-1.9.0-SNAPSHOT.jar \
                 && : > runtime/target/gluten-flink-runtime-1.9.0-SNAPSHOT.jar \
                 && : > loader/target/gluten-flink-loader-1.9.0-SNAPSHOT.jar; } || cannot_write ;;
    esac
    exit 0
    ''')
GIT_STUB = textwrap.dedent(f'''\
    # fake git: read-only questions about a checkout
    echo "$@" >> "$FAKE_LOG"
    dir="."; [ "$1" = "-C" ] && {{ dir="$2"; shift 2; }}
    case "$1" in
      rev-parse) echo "{GIT_HEAD}"; exit 0 ;;
      remote) [ "$2" = get-url ] && echo "${{FAKE_GIT_ORIGIN:-https://github.com/example/repo.git}}"; exit 0 ;;
      status) [ "${{FAKE_GIT_DIRTY:-}}" = 1 ] && echo " M pom.xml"; exit 0 ;;
    esac
    exit 0
    ''')
RUNUSER_STUB = textwrap.dedent('''\
    # fake runuser: -u <user> -- cmd ... -> record, then run the command as that user when the
    # case runs as root (a real uid switch: the command meets the permission boundary a
    # demoted build meets on a real machine — a directory root keeps to itself is out of its
    # reach); otherwise as-is.
    echo "runuser $*" >> "$FAKE_LOG"
    user="$2"; shift 3
    uid="$(id -u "$user" 2>/dev/null)"
    if [ "$(id -u 2>/dev/null)" = 0 ] && [ -n "$uid" ] && command -v setpriv >/dev/null 2>&1; then
      HOME="$(getent passwd "$user" | cut -d: -f6)"; export HOME FAKE_RUN_AS="$user"
      exec setpriv --reuid="$uid" --regid="$(id -g "$user")" --clear-groups "$@"
    fi
    FAKE_RUN_AS="$user"; export FAKE_RUN_AS
    exec "$@"
    ''')
CHECK_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    ok = os.environ.get("FAKE_CHECK_FAIL") != "lib-state"
    print(json.dumps({"flink_velox_lib_state_check_result": {"success": ok, "missing": [] if ok else ["build: partial"],
                                                             "lib_state": {"build": "gfv" if ok else "partial"}, "next_step": "stub",
                                                             "flink_home_seen": os.environ.get("FLINK_HOME")}}))
    sys.exit(0 if ok else 1)
    ''')
# Helper process for fault injection: runs the script as __main__ with subprocess.run raising
# for the commands the test names ("mvn=timeout").
FAULTY_RUNNER = textwrap.dedent('''\
    import runpy, subprocess, sys
    real_run = subprocess.run
    faults = dict(item.split("=", 1) for item in sys.argv[1].split(","))

    def faulty(argv, *args, **kwargs):
        for head, kind in faults.items():
            if str(argv[0]).endswith(head) or str(argv[0]).endswith("/" + head):
                if kind == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 120)
                raise FileNotFoundError(2, "No such file or directory", str(argv[0]))
        return real_run(argv, *args, **kwargs)

    subprocess.run = faulty
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name="__main__")
    ''')


def load_module():
    spec = importlib.util.spec_from_file_location("build_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dep_file(home, dep):
    g, a, v = dep.split(":")
    return Path(home) / ".m2" / "repository" / g.replace(".", "/") / a / v / f"{a}-{v}.jar"


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
        self.script(self.bin / "git", GIT_STUB)
        self.script(self.bin / "runuser", RUNUSER_STUB)
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
        self.write(self.repos / "gluten" / "gluten-flink" / "pom.xml",
                   "<project><properties><velox4j.version>0.1.0-SNAPSHOT</velox4j.version></properties></project>\n")

    def m2_velox4j(self, version="0.1.0-SNAPSHOT"):
        # what a machine that has built velox4j once holds in ~/.m2 (the artifact gluten-flink resolves)
        self.write(self.home / ".m2" / "repository" / "io" / "github" / "zhztheplayer" / "velox4j"
                   / version / f"velox4j-{version}.jar", "jar\n")

    def flink_home(self, old_jars=OLD_JARS, nexmark=True, conf="rest.port: 8081\n", conf_name="flink-conf.yaml", also=None):
        self.script(self.flink / "bin" / "flink")
        self.write(self.lib / "flink-dist-1.19.2.jar", "jar\n")
        for jar in old_jars:
            self.write(self.lib / jar, "old\n")
        if nexmark:
            with zipfile.ZipFile(self.lib / "nexmark-flink-0.3-SNAPSHOT.jar", "w") as zf:
                zf.writestr("META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml", "<project/>")
        for stale in self.flink.glob("conf/*.yaml"):
            stale.unlink()
        if conf is not None:
            self.write(self.flink / "conf" / conf_name, conf)
        if also:
            self.write(self.flink / "conf" / also[0], also[1])

    def m2(self, deps):
        for dep in deps:
            self.write(dep_file(self.home, dep), "jar\n")

    def fake_family(self, root=None):
        self.script(Path(root or self.family) / "flink-velox-lib-state-check" / "scripts" / "lib_state_check.py", CHECK_STUB)

    def real_family(self, root=None):
        """The family dir holds the real lib-state-check, not the stub: the fallback the sibling
        does on an empty FLINK_HOME (PATH, /opt/flink, /usr/local/flink) is a behavior of the
        real script, and it is what verify must never set off."""
        src = ROOT.parent.parent / "flink-velox-env" / "flink-velox-lib-state-check" / "scripts" / "lib_state_check.py"
        dst = Path(root or self.family) / "flink-velox-lib-state-check" / "scripts" / "lib_state_check.py"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def env(self, **overrides):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys), "ENV_PREPARE_HOME": str(self.home),
               "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family), "ENV_PREPARE_EUID": "1000", "HOME": str(self.home), "LC_ALL": "C",
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
        self.m.m2(DEPS)  # all fourteen in ~/.m2
        self.m.m2_velox4j()  # and velox4j: this machine has built it before (a gluten-only build resolves it)

    def run_tool(self, *args, env=None, cwd=None, timeout=120):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args, *self.m.args()], capture_output=True, text=True,
                              errors="replace", env=self.m.env() if env is None else env,
                              cwd=str(self.tmp if cwd is None else cwd), stdin=subprocess.DEVNULL, timeout=timeout)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def run_tool_with_faults(self, faults, *args):
        """The script in its own process with subprocess.run failing for chosen commands. Faults
        are always injected in a child process that carries the fake environment: the script never
        runs inside this test process, where the real environment could point it at the machine."""
        runner = self.tmp / "faulty_runner.py"
        runner.write_text(FAULTY_RUNNER, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(runner), faults, str(SCRIPT), *args, *self.m.args()], capture_output=True,
                              text=True, errors="replace", env=self.m.env(), cwd=str(self.tmp), stdin=subprocess.DEVNULL, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr, json.loads(proc.stdout)[KEY]

    def step(self, result, name):
        return next(s for s in result["steps"] if s["name"] == name)

    def log(self):
        return self.m.log.read_text(encoding="utf-8") if self.m.log.exists() else ""

    def reviewed(self, sub, yes=False, extra=""):
        """The command next_step should give: complete (interpreter and the script's own path),
        the repos dir this case passes being explicit."""
        return f"python3 {SCRIPT} {sub} {'--yes ' if yes else ''}--repos-dir {self.m.repos}{extra}"

    def backup_dirs(self):
        return sorted((self.m.lib.parent / "lib.bak").iterdir(), key=lambda p: p.name)

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
        self.assertEqual((velox4j["source"], velox4j["velox_source"]), ({'origin': None, 'head': None, 'dirty': None}, {'origin': None, 'head': None, 'dirty': None}))
        self.assertIn("; source not a git checkout / velox not a git checkout", velox4j["detail"])
        gluten = self.step(r, "gluten")
        self.assertEqual(gluten["plan"][1], f"#   unzip {self.m.lib}/nexmark-flink-0.3-SNAPSHOT.jar META-INF/maven/com.github.nexmark/nexmark-flink/pom.xml -> <tmp>")
        self.assertEqual(gluten["plan"][2], f"#   mvn install:install-file -Dfile={self.m.lib}/nexmark-flink-0.3-SNAPSHOT.jar -DpomFile=<tmp>/pom.xml")
        self.assertEqual(gluten["plan"][4], f"mvn clean install -DskipTests -pl planner,loader,runtime -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true   (in {self.m.repos / 'gluten/gluten-flink'})")
        self.assertEqual(gluten["plan"][5], f"cp {self.m.repos / 'gluten/gluten-flink'}/planner/target/gluten-flink-planner-*.jar "
                         f"{self.m.repos / 'gluten/gluten-flink'}/runtime/target/gluten-flink-runtime-*.jar "
                         f"{self.m.repos / 'gluten/gluten-flink'}/loader/target/gluten-flink-loader-*.jar {self.m.lib}/")
        deps = self.step(r, "runtime-deps")
        self.assertEqual((deps["found"], deps["missing"]), (list(DEPS), []))
        self.assertEqual(len(deps["plan"]), 14)
        self.assertEqual(deps["plan"][0], f"cp {self.m.home}/.m2/repository/com/google/guava/guava/33.4.0-jre/guava-33.4.0-jre.jar {self.m.lib}/")
        pin = self.step(r, "pin-jdk")
        self.assertEqual((pin["status"], pin["conf"], pin["plan"]), ("todo", str(self.m.flink / "conf/flink-conf.yaml"),
                                                                    [f'echo "env.java.home: {self.m.jdk}" >> {self.m.flink / "conf/flink-conf.yaml"}']))
        self.assertEqual(r["root_steps"], [])  # the fake lib is writable: no root needed
        self.assertEqual(r["next_step"], "review the plan above, then run " + self.reviewed("apply", yes=True)
                         + " (the cluster must be restarted afterwards: flink-velox-cluster-switch)")
        _, out, _, _ = self.run_tool()
        self.assertIn(f"stack-build plan  (velox4j {self.m.repos / 'velox4j'}, gluten {self.m.repos / 'gluten'}, JDK {self.m.jdk}, lib {self.m.lib})", out)
        self.assertIn("  backup        TODO    3 jar(s) in lib would be overwritten by this build: gluten-flink-runtime-1.8.0-SNAPSHOT.jar, guava-33.4.0-jre.jar, velox4j-0.0.9-SNAPSHOT.jar", out)
        self.assertIn("  runtime-deps  TODO    14 of 14 runtime dependencies in ~/.m2 now, re-checked after the builds -> lib", out)
        self.assertIn("PLAN: 5 step(s) to apply", out)
        self.assertIn("next: review the plan above, then run " + self.reviewed("apply", yes=True), out)
        self.assertEqual(err, "")

    def test_plan_shows_what_is_missing_from_the_m2_snapshot(self):
        for dep in DEPS[10:]:
            dep_file(self.m.home, dep).unlink()
        _, out, _, r = self.run_tool("--json")
        deps = self.step(r, "runtime-deps")
        self.assertEqual((deps["status"], deps["found"], deps["missing"]), ("todo", list(DEPS[:10]), list(DEPS[10:])))
        self.assertEqual(len(deps["plan"]), 11)  # ten copies …
        self.assertIn("# not in ~/.m2 now (the builds above populate it; still missing after them = this step fails): " + ", ".join(DEPS[10:]), deps["plan"][-1])  # … and that note
        self.assertIn("  runtime-deps  TODO    10 of 14 runtime dependencies in ~/.m2 now, re-checked after the builds -> lib; 4 not there yet", self.run_tool()[1])

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
        self.assertIn(f"{self.m.flink / 'conf/flink-conf.yaml'} pins env.java.home to {other} but this build uses {self.m.jdk}; left untouched (WARNING)", pin["detail"])

    def test_plan_pin_jdk_targets_the_config_file_flink_reads(self):
        self.m.flink_home(conf="rest.port: 8081\n", conf_name="config.yaml")  # what a 1.19 distribution ships
        _, _, _, r = self.run_tool("--json")
        pin = self.step(r, "pin-jdk")
        self.assertEqual((pin["status"], pin["conf"]), ("todo", str(self.m.flink / "conf/config.yaml")))
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual((self.m.flink / "conf" / "config.yaml").read_text(encoding="utf-8"),
                         f"rest.port: 8081\nenv.java.home: {self.m.jdk}\n")
        self.assertFalse((self.m.flink / "conf" / "flink-conf.yaml").exists())  # never created: Flink would prefer it over config.yaml
        # both files and the key only in config.yaml: Flink reads flink-conf.yaml, so that is where it goes
        self.m.flink_home(conf="rest.port: 8081\n", also=("config.yaml", "env.java.home: /elsewhere/jdk\n"))
        _, _, _, r = self.run_tool("--json")
        pin = self.step(r, "pin-jdk")
        self.assertEqual((pin["status"], pin["conf"], pin["action"]), ("todo", str(self.m.flink / "conf/flink-conf.yaml"), "append"))

    def test_a_java_17_directory_holding_another_jdk_is_skipped(self):
        decoy = self.m.sys / "usr" / "lib" / "jvm" / "java-17-aaa-decoy"  # sorts first, is not a JDK 17
        self.m.write(decoy / "bin" / "javac", "", 0o755)
        self.m.write(decoy / "release", 'JAVA_VERSION="21.0.3"\n')
        _, _, _, r = self.run_tool("--json")
        self.assertEqual(r["jdk"], str(self.m.jdk))

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

    # -- plan: the checkout prerequisites follow --only -------------------------
    def test_only_gluten_plans_without_the_velox_and_velox4j_checkouts(self):
        """A checkout no step of the requested build reads is not its blocker: --only gluten
        plans and applies with velox and velox4j absent (the gluten build reads no other
        checkout; the velox4j it resolves comes from ~/.m2 — the test below pins that part)."""
        for name in ("velox", "velox4j"):
            shutil.rmtree(self.m.repos / name)
        code, out, _, r = self.run_tool("--json", "--only", "gluten")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("backup", "todo"), ("gluten", "todo")])
        self.assertIn("PLAN: 2 step(s) to apply", self.run_tool("--only", "gluten")[1])
        # the absent checkouts are still on record (where they would be), the same contract shape
        self.assertEqual(r["repos"], {"velox": str(self.m.repos / "velox"), "velox4j": str(self.m.repos / "velox4j"),
                                      "gluten": str(self.m.repos / "gluten")})
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--only", "gluten")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(sorted(n for n in (p.name for p in self.m.lib.glob("gluten*"))),
                         sorted(n for n in NEW_JARS if n.startswith("gluten")))
        self.assertIn("cwd=" + str(self.m.repos / "gluten/gluten-flink"), self.log())

    def test_only_velox4j_plans_without_the_gluten_checkout(self):
        """The other way round: --only velox4j plans and applies with gluten absent."""
        shutil.rmtree(self.m.repos / "gluten")
        code, out, _, r = self.run_tool("--json", "--only", "velox4j")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("backup", "todo"), ("velox4j", "todo")])
        self.assertIn("VELOX4J_VELOX_SOURCE_DIR=" + str(self.m.repos / "velox"), out)  # velox it still reads
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--only", "velox4j")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())
        self.assertNotIn("cwd=" + str(self.m.repos / "gluten/gluten-flink"), self.log())

    def test_the_full_build_still_requires_every_checkout(self):
        """--only all is not filtered: any one of the three checkouts missing still blocks."""
        for missing in ("velox", "velox4j", "gluten"):
            shutil.rmtree(self.m.repos / missing)
            code, _, _, r = self.run_tool("--json")
            self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("prereqs", "blocked")])
            self.assertIn(f"no usable {missing} checkout at {self.m.repos / missing}", r["steps"][0]["detail"])
            self.m.checkouts()  # restore for the next round

    def test_only_gluten_needs_velox4j_built_into_m2(self):
        """gluten-flink resolves velox4j from ~/.m2 (a SNAPSHOT no remote repo serves), so a
        gluten-only build needs velox4j built before: the artifact named by the velox4j.version
        property of gluten-flink/pom.xml, absent → BLOCKED pointing at building velox4j first.
        The full build and --only velox4j never check it (the velox4j step installs it in the
        same run / does not resolve it); a pom that says nothing fails closed."""
        pom = self.m.repos / "gluten" / "gluten-flink" / "pom.xml"
        artifact = self.m.home / ".m2" / "repository" / "io" / "github" / "zhztheplayer" / "velox4j" \
            / "0.1.0-SNAPSHOT" / "velox4j-0.1.0-SNAPSHOT.jar"
        artifact.unlink()
        code, out, _, r = self.run_tool("--json", "--only", "gluten")
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("prereqs", "blocked")])
        self.assertIn("velox4j 0.1.0-SNAPSHOT is not in ~/.m2 (gluten-flink resolves it from there); "
                      "build it first with --only velox4j, or run the full build", r["steps"][0]["detail"])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--only", "gluten")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertTrue(r["error_message"].startswith("refused: the prereqs step is blocked"))
        self.assertEqual(self.log(), "")  # nothing ran
        # the modes that do not resolve velox4j from ~/.m2 are not held up by its absence
        code, _, _, r = self.run_tool("--json")
        self.assertEqual((code, [s["name"] for s in r["steps"]]),
                         (0, ["backup", "velox4j", "gluten", "runtime-deps", "pin-jdk"]))  # the full build installs velox4j before gluten
        code, _, _, r = self.run_tool("--json", "--only", "velox4j")
        self.assertEqual((code, [s["name"] for s in r["steps"]]), (0, ["backup", "velox4j"]))
        # velox4j built → a gluten-only build plans again; a pom without the property fails closed
        self.m.m2_velox4j()
        code, _, _, r = self.run_tool("--json", "--only", "gluten")
        self.assertEqual([s["name"] for s in r["steps"]], ["backup", "gluten"])
        self.m.write(pom, "x\n")
        code, _, _, r = self.run_tool("--json", "--only", "gluten")
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("prereqs", "blocked")])
        self.assertIn(f"cannot read the velox4j version from {pom} (gluten-flink resolves velox4j "
                      "from ~/.m2); update this skill to match the gluten-flink pom", r["steps"][0]["detail"])

    def test_explicit_flink_home_is_the_only_candidate(self):
        typo = self.tmp / "no-such-flink"
        _, out, _, r = self.run_tool("--json", "--flink-home", str(typo))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("prereqs", "blocked")])
        self.assertEqual(r["steps"][0]["detail"], f"--flink-home {typo} is not a Flink installation (no bin/flink)")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--flink-home", str(typo))
        self.assertEqual((code, self.log(), (self.m.lib.parent / "lib.bak").exists()), (2, "", False))  # FLINK_HOME is not fallen back to
        other = self.m.sys / "opt" / "other-flink"
        self.m.script(other / "bin" / "flink")
        self.m.write(other / "lib" / "flink-dist-1.19.2.jar", "jar\n")
        _, _, _, r = self.run_tool("--json", "--flink-home", str(other))
        self.assertEqual((r["flink_home"], r["lib"], self.step(r, "backup")["status"]), (str(other), str(other / "lib"), "skipped"))
        self.assertEqual(r["next_step"], "review the plan above, then run " + self.reviewed("apply", yes=True, extra=f" --flink-home {other}")
                         + " (the cluster must be restarted afterwards: flink-velox-cluster-switch)")

    def test_the_source_of_every_build_is_on_record(self):
        self.m.write(self.m.repos / "velox4j" / ".git" / "HEAD", "ref: refs/heads/main\n")  # a git checkout, with local work in it
        env = self.m.env(FAKE_GIT_ORIGIN="https://github.com/example/velox4j.git", FAKE_GIT_DIRTY="1")
        _, out, _, r = self.run_tool("--json", env=env)
        self.assertEqual(self.step(r, "velox4j")["source"], {"origin": "https://github.com/example/velox4j.git", "head": GIT_HEAD, "dirty": True})
        self.assertIn(f"; source https://github.com/example/velox4j.git @ {GIT_HEAD[:12]} with local modifications / velox not a git checkout",
                      self.step(r, "velox4j")["detail"])
        self.assertIn(f"; source not a git checkout", self.step(r, "gluten")["detail"])
        self.assertIn("@ 0123456789ab with local modifications", out)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual(code, 0)
        built = next(c for c in r["changes"] if c["step"] == "velox4j")
        self.assertEqual(built["detail"], f"velox4j-0.1.0-SNAPSHOT.jar -> {self.m.lib} (source: https://github.com/example/velox4j.git @ {GIT_HEAD[:12]} with local modifications)")

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
        self.assertEqual(r["next_step"], "as root: review the plan above, then run " + self.reviewed("apply", yes=True)
                         + " (the cluster must be restarted afterwards: flink-velox-cluster-switch)")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn("need root (lib not writable): sudo " + self.reviewed("apply", yes=True), r["error_message"])
        self.assertEqual(self.log(), "")

    def test_the_suggested_commands_run_as_printed(self):
        """next_step / the sudo hint give complete commands: interpreter and the script's own
        path, so that copying them into a shell from any directory works (the bare file name is
        neither on PATH nor in the working directory)."""
        self.m.fake_family()
        _, _, _, r = self.run_tool("--json")
        self.assertTrue(str(SCRIPT).startswith(str(ROOT) + os.sep))  # the skill's real location, not a relative name
        # the plan's command, taken verbatim from next_step, pasted into a shell elsewhere
        command = r["next_step"].split("then run ", 1)[1].split(" (the cluster", 1)[0]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("APPLIED: GlutenFlink build deployed into lib", proc.stdout)
        # the command suggested after a successful build, again verbatim
        _, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertTrue(r["next_step"].endswith(f"restart the cluster so it loads the new build (flink-velox-cluster-switch, needs consent), "
                                                f"then run {self.reviewed('verify')}"))
        command = r["next_step"].split("then run ", 1)[1]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("VERIFIED: lib holds the complete GlutenFlink build", proc.stdout)

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
        # the replaced set is kept under lib.bak/<stamp>/, one entry per jar on record
        dirs = self.backup_dirs()
        self.assertEqual(len(dirs), 1)
        self.assertEqual(sorted(p.name for p in dirs[0].iterdir()), sorted(OLD_JARS))
        self.assertEqual([(c["action"], c["detail"]) for c in r["changes"][:1]],
                         [("backed up", f"{self.m.lib}/gluten-flink-runtime-1.8.0-SNAPSHOT.jar -> {dirs[0]}/gluten-flink-runtime-1.8.0-SNAPSHOT.jar")])
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
        for dep in DEPS:
            self.assertTrue((self.m.lib / dep_file(self.m.home, dep).name).exists(), dep)
        conf = (self.m.flink / "conf" / "flink-conf.yaml").read_text(encoding="utf-8")
        self.assertEqual(conf, f"rest.port: 8081\nenv.java.home: {self.m.jdk}\n")
        # mvn was driven with the build env, the nexmark pom installed first, in the right directories
        log = self.log()
        self.assertIn(f"mvn install:install-file -Dfile={self.m.lib}/nexmark-flink-0.3-SNAPSHOT.jar -DpomFile=", log)
        self.assertIn(f"mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true JAVA_HOME={self.m.jdk} "
                      f"VELOX4J_VELOX_SOURCE_DIR={self.m.repos / 'velox'} JOBS={min(8, os.cpu_count())} cwd={self.m.repos / 'velox4j'}", log)
        self.assertIn(f"cwd={self.m.repos / 'gluten/gluten-flink'}", log)
        self.assertLess(log.index("install:install-file"), log.index("cwd=" + str(self.m.repos / "gluten/gluten-flink")))
        self.assertNotIn("runuser", log)  # not running through sudo: nothing to hand back
        self.assertEqual([c["action"] for c in r["changes"]], ["backed up"] * 3 + ["built+deployed", "installed nexmark-flink into ~/.m2",
                                                                         "built+deployed", "deployed", "configured"])
        self.assertEqual(r["changes"][6]["detail"], f"14 runtime dependencies from {self.m.home / '.m2/repository'} -> {self.m.lib}")
        self.assertEqual(r["next_step"], f"5 step(s) done; restart the cluster so it loads the new build (flink-velox-cluster-switch, needs consent), "
                         f"then run {self.reviewed('verify')}")
        report = load_module().render({KEY: r})
        self.assertIn("APPLIED: GlutenFlink build deployed into lib", report)
        self.assertIn("    - gluten: built+deployed — gluten-flink-planner-1.9.0-SNAPSHOT.jar, gluten-flink-runtime-1.9.0-SNAPSHOT.jar, gluten-flink-loader-1.9.0-SNAPSHOT.jar ->", report)  # module order
        # rerun: rebuilds again (sources may have changed) and backs up the set it deployed before
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(len(self.backup_dirs()), 2)
        self.assertEqual(self.log().count("install:install-file"), 2)

    def test_dependencies_a_first_build_downloads_are_deployed(self):
        for dep in DEPS[10:]:  # not in ~/.m2 before the first build …
            dep_file(self.m.home, dep).unlink()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_MVN_FILL_M2="1"))  # … the velox4j build fetches them
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual((self.step(r, "runtime-deps")["status"], r["changes"][-2]["action"]), ("done", "deployed"))
        self.assertEqual(r["changes"][-2]["detail"], f"14 runtime dependencies from {self.m.home / '.m2/repository'} -> {self.m.lib}")
        for dep in DEPS:
            self.assertTrue((self.m.lib / dep_file(self.m.home, dep).name).exists(), dep)

    def test_a_runtime_dependency_still_missing_after_the_builds_fails_the_step(self):
        for dep in DEPS[12:]:
            dep_file(self.m.home, dep).unlink()
        code, out, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "runtime-deps failed"))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]][2:], [("gluten", "done"), ("runtime-deps", "failed"), ("pin-jdk", "todo")])
        self.assertEqual(r["changes"][-1], {"step": "runtime-deps", "action": "failed", "detail": "not in ~/.m2 after the builds: " + ", ".join(DEPS[12:])})
        self.assertTrue(r["next_step"].startswith("do NOT restart the cluster: lib holds the GlutenFlink jars without all their runtime dependencies"))
        self.assertTrue(r["next_step"].endswith(f"then rerun plan and {self.reviewed('apply', yes=True)}"))
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())  # the builds before it did deploy
        for dep in DEPS[:12]:
            self.assertTrue((self.m.lib / dep_file(self.m.home, dep).name).exists(), dep)
        for dep in DEPS[12:]:
            self.assertFalse((self.m.lib / dep_file(self.m.home, dep).name).exists(), dep)
        self.assertNotIn("env.java.home", (self.m.flink / "conf" / "flink-conf.yaml").read_text(encoding="utf-8"))
        self.assertIn("NOT APPLIED: runtime-deps failed; later steps not run", self.run_tool("apply", "--yes")[1])

    def test_apply_only_one_component(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--only", "velox4j")
        self.assertEqual((code, [s["name"] for s in r["steps"]]), (0, ["backup", "velox4j"]))
        self.assertEqual(self.step(r, "backup")["backup_list"], ["velox4j-0.0.9-SNAPSHOT.jar"])  # only what is rebuilt
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())
        self.assertFalse((self.m.lib / "velox4j-0.0.9-SNAPSHOT.jar").exists())
        # what is not rebuilt is neither moved nor touched
        for jar in ("gluten-flink-runtime-1.8.0-SNAPSHOT.jar", "guava-33.4.0-jre.jar"):
            self.assertEqual((self.m.lib / jar).read_text(encoding="utf-8"), "old\n", jar)
        self.assertEqual(sorted(p.name for p in self.backup_dirs()[0].iterdir()), ["velox4j-0.0.9-SNAPSHOT.jar"])
        self.assertNotIn("cwd=" + str(self.m.repos / "gluten/gluten-flink"), self.log())
        self.assertNotIn("env.java.home", (self.m.flink / "conf" / "flink-conf.yaml").read_text(encoding="utf-8"))
        self.assertEqual(r["next_step"], f"2 step(s) done; restart the cluster so it loads the new build (flink-velox-cluster-switch, needs consent), "
                         f"then run {self.reviewed('verify', extra=' --only velox4j')}")
        # the plan says what stays before anything runs
        _, out, _, _ = self.run_tool("--only", "gluten")
        self.assertIn("1 jar(s) in lib would be overwritten by this build: gluten-flink-runtime-1.8.0-SNAPSHOT.jar; "
                      "not rebuilt, left in lib: velox4j-0.1.0-SNAPSHOT.jar; the runtime dependencies stay in lib", out)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--only", "gluten")
        self.assertEqual((code, [s["name"] for s in r["steps"]]), (0, ["backup", "gluten"]))
        self.assertEqual(self.step(r, "backup")["backup_list"], ["gluten-flink-runtime-1.8.0-SNAPSHOT.jar"])
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())  # from the first run, left in place
        self.assertTrue((self.m.lib / "guava-33.4.0-jre.jar").exists())
        self.assertEqual(sorted(n for n in (p.name for p in self.m.lib.glob("gluten*"))),
                         sorted(n for n in NEW_JARS if n.startswith("gluten")))
        self.assertNotIn("--only", self.run_tool("--json")[3]["next_step"])  # the default needs no flag


    def test_under_sudo_the_maven_builds_run_as_the_invoking_user(self):
        env = self.m.env(ENV_PREPARE_EUID="0", SUDO_USER="builder")
        for name in ("velox4j", "gluten"):  # git checkouts: their provenance is asked for
            self.m.write(self.m.repos / name / ".git" / "HEAD", "ref: refs/heads/main\n")
        _, _, _, r = self.run_tool("--json", env=env)
        self.assertEqual(self.step(r, "velox4j")["plan"][1], f"runuser -u builder -- mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true   (in {self.m.repos / 'velox4j'})")
        self.assertEqual(self.step(r, "gluten")["plan"][4], f"runuser -u builder -- mvn clean install -DskipTests -pl planner,loader,runtime -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true   (in {self.m.repos / 'gluten/gluten-flink'})")
        self.assertIn("runuser -u builder -- mvn install:install-file", self.step(r, "gluten")["plan"][2])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"]), (0, True))
        log = self.log()
        self.assertEqual([l for l in log.splitlines() if l.startswith("runuser -u builder -- mvn ")].__len__(), 3)  # install-file, velox4j, gluten
        self.assertIn(f"runuser -u builder -- mvn clean install -DskipTests -pl planner,loader,runtime -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true\n", log)
        self.assertIn(f"mvn clean install -DskipTests -Dgpg.skip -Dgpg.skip=true -Dmaven.javadoc.skip=true JAVA_HOME={self.m.jdk}", log)  # the env reached Maven
        self.assertEqual(len(self.backup_dirs()), 1)  # the backup and the deploy are the root part
        self.assertTrue((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").exists())
        raw = self.m.log.read_text(encoding="utf-8")  # nor does root run git inside their checkout
        self.assertIn(f"runuser -u builder -- git -C {self.m.repos / 'velox4j'} rev-parse HEAD", raw)
        self.assertNotIn(f"\ngit -C {self.m.repos / 'velox4j'} rev-parse HEAD", raw)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("setpriv"), "switching to another uid needs root and setpriv")
    def test_under_sudo_the_demoted_build_reaches_what_it_must(self):
        """The permission boundary of a real sudo run, played for real: the Maven builds run
        as another uid (the fake runuser switches when the case runs as root). The checkouts
        are that user's; the directory root creates for the install-file pom is handed over,
        so the demoted build can read it; the deploy into lib stays with root."""
        import pwd as pwd_module
        try:
            entry = pwd_module.getpwnam("nobody")
        except KeyError:
            self.skipTest("no 'nobody' account on this machine")
        self.tmp.chmod(0o755)  # the demoted build has to reach the fake machine
        self.m.log.write_text("", encoding="utf-8")
        self.m.log.chmod(0o666)
        for path in (self.m.repos / "velox4j", self.m.repos / "gluten", self.m.repos / "gluten" / "gluten-flink"):
            os.chown(path, entry.pw_uid, entry.pw_gid)  # the invoking user's own checkouts
        env = self.m.env(ENV_PREPARE_EUID="0", SUDO_USER="nobody")
        code, _, err, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"], err), (0, True, ""))
        self.assertEqual([s["status"] for s in r["steps"]], ["done"] * 5)
        self.assertEqual(sum(1 for l in self.log().splitlines() if l.startswith("runuser -u nobody -- mvn ")), 3)
        # what the user built belongs to the user; what root deployed into lib belongs to root
        self.assertEqual((self.m.repos / "velox4j" / "target").lstat().st_uid, entry.pw_uid)
        self.assertEqual((self.m.lib / "velox4j-0.1.0-SNAPSHOT.jar").lstat().st_uid, 0)

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
        self.assertEqual((r["error_message"], [c["action"] for c in r["changes"]]), ("velox4j failed", ["backed up"] * 3 + ["failed"]))
        self.assertEqual(r["next_step"], "fix the velox4j step (mvn build of velox4j failed: [ERROR] velox4j boom); the replaced set is kept under "
                         + str(self.m.lib) + ".bak/, then rerun plan and " + self.reviewed("apply", yes=True))
        # the backup ran before the build: the old jars are out of lib but kept under lib.bak
        kept = self.backup_dirs()
        self.assertEqual(sorted(p.name for p in kept[0].iterdir()), sorted(OLD_JARS))
        for jar in OLD_JARS:
            self.assertFalse((self.m.lib / jar).exists(), jar)
        self.assertFalse(any(p.name in NEW_JARS for p in self.m.lib.iterdir()))
        self.assertNotIn("env.java.home", (self.m.flink / "conf" / "flink-conf.yaml").read_text(encoding="utf-8"))
        self.assertIn("NOT APPLIED: velox4j failed; later steps not run", self.run_tool("apply", "--yes", env=env)[1])

    def test_a_build_command_that_cannot_run_is_the_step_failure_and_keeps_the_log(self):
        code, _, _, r = self.run_tool_with_faults("mvn=timeout", "--json", "apply", "--yes")
        self.assertEqual((code, r["error_message"], self.step(r, "velox4j")["status"]), (1, "velox4j failed", "failed"))
        self.assertIn("TimeoutExpired", self.step(r, "velox4j")["detail"])
        self.assertEqual([c["action"] for c in r["changes"]], ["backed up"] * 3 + ["failed"])
        self.assertEqual(r["changes"][-1]["detail"], self.step(r, "velox4j")["detail"].split("; ")[-1])
        self.assertEqual(sorted(p.name for p in self.backup_dirs()[0].iterdir()), sorted(OLD_JARS))

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
        self.assertEqual([c["action"] for c in r["changes"]],
                         ["backed up"] * 3 + ["built+deployed", "installed nexmark-flink into ~/.m2", "failed"])

    # -- verify ---------------------------------------------------------------
    def test_verify_runs_lib_state_check_with_expect_gfv(self):
        self.m.fake_family()
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"], r["command"]), (0, True, "verify"))
        self.assertEqual(r["checks"], [{"name": "lib-state", "skill": "flink-velox-lib-state-check", "status": "ok",
                                        "detail": "build=gfv; 0 item(s) missing; next: stub", "missing": []}])
        self.assertEqual(r["repos"], {"velox": str(self.m.repos / "velox"), "velox4j": str(self.m.repos / "velox4j"), "gluten": str(self.m.repos / "gluten")})
        self.assertIn("VERIFIED: lib holds the complete GlutenFlink build", self.run_tool("verify")[1])
        code, _, _, r = self.run_tool("--json", "verify", env=self.m.env(FAKE_CHECK_FAIL="lib-state"))
        self.assertEqual((code, r["error_message"], r["checks"][0]["detail"]), (1, "1 check(s) failing", "build=partial; 1 item(s) missing; next: stub"))

    def test_verify_reports_a_missing_sibling_as_not_verified(self):
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(r["checks"], [{"name": "lib-state", "skill": "flink-velox-lib-state-check", "status": "missing",
                                        "detail": "flink-velox-lib-state-check is not installed next to this skill"}])
        self.assertEqual(r["error_message"], "1 check(s) not run (sibling skills not installed): flink-velox-lib-state-check")
        self.assertEqual(r["next_step"], "install the missing check skill(s) (flink-velox-lib-state-check), then rerun " + self.reviewed("verify"))
        report = self.run_tool("verify")[1]
        self.assertIn("NOT VERIFIED: 1 check(s) not run (sibling skills not installed): flink-velox-lib-state-check", report)
        self.assertNotIn("\nVERIFIED", report)

    def test_verify_reports_the_repos_dir_it_was_given(self):
        elsewhere = self.tmp / "elsewhere"  # not the default ~/repos
        for name in ("velox", "velox4j", "gluten"):
            shutil.copytree(self.m.repos / name, elsewhere / name)
        self.m.fake_family()
        proc = subprocess.run([sys.executable, str(SCRIPT), "--json", "verify", "--repos-dir", str(elsewhere)], env=self.m.env(),
                              cwd=str(self.tmp), capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        r = json.loads(proc.stdout)[KEY]
        self.assertEqual((proc.returncode, r["repos"]["velox"], r["repos"]["gluten"]),
                         (0, str(elsewhere / "velox"), str(elsewhere / "gluten")))
        self.assertEqual(r["next_step"], "")  # verified: nothing left to do

    def test_verify_finds_the_sibling_in_the_flat_install_layout(self):
        # install.sh copies every skill to <config>/skills/<skill>/: no family dir, resolution goes by skill name
        skills = self.tmp / "config" / "skills"
        shutil.copytree(ROOT, skills / "flink-velox-stack-build", ignore=shutil.ignore_patterns("results", "__pycache__"))
        self.m.fake_family(skills)
        proc = subprocess.run([sys.executable, str(skills / "flink-velox-stack-build" / "scripts" / "stack_build.py"), "--json", "verify", *self.m.args()],
                              env=self.m.env(ENV_PREPARE_ENV_FAMILY_DIR=None), cwd=str(self.tmp), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        r = json.loads(proc.stdout)[KEY]
        self.assertEqual((proc.returncode, r["checks"][0]["status"]), (0, "ok"))

    def test_verify_refuses_an_explicit_flink_home_that_is_not_an_installation(self):
        """The machine holds another Flink the sibling check would fall back to on an empty
        FLINK_HOME (PATH, /opt/flink, /usr/local/flink) — and that one holds the complete
        GlutenFlink build, so the fallback would print VERIFIED for a Flink the caller never named (the
        real sibling, not the stub: the fallback is its behavior). verify refuses like apply
        instead: exit 2, REFUSED, and no checks at all — the sibling never runs."""
        self.m.real_family()
        self.m.flink_home(old_jars=NEW_JARS + tuple(dep_file(self.m.home, d).name for d in DEPS), nexmark=False)
        not_flink = self.tmp / "not-a-flink"  # a directory that is not a Flink installation
        not_flink.mkdir()
        self.m.write(not_flink / "flink", "no bin/flink here\n")
        for bad in (not_flink, self.tmp / "gone-flink"):  # … and a path that does not exist
            code, out, err, r = self.run_tool("--json", "verify", "--flink-home", str(bad))
            self.assertEqual((code, r["success"], r["flink_home"]), (2, False, None))
            self.assertEqual(r["error_message"], f"refused: --flink-home {bad} is not a Flink installation (no bin/flink)")
            self.assertIn("--flink-home", r["next_step"])
            self.assertNotIn("checks", r)  # no verdict about another Flink: the sibling never ran
            report = self.run_tool("verify", "--flink-home", str(bad))[1]
            self.assertNotIn("VERIFIED", report)
            self.assertIn(f"REFUSED: --flink-home {bad} is not a Flink installation (no bin/flink)", report)
            self.assertIn("next: point --flink-home at the Flink installation to verify", report)

    def test_verify_with_an_explicit_flink_home_checks_that_one_only(self):
        """A valid explicit target is the only candidate verify ever reports: the sibling
        check gets it as FLINK_HOME and reports its state. Its complete GlutenFlink build is VERIFIED
        while the discoverable Flink sits half-empty (an empty FLINK_HOME would fail), and
        its half-empty set is NOT VERIFIED while the discoverable Flink holds the complete
        build (an empty FLINK_HOME would pass)."""
        self.m.real_family()
        complete = self.tmp / "other-flink"  # the explicit target: the complete GlutenFlink set
        self.m.script(complete / "bin" / "flink")
        for jar in NEW_JARS + tuple(dep_file(self.m.home, d).name for d in DEPS):
            self.m.write(complete / "lib" / jar, "jar\n")
        code, _, _, r = self.run_tool("--json", "verify", "--flink-home", str(complete))
        self.assertEqual((code, r["success"], r["flink_home"]), (0, True, str(complete)))
        self.assertEqual(r["checks"][0]["status"], "ok")
        self.assertIn("VERIFIED: lib holds the complete GlutenFlink build", self.run_tool("verify", "--flink-home", str(complete))[1])
        self.m.flink_home(old_jars=NEW_JARS + tuple(dep_file(self.m.home, d).name for d in DEPS), nexmark=False)
        partial = self.tmp / "half-a-flink"  # the explicit target is half-empty now; the discoverable one is complete
        self.m.script(partial / "bin" / "flink")
        self.m.write(partial / "lib" / "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "half a stack\n")
        code, out, _, r = self.run_tool("--json", "verify", "--flink-home", str(partial))
        self.assertEqual((code, r["success"], r["flink_home"]), (1, False, str(partial)))
        self.assertEqual(r["checks"][0]["status"], "fail")
        report = self.run_tool("verify", "--flink-home", str(partial))[1]
        self.assertIn("lib-state: build=partial", report)  # the verdict is about the named Flink
        self.assertIn("missing velox4j, gluten-flink-planner, gluten-flink-loader", report)
        self.assertIn("NOT VERIFIED: 1 check(s) failing", report)

    # -- shape, --out, usage, crash -------------------------------------------
    def test_json_contract_shape_and_crash_shape(self):
        _, _, _, plan = self.run_tool("--json")
        _, _, _, applied = self.run_tool("--json", "apply", "--yes")
        _, _, _, verified = self.run_tool("--json", "verify")
        for r in (plan, applied, verified):
            self.assertEqual(set(r), {"success", "checked_at", "command", "repos", "jdk", "flink_home", "lib", "steps", "changes",
                                      "root_steps", "next_step", "error_message"} | ({"checks"} if r is verified else set()))
        module = load_module()

        def boom(repos_dir, flink_home=None):
            raise RuntimeError("probe exploded")

        module.prereqs = boom
        buffer = io.StringIO()
        # in-process on purpose (to intercept the prerequisite probe) — inside the fake environment, so
        # nothing the script could reach for points at the real machine
        with patch.dict(os.environ, self.m.env(), clear=True), redirect_stdout(buffer):
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

    def test_out_never_opens_a_fifo_and_is_checked_before_apply(self):
        project = self.tmp / "project"
        project.mkdir()
        fifo = project / "pipe.json"
        os.mkfifo(str(fifo))
        # open() on a fifo blocks until a reader appears: the guard must refuse instead
        code, _, err, r = self.run_tool("--json", "--out", str(fifo), "--overwrite", timeout=60)
        self.assertEqual((code, r["success"], stat.S_ISFIFO(fifo.lstat().st_mode)), (2, False, True))
        self.assertIn("exists and is not a regular file, refusing to overwrite it", err)
        self.assertTrue(r["error_message"].startswith("refused: --out exists and is not a regular file"))
        # a bad --out is refused before anything runs: apply leaves the machine untouched
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        before = self.m.snapshot()
        code, _, err, r = self.run_tool("--json", "apply", "--yes", "--out", str(self.tmp / "outside.json"), cwd=str(elsewhere))
        self.assertEqual((code, r["success"], r["steps"], r["changes"]), (2, False, [], []))
        self.assertTrue(r["error_message"].startswith("refused: --out must be a regular path inside the project directory"))
        self.assertEqual((self.m.snapshot(), self.log()), (before, ""))
        self.assertFalse((self.m.lib.parent / "lib.bak").exists())

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
