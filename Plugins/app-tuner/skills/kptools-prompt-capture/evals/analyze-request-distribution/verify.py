"""Exercise the documented time filter and inspect the request table."""
import csv
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent

def main():
    with TemporaryDirectory() as work:
        out = Path(work) / "analysis"
        subprocess.run([sys.executable, str(HERE.parents[1] / "scripts/analyze.py"),
                        str(HERE / "fixtures/seg-window.jsonl"), "--out", str(out),
                        "--since", "1700000000", "--until", "1700000002"],
                       check=True, timeout=20)
        with (out / "requests.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        observed = [(row["id"], row["chars_total"], row["prompt_tokens"]) for row in rows]
        if observed != [("target-short", "5", ""), ("target-long", "25", "")]:
            raise SystemExit(f"wrong time range, lengths or missing token counts: {observed}")
        if not (out / "summary.csv").is_file():
            raise SystemExit("analysis did not produce a summary")


if __name__ == "__main__":
    main()
