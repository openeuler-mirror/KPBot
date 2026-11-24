#!/usr/bin/env python3
"""install-contract — tier-A case for flink-velox-runtime-install.

Drives scripts/runtime_install.py (plan / apply / verify) against a fake machine
laid out in a scratch directory — a sysroot holding /opt (the install prefix) and a
JDK, a fake HOME, a fake PATH whose curl / mvn / runuser are sh stubs and git is a
Python stub, all driven by files (a download fixture directory with a real tgz +
sha512, a mirror-failure switch, a fetch-failure switch, an mvn-failure switch, the
checkout's HEAD / origin / dirty state, the commit's tree the checkout's HEAD is said
to hold, a call log), real sha512sum / tar, and a fake flink-velox-env family directory
with stub check scripts — with nothing from the real machine in play: the script only
ever runs in a child process that carries the fake environment (ENV_PREPARE_SYSROOT /
ENV_PREPARE_HOME / ENV_PREPARE_EUID / ENV_PREPARE_PREFIX_WRITABLE /
ENV_PREPARE_ENV_FAMILY_DIR), and the commands that are made to fail are broken inside
that child process too. Pins the plan (exact commands, skipped / blocked steps, the
1.19 line), the apply contract (--yes required, root required when the prefix is not
writable, mirror-then-archive fallback, sha512 verification, symlink, minimal conf
appended only to the config file Flink reads and only when it is all comments, nexmark
fetched at the pinned commit / mvn / jar copy with its sha256 / tree completed from a
manifest, stop at the first failing step with a change log, idempotent rerun; a jar
already in lib is kept with its identity on record — its recomputed sha256 and the
requested ref in the skip's detail), what is
never done to the user's files (a foreign directory is not removed, a checkout at
another commit is reported), who builds what (a user builds their own checkout as it
is, from a tag / branch too; a privileged install refuses a moving ref outright, and
root installs only the official commit — origin, no local modifications, and a
root-owned copy compared file by file with the tree of the pinned commit — read at
the SHA itself with refs/replace off, the guards re-checked after the copy — before
anything is built or laid out from it, whatever git status says; through sudo git runs as the
invoking user in directories root hands over to them, with a real uid switch when
the case runs as root, and the Maven build is root's), the suggested commands
running as printed, verify aggregation, the JSON shape (also for a crash), --out
safety (checked before anything runs, a fifo is never opened) and that plan touches
nothing.

Writes results/result.json; exit 0 iff all tests pass.
"""
import hashlib
import importlib.util
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
PINNED = "6b3646c3baec701f1fa74baf938d235f742e5d3c"
MINIMAL_CONF_FIRST = "jobmanager.rpc.address: localhost"
MINIMAL_CONF_LAST = "taskmanager.numberOfTaskSlots: 8"
MANIFEST = ("bin/run_query.sh", "bin/config.sh", "conf/log4j.properties", "conf/nexmark.yaml", "conf/config.yaml",
            "queries/ddl_gen.sql", "queries/ddl_kafka.sql", "queries/ddl_views.sql", "queries/insert_kafka.sql")

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
GIT_STUB = textwrap.dedent(f'''\
    #!/usr/bin/env python3
    # fake git: fetching the pinned nexmark commit lays down a checkout with its resources; what
    # the checkout's HEAD holds is recorded in .git/FAKE_TREE, so ls-tree describes the commit
    # and not whatever the working tree was turned into afterwards; rev-parse / get-url / status
    # answer from the environment (read-only provenance and trust queries). Fixtures for the
    # trust-chain tests: .git/FAKE_REPLACE_TREE is what refs/replace/<pinned> would make ls-tree
    # say instead (only when GIT_NO_REPLACE_OBJECTS is not 1 — the value the stub saw lands in
    # .git/FAKE_REPLACE_SEEN); .git/FAKE_HEAD_TREE is what ls-tree of the movable HEAD answers
    # (HEAD is a ref the checkout's owner moves, the pinned SHA is not); FAKE_GIT_HEAD_AFTER
    # "<n>:<sha>" makes rev-parse answer <sha> once the first <n> calls are over (a HEAD that
    # moves between the guards and the copy)
    import hashlib, os, sys

    args = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(" ".join(args) + "\\n")
    repo = "."
    if args and args[0] == "-C":
        repo, args = args[1], args[2:]
    if not args:
        sys.exit(0)


    def record(root):
        entries = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for name in filenames:
                path = os.path.join(dirpath, name)
                data = open(path, "rb").read()
                digest = hashlib.sha1(b"blob %d\\0" % len(data) + data).hexdigest()
                mode = "100755" if os.stat(path).st_mode & 0o111 else "100644"
                entries.append("%s blob %s\\t%s" % (mode, digest, os.path.relpath(path, root).replace(os.sep, "/")))
        with open(os.path.join(root, ".git", "FAKE_TREE"), "w") as fh:
            fh.write("\\0".join(sorted(entries)) + "\\0")


    def lay_down(root):
        res = os.path.join(root, "nexmark-flink", "src", "main", "resources")
        for d in (os.path.join(root, ".git"), os.path.join(res, "bin"), os.path.join(res, "conf"),
                  os.path.join(res, "queries"), os.path.join(res, "queries-cep")):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(root, ".git", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/master\\n")
        for name, what in (("run_query.sh", "run_query"), ("config.sh", "config")):
            path = os.path.join(res, "bin", name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\\necho %s\\n" % what)
            os.chmod(path, 0o755)
        for rel in ("conf/nexmark.yaml", "conf/config.yaml", "conf/log4j.properties", "queries-cep/q0.sql",
                    "queries/ddl_gen.sql", "queries/ddl_kafka.sql", "queries/ddl_views.sql", "queries/insert_kafka.sql",
                    "queries/q0.sql", "queries/q7.sql"):
            open(os.path.join(res, rel), "w").close()
        record(root)


    cmd = args[0]
    if cmd == "rev-parse":
        head = os.environ.get("FAKE_GIT_HEAD", "{PINNED}")
        later = os.environ.get("FAKE_GIT_HEAD_AFTER", "")  # "<n>:<sha>": <sha> from the (n+1)th rev-parse on
        if later:
            n, _, sha = later.partition(":")
            try:
                with open(os.environ["FAKE_LOG"]) as fh:
                    seen = sum(1 for line in fh if " rev-parse " in line)  # this call's own line included
            except OSError:
                seen = 0
            if seen > int(n):
                head = sha
        print(head)
    elif cmd == "remote" and args[1] == "get-url":
        print(os.environ.get("FAKE_GIT_ORIGIN", "{NEXMARK_REPO}"))
    elif cmd == "status":
        if os.environ.get("FAKE_GIT_DIRTY"):
            print(" M pom.xml")
    elif cmd == "ls-tree":
        def fixture(name):
            try:
                with open(os.path.join(repo, ".git", name)) as fh:
                    return fh.read()
            except OSError:
                return None
        tree = fixture("FAKE_TREE")
        if tree is None:
            sys.exit(128)  # no commit to describe
        replaced = fixture("FAKE_REPLACE_TREE")  # what refs/replace/<pinned> would put in the SHA's place
        if replaced is not None:
            with open(os.path.join(repo, ".git", "FAKE_REPLACE_SEEN"), "a") as fh:
                fh.write(os.environ.get("GIT_NO_REPLACE_OBJECTS", "") + "\\n")
        if replaced is not None and os.environ.get("GIT_NO_REPLACE_OBJECTS") != "1":
            sys.stdout.write(replaced)  # the replacement stands in for the pinned commit
        elif args[-1] == "HEAD":
            forged = fixture("FAKE_HEAD_TREE")  # what the movable HEAD names, not the pinned SHA
            sys.stdout.write(forged if forged is not None else tree)
        else:
            sys.stdout.write(tree)
    elif cmd == "init":
        os.makedirs(os.path.join(args[-1], ".git"), exist_ok=True)
        with open(os.path.join(args[-1], ".git", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/master\\n")
    elif cmd == "fetch":
        if os.environ.get("FAKE_GIT_CLONE_FAIL") == "nexmark":
            print("fatal: unable to access '{NEXMARK_REPO}'", file=sys.stderr)
            sys.exit(128)
        lay_down(repo)
    elif cmd == "clone":
        if os.environ.get("FAKE_GIT_CLONE_FAIL") == "nexmark":
            print("fatal: unable to access '{NEXMARK_REPO}'", file=sys.stderr)
            sys.exit(128)
        lay_down(args[-1])
    sys.exit(0)
    ''')
MVN_STUB = textwrap.dedent('''\
    # fake mvn: -pl nexmark-flink -am package -DskipTests -> target jars in the cwd; fails the
    # way Maven does when it cannot write there
    echo "mvn $* JAVA_HOME=$JAVA_HOME HOME=$HOME" >> "$FAKE_LOG"
    [ "${FAKE_MVN_FAIL:-}" = 1 ] && { echo "[ERROR] Failed to execute goal" >&2; exit 1; }
    mkdir -p nexmark-flink/target 2>/dev/null || { echo "[ERROR] Build failed: cannot write under $PWD" >&2; exit 1; }
    : > nexmark-flink/target/nexmark-flink-0.3-SNAPSHOT.jar
    : > nexmark-flink/target/nexmark-flink-0.3-SNAPSHOT-sources.jar
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
# Helper process for fault injection: runs the script as __main__ with subprocess.run raising
# for the commands the test names ("tar=missing,mvn=timeout").
FAULTY_RUNNER = textwrap.dedent('''\
    import runpy, subprocess, sys
    real_run = subprocess.run
    faults = dict(item.split("=", 1) for item in sys.argv[1].split(","))

    def faulty(argv, *args, **kwargs):
        for head, kind in faults.items():
            if str(argv[0]).endswith(head):
                if kind == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 120)
                raise FileNotFoundError(2, "No such file or directory", str(argv[0]))
        return real_run(argv, *args, **kwargs)

    subprocess.run = faulty
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name="__main__")
    ''')


def record_tree(root, name="FAKE_TREE"):
    """What the fake checkout's HEAD holds (the fake git's bookkeeping): every file outside
    .git with its git blob hash and mode, in the layout of `git ls-tree -r -z`. `name`
    records the same walk under another fixture (what a forgery would like the commit to
    hold, what the movable HEAD answers)."""
    root = Path(root)
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts):
        data = path.read_bytes()
        digest = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        entries.append(f"{mode} blob {digest}\t{path.relative_to(root).as_posix()}")
    (root / ".git" / name).write_text("\0".join(entries) + "\0", encoding="utf-8")
    return entries


def load_module():
    spec = importlib.util.spec_from_file_location("install_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def demoted_user_can_create_dirs():
    """Whether the real uid-switch test can run here: it needs root, setpriv and a
    filesystem with room for a non-root user. On a full one the root reserve keeps root
    writing while every demoted mkdir fails with ENOSPC — the run would exercise the
    disk, not the permission boundary."""
    if not (os.geteuid() == 0 and shutil.which("setpriv")):
        return False
    try:
        entry = pwd.getpwnam("nobody")
    except KeyError:
        return False
    SCRATCH.mkdir(parents=True, exist_ok=True)
    probe = tempfile.mkdtemp(dir=str(SCRATCH))
    try:
        os.chmod(probe, 0o777)
        return subprocess.run(["setpriv", f"--reuid={entry.pw_uid}", f"--regid={entry.pw_gid}",
                                "--clear-groups", "mkdir", os.path.join(probe, "d")],
                               stdin=subprocess.DEVNULL, capture_output=True).returncode == 0
    finally:
        shutil.rmtree(probe, ignore_errors=True)


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
        self.write(self.bin / "git", GIT_STUB, 0o755)
        self.script(self.bin / "mvn", MVN_STUB)
        self.script(self.bin / "runuser", RUNUSER_STUB)
        # wrappers so that a PATH without the system directories still has them
        for tool in ("sha512sum", "tar"):
            self.script(self.bin / tool, f'exec /usr/bin/{tool} "$@"\n')
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

    def fixture_tgz(self, version=VERSION, comments_only=True, name=TGZ, sha_name=None, bad_sha=False,
                    conf_name="flink-conf.yaml", also=None):
        """A real tgz holding a stock-looking distribution + its sha512 file. `also` =
        (name, text) of a second conf file to ship."""
        staging = self.root / "staging"
        dist = staging / f"flink-{version}"
        for f in ("bin/flink", "bin/start-cluster.sh", "bin/stop-cluster.sh", "bin/sql-client.sh"):
            self.script(dist / f)
        self.write(dist / "conf" / conf_name,
                   ("# a stock file: every line is a comment\n" if comments_only else "rest.port: 8081\n"))
        if also:
            self.write(dist / "conf" / also[0], also[1])
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

    def installed_flink(self, conf="rest.port: 8081\n", version=VERSION):
        dist = self.prefix / f"flink-{version}"
        for f in ("bin/flink", "bin/start-cluster.sh"):
            self.script(dist / f)
        self.write(dist / "conf" / "flink-conf.yaml", conf)
        self.write(dist / "lib" / f"flink-dist-{version}.jar", "jar\n")
        (self.prefix / "flink").symlink_to(dist, target_is_directory=True)
        return dist

    def checkout(self, path=None):
        """A nexmark checkout as the fake git lays it down."""
        path = Path(path or self.repos / "nexmark")
        res = path / "nexmark-flink" / "src" / "main" / "resources"
        self.write(path / ".git" / "HEAD", "ref: refs/heads/master\n")
        self.script(res / "bin" / "run_query.sh", "echo run_query\n")
        self.script(res / "bin" / "config.sh", "echo config\n")
        for rel in ("conf/nexmark.yaml", "conf/config.yaml", "conf/log4j.properties",
                    "queries/ddl_gen.sql", "queries/ddl_kafka.sql", "queries/ddl_views.sql", "queries/insert_kafka.sql",
                    "queries/q0.sql", "queries-cep/q0.sql"):
            self.write(res / rel, "")
        record_tree(path)
        return path

    def laid_out_tree(self, run_query="#!/bin/sh\necho mine\n"):
        home = self.nexmark_home
        self.script(home / "bin" / "run_query.sh", run_query.split("\n", 1)[1])
        self.script(home / "bin" / "config.sh", "echo config\n")
        for rel in MANIFEST[2:]:
            self.write(home / rel, "")
        self.write(home / "queries" / "q0.sql", "")
        return home

    def fake_family(self, root=None):
        for skill, script in (("flink-velox-flink-dist-check", "flink_dist_check.py"),
                              ("flink-velox-nexmark-check", "nexmark_check.py")):
            self.script(Path(root or self.family) / skill / "scripts" / script, CHECK_STUB)

    def env(self, **overrides):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ENV_PREPARE_SYSROOT": str(self.sys),
               "ENV_PREPARE_HOME": str(self.home), "ENV_PREPARE_ENV_FAMILY_DIR": str(self.family),
               "ENV_PREPARE_EUID": "1000", "ENV_PREPARE_RETRY_SLEEP": "0", "HOME": str(self.home), "LC_ALL": "C",
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


def clone_text(dest):
    return (f"git init -q {dest} && git -C {dest} remote add origin {NEXMARK_REPO} && "
            f"git -C {dest} fetch --depth 1 origin {PINNED} && git -C {dest} checkout -q --detach FETCH_HEAD")


class InstallCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def run_tool(self, *args, env=None, cwd=None, timeout=120):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args, *self.m.args()], capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env, cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=timeout)
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

    def log(self, without_queries=True):
        """The stubs' call log; read-only git queries (rev-parse / get-url / status) left out."""
        if not self.m.log.exists():
            return ""
        lines = self.m.log.read_text(encoding="utf-8").splitlines(True)
        return "".join(l for l in lines if not (without_queries and any(f" {q} " in l for q in ("rev-parse", "get-url", "status", "ls-tree"))))

    def reviewed(self, sub, yes=False):
        """The command next_step should give: complete (interpreter and the script's own path),
        the options this case passes being explicit."""
        return (f"python3 {SCRIPT} {sub} {'--yes ' if yes else ''}--prefix {self.m.prefix} "
                f"--nexmark-home {self.m.nexmark_home} --repos-dir {self.m.repos}")

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
        self.assertEqual(jar["plan"][0], clone_text("<tmp>/nexmark")
                         + f"   (retries 3; the checkout then moves into place as {self.m.repos / 'nexmark'})")
        self.assertEqual(jar["plan"][1], f"# JAVA_HOME={self.m.jdk} mvn -pl nexmark-flink -am package -DskipTests   (in {self.m.repos / 'nexmark'})")
        self.assertEqual(jar["plan"][2], f"# cp {self.m.repos / 'nexmark'}/nexmark-flink/target/nexmark-flink-*.jar (not -sources) -> {self.m.prefix}/flink-{VERSION}/lib/")
        self.assertEqual(jar["detail"], f"build nexmark-flink at {PINNED[:12]} from {self.m.repos / 'nexmark'} with {self.m.jdk} and copy the jar into {self.m.prefix}/flink-{VERSION}/lib")
        self.assertEqual(tree["missing"], list(MANIFEST) + ["queries/q*.sql"])
        self.assertEqual(tree["plan"][0], f"mkdir -p {self.m.nexmark_home}")
        self.assertTrue(tree["plan"][1].startswith("# copy what is missing (10 manifest entries: bin/run_query.sh, bin/config.sh, "))
        self.assertIn(f"from {self.m.repos / 'nexmark/nexmark-flink/src/main/resources'}; existing files are kept", tree["plan"][1])
        self.assertEqual(tree["plan"][2], f"mkdir -p {self.m.nexmark_home}/{{lib,log,data}}")
        self.assertEqual(r["root_steps"], [])
        self.assertEqual(r["next_step"], "review the plan above, then run " + self.reviewed("apply", yes=True))
        _, out, _, _ = self.run_tool()
        self.assertIn(f"runtime-install plan  (Flink {VERSION} -> {self.m.prefix / 'flink'}, nexmark {self.m.nexmark_home})", out)
        self.assertIn("  flink        TODO    Flink 1.19.2: download+unpack, symlink, minimal-conf into", out)
        self.assertIn("        curl -fsSL --retry 3 --max-time 3600 -o <tmp>/" + TGZ + " <url>", out)
        self.assertIn("  nexmark-tree TODO    complete the runnable tree at", out)
        self.assertIn("PLAN: 3 step(s) to apply", out)
        self.assertEqual(err, "")

    def test_plan_when_everything_is_installed(self):
        self.m.installed_flink()
        self.m.write(self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "jar\n")
        self.m.laid_out_tree()
        code, out, _, r = self.run_tool("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual([s["status"] for s in r["steps"]], ["skipped"] * 3)
        self.assertEqual(self.step(r, "flink")["detail"], f"Flink {VERSION} at {self.m.prefix / 'flink-1.19.2'} (via {self.m.prefix / 'flink'}); conf already set")
        self.assertEqual(self.step(r, "nexmark-tree")["detail"], f"runnable tree at {self.m.nexmark_home} (complete)")
        self.assertEqual(r["next_step"], "ready: the Flink runtime and nexmark are installed (verify with: " + self.reviewed("verify") + ")")
        self.assertIn("PLAN: nothing to do", self.run_tool()[1])

    def test_plan_lists_what_the_runnable_tree_lacks(self):
        self.m.installed_flink()
        self.m.write(self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "jar\n")
        self.m.script(self.m.nexmark_home / "bin" / "run_query.sh")  # only the launcher: not a runnable tree
        _, out, _, r = self.run_tool("--json")
        tree = self.step(r, "nexmark-tree")
        self.assertEqual(tree["status"], "blocked")  # no source checkout to complete it from
        self.m.checkout()
        _, _, _, r = self.run_tool("--json")
        tree = self.step(r, "nexmark-tree")
        self.assertEqual((tree["status"], tree["missing"]), ("todo", list(MANIFEST[1:]) + ["queries/q*.sql"]))
        out = self.run_tool()[1]
        self.assertIn("  nexmark-tree TODO    complete the runnable tree at", out)
        self.assertIn("(missing 9: bin/config.sh, conf/log4j.properties", out)

    def test_plan_dist_present_but_link_and_conf_missing(self):
        self.m.installed_flink(conf="# all comments\n")
        (self.m.prefix / "flink").unlink()
        _, _, _, r = self.run_tool("--json")
        flink = self.step(r, "flink")
        self.assertEqual(flink["pending"], ["symlink", "minimal-conf"])
        self.assertEqual((flink["conf"], flink["plan"][0]),
                         (str(self.m.prefix / f"flink-{VERSION}" / "conf" / "flink-conf.yaml"), f"# {self.m.prefix}/flink-{VERSION} already installed"))

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
        self.assertEqual((jar["status"], self.step(r, "flink")["status"]), ("blocked", "todo"))
        self.assertIn("mvn is not on PATH (flink-velox-build-env-prepare installs maven)", jar["detail"])
        self.assertEqual(self.step(r, "nexmark-tree")["status"], "blocked")
        self.assertIn("PLAN: 1 step(s) to apply (2 step(s) blocked — see the fix: lines)", self.run_tool(env=self.m.env(PATH=str(self.m.bin)))[1])
        shutil.rmtree(self.m.jdk)
        _, _, _, r = self.run_tool("--json", env=self.m.env(PATH=str(self.m.bin)))
        self.assertIn("no JDK found (JAVA_HOME or /usr/lib/jvm/java-17-* / java-1.8.0-openjdk-*)",
                      self.step(r, "nexmark-jar")["detail"])

    def test_plan_names_the_tools_a_step_needs(self):
        (self.m.bin / "git").unlink()
        _, _, _, r = self.run_tool("--json", env=self.m.env(PATH=str(self.m.bin)))
        jar = self.step(r, "nexmark-jar")
        self.assertEqual(jar["status"], "blocked")
        self.assertIn("git is not on PATH (flink-velox-build-env-prepare installs git)", jar["detail"])
        (self.m.bin / "curl").unlink()
        _, _, _, r = self.run_tool("--json", env=self.m.env(PATH=str(self.m.bin)))
        flink = self.step(r, "flink")
        self.assertEqual((flink["status"], flink["fix"]), ("blocked", "install the missing tool(s) (flink-velox-build-env-prepare installs them), then rerun plan"))
        self.assertEqual(flink["detail"], "curl not on PATH — needed to download and unpack " + TGZ)

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
        self.assertEqual(r["next_step"], "as root: review the plan above, then run " + self.reviewed("apply", yes=True))
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_PREFIX_WRITABLE="0", ENV_PREPARE_EUID="1000"))
        self.assertEqual(code, 2)
        self.assertIn("steps flink, nexmark-jar, nexmark-tree need root (not writable): sudo " + self.reviewed("apply", yes=True),
                      r["error_message"])
        self.assertEqual(self.log(), "")

    def test_the_suggested_commands_run_as_printed(self):
        """next_step / the sudo hint give complete commands: interpreter and the script's own
        path, so that copying them into a shell from any directory works (the bare file name is
        neither on PATH nor in the working directory)."""
        self.m.fake_family()
        _, _, _, r = self.run_tool("--json")
        self.assertTrue(str(SCRIPT).startswith(str(ROOT) + os.sep))  # the skill's real location, not a relative name
        # the plan's command, taken verbatim from next_step, pasted into a shell elsewhere
        command = r["next_step"].split("then run ", 1)[1]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("APPLIED: all steps done or already satisfied", proc.stdout)
        # the command suggested after a successful run, again verbatim
        _, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((r["success"], r["error_message"]), (True, ""))
        self.assertTrue(r["next_step"].endswith("nothing left to do; run " + self.reviewed("verify")))
        command = r["next_step"].split("run ", 1)[1]
        proc = subprocess.run(["bash", "-c", command], env=self.m.env(), cwd=str(self.m.root), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertIn("VERIFIED: Flink runtime and nexmark checks pass", proc.stdout)

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
        actions = [c["action"] for c in r["changes"]]
        self.assertEqual(actions, ["downloaded", "unpacked", "symlinked", "configured", "cloned", "built+deployed", "laid out"])
        self.assertEqual(r["changes"][0]["detail"], f"{TGZ} from {MIRROR}/flink-{VERSION}/{TGZ}")
        self.assertEqual(r["changes"][1]["detail"], f"{TGZ} (sha512 verified) -> {dist}")
        built = r["changes"][5]["detail"]
        digest = hashlib.sha256(b"").hexdigest()  # the stub's jar is empty
        self.assertEqual(built, f"nexmark-flink-0.3-SNAPSHOT.jar (sha256 {digest}) from {self.m.repos / 'nexmark'} at {PINNED[:12]} -> {dist / 'lib'}")
        self.assertEqual(r["changes"][3]["detail"], f"appended {11} keys to {dist / 'conf' / 'flink-conf.yaml'}")
        self.assertEqual(r["next_step"], f"3 step(s) done, nothing left to do; run {self.reviewed('verify')}")
        log = self.log()
        self.assertIn(f"{MIRROR}/flink-{VERSION}/{TGZ}\n{ARCHIVE}/flink-{VERSION}/{TGZ}.sha512\n", log)
        self.assertIn(f"fetch --depth 1 origin {PINNED}", log)
        self.assertIn(f"mvn -pl nexmark-flink -am package -DskipTests JAVA_HOME={self.m.jdk}", log)
        self.assertNotIn("runuser", log)  # not running through sudo: nothing to hand back
        report = load_module().render({KEY: r})
        self.assertIn("APPLIED: all steps done or already satisfied", report)
        self.assertIn("    - nexmark-tree: laid out — ", report)
        # rerun: everything skipped, curl / git fetch / mvn never called again
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, [s["status"] for s in r["steps"]], [c["action"] for c in r["changes"]]),
                         (0, ["skipped"] * 3, ["skipped"] * 3))
        self.assertEqual(self.log(), log)
        # the kept jar's identity is on record in the rerun's changes too, with the ref asked for
        kept = next(c for c in r["changes"] if c["step"] == "nexmark-jar")
        self.assertIn(f"nexmark-flink-0.3-SNAPSHOT.jar (sha256 {digest}) already in {dist / 'lib'}", kept["detail"])
        self.assertIn(f"this run asked for nexmark at {PINNED[:12]}", kept["detail"])

    def test_a_jar_already_in_lib_is_kept_with_its_identity_on_record(self):
        """The skip when lib already holds a nexmark jar is legal (idempotence) but never
        silent: every kept jar's sha256 is recomputed and the requested ref is named, so a
        changed --nexmark-ref is seen as ignored and the operator learns what to remove to
        build the ref they asked for."""
        self.m.installed_flink()
        lib = self.m.prefix / f"flink-{VERSION}" / "lib"
        newer = self.m.write(lib / "nexmark-flink-0.2.jar", "kept\n")
        self.m.write(lib / "nexmark-flink-0.1.jar", "older\n")
        self.m.laid_out_tree()
        ref = "f" * 40
        _, _, _, r = self.run_tool("--json", "--nexmark-ref", ref)
        jar = self.step(r, "nexmark-jar")
        older_digest = hashlib.sha256(b"older\n").hexdigest()
        kept_digest = hashlib.sha256(b"kept\n").hexdigest()
        kept = f"nexmark-flink-0.1.jar (sha256 {older_digest}), nexmark-flink-0.2.jar (sha256 {kept_digest})"
        self.assertEqual((jar["status"], jar["ref"]), ("skipped", ref))
        self.assertEqual(jar["detail"],
                         f"{kept} already in {lib} — kept, not rebuilt; this run asked for nexmark "
                         f"at {ref[:12]}; remove the jar(s) to build another ref (or install another version / prefix)")
        self.assertIn(jar["detail"], self.run_tool("--nexmark-ref", ref)[1])  # the human report carries it too
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--nexmark-ref", ref)
        self.assertEqual((code, r["success"], [s["status"] for s in r["steps"]]), (0, True, ["skipped"] * 3))
        self.assertEqual([c for c in r["changes"] if c["step"] == "nexmark-jar"],
                         [{"step": "nexmark-jar", "action": "skipped", "detail": jar["detail"]}])
        self.assertEqual(self.log(), "")  # nothing ran: no fetch, no mvn — the jar is kept as is
        self.assertEqual(newer.read_text(encoding="utf-8"), "kept\n")

    def test_apply_falls_back_to_the_archive_when_the_mirror_fails(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_CURL_MIRROR_FAIL="1"))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertIn(f"{MIRROR}/flink-{VERSION}/{TGZ}\n{ARCHIVE}/flink-{VERSION}/{TGZ}\n", self.log())
        self.assertEqual(r["changes"][0]["detail"], f"{TGZ} from {ARCHIVE}/flink-{VERSION}/{TGZ}")

    def test_apply_stops_on_a_checksum_mismatch(self):
        self.m.fixture_tgz(bad_sha=True)
        code, out, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (1, False))
        flink = self.step(r, "flink")
        self.assertIn(f"sha512 mismatch for {TGZ}:", flink["detail"])
        self.assertFalse((self.m.prefix / f"flink-{VERSION}").exists())
        self.assertEqual(self.step(r, "nexmark-jar")["status"], "todo")  # later steps not run
        self.assertNotIn("fetch", self.log())
        self.assertEqual((r["error_message"], [c["action"] for c in r["changes"]]), ("flink failed", ["downloaded", "failed"]))
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

    def test_apply_leaves_a_file_at_the_old_temp_name_alone(self):
        self.m.installed_flink()
        (self.m.prefix / "flink").unlink()
        keeper = self.m.prefix / "flink.runtime-install.tmp"  # a file of the user's where a temp link used to go
        keeper.write_text("mine\n", encoding="utf-8")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--no-nexmark")
        self.assertEqual((code, keeper.read_text(encoding="utf-8")), (0, "mine\n"))
        self.assertEqual(os.path.realpath(self.m.prefix / "flink"), str(self.m.prefix / f"flink-{VERSION}"))
        self.assertEqual(sorted(p.name for p in self.m.prefix.iterdir() if "runtime-install" in p.name), ["flink.runtime-install.tmp"])

    def test_apply_stops_at_a_failed_nexmark_fetch_then_resumes(self):
        env = self.m.env(FAKE_GIT_CLONE_FAIL="nexmark")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]][1:], [("nexmark-jar", "failed"), ("nexmark-tree", "todo")])
        self.assertIn("git clone of nexmark failed: fatal: unable to access", self.step(r, "nexmark-jar")["detail"])
        self.assertIn("(3 attempts)", self.step(r, "nexmark-jar")["detail"])
        self.assertEqual((self.m.repos / "nexmark").exists(), False)
        self.assertEqual(sorted(p.name for p in self.m.repos.iterdir()), [])  # no clone directory left behind
        self.assertEqual(self.log().count(f"fetch --depth 1 origin {PINNED}"), 3)
        self.assertEqual([c["action"] for c in r["changes"]][-3:], ["clone failed", "clone failed", "failed"])
        self.assertFalse(self.m.nexmark_home.exists())
        self.assertIn(f"fix the nexmark-jar step", r["next_step"])
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, [s["status"] for s in r["steps"]]), (0, ["skipped", "done", "done"]))

    def test_apply_stops_at_a_failed_mvn_build(self):
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(FAKE_MVN_FAIL="1"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("mvn build of nexmark-flink failed: [ERROR] Failed to execute goal", self.step(r, "nexmark-jar")["detail"])
        self.assertEqual([c["action"] for c in r["changes"]], ["downloaded", "unpacked", "symlinked", "configured", "cloned", "failed"])
        self.assertFalse((self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())
        self.assertFalse(self.m.nexmark_home.exists())
        self.assertIn(f"rerun plan and {self.reviewed('apply', yes=True)} — it is idempotent", r["next_step"])
        # the checkout is kept: the rerun builds without fetching again
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, self.step(r, "nexmark-jar")["status"]), (0, "done"))
        self.assertEqual(self.log().count(f"fetch --depth 1 origin {PINNED}"), 1)

    def test_a_fetched_checkout_at_another_commit_is_not_used(self):
        env = self.m.env(FAKE_GIT_HEAD="e" * 40)  # what the fetch brings back is not the pinned commit
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "nexmark-jar failed"))
        self.assertIn(f"FETCH_HEAD is {'e' * 40}, expected {PINNED} (3 attempts)", self.step(r, "nexmark-jar")["detail"])
        self.assertEqual((self.m.repos / "nexmark").exists(), False)  # nothing moved into place
        self.assertEqual(sorted(p.name for p in self.m.repos.iterdir()), [])  # and no clone directory left behind
        self.assertEqual(self.log().count(f"fetch --depth 1 origin {PINNED}"), 3)
        self.assertNotIn("mvn", self.log())

    def test_a_foreign_directory_is_never_cloned_over_or_removed(self):
        mine = self.m.write(self.m.repos / "nexmark" / "notes.txt", "my notes\n")
        _, _, _, r = self.run_tool("--json")
        jar = self.step(r, "nexmark-jar")
        self.assertEqual(jar["status"], "blocked")
        self.assertEqual(jar["detail"], f"{self.m.repos / 'nexmark'} exists and is not a git checkout — it is not cloned over and not removed")
        self.assertEqual(jar["fix"], f"move or remove {self.m.repos / 'nexmark'} by hand, or point --repos-dir elsewhere")
        self.assertIn("  nexmark-jar  BLOCKED", self.run_tool()[1])
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["success"]), (2, False))
        self.assertIn("the nexmark-jar step is blocked", r["error_message"])
        self.assertEqual((mine.read_text(encoding="utf-8"), sorted(p.name for p in self.m.repos.iterdir())), ("my notes\n", ["nexmark"]))
        self.assertNotIn("fetch", self.log())

    def test_an_existing_checkout_at_another_commit_is_reported_not_built(self):
        self.m.installed_flink()
        self.m.checkout()
        env = self.m.env(FAKE_GIT_HEAD="f" * 40)
        _, _, _, r = self.run_tool("--json", env=env)
        jar = self.step(r, "nexmark-jar")
        self.assertEqual(jar["status"], "blocked")
        self.assertIn(f"the existing checkout at {self.m.repos / 'nexmark'} is not used: it is at {'f' * 40}, not the requested {PINNED}", jar["detail"])
        self.assertEqual(jar["fix"], "pass --nexmark-ref <commit> for the commit the checkout is at, or move it away")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual(code, 2)
        self.assertNotIn("mvn", self.log())
        # the user decides: asking for that commit explicitly makes it the requested one
        _, _, _, r = self.run_tool("--json", "--nexmark-ref", "f" * 40, env=env)
        self.assertEqual(self.step(r, "nexmark-jar")["status"], "todo")
        self.assertIn("--nexmark-ref " + "f" * 40, r["next_step"])

    def test_a_privileged_install_only_builds_the_pinned_official_source(self):
        self.m.installed_flink()
        self.m.checkout()
        dirty = self.m.env(FAKE_GIT_DIRTY="1")
        _, _, _, r = self.run_tool("--json", env=dirty)  # an ordinary user builds a modified checkout all the time
        jar = self.step(r, "nexmark-jar")
        self.assertEqual((jar["status"], jar["builds_from"]), ("todo", "the checkout"))
        why = "root installs the artifacts, so only the official commit is built"
        for as_root in (dict(dirty, ENV_PREPARE_EUID="0"), dict(dirty, ENV_PREPARE_EUID="0", SUDO_USER="builder")):
            _, _, _, r = self.run_tool("--json", env=as_root)  # root, with or without a sudo user to hand commands to
            jar = self.step(r, "nexmark-jar")
            self.assertEqual((jar["status"], jar["builds_from"]), ("blocked", "a verified copy"))
            self.assertIn("it has local modifications (" + why + ")", jar["detail"])
        root = self.m.env(ENV_PREPARE_EUID="0", FAKE_GIT_ORIGIN="https://evil.example/nexmark.git")
        _, _, _, r = self.run_tool("--json", env=root)
        self.assertIn("its origin is https://evil.example/nexmark.git, not " + NEXMARK_REPO + " (" + why + ")", self.step(r, "nexmark-jar")["detail"])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertEqual((code, self.log().count("mvn")), (2, 0))

    def test_a_privileged_install_builds_only_what_the_commit_holds(self):
        """git status is not what decides: the copy root builds is compared file by file with
        the commit's tree. A file git does not report (status.showUntrackedFiles=no, a
        core.worktree elsewhere, a race between the query and the copy) is still caught."""
        self.m.installed_flink()
        self.m.checkout()
        self.m.write(self.m.repos / "nexmark" / "nexmark-flink" / "pom.xml", "<project/>\n")
        record_tree(self.m.repos / "nexmark")
        root = self.m.env(ENV_PREPARE_EUID="0")
        evil = self.m.write(self.m.repos / "nexmark" / "nexmark-flink" / "src" / "main" / "java" / "evil" / "Evil.java", "class Evil {}\n")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)  # status says nothing: FAKE_GIT_DIRTY is unset
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "nexmark-jar failed"))
        self.assertIn(f"is not what the commit {PINNED[:12]} holds, not built: nexmark-flink/src/main/java/evil/Evil.java is not in the commit",
                      self.step(r, "nexmark-jar")["detail"])
        self.assertNotIn("mvn", self.log())
        self.assertFalse((self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())
        self.assertEqual([p.name for p in self.m.prefix.iterdir() if "nexmark-build" in p.name], [])  # nothing left behind
        evil.unlink()
        pom = self.m.repos / "nexmark" / "nexmark-flink" / "pom.xml"
        self.m.write(pom, "<project>changed</project>\n")
        _, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertIn("not built: nexmark-flink/pom.xml differs from the commit", self.step(r, "nexmark-jar")["detail"])
        pom.unlink()
        _, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertIn("not built: nexmark-flink/pom.xml is missing", self.step(r, "nexmark-jar")["detail"])
        (self.m.repos / "nexmark" / ".git" / "FAKE_TREE").unlink()  # the commit cannot be described: nothing is built either
        _, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertIn(f"not built: the commit {PINNED[:12]} cannot be listed (git ls-tree failed)", self.step(r, "nexmark-jar")["detail"])
        self.assertNotIn("mvn", self.log())
        # the same checkout, built by the user who owns it into a prefix they can write: as it is
        self.m.checkout()
        _, _, _, r = self.run_tool("--json")
        self.assertEqual(self.step(r, "nexmark-jar")["builds_from"], "the checkout")

    def test_a_privileged_install_builds_in_a_verified_root_owned_copy(self):
        self.m.checkout()
        code, _, err, r = self.run_tool("--json", "apply", "--yes", env=self.m.env(ENV_PREPARE_EUID="0"))
        self.assertEqual((code, r["success"], err), (0, True, ""))
        actions = [c["action"] for c in r["changes"]]
        self.assertEqual(actions, ["downloaded", "unpacked", "symlinked", "configured", "verified copy", "built+deployed", "laid out"])
        files = len(record_tree(self.m.repos / "nexmark"))
        self.assertTrue(r["changes"][4]["detail"].startswith(f"{files} file(s) of {self.m.repos / 'nexmark'} at {PINNED[:12]} copied into a root-owned build directory, every one equal to the commit"))
        self.assertIn(" via a verified root-owned copy -> ", r["changes"][5]["detail"])
        # the runnable tree comes from that copy as well, and the copy is gone afterwards
        self.assertIn("/.nexmark-build-", r["changes"][6]["detail"])
        self.assertEqual([p.name for p in self.m.prefix.iterdir() if "nexmark-build" in p.name], [])
        self.assertFalse((self.m.repos / "nexmark" / "nexmark-flink" / "target").exists())  # the checkout is not built in
        self.assertIn(f"mvn -pl nexmark-flink -am package -DskipTests JAVA_HOME={self.m.jdk} HOME={self.m.home}\n", self.m.log.read_text(encoding="utf-8"))
        # without the privilege the user's checkout is built in place: no copy on record
        shutil.rmtree(self.m.nexmark_home)
        for stale in self.m.prefix.glob("flink-*/lib/*nexmark*.jar"):
            stale.unlink()
        _, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual([c["action"] for c in r["changes"]], ["skipped", "built+deployed", "laid out"])  # Flink was already installed
        self.assertTrue((self.m.repos / "nexmark" / "nexmark-flink" / "target" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())

    def test_a_privileged_install_refuses_a_moving_ref(self):
        """A tag / branch names whatever was last pushed to it, so with root installing it is
        the checkout's owner — not the operator who consented — who decides what ends up in
        the cluster's classpath: plan and apply refuse the ref outright (refusing before
        anything runs; a user installing into a prefix they can write keeps the right)."""
        root = self.m.env(ENV_PREPARE_EUID="0")
        for ref in ("v0.3", "master"):
            code, _, _, r = self.run_tool("--json", "--nexmark-ref", ref, env=root)
            self.assertEqual((code, r["success"], r["steps"]), (2, False, []))
            self.assertIn(f"refused: --nexmark-ref {ref} is not a commit", r["error_message"])
            self.assertIn("40 hex digits", r["next_step"])
            code, _, _, r = self.run_tool("--json", "apply", "--yes", "--nexmark-ref", ref, env=root)
            self.assertEqual((code, r["success"]), (2, False))
            self.assertIn(f"refused: --nexmark-ref {ref} is not a commit", r["error_message"])
        self.assertFalse(self.m.log.exists())  # not one stub ran: the refusal comes before any probing
        self.assertIn("REFUSED: --nexmark-ref v0.3 is not a commit", self.run_tool("--nexmark-ref", "v0.3", env=root)[1])
        # the ref is not in play under --no-nexmark: nothing to refuse
        code, _, _, r = self.run_tool("--json", "--no-nexmark", "--nexmark-ref", "v0.3", env=root)
        self.assertEqual((code, r["success"]), (0, True))

    def test_a_user_installing_their_own_prefix_builds_a_tag(self):
        """Without the privilege the moving ref is the user's own business: their prefix,
        their build — the tag is cloned by name and the commit it names is on record."""
        _, _, _, r = self.run_tool("--json", "--nexmark-ref", "v0.3")
        jar = self.step(r, "nexmark-jar")
        self.assertEqual((jar["status"], jar["builds_from"], jar["ref"]), ("todo", "the checkout", "v0.3"))
        self.assertIn("git clone --depth 1 --branch v0.3 " + NEXMARK_REPO + " <tmp>/nexmark", jar["plan"][0])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--nexmark-ref", "v0.3")
        self.assertEqual((code, r["success"], [s["status"] for s in r["steps"]]), (0, True, ["done"] * 3))
        cloned = [c for c in r["changes"] if c["action"] == "cloned"]
        self.assertEqual(cloned, [{"step": "nexmark-jar", "action": "cloned",
                                   "detail": f"nexmark {PINNED} into {self.m.repos / 'nexmark'}"}])
        self.assertTrue((self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())
        self.assertIn("clone --depth 1 --branch v0.3 " + NEXMARK_REPO, self.m.log.read_text(encoding="utf-8"))

    def test_refs_replace_cannot_swap_the_pinned_commit(self):
        """refs/replace in the checkout would make ls-tree of the pinned SHA describe
        another tree while rev-parse and status still answer clean: every read query runs
        with GIT_NO_REPLACE_OBJECTS=1, so the forgery cannot become the build's baseline."""
        self.m.installed_flink()
        self.m.checkout()
        self.m.write(self.m.repos / "nexmark" / "nexmark-flink" / "src" / "main" / "java" / "evil" / "Evil.java", "class Evil {}\n")
        record_tree(self.m.repos / "nexmark", "FAKE_REPLACE_TREE")  # the tree the replacement would name
        root = self.m.env(ENV_PREPARE_EUID="0")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "nexmark-jar failed"))
        self.assertIn(f"is not what the commit {PINNED[:12]} holds, not built: "
                      "nexmark-flink/src/main/java/evil/Evil.java is not in the commit",
                      self.step(r, "nexmark-jar")["detail"])
        self.assertNotIn("mvn", self.log())
        seen = (self.m.repos / "nexmark" / ".git" / "FAKE_REPLACE_SEEN").read_text(encoding="utf-8")
        self.assertEqual(seen.splitlines(), ["1"])  # the read query reached git with replace off
        self.assertEqual([p.name for p in self.m.prefix.iterdir() if "nexmark-build" in p.name], [])

    def test_the_tree_baseline_is_the_pinned_sha_not_the_movable_head(self):
        """HEAD is a ref the checkout's owner moves; the baseline is read at the pinned SHA.
        A HEAD naming another tree than the pinned commit cannot become what root builds."""
        self.m.installed_flink()
        self.m.checkout()
        self.m.write(self.m.repos / "nexmark" / "nexmark-flink" / "src" / "main" / "java" / "evil" / "Evil.java", "class Evil {}\n")
        record_tree(self.m.repos / "nexmark", "FAKE_HEAD_TREE")  # what ls-tree of the movable HEAD answers
        root = self.m.env(ENV_PREPARE_EUID="0")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "nexmark-jar failed"))
        self.assertIn("nexmark-flink/src/main/java/evil/Evil.java is not in the commit",
                      self.step(r, "nexmark-jar")["detail"])
        self.assertNotIn("mvn", self.log())
        self.assertFalse((self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())

    def test_the_guards_are_rechecked_after_the_copy(self):
        """A checkout whose HEAD moves after the first guards ran — between the copy and the
        baseline read — is not built: the guards are taken again once the copy exists."""
        self.m.installed_flink()
        self.m.checkout()
        env = self.m.env(ENV_PREPARE_EUID="0", FAKE_GIT_HEAD_AFTER="2:" + "e" * 40)
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "nexmark-jar failed"))
        self.assertIn(f"is not built: it is at {'e' * 40}, not the requested {PINNED}",
                      self.step(r, "nexmark-jar")["detail"])
        self.assertNotIn("mvn", self.log())
        self.assertEqual([p.name for p in self.m.prefix.iterdir() if "nexmark-build" in p.name], [])
        self.assertFalse((self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())

    def test_under_sudo_git_runs_as_the_invoking_user_and_root_builds_a_verified_copy(self):
        env = self.m.env(ENV_PREPARE_EUID="0", SUDO_USER="builder")
        _, _, _, r = self.run_tool("--json", env=env)
        jar = self.step(r, "nexmark-jar")
        self.assertEqual((jar["status"], jar["builds_from"]), ("todo", "a verified copy"))
        self.assertEqual(jar["plan"][0], "runuser -u builder -- " + clone_text("<tmp>/nexmark").replace(" && ", " && runuser -u builder -- ")
                         + f"   (retries 3; the checkout then moves into place as {self.m.repos / 'nexmark'})")
        self.assertEqual(jar["plan"][1], f"# JAVA_HOME={self.m.jdk} mvn -pl nexmark-flink -am package -DskipTests   (in a root-owned copy of {self.m.repos / 'nexmark'}, "
                                         f"verified file by file against {PINNED[:12]}; Maven settings and local repository are root's)")
        self.assertEqual(jar["plan"][2], f"# cp nexmark-flink/target/nexmark-flink-*.jar (not -sources) from that copy -> {self.m.prefix / f'flink-{VERSION}'}/lib/")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"]), (0, True))
        raw = self.m.log.read_text(encoding="utf-8")
        self.assertIn("runuser -u builder -- git init -q ", raw)
        self.assertIn("runuser -u builder -- git -C ", raw)
        # the trust queries too: root never runs git inside their checkout — and the tree is
        # read at the pinned SHA, not at the movable HEAD
        self.assertIn(f"runuser -u builder -- git -C {self.m.repos / 'nexmark'} ls-tree -r -z {PINNED}", raw)
        self.assertNotIn("\ngit -C ", raw)
        # the build is root's, not handed back: no runuser in front of mvn, root's own HOME
        self.assertEqual([l for l in raw.splitlines() if l.startswith("runuser") and " mvn " in l], [])
        plain = [l for l in raw.splitlines() if l.split(" ")[:1] == ["mvn"]]
        self.assertEqual(plain, [f"mvn -pl nexmark-flink -am package -DskipTests JAVA_HOME={self.m.jdk} HOME={pwd.getpwnam('root').pw_dir}"])
        actions = [c["action"] for c in r["changes"]]
        self.assertEqual(actions, ["downloaded", "unpacked", "symlinked", "configured", "cloned", "verified copy", "built+deployed", "laid out"])
        (self.m.bin / "runuser").unlink()
        # runuser gone from PATH (a fresh prefix: the jar is not deployed there yet)
        fresh = self.m.sys / "opt2"
        proc = subprocess.run([sys.executable, str(SCRIPT), "--json", "--prefix", str(fresh), "--nexmark-home", str(fresh / "nexmark"),
                               "--repos-dir", str(self.tmp / "elsewhere")], env=self.m.env(ENV_PREPARE_EUID="0", SUDO_USER="builder", PATH=f"{self.m.bin}:/bin"),
                              cwd=str(self.tmp), capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        jar = self.step(json.loads(proc.stdout)[KEY], "nexmark-jar")
        self.assertEqual(jar["status"], "blocked")
        self.assertIn("runuser is not on PATH (needed to run git as the invoking user)", jar["detail"])

    @unittest.skipUnless(demoted_user_can_create_dirs(),
                         "switching to another uid needs root, setpriv and a filesystem with room for a non-root user")
    def test_under_sudo_the_demoted_clone_reaches_what_root_created_for_it(self):
        """The permission boundary of a real sudo run, played for real: the clone runs as
        another uid (the fake runuser switches when the case runs as root), so a directory
        root keeps to itself makes it fail — the repos directory this run creates and every
        clone's temporary directory are handed to that user instead."""
        import pwd as pwd_module
        try:
            entry = pwd_module.getpwnam("nobody")
        except KeyError:
            self.skipTest("no 'nobody' account on this machine")
        self.assertFalse(self.m.repos.exists())  # created by the run itself
        self.tmp.chmod(0o755)  # the demoted clone has to reach the fake machine
        self.m.log.write_text("", encoding="utf-8")
        self.m.log.chmod(0o666)
        env = self.m.env(ENV_PREPARE_EUID="0", SUDO_USER="nobody")
        code, _, err, r = self.run_tool("--json", "apply", "--yes", env=env)
        self.assertEqual((code, r["success"], err), (0, True, ""))
        self.assertEqual([s["status"] for s in r["steps"]], ["done"] * 3)
        # the repos directory root created and the checkout cloned into it belong to that user
        self.assertEqual(self.m.repos.lstat().st_uid, entry.pw_uid)
        self.assertEqual((self.m.repos / "nexmark").lstat().st_uid, entry.pw_uid)
        self.assertEqual([c["action"] for c in r["changes"]][4:7], ["created directory", "cloned", "verified copy"])
        self.assertEqual(r["changes"][4]["detail"], f"{self.m.repos} for nobody")
        self.assertEqual([p.name for p in self.m.repos.iterdir()], ["nexmark"])  # no clone directory left behind
        self.assertFalse((self.m.repos / "nexmark" / "nexmark-flink" / "target").exists())  # their checkout is not built in
        self.assertTrue((self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())

    def test_the_jar_goes_into_the_version_being_installed(self):
        old = self.m.installed_flink()  # the symlink points at 1.19.2, whose lib already holds a nexmark jar
        self.m.write(old / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "old\n")
        self.m.fixture_tgz(version="1.19.3", name="flink-1.19.3-bin-scala_2.12.tgz")
        _, _, _, r = self.run_tool("--json", "--version", "1.19.3")
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]][1], ("nexmark-jar", "todo"))  # not "already deployed"
        self.assertEqual(self.step(r, "nexmark-jar")["lib"], str(self.m.prefix / "flink-1.19.3" / "lib"))
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--version", "1.19.3")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertTrue((self.m.prefix / "flink-1.19.3" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())
        self.assertEqual((old / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(os.path.realpath(self.m.prefix / "flink"), str(self.m.prefix / "flink-1.19.3"))
        self.assertIn("--version 1.19.3", r["next_step"])

    def test_tree_blocked_when_jar_deployed_but_no_source(self):
        self.m.installed_flink()
        self.m.write(self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "jar\n")
        self.m.script(self.m.nexmark_home / "bin" / "run_query.sh")
        _, _, _, r = self.run_tool("--json")
        tree = self.step(r, "nexmark-tree")
        self.assertEqual(tree["status"], "blocked")
        self.assertIn("nexmark source resources not found at", tree["detail"])
        self.assertEqual(tree["fix"], clone_text(self.m.repos / "nexmark"))
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, r["error_message"][:9]), (2, "refused: "))

    def test_tree_is_completed_and_what_is_there_is_kept(self):
        self.m.installed_flink()
        self.m.write(self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "jar\n")
        self.m.checkout()
        self.m.laid_out_tree(run_query="#!/bin/sh\necho mine\n")  # a tree of the user's that lacks nothing…
        for rel in MANIFEST[2:]:  # …until the query definitions go missing
            (self.m.nexmark_home / rel).unlink()
        code, _, _, r = self.run_tool("--json", "apply", "--yes")
        self.assertEqual((code, [s["status"] for s in r["steps"]]), (0, ["skipped", "skipped", "done"]))
        self.assertEqual((r["changes"][-1]["action"], r["changes"][-1]["step"]), ("completed", "nexmark-tree"))
        self.assertEqual(r["changes"][-1]["detail"].split(" file(s) from ")[0], "8")
        for rel in MANIFEST:
            self.assertTrue((self.m.nexmark_home / rel).is_file(), rel)
        self.assertEqual((self.m.nexmark_home / "bin" / "run_query.sh").read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")  # not overwritten
        self.assertEqual(self.step(r, "nexmark-tree")["detail"], f"runnable tree at {self.m.nexmark_home}")
        self.assertTrue((self.m.nexmark_home / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar").exists())

    def test_the_tree_is_completed_from_a_verified_copy_when_root_installs(self):
        self.m.installed_flink()
        self.m.write(self.m.prefix / "flink" / "lib" / "nexmark-flink-0.3-SNAPSHOT.jar", "jar\n")
        self.m.checkout()
        root = self.m.env(ENV_PREPARE_EUID="0")
        self.m.script(self.m.nexmark_home / "bin" / "run_query.sh")  # only the launcher is there
        _, _, _, r = self.run_tool("--json", env=root)
        tree = self.step(r, "nexmark-tree")
        self.assertIn(" — a root-owned copy verified against the commit; existing files are kept", tree["plan"][1])
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertEqual((code, [s["status"] for s in r["steps"]]), (0, ["skipped", "skipped", "done"]))
        self.assertEqual([c["action"] for c in r["changes"]], ["skipped", "skipped", "verified copy", "completed"])
        self.assertEqual(r["changes"][2]["step"], "nexmark-tree")
        self.assertIn("/.nexmark-build-", r["changes"][3]["detail"])
        self.assertEqual([p.name for p in self.m.prefix.iterdir() if "nexmark-build" in p.name], [])
        # a checkout that is not the commit is not laid out by root either, launcher or not
        shutil.rmtree(self.m.nexmark_home)
        self.m.write(self.m.repos / "nexmark" / "nexmark-flink" / "src" / "main" / "resources" / "conf" / "nexmark.yaml", "changed: true\n")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", env=root)
        self.assertEqual((code, r["error_message"], self.step(r, "nexmark-tree")["status"]), (1, "nexmark-tree failed", "failed"))
        self.assertIn("is not what the commit " + PINNED[:12] + " holds, not used: nexmark-flink/src/main/resources/conf/nexmark.yaml differs from the commit",
                      self.step(r, "nexmark-tree")["detail"])
        self.assertFalse(self.m.nexmark_home.exists())

    def test_no_nexmark_limits_every_step(self):
        _, _, _, r = self.run_tool("--json", "--no-nexmark")
        self.assertEqual([s["name"] for s in r["steps"]], ["flink"])
        self.assertIsNone(r["nexmark_home"])
        self.assertEqual(r["next_step"], "review the plan above, then run " + self.reviewed("apply", yes=True) + " --no-nexmark")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--no-nexmark")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertFalse((self.m.repos / "nexmark").exists())
        self.assertNotIn("fetch", self.log())
        _, _, _, r = self.run_tool("--json", "verify", "--no-nexmark")
        self.assertEqual(r["checks"][1]["status"], "disabled")

    def test_minimal_conf_goes_to_the_file_flink_reads(self):
        # a 1.19 distribution ships config.yaml: the keys go there and no flink-conf.yaml appears
        self.m.fixture_tgz(comments_only=True, conf_name="config.yaml")
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--no-nexmark")
        self.assertEqual((code, r["success"]), (0, True))
        dist = self.m.prefix / f"flink-{VERSION}"
        self.assertTrue((dist / "conf" / "config.yaml").read_text(encoding="utf-8").rstrip().endswith(MINIMAL_CONF_LAST))
        self.assertFalse((dist / "conf" / "flink-conf.yaml").exists())
        # both files, Flink reads flink-conf.yaml only: config.yaml is left alone (another version, same prefix)
        self.m.fixture_tgz(version="1.19.3", name="flink-1.19.3-bin-scala_2.12.tgz", comments_only=True,
                           also=("config.yaml", "rest.port: 8081\n"))
        code, _, _, r = self.run_tool("--json", "apply", "--yes", "--no-nexmark", "--version", "1.19.3")
        self.assertEqual((code, r["success"]), (0, True))
        conf = self.m.prefix / "flink-1.19.3" / "conf"
        self.assertTrue((conf / "flink-conf.yaml").read_text(encoding="utf-8").rstrip().endswith(MINIMAL_CONF_LAST))
        self.assertEqual((conf / "config.yaml").read_text(encoding="utf-8"), "rest.port: 8081\n")

    def test_a_failing_step_command_is_the_step_failure_and_keeps_the_log(self):
        code, _, _, r = self.run_tool_with_faults("tar=missing", "--json", "apply", "--yes")
        self.assertEqual((code, r["success"], r["error_message"]), (1, False, "flink failed"))
        self.assertIn("FileNotFoundError", self.step(r, "flink")["detail"])
        self.assertEqual([(s["name"], s["status"]) for s in r["steps"]], [("flink", "failed"), ("nexmark-jar", "todo"), ("nexmark-tree", "todo")])
        self.assertEqual([c["action"] for c in r["changes"]], ["downloaded", "failed"])
        self.assertEqual(r["changes"][-1]["detail"], self.step(r, "flink")["detail"].split("; ")[-1])
        self.assertNotIn("fetch", self.log())
        code, _, _, r = self.run_tool_with_faults("mvn=timeout", "--json", "apply", "--yes")
        self.assertEqual((code, r["error_message"], self.step(r, "nexmark-jar")["status"]), (1, "nexmark-jar failed", "failed"))
        self.assertIn("TimeoutExpired", self.step(r, "nexmark-jar")["detail"])
        self.assertEqual([c["action"] for c in r["changes"]], ["downloaded", "unpacked", "symlinked", "configured", "cloned", "failed"])
        self.assertTrue((self.m.repos / "nexmark" / ".git" / "HEAD").exists())  # the checkout made before the failure stays

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

    def test_verify_reports_missing_siblings_as_not_verified(self):
        code, out, _, r = self.run_tool("--json", "verify")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual([(c["name"], c["status"], c["skill"]) for c in r["checks"]],
                         [("flink-dist", "missing", "flink-velox-flink-dist-check"), ("nexmark", "missing", "flink-velox-nexmark-check")])
        self.assertEqual(r["error_message"], "2 check(s) not run (sibling skills not installed): flink-velox-flink-dist-check, flink-velox-nexmark-check")
        self.assertEqual(r["next_step"], "install the missing check skill(s) (flink-velox-flink-dist-check, flink-velox-nexmark-check), "
                         "then rerun " + self.reviewed("verify"))
        report = self.run_tool("verify")[1]
        self.assertIn("  MISSING  flink-dist: flink-velox-flink-dist-check is not installed next to this skill", report)
        self.assertIn("NOT VERIFIED: 2 check(s) not run (sibling skills not installed): flink-velox-flink-dist-check, flink-velox-nexmark-check", report)
        self.assertNotIn("\nVERIFIED", report)

    def test_verify_finds_the_siblings_in_the_flat_install_layout(self):
        # install.sh copies every skill to <config>/skills/<skill>/: no family dir, resolution goes by skill name
        skills = self.tmp / "config" / "skills"
        shutil.copytree(ROOT, skills / "flink-velox-runtime-install", ignore=shutil.ignore_patterns("results", "__pycache__"))
        self.m.fake_family(skills)
        proc = subprocess.run([sys.executable, str(skills / "flink-velox-runtime-install" / "scripts" / "runtime_install.py"), "--json", "verify", *self.m.args()],
                              env=self.m.env(ENV_PREPARE_ENV_FAMILY_DIR=None), cwd=str(self.tmp), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        r = json.loads(proc.stdout)[KEY]
        self.assertEqual((proc.returncode, [(c["name"], c["status"]) for c in r["checks"]]), (0, [("flink-dist", "ok"), ("nexmark", "ok")]))

    # -- shape, --out, usage, crash -------------------------------------------
    def test_json_contract_shape_and_crash_shape(self):
        _, _, _, plan = self.run_tool("--json")
        _, _, _, applied = self.run_tool("--json", "apply", "--yes")
        _, _, _, verified = self.run_tool("--json", "verify")
        for r in (plan, applied, verified):
            self.assertEqual(set(r), {"success", "checked_at", "command", "version", "prefix", "flink_home", "nexmark_home",
                                      "steps", "changes", "root_steps", "next_step", "error_message"} | ({"checks"} if r is verified else set()))
        self.assertEqual(set(plan["steps"][0]), {"name", "actions", "needs_root", "dist", "link", "tgz", "conf", "pending", "status", "detail", "plan"})
        self.assertEqual(set(plan["steps"][1]), {"name", "actions", "needs_root", "src", "lib", "ref", "checkout", "status", "detail", "plan", "jdk", "builds_from"})
        module = load_module()

        def boom(opts):
            raise RuntimeError("probe exploded")

        module.step_flink = boom
        buffer = io.StringIO()
        # in-process on purpose (to intercept the step builder) — inside the fake environment, so
        # nothing the script could reach for points at the real machine
        with patch.dict(os.environ, self.m.env(), clear=True), redirect_stdout(buffer):
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
        code, _, err, r = self.run_tool("--json", "--out", str(archive))
        self.assertEqual((code, archive.read_text(encoding="utf-8")), (2, first))
        self.assertIn("--overwrite", err)
        self.assertTrue(r["error_message"].startswith("refused: --out already exists"))
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
        # a bad --out is refused before anything runs: apply leaves the machine untouched
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        before = self.m.snapshot()
        code, _, err, r = self.run_tool("--json", "apply", "--yes", "--out", str(self.tmp / "outside.json"), cwd=str(elsewhere))
        self.assertEqual((code, r["success"], r["steps"], r["changes"]), (2, False, [], []))
        self.assertTrue(r["error_message"].startswith("refused: --out must be a regular path inside the project directory"))
        self.assertEqual((self.m.snapshot(), self.log()), (before, ""))
        self.assertFalse((self.m.prefix / f"flink-{VERSION}").exists())

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
