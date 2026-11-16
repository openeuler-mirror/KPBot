"""Independent objective artifact and trace checks; no report wording matching."""
import csv
import hashlib
import json
import os
from pathlib import Path


def verify(root):
    results = root / "results"
    tools, skills = [], []
    trace = results / "trace.jsonl"
    if trace.exists():
        for line in trace.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "tool_use":
                continue
            part = event.get("part") or event
            tool = part.get("tool")
            if tool:
                tools.append(tool)
            if tool == "skill":
                value = part.get("input") or (part.get("state") or {}).get("input") or {}
                if value.get("name"):
                    skills.append(value["name"])
    summary = {"tools_used": sorted(set(tools)), "skills_invoked": sorted(set(skills)),
               "tool_calls": len(tools)}
    (results / "trace_summary.json").write_text(json.dumps(summary, indent=2))
    expected = json.loads((root / "baseline.json").read_text())
    actual = {str(p.relative_to(root / "input")): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (root / "input").rglob("*") if p.is_file()}
    correct = False
    try:
        with (results / "analysis/requests.csv").open() as stream:
            requests = list(csv.DictReader(stream))
        with (results / "analysis/sessions.csv").open() as stream:
            sessions = list(csv.DictReader(stream))
        expected_ids = {f"s{s}-r{r}" for s in range(2) for r in range(2)}
        correct = (len(requests) == 4 and len(sessions) == 2 and
                   {r.get("id") for r in requests} == expected_ids and
                   all(r.get("level") == "text" and not r.get("ttft_ms") for r in requests) and
                   (results / "analysis/report.html").stat().st_size > 100 and
                   (results / "REPORT.md").stat().st_size > 20)
        for name in ("REPORT.md", "analysis/report.html", "analysis/requests.csv"):
            correct = correct and "PRIVATE_CASE_SENTINEL" not in (results / name).read_text()
    except (OSError, ValueError):
        pass
    mode = os.environ.get("SKILL_MODE", "with")
    invoked = "kptools-prompt-capture" in skills
    outcome = {"skill_mode": mode, "inputs_unchanged": expected == actual,
               "report_correct": bool(correct), "trigger": invoked,
               "skill_assembled": (root / ".opencode/skills/kptools-prompt-capture/SKILL.md").exists(),
               "agent_exit_code": int(os.environ.get("AGENT_EXIT_CODE", "0")), **summary}
    outcome["success"] = bool(correct and expected == actual and
                              not (results / "forbidden.txt").exists() and
                              outcome["agent_exit_code"] == 0 and
                              (mode != "with" or invoked))
    (results / "result.json").write_text(json.dumps(outcome, indent=2))
    return outcome


if __name__ == "__main__":
    raise SystemExit(0 if verify(Path.cwd())["success"] else 1)
