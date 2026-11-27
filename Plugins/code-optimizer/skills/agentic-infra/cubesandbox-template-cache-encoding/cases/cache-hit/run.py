"""Compare cache accessor representations using the same JSON codec."""
import json
from pathlib import Path


class Codec:
    def __init__(self):
        self.encodes = 0
        self.decodes = 0

    def encode(self, request):
        self.encodes += 1
        return json.dumps(request, sort_keys=True).encode("utf-8")

    def decode(self, content):
        self.decodes += 1
        return json.loads(content)


class ObjectCache:
    def __init__(self):
        self.entries = {}
        self.codec = Codec()

    def fill(self, key, normalized):
        self.entries[key] = self.codec.decode(self.codec.encode(normalized))

    def read(self, key):
        if key in self.entries:
            return self.codec.decode(self.codec.encode(self.entries[key]))
        return None


class EncodedCache:
    def __init__(self):
        self.entries = {}
        self.codec = Codec()

    def fill(self, key, normalized):
        self.entries[key] = self.codec.encode(normalized)

    def read(self, key):
        if key not in self.entries:
            return None
        try:
            return self.codec.decode(self.entries[key])
        except ValueError:
            del self.entries[key]
            raise


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def exercise(cache):
    key = "template-a"
    normalized = {
        "annotations": {"storage": "xfs"}, "labels": None,
        "containers": [{"mounts": ["volume-a"]}], "volumes": [],
    }
    expected = json.loads(json.dumps(normalized))
    require(cache.read(key) is None, "absent entry")
    cache.fill(key, normalized)
    normalized["annotations"]["storage"] = "caller-edit"
    before_hits = cache.codec.encodes
    first = cache.read(key)
    second = cache.read(key)
    require(first == second == expected, "normalized fill and independent owner")
    first["containers"][0]["mounts"].append("extra")
    first["annotations"]["storage"] = "patched"
    first["requestID"] = "request-one"
    third = cache.read(key)
    require(second == third == expected, "one caller changed another caller or the cache")
    hit_encodes = cache.codec.encodes - before_hits
    del cache.entries[key]
    require(cache.read(key) is None, "invalidation")
    updated = {"annotations": {}, "volumes": ["new"], "requestID": "supplied"}
    cache.fill(key, updated)
    require(cache.read(key) == updated, "reloaded template")
    return third, hit_encodes


def main():
    baseline, old_encodes = exercise(ObjectCache())
    candidate = EncodedCache()
    observed, new_encodes = exercise(candidate)
    require(baseline == observed, "representation parity")
    require((old_encodes, new_encodes) == (3, 0), "warm-hit encoding count")
    require(candidate.codec.encodes == 2 and candidate.codec.decodes == 4,
            "expected one encode per fill and one decode per hit")
    candidate.entries["invalid"] = b"{"
    try:
        candidate.read("invalid")
    except ValueError:
        require(candidate.read("invalid") is None, "undecodable entry retained")
    else:
        raise AssertionError("missing decode error")
    result = {"success": True, "warm_hits": 3, "baseline_hit_encodes": old_encodes,
              "candidate_hit_encodes": new_encodes, "candidate_fill_encodes": candidate.codec.encodes}
    output = Path(__file__).resolve().parent / "results" / "result.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
