# Validate a candidate on the target

Use a disposable snapshot restore environment. Record the shim and guest revisions, transport version, snapshot identity, guest configuration, CPU allocation, concurrency and cleanup protocol. The pinned v0.7.1 caller establishes the serial order. Handler independence and performance require target measurements.

## Trace a successful restore

Assign each attempt a restore generation. Trace entry and exit of each guest handler with one guest monotonic clock, plus dispatch, acknowledgement, reset completion and CreateSandbox submission with one host monotonic clock. Correlate records by generation and RPC ID. Avoid recording RNG bytes. Compare timestamps only within their respective clock domains.

For one successful generation, compute guest overlap as `max(0, min(clock_end, rng_end) - max(clock_start, rng_start))`. Measure host reset duration from reset entry through acknowledgement of both calls. Verify CreateSandbox dispatch follows both acknowledgements. Check postconditions using the clock tolerance and reseeding success criteria already used by the deployment.

In an isolated guest test double, block each handler at entry until both have arrived, with a bounded test timeout. Release clock first, then repeat with RNG first. Both runs must reach create exactly once. Repeat on the real transport without the artificial delay to measure actual overlap. Reaching the test barrier alone provides no performance figure.

## Exercise failures

| Injection | Required observation |
|---|---|
| Host RNG generation fails | No candidate RPC is submitted and create reports failure. |
| Clock fails while RNG is in flight, then reverse the roles | No CreateSandbox submission. Record each branch outcome and confirm cleanup disposes of that generation even when its sibling succeeds. |
| Cancel after dispatch or expire the request with one reply outstanding | No READY publication. A late remote completion cannot modify the guest used for the next attempt. |
| Disconnect the guest during the pair | Return a transport error within the request budget and verify a fresh generation can restore successfully. |

## Measure and roll back

Compare the serial and opt-in paths in alternating order on the same binary and snapshot. First run at concurrency one to attribute latency, then run the representative 100-create batch. Record successful creates, errors, reset duration, guest overlap, creation p95 and batch `total_time`. Track and clean up both warmup and measured instances outside the timing window. Compare equal-success batches and report variance across repeats.

Reject the candidate if failure isolation fails, workload traces show serialization, or end-to-end measurements do not justify the change. Disable the candidate, repeat the serial trace, and confirm reseeding starts only after the clock RPC succeeds.
