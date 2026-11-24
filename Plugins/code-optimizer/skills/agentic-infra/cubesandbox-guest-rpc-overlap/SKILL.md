---
name: cubesandbox-guest-rpc-overlap
description: Evaluate whether CubeSandbox snapshot restores can overlap the SetGuestDateTime and ReseedRandomDev guest RPCs. Use when both waits occur on the create critical path after connection establishment and the target can trace guest handler execution and isolate failed restores.
---

# Evaluate guest RPC overlap during snapshot restore

1. Locate `reset_guest` and its caller in the [v0.7.1 shim source](https://github.com/tencentcloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeShim/shim/src/sandbox/sb.rs). The baseline awaits the clock update, obtains host RNG data, then awaits reseeding. Sandbox create requests restoring a snapshot reach device setup and the guest create RPC only after reset succeeds. Confirm these boundaries on the target revision.
2. Inspect both guest handlers and the transport. Establish whether reseeding reads the clock, whether time changes affect deadlines, and whether the client and server permit multiple outstanding calls on this connection. Record lock scopes on both sides. Separate client futures can still serialize at dispatch.
3. Check the failure contract before changing the order. The serial path skips RNG collection and reseeding when the clock call fails. Concurrent submission permits either side effect to complete first. Use the serial path if a partially configured guest could be reused or if the target requires that failure ordering. Confirm the target can fence and dispose of a failed restore generation before retry.
4. For an eligible target, prototype an opt-in branch inside `reset_guest`. Prepare RNG input and sample the wall clock immediately before dispatch. Drive both RPC futures within the request under its deadline. Keep their results separately and mark reset successful only after both succeed. A local timeout or dropped future can leave remote work running. Fence the generation and finish cleanup on errors or cancellation. Keep device setup, storage selection and CreateSandbox behind the success barrier.
5. Follow the [validation runbook](references/runbook.md) to compare serial and concurrent executions. A shorter RPC phase and a lower end-to-end create time are separate measurements.
