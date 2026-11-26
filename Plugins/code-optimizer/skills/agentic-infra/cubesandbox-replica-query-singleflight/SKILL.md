---
name: cubesandbox-replica-query-singleflight
description: Diagnose simultaneous READY replica list queries in CubeMaster and verify that one in-flight read can serve overlapping requests. Use when concurrent sandbox creates target the same template, database traces show overlapping duplicate reads, and placement must still be evaluated per request.
---

# Coalesce overlapping CubeMaster replica reads

1. Trace READY list reads by CubeMaster process, template and metadata generation. If a second create starts the same query while the first read is still in flight, evaluate singleflight. If it starts after the first read has returned, evaluate a short metadata TTL only when the placement policy permits bounded staleness. For mixed traffic, count overlapping and sequential reads separately. Record each caller's preferred node and placement outcome.
2. Inspect the target resolver for an existing in-flight group. In the Scheme 2 reference implementation, `resolveResultFetchGroup.Do` already covers the READY list. Use the case below to evaluate that path. On a serial target, group equivalent reads by operation, complete data scope and generation. Hold the group lock only to register or find a flight. Run the database query outside that lock.
3. Share only the ordered replica metadata. Apply preferred-node matching, request cancellation and the target's schedulability predicate for each caller. Include tenant or authorization scope in the key whenever it changes the rows a query can see.
4. Advance the generation after a successful metadata write. A request starting in the new generation must be able to read before the old operation completes. Recheck the generation before publishing or consuming the result. Bound the database operation and verify caller cancellation and shared-error handling on the target.
5. Run the [validation procedure](references/runbook.md). Compare equivalent enabled and disabled workloads and report measured query counts separately from sandbox creation time.
