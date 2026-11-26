# Reproduce in-flight read sharing

From the skill directory, run the following with Go 1.20 or later. The standalone Go module uses the standard library.

```sh
go -C cases/concurrent-reads test -count=1 -timeout=30s -v .
```

The case contains the READY list resolver and generation cache from the CubeSandbox v0.7.1 Scheme 2 reference implementation. The fetch group includes a test-only notification when a follower joins. A fake metadata store counts reads and supplies barriers. The fixture also models a request-time health predicate. Target versions may implement different placement rules.

- **Same key and generation.** Block the first list read. Start a second request for the same template with another preferred node. Observe it join before releasing the first read, and complete an unrelated template request while both wait. Expect one list read per template and the requested node for each caller. No completed value exists when the follower joins.
- **Write during a read.** Hold a read after it captures the old rows, update the fixture and invalidate the template. Resolve the new generation before releasing the old one. Both callers must eventually return the new node, with one read per generation.
- **Leader cancellation.** Cancel the leader after a live follower joins its blocked read. The leader receives its own cancellation error and the follower obtains its preferred node from a new read.
- **Shared database error.** The blocked read captures an injected error. After the follower joins, configure subsequent reads to succeed and release the first read. Both callers must receive the original error from that one read, and the failed generation must be gone before retry. The next request for the same template must issue a second database read and return its preferred node.

The consistency test checks SHA-256 pins for the resolver and cache fixtures and both Scheme 2 patch copies in a KPBot checkout. For a standalone export, set `SCHEME2_REFERENCE_PATCH` to an absolute patch path to check that revision as well. When the reference changes, apply it to the pinned v0.7.1 source, compare the two resolver declarations and the generated cache file, then refresh the three digests after review.

## Validate the deployment

Use one CubeMaster process and a fixed template and placement policy. Record baseline and candidate database read counts per process, join counts, errors, and successful creates. Keep completed-result TTL configuration equal across arms and start each burst with cold metadata. Exercise an unrelated template during the burst and a committed replica update while a read is outstanding.

Repeat the shared-error and retry sequence against the target database adapter. In a separate run, cancel a follower while the leader remains blocked and record when each caller returns relative to its deadline. The reference group's wait on the leader is synchronous. A target requiring prompt follower cancellation needs a context-aware wait and a bounded shared read before rollout.

Measure c100/n100 creation time on equal-success runs. Check that query reduction also reduces the critical-path wait. Restore the original resolver if placement, update visibility or request deadlines regress.
