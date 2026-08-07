#!/usr/bin/env python3
import argparse
import gzip
import json
import os
import shutil
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PERFORMANCE_KEYWORDS = [
    "performance",
    "maximum performance",
    "max performance",
    "hpc",
    "high performance",
]

POWER_SAVE_KEYWORDS = [
    "powersave",
    "power save",
    "balanced",
    "energy efficient",
    "energy saving",
    "low power",
]


def read_text(path):
    try:
        return Path(path).read_text(errors="ignore")
    except OSError:
        return ""


def load_json(path):
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_invalid": str(exc)}


def backup_text(backup_dir, relative_path):
    if not backup_dir or not relative_path:
        return ""
    base = Path(backup_dir).resolve()
    path = (base / relative_path).resolve()
    if base not in path.parents and path != base:
        return ""
    return read_text(path)


def run_command(command, timeout):
    if isinstance(command, str):
        argv = shlex.split(command)
    else:
        argv = [str(part) for part in command]
    if not argv:
        return 1, "", "empty command"
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def detect_runtime_environment(backup_dir):
    cgroup_text = read_text("/proc/1/cgroup") or backup_text(backup_dir, "virtualization.txt")
    if Path("/.dockerenv").exists() or any(token in cgroup_text for token in ("docker", "containerd", "kubepods", "podman")):
        return "container"

    code, stdout, _ = run_command(["systemd-detect-virt"], timeout=5)
    virt = stdout.strip()
    if code == 0 and virt and virt != "none":
        return "vm"

    backup_env = backup_text(backup_dir, "environment-type.txt")
    if "ENV_TYPE=container" in backup_env:
        return "container"
    if "ENV_TYPE=vm" in backup_env:
        return "vm"
    if "ENV_TYPE=baremetal" in backup_env:
        return "baremetal"
    return "unknown"


def capability_hex():
    status = read_text("/proc/self/status")
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            return line.split(":", 1)[1].strip()
    return ""


def has_capability(bit):
    cap = capability_hex()
    if not cap:
        return False
    try:
        return bool(int(cap, 16) & (1 << bit))
    except ValueError:
        return False


def diagnose_perf_pmu(backup_dir, timeout):
    perf_path = shutil.which("perf")
    runtime = detect_runtime_environment(backup_dir)
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    perf_event_paranoid = read_text("/proc/sys/kernel/perf_event_paranoid").strip()
    kptr_restrict = read_text("/proc/sys/kernel/kptr_restrict").strip()
    cap_perfmon = has_capability(38)
    cap_sys_admin = has_capability(21)
    cap_sys_ptrace = has_capability(19)

    findings = []
    next_steps = []
    degraded_items = []
    blocked_items = []

    if not perf_path:
        return {
            "status": "failed",
            "perf_command_status": "missing",
            "perf_permission_status": "unknown",
            "runtime_environment": runtime,
            "perf_event_paranoid": perf_event_paranoid,
            "kptr_restrict": kptr_restrict,
            "pmu_event_status": "unknown",
            "perf_smoke_test_status": "not_run",
            "findings": ["perf command not found"],
            "next_steps": ["Install perf/linux-tools matching the running kernel, or collect PMU data on the host"],
            "degraded_items": [],
            "blocked_items": ["perf_missing"],
        }

    code, stdout, stderr = run_command([perf_path, "list"], timeout)
    perf_list_text = (stdout + stderr).lower()
    hardware_events = [event for event in ("cycles", "instructions", "cache-misses", "branches") if event in perf_list_text]
    if code != 0:
        pmu_event_status = "unknown"
        degraded_items.append("perf_list_failed")
        findings.append(f"perf list failed: {(stderr or stdout).strip()[:300]}")
    elif hardware_events:
        pmu_event_status = "hardware_events_listed"
    else:
        pmu_event_status = "no_hardware_events_listed"
        degraded_items.append("hardware_pmu_events_not_visible")
        findings.append("perf list did not expose common hardware PMU events")

    smoke_code, smoke_stdout, smoke_stderr = run_command(
        [perf_path, "stat", "-e", "cycles,instructions", "--", "true"],
        timeout,
    )
    smoke_text = (smoke_stdout + smoke_stderr).strip()
    if smoke_code == 0:
        perf_smoke_test_status = "passed"
    else:
        perf_smoke_test_status = "failed"
        findings.append(f"perf stat smoke test failed: {smoke_text[:500]}")

    if is_root or cap_perfmon or cap_sys_admin:
        perf_permission_status = "likely_sufficient"
    else:
        perf_permission_status = "non_root_limited"
        degraded_items.append("non_root_perf_permissions")
        next_steps.append("Use root or grant CAP_PERFMON/CAP_SYS_ADMIN as appropriate for the kernel")

    try:
        paranoid_value = int(perf_event_paranoid) if perf_event_paranoid else None
    except ValueError:
        paranoid_value = None
    if paranoid_value is not None and paranoid_value > 1 and not (is_root or cap_perfmon or cap_sys_admin):
        degraded_items.append("perf_event_paranoid_restrictive")
        next_steps.append("Lower kernel.perf_event_paranoid or collect as root/with CAP_PERFMON")

    if kptr_restrict and kptr_restrict != "0" and not is_root:
        degraded_items.append("kernel_symbols_restricted")
        next_steps.append("Use root or adjust kptr_restrict if kernel symbol resolution is required")

    if runtime == "container":
        degraded_items.append("container_perf_mapping_required")
        next_steps.append("For container targets, collect on the host or run with host pid/perf_event access and CAP_PERFMON/CAP_SYS_ADMIN")
    elif runtime == "vm" and pmu_event_status != "hardware_events_listed":
        degraded_items.append("vm_pmu_not_visible")
        next_steps.append("Enable virtual PMU/perf event passthrough in the hypervisor or collect on the host")

    if smoke_code != 0:
        if "permission" in smoke_text.lower() or "not permitted" in smoke_text.lower():
            blocked_items.append("perf_permission_denied")
        else:
            degraded_items.append("perf_smoke_failed")

    if blocked_items or (perf_smoke_test_status == "failed" and pmu_event_status == "no_hardware_events_listed"):
        status = "failed"
    elif degraded_items or perf_smoke_test_status == "failed" or pmu_event_status != "hardware_events_listed":
        status = "degraded"
    else:
        status = "passed"

    if status == "passed":
        findings.append("perf command, common hardware events, and minimal perf stat smoke test are available")

    return {
        "status": status,
        "perf_command_status": "present",
        "perf_permission_status": perf_permission_status,
        "runtime_environment": runtime,
        "is_root": is_root,
        "capabilities": {
            "CapEff": capability_hex(),
            "CAP_PERFMON": cap_perfmon,
            "CAP_SYS_ADMIN": cap_sys_admin,
            "CAP_SYS_PTRACE": cap_sys_ptrace,
        },
        "perf_event_paranoid": perf_event_paranoid,
        "kptr_restrict": kptr_restrict,
        "pmu_event_status": pmu_event_status,
        "hardware_events_seen": hardware_events,
        "perf_smoke_test_status": perf_smoke_test_status,
        "perf_smoke_test_output": smoke_text[:1000],
        "findings": findings,
        "next_steps": sorted(set(next_steps)),
        "degraded_items": sorted(set(degraded_items)),
        "blocked_items": sorted(set(blocked_items)),
    }


def sysctl_value(name):
    proc_path = Path("/proc/sys") / name.replace(".", "/")
    if proc_path.exists():
        return read_text(proc_path).strip()
    code, stdout, _ = run_command(["sysctl", "-n", name], timeout=5)
    if code == 0:
        return stdout.strip()
    return None


def kernel_release(backup_dir):
    code, stdout, _ = run_command(["uname", "-r"], timeout=5)
    if code == 0 and stdout.strip():
        return stdout.strip()
    text = backup_text(backup_dir, "os-kernel.txt")
    for line in text.splitlines():
        if "Linux" in line and "GNU/Linux" in line:
            parts = line.split()
            if len(parts) > 2:
                return parts[2]
    return ""


def read_kernel_config(backup_dir):
    release = kernel_release(backup_dir)
    candidates = []
    if release:
        candidates.append(Path("/boot") / f"config-{release}")
    candidates.extend([Path("/proc/config.gz"), Path("/boot/config")])

    for path in candidates:
        if not path.exists():
            continue
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", errors="ignore") as handle:
                    return handle.read()
            return path.read_text(errors="ignore")
        except OSError:
            continue

    text = backup_text(backup_dir, "os-kernel.txt")
    if "CONFIG_" in text:
        return text
    return ""


def check_one(check, backup_dir, timeout):
    check_type = check.get("type", "")
    expected = str(check.get("expected", ""))

    if check_type == "file_equals":
        actual = read_text(check.get("path", "")).strip()
        return actual == expected, actual, ""
    if check_type == "file_contains":
        actual = read_text(check.get("path", ""))
        return expected in actual, actual[:500], ""
    if check_type == "file_exists":
        path = Path(check.get("path", ""))
        return path.exists(), str(path), ""
    if check_type == "sysctl_equals":
        actual = sysctl_value(check.get("name", ""))
        return actual == expected, actual, ""
    if check_type == "command_contains":
        code, stdout, stderr = run_command(check.get("command", []), timeout)
        actual = stdout + stderr
        return code == 0 and expected in actual, actual[:500], stderr
    if check_type == "backup_file_contains":
        actual = backup_text(backup_dir, check.get("path", ""))
        return expected in actual, actual[:500], ""
    if check_type == "backup_file_not_contains":
        actual = backup_text(backup_dir, check.get("path", ""))
        return expected not in actual, actual[:500], ""
    if check_type == "uname_contains":
        actual = kernel_release(backup_dir)
        return expected in actual, actual, ""
    if check_type == "os_release_contains":
        actual = read_text("/etc/os-release") or backup_text(backup_dir, "os-kernel.txt")
        return expected in actual, actual[:500], ""
    if check_type in ("kernel_config_enabled", "kernel_config_equals"):
        config = read_kernel_config(backup_dir)
        name = check.get("name", "")
        value = check.get("value", "y") if check_type == "kernel_config_equals" else "y"
        needle = f"{name}={value}"
        return needle in config, needle if config else "kernel config unavailable", ""

    return None, "", f"unsupported check type: {check_type}"


def diagnose_reference_issues(path, backup_dir, timeout):
    data = load_json(path)
    if data is None:
        return {
            "status": "not_present",
            "checks": [],
            "findings": [],
            "degraded_items": [],
            "blocked_items": [],
        }
    if isinstance(data, dict) and data.get("_invalid"):
        return {
            "status": "invalid",
            "checks": [],
            "findings": [f"invalid reference issue set: {data['_invalid']}"],
            "degraded_items": ["reference_issue_set_parse_failed"],
            "blocked_items": [],
        }

    checks = []
    failed = False
    degraded = False
    for issue in data.get("issues", []):
        ok, actual, error = check_one(issue.get("check", {}), backup_dir, timeout)
        if ok is None:
            status = "unknown"
            degraded = True
        elif ok:
            status = "passed"
        else:
            status = "failed"
            failed = True
        checks.append({
            "id": issue.get("id", ""),
            "description": issue.get("description", ""),
            "severity": issue.get("severity", ""),
            "status": status,
            "actual": actual,
            "error": error,
        })

    return {
        "status": "failed" if failed else "degraded" if degraded else "passed",
        "checks": checks,
        "findings": [item for item in checks if item["status"] in ("failed", "unknown")],
        "degraded_items": [item["id"] for item in checks if item["status"] == "unknown"],
        "blocked_items": [item["id"] for item in checks if item["status"] == "failed" and item.get("severity") == "high"],
    }


def diagnose_bios(backup_dir):
    bios_text = "\n".join([
        backup_text(backup_dir, "bios-info.txt"),
        backup_text(backup_dir, "cpu-info.txt"),
    ]).lower()

    if not bios_text.strip():
        return {
            "status": "degraded",
            "findings": ["BIOS evidence not available in environment backup"],
            "next_steps": ["Collect BMC/Redfish BIOS attributes or BIOS Setup screenshots"],
        }

    performance_hits = [word for word in PERFORMANCE_KEYWORDS if word in bios_text]
    power_save_hits = [word for word in POWER_SAVE_KEYWORDS if word in bios_text]

    if performance_hits and not power_save_hits:
        status = "passed"
        findings = [f"performance-oriented keyword(s) found: {', '.join(performance_hits)}"]
        next_steps = []
    elif power_save_hits:
        status = "failed"
        findings = [f"power-saving/balanced keyword(s) found: {', '.join(power_save_hits)}"]
        next_steps = ["Confirm BIOS Power Profile, C-State, frequency policy and NUMA settings"]
    else:
        status = "degraded"
        findings = ["BIOS version/platform evidence exists, but performance policy fields are not visible"]
        next_steps = ["Collect BMC/Redfish BIOS attributes: Power Profile, C-State, frequency policy, NUMA/Node Interleaving"]

    return {
        "status": status,
        "findings": findings,
        "next_steps": next_steps,
    }


def diagnose_kernel_patches(path, backup_dir, timeout):
    data = load_json(path)
    if data is None:
        return {
            "status": "not_applicable_or_unknown",
            "checks": [],
            "findings": ["kernel patch manifest not provided"],
            "degraded_items": ["kernel_patch_manifest_missing"],
            "blocked_items": [],
        }
    if isinstance(data, dict) and data.get("_invalid"):
        return {
            "status": "invalid",
            "checks": [],
            "findings": [f"invalid kernel patch manifest: {data['_invalid']}"],
            "degraded_items": ["kernel_patch_manifest_parse_failed"],
            "blocked_items": [],
        }

    checks = []
    failed = False
    degraded = False
    for patch in data.get("patches", []):
        patch_results = []
        patch_failed = False
        patch_unknown = False
        for check in patch.get("checks", []):
            ok, actual, error = check_one(check, backup_dir, timeout)
            if ok is None:
                status = "unknown"
                patch_unknown = True
            elif ok:
                status = "passed"
            else:
                status = "failed"
                patch_failed = True
            patch_results.append({
                "type": check.get("type", ""),
                "status": status,
                "actual": actual,
                "error": error,
            })
        status = "failed" if patch_failed else "unknown" if patch_unknown else "passed"
        failed = failed or patch_failed
        degraded = degraded or patch_unknown
        checks.append({
            "id": patch.get("id", ""),
            "description": patch.get("description", ""),
            "severity": patch.get("severity", ""),
            "status": status,
            "checks": patch_results,
        })

    return {
        "status": "failed" if failed else "degraded" if degraded else "passed",
        "checks": checks,
        "findings": [item for item in checks if item["status"] in ("failed", "unknown")],
        "degraded_items": [item["id"] for item in checks if item["status"] == "unknown"],
        "blocked_items": [item["id"] for item in checks if item["status"] == "failed" and item.get("severity") == "high"],
    }


def main():
    parser = argparse.ArgumentParser(description="Diagnose environment after backup and before service health checks.")
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--reference-issues", default="")
    parser.add_argument("--kernel-patch-manifest", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir)
    evidence_paths = [str(backup_dir)]
    reference = diagnose_reference_issues(args.reference_issues, backup_dir, args.timeout)
    bios = diagnose_bios(backup_dir)
    perf_pmu = diagnose_perf_pmu(backup_dir, args.timeout)
    kernel = diagnose_kernel_patches(args.kernel_patch_manifest, backup_dir, args.timeout)

    blocked_items = []
    degraded_items = []
    findings = []
    for category, result in (
        ("reference_issues", reference),
        ("bios_performance", bios),
        ("perf_pmu", perf_pmu),
        ("kernel_patches", kernel),
    ):
        findings.append({"category": category, "result": result})
        blocked_items.extend(f"{category}:{item}" for item in result.get("blocked_items", []))
        degraded_items.extend(f"{category}:{item}" for item in result.get("degraded_items", []))
        if result.get("status") in ("degraded", "invalid", "not_applicable_or_unknown"):
            degraded_items.append(category)

    if (
        blocked_items
        or reference["status"] == "failed"
        or bios["status"] == "failed"
        or perf_pmu["status"] == "failed"
        or kernel["status"] == "failed"
    ):
        status = "failed"
    elif degraded_items or bios["status"] in ("degraded", "unknown") or perf_pmu["status"] == "degraded":
        status = "degraded"
    else:
        status = "passed"

    diagnosis = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reference_issue_set_status": reference["status"],
        "bios_performance_status": bios["status"],
        "perf_pmu_status": perf_pmu["status"],
        "kernel_patch_status": kernel["status"],
        "reference_issue_checks": reference.get("checks", []),
        "bios_performance_findings": bios.get("findings", []),
        "perf_pmu_checks": {
            "perf_command_status": perf_pmu.get("perf_command_status", ""),
            "perf_permission_status": perf_pmu.get("perf_permission_status", ""),
            "runtime_environment": perf_pmu.get("runtime_environment", ""),
            "perf_event_paranoid": perf_pmu.get("perf_event_paranoid", ""),
            "kptr_restrict": perf_pmu.get("kptr_restrict", ""),
            "pmu_event_status": perf_pmu.get("pmu_event_status", ""),
            "perf_smoke_test_status": perf_pmu.get("perf_smoke_test_status", ""),
            "hardware_events_seen": perf_pmu.get("hardware_events_seen", []),
            "capabilities": perf_pmu.get("capabilities", {}),
        },
        "perf_pmu_findings": perf_pmu.get("findings", []),
        "kernel_patch_checks": kernel.get("checks", []),
        "findings": findings,
        "blocked_items": blocked_items,
        "degraded_items": degraded_items,
        "evidence_paths": evidence_paths,
        "next_steps": sorted(set(bios.get("next_steps", []) + perf_pmu.get("next_steps", []))),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(diagnosis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"environment_diagnosis_path": str(output), "status": status}, ensure_ascii=False))
    if blocked_items:
        sys.exit(2)


if __name__ == "__main__":
    main()
