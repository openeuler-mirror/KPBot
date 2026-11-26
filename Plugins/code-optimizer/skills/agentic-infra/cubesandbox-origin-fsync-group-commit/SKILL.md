---
name: cubesandbox-origin-fsync-group-commit
description: Implement directory fsync batching for CubeSandbox rootfs adoptions when concurrent renames share one origin directory and the caller requires durable completion. Map each successful rename to a verifiable flush frontier and preserve the existing creation response contract.
---

# Group directory fsyncs for one rootfs origin

## Locate the completion boundary

From the target revision, collect the storage backend, directory identities, rootfs rename and resize order, and the caller's definition of a successful create. Trace from Cubelet's `CreateSandboxRootfsFromTemplate` through the backend that owns the directory. A suitable path has multiple committed renames within the same parent directory, a way to sync that directory, and a completion point at which the caller can wait. Keep the existing synchronous path for other backends. [Cubelet storage entry in v0.7.1](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/Cubelet/storage/cubecow_volume_manager.go)

The historical pool variant performs a rename, then signals a flusher that waits up to a configured batching interval and calls `FsyncSnapshotDir`. That variant returns independently of the flush. To implement durable-name acknowledgement, introduce an explicit ticket for each rename and connect it to the create response. Keep file-data sync, index updates, resize and any other required storage barriers at their existing completion points. A directory fsync covers directory entries on the filesystem hosting that directory. [Historical Cubelet and cubecow changes](https://gitcode.com/openeuler/KPBot/commit/175e562b8068b156377b9fbcfd446b524c3452d1)

## Implement the coordinator

1. Key the coordinator by the backend instance and stable directory identity. Pin the directory handle or an equivalent generation-checked engine handle for its lifetime. Adapt name-based backends to keep the original origin identity stable through the flush. For a cross-directory move, follow the target's existing ordering and durability protocol for both parents.
2. Reserve a producer reference before mutation. Once the rename publishes a directory change, register it exactly once and allocate an increasing ticket under the coordinator lock, including when a later resize fails. Release the producer reference after registration. A caller that cancels may detach its waiter. The coordinator still owns the directory change.
3. With work pending, capture the highest registered ticket under the lock and start one flush. A bounded batching window may collect more work before the capture. Release the lock during I/O. New registrations above the captured ticket belong to a subsequent flush, even if the filesystem happened to include their updates.
4. On success, advance the durable frontier to the captured ticket and release covered waiters. A successful create also requires its normal device and storage steps to finish. On sync failure, preserve the frontier, report the error to the covered waiters and retain the dirty range for the target's retry or recovery policy. An individual request that received an error keeps that outcome. Preserve the ownership of already-renamed objects when handling retries.
5. At shutdown, close admission to new producers, wait for admitted producers to register completed mutations, then flush and join existing work until all registered generations are durable. Report an undrained frontier and the underlying error if final sync fails. Retire the handle only under the target's shutdown or recovery protocol.

## Verify a migration

Run the Python 3 state-transition case from the skill directory. It drives admissions and flush completions independently for two origins, including a rename arriving during sync, cancellation, an injected I/O error and shutdown recovery.

```sh
python3 cases/flush-frontier/run.py
```

The result in `cases/flush-frontier/results/result.json` describes the logical protocol. For target integration, instrument the real backend's sync entry and create acknowledgement. Stall the first sync while publishing another rename. Release the first sync and check that only its captured generations can acknowledge. Repeat across two actual origins, inject an I/O error, and exercise shutdown with admitted producers. In a disposable filesystem, include recovery after interruption at the agreed acknowledgement boundary and verify the names and contents by the engine's normal reopen path.

Return the target revision, backend, acknowledgement point and per-origin sequence of registered tickets, flush cuts, durable frontier, errors and request completions. Compare flush counts for the same rename sequence against one sync per rename. Keep directory identities, durability policy and foreground adoption logic fixed across the two runs.
