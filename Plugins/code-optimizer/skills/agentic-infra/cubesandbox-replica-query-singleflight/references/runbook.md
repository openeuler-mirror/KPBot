# Reproduce in-flight read sharing

From the skill directory, run the following with Go 1.20 or later. The standalone Go module uses the standard library.

```sh
go -C cases/concurrent-reads test -count=1 -timeout=30s -v .
```

The case contains the READY list resolver and generation cache from the CubeSandbox v0.7.1 Scheme 2 reference implementation. The fetch group includes a test-only notification when a follower joins. A fake metadata store counts reads and supplies barriers. The fixture also models a request-time health predicate. Target versions may implement different placement rules.

- **Same key and generation.** Block the first list read. Start a second request for the same template with another preferred node. Observe it join before releasing the first read, and complete an unrelated template request while both wait. Expect one list read per template and the requested node for each caller. No completed value exists when the follower joins.
- **Write during a read.** Hold a read after it captures the old rows, update the fixture and invalidate the template. Resolve the new generation before releasing the old one. Both callers must eventually return the new node, with one read per generation.

## Validate the deployment

The cancellation case blocks a leader's read, joins a live follower, then cancels the leader. The canceled request must return its own error and the follower must obtain its preferred node through a new generation. Ordinary database errors retain their original meaning.

The consistency test checks SHA-256 pins for the resolver and cache fixtures and both Scheme 2 patch copies in a KPBot checkout. For a standalone export, set `SCHEME2_REFERENCE_PATCH` to an absolute patch path to check that revision as well. When the reference changes, apply it to the pinned v0.7.1 source, compare the two resolver declarations and the generated cache file, then refresh the three digests after review.

Use one CubeMaster process and a fixed template and placement policy. Record baseline and candidate database read counts per process, join counts, errors, and successful creates. Keep completed-result TTL configuration equal across arms and start each burst with cold metadata. Exercise an unrelated template during the burst and a committed replica update while a read is outstanding.

Inject a database error and cancel a follower while the leader is blocked. Verify each caller's deadline and check that the next request can start a fresh read after a failed flight. The reference group's wait on the leader is synchronous. A target requiring prompt follower cancellation needs a context-aware wait and a bounded shared read before rollout.

Measure c100/n100 creation time on equal-success runs. Check that query reduction also reduces the critical-path wait. Restore the original resolver if placement, update visibility or request deadlines regress.
