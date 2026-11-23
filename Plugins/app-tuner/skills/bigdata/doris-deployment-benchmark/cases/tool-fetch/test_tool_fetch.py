"""Offline integrity and failure-path tests for public tool acquisition."""

import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/fetch_tpch_tools.py"
spec = importlib.util.spec_from_file_location("tool_fetch", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ToolFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "tools"
        self.data = b"echo public-tool\n"
        self.sha = hashlib.sha1(
            b"blob " + str(len(self.data)).encode() + b"\0" + self.data
        ).hexdigest()
        self.item = {
            "type": "file",
            "path": "tools/tpch-tools/bin/run.sh",
            "sha": self.sha,
        }
        self.commit = "a" * 40
        self.urls = []

    def metadata(self, url):
        self.urls.append(url)
        return {"sha": self.commit} if "/commits/" in url else [self.item]

    def fetch(self):
        with (
            patch.object(module, "read_json", side_effect=self.metadata),
            patch.object(module, "read_bytes", return_value=self.data),
        ):
            return module.fetch("test-tag", self.output)

    def test_pinned_source_and_matching_blob_are_saved(self):
        result = self.fetch()
        self.assertEqual((self.output / "bin/run.sh").read_bytes(), self.data)
        self.assertEqual(result["commit"], self.commit)
        self.assertTrue(
            all(
                "ref=" + self.commit in url
                for url in self.urls
                if "/contents/" in url
            )
        )
        self.assertTrue((self.output / "source.json").is_file())

    def test_corrupt_download_never_publishes_directory(self):
        self.item["sha"] = "0" * 40
        with self.assertRaises(ValueError):
            self.fetch()
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            self.fetch()

    def test_unexpected_source_path_is_rejected(self):
        self.item["path"] = "tools/tpch-tools/../../outside"
        with self.assertRaises(ValueError):
            self.fetch()
        self.assertFalse(self.output.exists())

    def test_network_error_does_not_publish_partial_tools(self):
        with (
            patch.object(module, "read_json", side_effect=self.metadata),
            patch.object(
                module, "read_bytes", side_effect=OSError("network error")
            ),
        ):
            with self.assertRaises(OSError):
                module.fetch("test-tag", self.output)
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_empty_source_is_rejected(self):
        with patch.object(
            module, "read_json", side_effect=[{"sha": self.commit}, []]
        ):
            with self.assertRaises(ValueError):
                module.fetch("test-tag", self.output)


if __name__ == "__main__":
    unittest.main()
