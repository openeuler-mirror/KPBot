---
name: cubesandbox-snapshot-binding-read
description: Evaluate and implement a single snapshot metadata read during CubeMaster create binding when readiness and placement share one consistency view. Use when tracing shows consecutive reads of the same snapshot and the target defines which committed state binding must observe.
---

# Read a checked snapshot record once during binding

During snapshot create binding, v0.7.1 first calls `EnsureSnapshotReadyForNewUse` and then `GetSnapshotRestoreSource` with the same snapshot ID. Both call `getSnapshotRecord`. The first call verifies eligibility under a template read lock. The second independently checks the row for deletion and projects placement data. This gives the resolver two opportunities to observe committed state. [Binding and fallback](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/service/httpservice/cube/cubeboxutil.go#L599-L615) · [Readiness check](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/templatecenter/snapshot_tombstone.go#L49-L68) · [Restore-source projection](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/templatecenter/snapshot_table.go#L187-L206)

## Trace the visibility boundary

From the target CubeSandbox checkout, locate the call pair and the code that updates status, remote readiness and origin metadata.

```sh
git rev-parse HEAD
rg -n 'EnsureSnapshotReadyForNewUse|GetSnapshotRestoreSource|bindSnapshotCreateReplicaWithHostMount|getSnapshotRecord|snapshotResourceLockKey' CubeMaster/pkg
```

For a successful bind, associate both reads with the same request, resolved snapshot ID, store and visibility scope. Inspect the caller's transaction, database isolation level, lock lifetime, and writer coverage, including other Master processes. A shared Go context or a process-local lock around only the first read does not provide a fixed view for both reads.

- When the target contract already binds against one immutable version or transaction snapshot, combine the reads **within that view**. A version identity must cover deletion eligibility and every placement field. Retain the existing check that permits dispatch or acquisition of that version if the target uses one.
- When the contract requires the latest committed placement or deletion state at the second statement, keep the second source lookup. Preserve its distinct error and fallback behavior wherever the target exposes a separately fallible source operation. Establishing a new transaction snapshot by itself changes the visibility policy; select that behavior only when the binding contract explicitly allows it.

## Implement the fixed-view path

1. Introduce a request-local helper that reads the row under the selected contract. Reuse the existing trim, lookup, tombstone and READY checks in their original order. Return the readiness error immediately for a missing or ineligible row. Leave the earlier HTTP locality probe in its existing position.
2. Project the target's full `RestoreSource` from that checked row. On v0.7.1 this includes snapshot ID, backend, remote status, origin node ID/IP, frozen host facts, instance type and export UUIDs. Pass the result and the original `pinToOrigin` flag to the existing placement function. Release lookup locks at the established ownership boundary.
3. Keep the two-read branch for live-view targets. In v0.7.1 a second lookup reporting `ErrSnapshotNotFound`, `ErrTemplateStoreNotInitialized`, or nil source with nil error reaches the original local binding path; a host mount requiring origin metadata returns an error there. Other source errors propagate. These rules apply after the initial readiness check has succeeded.

## Reproduce and accept

Run the stdlib contract fixture from the skill directory with Python 3.8+ and SQLite.

```sh
cd cases/binding-read
python3 run.py
```

The case uses two SQLite connections with WAL and counts executed snapshot SELECTs. Between readiness and placement a writer commits a metadata change or deletes the row. Within one fixed read snapshot, the candidate should use one SELECT and return the baseline placement. In autocommit mode, the candidate keeps the second SELECT and observes the committed change. `results/result.json` reports these counts. The fixture models the binding decision and a subset of placement fields.

On the target, compare baseline and candidate outcomes with the actual Go store and deployment isolation level. Exercise missing, deleting, building and READY snapshots, local and remote placement, and host-mount pinning. Coordinate two store connections to commit a deletion and a remote-status update at the inter-read boundary. Compare the selected node, scope, annotations, returned error and SQL count for each mode; test source lookup errors on the two-read path. From `CubeMaster`, run `go test -race ./pkg/templatecenter ./pkg/service/httpservice/cube`. Report which consistency contract was demonstrated and the SELECT count for that contract. Keep the existing path for unverified modes.
