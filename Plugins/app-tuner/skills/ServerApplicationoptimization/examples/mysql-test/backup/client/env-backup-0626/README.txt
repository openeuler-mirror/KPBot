Environment backup artifacts collected by sequential read-only command execution.

The collector runs one command at a time and records:
- the command that was attempted
- whether it succeeded, failed, or was skipped
- the stdout/stderr captured for each target file

Collection groups:
- BIOS configuration: bios-info.txt, bios-redfish.txt, bios-redfish-*.json
- Hardware configuration: hardware-cpu.txt, hardware-memory.txt, hardware-disk.txt, hardware-nic.txt
- Software configuration: software-versions.txt, os-config.txt, kernel-config.txt, build-system.txt, perf-diagnosis.txt
- Runtime context: virtualization.txt, environment-type.txt, container-limits.txt
- Compatibility files: cpu-info.txt, numa-topology.txt, memory-info.txt, disk-info.txt, nic-info.txt, os-kernel.txt,
  compiler-runtime.txt, thp-status.txt, hugepages-status.txt
- Summary: environment-backup-report.html
- Audit trail: optimization-timeline.txt, command-manifest.txt
