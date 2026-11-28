"""Exercise the installed generator with the example template and an invalid schedule."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook


SKILL = Path(__file__).resolve().parents[2]


class WorkbookRenderTest(unittest.TestCase):
    def test_render_and_preserve_on_invalid_dates(self):
        with TemporaryDirectory() as work:
            output = Path(work) / "schedule.html"
            template = SKILL / "assets" / "事项和时间窗.xlsx"

            def generate(source, target):
                return subprocess.run(
                    [sys.executable, "-B", str(SKILL / "scripts" / "generate.py"), str(source),
                     "--output", str(target), "--as-of", "2026-11-28"],
                    cwd=work, capture_output=True, encoding="utf-8",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )

            success = generate(template, output)
            self.assertEqual(success.returncode, 0, success.stderr)
            report = json.loads(success.stdout)
            self.assertEqual(Path(report["output"]), output.resolve())
            self.assertEqual((report["tasks"], report["milestones"]), (6, 3))
            self.assertEqual(report["as_of"], "2026-11-28")

            original = output.read_bytes()
            data = re.search(r'<script id="schedule-data" type="application/json">(.*?)</script>',
                             original.decode("utf-8"), re.DOTALL)
            self.assertIsNotNone(data, "Generated HTML lacks the schedule data")
            schedule = json.loads(data.group(1))
            self.assertEqual(len(schedule["tasks"]), 6)
            self.assertEqual(len(schedule["milestones"]), 3)
            self.assertEqual(schedule["asOf"], report["as_of"])

            invalid = Path(work) / "invalid.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "事项"
            sheet.append(["事项名称", "开始日期", "截止日期"])
            sheet.append(["示例", "2026-11-29", "2026-11-28"])
            workbook.save(invalid)
            workbook.close()

            failure = generate(invalid, output)
            self.assertEqual(failure.returncode, 2, failure.stderr)
            self.assertIn("截止日期", failure.stderr)
            self.assertEqual(output.read_bytes(), original)

            blocked = output / "nested.html"
            failure = generate(template, blocked)
            self.assertEqual(failure.returncode, 2, failure.stderr)
            self.assertIn("生成失败", failure.stderr)
            self.assertEqual(failure.stdout, "")
            self.assertFalse(blocked.exists())
            self.assertEqual(output.read_bytes(), original)

    def test_documented_states(self):
        from runpy import run_path
        states = run_path(str(SKILL / "scripts" / "read_workbook.py"))["STATES"]
        docs = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        listed = re.search(r"状态可选([^。]+)。", docs)
        self.assertIsNotNone(listed)
        self.assertEqual(set(listed.group(1).split("、")), states)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    success = result.wasSuccessful()
    results = Path("results")
    results.mkdir(exist_ok=True)
    (results / "result.json").write_text(json.dumps({"success": success}) + "\n", encoding="utf-8")
    sys.exit(0 if success else 1)
