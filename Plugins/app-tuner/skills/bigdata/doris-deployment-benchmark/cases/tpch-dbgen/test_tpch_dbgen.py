"""Offline tests for verified TPC-H dbgen archive handling."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/build_tpch_dbgen.py"
spec = importlib.util.spec_from_file_location("build_tpch_dbgen", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ROOT = "TPC-H_Tools_v3.0.0"


def make_archive(path, extra=None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{ROOT}/dbgen/makefile.suite", "CC = gcc\n")
        archive.writestr(f"{ROOT}/dbgen/tpcd.h", "\n")
        archive.writestr(f"{ROOT}/dbgen/dss.ddl", "\n")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)


class DbgenVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / "tools"
        (self.tools / "bin").mkdir(parents=True)
        (self.tools / "bin/build-tpch-dbgen.sh").write_text(
            "#!/usr/bin/env bash\n", encoding="utf-8"
        )
        self.archive = self.root / "TPC-H_Tools_v3.0.0new.zip"
        make_archive(self.archive)
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "artifact": self.archive.name,
                    "url": "https://example.invalid/tools.zip",
                    "sha256": self.digest,
                    "size_bytes": self.archive.stat().st_size,
                    "source_apache_commit": "a" * 40,
                    "digest_kind": "project-pinned",
                }
            ),
            encoding="utf-8",
        )
        self.builds = []

    def fake_build(self, argv, cwd, check):
        marker = Path(cwd) / module.MARKER
        self.assertTrue(marker.is_file())
        dbgen = Path(cwd) / ROOT / "dbgen/dbgen"
        dbgen.write_text("built", encoding="utf-8")
        self.builds.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def run_tool(self, **kwargs):
        with patch.object(
            module.subprocess, "run", side_effect=self.fake_build
        ):
            return module.run(
                self.tools,
                self.manifest,
                archive=self.archive,
                **kwargs,
            )

    def test_valid_archive_is_verified_before_extraction_and_build(self):
        result = self.run_tool()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(
            (self.tools / "bin" / ROOT / "dbgen/makefile.suite").is_file()
        )
        self.assertTrue((self.tools / "bin" / ROOT / "dbgen/dbgen").is_file())
        marker = json.loads(
            (self.tools / "bin" / module.MARKER).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["sha256"], self.digest)
        self.assertEqual(marker["source_apache_commit"], "a" * 40)
        self.assertEqual(len(self.builds), 1)

    def test_tampered_archive_is_rejected_before_extraction_and_build(self):
        content = bytearray(self.archive.read_bytes())
        content[-1] ^= 0xFF
        self.archive.write_bytes(content)
        with self.assertRaises(ValueError) as caught:
            self.run_tool()
        self.assertIn("SHA-256", str(caught.exception))
        self.assertFalse((self.tools / "bin" / ROOT).exists())
        self.assertEqual(self.builds, [])

    def test_size_mismatch_is_rejected_before_extraction(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["size_bytes"] += 1
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.run_tool()
        self.assertIn("size mismatch", str(caught.exception))
        self.assertEqual(self.builds, [])

    def test_pre_existing_unverified_tree_blocks_build(self):
        stale = self.tools / "bin" / ROOT / "dbgen"
        stale.mkdir(parents=True)
        (stale / "dbgen").write_text("stale", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.run_tool()
        self.assertIn(
            "without a matching verified marker", str(caught.exception)
        )
        self.assertEqual(
            (stale / "dbgen").read_text(encoding="utf-8"), "stale"
        )
        self.assertEqual(self.builds, [])

    def test_replace_existing_removes_only_stale_tree_after_verification(self):
        stale = self.tools / "bin" / ROOT / "dbgen"
        stale.mkdir(parents=True)
        (stale / "dbgen").write_text("stale", encoding="utf-8")
        keep = self.tools / "bin" / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        result = self.run_tool(replace_existing=True)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(
            (stale / "dbgen").read_text(encoding="utf-8") == "stale"
        )
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep")

    def test_stale_tree_with_reused_marker_is_rejected(self):
        target, _ = module.prepare(
            self.archive,
            module.load_manifest(self.manifest),
            self.tools / "bin",
            False,
        )
        (target / "dbgen/makefile.suite").write_text("stale", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.run_tool()
        self.assertIn(
            "without a matching verified marker", str(caught.exception)
        )
        self.assertEqual(self.builds, [])

    def test_unsafe_archive_root_is_rejected(self):
        make_archive(self.archive, {"outside/payload": "x"})
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["sha256"] = hashlib.sha256(
            self.archive.read_bytes()
        ).hexdigest()
        manifest["size_bytes"] = self.archive.stat().st_size
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.run_tool()
        self.assertIn("unexpected archive root", str(caught.exception))
        self.assertEqual(self.builds, [])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": result.wasSuccessful(),
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if result.wasSuccessful() else 1)
