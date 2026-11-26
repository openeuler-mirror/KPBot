"""Deterministic model of per-origin directory flush tickets. Python 3.8+."""
import json
from pathlib import Path


class Directory:
    def __init__(self):
        self.issued = 0
        self.durable = 0
        self.flushing = None
        self.accepting = True
        self.producers = set()
        self.outcome = {}
        self.flushes = 0

    def admit(self):
        if not self.accepting:
            raise RuntimeError("draining")
        producer = object()
        self.producers.add(producer)
        return producer

    def publish_rename(self, producer):
        self.producers.remove(producer)
        self.issued += 1
        self.outcome[self.issued] = "waiting"
        return self.issued

    def submit(self):
        return self.publish_rename(self.admit())

    def detach(self, ticket):
        if self.outcome[ticket] == "waiting":
            self.outcome[ticket] = "cancelled"

    def begin_sync(self):
        if self.flushing is not None or self.issued == self.durable:
            return False
        self.flushing = self.issued
        self.flushes += 1
        return True

    def finish_sync(self, success):
        cut = self.flushing
        if cut is None:
            raise RuntimeError("no sync in flight")
        self.flushing = None
        if success:
            self.durable = cut
        for ticket, status in self.outcome.items():
            if ticket <= cut and status == "waiting":
                self.outcome[ticket] = "durable" if success else "io-error"

    def drained(self):
        return (not self.accepting and not self.producers and self.flushing is None
                and self.issued == self.durable)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def exercise():
    first = Directory()
    second = Directory()
    a, b = first.submit(), first.submit()
    other = second.submit()
    require(first.begin_sync(), "first origin did not start a sync")
    late = first.submit()
    require(not first.begin_sync(), "second sync started before first completed")
    first.finish_sync(True)
    require(first.outcome[a] == first.outcome[b] == "durable", "batch left waiters behind")
    require(first.durable == b and first.flushes == 1, "first batch frontier or sync count")
    require(first.outcome[late] == second.outcome[other] == "waiting", "frontier leaked")

    producer = first.admit()
    first.accepting = False
    try:
        first.admit()
    except RuntimeError:
        pass
    else:
        raise AssertionError("new producer admitted while draining")
    require(first.begin_sync(), "missing next batch")
    first.finish_sync(True)
    require(first.outcome[late] == "durable", "late rename did not receive its own flush")
    require(not first.drained(), "shutdown passed an admitted producer")

    final = first.publish_rename(producer)
    require(first.begin_sync(), "no final sync")
    first.finish_sync(False)
    require(first.outcome[final] == "io-error" and first.durable == late, "error frontier")
    require(not first.drained(), "failed sync counted as a clean shutdown")
    require(first.begin_sync(), "failed generation was lost")
    first.finish_sync(True)
    require(first.drained() and first.outcome[final] == "io-error", "drain and caller outcome")

    second.detach(other)
    require(second.begin_sync(), "cancelled caller dropped a published rename")
    second.finish_sync(True)
    require(second.durable == other and second.outcome[other] == "cancelled", "detach outcome")
    return {"success": True, "first_batch_renames": b, "final_durable_ticket": first.durable,
            "first_origin_sync_attempts": first.flushes, "other_origin_sync_attempts": second.flushes}


if __name__ == "__main__":
    report = exercise()
    path = Path(__file__).resolve().parent / "results" / "result.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
