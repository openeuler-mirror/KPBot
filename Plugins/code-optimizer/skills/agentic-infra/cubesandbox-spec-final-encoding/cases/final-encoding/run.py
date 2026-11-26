"""Model the order of canonicalization, normalization and final payload encoding."""
import json
from pathlib import Path


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def canonicalize(request):
    # Model private JSON ownership and representative transient fields.
    result = json.loads(json.dumps(request))
    result.pop("timeout", None)
    annotations = result.get("annotations") or {}
    annotations.pop("runtimeSnapshotID", None)
    result["annotations"] = annotations
    return result


def normalized_backend(request):
    # Fixture vocabulary for the accepted / omitted / unrecognized decisions.
    value = request.get("backend", "").strip().lower()
    return value if value == "xfs" else ""


class PayloadEncoder:
    def __init__(self):
        self.calls = 0

    def encode(self, canonical):
        self.calls += 1
        return json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")


def baseline(request, encoder):
    canonical = canonicalize(request)
    payload = encoder.encode(canonical)
    template = canonical["annotations"].get("template", "").strip()
    backend = normalized_backend(canonical)
    if backend:
        canonical["backend"] = backend
        canonical["annotations"]["storage"] = backend
        payload = encoder.encode(canonical)
    return template, backend, payload


def candidate(request, encoder):
    canonical = canonicalize(request)
    template = canonical["annotations"].get("template", "").strip()
    backend = normalized_backend(canonical)
    if backend:
        canonical["annotations"]["storage"] = backend
        canonical["backend"] = backend
    return template, backend, encoder.encode(canonical)


def main():
    cases = [
        ({"backend": " XFS ", "annotations": {"storage": "old", "template": "  t1 ",
                                              "runtimeSnapshotID": "transient"},
          "timeout": 5, "containers": [{"labels": {"group": "a"}}]},
         "t1", "xfs", {"backend": "xfs", "annotations": {"storage": "xfs", "template": "  t1 "},
                       "containers": [{"labels": {"group": "a"}}]}),
        ({"backend": "xfs", "annotations": None}, "", "xfs",
         {"backend": "xfs", "annotations": {"storage": "xfs"}}),
        ({"annotations": {"storage": "existing"}}, "", "",
         {"annotations": {"storage": "existing"}}),
        ({"backend": "unknown", "annotations": {"storage": "keep", "template": " t2"}},
         "t2", "", {"backend": "unknown", "annotations": {"storage": "keep", "template": " t2"}}),
    ]
    baseline_calls = candidate_calls = 0
    matched = 0
    for request, template, backend, expected in cases:
        original = json.loads(json.dumps(request))
        old = PayloadEncoder()
        new = PayloadEncoder()
        prior_record = baseline(request, old)
        require(request == original, "baseline changed caller-owned request")
        new_record = candidate(request, new)
        require(request == original, "candidate changed caller-owned request")
        require(new_record == prior_record, "template ID, record backend or serialized bytes changed")
        require(new_record[:2] == (template, backend), "unexpected projected columns")
        require(json.loads(new_record[2]) == expected, "unexpected canonical payload")
        require(old.calls == 1 + bool(backend) and new.calls == 1, "payload encode count per row")
        matched += 1
        baseline_calls += old.calls
        candidate_calls += new.calls

    for builder in (baseline, candidate):
        counter = PayloadEncoder()
        try:
            builder({"annotations": {"invalid": object()}}, counter)
        except TypeError:
            require(counter.calls == 0, "payload encoding started after a canonicalization error")
        else:
            raise AssertionError("expected canonicalization error")
    result = {"success": True, "matched_records": matched, "baseline_payload_encodes": baseline_calls,
              "candidate_payload_encodes": candidate_calls}
    output = Path(__file__).resolve().parent / "results" / "result.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
