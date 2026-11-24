"""Check the installed static quality review Skill without running an agent."""

import json
import re
from pathlib import Path


CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]
RULES_PATH = "references/static-quality-review-rules.json"
EXPECTED_COUNTS = {"C": 3, "M": 7, "S": 8, "E": 4}
EXPECTED_IDS = {
    f"{prefix}{number:02d}"
    for prefix, count in EXPECTED_COUNTS.items()
    for number in range(1, count + 1)
}


def check_entrypoint(failures):
    try:
        entry = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        failures.append("Cannot read SKILL.md as UTF-8")
        return

    frontmatter = re.match(r"^---\n(.*?)\n---(?:\n|$)", entry, re.DOTALL)
    name = re.search(
        r"""^name:\s*(['"]?)([a-z0-9-]+)\1[ \t]*$""",
        frontmatter[1] if frontmatter else "",
        re.MULTILINE,
    )
    if not name or name[2] != SKILL_DIR.name:
        failures.append("SKILL.md frontmatter name must match the Skill directory")
    rule_link = r"\]\((?:\./)?" + re.escape(RULES_PATH) + r"\)"
    if not re.search(rule_link, entry):
        failures.append(f"SKILL.md must link to {RULES_PATH}")


def check_rules(failures):
    try:
        rules = json.loads((SKILL_DIR / RULES_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        failures.append(f"Cannot read {RULES_PATH} as UTF-8 JSON")
        return 0
    if not isinstance(rules, list):
        failures.append("The rule file must contain a list")
        return 0

    rule_ids = []
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict) or any(
            not isinstance(rule.get(key), str) or not rule[key].strip()
            for key in ("id", "item", "criteria")
        ):
            failures.append(f"Rule {index} must contain a nonempty id, item and criteria")
            continue
        rule_ids.append(rule["id"])
    if len(set(rule_ids)) != len(rule_ids):
        failures.append("Duplicate rule IDs")
    missing = EXPECTED_IDS.difference(rule_ids)
    unexpected = set(rule_ids).difference(EXPECTED_IDS)
    if missing:
        failures.append("Missing rule IDs: " + ", ".join(sorted(missing)))
    if unexpected:
        failures.append("Unexpected rule IDs: " + ", ".join(sorted(unexpected)))
    return len(rules)


def main():
    failures = []
    check_entrypoint(failures)
    rule_count = check_rules(failures)
    result = {"success": not failures, "rule_count": rule_count, "failures": failures}
    output = CASE_DIR / "results" / "result.json"
    output.parent.mkdir(exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
