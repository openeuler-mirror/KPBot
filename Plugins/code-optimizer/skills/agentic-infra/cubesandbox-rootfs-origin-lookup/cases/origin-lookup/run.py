"""Model Cubelet retry lookup against a source-scoped enumeration oracle."""

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    kind: str
    origin: str
    path: str


class Catalog:
    def __init__(self, siblings=0):
        self.entries = {
            "base": Entry("volume", "", "/base"),
            "other": Entry("volume", "", "/other"),
            "child": Entry("snapshot", "base", "/child"),
            "foreign": Entry("snapshot", "other", "/foreign"),
        }
        for number in range(siblings):
            name = f"sibling-{number}"
            self.entries[name] = Entry("snapshot", "base", f"/{name}")
        self.list_calls = self.list_entries = self.point_calls = 0

    def listed_child(self, source, name):
        self.list_calls += 1
        parent = self.entries.get(source)
        if parent is None or parent.kind != "volume":
            return None
        self.list_entries += len(self.entries)
        children = sorted(key for key, item in self.entries.items()
                          if item.kind == "snapshot" and item.origin == source)
        return self.entries[name] if name in children else None

    def indexed_child(self, source, name):
        self.point_calls += 1
        parent = self.entries.get(source)
        child = self.entries.get(name)
        if parent is None or parent.kind != "volume" or child is None:
            return None
        return child if child.kind == "snapshot" and child.origin == source else None

    def resolve(self, source, name, create_result, indexed, after_lookup=None):
        if create_result != "exists":
            return create_result
        lookup = self.indexed_child if indexed else self.listed_child
        child = lookup(source, name)
        if child is None:
            return "conflict"
        if after_lookup:
            after_lookup()
        if self.entries.get(name) is not child:
            return "changed"
        return child.path


def expect(actual, expected):
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def main():
    for size in (0, 64):
        baseline, candidate = Catalog(size), Catalog(size)
        expect(candidate.resolve("base", "fresh", "/new", True), "/new")
        expect(candidate.resolve("base", "fresh", "io-error", True), "io-error")
        expect((candidate.list_calls, candidate.point_calls), (0, 0))

        for source, name, expected in (
            ("base", "child", "/child"), ("base", "foreign", "conflict"),
            ("base", "other", "conflict"), ("base", "absent", "conflict"),
            ("absent", "child", "conflict"), ("child", "child", "conflict"),
        ):
            expect(baseline.resolve(source, name, "exists", False), expected)
            expect(candidate.resolve(source, name, "exists", True), expected)
        expect((candidate.point_calls, candidate.list_calls), (6, 0))
        expect(baseline.list_entries, 4 * (size + 4))

    # An engine without the point-lookup capability dispatches to its scoped list.
    fallback = Catalog()
    expect(fallback.resolve("base", "child", "exists", False), "/child")
    expect((fallback.list_calls, fallback.point_calls), (1, 0))

    # A child record can outlive the volume that originally supplied its name.
    for replacement in (None, Entry("snapshot", "other", "/new-base")):
        catalog = Catalog()
        del catalog.entries["base"]
        if replacement is not None:
            catalog.entries["base"] = replacement
        for indexed in (False, True):
            expect(catalog.resolve("base", "child", "exists", indexed), "conflict")

    interleaved = Catalog()

    def replace_child():
        interleaved.entries["child"] = Entry("snapshot", "other", "/replacement")

    expect(interleaved.resolve("base", "child", "exists", True, replace_child), "changed")
    report = {
        "success": True,
        "point_lookups": candidate.point_calls,
        "fast_path_list_calls": candidate.list_calls,
        "baseline_entries_inspected_64_siblings": baseline.list_entries,
        "fallback_list_calls": fallback.list_calls,
    }
    target = Path(__file__).resolve().parent / "results" / "result.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
