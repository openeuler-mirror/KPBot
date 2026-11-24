"""Verify that the summary case has enough source evidence after capture data is removed."""
import csv
import json
from pathlib import Path


def main():
    fixture = Path(__file__).resolve().parent / "fixtures"
    run = json.loads((fixture / "run.json").read_text(encoding="utf-8"))
    with (fixture / "summary.csv").open(encoding="utf-8", newline="") as stream:
        counts = {(row["section"], row["metric"]): int(row["value"])
                  for row in csv.DictReader(stream)}
    meta = counts["capture", "meta_level"]
    sessions = counts["session", "sessions"]
    if not 0 < sessions <= meta == counts["capture", "requests"]:
        raise SystemExit("inconsistent metadata request or session counts")
    if (run["capture_start_unix"] >= run["capture_end_unix"] or
            run["capture_closed"] is not True or run["restore"] != "not_authorized" or
            run["runtime_version"] != "modified" or run["log_correlation"] != "not_run" or
            (fixture / run["data_dir"]).exists()):
        raise SystemExit("run.json does not describe the expected retained-summary scenario")
    print(f"Metadata requests: {meta}; sessions: {sessions}")


if __name__ == "__main__":
    main()
