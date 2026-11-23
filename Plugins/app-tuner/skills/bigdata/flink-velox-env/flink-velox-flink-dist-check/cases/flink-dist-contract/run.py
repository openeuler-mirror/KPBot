#!/usr/bin/env python3
"""flink-dist-contract — tier-A case for flink-velox-flink-dist-check.

Drives scripts/flink_dist_check.py against a fake machine laid out in a scratch
directory — a sysroot with a Flink installation (stock bin/ scripts, real zip
flink-dist jars, conf/config.yaml or conf/flink-conf.yaml or neither), a fake PATH
with nothing on it or a flink symlink — with nothing from the real machine in play;
the script's seam ENV_CHECK_SYSROOT carries the fake. Pins the verdict contract
(exit codes, success, status, missing, next_step), every probe rule (installation
lookup order FLINK_HOME / PATH / /opt/flink / /usr/local/flink, the stock files,
exactly one flink-dist jar, version from the jar name then MANIFEST.MF, the 1.19
line with a free patch level, the conf warning, REST URL precedence), the JSON
shape (also for a crash), --out safety (inside the project, no symlink, no silent
overwrite) and that a check leaves the machine untouched.

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
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the case dir

CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parents[1]  # skill root
SCRIPT = ROOT / "scripts" / "flink_dist_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_flink_dist_check_result"
SCRIPTS = ("flink", "start-cluster.sh", "stop-cluster.sh", "sql-client.sh")


def load_module():
    """flink_dist_check.py as an in-process module (for the crash path a
    subprocess cannot be driven into hermetically)."""
    spec = importlib.util.spec_from_file_location("flink_dist_check_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMachine:
    """A whole machine in a directory: sysroot with Flink installations, fake PATH."""

    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        self.opt = self.sys / "opt" / "flink"
        self.local = self.sys / "usr" / "local" / "flink"
        self.flink = self.opt

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body="exit 0\n"):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def jar(self, path, manifest_version=None):
        """A real (near-empty) jar: the MANIFEST fallback test reads it."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = "Manifest-Version: 1.0\nImplementation-Title: Flink\n"
        if manifest_version:
            manifest += f"Implementation-Version: {manifest_version}\n"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("META-INF/MANIFEST.MF", manifest)
            zf.writestr("org/apache/flink/dummy.txt", "")

    def flink_home(self, home=None, version="1.19.2", dist=None, manifest_version=True,
                   conf="none", rest=("127.0.0.1", "6123")):
        """Stock bin/ scripts, exactly one flink-dist jar (dist = another jar name;
        manifest_version: True = carries `version`, a string = that one, falsy = none)
        and one conf layout (none / config.yaml / flink-conf.yaml)."""
        home = Path(home) if home else self.flink
        for stale in (home / "lib").glob("flink-dist*.jar"):
            stale.unlink()
        for name in SCRIPTS:
            self.script(home / "bin" / name)
        carried = version if manifest_version is True else (manifest_version or None)
        self.jar(home / "lib" / (dist or f"flink-dist-{version}.jar"), carried)
        conf_dir = home / "conf"
        for name in ("config.yaml", "flink-conf.yaml"):
            (conf_dir / name).unlink(missing_ok=True)
        if conf == "config.yaml":
            self.write(conf_dir / "config.yaml",
                       "rest:\n  # the REST endpoint\n  address: %s\n  port: %s\n" % rest)
        elif conf == "flink-conf.yaml":
            self.write(conf_dir / "flink-conf.yaml", "rest.address: %s\nrest.port: %s\n" % rest)

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "ENV_CHECK_SYSROOT": str(self.sys), "HOME": str(self.root),
               "LC_ALL": "C", "FLINK_HOME": str(self.flink)}
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


class FlinkDistCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def run_check(self, *args, env=None, cwd=None):
        """Run flink_dist_check.py; returns (exit code, stdout, stderr, result or None)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env,
            cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=60)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def item(self, result, name):
        return next(i for i in result["items"] if i["name"] == name)

    def test_found_via_flink_home_version_from_jar_name(self):
        self.m.flink_home(conf="none")  # no conf file: a warning, not a failure
        code, out, err, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"], r["missing"]), (0, True, "warn", []))
        f = r["flink"]
        self.assertEqual((f["path"], f["found_via"], f["version"], f["on_1_19_line"], f["version_source"]),
                         (str(self.m.flink), "FLINK_HOME", "1.19.2", True, "lib/flink-dist-1.19.2.jar"))
        self.assertEqual(f["dist_jars"], ["flink-dist-1.19.2.jar"])
        self.assertEqual((f["rest_url"], f["conf"]), ("http://localhost:8081", {}))
        self.assertEqual([(i["name"], i["status"]) for i in r["items"]], [("home", "ok"), ("conf", "warn")])
        self.assertEqual(r["next_step"], f"ready: Flink 1.19.2 at {self.m.flink} (warnings above)")
        _, out, _, _ = self.run_check()
        self.assertIn(f"flink-dist  OK       Flink 1.19.2 at {self.m.flink} (via FLINK_HOME; "
                      "version from lib/flink-dist-1.19.2.jar); REST http://localhost:8081", out)
        self.assertIn("  WARN  conf: neither conf/config.yaml nor conf/flink-conf.yaml exists", out)
        self.assertIn(f"READY: Flink 1.19.2 at {self.m.flink}", out)
        self.assertEqual(err, "")

    def test_patch_level_is_free(self):
        self.m.flink_home(version="1.19.3", conf="config.yaml")
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["success"], r["flink"]["version"], r["flink"]["on_1_19_line"]), (True, "1.19.3", True))

    def test_conf_and_rest_url(self):
        self.m.flink_home(conf="config.yaml", rest=("127.0.0.1", "6123"))
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (0, True, "ok"))
        self.assertEqual(r["flink"]["rest_url"], "http://127.0.0.1:6123")
        self.assertEqual(r["flink"]["conf"], {"rest.address": "127.0.0.1", "rest.port": "6123"})
        self.assertNotIn("conf", [i["name"] for i in r["items"]])
        self.assertTrue(r["next_step"].startswith(f"ready: Flink 1.19.2 at {self.m.flink}"))
        self.assertNotIn("warnings", r["next_step"])
        # flat flink-conf.yaml is read the same way
        self.m.flink_home(conf="flink-conf.yaml", rest=("flink.local", "8081"))
        _, _, _, r = self.run_check("--json")
        self.assertEqual(r["flink"]["rest_url"], "http://flink.local:8081")
        # FLINK_REST_URL wins over the conf and gets its scheme normalized
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_REST_URL="10.0.0.1:8159/"))
        self.assertEqual(r["flink"]["rest_url"], "http://10.0.0.1:8159")
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_REST_URL="https://flink.example:8443"))
        self.assertEqual(r["flink"]["rest_url"], "https://flink.example:8443")

    def test_installation_lookup_order(self):
        # FLINK_HOME wins
        self.m.flink_home()
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["flink"]["found_via"], r["flink"]["path"]), ("FLINK_HOME", str(self.m.opt)))
        # then a flink on PATH, resolved through its symlink
        self.m.flink_home(home=self.m.local, version="1.19.1")
        (self.m.bin / "flink").symlink_to(self.m.local / "bin" / "flink")
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((r["flink"]["found_via"], r["flink"]["path"], r["flink"]["version"]),
                         ("PATH", str(self.m.local), "1.19.1"))
        # then the default locations: /opt/flink before /usr/local/flink
        (self.m.bin / "flink").unlink()
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((r["flink"]["found_via"], r["flink"]["path"]), ("default", f"{self.m.sys}/opt/flink"))
        for name in SCRIPTS:
            (self.m.opt / "bin" / name).unlink()
        _, _, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((r["flink"]["found_via"], r["flink"]["path"]), ("default", f"{self.m.sys}/usr/local/flink"))

    def test_not_found(self):
        code, out, _, r = self.run_check("--json", env=self.m.env(FLINK_HOME=None))
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        home = self.item(r, "home")
        self.assertEqual(home["status"], "fail")
        self.assertIn("Flink not found", home["detail"])
        self.assertEqual(home["fix"], "install the official binary distribution "
                         "flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it")
        self.assertEqual(r["flink"], {"path": None, "found_via": None, "version": None, "on_1_19_line": None,
                                      "version_source": None, "dist_jars": [], "rest_url": "http://localhost:8081",
                                      "conf": {}})
        self.assertEqual(r["next_step"], home["fix"] + ", then rerun")
        self.assertEqual(r["error_message"], "1 item(s) missing: home")
        self.assertIn("NOT READY: 1 item(s) missing", self.run_check(env=self.m.env(FLINK_HOME=None))[1])

    def test_missing_stock_scripts(self):
        self.m.flink_home(conf="config.yaml")
        (self.m.flink / "bin" / "sql-client.sh").unlink()
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        files = self.item(r, "files")
        self.assertEqual(files["status"], "fail")
        self.assertEqual(files["detail"], "incomplete installation, missing bin/sql-client.sh")
        self.assertEqual(r["next_step"], "unpack the Flink distribution again (or point FLINK_HOME at a "
                         "complete one), then rerun")
        self.assertEqual(self.item(r, "home")["status"], "ok")

    def test_missing_dist_jar(self):
        self.m.flink_home(conf="config.yaml")
        (self.m.flink / "lib" / "flink-dist-1.19.2.jar").unlink()
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertIn("lib/flink-dist-*.jar", self.item(r, "files")["detail"])
        self.assertEqual(self.item(r, "version")["status"], "fail")
        self.assertEqual(r["flink"]["dist_jars"], [])
        # files come first in next_step
        self.assertTrue(r["next_step"].startswith("unpack the Flink distribution again"))
        self.assertEqual(r["error_message"], "2 item(s) missing: files, version")

    def test_several_dist_jars(self):
        self.m.flink_home(conf="config.yaml", dist="flink-dist-1.18.3.jar", manifest_version="1.18.3")
        self.m.jar(self.m.flink / "lib" / "flink-dist-1.19.2.jar", "1.19.2")
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        self.assertEqual(r["flink"]["dist_jars"], ["flink-dist-1.18.3.jar", "flink-dist-1.19.2.jar"])
        self.assertEqual(r["flink"]["version"], "1.18.3")  # read from the first in name order
        item = self.item(r, "dist-jars")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["detail"], "several flink-dist jars in lib: flink-dist-1.18.3.jar, "
                         "flink-dist-1.19.2.jar — Flink's scripts refuse to start with more than one "
                         "(the version above is read from the first in name order)")
        self.assertIn("a restart needs the user consent", item["fix"])
        self.assertEqual(r["next_step"], "keep only the flink-dist jar of the installed version in lib, "
                         "then restart the cluster (a restart needs the user consent), then rerun")
        self.assertIn("        fix: keep only the flink-dist jar", self.run_check()[1])

    def test_version_from_manifest_when_the_jar_name_has_none(self):
        self.m.flink_home(conf="config.yaml", dist="flink-dist-custom.jar", manifest_version="1.19.1")
        _, _, _, r = self.run_check("--json")
        self.assertEqual((r["success"], r["flink"]["version"], r["flink"]["version_source"]),
                         (True, "1.19.1", "lib/flink-dist-custom.jar MANIFEST.MF"))
        self.assertIn("version from lib/flink-dist-custom.jar MANIFEST.MF", self.item(r, "home")["detail"])

    def test_version_undeterminable(self):
        self.m.flink_home(conf="config.yaml", dist="flink-dist-custom.jar", manifest_version=None)
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        version = self.item(r, "version")
        self.assertEqual(version["status"], "fail")
        self.assertIn("cannot determine the Flink version", version["detail"])
        self.assertIsNone(r["flink"]["on_1_19_line"])
        self.assertEqual(r["next_step"], "reinstall the Flink 1.19.x runtime (its flink-dist jar carries "
                         "no readable version), then rerun")

    def test_not_on_the_1_19_line(self):
        self.m.flink_home(conf="config.yaml", version="1.18.3")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        version = self.item(r, "version")
        self.assertEqual(version["status"], "fail")
        self.assertEqual(version["detail"], "Flink 1.18.3 is not on the 1.19 line — the stack is built "
                         "and verified against 1.19.x only")
        self.assertIsNone(r["flink"]["on_1_19_line"])
        self.assertEqual(r["next_step"], "install the Flink 1.19.x runtime instead (this installation "
                         "is 1.18.3), then rerun")

    def test_json_contract_shape_and_crash_shape(self):
        self.m.flink_home(conf="config.yaml")
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "status", "flink", "items", "missing",
                                  "next_step", "error_message"})
        self.assertEqual(set(r["flink"]), {"path", "found_via", "version", "on_1_19_line", "version_source",
                                           "dist_jars", "rest_url", "conf"})
        module = load_module()

        def boom():
            raise RuntimeError("probe exploded")

        module.find_flink_home = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        a = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, a["success"], a["status"], a["error_message"][:15]), (2, False, "fail", "check aborted: "))
        self.assertEqual(set(a), set(r))
        self.assertEqual((a["items"], a["missing"], a["flink"]["path"]), ([], [], None))
        self.assertTrue(a["next_step"].startswith("fix the check error first"))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            module.main([])
        self.assertTrue(buffer.getvalue().startswith("CHECK ERROR: check aborted:"))

    def test_out_file_matches_stdout_and_is_confined(self):
        self.m.flink_home(conf="config.yaml")
        archive = self.tmp / "project" / "flink-dist-check.json"
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

    def test_invalid_argument_is_2_without_json(self):
        code, out, err, r = self.run_check("--bogus")
        self.assertEqual((code, r), (2, None))
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())

    def test_check_leaves_machine_untouched(self):
        self.m.flink_home(conf="config.yaml", version="1.18.0")
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "flink-dist-contract",
        "skill": "flink-velox-flink-dist-check",
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
