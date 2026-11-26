---
name: cubesandbox-spec-final-encoding
description: Consolidate final JSON encoding in CubeMaster sandbox spec persistence when an accepted explicit backend causes the same canonical request to be encoded twice. Use to retain the persisted record and caller ownership while reducing final encodes on that branch.
---

# Finalize the sandbox spec before encoding

Start with the spec store from the target revision, the concrete create request type and examples of accepted, omitted and invalid backend values. Inspect the store's record construction. The relevant unit is one `RequestJSON` payload for one spec row.

## Identify the two encodings

In v0.7.1, `doPut` validates the inputs and calls `CanonicalizeRequest`. That helper makes a private JSON copy and removes transient fields. The caller marshals the canonical object, then examines the backend. When `OptionalSnapshotBackend` reports a valid explicit value, the caller updates the backend and storage annotation and overwrites the payload with a second marshal. With an omitted or invalid backend, the original branch leaves the separate record backend empty. The first final-payload encoding on the accepted branch is the operation to remove. [Record construction](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/sandboxspec/store.go#L90-L145) · [Canonical copy](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/sandboxspec/store.go#L221-L258)

## Implement at the record boundary

1. Locate the record builder. This may be `doPut` or a helper called before a queued write. Keep the store readiness, input validation and private canonicalization at their existing boundaries. Retain the canonicalizer's codec, map initialization, transient field handling and error propagation.
2. Take the template ID from the same canonical annotation and trim it as before. Preserve the current `OptionalSnapshotBackend` condition. For an accepted explicit backend, assign the normalized value to the record backend, canonical backend and storage annotation. Preserve the target's treatment of an omitted or unrecognized backend, including its existing annotation value.
3. Marshal the completed canonical request at the common point immediately before constructing the record. Return the codec error on failure. Use those bytes for `RequestJSON`, and populate all other record fields as before. Keep UPSERT, write ordering and scheduling in their existing locations.
4. Compare records for each backend branch with the target codec. Check the exact payload bytes, logical template ID, normalized record backend, trimmed host fields and backfilled flag. Compare the caller request before and after each call. If the target uses custom marshalers, cover the accepted value types and their error behavior before moving the first encoding.

## Reproduce and check

From the skill directory run `python3 cases/final-encoding/run.py`. The fixture models the record boundary with a private canonical request and a small representative backend policy. It compares independent baseline and candidate records, including encoded payload bytes, and counts payload encodes separately from the canonical copy. Results are written to `cases/final-encoding/results/result.json`.

In the target Go package, use the real constants and codec for explicit, omitted and invalid backend fixtures. Include conflicting pre-existing storage annotations, missing annotations, runtime-only fields and nested container data. Exercise the same input after an unsuccessful canonicalization or encoding and verify that the store publishes no partial record for that invocation. Expected final-payload counts on v0.7.1 are two to one for an accepted explicit backend, and one to one on the other branches. Deliver the change, per-branch counts, byte and row comparisons, and the input-ownership check.
