#!/usr/bin/env python3
"""Create the summary/ skeleton without touching user-maintained content.

run:
    python3 init_summary.py [--skill-root /path/to/multimedia-code-optimizer]

`summary/manual/` is created only when missing.  Once it exists, this script
never writes into it.  Existing summary.md files and README.md are left alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_summary import ensure_skeleton, rebuild_readme  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root",
                        default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args()
    skill_root = Path(args.skill_root).resolve()
    if not (skill_root / "SKILL.md").exists():
        print(json.dumps({"success": False,
                          "error_message": f"not a skill directory: {skill_root}"},
                         ensure_ascii=False))
        return 1
    summary = skill_root / "summary"
    manual_existed = (summary / "manual").exists()
    readme = summary / "README.md"
    created_readme = not readme.exists()
    summary = ensure_skeleton(skill_root)
    if created_readme:
        rebuild_readme(summary)
    print(json.dumps({
        "success": True,
        "summary": str(summary),
        "created_manual": not manual_existed,
        "created_readme": created_readme,
        "note": "manual/ is user-owned; existing files were not touched",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
