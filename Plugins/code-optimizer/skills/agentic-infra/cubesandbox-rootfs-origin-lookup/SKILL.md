---
name: cubesandbox-rootfs-origin-lookup
description: Verify an existing CubeSandbox rootfs snapshot by name when a Cubelet create retry reports AlreadyExists and origin-scoped enumeration visits sibling snapshots. Apply the lookup to backends that can prove object kind, origin and identity.
---

# Verify a retried rootfs snapshot by name

Use this method when retry or replay traffic reaches `XfsCow.createOrResolveSnapshotPathFromSource`. Collect the CubeSandbox revision, the selected storage backend, counts of `SemAlreadyExists` and a sample of sibling counts for the affected origin. Count fresh creates separately from retries.

## Locate the two paths

From the target CubeSandbox checkout run:

```bash
git rev-parse HEAD
rg -n 'createOrResolveSnapshotPathFromSource|ensureSnapshotOrigin|ResolveDevPath' Cubelet/storage
rg -n 'GetVolumeInfo|ListSnapshots' Cubelet/pkg/cubecow
rg -n 'fn list_snapshots|name_index|origin_volume' cubecow/src/engine/reflink.rs
```

In v0.7.1 a successful create already returns its device path. On `SemAlreadyExists`, Cubelet calls `ListSnapshots(sourceName, 0, "")`, checks the entry and then resolves the device path. Membership in that **source-scoped** list can establish origin even when the returned row omits `OriginVolume`. The Reflink backend also checks that the named source exists as a volume before enumerating. Its name index records the origin of a snapshot, whereas the generic Go volume-info result omits this relation. [Cubelet retry and validation](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/Cubelet/storage/cubecow_volume_manager.go#L377-L411) · [Reflink list](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/cubecow/src/engine/reflink.rs#L906-L938) · [Volume-info projection](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/cubecow/src/engine/reflink.rs#L546-L561)

## Implement the retry lookup

1. Preserve the success path from `CreateSnapshotFromVolume` and the error classification of other create failures. Invoke the lookup only for an existing object.
2. Expose a backend operation that accepts **both** the expected source and the target name. In Reflink, use one index read guard to verify the source is a volume, the target is a snapshot, and the recorded origin equals the expected source. Adapt the Rust, C and Go boundaries together if the target revision lacks this operation.
3. Return a path bound to the verified object, or carry a stable object identity into path resolution and revalidate it there. Test deletion and same-name recreation between verification and resolution. Use the target's existing object-lifetime protocol when returning the path.
4. Select the original scoped listing for engines without authoritative point lookup. Preserve the caller-visible outcome for a missing source, absent child, wrong kind, wrong origin, lookup error or path-resolution error. On the fast path an empty origin from an unscoped response is insufficient to accept the object.

## Verify and report

From the skill directory run `python3 cases/origin-lookup/run.py`. This standard-library contract model compares both lookup paths over the same catalog and writes counters to `cases/origin-lookup/results/result.json`. It also replaces a verified child under the same name before path resolution and checks the modeled identity guard.

On the target backend, exercise fresh creation, a same-source retry, a same-name conflict under a different source, an unsupported backend and deletion/recreation. Check returned path identity and error classes against the target contract. Repeat with small and large sibling sets. Report retry count, scoped list calls, inspected entries and keyed lookups for the same workload. A fresh create should use neither recovery lookup. The target integration check covers the native index, FFI and actual device resolution.
