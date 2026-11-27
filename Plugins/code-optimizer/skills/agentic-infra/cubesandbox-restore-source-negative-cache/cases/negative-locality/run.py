"""Exercise the negative locality decision using controlled store outcomes."""

from collections import OrderedDict
import json
from pathlib import Path


class MissingSource(Exception):
    pass


class StoreUnavailable(Exception):
    pass


class RequestCancelled(Exception):
    pass


class Fixture:
    def __init__(self):
        self.now = 0
        self.delay = 0
        self.rows = {}
        self.reads = 0
        self.local_checks = 0
        self.local_ready = True

    def lookup(self, key):
        self.reads += 1
        self.now += self.delay
        outcome = self.rows.get(key, MissingSource())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def local_check(self):
        self.local_checks += 1
        return self.local_ready


class LocalityProbe:
    def __init__(self, store, ttl=5, capacity=2):
        self.store = store
        self.ttl = ttl
        self.capacity = capacity
        self.entries = OrderedDict()

    def may_skip(self, key):
        start = self.store.now
        expiry = self.entries.get(key)
        if expiry is not None:
            if start < expiry:
                return False
            del self.entries[key]

        try:
            source = self.store.lookup(key)
        except MissingSource:
            if self.capacity > 0 and self.store.now < start + self.ttl:
                if len(self.entries) >= self.capacity:
                    self.entries.popitem(last=False)
                self.entries[key] = start + self.ttl
            return False
        except (TimeoutError, PermissionError, StoreUnavailable, RequestCancelled):
            return False
        return source == "cross-node-ready"

    def ready(self, key):
        return self.may_skip(key) or self.store.local_check()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    store = Fixture()
    probe = LocalityProbe(store)
    key = ("tenant-a", "template-x")
    check(probe.ready(key), "first request should pass the local check")
    store.local_ready = False
    check(not probe.ready(key), "a hit must observe the current local result")
    reads, local = store.reads, store.local_checks
    check((reads, local) == (1, 2), "repeated absence lost per-request checks")

    store.rows[key] = "cross-node-ready"
    store.now = 4
    check(not probe.ready(key) and store.reads == 1, "entry should cover the accepted window")
    other = ("tenant-b", key[1])
    store.rows[other] = "cross-node-ready"
    check(probe.ready(other) and store.reads == 2, "scopes shared an absence")
    store.now = 5
    visible = probe.ready(key)
    check(visible and store.reads == 3, "source should be observed at expiry")

    transient = (None, TimeoutError(), PermissionError(), StoreUnavailable(), RequestCancelled(),
                 "local-only", "cross-node-ready")
    for outcome in transient:
        f = Fixture()
        p = LocalityProbe(f)
        f.rows[key] = outcome
        check(p.may_skip(key) == (outcome == "cross-node-ready"), "initial decision changed")
        f.rows[key] = "cross-node-ready"
        check(p.may_skip(key) and f.reads == 2, "a present source or transient result was cached")

    slow = Fixture()
    slow.delay = 6
    p = LocalityProbe(slow)
    p.may_skip(key)
    slow.rows[key] = "cross-node-ready"
    check(p.may_skip(key) and slow.reads == 2, "slow read renewed an expired absence")

    bounded = Fixture()
    p = LocalityProbe(bounded, capacity=2)
    for name in ("a", "b", "c", "a"):
        p.may_skip(("tenant", name))
    check(len(p.entries) == 2 and bounded.reads == 4, "capacity or eviction changed")

    bypass = Fixture()
    p = LocalityProbe(bypass, capacity=0)
    check(not p.may_skip(key) and not p.may_skip(key) and bypass.reads == 2,
          "disabled cache should use the original lookup")

    result = {"repeated_absence_reads": reads, "repeated_local_checks": local,
              "expiry_restored_cross_node": visible, "uncached_response_pairs": len(transient)}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
