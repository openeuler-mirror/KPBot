"""Check that old log lines are retained but not attributed without an ID."""
import csv
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent

with TemporaryDirectory() as work:
    out = Path(work) / "join"
    subprocess.run([sys.executable, str(HERE.parents[1] / "scripts/join_vllm_log.py"),
                    "--requests", str(HERE / "fixtures/requests.csv"),
                    "--p-log", str(HERE / "fixtures/p.ttft"), "--out", str(out)],
                   check=True, timeout=20)
    with (out / "requests_joined.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    observed = [(row["request_id"], Decimal(row["p_tokenize_ms"]) if row["p_tokenize_ms"] else None,
                 row["d_tokenize_ms"]) for row in rows]
    if observed != [("matching", Decimal("2"), ""), ("unmatched", None, "")]:
        raise SystemExit(f"wrong request association: {observed}")
    with (out / "tokenizer_lines.csv").open(newline="") as stream:
        ids = [row["rid"] for row in csv.DictReader(stream)]
    if ids != ["matching", ""]:
        raise SystemExit(f"expected a separate, unattributed legacy log line: {ids}")
