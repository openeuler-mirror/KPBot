#!/usr/bin/env python3
"""cpp-deps-contract — tier-A case for flink-velox-cpp-deps-check.

Drives scripts/cpp_deps_check.py against a fake machine laid out in a scratch
directory — a sysroot (os-release, headers incl. a <triplet>/ directory,
/usr/local artifacts, a fake PATH with nothing on it) and three source
checkouts — with nothing from the real machine in play; the script's seam
ENV_CHECK_SYSROOT carries the fake. Pins the verdict contract (exit codes,
success, status, missing, next_step order), every probe rule of both levels
(header directories, the 13 packages and their openEuler names, setup-script
choice by OS, install_velox_deps parsing boundaries, the four /usr/local
artifact kinds, the protoc override), the velox-checkout resolution order,
skipped-vs-fail semantics, the JSON shape, --out, "check aborted:" and that a
check leaves the machine untouched.

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
SCRIPT = ROOT / "scripts" / "cpp_deps_check.py"
RESULTS = CASE_DIR / "results"
SCRATCH = RESULTS / "scratch"

KEY = "flink_velox_cpp_deps_check_result"

# header -> openEuler package, pinned independently of the script
HEADERS = {
    "event2/event.h": "libevent-devel", "openssl/ssl.h": "openssl-devel",
    "re2/re2.h": "re2-devel", "zstd.h": "zstd-devel", "lz4.h": "lz4-devel",
    "double-conversion/double-conversion.h": "double-conversion-devel",
    "libdwarf.h": "libdwarf-devel", "libelf.h": "elfutils-libelf-devel",
    "curl/curl.h": "libcurl-devel", "unicode/uversion.h": "libicu-devel",
    "sodium.h": "libsodium-devel", "zlib.h": "zlib-devel",
    "librdkafka/rdkafka.h": "librdkafka-devel",
}
ORDER = ("libevent", "openssl", "re2", "zstd", "lz4", "double-conversion", "libdwarf",
         "libelf", "curl", "icu", "sodium", "zlib", "librdkafka")

SETUP_SCRIPT = textwrap.dedent('''\
    #!/bin/bash
    function install_velox_deps_from_dnf() { :; }
    function install_velox_deps {
      run_and_time install_velox_deps_from_dnf
      run_and_time install_conda
      run_and_time install_gflags
      run_and_time install_glog
      run_and_time install_protobuf
      run_and_time install_fmt
    }
    function install_fmt() { cmake_install; }
    # outside install_velox_deps: not on the list
    function install_conda() {
      run_and_time install_conda_helper
    }
    ''')
PARSED = ["gflags", "glog", "protobuf", "fmt"]


def load_module():
    """cpp_deps_check.py as an in-process module (for the crash path a
    subprocess cannot be driven into hermetically)."""
    spec = importlib.util.spec_from_file_location("cpp_deps_check_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMachine:
    """A whole machine in a directory: sysroot and source checkouts."""

    def __init__(self, root):
        self.root = Path(root)
        self.sys = self.root / "sysroot"
        self.bin = self.root / "bin"
        self.repos = self.root / "repos"
        self.bin.mkdir(parents=True)

    def write(self, path, text, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def script(self, path, body):
        return self.write(path, "#!/bin/sh\n" + body, 0o755)

    def os_release(self, os_id="openeuler", version="24.03"):
        self.write(self.sys / "etc" / "os-release", f'ID={os_id}\nVERSION_ID="{version}"\n')

    def headers(self, without=()):
        for header in HEADERS:
            if header not in without:
                self.write(self.sys / "usr" / "include" / header, "/* header */\n")

    def source_libs(self, without=()):
        # one library per kind of /usr/local trace the probe accepts
        kinds = {
            "gflags": ("dir", self.sys / "usr/local/lib/cmake/gflags"),
            "glog": ("dir", self.sys / "usr/local/include/glog"),
            "fmt": ("dir", self.sys / "usr/local/lib64/cmake/Fmt"),
            "protobuf": ("bin", self.sys / "usr/local/bin/protoc"),
        }
        for lib, (kind, path) in kinds.items():
            if lib in without:
                continue
            if kind == "dir":
                path.mkdir(parents=True, exist_ok=True)
            else:
                self.script(path, 'echo "libprotoc 25.3"\n')

    def checkout(self, script="setup-centos9.sh", body=SETUP_SCRIPT):
        self.write(self.repos / "velox" / "CMakeLists.txt", "cmake_minimum_required(VERSION 3.16)\n")
        if script:
            self.write(self.repos / "velox" / "scripts" / script, body)

    def env(self, **overrides):
        env = {"PATH": str(self.bin), "ENV_CHECK_SYSROOT": str(self.sys), "HOME": str(self.root),
               "LC_ALL": "C", "GFV_REPOS_DIR": str(self.repos)}
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


class CppDepsCase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.m = FakeMachine(self.tmp / "machine")

    def ready(self):
        """A machine with everything: headers, /usr/local artifacts, checkout."""
        self.m.os_release()
        self.m.headers()
        self.m.source_libs()
        self.m.checkout()

    def run_check(self, *args, env=None, cwd=None):
        """Run cpp_deps_check.py; returns (exit code, stdout, stderr, result or None)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True,
            errors="replace", env=self.m.env() if env is None else env,
            cwd=str(self.tmp if cwd is None else cwd),
            stdin=subprocess.DEVNULL, timeout=60)
        result = json.loads(proc.stdout)[KEY] if proc.stdout.lstrip().startswith("{") else None
        return proc.returncode, proc.stdout, proc.stderr, result

    def item(self, result, name):
        return next(i for i in result["items"] if i["name"] == name)

    def test_all_present(self):
        self.ready()
        code, out, err, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"], r["missing"], r["error_message"]),
                         (0, True, "ok", [], ""))
        self.assertEqual((r["system"]["expected"], r["system"]["present"], r["system"]["missing"]),
                         (13, 13, []))
        self.assertEqual(r["source"]["expected"], PARSED)
        self.assertEqual((r["source"]["present"], r["source"]["missing"]), (4, []))
        self.assertEqual(r["source"]["list_source"],
                         str(self.m.repos / "velox" / "scripts" / "setup-centos9.sh"))
        self.assertEqual(r["velox_dir"], os.path.realpath(str(self.m.repos / "velox")))
        self.assertEqual([i["name"] for i in r["items"]], list(ORDER) + ["source-deps"])
        self.assertEqual(self.item(r, "source-deps")["status"], "ok")
        self.assertEqual(self.item(r, "source-deps")["detail"],
                         f"4/4 present under /usr/local (list: {r['source']['list_source']}; "
                         "velox checkout via GFV_REPOS_DIR/velox)")
        self.assertEqual(r["next_step"], "ready: every C++ library the stack needs is present")
        _, out, _, _ = self.run_check()
        self.assertIn("cpp-deps    OK       13/13 system headers present; source-level 4/4 present "
                      "under /usr/local (list: ", out)
        self.assertIn("READY: every C++ library the stack needs is present", out)
        self.assertEqual(err, "")

    def test_header_in_triplet_dir_counts(self):
        self.ready()
        (self.m.sys / "usr/include/librdkafka/rdkafka.h").unlink()
        self.m.write(self.m.sys / "usr/include/aarch64-linux-gnu/librdkafka/rdkafka.h", "/* h */\n")
        code, _, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.item(r, "librdkafka")["detail"],
                         f"header found: {self.m.sys}/usr/include/aarch64-linux-gnu/librdkafka/rdkafka.h")

    def test_header_under_usr_local_counts(self):
        self.ready()
        (self.m.sys / "usr/include/sodium.h").unlink()
        self.m.write(self.m.sys / "usr/local/include/sodium.h", "/* h */\n")
        _, _, _, r = self.run_check("--json")
        self.assertEqual(self.item(r, "sodium")["status"], "ok")
        self.assertTrue(self.item(r, "sodium")["detail"].startswith(f"header found: {self.m.sys}/usr/local/include/"))

    def test_missing_system_lib_names_its_package(self):
        self.ready()
        (self.m.sys / "usr/include/openssl/ssl.h").unlink()
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"], r["status"]), (1, False, "fail"))
        self.assertEqual(r["missing"], ["openssl: no header openssl/ssl.h under /usr/include "
                                        "or /usr/local/include"])
        item = self.item(r, "openssl")
        self.assertEqual((item["status"], item["fix"]), ("fail", "system package (openEuler name): openssl-devel"))
        self.assertEqual(r["system"], {"expected": 13, "present": 12, "missing": ["openssl"]})
        self.assertEqual(r["next_step"], "install the missing system packages (dnf install openssl-devel), then rerun")
        self.assertEqual(r["error_message"], "1 item(s) missing: openssl")
        _, out, _, _ = self.run_check()
        self.assertIn("cpp-deps    FAIL     1 system, 0 source-level missing", out)
        self.assertIn("  FAIL  openssl: no header openssl/ssl.h under /usr/include or /usr/local/include\n"
                      "        fix: system package (openEuler name): openssl-devel", out)
        self.assertIn("NOT READY: 1 item(s) missing", out)

    def test_several_system_missing_dnf_in_table_order(self):
        self.ready()
        for header in ("zstd.h", "re2/re2.h", "libelf.h"):
            (self.m.sys / "usr/include" / header).unlink()
        _, _, _, r = self.run_check("--json")
        self.assertEqual(r["system"]["missing"], ["re2", "zstd", "libelf"])
        self.assertEqual(r["next_step"],
                         "install the missing system packages (dnf install re2-devel zstd-devel "
                         "elfutils-libelf-devel), then rerun")
        self.assertEqual(self.item(r, "libelf")["fix"], "system package (openEuler name): elfutils-libelf-devel")

    def test_setup_script_parsing_excludes_package_manager_and_conda(self):
        self.ready()
        _, _, _, r = self.run_check("--json")
        # install_velox_deps_from_dnf and install_conda are excluded; parsing stops at the
        # function's closing brace, so the redefined install_conda (and its helper) after it
        # never make the list
        self.assertEqual(r["source"]["expected"], PARSED)
        module = load_module()
        self.assertEqual(module.velox_source_libs(str(self.m.repos / "velox/scripts/setup-centos9.sh")), PARSED)

    def test_source_lib_missing_names_setup_function(self):
        self.ready()
        (self.m.sys / "usr/local/bin/protoc").unlink()
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (1, False))
        item = self.item(r, "protobuf")
        self.assertEqual((item["status"], item["fix"]),
                         ("fail", "built by Velox's own setup script: install_protobuf()"))
        self.assertEqual(self.item(r, "source-deps")["status"], "info")
        self.assertIn("3/4 present (list: ", self.item(r, "source-deps")["detail"])
        self.assertEqual(r["source"]["missing"], ["protobuf"])
        self.assertIn("protobuf: not found under /usr/local", r["missing"])
        self.assertEqual(r["next_step"], "build the missing libraries with Velox's own setup script "
                         "(install_protobuf()), then rerun")
        _, out, _, _ = self.run_check()
        self.assertIn("cpp-deps    FAIL     0 system, 1 source-level missing", out)
        self.assertIn("        fix: built by Velox's own setup script: install_protobuf()", out)

    def test_system_libraries_before_source_level_in_next_step(self):
        self.ready()
        (self.m.sys / "usr/include/openssl/ssl.h").unlink()
        (self.m.sys / "usr/local/bin/protoc").unlink()
        _, _, _, r = self.run_check("--json")
        self.assertEqual(r["next_step"], "install the missing system packages (dnf install openssl-devel), then rerun")
        self.assertEqual(r["error_message"], "2 item(s) missing: openssl, protobuf")
        self.assertEqual(r["missing"][1], "protobuf: not found under /usr/local")

    def test_setup_script_chosen_by_os(self):
        # Ubuntu picks setup-ubuntu.sh
        self.m.os_release(os_id="ubuntu", version="22.04")
        self.m.headers()
        self.m.source_libs()
        self.m.checkout(script="setup-ubuntu.sh")
        _, _, _, r = self.run_check("--json")
        self.assertEqual(r["os"], {"id": "ubuntu", "version": "22.04"})
        self.assertTrue(r["source"]["list_source"].endswith("/scripts/setup-ubuntu.sh"))
        self.assertEqual(r["source"]["expected"], PARSED)
        # CentOS/RHEL 7: the checkout ships no fitting script -> warn, not fail
        self.m.os_release(os_id="centos", version="7")
        code, out, _, r = self.run_check("--json")
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(self.item(r, "source-deps")["status"], "warn")
        self.assertIn("ships no setup script for CentOS/RHEL 7", self.item(r, "source-deps")["detail"])
        self.assertEqual(r["source"], {"list_source": None, "expected": [], "present": None, "missing": []})
        _, out, _, _ = self.run_check()
        self.assertIn("system headers present; source-level warn", out)
        self.assertIn("  WARN  source-deps: not probed: the velox checkout ships no setup script for "
                      "CentOS/RHEL 7", out)
        self.assertIn("READY: every C++ library the stack needs is present", out)
        # a checkout without the script file -> warn
        self.m.os_release()
        (self.m.repos / "velox/scripts/setup-centos9.sh").unlink(missing_ok=True)
        _, _, _, r = self.run_check("--json")
        self.assertIn("not found", self.item(r, "source-deps")["detail"])
        # a script without install calls -> warn
        self.m.checkout(body="#!/bin/bash\nfunction install_velox_deps {\n  run_and_time install_conda\n}\n")
        _, _, _, r = self.run_check("--json")
        self.assertIn("no install_* calls found", self.item(r, "source-deps")["detail"])

    def test_no_velox_checkout_is_skipped_not_failure(self):
        self.m.os_release()
        self.m.headers()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        env = self.m.env(GFV_REPOS_DIR=None)
        code, _, _, r = self.run_check("--json", env=env, cwd=str(elsewhere))
        self.assertEqual((code, r["success"]), (0, True))
        self.assertEqual(r["status"], "skipped")
        self.assertIsNone(r["velox_dir"])
        self.assertEqual(r["source"], {"list_source": None, "expected": [], "present": None, "missing": []})
        item = self.item(r, "source-deps")
        self.assertEqual(item["status"], "skipped")
        self.assertTrue(item["detail"].startswith("not probed: needs the velox checkout"))
        self.assertEqual(r["next_step"], "ready: every C++ library the stack needs is present")
        _, out, _, _ = self.run_check(env=env, cwd=str(elsewhere))
        self.assertIn("source-level skipped: not probed: needs the velox checkout", out)
        self.assertIn("READY", out)

    def test_velox_dir_resolution_order(self):
        self.m.os_release()
        self.m.headers()
        self.m.checkout()
        # --velox-dir wins over the environment
        _, _, _, r = self.run_check("--json", "--velox-dir", str(self.m.repos / "velox"))
        self.assertIn("velox checkout via --velox-dir", self.item(r, "source-deps")["detail"])
        # VELOX_HOME beats GFV_REPOS_DIR
        _, _, _, r = self.run_check("--json", env=self.m.env(VELOX_HOME=str(self.m.repos / "velox"),
                                                               GFV_REPOS_DIR=str(self.tmp / "nope")))
        self.assertIn("velox checkout via VELOX_HOME", self.item(r, "source-deps")["detail"])
        # GFV_REPOS_DIR/velox by default
        _, _, _, r = self.run_check("--json")
        self.assertIn("velox checkout via GFV_REPOS_DIR/velox", self.item(r, "source-deps")["detail"])
        # walked upward from the working directory when nothing is given (the machine root
        # holds repos/velox one level down)
        _, _, _, r = self.run_check("--json", env=self.m.env(GFV_REPOS_DIR=None), cwd=str(self.m.root))
        self.assertIn("velox checkout via repos/velox walked up from the working directory",
                      self.item(r, "source-deps")["detail"])
        # an explicit path that is not a directory: warn, level 2 skipped, still not a failure
        code, _, _, r = self.run_check("--json", "--velox-dir", str(self.tmp / "nope"))
        self.assertEqual((code, r["success"], r["velox_dir"]), (0, True, None))
        self.assertEqual(self.item(r, "source-deps")["status"], "warn")
        self.assertEqual(self.item(r, "source-deps")["detail"],
                         f"not probed: {self.tmp / 'nope'} is not a directory")

    def test_json_contract_shape(self):
        self.ready()
        (self.m.sys / "usr/include/openssl/ssl.h").unlink()
        _, _, _, r = self.run_check("--json")
        self.assertEqual(set(r), {"success", "checked_at", "status", "os", "system", "source", "velox_dir",
                                  "items", "missing", "next_step", "error_message"})
        self.assertEqual(set(r["system"]), {"expected", "present", "missing"})
        self.assertEqual(set(r["source"]), {"list_source", "expected", "present", "missing"})
        self.assertEqual(set(self.item(r, "libevent")), {"name", "status", "detail"})
        self.assertEqual(set(self.item(r, "openssl")), {"name", "status", "detail", "fix", "package"})
        # a present source-level library has no item of its own; only the summary carries it
        self.assertEqual([i["name"] for i in r["items"] if i["name"] in PARSED], [])
        self.assertRegex(r["checked_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_out_file_matches_stdout(self):
        self.ready()
        out_file = self.tmp / "project" / "cpp-deps-check.json"
        _, stdout, _, _ = self.run_check("--json", "--out", str(out_file))
        self.assertEqual(json.loads(out_file.read_text(encoding="utf-8")), json.loads(stdout))

    def test_invalid_argument_is_2_without_json(self):
        code, out, err, r = self.run_check("--bogus")
        self.assertEqual((code, r), (2, None))
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())

    def test_scan_aborted_is_2(self):
        module = load_module()

        def boom(headers):
            raise RuntimeError("probe exploded")

        module.header_exists = boom
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(["--json"])
        r = json.loads(buffer.getvalue())[KEY]
        self.assertEqual((code, r["success"]), (2, False))
        self.assertTrue(r["error_message"].startswith("check aborted:"))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            module.main([])
        self.assertTrue(buffer.getvalue().startswith("CHECK ERROR: check aborted:"))

    def test_check_leaves_machine_untouched(self):
        self.ready()
        (self.m.sys / "usr/local/bin/protoc").unlink()
        before = self.m.snapshot()
        self.run_check("--json")
        self.assertEqual(self.m.snapshot(), before)


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "case": "cpp-deps-contract",
        "skill": "flink-velox-cpp-deps-check",
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
