---
name: cubesandbox-spec-final-encoding
description: Encode the final CubeMaster sandbox spec once when normalizing an explicit storage backend would otherwise serialize the same request twice.
metadata:
  author: theFool001
  tags: cubemaster, sandbox-spec, json
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

From the skill directory run `python3 cases/final-encoding/run.py`. The fixture models the record boundary with a private canonical request and the v0.7.1 backend decisions for XFS, S3, historical aliases, blank and invalid inputs. It compares independent baseline and candidate records, including encoded payload bytes, and counts payload encodes separately from the canonical copy. Results are written to `cases/final-encoding/results/result.json`.

With Go on PATH, compare the fixture's ten backend decisions with the production `OptionalSnapshotBackend` function. From this skill directory run the following command, supplying a v0.7.1 checkout. The verifier compiles that checkout's `snapshot_backend.go` in a temporary directory and checks the value that would be stored in the backend column.

```sh
python3 cases/final-encoding/verify_backend.py /path/to/CubeSandbox
```

In a v0.7.1 checkout, verify the normalization rules separately using the production Go source and its upstream tests. The second command exercises canonicalization of real request types and requires the toolchain and dependencies declared by `CubeMaster/go.mod`. Run it on the baseline and on the candidate.

```sh
(cd CubeMaster/pkg/base/constants && GO111MODULE=off go test -count=1 snapshot_backend.go snapshot_backend_test.go)
(cd CubeMaster && go test -count=1 -run '^TestCanonicalizeRequest' ./pkg/sandboxspec)
```

In the target Go package, use the real constants and codec for explicit, omitted and invalid backend fixtures. Include conflicting pre-existing storage annotations, missing annotations, runtime-only fields and nested container data. Exercise the same input after an unsuccessful canonicalization or encoding and verify that the store publishes no partial record for that invocation. Expected final-payload counts on v0.7.1 are two to one for an accepted explicit backend, and one to one on the other branches. Deliver the change, per-branch counts, byte and row comparisons, and the input-ownership check.
