---
name: cubesandbox-rootfs-refill-budget
description: Bound rootfs pool inventory and concurrent background derives when multiple template generations refill pools on the same Cubelet storage node.
metadata:
  author: theFool001
  tags: cubelet, rootfs, refill
---

# Budget rootfs refills across templates

## Identify the admission points

Use the target revision, the Cubelet processes serving the storage, the set of template generations, and a trace of foreground adoptions and background derives. The historical pool indexes by `templateSource`. Each pool has its own target and `building` flag. `kickRootfsPoolFill` starts a builder for a pool while `adoptFromRootfsPool` consumes available members. Trace at least two simultaneous pools on the same node. [Historical Cubelet changes](https://gitcode.com/openeuler/KPBot/commit/175e562b8068b156377b9fbcfd446b524c3452d1) · [Upstream Cubelet storage path](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/Cubelet/storage/cubecow_volume_manager.go)

## Implement admission and ownership

1. Choose two limits for the shared storage domain. Count all pool-owned members against an inventory limit `S`, and all active background derives against an I/O concurrency limit `D`. Specify the units explicitly. Member counts bound object counts. If byte or inode quotas matter, account for them separately. One Cubelet process can own a node budget when it is the sole builder. Multiple processes sharing a backend need a common admission authority or fixed quotas whose sum fits the node limit.
2. Use a coordinator with one pending entry per template generation. Select the oldest waiting pool below its target that has no active builder. Reserve **both** a member slot and a derive permit before starting I/O. Release the coordinator lock during storage operations. After one grant, requeue remaining demand behind other eligible pools. A full inventory can stay full until a member is adopted or removed. If hot templates require reserved capacity, define per-pool ceilings or a separate reclaim policy for **available** members.
3. On successful derive, release the I/O permit and move the same slot to available. A foreground reservation keeps its slot through rename. Release the charge when ownership transfers to the sandbox; return a confirmed intact and reusable member to available after a cancelled reservation. An empty pool uses the existing foreground creation path. Count this separately from background derives.
4. Give each physical member a name scoped to the template, an immutable pool generation and a slot. Persist the generation before deriving and allocate a fresh generation on replacement, including across Cubelet restarts. The historical implementation formats names from template source and slot alone and accepts `AlreadyExists` during refill. For legacy names, reserve the name across builders, adopters and cleanup until the old generation's operations have settled and its object has been reconciled.
5. Record the owning generation and stable backend object ID with the grant. Accept `AlreadyExists` for recovery only when the recorded owner and ID match the object at that name and it is still a reusable pool snapshot of the expected origin. Close admissions on retirement, withdraw queued demand, and keep in-flight derives and foreground reservations charged until they settle. Send late completions and possible partial objects to cleanup. Delete only the recorded incarnation using an atomic identity-checked delete or a backend lock covering lookup, comparison and deletion across every name mutator. When a first derive definitively collided with a foreign object and never created a member, release that failed grant and leave the existing owner and charge intact. If an earlier write may have created a member, or cleanup finds a different incarnation, hold the old charge for reconciliation. Use an authoritative inventory or backend completion record to locate the old ID, then clean it or release its charge once removal or ownership transfer is proven. Account for the replacement under its actual owner. Rebuild the ledger and outstanding name reservations before admitting work after restart.
6. Instrument a snapshot of the ledger on each transition. Its charged inventory is the sum of granted derives, available members, foreground reservations and objects awaiting cleanup. Transfer charges between states under the same authority. Use bounded counters, explicit completion ownership and per-generation job IDs so duplicate callbacks cannot return a second grant.

The inventory slot can transfer at adoption even while the create is waiting for directory durability. Keep the backend's acknowledgement point: if it requires a directory flush, the request waits for that flush independently of refill admission.

## Exercise a mixed-template workload

Run this Python 3 contract from the skill directory. The deterministic scheduler models slot and permit ownership across interleaved operations. It reports the observed peaks, including completion of a retired generation and cleanup of a partial derive.

```sh
python3 cases/node-budget/run.py
```

Read `cases/node-budget/results/result.json`. For the actual backend, record the current per-pool policy as a baseline, then enable the shared limits on the same storage and templates. Introduce several cold templates while a hot template continues creating, hold one derive open, retire its generation and resume it after a replacement generation appears. Exercise a full inventory, adoption and the normal empty-pool fallback. Check maximum charged slots and active derives, grant order across eligible pools, and charges after cancellation and cleanup. Check the mounted rootfs and selected template generation for each successful create.

Repeat with the same template and slot across retirement and restart. Record the physical names and backend IDs. Queue an old cleanup, replace the object at its recorded name, then release cleanup. The replacement must survive and the unresolved grant must remain charged. Force an `AlreadyExists` collision and verify that only a proven member of the requesting generation reaches its available list. The identity fixture reports one outstanding cleanup charge at the mismatch, before reconciliation; a confirmed no-create collision releases its separate grant immediately. The fixture models the guarded delete as one operation; verify the equivalent exclusion or conditional delete on the target storage engine.

On the pool-enabled target, pause builders at the storage derive entry. Count operations that actually entered the backend before releasing any of them, then reconcile pool-owned members with a backend inventory after the builders settle. Compare both observations with the coordinator ledger. Use a build containing the pool for this check because stock v0.7.1 has no per-template refill workers.

Return the chosen inventory and concurrency limits, their scope, per-template demand, admissions, adoptions, foreground fallbacks, queued or reclaimable inventory, and reconciled peaks. Record background I/O and request latency if the target exposes them. These observations distinguish budget enforcement from workload-specific performance.
