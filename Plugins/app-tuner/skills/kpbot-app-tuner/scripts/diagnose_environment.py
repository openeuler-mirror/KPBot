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


def diagnose_npu_device(backup_dir):
    """NPU 设备诊断：发现、驱动、CANN、torch_npu、HBM、NUMA 拓扑。

    针对华为 Ascend NPU（Ascend910/920/930/950 系列）采集：
      1. npu-smi info 设备发现与健康检查
      2. CANN 版本（/usr/local/Ascend/ascend-toolkit/latest/version.cfg）
      3. torch_npu 版本（pip show torch_npu）
      4. NPU-NUMA 拓扑（lspci -d 19e5: -v | grep NUMA）
      5. HBM 使用率（npu-smi info -t usages -i 0）

    任何外部命令缺失都降级为 issues 中的提示信息，不抛出异常。
    """
    result = {
        "npu_available": False,
        "npu_devices": [],
        "device_count": 0,
        "cann_version": None,
        "torch_npu_version": None,
        "npu_numa_topology": {},
        "hbm_status": {},
        "issues": [],
    }

    # 1. NPU 设备发现 + 健康检查
    npu_smi_path = shutil.which("npu-smi")
    if not npu_smi_path:
        result["issues"].append("npu-smi not found, NPU diagnostics skipped")
        result["status"] = "not_present"
        return result
    try:
        code, stdout, stderr = run_command([npu_smi_path, "info"], timeout=15)
        if code != 0:
            result["issues"].append(
                f"npu-smi info failed (rc={code}): {(stderr or stdout).strip()[:300]}"
            )
            result["status"] = "failed"
            return result
        result["npu_available"] = True
        # 解析 npu-smi info 表格中的设备条目
        # 实际输出格式（Ascend 26.0.rc1）：
        #   | 0     Ascend910           | OK            | ... |
        #   | 0     0                   | 0000:9D:00.0  | ... |
        # 注意：NPU ID 和 Name 在同一 cell 内用多空格分隔（split("|") 后 cells[0]="0     Ascend910"）
        # 芯片行格式：| ChipID  PhyID | Bus-Id | AICore(%) | ... |
        # 需要将 cells[0] 按空格拆分，第一个 token 是 ID，其余是 Name
        devices = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            # 跳过表头/分隔行
            if "Health" in cells[0] or cells[0].lower().startswith("chip") or cells[0].lower().startswith("npu"):
                continue
            # cells[0] 可能是 "0     Ascend910" 或纯数字 "0"
            # 按空格拆分，第一个 token 尝试作为 device_id
            first_cell_tokens = cells[0].split()
            if not first_cell_tokens:
                continue
            try:
                device_id = int(first_cell_tokens[0])
            except ValueError:
                continue
            # 跳过芯片行：芯片行的 cells[1] 是 Bus-Id（含冒号和点），不是健康状态
            # 设备行：cells[1] 是 "OK"/"Warning"/"Critical" 等健康状态
            # 芯片行：cells[1] 是 "0000:9D:00.0" 格式
            health = cells[1] if len(cells) > 1 else ""
            if ":" in health and "." in health:
                # 这是芯片行（Bus-Id 格式），跳过
                continue
            name = " ".join(first_cell_tokens[1:]) if len(first_cell_tokens) > 1 else ""
            devices.append({
                "id": device_id,
                "name": name,
                "health": health,
            })
        if devices:
            # 按 id 去重
            seen_ids = set()
            unique_devices = []
            for dev in devices:
                if dev["id"] in seen_ids:
                    continue
                seen_ids.add(dev["id"])
                unique_devices.append(dev)
            result["npu_devices"] = unique_devices
            result["device_count"] = len(unique_devices)
        else:
            result["issues"].append("npu-smi info succeeded but no devices parsed")
    except FileNotFoundError:
        result["issues"].append("npu-smi not found, NPU diagnostics skipped")
        result["status"] = "not_present"
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result["issues"].append(f"npu-smi discovery raised: {exc}")
        result["status"] = "failed"
        return result

    # 2. CANN 版本
    cann_version_path = "/usr/local/Ascend/ascend-toolkit/latest/version.cfg"
    try:
        if Path(cann_version_path).exists():
            cann_text = read_text(cann_version_path).strip()
            if cann_text:
                result["cann_version"] = cann_text
            else:
                result["issues"].append("CANN version.cfg is empty")
        else:
            # 兜底：尝试 backup 目录或环境变量
            cann_env = os.environ.get("ASCEND_TOOLKIT_VERSION", "")
            if cann_env:
                result["cann_version"] = cann_env
            else:
                result["issues"].append(
                    "CANN version.cfg not found at default path and ASCEND_TOOLKIT_VERSION unset"
                )
    except Exception as exc:  # pragma: no cover - defensive
        result["issues"].append(f"CANN version read failed: {exc}")

    # 3. torch_npu 版本
    try:
        code, stdout, stderr = run_command(["pip", "show", "torch_npu"], timeout=15)
        if code == 0:
            version = None
            location = None
            for line in stdout.splitlines():
                if line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
                elif line.startswith("Location:"):
                    location = line.split(":", 1)[1].strip()
            if version:
                result["torch_npu_version"] = version
                if location:
                    result.setdefault("torch_npu_location", location)
            else:
                result["issues"].append("torch_npu installed but Version field missing")
        else:
            # pip 可能不在 PATH，尝试 pip3
            code3, stdout3, _ = run_command(["pip3", "show", "torch_npu"], timeout=15)
            if code3 == 0:
                for line in stdout3.splitlines():
                    if line.startswith("Version:"):
                        result["torch_npu_version"] = line.split(":", 1)[1].strip()
                        break
            else:
                result["issues"].append("torch_npu not installed or pip unavailable")
    except FileNotFoundError:
        result["issues"].append("pip not found; torch_npu version check skipped")
    except Exception as exc:  # pragma: no cover - defensive
        result["issues"].append(f"torch_npu version check failed: {exc}")

    # 4. NPU-NUMA 拓扑
    try:
        code, stdout, stderr = run_command(
            ["lspci", "-d", "19e5:", "-v"], timeout=15
        )
        if code == 0 and stdout:
            topology = {}
            current_device = None
            for line in stdout.splitlines():
                stripped = line.strip()
                # 设备块以设备地址行开始，如 "08:00.0 ..."
                if stripped and not line.startswith("\t") and not line.startswith(" "):
                    # 新设备块
                    header = stripped.split()[0] if stripped.split() else None
                    if header and ":" in header:
                        current_device = header
                        topology.setdefault(current_device, {})
                elif "NUMA node:" in stripped and current_device:
                    # 形如 "NUMA node: 0"
                    numa_value = stripped.split("NUMA node:", 1)[1].strip()
                    topology[current_device]["numa_node"] = numa_value
                elif "Subsystem:" in stripped and current_device:
                    topology[current_device]["subsystem"] = stripped.split(
                        "Subsystem:", 1
                    )[1].strip()
            if topology:
                result["npu_numa_topology"] = topology
            else:
                result["issues"].append("lspci succeeded but no NPU NUMA bindings parsed")
        else:
            result["issues"].append(
                f"lspci NPU topology unavailable (rc={code}): {(stderr or '').strip()[:200]}"
            )
    except FileNotFoundError:
        result["issues"].append("lspci not found; NPU-NUMA topology skipped")
    except Exception as exc:  # pragma: no cover - defensive
        result["issues"].append(f"NPU-NUMA topology check failed: {exc}")

    # 5. HBM 状态（首张卡）
    try:
        code, stdout, stderr = run_command(
            ["npu-smi", "info", "-t", "usages", "-i", "0"], timeout=15
        )
        if code == 0 and stdout:
            hbm = {}
            for line in stdout.splitlines():
                stripped = line.strip()
                if "HBM" in stripped and ":" in stripped:
                    # 形如 "HBM Usage : 13.4 GB / 32.0 GB (41.8%)"
                    try:
                        label, value = stripped.split(":", 1)
                        hbm[label.strip().lower().replace(" ", "_")] = value.strip()
                    except ValueError:
                        continue
                elif stripped and "GB" in stripped and "%" in stripped:
                    # 兜底匹配
                    hbm["raw_line"] = stripped
            if hbm:
                result["hbm_status"] = hbm
            else:
                # fallback：用 npu-smi info 主表中的 HBM 数字
                for dev in result["npu_devices"]:
                    # npu-smi info 主表已解析到设备，但 HBM 字段未单独提取
                    pass
                result["issues"].append("HBM usages command succeeded but no usage parsed")
        else:
            result["issues"].append(
                f"npu-smi usages failed (rc={code}): {(stderr or '').strip()[:200]}"
            )
    except FileNotFoundError:
        result["issues"].append("npu-smi unavailable for HBM check")
    except Exception as exc:  # pragma: no cover - defensive
        result["issues"].append(f"HBM status check failed: {exc}")

    # 派生状态
    if result["npu_available"] and not result["npu_devices"]:
        result["status"] = "degraded"
    elif result["issues"]:
        # 存在 issue 但设备可用：降级
        result["status"] = "degraded" if result["npu_available"] else "failed"
    else:
        result["status"] = "passed" if result["npu_available"] else "not_present"

    return result


def diagnose_arm_pmu():
    """ARM PMU 事件可用性检查。

    检查 ARM 平台常见 PMU 事件是否可被 perf list 枚举，
    以及 /sys/bus/event_source/devices/ 下是否存在 ARM PMU 节点。
    """
    arm_events = ["L1I_CACHE_REFILL", "LLC_CACHE_MISS", "BR_INDIRECT"]
    result = {
        "platform": "arm",
        "events": {},
        "available_count": 0,
        "issues": [],
    }

    # 通过 perf list 检查
    perf_path = shutil.which("perf")
    if not perf_path:
        result["issues"].append("perf not found; ARM PMU event enumeration skipped")
        result["status"] = "degraded"
        return result

    code, stdout, stderr = run_command([perf_path, "list"], timeout=10)
    combined = (stdout + stderr).lower() if code == 0 or stdout else ""
    available = 0
    for event in arm_events:
        present = event.lower() in combined
        result["events"][event] = "present" if present else "missing"
        if present:
            available += 1
    result["available_count"] = available

    # /sys 兜底
    arm_pmu_devices = []
    try:
        devices_root = Path("/sys/bus/event_source/devices")
        if devices_root.exists():
            for entry in devices_root.iterdir():
                name = entry.name.lower()
                if "arm" in name or "pmu" in name or "cortex" in name or "neoverse" in name:
                    arm_pmu_devices.append(entry.name)
    except OSError as exc:
        result["issues"].append(f"failed to enumerate /sys PMU devices: {exc}")
    result["arm_pmu_devices"] = arm_pmu_devices

    if available == len(arm_events):
        result["status"] = "passed"
    elif available > 0:
        result["status"] = "degraded"
        result["issues"].append(
            f"only {available}/{len(arm_events)} ARM PMU events visible via perf list"
        )
    else:
        result["status"] = "failed"
        result["issues"].append("none of the key ARM PMU events are visible via perf list")

    return result


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
    npu_device = diagnose_npu_device(backup_dir)
    arm_pmu = diagnose_arm_pmu()

    blocked_items = []
    degraded_items = []
    findings = []
    for category, result in (
        ("reference_issues", reference),
        ("bios_performance", bios),
        ("perf_pmu", perf_pmu),
        ("kernel_patches", kernel),
        ("npu_device", npu_device),
        ("arm_pmu", arm_pmu),
    ):
        findings.append({"category": category, "result": result})
        blocked_items.extend(f"{category}:{item}" for item in result.get("blocked_items", []))
        degraded_items.extend(f"{category}:{item}" for item in result.get("degraded_items", []))
        if result.get("status") in ("degraded", "invalid", "not_applicable_or_unknown"):
            degraded_items.append(category)

    # NPU 设备相关的 issue 也作为降级项汇总
    for issue in npu_device.get("issues", []):
        degraded_items.append(f"npu_device:{issue[:80]}")
    for issue in arm_pmu.get("issues", []):
        degraded_items.append(f"arm_pmu:{issue[:80]}")

    if (
        blocked_items
        or reference["status"] == "failed"
        or bios["status"] == "failed"
        or perf_pmu["status"] == "failed"
        or kernel["status"] == "failed"
        or npu_device.get("status") == "failed"
        or arm_pmu.get("status") == "failed"
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
        "npu_device_status": npu_device.get("status", "not_present"),
        "arm_pmu_status": arm_pmu.get("status", "degraded"),
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
        "npu_device": npu_device,
        "arm_pmu": arm_pmu,
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
