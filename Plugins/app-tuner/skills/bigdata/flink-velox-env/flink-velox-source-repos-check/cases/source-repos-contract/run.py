#!/usr/bin/env python3
"""source-repos-contract — tier-A case for flink-velox-source-repos-check.

Drives scripts/source_repos_check.py against a fake machine laid out in a scratch
directory — three checkouts with their marker files and a .git/HEAD, a fake PATH
whose git is an sh stub (or absent) — with nothing from the real machine in play;
nothing, writes nowhere except the optional --out file). Pins the verdict
contract (exit codes, success, status, missing, next_step), every probe rule
(root via --repos-dir / GFV_REPOS_DIR / repos walked upward — an existing
explicit candidate is the root as given, empty or not — per-checkout pinning,
marker files, branch and HEAD through git, off-baseline reported not failed,
git absent or not a checkout = warning), the JSON shape (also for a crash), --out
safety (inside the project, no symlink, no silent overwrite) and that a check
leaves the machine untouched.

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
import stat
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "source_repos_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_source_repos_check_result"
# name -> (marker files, baseline branch, upstream), pinned independently of the script
REPOS = {
    "velox": (("CMakeLists.txt", "scripts"), "gluten-20260829", "https://github.com/bigo-sg/velox.git"),
    "velox4j": (("pom.xml",), "gluten-20260829", "https://github.com/bigo-sg/velox4j.git"),
    "gluten": (("gluten-flink/pom.xml",), "main", "https://github.com/apache/gluten.git"),
}
ENV_OF = {"velox": "VELOX_HOME", "velox4j": "VELOX4J_HOME", "gluten": "GLUTEN_HOME"}

GIT_STUB = textwrap.dedent('''\
    # fake git: -C <repo> rev-parse ... / remote get-url
    if [ "$1" = "--version" ]; then echo "git version 2.43.0"; exit 0; fi
    dir="$2"; shift 2
    [ -d "$dir/.git" ] || { echo "fatal: not a git repository" >&2; exit 128; }
    name="${dir##*/}"
    case "$1 $2" in
      "rev-parse --abbrev-ref")
        if [ -n "${FAKE_GIT_BRANCH:-}" ]; then echo "$FAKE_GIT_BRANCH"
        else case "$name" in
          velox|velox4j) echo gluten-20260829 ;;
          *) echo main ;;
        esac; fi ;;
      "rev-parse --short") echo 0123abc ;;
      "remote get-url") echo "${FAKE_GIT_ORIGIN:-https://example.invalid/$name.git}" ;;
      *) exit 1 ;;
    esac
    ''')


def load_module():
    """source_repos_check.py as an in-process module (for the crash path a
    subprocess cannot be driven into hermetically)."""
    spec = importlib.util.spec_from_file_location("source_repos_check_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMachine:
    """A whole machine in a directory: fake PATH, three checkouts under repos/."""

    def __init__(self, root):
        self.root = Path(root)
        self.bin = self.root / "bin"
        self.repos = self.root / "repos"
        self.bin.mkdir(parents=True)
        self.git = self.script(self.bin / "git", GIT_STUB)

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def checkout(self, name, home=None, without=(), git=True):
        home = Path(home) if home else self.repos / name
        markers, _, _ = REPOS[name]
        for marker in markers:
            if marker in without:
                continue
            if marker == "scripts":  # a directory in a real velox checkout
                self.write(home / marker / ".keep", "")
            else:
                self.write(home / marker, "placeholder\n")
        if git:
            self.write(home / ".git" / "HEAD", "ref: refs/heads/main\n")
        return home

    def checkouts(self):
        for name in REPOS:
            self.checkout(name)

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "HOME": str(self.root), "LC_ALL": "C",
               "GFV_REPOS_DIR": str(self.repos),
               # the upward search for repos/ never leaves the fake machine
               "ENV_CHECK_WALK_ROOT": str(self.root)}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def snapshot(self):
        """(path -> kind, mode, size, mtime_ns) of every entry under root/."""
        seen = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            base = Path(dirpath)
            for name in dirnames:
                seen[str(base / name)] = ("d", (base / name).lstat().st_mode)
            for name in filenames:
                st = (base / name).lstat()
                seen[str(base / name)] = ("f", st.st_mode, st.st_size, st.st_mtime_ns)
        return seen


class SourceReposCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def run_check(self, *args, env=None, cwd=None):
        """Run source_repos_check.py; returns (exit code, stdout, stderr, result or None)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env,
            cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=60)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def item(self, result, name):
        return next(i for i in result["items"] if i["name"] == name)

    def test_all_three_present_via_repos_dir_env(self):
        self.m.checkouts()
        code, out, err, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"], r["missing"], r["error_message"]),
                         (0, True, "ok", [], ""))
        self.assertEqual((r["root"], r["root_via"]), (str(self.m.repos), "GFV_REPOS_DIR"))
        self.assertEqual([i["name"] for i in r["items"]], ["velox", "velox4j", "gluten"])
        for name, (_, branch, url) in REPOS.items():
            self.assertEqual(self.item(r, name)["detail"], f"{branch} @ 0123abc ({self.m.repos / name})")
            self.assertIs(self.item(r, name)["on_baseline"], True)
            entry = r["checkouts"][name]
            self.assertEqual((entry["branch"], entry["head"], entry["origin"], entry["on_baseline"],
                              entry["baseline_branch"], entry["pinned_via"]),
                             (branch, "0123abc", f"https://example.invalid/{name}.git", True, branch, None))
            self.assertNotIn("error", entry)
        self.assertEqual(r["next_step"], f"ready: the three source checkouts are in place at {self.m.repos}")
        _, out, _, _ = self.run_check()
        self.assertIn("source-repos OK       velox gluten-20260829 @ 0123abc (", out)
        self.assertIn(f"READY: the three source checkouts are in place at {self.m.repos}", out)
        self.assertEqual(err, "")

    def test_explicit_repos_dir_wins(self):
        self.m.checkouts()
        _, _, _, r = self.run_check("--json", "--repos-dir", str(self.m.repos))
        self.assertEqual((r["root_via"], r["root"]), ("--repos-dir", str(self.m.repos)))

    def test_repos_dir_walked_up_and_bounded(self):
        self.m.checkouts()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        # from the machine root: repos/ sits one level down, reached by walking up
        env = self.m.env(GFV_REPOS_DIR=None)
        _, _, _, r = self.run_check("--json", env=env, cwd=str(self.m.root))
        self.assertEqual((r["root_via"], r["root"]), ("repos/ walked up from the working directory", str(self.m.repos)))
        # from elsewhere with the boundary there: the ancestor repos/ is never reached
        env = self.m.env(GFV_REPOS_DIR=None, ENV_CHECK_WALK_ROOT=str(elsewhere))
        code, _, _, r = self.run_check("--json", env=env, cwd=str(elsewhere))
        self.assertEqual((code, r["success"], r["root"], r["root_via"]), (1, False, None, None))
        self.assertEqual(r["error_message"], "3 item(s) missing: velox, velox4j, gluten")
        fix = self.item(r, "velox4j")["fix"]
        self.assertEqual(fix, "git clone -b gluten-20260829 https://github.com/bigo-sg/velox4j.git "
                         "(pass --repos-dir or set GFV_REPOS_DIR), or set VELOX4J_HOME")
        self.assertIn("next: clone the missing checkouts (", self.run_check(env=env, cwd=str(elsewhere))[1])
        # a repos/ ABOVE the boundary (an ancestor of the working directory) is reached
        # only when the boundary seam is absent
        ancestor = self.tmp / "repos"
        self.m.checkout("velox", home=ancestor / "velox")
        _, _, _, r = self.run_check("--json", env=env, cwd=str(elsewhere))  # boundary = elsewhere
        self.assertIsNone(r["root"])
        _, _, _, r = self.run_check("--json", env=self.m.env(GFV_REPOS_DIR=None, ENV_CHECK_WALK_ROOT=None), cwd=str(elsewhere))
        self.assertEqual((r["root"], r["root_via"]), (str(ancestor), "repos/ walked up from the working directory"))

    def test_explicit_repos_dir_is_the_root_as_given_empty_or_not(self):
        self.m.checkouts()
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, _, r = self.run_check("--json", "--repos-dir", str(empty))
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        self.assertEqual((r["root"], r["root_via"]), (str(empty), "--repos-dir"))
        self.assertEqual(r["error_message"], "3 item(s) missing: velox, velox4j, gluten")
        for name, (_, branch, url) in REPOS.items():
            item = self.item(r, name)
            self.assertEqual((item["status"], item["detail"]),
                             ("fail", f"no {name} checkout at {empty / name}"))
            self.assertEqual(item["fix"], f"git clone -b {branch} {url} into {empty}, or set {ENV_OF[name]}")
            self.assertEqual(r["checkouts"][name]["error"], "missing")
        self.assertTrue(r["next_step"].endswith(f"into {empty}, then rerun"))
        self.assertNotIn(str(self.m.repos), out)  # the verdict is about the given dir, not another one
        self.assertIn("NOT READY: 3 item(s) missing", self.run_check("--repos-dir", str(empty))[1])

    def test_explicit_repos_dir_wins_over_a_qualified_repos_walked_up(self):
        self.m.checkouts()
        foreign = self.tmp / "foreign"
        (foreign / "not-a-repo").mkdir(parents=True)
        # the working directory sits in a tree whose repos/ holds all three: the
        # given dir still wins, the walk-up is never used as a fallback
        env = self.m.env(GFV_REPOS_DIR=None)
        code, out, _, r = self.run_check("--json", "--repos-dir", str(foreign), env=env, cwd=str(self.m.root))
        self.assertEqual((r["root"], r["root_via"]), (str(foreign), "--repos-dir"))
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(self.item(r, "velox")["detail"], f"no velox checkout at {foreign / 'velox'}")
        self.assertEqual(self.item(r, "velox")["fix"],
                         f"git clone -b gluten-20260829 https://github.com/bigo-sg/velox.git "
                         f"into {foreign}, or set VELOX_HOME")
        self.assertNotIn(str(self.m.repos), out)

    def test_gfv_repos_dir_is_the_root_as_given_empty_or_not(self):
        self.m.checkouts()
        bare = self.tmp / "bare"
        bare.mkdir()
        env = self.m.env(GFV_REPOS_DIR=str(bare))
        code, out, _, r = self.run_check("--json", env=env, cwd=str(self.m.root))
        self.assertEqual((r["root"], r["root_via"]), (str(bare), "GFV_REPOS_DIR"))
        self.assertEqual((code, r["success"]), (1, False))
        gluten = self.item(r, "gluten")
        self.assertEqual((gluten["status"], gluten["detail"]), ("fail", f"no gluten checkout at {bare / 'gluten'}"))
        self.assertEqual(gluten["fix"], f"git clone -b main https://github.com/apache/gluten.git "
                         f"into {bare}, or set GLUTEN_HOME")
        self.assertNotIn(str(self.m.repos), out)

    def test_walk_up_skips_a_repos_dir_without_any_checkout(self):
        self.m.checkouts()
        nested = self.m.root / "a" / "b"
        (nested / "repos").mkdir(parents=True)  # exists, holds none of the three
        env = self.m.env(GFV_REPOS_DIR=None)
        _, _, _, r = self.run_check("--json", env=env, cwd=str(nested))
        self.assertEqual((r["root"], r["root_via"]),
                         (str(self.m.repos), "repos/ walked up from the working directory"))
        self.assertEqual(r["success"], True)

    def test_repos_dir_that_does_not_exist_falls_to_the_next_candidate(self):
        self.m.checkouts()
        missing = self.tmp / "no-such-dir"
        _, _, _, r = self.run_check("--json", "--repos-dir", str(missing))  # GFV_REPOS_DIR answers
        self.assertEqual((r["root"], r["root_via"]), (str(self.m.repos), "GFV_REPOS_DIR"))
        self.assertEqual(r["success"], True)
        env = self.m.env(GFV_REPOS_DIR=None)
        _, _, _, r = self.run_check("--json", "--repos-dir", str(missing), env=env, cwd=str(self.m.root))
        self.assertEqual((r["root"], r["root_via"]),
                         (str(self.m.repos), "repos/ walked up from the working directory"))

    def test_pinned_checkout_via_env(self):
        self.m.checkouts()
        other = self.m.checkout("velox", home=self.tmp / "elsewhere-velox")
        _, _, _, r = self.run_check("--json", env=self.m.env(VELOX_HOME=str(other)))
        velox = r["checkouts"]["velox"]
        self.assertEqual((velox["path"], velox["pinned_via"]), (str(other), "VELOX_HOME"))
        self.assertEqual(r["checkouts"]["velox4j"]["path"], str(self.m.repos / "velox4j"))
        self.assertEqual(r["success"], True)

    def test_missing_repo_fails_with_clone_fix(self):
        self.m.checkouts()
        shutil.rmtree(self.m.repos / "velox4j")
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        v = self.item(r, "velox4j")
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["detail"], f"no velox4j checkout at {self.m.repos / 'velox4j'}")
        self.assertEqual(v["fix"], "git clone -b gluten-20260829 https://github.com/bigo-sg/velox4j.git "
                         f"into {self.m.repos}, or set VELOX4J_HOME")
        self.assertEqual(r["checkouts"]["velox4j"]["error"], "missing")
        self.assertEqual(r["next_step"], "clone the missing checkouts (git clone -b gluten-20260829 "
                         "https://github.com/bigo-sg/velox4j.git) into " + str(self.m.repos) + ", then rerun")
        self.assertEqual(r["error_message"], "1 item(s) missing: velox4j")
        self.assertEqual((self.item(r, "velox")["status"], self.item(r, "gluten")["status"]), ("ok", "ok"))
        _, out, _, _ = self.run_check()
        self.assertIn("        fix: git clone -b gluten-20260829", out)

    def test_several_missing_next_step_lists_every_clone_in_table_order(self):
        self.m.checkouts()
        shutil.rmtree(self.m.repos / "gluten")
        shutil.rmtree(self.m.repos / "velox")
        _, _, _, r = self.run_check("--json")
        self.assertEqual(r["next_step"], "clone the missing checkouts (git clone -b gluten-20260829 "
                         "https://github.com/bigo-sg/velox.git; git clone -b main https://github.com/apache/gluten.git) "
                         f"into {self.m.repos}, then rerun")
        self.assertEqual(r["error_message"], "2 item(s) missing: velox, gluten")

    def test_unusable_checkout(self):
        self.m.checkouts()
        shutil.rmtree(self.m.repos / "velox" / "scripts")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        v = self.item(r, "velox")
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["detail"], f"{self.m.repos / 'velox'} is not a usable velox checkout (missing scripts)")
        self.assertEqual(v["fix"], "git clone -b gluten-20260829 https://github.com/bigo-sg/velox.git afresh, "
                         "or point --repos-dir (or VELOX_HOME) at a complete checkout")
        self.assertEqual(r["checkouts"]["velox"]["error"], "unusable")
        self.assertEqual(r["next_step"], "re-clone the unusable checkouts (velox), then rerun")

    def test_off_baseline_branch_is_reported_not_failed(self):
        self.m.checkouts()
        code, _, _, r = self.run_check("--json", env=self.m.env(FAKE_GIT_BRANCH="feature-x"))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.item(r, "velox")["detail"], f"feature-x @ 0123abc ({self.m.repos / 'velox'}); "
                         "baseline branch is gluten-20260829")
        self.assertIs(self.item(r, "velox")["on_baseline"], False)
        self.assertIn("not on the baseline branch: velox, velox4j, gluten", r["next_step"])

    def test_origin_is_reported_without_its_secrets(self):
        self.m.checkouts()
        # a private fork reached with an embedded password and access tokens in the query
        secret_url = ("https://build:Sup3r-Secret@github.com/org/velox.git"
                      "?access_token=ghp_TopSecret&x-access-token=abc123&token=t0k&private_token=pv&foo=bar")
        archive = self.tmp / "project" / "source-repos-check.json"
        code, out, err, r = self.run_check("--json", "--out", str(archive), env=self.m.env(FAKE_GIT_ORIGIN=secret_url))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(r["checkouts"]["velox"]["origin"], "https://github.com/org/velox.git?foo=bar")
        for secret in ("Sup3r-Secret", "ghp_TopSecret", "abc123", "t0k", "private_token=pv", "build:"):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, archive.read_text(encoding="utf-8"))
        # a user name without a password (ssh / scp-like syntax) is not a secret and stays
        for given, expected in (("ssh://git@github.com/org/velox.git", "ssh://github.com/org/velox.git"),
                                ("git@github.com:org/velox.git", "git@github.com:org/velox.git"),
                                ("https://github.com/org/velox.git?token=only-secret", "https://github.com/org/velox.git"),
                                ("https://x-access-token:ghp_x@github.com/org/velox.git", "https://github.com/org/velox.git")):
            _, _, _, r = self.run_check("--json", env=self.m.env(FAKE_GIT_ORIGIN=given))
            self.assertEqual(r["checkouts"]["gluten"]["origin"], expected, given)

    def test_git_not_on_path_is_a_warning(self):
        self.m.checkouts()
        self.m.git.unlink()
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (0, True, "warn"))
        self.assertEqual(self.item(r, "gluten")["detail"], f"{self.m.repos / 'gluten'}: git is not on PATH, "
                         "branch and HEAD not read")
        self.assertIsNone(r["checkouts"]["gluten"]["branch"])
        self.assertIn("warnings above", r["next_step"])
        self.assertIn("  WARN  velox: ", self.run_check()[1])

    def test_not_a_git_checkout_is_a_warning(self):
        self.m.checkouts()
        shutil.rmtree(self.m.repos / "gluten" / ".git")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.item(r, "gluten")["status"], "warn")
        self.assertIn("not a git checkout", self.item(r, "gluten")["detail"])

    def test_json_contract_shape_and_crash_shape(self):
        self.m.checkouts()
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "status", "root", "root_via", "checkouts", "items",
                                  "missing", "next_step", "error_message"})
        self.assertEqual(set(r["checkouts"]["velox"]), {"path", "branch", "head", "origin", "baseline_branch",
                                                        "on_baseline", "pinned_via"})
        module = load_module()

        def boom(explicit):
            raise RuntimeError("probe exploded")

        module.locate_repos_root = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["status"], a["error_message"][:15]), (2, False, "fail", "check aborted: "))
        self.assertEqual(set(a), set(r))
        self.assertEqual((a["items"], a["missing"], a["root"], a["checkouts"]), ([], [], None, {}))
        self.assertTrue(a["next_step"].startswith("fix the check error first"))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            module.main([])
        self.assertTrue(buffer.getvalue().startswith("CHECK ERROR: check aborted:"))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.m.checkouts()
        archive = self.tmp / "project" / "source-repos-check.json"
        _, stdout, _, _ = self.run_check("--json", "--out", str(archive))
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), json.loads(stdout))
        first = archive.read_text(encoding="utf-8")
        code, _, err, _ = self.run_check("--json", "--out", str(archive))
        self.assertEqual((code, archive.read_text(encoding="utf-8")), (2, first))
        self.assertIn("--overwrite", err)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        outside = self.tmp / "outside.json"
        code, _, err, _ = self.run_check("--json", "--out", str(outside), cwd=str(elsewhere))
        self.assertEqual(code, 2)
        self.assertIn("inside the project directory", err)
        self.assertFalse(outside.exists())
        target = self.tmp / "keep.json"
        target.write_text("keep me\n", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(target)
        code, _, err, _ = self.run_check("--json", "--out", str(link))
        self.assertEqual(code, 2)
        self.assertIn("not a symlink", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(self.run_check("--json", "--out", str(archive), "--overwrite")[0], 0)

    def test_out_never_opens_a_fifo(self):
        self.m.checkouts()
        fifo = self.tmp / "project" / "pipe.json"
        fifo.parent.mkdir()
        os.mkfifo(str(fifo))
        # open() on a fifo blocks until a reader appears: the guard must refuse instead
        # (a blocked check would run into the subprocess timeout of run_check and error out)
        code, _, err, _ = self.run_check("--json", "--out", str(fifo), "--overwrite")
        self.assertEqual((code, stat.S_ISFIFO(fifo.lstat().st_mode)), (2, True))
        self.assertIn("--out exists and is not a regular file, refusing to overwrite it", err)
        self.assertIn("source-repos OK", self.run_check()[1])  # the verdict itself is unaffected

    def test_invalid_argument_is_2_without_json(self):
        code, out, err, r = self.run_check("--bogus")
        self.assertEqual((code, r), (2, None))
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())

    def test_check_leaves_machine_untouched(self):
        self.m.checkouts()
        shutil.rmtree(self.m.repos / "velox4j")
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "source-repos-contract",
        "skill": "flink-velox-source-repos-check",
        "success": result.wasSuccessful(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }
    (RESULTS / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
