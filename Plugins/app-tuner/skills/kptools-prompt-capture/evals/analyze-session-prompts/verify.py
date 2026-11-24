"""Check explicit session selection and that default reports omit request text."""
import csv
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent

def main():
    with TemporaryDirectory() as work:
        out = Path(work) / "session"
        subprocess.run([sys.executable, str(HERE.parents[1] / "scripts/session_view.py"),
                        str(HERE / "fixtures/seg-sessions.jsonl"), "--out", str(out),
                        "--session", "fp:fixture"], check=True, timeout=20)
        with (out / "session_requests.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        observed = [(row["session"], row["id"], row["level"]) for row in rows]
        expected = [("fp:fixture", f"turn-{i}", level)
                    for i, level in enumerate(("text", "meta", "text"), 1)]
        if observed != expected:
            raise SystemExit(f"wrong session or coverage: {observed}")
        for artifact in out.iterdir():
            if "SESSION_PRIVATE_SENTINEL" in artifact.read_text(encoding="utf-8"):
                raise SystemExit(f"request text leaked into {artifact.name}")


if __name__ == "__main__":
    main()
