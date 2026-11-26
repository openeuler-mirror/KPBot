---
name: cubesandbox-restore-source-negative-cache
description: Reuse a recent missing snapshot restore source in CubeMaster when creates for the same identifier repeat the locality probe. Use when local replica checks remain request-specific and the deployment accepts a bounded interval before newly visible sources are observed.
---

# Reuse a missing restore source during the locality probe

In v0.7.1 a template create asks whether it can skip local replica readiness. A restore-source lookup supplies the backend and remote status for that decision. A missing source yields `false`, and the caller runs `EnsureTemplateLocalityReady`. Repeated creates on one Master can reuse that negative decision while still checking local readiness for each request. [Locality call site](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/service/httpservice/cube/cubeboxutil.go#L446-L462) · [Decision function](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/service/httpservice/cube/cubeboxutil.go#L545-L551)

## Locate and qualify the read

From the target CubeSandbox checkout, record the revision and inspect the lookup, its error classification, and the callers of the skip decision.

```sh
git rev-parse HEAD
rg -n 'skipTemplateLocalityReady|getSnapshotRestoreSourceFn|EnsureTemplateLocalityReady|snapshotRejectsNewUse' CubeMaster/pkg
```

Count restore-source reads and local readiness checks separately, grouped by Master process, resolved ID and request scope. Choose a TTL consistent with the accepted create-after-publication visibility interval. The store can return `ErrSnapshotNotFound` both for a missing row and for a row hidden by its lifecycle state. Establish the required handling of newly created or restored rows in both cases. When such rows must be visible immediately, keep reading from the store unless all relevant writers and Master instances participate in an ordered invalidation protocol. [Snapshot store semantics](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/templatecenter/snapshot_table.go#L187-L206)

## Implement the decision

1. Scope a bounded, process-local cache to the locality probe. Key by the normalized resolved ID and every dimension that changes visibility, such as tenant or namespace on targets that use them. A live entry means “perform this request’s local readiness check.” Keep errors and results from the readiness check on the caller's path.
2. On a miss, record the monotonic start time, release the cache lock and execute the existing lookup with the request context. Insert only for `errors.Is(err, templatecenter.ErrSnapshotNotFound)`. Use the existing conservative decision for nil-without-error, cancellation, deadline, store, or permission errors and leave them uncached. For a present source, evaluate `CanCrossNode` from the returned backend and status on that request.
3. Expire at lookup start plus the chosen TTL. Discard a result that has already reached expiry, and bound retained keys. Synchronize cache access without serializing independent store reads. Bypass the cache when disabled. Keep placement, binding and snapshot-use authorization on their original paths.
4. If local writes invalidate negative entries, fence publication of in-flight misses across invalidation. For example, sample a generation on miss, advance it after a committed change, and fill only when the generation still matches. Separate processes require their own visibility contract. The model below uses expiry alone.

## Reproduce and accept

Run the stdlib case from this skill directory with Python 3.8 or later.

```sh
cd cases/negative-locality
python3 run.py
```

The fixture injects lookup outcomes and advances a logical clock. Two requests for a missing key should issue one source read and two local checks; insertion of a cross-node source becomes visible at expiry. It also checks independent scopes, slow misses, capacity eviction, and uncached errors and present sources. Read `results/result.json` for the observed counts.

On the target, use the real lookup hook to count reads in the Go tests. Compare enabled and bypassed runs for repeated IDs, then publish a source between creates and check behavior at the agreed boundary. Change local readiness between two negative hits to confirm it is evaluated each time. From `CubeMaster` run `go test -race ./pkg/service/httpservice/cube ./pkg/templatecenter`. Hand back the cache scope, TTL/capacity, visibility contract, read counts and create outcomes. End-to-end timing is optional.
