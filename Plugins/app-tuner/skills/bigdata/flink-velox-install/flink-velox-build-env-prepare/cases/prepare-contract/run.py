#!/usr/bin/env python3
"""prepare-contract — tier-A case for flink-velox-build-env-prepare.

Drives scripts/build_env_prepare.py (plan / apply / verify) against a fake
machine laid out in a scratch directory — a sysroot (os-release, /usr/local
artifacts), a fake HOME, a fake PATH whose rpm / dnf / git are sh stubs driven
by files (an installed-package list, a failure switch, a clone log), a fake
velox setup script whose install_* functions create /usr/local traces under the
sysroot, and a fake flink-velox-env family directory with stub check scripts
for verify — with nothing from the real machine in play; the script's seams
ENV_PREPARE_SYSROOT / ENV_PREPARE_HOME / ENV_PREPARE_EUID /
ENV_PREPARE_ENV_FAMILY_DIR carry the fake. Pins the plan (exact commands,
deferred source-deps list, skipped steps), the apply contract (--yes required,
root required for root steps, unsupported OS refused, idempotent rerun, stop at
the first failing step with a change log, blocked incomplete checkout never
deleted), verify aggregation, the JSON shape (also for a crash), --out safety
and that plan touches nothing.

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
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "build_env_prepare.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_build_env_prepare_result"

# pinned independently of the script
PACKAGES = ("git", "cmake", "gcc", "gcc-c++", "maven",
            "ninja-build", "ccache", "autoconf", "automake", "libtool", "flex", "bison", "python3", "patchelf",
            "libevent-devel", "openssl-devel", "re2-devel", "zstd-devel", "lz4-devel", "double-conversion-devel",
            "libdwarf-devel", "elfutils-libelf-devel", "libcurl-devel", "libicu-devel", "libsodium-devel",
            "zlib-devel", "librdkafka-devel", "java-17-openjdk-devel")
REPOS = {"velox": ("https://github.com/bigo-sg/velox.git", "gluten-20260829", ("CMakeLists.txt", "scripts")),
         "velox4j": ("https://github.com/bigo-sg/velox4j.git", "gluten-20260829", ("pom.xml",)),
         "gluten": ("https://github.com/apache/gluten.git", "main", ("gluten-flink/pom.xml",))}
SOURCE_LIBS = ["gflags", "glog", "fmt"]

RPM_STUB = textwrap.dedent('''\
    # fake rpm: -q <pkg> answers from the installed-package list
    [ "$1" = "-q" ] || exit 2
    grep -qxF "$2" "$FAKE_RPM_DB" && exit 0 || exit 1
    ''')
DNF_STUB = textwrap.dedent('''\
    # fake dnf: install -y <pkgs...> appends to the installed list; FAKE_DNF_FAIL makes it fail
    echo "$@" >> "$FAKE_LOG"
    [ "${FAKE_DNF_FAIL:-}" = 1 ] && { echo "Error: fake repository unreachable" >&2; exit 1; }
    shift   # install
    [ "$1" = "-y" ] && shift
    for p in "$@"; do echo "$p" >> "$FAKE_RPM_DB"; done
    exit 0
    ''')
GIT_STUB = textwrap.dedent('''\
    # fake git: clone --quiet -b <branch> <url> <dest> lays down a checkout with its marker files
    echo "$@" >> "$FAKE_LOG"
    [ "$1" = "clone" ] || exit 0
    [ "$2" = "--quiet" ] && shift
    shift   # clone
    [ "$1" = "-b" ] && shift 2
    url="$1"; dest="$2"; name="${dest##*/}"
    [ "${FAKE_GIT_CLONE_FAIL:-}" = "$name" ] && { echo "fatal: unable to access '$url'" >&2; exit 128; }
    case "$name" in
      velox)    mkdir -p "$dest/scripts"; : > "$dest/CMakeLists.txt"
                cp "$FAKE_VELOX_SETUP" "$dest/scripts/setup-centos9.sh" ;;
      velox4j)  mkdir -p "$dest"; : > "$dest/pom.xml" ;;
      gluten)   mkdir -p "$dest/gluten-flink"; : > "$dest/gluten-flink/pom.xml" ;;
    esac
    mkdir -p "$dest/.git"; echo "ref: refs/heads/main" > "$dest/.git/HEAD"
    ''')
VELOX_SETUP = textwrap.dedent('''\
    #!/bin/bash
    function install_velox_deps_from_dnf() { :; }
    function install_velox_deps {
      run_and_time install_velox_deps_from_dnf
      run_and_time install_conda
      run_and_time install_gflags
      run_and_time install_glog
      run_and_time install_fmt
    }
    function install_gflags() { mkdir -p "$INSTALL_PREFIX/lib/cmake/gflags"; }
    function install_glog() { [ "${FAKE_SD_FAIL:-}" = glog ] && { echo "cmake: boom" >&2; return 1; }; mkdir -p "$INSTALL_PREFIX/include/glog"; }
    function install_fmt() { : > "$INSTALL_PREFIX/include/fmt.h"; }
    ''')
CHECK_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    name = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ok = os.environ.get("FAKE_CHECK_FAIL") != name
    key = "flink_velox_" + name.replace("flink-velox-", "").replace("-", "_") + "_result"
    print(json.dumps({key: {"success": ok, "missing": [] if ok else ["x: missing"], "next_step": "stub"}}))
    sys.exit(0 if ok else 1)
    ''')


def load_module():
    spec = importlib.util.spec_from_file_location("prepare_under_test", str(SCRIPT))
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
        for d in (self.bin, self.home, self.sys / "etc"):
            d.mkdir(parents=True)
        self.rpm_db = self.root / "rpm-db"
        self.rpm_db.write_text("", encoding="utf-8")
        self.log = self.root / "log"
        self.velox_setup = self.root / "setup-centos9.sh"
        self.write(self.velox_setup, VELOX_SETUP, 0o644)
        self.write(self.sys / "etc" / "os-release", 'ID="openEuler"\nVERSION_ID="24.03"\n')
        self.script(self.bin / "rpm", RPM_STUB)
        self.script(self.bin / "dnf", DNF_STUB)
        self.script(self.bin / "git", GIT_STUB)
        self.family = self.root / "flink-velox-env"

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def os_release(self, os_id, version):
        self.write(self.sys / "etc" / "os-release", f'ID="{os_id}"\nVERSION_ID="{version}"\n')

    def installed(self, *packages):
        self.rpm_db.write_text("\n".join(packages) + ("\n" if packages else ""), encoding="utf-8")

    def checkout(self, name, home=None, without=()):
        home = Path(home) if home else self.repos / name
        _, _, markers = REPOS[name]
        for marker in markers:
            if marker == "scripts":
                self.write(home / marker / "setup-centos9.sh", VELOX_SETUP)
            elif marker not in without:
                self.write(home / marker, "x\n")
        self.write(home / ".git" / "HEAD", "ref: refs/heads/main\n")
        return home

    def source_lib(self, lib):
        local = self.sys / "usr" / "local"
        if lib == "gflags":
            (local / "lib" / "cmake" / "gflags").mkdir(parents=True, exist_ok=True)
        elif lib == "glog":
            (local / "include" / "glog").mkdir(parents=True, exist_ok=True)
        else:
            self.write(local / "include" / "fmt.h", "")

    def fake_family(self):
        """Stub check scripts under the real skill / script names verify expects."""
        for skill, script in (("flink-velox-toolchain-check", "toolchain_check.py"),
                              ("flink-velox-build-tools-check", "build_tools_check.py"),
                              ("flink-velox-cpp-deps-check", "cpp_deps_check.py"),
                              ("flink-velox-jdk-check", "jdk_check.py"),
                              ("flink-velox-source-repos-check", "source_repos_check.py")):
            self.script(self.family / skill / "scripts" / script, CHECK_STUB)

    def env(self, **overrides):
        # the fake bin comes first so rpm / dnf / git resolve to the stubs; the system
        # paths follow only for the coreutils the stubs themselves use (grep, mkdir, cp)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys),
               "ENV_PREPARE_HOME": str(self.home), "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family),
               "ENV_PREPARE_RETRY_SLEEP": "0", "HOME": str(self.home), "LC_ALL": "C",
               "FAKE_RPM_DB": str(self.rpm_db), "FAKE_LOG": str(self.log), "FAKE_VELOX_SETUP": str(self.velox_setup)}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

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


class PrepareCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def run_tool(self, *args, env=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True, errors="replace",
            env=self.m.env() if env is None else env, cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=120)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def step(self, result, name):
        return next(s for s in result["steps"] if s["name"] == name)

    def render_report(self, result):
        """The human report for a JSON result (the renderer is pure)."""
        return load_module().render(result)  # the renderer returns the text

    # -- plan -----------------------------------------------------------------
    def test_plan_on_a_bare_machine(self):
        code, out, err, r = self.run_tool("--json", "--repos-dir", str(self.m.repos))
        self.assertEqual((code, r["success"], r["command"]), (0, True, "plan"))
        self.assertEqual(r["os"], {"id": "openEuler", "version": "24.03", "supported": True, "label": "openEuler"})
        packages, repos, sd = (self.step(r, n) for n in ("packages", "repos", "source-deps"))
        self.assertEqual((packages["status"], packages["needs_root"], packages["missing"]), ("todo", True, list(PACKAGES)))
        self.assertEqual(packages["plan"], ["dnf install -y " + " ".join(PACKAGES)])
        self.assertEqual((repos["status"], repos["needs_root"]), ("todo", False))
        self.assertEqual(repos["plan"], [f"git clone -b {REPOS[n][1]} {REPOS[n][0]} {self.m.repos / n}" for n in REPOS])
        self.assertEqual((sd["status"], sd.get("deferred"), sd["needs_root"]), ("todo", True, True))
        self.assertIn(str(self.m.repos / "velox" / "scripts" / "setup-centos9.sh"), sd["detail"])
        self.assertEqual(r["root_steps"], ["packages", "source-deps"])
        self.assertEqual(r["next_step"], "as root: review the plan above, then run build_env_prepare.py apply --yes")
        _, out, _, _ = self.run_tool("--repos-dir", str(self.m.repos))
        self.assertIn("build-env-prepare plan  (openEuler, supported)", out)
        self.assertIn("  packages     TODO    28 of 28 packages missing: git, cmake, gcc, gcc-c++, maven", out)
        self.assertIn("        dnf install -y git cmake gcc", out)
        self.assertIn("  repos        TODO    3 checkout(s) to clone into", out)
        self.assertIn(f"        git clone -b gluten-20260829 https://github.com/bigo-sg/velox.git {self.m.repos / 'velox'}", out)
        self.assertIn("  source-deps  TODO    library list is parsed from", out)
        self.assertIn("PLAN: 3 step(s) to apply", out)
        self.assertEqual(err, "")

    def test_plan_when_everything_is_present(self):
        self.m.installed(*PACKAGES)
        for name in REPOS:
            self.m.checkout(name)
        for lib in SOURCE_LIBS:
            self.m.source_lib(lib)
        code, out, _, r = self.run_tool("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]],
                         [("packages", "skipped"), ("repos", "skipped"), ("source-deps", "skipped")])
        self.assertEqual(self.step(r, "packages")["detail"], "all 28 packages installed")
        self.assertEqual(self.step(r, "source-deps")["detail"],
                         f"all 3 source-built libraries present under /usr/local (list: {self.m.repos / 'velox/scripts/setup-centos9.sh'})")
        self.assertEqual(r["next_step"], "ready: the build environment is complete (verify with: build_env_prepare.py verify)")
        _, out, _, _ = self.run_tool()
        self.assertIn("PLAN: nothing to do", out)

    def test_plan_marks_blocked_incomplete_checkout(self):
        self.m.installed(*PACKAGES)
        self.m.checkout("velox")
        self.m.checkout("velox4j", without=("pom.xml",))  # a dir without its marker
        self.m.checkout("gluten")
        code, out, _, r = self.run_tool("--json")
        self.assertEqual(self.step(r, "repos")["status"], "blocked")
        self.assertIn(f"{self.m.repos / 'velox4j'} exists but is not a usable velox4j checkout (missing pom.xml)",
                      self.step(r, "repos")["detail"])
        self.assertIn("never deleted automatically", self.step(r, "repos")["fix"])
        self.assertTrue(r["next_step"].startswith("remove or repair the incomplete checkout"))
        self.assertIn("  repos        BLOCKED ", self.run_tool()[1])

    def test_plan_by_distribution(self):
        self.m.os_release("centos", "7")
        _, _, _, r = self.run_tool("--json")
        self.assertEqual((r["os"]["supported"], r["os"]["label"]), (False, "centos 7 (only 9 has a velox setup script)"))
        self.assertIn("not supported for apply", r["next_step"])
        self.m.os_release("centos", "9")
        _, _, _, r = self.run_tool("--json")
        self.assertEqual((r["os"]["supported"], r["os"]["label"]), (True, "CentOS/RHEL 9"))
        self.m.os_release("ubuntu", "22.04")
        self.m.checkout("velox")
        _, _, _, r = self.run_tool("--json")
        self.assertEqual((r["os"]["supported"], r["os"]["label"]), (False, "ubuntu (package names are openEuler's)"))
        self.assertIn("setup-ubuntu.sh", self.step(r, "source-deps")["detail"])

    def test_plan_touches_nothing(self):
        before = self.m.snapshot()
        self.run_tool("--json")
        self.assertEqual(self.m.snapshot(), before)

    # -- apply ----------------------------------------------------------------
    def test_apply_refuses_without_yes(self):
        code, out, err, r = self.run_tool("--json", "apply")
        self.assertEqual((code, r["success"], r["command"]), (2, False, "apply"))
        self.assertTrue(r["error_message"].startswith("refused: apply needs --yes"))
        self.assertFalse((self.m.repos / "velox").exists())
        self.assertEqual(self.m.rpm_db.read_text(encoding="utf-8"), "")
        self.assertIn("REFUSED: apply needs --yes", self.run_tool("apply")[1])

    def test_apply_refuses_root_steps_when_not_root(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn("need root: run \"sudo python3 build_env_prepare.py apply --yes\"", r["error_message"])
        self.assertFalse((self.m.repos / "velox").exists())
        # with the root steps already satisfied (packages installed, velox checked out and its
        # libraries built), a non-root apply only clones the two remaining repos
        self.m.installed(*PACKAGES)
        self.m.checkout("velox")
        for lib in SOURCE_LIBS:
            self.m.source_lib(lib)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_EUID="1000"))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.step(r, "repos")["detail"], "cloned velox4j, gluten")
        # but as long as source-deps still needs root, a non-root run is refused
        (self.m.sys / "usr" / "local" / "include" / "fmt.h").unlink()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn("steps source-deps need root", r["error_message"])

    def test_apply_refuses_unsupported_os(self):
        self.m.os_release("ubuntu", "22.04")
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("ubuntu (package names are openEuler's) is not supported for apply", r["error_message"])
        self.assertFalse((self.m.repos / "velox").exists())

    def test_apply_refuses_blocked_checkout_without_deleting(self):
        self.m.installed(*PACKAGES)
        self.m.checkout("velox4j", without=("pom.xml",))
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("remove or repair the incomplete checkout by hand", r["error_message"])
        self.assertTrue((self.m.repos / "velox4j" / ".git" / "HEAD").exists())  # untouched

    def test_apply_refuses_without_dnf(self):
        # a PATH without the system directories hides the real dnf (the rpm stub then
        # also cannot answer, which only makes the packages step "todo")
        (self.m.bin / "dnf").unlink()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(PATH=str(self.m.bin)))
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("dnf is not on PATH", r["error_message"])

    def test_apply_full_run_then_idempotent_rerun(self):
        code, out, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]],
                         [("packages", "done"), ("repos", "done"), ("source-deps", "done")])
        self.assertEqual(self.step(r, "source-deps")["detail"], "built 3 source libraries into /usr/local: gflags, glog, fmt")
        self.assertEqual([c["action"] for c in r["changes"]], ["installed", "cloned", "cloned", "cloned", "built", "built", "built"])
        self.assertEqual(r["changes"][0]["detail"], f"installed 28 package(s): {', '.join(PACKAGES)}")
        self.assertEqual(set(self.m.rpm_db.read_text(encoding="utf-8").split()), set(PACKAGES))
        for name in REPOS:
            self.assertTrue((self.m.repos / name / ".git" / "HEAD").exists(), name)
        for trace in ("lib/cmake/gflags", "include/glog", "include/fmt.h"):
            self.assertTrue((self.m.sys / "usr" / "local" / trace).exists(), trace)
        self.assertEqual(r["next_step"], "3 step(s) done, nothing left to do; run build_env_prepare.py verify")
        # the human report of a fresh apply names every change (reconstructed from the JSON run)
        self.assertIn("APPLIED: all steps done or already satisfied", self.render_report({KEY: r}))
        self.assertIn("    - repos: cloned — velox (gluten-20260829) into", self.render_report({KEY: r}))
        self.assertIn("    - source-deps: built — install_glog() -> /usr/local", self.render_report({KEY: r}))
        # rerun: everything is skipped, nothing installed/cloned/built twice
        log_before = self.m.log.read_text(encoding="utf-8")
        code, out, _, r = self.run_tool("apply", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("APPLIED: all steps done or already satisfied", out)
        self.assertIn("    - repos: skipped — velox, velox4j and gluten are checked out under", out)
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([s["status"] for s in r["steps"]], ["skipped"] * 3)
        self.assertEqual([c["action"] for c in r["changes"]], ["skipped"] * 3)
        self.assertEqual(self.m.log.read_text(encoding="utf-8"), log_before)

    def test_apply_only_what_is_missing(self):
        self.m.installed(*PACKAGES[:-1])  # everything but the JDK
        self.m.checkout("velox4j")
        self.m.source_lib("gflags")
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.step(r, "packages")["detail"], "installed 1 package(s): java-17-openjdk-devel")
        self.assertEqual(self.step(r, "repos")["detail"], "cloned velox, gluten")
        self.assertEqual(self.step(r, "source-deps")["detail"], "built 2 source libraries into /usr/local: glog, fmt")

    def test_apply_stops_at_a_failed_package_install(self):
        code, out, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_DNF_FAIL="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("packages", "failed"), ("repos", "todo"), ("source-deps", "todo")])
        self.assertIn("dnf install failed: Error: fake repository unreachable", self.step(r, "packages")["detail"])
        self.assertEqual([c["action"] for c in r["changes"]], ["failed"])
        self.assertEqual(r["error_message"], "packages failed")
        self.assertIn("fix the packages step", r["next_step"])
        self.assertFalse((self.m.repos / "velox").exists())  # later steps not run
        self.assertIn("NOT APPLIED: packages failed; later steps not run", self.run_tool("apply", "--yes", env=self.m.env(FAKE_DNF_FAIL="1"))[1])

    def test_apply_stops_at_a_failed_clone(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_GIT_CLONE_FAIL="velox4j"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("packages", "done"), ("repos", "failed"), ("source-deps", "todo")])
        self.assertIn("git clone of velox4j failed after 3 attempts: fatal: unable to access", self.step(r, "repos")["detail"])
        self.assertTrue((self.m.repos / "velox" / ".git" / "HEAD").exists())   # velox was cloned first
        self.assertFalse((self.m.repos / "gluten").exists())                  # gluten not reached
        self.assertFalse((self.m.repos / "velox4j").exists())                 # failed clone left nothing behind
        self.assertFalse((self.m.sys / "usr" / "local" / "lib" / "cmake" / "gflags").exists())
        # the clone step is retried, not skipped, on the next run
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, self.step(r, "repos")["status"]), (0, "done"))

    def test_apply_stops_at_a_failed_source_library_and_resumes(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_SD_FAIL="glog"))
        self.assertEqual((code, r["success"]), (1, False))
        sd = self.step(r, "source-deps")
        self.assertEqual(sd["status"], "failed")
        self.assertIn("install_glog failed (cmake: boom); later libraries build against it — stopped", sd["detail"])
        self.assertEqual((sd["built"], sd["missing"]), (["gflags"], ["gflags", "glog", "fmt"]))
        self.assertTrue((self.m.sys / "usr" / "local" / "lib" / "cmake" / "gflags").exists())
        self.assertFalse((self.m.sys / "usr" / "local" / "include" / "fmt.h").exists())
        # resume: gflags is detected, only glog and fmt are built
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, self.step(r, "source-deps")["detail"]), (0, "built 2 source libraries into /usr/local: glog, fmt"))

    # -- verify ---------------------------------------------------------------
    def test_verify_aggregates_the_env_family_checks(self):
        self.m.fake_family()
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"], r["command"]), (0, True, "verify"))
        self.assertEqual([(i["name"], i["status"]) for i in r["checks"]],
                         [("toolchain", "ok"), ("build-tools", "ok"), ("cpp-deps", "ok"), ("jdk", "ok"), ("source-repos", "ok")])
        self.assertEqual(r["next_step"], "")
        self.assertIn("VERIFIED: every build-environment check passes", self.run_tool("verify")[1])
        code, out, _, r = self.run_tool("--json", "verify", env=self.m.env(FAKE_CHECK_FAIL="flink-velox-cpp-deps-check"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.step_like(r, "cpp-deps")["detail"], "1 item(s) missing; next: stub")
        self.assertIn("NOT VERIFIED: 1 check(s) failing", self.run_tool("verify", env=self.m.env(FAKE_CHECK_FAIL="flink-velox-cpp-deps-check"))[1])

    def test_verify_reports_missing_siblings(self):
        code, _, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([i["status"] for i in r["checks"]], ["skipped"] * 5)
        self.assertIn("is not installed next to this skill", r["checks"][0]["detail"])

    def step_like(self, result, name):
        return next(i for i in result["checks"] if i["name"] == name)

    # -- shape, --out, usage, crash -------------------------------------------
    def test_json_contract_shape_and_crash_shape(self):
        self.run_tool("--json")
        _, _, _, plan = self.run_tool("--json")
        _, _, _, applied = self.run_tool("--json", "apply", "--yes")
        _, _, _, verified = self.run_tool("--json", "verify")
        for r in (plan, applied, verified):
            self.assertEqual(set(r), {"success", "checked_at", "command", "os", "repos_dir", "steps", "changes",
                                      "root_steps", "next_step", "error_message"} | ({"checks"} if r is verified else set()))
        self.assertEqual(set(plan["steps"][0]), {"name", "needs_root", "actions", "missing", "present", "status", "detail", "plan"})
        module = load_module()

        def boom():
            raise RuntimeError("probe exploded")

        module.read_os_release = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["command"], a["error_message"][:14]), (2, False, "plan", "tool aborted: "))
        self.assertEqual(set(a), set(plan) - {"checks"})
        self.assertEqual((a["steps"], a["changes"], a["repos_dir"]), ([], [], None))
        self.assertTrue(a["next_step"].startswith("fix the tool error first"))

    def test_out_file_matches_stdout_and_is_confined(self):
        archive = self.tmp / "project" / "prepare.json"
        _, stdout, _, _ = self.run_tool("--json", "--out", str(archive))
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), json.loads(stdout))
        first = archive.read_text(encoding="utf-8")
        code, _, err, _ = self.run_tool("--json", "--out", str(archive))
        self.assertEqual((code, archive.read_text(encoding="utf-8")), (2, first))
        self.assertIn("--overwrite", err)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        outside = self.tmp / "outside.json"
        code, _, err, _ = self.run_tool("--json", "--out", str(outside), cwd=str(elsewhere))
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
    report = {"case": "prepare-contract", "skill": "flink-velox-build-env-prepare",
              "success": result.wasSuccessful(), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (RESULTS / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
