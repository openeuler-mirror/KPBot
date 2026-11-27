"""Count snapshot reads and compare binding outcomes across committed writes."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile


def check(condition, message):
    if not condition:
        raise AssertionError(message)


READ = "SELECT status, origin, remote FROM snapshots WHERE id = ?"


def bind(reader, writer, *, fixed_view, candidate, action, pinned, separate_source, source_error):
    if fixed_view:
        reader.execute("BEGIN")
    row = reader.execute(READ, ("s",)).fetchone()
    if row is None or row[0] == "deleted":
        return ("ready-error", "not-found")
    if row[0] != "ready":
        return ("ready-error", "in-progress")

    if action == "update":
        writer.execute("UPDATE snapshots SET origin = 'B', remote = 'ready' WHERE id = 's'")
    elif action == "delete":
        writer.execute("DELETE FROM snapshots WHERE id = 's'")
    elif action == "tombstone":
        writer.execute("UPDATE snapshots SET status = 'deleted' WHERE id = 's'")

    if candidate and fixed_view and not separate_source:
        source = row
    else:
        source = reader.execute(READ, ("s",)).fetchone()
        if source_error in ("store-uninitialized", "nil-source"):
            source = None
        elif source_error is not None:
            return ("source-error", source_error)
    if source is None or source[0] == "deleted":
        return ("origin-required",) if pinned else ("local-binding",)
    return ("placement", source[1], source[2], pinned)


def run(*, fixed_view, candidate, action="none", pinned=False, state="ready",
        separate_source=False, source_error=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fixture.db"
        with closing(sqlite3.connect(path, isolation_level=None)) as writer, \
             closing(sqlite3.connect(path, isolation_level=None)) as reader:
            mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            check(mode == "wal", "fixture needs a reader snapshot and a separate writer")
            writer.execute("CREATE TABLE snapshots(id TEXT PRIMARY KEY, status TEXT, origin TEXT, remote TEXT)")
            if state:
                writer.execute("INSERT INTO snapshots VALUES ('s', ?, 'A', 'pending')", (state,))
            selects = []
            reader.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT ") else None)
            outcome = bind(reader, writer, fixed_view=fixed_view, candidate=candidate, action=action,
                           pinned=pinned, separate_source=separate_source, source_error=source_error)
            return outcome, len(selects)


def main():
    fixed_cases = 0
    live_cases = 0
    for action, pinned, expected_live in (
        ("none", False, ("placement", "A", "pending", False)),
        ("update", False, ("placement", "B", "ready", False)),
        ("delete", False, ("local-binding",)),
        ("tombstone", True, ("origin-required",)),
    ):
        options = {"action": action, "pinned": pinned}
        stable_base, stable_reads = run(fixed_view=True, candidate=False, **options)
        stable_new, fewer_reads = run(fixed_view=True, candidate=True, **options)
        expected_stable = ("placement", "A", "pending", pinned)
        check(stable_base == stable_new == expected_stable, f"{action}: fixed view changed placement")
        check((stable_reads, fewer_reads) == (2, 1), f"{action}: fixed-view SELECT count")
        fixed_cases += 1

        live_base = run(fixed_view=False, candidate=False, **options)
        live_new, live_reads = run(fixed_view=False, candidate=True, **options)
        check(live_base == (live_new, live_reads) == (expected_live, 2), f"{action}: live view failed")
        live_cases += 1

    for state, reason in ((None, "not-found"), ("deleted", "not-found"), ("building", "in-progress")):
        for fixed in (True, False):
            for candidate in (True, False):
                got = run(fixed_view=fixed, candidate=candidate, state=state)
                check(got == (("ready-error", reason), 1), "readiness error reached placement")

    for source_error in ("store-uninitialized", "nil-source", "database-timeout"):
        for pin in (True, False):
            if source_error == "database-timeout":
                expected = ("source-error", source_error)
            else:
                expected = ("origin-required",) if pin else ("local-binding",)
            for fixed in (True, False):
                outcome = run(fixed_view=fixed, candidate=True, pinned=pin,
                              separate_source=True, source_error=source_error)
                check(outcome == (expected, 2), "source fallback or error changed")

    result = {"fixed_view_baseline_selects": stable_reads, "fixed_view_candidate_selects": fewer_reads,
              "live_view_selects": live_reads, "fixed_view_cases": fixed_cases, "live_view_cases": live_cases}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
