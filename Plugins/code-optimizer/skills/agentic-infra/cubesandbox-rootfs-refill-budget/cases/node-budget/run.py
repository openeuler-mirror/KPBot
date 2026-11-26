"""Drive a single-node refill ledger through deterministic interleavings."""
from collections import deque
import json
from pathlib import Path


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Budget:
    def __init__(self, slots, permits):
        self.slots = slots
        self.permits = permits
        self.waiting = deque()
        self.closed = set()
        self.members = {}
        self.next_id = 0
        self.peak_slots = 0
        self.peak_io = 0
        self.fallbacks = 0

    def request(self, pool):
        if pool not in self.closed and pool not in self.waiting:
            self.waiting.append(pool)

    def dispatch(self):
        active = [owner for owner, state in self.members.values() if state == "deriving"]
        if len(self.members) >= self.slots or len(active) >= self.permits:
            return None
        pool = next((owner for owner in self.waiting if owner not in active), None)
        if pool is None:
            return None
        self.waiting.remove(pool)
        self.next_id += 1
        self.members[self.next_id] = (pool, "deriving")
        self.peak_io = max(self.peak_io, len(active) + 1)
        self.peak_slots = max(self.peak_slots, len(self.members))
        return self.next_id

    def complete(self, member, *, ok=True, object_exists=True):
        pool, state = self.members[member]
        require(state == "deriving", "completion requires an active grant")
        if ok and pool not in self.closed:
            self.members[member] = (pool, "available")
        elif object_exists:
            self.members[member] = (pool, "cleanup")
        else:
            del self.members[member]

    def reserve(self, pool):
        for member, (owner, state) in self.members.items():
            if owner == pool and state == "available":
                self.members[member] = (pool, "reserved")
                return member
        self.fallbacks += 1
        return None

    def settle(self, member, adopted):
        pool, state = self.members[member]
        require(state == "reserved", "settling requires a reservation")
        if adopted:
            del self.members[member]
        else:
            self.members[member] = (pool, "cleanup" if pool in self.closed else "available")

    def retire(self, pool):
        self.closed.add(pool)
        if pool in self.waiting:
            self.waiting.remove(pool)
        for member, (owner, state) in self.members.items():
            if owner == pool and state == "available":
                self.members[member] = (pool, "cleanup")

    def removed(self, member):
        require(self.members[member][1] == "cleanup", "expected deletion confirmation")
        del self.members[member]


def exercise():
    ledger = Budget(slots=3, permits=2)
    hot, cold, replacement = ("hot", 1), ("cold", 1), ("cold", 2)
    ledger.request(hot)
    hot_job = ledger.dispatch()
    ledger.request(hot)
    require(ledger.dispatch() is None, "second builder admitted for the same pool")
    ledger.request(cold)
    cold_job = ledger.dispatch()
    require(ledger.members[cold_job] == (cold, "deriving"), "busy pool blocked eligible peer")
    require(ledger.dispatch() is None, "I/O limit failed")

    ledger.complete(hot_job)
    reservation = ledger.reserve(hot)
    require(reservation == hot_job, "expected first hot member")
    require(ledger.reserve(hot) is None, "empty pool must use foreground fallback")
    next_hot = ledger.dispatch()
    ledger.request(cold)
    ledger.retire(cold)
    ledger.request(replacement)
    ledger.complete(cold_job)
    require(ledger.members[cold_job] == (cold, "cleanup"), "stale completion published")
    require(ledger.dispatch() is None, "available I/O permit ignored occupied inventory")

    ledger.settle(reservation, adopted=False)
    require(ledger.reserve(hot) == reservation, "cancelled reservation lost member")
    ledger.settle(reservation, adopted=True)
    new_job = ledger.dispatch()
    require(ledger.members[new_job] == (replacement, "deriving"), "wrong generation admitted")
    ledger.complete(new_job, ok=False, object_exists=True)
    ledger.complete(next_hot)
    ledger.request(replacement)
    require(ledger.dispatch() is None, "partial object lost its charge")
    ledger.removed(new_job)
    retry = ledger.dispatch()
    require(ledger.members[retry] == (replacement, "deriving"), "retry did not get freed slot")
    ledger.complete(retry, ok=False, object_exists=False)
    ledger.removed(cold_job)
    ledger.retire(hot)
    ledger.removed(next_hot)
    require(not ledger.members and not ledger.waiting, "ledger did not drain")

    # After a new generation is admitted, its next request joins the tail.
    hot_again = ("hot", 2)
    ledger.request(hot_again)
    ledger.request(replacement)
    grant = ledger.dispatch()
    require(ledger.members[grant][0] == hot_again, "oldest eligible pool did not get a turn")
    ledger.complete(grant, ok=False, object_exists=False)
    ledger.request(hot_again)
    grant = ledger.dispatch()
    require(ledger.members[grant][0] == replacement, "requeued pool bypassed another waiter")
    ledger.complete(grant, ok=False, object_exists=False)
    ledger.retire(hot_again)
    return {"success": True, "peak_charged_slots": ledger.peak_slots,
            "peak_active_derives": ledger.peak_io, "foreground_fallbacks": ledger.fallbacks,
            "remaining_charges": len(ledger.members)}


if __name__ == "__main__":
    report = exercise()
    path = Path(__file__).resolve().parent / "results" / "result.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
