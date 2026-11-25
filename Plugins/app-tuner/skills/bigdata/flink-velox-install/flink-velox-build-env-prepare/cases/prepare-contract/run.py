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
ENV_PREPARE_ENV_FAMILY_DIR carry the fake; the script only ever runs in a child
process that carries that environment, and the commands that are made to fail
are broken inside that child process too. Pins the plan (exact commands,
deferred source-deps list, skipped steps), the apply contract (--yes required,
root required for root steps, unsupported OS refused, idempotent rerun, stop at
the first failing step with a change log, a clone that never removes what is at
the destination, git missing but about to be installed, a library source
directory without a build entry kept rather than wiped, sudo handing the clones
and the git queries to the invoking user — with a real uid switch when the case
runs as root, so the directories root creates for that user are seen to be
reachable by it — the source of the setup script on record, the suggested
commands running as printed), the setup-script fingerprint guard (--plan: the
script root is about to source is re-probed against the plan's record — a script
swapped, a head moved or a checkout vanished between plan and apply is refused
before anything runs, and one swapped while apply runs is refused right before
it sources the script, with the steps that did run on record), verify
aggregation (missing siblings reported as NOT VERIFIED, the ones a pending PR
delivers pointing at that PR, next_step separating missing from failing), the JSON
shape (also for a crash), --out safety (checked before anything runs, a fifo is never opened)
and that plan touches nothing.

Writes results/result.json; exit 0 iff all tests pass.
"""
import hashlib
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
GIT_HEAD = "0123456789abcdef0123456789abcdef01234567"

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
    for p in "$@"; do
      echo "$p" >> "$FAKE_RPM_DB"
      [ "$p" = git ] && [ -n "${FAKE_GIT_STASH:-}" ] && cp "$FAKE_GIT_STASH" "$FAKE_BIN_DIR/git" && chmod 755 "$FAKE_BIN_DIR/git"
    done
    exit 0
    ''')
GIT_STUB = textwrap.dedent('''\
    # fake git: clone --quiet -b <branch> <url> <dest> lays down a checkout with its marker files;
    # -C <dir> rev-parse / remote get-url / status answer from files (read-only provenance queries)
    echo "$@" >> "$FAKE_LOG"
    dir="."; [ "$1" = "-C" ] && { dir="$2"; shift 2; }
    case "$1" in
      rev-parse) echo "${FAKE_GIT_HEAD:-0123456789abcdef0123456789abcdef01234567}"; exit 0 ;;
      remote) [ "$2" = get-url ] && echo "${FAKE_GIT_ORIGIN:-https://github.com/example/repo.git}"; exit 0 ;;
      status) [ -n "${FAKE_GIT_DIRTY:-}" ] && echo " M scripts/setup-centos9.sh"; exit 0 ;;
    esac
    [ "$1" = "clone" ] || exit 0
    [ "$2" = "--quiet" ] && shift
    shift   # clone
    [ "$1" = "-b" ] && shift 2
    url="$1"; dest="$2"; name="${dest##*/}"
    case "$dest" in *.git-clone-*|*/.*-clone-*) ;; *) echo "clone into the destination itself, not a directory of its own: $dest" >&2; exit 99 ;; esac
    [ "${FAKE_GIT_CLONE_FAIL:-}" = "$name" ] && {
      echo "fatal: unable to access '$url'" >&2
      [ "${FAKE_GIT_RACE:-}" = "$name" ] && { mkdir -p "$(dirname "$(dirname "$dest")")/$name"; echo mine > "$(dirname "$(dirname "$dest")")/$name/notes.txt"; }
      exit 128; }
    mkdir -p "$dest" 2>/dev/null || { echo "fatal: could not create work tree dir '$dest': Permission denied" >&2; exit 128; }
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
RUNUSER_STUB = textwrap.dedent('''\
    # fake runuser: -u <user> -- cmd ... -> record, then run the command as that user when the
    # case runs as root (a real uid switch: the command meets the permission boundary a
    # demoted clone meets on a real machine — a directory root keeps to itself is out of its
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
# Helper process for fault injection: runs the script as __main__ with subprocess.run raising
# for the commands the test names ("dnf=timeout").
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
# Helper process for the plan→apply race: while apply runs (inside the fake environment),
# appends to the setup script the moment the named command first runs — a swap after
# apply's own start-of-run probe, before source-deps gets to source the script.
SCRIPT_SWAPPER = textwrap.dedent('''\
    import runpy, subprocess, sys
    marker, target, extra = sys.argv[1], sys.argv[2], sys.argv[3]
    real_run = subprocess.run

    def swapping(argv, *args, **kwargs):
        if str(argv[0]).endswith(marker):
            with open(target, "a") as fh:
                fh.write(extra)
        return real_run(argv, *args, **kwargs)

    subprocess.run = swapping
    sys.argv = sys.argv[4:]
    runpy.run_path(sys.argv[0], run_name="__main__")
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
        self.script(self.bin / "runuser", RUNUSER_STUB)
        for tool, real in (("grep", "/usr/bin/grep"), ("mkdir", "/usr/bin/mkdir"), ("cp", "/usr/bin/cp"), ("chmod", "/usr/bin/chmod"),
                           ("dirname", "/usr/bin/dirname"), ("bash", "/bin/bash")):
            self.script(self.bin / tool, f'exec {real} "$@"\n')  # a PATH of the fake bin alone hides the real rpm / dnf / git
        self.deps_dir = self.root / "source-deps"  # where velox's setup script downloads its libraries
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

    def fake_family(self, root=None):
        """Stub check scripts under the real skill / script names verify expects."""
        for skill, script in (("flink-velox-toolchain-check", "toolchain_check.py"),
                              ("flink-velox-build-tools-check", "build_tools_check.py"),
                              ("flink-velox-cpp-deps-check", "cpp_deps_check.py"),
                              ("flink-velox-jdk-check", "jdk_check.py"),
                              ("flink-velox-source-repos-check", "source_repos_check.py")):
            self.script(Path(root or self.family) / skill / "scripts" / script, CHECK_STUB)

    def env(self, **overrides):
        # the fake bin comes first so rpm / dnf / git resolve to the stubs; the system
        # paths follow only for the coreutils the stubs themselves use (grep, mkdir, cp)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys),
               "ENV_PREPARE_HOME": str(self.home), "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family),
               "ENV_PREPARE_RETRY_SLEEP": "0", "ENV_PREPARE_EUID": "0", "HOME": str(self.home), "LC_ALL": "C",
               "GFVBOT_SOURCE_DEPS_DIR": str(self.deps_dir),
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

    def run_tool(self, *args, env=None, cwd=None, timeout=120):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True, errors="replace",
            env=self.m.env() if env is None else env, cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=timeout)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def run_tool_with_faults(self, faults, *args):
        """The script in its own process with subprocess.run raising for chosen commands. Faults
        are always injected in a child process that carries the fake environment: the script never
        runs inside this test process, where the real environment could point it at the machine."""
        runner = self.tmp / "faulty_runner.py"
        runner.write_text(FAULTY_RUNNER, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(runner), faults, str(SCRIPT), *args], capture_output=True,
                              text=True, errors="replace", env=self.m.env(), cwd=str(self.tmp), stdin=subprocess.DEVNULL, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr, json.loads(proc.stdout)[KEY]

    def repos_dir_arg(self):
        return ["--repos-dir", str(self.m.repos)]

    def step(self, result, name):
        return next(s for s in result["steps"] if s["name"] == name)

    def stub_log(self):
        """The stubs' call log; read-only git queries (rev-parse / get-url / status) left out."""
        if not self.m.log.exists():
            return ""
        return "".join(l for l in self.m.log.read_text(encoding="utf-8").splitlines(True)
                       if not any(f" {q} " in l for q in ("rev-parse", "get-url", "status")))

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
        self.assertEqual(r["next_step"], f"as root: review the plan above, then run python3 {SCRIPT} apply --yes --repos-dir {self.m.repos}")
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
        self.assertEqual(r["next_step"], f"ready: the build environment is complete (verify with: python3 {SCRIPT} verify)")
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
        self.assertIn(f"need root: sudo python3 {SCRIPT} apply --yes", r["error_message"])
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

    # -- the plan→apply fingerprint guard (--plan) -------------------------------
    def fingerprinted_machine(self):
        """Everything but the source-built libraries present: the plan records the setup
        script's fingerprint, and apply would run that script as root with nothing
        else to do."""
        self.m.installed(*PACKAGES)
        for name in REPOS:
            self.m.checkout(name)
        plan = self.tmp / "prepare-plan.json"
        code, _, _, r = self.run_tool("--json", "--out", str(plan), *self.repos_dir_arg())
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([s["status"] for s in r["steps"]], ["skipped", "skipped", "todo"])
        return plan

    def test_apply_refuses_a_setup_script_that_changed_between_plan_and_apply(self):
        """The user consented to the plan's fingerprint; a script swapped in after that
        must not run as root. apply re-probes and refuses before anything runs."""
        plan = self.fingerprinted_machine()
        setup = self.m.repos / "velox" / "scripts" / "setup-centos9.sh"
        before = hashlib.sha256(VELOX_SETUP.encode("utf-8")).hexdigest()
        self.m.write(setup, VELOX_SETUP + "# swapped in after the plan\n")
        after = hashlib.sha256(setup.read_bytes()).hexdigest()
        code, out, err, r = self.run_tool("--json", "apply", "--yes", "--plan", str(plan), *self.repos_dir_arg())
        self.assertEqual((code, r["success"]), (2, False))
        self.assertEqual(r["error_message"], "refused: the setup script changed between plan and apply "
                         f"(setup script sha256 {before[:12]} -> {after[:12]}) — "
                         "rerun plan and review, then apply again")
        self.assertEqual(r["next_step"], r["error_message"][len("refused: "):])
        self.assertIn("REFUSED: the setup script changed between plan and apply",
                      self.run_tool("apply", "--yes", "--plan", str(plan), *self.repos_dir_arg())[1])
        # refused before anything ran: the setup script's install_* stubs were never sourced
        self.assertEqual(self.stub_log(), "")
        self.assertFalse((self.m.sys / "usr" / "local" / "lib" / "cmake" / "gflags").exists())

    def test_apply_with_a_matching_fingerprint_still_runs(self):
        plan = self.fingerprinted_machine()
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--plan", str(plan), *self.repos_dir_arg())
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([s["status"] for s in r["steps"]], ["skipped", "skipped", "done"])
        self.assertIn("source recorded", [c["action"] for c in r["changes"]])
        for trace in ("lib/cmake/gflags", "include/glog", "include/fmt.h"):
            self.assertTrue((self.m.sys / "usr" / "local" / trace).exists(), trace)

    def test_apply_refuses_a_velox_head_that_moved_between_plan_and_apply(self):
        """The script's content is not the whole consent: the checkout it runs from
        (origin, head) is part of the fingerprint and is re-verified too."""
        plan = self.fingerprinted_machine()
        moved = "ffffffffffffffffffffffffffffffffffffffff"
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--plan", str(plan), *self.repos_dir_arg(),
                                      env=self.m.env(FAKE_GIT_HEAD=moved))
        self.assertEqual((code, r["success"]), (2, False))
        self.assertEqual(r["error_message"], "refused: the setup script changed between plan and apply "
                         f"(velox https://github.com/example/repo.git @ {GIT_HEAD[:12]} -> "
                         f"https://github.com/example/repo.git @ {moved[:12]}) — "
                         "rerun plan and review, then apply again")
        self.assertFalse((self.m.sys / "usr" / "local" / "lib" / "cmake" / "gflags").exists())

    def test_apply_refuses_when_the_recorded_velox_checkout_is_gone(self):
        """The checkout the fingerprint was recorded from vanished between plan and
        apply: the re-clone apply would make cannot be shown to bring the consented
        script — refused before anything runs."""
        plan = self.fingerprinted_machine()
        shutil.rmtree(self.m.repos / "velox")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--plan", str(plan), *self.repos_dir_arg())
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("no longer there and apply would re-clone it", r["error_message"])
        self.assertIn("changed between plan and apply", r["error_message"])
        self.assertEqual(sorted(p.name for p in self.m.repos.iterdir()), ["gluten", "velox4j"])
        self.assertEqual(set(self.m.rpm_db.read_text(encoding="utf-8").split()), set(PACKAGES))
        self.assertEqual(self.stub_log(), "")

    def test_the_fingerprint_is_reverified_against_the_script_right_before_it_runs(self):
        """The probe at the start of apply can be minutes old when source-deps runs
        (the package install, the clones): the script root is about to source is probed
        once more, and a swap in between is a refusal — with the steps that did run
        on record in changes."""
        self.m.checkout("velox")  # the plan records a fingerprint; packages, the other two checkouts and the libraries are still to do
        plan = self.tmp / "prepare-plan.json"
        code, _, _, r = self.run_tool("--json", "--out", str(plan), *self.repos_dir_arg())
        self.assertEqual(self.step(r, "source-deps")["status"], "todo")
        swapper = self.tmp / "swap_runner.py"
        swapper.write_text(SCRIPT_SWAPPER, encoding="utf-8")
        setup = self.m.repos / "velox" / "scripts" / "setup-centos9.sh"
        proc = subprocess.run([sys.executable, str(swapper), "dnf", str(setup), "# swapped while apply runs\n",
                               str(SCRIPT), "--json", "apply", "--yes", "--plan", str(plan), *self.repos_dir_arg()],
                              env=self.m.env(), cwd=str(self.tmp), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        r2 = json.loads(proc.stdout)[KEY]
        self.assertEqual((proc.returncode, r2["success"]), (2, False))
        self.assertIn("the setup script changed between plan and apply", r2["error_message"])
        self.assertTrue(setup.read_text(encoding="utf-8").endswith("# swapped while apply runs\n"))  # the swap really happened
        self.assertEqual([c["action"] for c in r2["changes"]], ["installed", "cloned", "cloned"])  # what did run is on record
        self.assertEqual(self.step(r2, "source-deps")["status"], "todo")  # it never ran
        self.assertFalse((self.m.sys / "usr" / "local" / "lib" / "cmake" / "gflags").exists())

    def test_a_plan_record_from_before_the_checkout_existed_applies_as_before(self):
        """A deferred plan (velox not cloned yet) records no fingerprint: apply clones
        and runs as before — the guard has nothing to compare and does not invent one."""
        plan = self.tmp / "prepare-plan.json"
        code, _, _, r = self.run_tool("--json", "--out", str(plan), *self.repos_dir_arg())
        self.assertEqual((self.step(r, "source-deps").get("deferred"), r["success"]), (True, True))
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--plan", str(plan), *self.repos_dir_arg())
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([s["status"] for s in r["steps"]], ["done"] * 3)
        self.assertIn("built", [c["action"] for c in r["changes"]])

    def test_apply_with_an_unreadable_plan_record_refuses(self):
        """--plan claims to hold the consented fingerprint: a file that cannot back that
        claim is a refusal before anything runs, not a silent fall-back."""
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--plan", str(self.tmp / "no-such-plan.json"),
                                      *self.repos_dir_arg())
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("cannot read", r["error_message"])
        self.assertIn("the file plan wrote with --out", r["error_message"])
        bad = self.tmp / "not-a-plan.json"
        bad.write_text('{"hello": 1}\n', encoding="utf-8")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--plan", str(bad), *self.repos_dir_arg())
        self.assertEqual(code, 2)
        self.assertIn("holds no plan result", r["error_message"])
        self.assertEqual(self.m.rpm_db.read_text(encoding="utf-8"), "")  # refused before anything ran

    def test_apply_full_run_then_idempotent_rerun(self):
        code, out, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]],
                         [("packages", "done"), ("repos", "done"), ("source-deps", "done")])
        self.assertEqual(self.step(r, "source-deps")["detail"], "built 3 source libraries into /usr/local: gflags, glog, fmt")
        self.assertEqual([c["action"] for c in r["changes"]],
                         ["installed", "created directory", "cloned", "cloned", "cloned", "source recorded", "built", "built", "built"])
        self.assertEqual(r["changes"][0]["detail"], f"installed 28 package(s): {', '.join(PACKAGES)}")
        self.assertEqual(r["changes"][1]["detail"], str(self.m.repos))  # created by this run: on record
        setup_sha = hashlib.sha256((self.m.repos / "velox" / "scripts" / "setup-centos9.sh").read_bytes()).hexdigest()
        self.assertEqual(r["changes"][5], {"step": "source-deps", "action": "source recorded",
                                           "detail": f"{self.m.repos / 'velox/scripts/setup-centos9.sh'} (sha256 {setup_sha}) "
                                                     f"from https://github.com/example/repo.git @ {GIT_HEAD[:12]}"})
        self.assertEqual((sd := self.step(r, "source-deps"))["source"], {"origin": "https://github.com/example/repo.git", "head": GIT_HEAD, "dirty": False})
        self.assertEqual(sd["setup_sha256"], setup_sha)
        self.assertEqual(set(self.m.rpm_db.read_text(encoding="utf-8").split()), set(PACKAGES))
        for name in REPOS:
            self.assertTrue((self.m.repos / name / ".git" / "HEAD").exists(), name)
        for trace in ("lib/cmake/gflags", "include/glog", "include/fmt.h"):
            self.assertTrue((self.m.sys / "usr" / "local" / trace).exists(), trace)
        self.assertEqual(r["next_step"], f"3 step(s) done, nothing left to do; run python3 {SCRIPT} verify")
        # the human report of a fresh apply names every change (reconstructed from the JSON run)
        self.assertIn("APPLIED: all steps done or already satisfied", self.render_report({KEY: r}))
        self.assertIn("    - repos: cloned — velox (gluten-20260829) into", self.render_report({KEY: r}))
        self.assertIn("    - source-deps: built — install_glog() -> /usr/local", self.render_report({KEY: r}))
        # rerun: everything is skipped, nothing installed/cloned/built twice
        log_before = self.stub_log()
        code, out, _, r = self.run_tool("apply", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("APPLIED: all steps done or already satisfied", out)
        self.assertIn("    - repos: skipped — velox, velox4j and gluten are checked out under", out)
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([s["status"] for s in r["steps"]], ["skipped"] * 3)
        self.assertEqual([c["action"] for c in r["changes"]], ["skipped"] * 3)
        self.assertEqual(self.stub_log(), log_before)

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

    # -- what is never done to the user's files, and who runs what -----------------
    def test_a_failed_clone_leaves_nothing_behind_and_never_removes_a_directory(self):
        env = self.m.env(FAKE_GIT_CLONE_FAIL="velox4j", FAKE_GIT_RACE="velox4j")  # and "someone else" creates the destination meanwhile
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"], self.step(r, "repos")["status"]), (1, False, "failed"))
        # the directory that appeared at the destination is not ours: kept, clone not retried over it
        self.assertEqual((self.m.repos / "velox4j" / "notes.txt").read_text(encoding="utf-8"), "mine\n")
        self.assertEqual(sorted(p.name for p in self.m.repos.iterdir()), ["velox", "velox4j"])  # no clone directory left behind
        self.assertFalse(any(p.name.startswith(".velox4j-clone-") for p in self.m.repos.iterdir()))
        self.assertIn("clone failed", [c["action"] for c in r["changes"]])

    def test_git_that_the_packages_step_installs_does_not_block_the_repos_step(self):
        stash = self.tmp / "git.stash"
        shutil.move(str(self.m.bin / "git"), str(stash))  # no git yet: it is one of the 28 packages
        env = self.m.env(FAKE_GIT_STASH=stash, FAKE_BIN_DIR=self.m.bin, PATH=str(self.m.bin))  # and the real git is not on the PATH
        _, out, _, r = self.run_tool("--json", *self.repos_dir_arg(), env=env)
        repos = self.step(r, "repos")
        self.assertEqual((self.step(r, "packages")["status"], repos["status"]), ("todo", "todo"))  # not blocked on git
        self.assertIn("3 checkout(s) to clone into", repos["detail"])
        self.assertIn("; git is not on PATH yet — the packages step installs it first", repos["detail"])
        self.assertNotIn("BLOCKED", out)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", *self.repos_dir_arg(), env=env)
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("packages", "done"), ("repos", "done"), ("source-deps", "done")])
        # git really missing and not about to be installed (rpm says so, the binary is not there): blocked
        (self.m.bin / "git").unlink()
        _, _, _, r = self.run_tool("--json", *self.repos_dir_arg(), env=env)
        self.assertEqual(self.step(r, "repos")["status"], "blocked")
        self.assertIn("git is not on PATH (installed by the packages step)", self.step(r, "repos")["detail"])
        self.assertIn("  repos        BLOCKED ", self.run_tool(*self.repos_dir_arg(), env=env)[1])

    def test_an_existing_library_directory_without_a_build_entry_is_kept(self):
        stale = self.m.deps_dir / "glog"  # a download cut mid-stream: a directory with nothing buildable in it
        stale.mkdir(parents=True)
        (stale / "half-downloaded.tar").write_text("x\n", encoding="utf-8")
        code, out, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "source-deps failed"))
        sd = self.step(r, "source-deps")
        self.assertEqual((sd["status"], sd["built"]), ("failed", ["gflags"]))
        self.assertIn(f"install_glog not started: {stale} exists and has no build entry file (a download cut mid-stream?) — left in place", sd["detail"])
        self.assertEqual(sorted(p.name for p in stale.iterdir()), ["half-downloaded.tar"])  # not deleted, not touched
        self.assertEqual(r["changes"][-1], {"step": "source-deps", "action": "failed", "detail": f"install_glog: {stale} kept, no build entry file"})
        self.assertTrue((self.m.sys / "usr" / "local" / "lib" / "cmake" / "gflags").exists())  # the library before it was built
        self.assertNotIn("NOT APPLIED: source-deps failed", out)

    def test_the_source_of_the_setup_script_is_on_record(self):
        env = self.m.env(FAKE_GIT_ORIGIN="https://github.com/example/velox.git", FAKE_GIT_DIRTY="1")
        self.m.checkout("velox")
        _, out, _, r = self.run_tool("--json", *self.repos_dir_arg(), env=env)
        sd = self.step(r, "source-deps")
        setup = self.m.repos / "velox" / "scripts" / "setup-centos9.sh"
        sha = hashlib.sha256(setup.read_bytes()).hexdigest()
        self.assertEqual((sd["source"], sd["setup_sha256"]), ({"origin": "https://github.com/example/velox.git", "head": GIT_HEAD, "dirty": True}, sha))
        self.assertIn(f"velox https://github.com/example/velox.git @ {GIT_HEAD[:12]} with local modifications; setup script sha256 {sha[:12]}", sd["detail"])
        self.assertIn("@ 0123456789ab with local modifications", out)
        shutil.rmtree(self.m.repos / "velox" / ".git")  # not a git checkout (an extracted tarball, say)
        _, _, _, r = self.run_tool("--json", *self.repos_dir_arg())
        self.assertEqual(self.step(r, "source-deps")["source"], {"origin": None, "head": None, "dirty": None})
        self.assertIn("velox not a git checkout", self.step(r, "source-deps")["detail"])

    def test_under_sudo_the_clones_run_as_the_invoking_user(self):
        env = self.m.env(SUDO_USER="builder")  # ENV_PREPARE_EUID is 0 by default in the fake
        _, _, _, r = self.run_tool("--json", *self.repos_dir_arg(), env=env)
        self.assertEqual(self.step(r, "repos")["plan"][0], f"runuser -u builder -- git clone -b gluten-20260829 https://github.com/bigo-sg/velox.git {self.m.repos / 'velox'}")
        self.assertIn("        runuser -u builder -- git clone -b ", self.run_tool(*self.repos_dir_arg(), env=env)[1])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", *self.repos_dir_arg(), env=env)
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([l for l in self.stub_log().splitlines() if l.startswith("runuser -u builder -- git clone --quiet -b")].__len__(), 3)
        self.assertEqual(r["changes"][2]["detail"], f"velox (gluten-20260829) into {self.m.repos / 'velox'} as builder")
        self.assertNotIn("\ngit clone", self.stub_log())  # never as root
        raw = self.m.log.read_text(encoding="utf-8")  # nor do the provenance queries inside their checkout
        self.assertIn(f"runuser -u builder -- git -C {self.m.repos / 'velox'} rev-parse HEAD", raw)
        self.assertNotIn(f"\ngit -C {self.m.repos / 'velox'} rev-parse HEAD", raw)
        (self.m.bin / "runuser").unlink()
        shutil.rmtree(self.m.repos / "gluten")  # something left to clone …
        code, _, _, r = self.run_tool("--json", "apply", "--yes", *self.repos_dir_arg(), env=self.m.env(SUDO_USER="builder", PATH=str(self.m.bin)))
        self.assertEqual(code, 2)  # … but nothing to hand the clone to
        self.assertIn("runuser is not on PATH (needed to run git as the invoking user)", r["error_message"])

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("setpriv"), "switching to another uid needs root and setpriv")
    def test_under_sudo_the_demoted_user_reaches_what_root_created_for_it(self):
        """The permission boundary of a real sudo run, played for real: the clone runs as
        another uid (the fake runuser switches when the case runs as root), so a directory
        root keeps to itself makes it fail — the temporary clone directory and a repos
        directory created by this run are handed to that user instead."""
        import pwd as pwd_module
        try:
            nobody = pwd_module.getpwnam("nobody").pw_uid
        except KeyError:
            self.skipTest("no 'nobody' account on this machine")
        self.assertFalse(self.m.repos.exists())  # created by the run itself
        self.tmp.chmod(0o755)  # the demoted clone has to reach the fake machine
        self.m.log.write_text("", encoding="utf-8")
        self.m.log.chmod(0o666)
        env = self.m.env(SUDO_USER="nobody")
        code, _, err, r = self.run_tool("--json", "apply", "--yes", *self.repos_dir_arg(), env=env)
        self.assertEqual((code, r["success"], err), (0, True, ""))
        self.assertEqual([s["status"] for s in r["steps"]], ["done", "done", "done"])
        # the repos directory root created and the checkouts cloned into it belong to that user
        self.assertEqual(self.m.repos.lstat().st_uid, nobody)
        for name in REPOS:
            self.assertEqual((self.m.repos / name).lstat().st_uid, nobody, name)
        self.assertEqual([c["action"] for c in r["changes"]][1:3], ["created directory", "cloned"])
        self.assertEqual(r["changes"][1]["detail"], f"{self.m.repos} for nobody")
        self.assertEqual(sorted(p.name for p in self.m.repos.iterdir()), sorted(REPOS))  # no clone directory left behind
        self.assertEqual(sum(1 for l in self.stub_log().splitlines() if l.startswith("runuser -u nobody -- git clone --quiet -b")), 3)

    def test_a_step_command_that_cannot_run_is_the_step_failure_and_keeps_the_log(self):
        code, _, _, r = self.run_tool_with_faults("dnf=timeout", "--json", "apply", "--yes", *self.repos_dir_arg())
        self.assertEqual((code, r["error_message"], self.step(r, "packages")["status"]), (1, "packages failed", "failed"))
        self.assertIn("TimeoutExpired", self.step(r, "packages")["detail"])
        self.assertEqual([c["action"] for c in r["changes"]], ["failed"])
        self.assertFalse((self.m.repos / "velox").exists())
        code, _, _, r = self.run_tool_with_faults("git=missing", "--json", "apply", "--yes", *self.repos_dir_arg())
        self.assertEqual((code, r["error_message"]), (1, "repos failed"))
        self.assertIn("FileNotFoundError", self.step(r, "repos")["detail"])
        # the package install made before it stays on record
        self.assertEqual([c["action"] for c in r["changes"]], ["installed", "created directory", "failed"])
        self.assertIn("git", self.m.rpm_db.read_text(encoding="utf-8").split())
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("packages", "done"), ("repos", "failed"), ("source-deps", "todo")])
        self.assertIn(f"then rerun plan and python3 {SCRIPT} apply --yes --repos-dir {self.m.repos}", r["next_step"])

    def test_next_step_and_root_hint_keep_the_reviewed_options(self):
        self.m.checkout("velox")  # so that the source-deps step has its real command list
        _, _, _, r = self.run_tool("--json", "--repos-dir", str(self.m.repos), "--jobs", "3")
        self.assertEqual(r["next_step"], f"as root: review the plan above, then run python3 {SCRIPT} apply --yes --repos-dir "
                         f"{self.m.repos} --jobs 3")
        self.assertIn("BUILD_THREADS=3", self.step(r, "source-deps")["plan"][0])
        _, _, _, r = self.run_tool("--json", "apply", "--yes", "--repos-dir", str(self.m.repos), "--jobs", "3", env=self.m.env(ENV_PREPARE_EUID="1000"))
        self.assertIn(f"need root: sudo python3 {SCRIPT} apply --yes --repos-dir {self.m.repos} --jobs 3", r["error_message"])

    def test_the_suggested_commands_run_as_printed(self):
        """next_step / the sudo hint give complete commands: interpreter and the script's own
        path, so that copying them into a shell from any directory works (the bare file name is
        neither on PATH nor in the working directory)."""
        self.m.fake_family()
        _, _, _, r = self.run_tool("--json", "--repos-dir", str(self.m.repos))
        self.assertTrue(str(SCRIPT).startswith(str(ROOT) + os.sep))  # the skill's real location, not a relative name
        self.assertIn(f"next: as root: review the plan above, then run python3 {SCRIPT} apply --yes --repos-dir {self.m.repos}",
                      self.run_tool("--repos-dir", str(self.m.repos))[1])
        # the plan's command, taken verbatim from next_step, pasted into a shell elsewhere
        command = r["next_step"].split("then run ", 1)[1]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("APPLIED: all steps done or already satisfied", proc.stdout)
        # the command suggested after a successful run, again verbatim
        _, _, _, r = self.run_tool("--json", "apply", "--yes", "--repos-dir", str(self.m.repos))
        self.assertEqual(r["next_step"], f"0 step(s) done, nothing left to do; run python3 {SCRIPT} verify --repos-dir {self.m.repos}")
        command = r["next_step"].split("run ", 1)[1]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("VERIFIED: every build-environment check passes", proc.stdout)

    def test_a_missing_dnf_blocks_the_packages_step_in_the_plan(self):
        (self.m.bin / "dnf").unlink()
        env = self.m.env(PATH=str(self.m.bin))  # rpm still answers through its wrapper; the real dnf is not on this PATH
        _, out, _, r = self.run_tool("--json", env=env)
        packages = self.step(r, "packages")
        self.assertEqual((packages["status"], packages["detail"], packages["fix"]),
                         ("blocked", "dnf is not on PATH: the packages step needs it", "install dnf (the package manager of openEuler / CentOS), then rerun plan"))
        self.assertTrue(r["next_step"].startswith("install dnf (the package manager"))
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual(code, 2)
        self.assertIn("the packages step is blocked: dnf is not on PATH: the packages step needs it — install dnf", r["error_message"])
        self.assertEqual(self.m.rpm_db.read_text(encoding="utf-8"), "")  # nothing was installed

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

    def test_verify_reports_missing_siblings_as_not_verified(self):
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(i["name"], i["status"], i["skill"]) for i in r["checks"]],
                         [("toolchain", "missing", "flink-velox-toolchain-check"), ("build-tools", "missing", "flink-velox-build-tools-check"),
                          ("cpp-deps", "missing", "flink-velox-cpp-deps-check"), ("jdk", "missing", "flink-velox-jdk-check"),
                          ("source-repos", "missing", "flink-velox-source-repos-check")])
        self.assertEqual(r["error_message"], "5 check(s) not run (sibling skills not installed): flink-velox-toolchain-check, "
                         "flink-velox-build-tools-check, flink-velox-cpp-deps-check, flink-velox-jdk-check, flink-velox-source-repos-check")
        self.assertTrue(r["next_step"].startswith("install the missing check skill(s) (flink-velox-toolchain-check, "))
        report = self.run_tool("verify")[1]
        self.assertIn("  MISSING  toolchain: flink-velox-toolchain-check is not installed next to this skill", report)
        self.assertIn("NOT VERIFIED: 5 check(s) not run (sibling skills not installed): flink-velox-toolchain-check", report)
        self.assertNotIn("\nVERIFIED", report)

    def test_verify_finds_the_siblings_in_the_flat_install_layout(self):
        # install.sh copies every skill to <config>/skills/<skill>/: no family dir, resolution goes by skill name
        skills = self.tmp / "config" / "skills"
        shutil.copytree(ROOT, skills / "flink-velox-build-env-prepare", ignore=shutil.ignore_patterns("results", "__pycache__"))
        self.m.fake_family(skills)
        proc = subprocess.run([sys.executable, str(skills / "flink-velox-build-env-prepare" / "scripts" / "build_env_prepare.py"), "--json", "verify", *self.repos_dir_arg()],
                              env=self.m.env(ENV_PREPARE_ENV_FAMILY_DIR=None), cwd=str(self.tmp), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        r = json.loads(proc.stdout)[KEY]
        self.assertEqual((proc.returncode, [i["status"] for i in r["checks"]]), (0, ["ok"] * 5))

    def test_verify_missing_pending_checks_point_at_the_pr_that_delivers_them(self):
        """The tree as long as !320 is not merged: the !268 checks are installed, jdk and
        source-repos are not — verify is NOT VERIFIED (a missing sibling is not a pass),
        the missing details say which PR delivers them, and next_step offers waiting for
        that merge as an alternative to installing the skills by hand."""
        self.m.fake_family()
        shutil.rmtree(self.m.family / "flink-velox-jdk-check")
        shutil.rmtree(self.m.family / "flink-velox-source-repos-check")
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([i["status"] for i in r["checks"]], ["ok", "ok", "ok", "missing", "missing"])
        for name in ("jdk", "source-repos"):
            item = self.step_like(r, name)
            self.assertEqual((item["skill"], item.get("pending_pr")), (f"flink-velox-{name}-check", "!320"))
            self.assertEqual(item["detail"], f"flink-velox-{name}-check is not installed next to this skill "
                             "yet — delivered by !320 (pending merge)")
        self.assertEqual(r["error_message"], "2 check(s) not run (sibling skills not installed): "
                         "flink-velox-jdk-check, flink-velox-source-repos-check")
        self.assertEqual(r["next_step"], "install the missing check skill(s) (flink-velox-jdk-check, "
                         "flink-velox-source-repos-check) — jdk, source-repos come with !320 (still pending "
                         f"merge): install those skills or wait for the merge, then rerun python3 {SCRIPT} verify")
        report = self.run_tool("verify")[1]
        self.assertIn("  MISSING  jdk: flink-velox-jdk-check is not installed next to this skill yet — "
                      "delivered by !320 (pending merge)", report)
        self.assertIn("NOT VERIFIED: 2 check(s) not run", report)

    def test_verify_next_step_separates_missing_from_failing(self):
        """A missing sibling (delivered by a pending PR) and a failing check are two
        different problems: next_step names both, each with its own advice."""
        self.m.fake_family()
        shutil.rmtree(self.m.family / "flink-velox-jdk-check")
        shutil.rmtree(self.m.family / "flink-velox-source-repos-check")
        code, out, _, r = self.run_tool("--json", "verify", env=self.m.env(FAKE_CHECK_FAIL="flink-velox-toolchain-check"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([i["status"] for i in r["checks"]], ["fail", "ok", "ok", "missing", "missing"])
        self.assertEqual(r["error_message"], "1 check(s) failing; 2 check(s) not run (sibling skills not installed): "
                         "flink-velox-jdk-check, flink-velox-source-repos-check")
        self.assertEqual(r["next_step"], "install the missing check skill(s) (flink-velox-jdk-check, "
                         "flink-velox-source-repos-check) — jdk, source-repos come with !320 (still pending "
                         "merge): install those skills or wait for the merge; fix the failing check(s) above, "
                         f"then rerun python3 {SCRIPT} verify")

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
        # in-process on purpose (to intercept the probe) — inside the fake environment, so nothing
        # the script could reach for points at the real machine
        with patch.dict(os.environ, self.m.env(), clear=True), redirect_stdout(buffer):
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
        self.assertEqual((self.m.snapshot(), self.m.log.exists() if self.m.log else False), (before, False))
        self.assertEqual(self.m.rpm_db.read_text(encoding="utf-8"), "")
        self.assertFalse(self.m.repos.exists())

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
