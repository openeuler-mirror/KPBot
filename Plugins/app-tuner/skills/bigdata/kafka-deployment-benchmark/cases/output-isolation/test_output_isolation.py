"""Verify ignored artifacts in source and installed layouts."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[2]


class OutputIsolationTests(unittest.TestCase):
    def test_generated_files_are_ignored_in_both_layouts(self):
        for layout in (
            "Plugins/app-tuner/skills/bigdata/example",
            ".claude/skills/example",
        ):
            with (
                self.subTest(layout=layout),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = Path(tmp)
                subprocess.run(
                    ["git", "init", "--quiet", str(repo)], check=True
                )
                skill = repo / layout
                skill.mkdir(parents=True)
                shutil.copyfile(SKILL / ".gitignore", skill / ".gitignore")
                generated = (
                    "results/smoke/result.json",
                    "experiment/tpch-tools/conf/doris-cluster.conf",
                    "cases/client-check/results/log",
                    "doris-source/tools/tpch-tools/conf/doris-cluster.conf",
                    "clickhouse-source/generated",
                    "scripts/__pycache__/cached.pyc",
                )
                for name in (
                    *generated,
                    "SKILL.md",
                    "scripts/check_service.py",
                ):
                    path = skill / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("test", encoding="utf-8")
                for name in generated:
                    result = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo),
                            "check-ignore",
                            "--quiet",
                            f"{layout}/{name}",
                        ],
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, name)
                for name in ("SKILL.md", "scripts/check_service.py"):
                    result = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo),
                            "check-ignore",
                            "--quiet",
                            f"{layout}/{name}",
                        ],
                        check=False,
                    )
                    self.assertEqual(result.returncode, 1, name)


if __name__ == "__main__":
    unittest.main()
