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
        self.temp = tempfile.TemporaryDirectory(prefix="faiss-build-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        (self.repo / "faiss").mkdir(parents=True)
        (self.repo / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
        (self.repo / "faiss/CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.script("python3", "#!/usr/bin/env bash\nset -o pipefail\n" + shlex.quote(Path(sys.executable).as_posix()) + ' "$@" | sed \'s/\\r$//\'\n')
        self.script("git", "#!/usr/bin/env bash\ncase \"$*\" in *rev-parse*) printf '%040d\\n' 1;; esac\n")
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

    def run_build(self, extra=(), force=None, suffix="default"):
        build = self.repo / ("build-" + suffix)
        evidence = self.root / ("evidence-" + suffix)
        arguments = [shell_path(HELPER), "--repo", shell_path(self.repo), "--algorithm", "ivfpq",
                     "--mode", "baseline", "--run-dir", shell_path(evidence),
                     "--build-dir", shell_path(build), "--jobs", "1", *extra]
        command = "export PATH=" + shlex.quote(shell_path(self.bin)) + ':"$PATH"\nbash ' + shlex.join(arguments)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        if force:
            env["FAISS_TEST_FORCE_BUILD_TYPE"] = force
        result = subprocess.run([str(BASH), "-c", command], env=env, capture_output=True,
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


if __name__ == "__main__":
    unittest.main()
