"""Check one-sided joins and keep log lines without IDs unattributed."""
import csv
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent

def main():
    with TemporaryDirectory() as work:
        for side, missing in (("p", "d"), ("d", "p")):
            out = Path(work) / side
            subprocess.run([sys.executable, str(HERE.parents[1] / "scripts/join_vllm_log.py"),
                            "--requests", str(HERE / "fixtures/requests.csv"),
                            f"--{side}-log", str(HERE / "fixtures/p.ttft"), "--out", str(out)],
                           check=True, timeout=20)
            observed_field, missing_field = f"{side}_tokenize_ms", f"{missing}_tokenize_ms"
            with (out / "requests_joined.csv").open(newline="") as stream:
                observed = [(row["request_id"],
                             Decimal(row[observed_field]) if row[observed_field] else None,
                             row[missing_field]) for row in csv.DictReader(stream)]
            if observed != [("matching", Decimal("2"), ""), ("unmatched", None, "")]:
                raise SystemExit(f"wrong {side}-only association: {observed}")
            with (out / "tokenizer_lines.csv").open(newline="") as stream:
                lines = [(row["side"], row["rid"]) for row in csv.DictReader(stream)]
            if lines != [(side.upper(), "matching"), (side.upper(), "")]:
                raise SystemExit(f"expected a separate, unattributed {side}-side log line: {lines}")


if __name__ == "__main__":
    main()
