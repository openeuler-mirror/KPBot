# Reproduce and validate READY replica metadata reuse

## Apply the reference implementation

Use CubeSandbox v0.7.1 at commit `31d911e430fdf8a8879bd062b8e78066c1a8e89d` in a clean checkout. Set `PATCH_PATH` to the absolute path to [replica-metadata-ttl.patch](replica-metadata-ttl.patch).

Use one READY replica cache implementation. This patch and `scheme2-rsi-cache.patch` both modify `ResolveTemplateReadyReplica` and `UpsertReplica` and use independent invalidation protocols. They are mutually exclusive. If the target already uses `readyReplicaResultCache`, keep the existing implementation or evaluate this patch on a separate clean checkout.

Choose this opt-in, TTL-only patch to measure reuse across completed requests with an independent switch. The existing Scheme 2 implementation also coalesces simultaneous misses with singleflight and invalidates by template. Use that variant when concurrent misses are the primary bottleneck and template-scoped invalidation matters.

```sh
git apply --numstat "$PATCH_PATH"
git apply --check "$PATCH_PATH"
git apply "$PATCH_PATH"
(cd CubeMaster && CGO_ENABLED=0 make build)
```

Use the Go version and build prerequisites declared in the target source tree. Verify the local write paths for replica insertion, update, compatibility changes, hard deletion, orphan reconciliation, snapshot removal, artifact transaction commits, and template-cache invalidation. For other revisions, audit additional writers and retain their existing transaction boundaries. Invalidate after a successful commit.

## Verify behavior

From the skill directory, run `python3 cases/sequential-ttl/run.py` with Git and Go 1.20 or later on the PATH. The case parses the full patch, applies the resolver changes to a source excerpt, and compiles those patched functions and the bundled cache implementation with a fake metadata store and clock. It checks query counts, per-request selection, expiry, and invalidation independently of a deployment. Run the target's own template-center tests and exercise the real deployment before rollout.

The feature defaults to off. The disabled path converts rows one at a time and stops at the first match, as in the original resolver. For the enabled arm, set `CUBEMASTER_READY_REPLICA_TTL=1` in the CubeMaster process environment and restart it using the target's release mechanism. For the disabled arm, unset the variable and restart the same binary. Confirm the effective process configuration before collecting measurements.

In a single-Master test environment, issue consecutive same-template requests after the first request completes, with resolver invocations less than a second apart. Count SQL queries against the replica table and record selected node IDs and errors. Three reads within the same TTL window should issue three replica-list queries with caching disabled and one with it enabled. Check the preferred-node result on a hit, re-query after expiry, and verify that a committed update or deletion changes the next request. When the target consults current node state independently of stored replica metadata, change that state between two requests and confirm that a hit still uses the live predicate.

The TTL starts at query entry. A slow query can return its result without producing a cache entry. Missing READY data and oversized results bypass storage. SQL errors return immediately, even if the local write epoch advanced. An epoch change triggers a retry only after a successful read. The map is process-local. Local invalidation clears all entries, which can reduce reuse during writes. Concurrent misses each perform their own query. Other processes and direct database writes do not share the invalidation epoch.

## Measure creation time

Use the same binary, template, initial capacity, placement constraints, and workload for both arms. Restore equivalent initial state and remove each arm's created instances before repeating. Record the replica-list SQL count, successful creates, errors, and `total_time` for each run.

Run a sequential batch to observe reuse across completed requests. Also run the representative production concurrency as a separate measurement. For example, with a configured `cube-bench` target and a different `REPORT_JSON` path per run:

```sh
cube-bench -c 1 -n 100 -w 0 -m create-only --no-tui -o "$REPORT_JSON"
```

Track the IDs of instances left by `create-only` and clean them through the deployment's usual deletion workflow. If consecutive resolver calls fall outside the TTL, use a controlled resolver trace to verify the mechanism and report the end-to-end workload as observed. An initial burst of simultaneous misses may show little reuse. Compare `total_time` only across runs with equivalent success counts and record the actual measurements.

If placement or visibility checks fail, unset `CUBEMASTER_READY_REPLICA_TTL`, restart CubeMaster, and confirm that consecutive requests query the replica table again.
