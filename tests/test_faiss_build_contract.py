"""Verify that the Faiss build helper never publishes contradictory metadata."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "Plugins/code-optimizer/skills/faiss-build/scripts/build_faiss.sh"
BASH = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else Path(shutil.which("bash") or "/bin/bash")


def shell_path(path):
    path = Path(path).resolve()
    return "/" + path.drive[0].lower() + path.as_posix()[2:] if os.name == "nt" else str(path)


@unittest.skipUnless(BASH.is_file(), "Bash is required for the build helper")
class FaissBuildContractTests(unittest.TestCase):
    def setUp(self):
        # Keep subprocess outputs within the checkout for restricted runners.
        temp_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="faiss-build-contract-", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        (self.repo / "faiss").mkdir(parents=True)
        (self.repo / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
        (self.repo / "faiss/CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.script("python3", "#!/usr/bin/env bash\nset -o pipefail\n" + shlex.quote(Path(sys.executable).as_posix()) + ' "$@" | sed \'s/\\r$//\'\n')
        self.script("git", '''#!/usr/bin/env bash
case "$*" in
  *rev-parse*)
    if [[ "${FAISS_TEST_GIT_REV_FAIL:-}" == "1" ]]; then
      echo "fixture revision failure" >&2
      exit 128
    fi
    printf '%040d\n' 1;;
  *status*)
    if [[ "${FAISS_TEST_GIT_STATUS_FAIL:-}" == "1" ]]; then
      echo "fixture status failure" >&2
      exit 128
    fi
    printf '%s' "${FAISS_TEST_DIRTY_FILES:-}";;
esac
''')
        self.script("cmake", '''#!/usr/bin/env bash
set -eu
if [[ "$1" == "--build" ]]; then
  mkdir -p "$2/faiss"
  printf '%s\n' "$@" > "$2/build-arguments"
  printf 'fixture library' > "$2/faiss/libfaiss.so"
  touch "$2/build-executed"
  exit 0
fi
build=""; build_type=""; level=""; krl=""
while (($#)); do
  case "$1" in
    -B) build="$2"; shift 2;;
    -DCMAKE_BUILD_TYPE=*) build_type="${1#*=}"; shift;;
    -DFAISS_OPT_LEVEL=*) level="${1#*=}"; shift;;
    -DKRL=*) krl="${1#*=}"; shift;;
    *) shift;;
  esac
done
mkdir -p "$build"
printf 'CMAKE_BUILD_TYPE:STRING=%s\nFAISS_OPT_LEVEL:STRING=%s\n' "${FAISS_TEST_FORCE_BUILD_TYPE:-$build_type}" "$level" > "$build/CMakeCache.txt"
if [[ -n "$krl" ]]; then printf 'KRL:BOOL=%s\n' "$krl" >> "$build/CMakeCache.txt"; fi
printf '[{"command":"c++ fixture.cpp"}]\n' > "$build/compile_commands.json"
''')

    def script(self, name, source):
        path = self.bin / name
        path.write_text(source, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def run_build(self, extra=(), force=None, suffix="default", env=None):
        build = self.repo / ("build-" + suffix)
        evidence = self.root / ("evidence-" + suffix)
        arguments = [shell_path(HELPER), "--repo", shell_path(self.repo), "--algorithm", "ivfpq",
                     "--mode", "baseline", "--run-dir", shell_path(evidence),
                     "--build-dir", shell_path(build), "--jobs", "1", *extra]
        command = "export PATH=" + shlex.quote(shell_path(self.bin)) + ':"$PATH"\nbash ' + shlex.join(arguments)
        child_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        child_env.update(env or {})
        if force:
            child_env["FAISS_TEST_FORCE_BUILD_TYPE"] = force
        result = subprocess.run([str(BASH), "-c", command], env=child_env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30)
        return result, build, evidence

    def test_matching_cache_produces_a_manifest(self):
        result, build, evidence = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((evidence / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["build_type"], manifest["faiss_opt_level"]), ("Release", "generic"))
        self.assertTrue((build / "build-executed").exists())
        arguments = (build / "build-arguments").read_text(encoding="utf-8").splitlines()
        self.assertEqual(arguments[arguments.index("--config") + 1], "Release")

    def test_source_rejection_preserves_existing_build_and_evidence(self):
        cases = (
            ({"FAISS_TEST_DIRTY_FILES": " M faiss/CMakeLists.txt"}, "clean worktree"),
            ({"FAISS_TEST_GIT_STATUS_FAIL": "1"}, "fixture status failure"),
            ({"FAISS_TEST_GIT_REV_FAIL": "1"}, "fixture revision failure"),
        )
        for index, (env, message) in enumerate(cases):
            with self.subTest(env=env):
                suffix = "rejected-" + str(index)
                build = self.repo / ("build-" + suffix)
                evidence = self.root / ("evidence-" + suffix)
                build.mkdir()
                evidence.mkdir()
                marker = build / "preserve.txt"
                manifest = evidence / "BUILD-MANIFEST.json"
                log = evidence / "build.log"
                for path in (marker, manifest, log):
                    path.write_text("previous contents", encoding="utf-8")
                result, _, _ = self.run_build(("--clean",), suffix=suffix, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout + result.stderr)
                for path in (marker, manifest, log):
                    self.assertEqual(path.read_text(encoding="utf-8"), "previous contents")

    def test_status_failure_cannot_publish_current_head_manifest(self):
        result, build, evidence = self.run_build(
            ("--mode", "current-head-profile"), env={"FAISS_TEST_GIT_STATUS_FAIL": "1"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture status failure", result.stdout + result.stderr)
        self.assertFalse((build / "build-executed").exists())
        self.assertFalse(evidence.exists())

    def test_current_head_manifest_records_dirty_sources(self):
        dirty_files = " M faiss/CMakeLists.txt\n?? new.cpp"
        result, _, evidence = self.run_build(
            ("--mode", "current-head-profile"), env={"FAISS_TEST_DIRTY_FILES": dirty_files}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((evidence / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["dirty"])
        self.assertEqual(manifest["dirty_files"], dirty_files.splitlines())

    def test_new_evidence_directory_does_not_dirty_baseline_sources(self):
        real_git = shutil.which("git")
        if not real_git:
            self.skipTest("Git is required for the baseline integration test")
        for arguments in (
            ("init",),
            ("add", "CMakeLists.txt", "faiss/CMakeLists.txt"),
            ("-c", "user.name=Contract Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             "commit", "-m", "fixture"),
        ):
            result = subprocess.run(
                [real_git, "-C", str(self.repo), *arguments], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.script("git", "#!/usr/bin/env bash\nexec " + shlex.quote(shell_path(real_git)) + ' "$@"\n')
        evidence = self.repo / "evidence"
        result, _, _ = self.run_build(("--run-dir", shell_path(evidence)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((evidence / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["dirty"])
        self.assertEqual(manifest["dirty_files"], [])

    def test_conflicting_arguments_cannot_generate_false_metadata(self):
        for index, extra in enumerate((
            ("--cmake-arg", "-DCMAKE_BUILD_TYPE=Debug"),
            ("--cmake-arg", "-DFAISS_OPT_LEVEL=avx2"),
            ("--krl", "on", "--cmake-arg", "-DKRL=OFF"),
        )):
            with self.subTest(extra=extra):
                result, build, evidence = self.run_build(extra, suffix=str(index))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((build / "build-executed").exists())
                self.assertFalse((evidence / "BUILD-MANIFEST.json").exists())

    def test_project_forced_values_are_checked_after_configuration(self):
        result, build, evidence = self.run_build(force="Debug")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((build / "build-executed").exists())
        self.assertFalse((evidence / "BUILD-MANIFEST.json").exists())

    def test_failed_rebuild_invalidates_previous_success_evidence(self):
        result, build, evidence = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        artifacts = ("BUILD-MANIFEST.json", "compile-definitions.txt", "libfaiss.sha256")
        for artifact in artifacts:
            self.assertTrue((evidence / artifact).is_file())
        for log in ("cmake-configure.log", "build.log"):
            (evidence / log).write_text("previous build log", encoding="utf-8")

        result, _, _ = self.run_build(force="Debug")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Effective CMAKE_BUILD_TYPE", result.stdout + result.stderr)
        for artifact in artifacts:
            self.assertFalse((evidence / artifact).exists(), artifact)
        for log in ("cmake-configure.log", "build.log"):
            self.assertNotIn("previous build log", (evidence / log).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
