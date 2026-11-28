"""Render a standalone offline schedule from normalized workbook rows."""

import json
from pathlib import Path


def render_html(tasks, milestones, title, as_of=None):
    template = Path(__file__).resolve().parents[1] / "assets" / "chart.html"
    payload = {"tasks": tasks, "milestones": milestones, "title": title, "asOf": as_of}
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # This element is raw text to the HTML parser, even with type=application/json.
    data = data.replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")
    return template.read_text(encoding="utf-8").replace("<!--WORKBOOK_JSON-->", data, 1)
