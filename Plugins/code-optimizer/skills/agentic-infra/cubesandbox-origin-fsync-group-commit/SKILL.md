---
name: cubesandbox-origin-fsync-group-commit
description: Batch directory fsyncs when concurrent Cubelet rootfs renames share an origin and each successful create must wait for its rename to become durable.
metadata:
  author: theFool001
  tags: cubelet, rootfs, fsync
---

# Group directory fsyncs for one rootfs origin

## Locate the completion boundary

From the target revision, collect the storage backend, directory identities, rootfs rename and resize order, and the caller's definition of a successful create. Trace from Cubelet's `CreateSandboxRootfsFromTemplate` through the backend that owns the directory. A suitable path has multiple committed renames within the same parent directory, a way to sync that directory, and a completion point at which the caller can wait. Keep the existing synchronous path for other backends. [Cubelet storage entry in v0.7.1](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/Cubelet/storage/cubecow_volume_manager.go)

The historical pool variant performs a rename, then signals a flusher that waits up to a configured batching interval and calls `FsyncSnapshotDir`. That variant returns independently of the flush. To implement durable-name acknowledgement, introduce an explicit ticket for each rename and connect it to the create response. Keep file-data sync, index updates, resize and any other required storage barriers at their existing completion points. A directory fsync covers directory entries on the filesystem hosting that directory. [Historical Cubelet and cubecow changes](https://gitcode.com/openeuler/KPBot/commit/175e562b8068b156377b9fbcfd446b524c3452d1)

A refill permit accounts for background derive capacity. Releasing it or transferring a pool slot to the sandbox cannot acknowledge a pending rename. Gate the successful create response on the ticket for its own origin.

## Implement the coordinator

1. Key the coordinator by the backend instance and stable directory identity. Pin the directory handle or an equivalent generation-checked engine handle for its lifetime. Adapt name-based backends to keep the original origin identity stable through the flush. For a cross-directory move, follow the target's existing ordering and durability protocol for both parents.
2. Reserve a producer reference before mutation. If the rename fails before changing the directory, release the reference immediately and issue no ticket. Continue through the caller's existing error or direct-derive fallback path. Once the rename changes the directory, assign and register exactly one increasing ticket under the coordinator lock **before** releasing the reference, so shutdown sees either the producer or its pending ticket. A later resize failure or request cancellation may finish the caller with an error while the coordinator retains the ticket for flushing. If cancellation races an in-flight rename, let its completion owner settle the reference using the backend's mutation outcome.
3. With work pending, capture the highest registered ticket under the lock and start one flush. A bounded batching window may collect more work before the capture. Release the lock during I/O. New registrations above the captured ticket belong to a subsequent flush, even if the filesystem happened to include their updates.
4. On success, advance the durable frontier to the captured ticket and release covered waiters. A successful create also requires its normal device and storage steps to finish. On sync failure, preserve the frontier, report the error to the covered waiters and retain the dirty range for the target's retry or recovery policy. An individual request that received an error keeps that outcome. Preserve the ownership of already-renamed objects when handling retries.
5. At shutdown, close admission to new producers. Each admitted producer must either release its reference on confirmed non-mutation or hand off a published rename to a ticket. Drain the registered tickets and join the flusher before retiring the handle. Report an undrained frontier and the underlying error if final sync fails.

## Verify a migration

Run the Python 3 state-transition case from the skill directory. It drives admissions and flush completions independently for two origins, including a rename arriving during sync, cancellation, an injected I/O error and shutdown recovery.

```sh
python3 cases/flush-frontier/run.py
```

The case writes model results to `cases/flush-frontier/results/result.json`. For a historical-pool baseline on Linux, trace the storage process that owns the directory handle. Identify the origin by the decoded file descriptor and record rename, directory `fsync` and create completion times on one clock. Report the observed ordering and flush count. The historical pool acknowledges creates independently of its flusher.

Run the following acceptance checks on a build that connects create completion to a ticket:

- Stall the first sync after it captures its ticket range. Publish another rename on the same origin, then release the first sync while holding the next one. Only callers covered by the first range may receive a successful response. Release the second sync and observe the later caller complete. Repeat across two origins and inject a sync error.
- Begin shutdown with one rename admitted and no other work. Force that rename to fail before directory mutation. For this failed rename, verify the usual error or fallback and zero new tickets or flushes. Shutdown must finish once any separately admitted work settles. Separately, publish a rename and fail the subsequent resize. Expect the resize error and a registered ticket. Shutdown may complete only after its directory sync succeeds.
- In a disposable filesystem, interrupt the process at the agreed acknowledgement boundary and verify names and contents by the engine's normal reopen path.

Return the target revision, backend, acknowledgement point and per-origin sequence of registered tickets, flush cuts, durable frontier, errors and request completions. Compare flush counts for the same rename sequence against one sync per rename. Keep directory identities, durability policy and foreground adoption logic fixed across the two runs.
