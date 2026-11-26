#!/usr/bin/env python3
"""Preflight, prepare and run the complete CubeSandbox lifecycle benchmark."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

from lib_cubesandbox import (
    BenchError,
    CubeAPI,
    PROXY_VARIABLES,
    PYTHON_SCRIPTS,
    SECTION_ORDER,
    SECTION_TITLES,
    available_memory_mb,
    capture,
    cubelet_network_config,
    host_lock,
    is_cube_or_system_process,
    is_root,
    list_ip_interfaces,
    list_taps,
    load_plan,
    log,
    numa_topology,
    plan_digest,
    plan_summary,
    process_cpus,
    process_inventory,
    python_imports,
    repo_commit,
    resolve_template,
    rewrite_numa_launches,
    sanitized_environment,
    script_bindings,
    select_sections,
    service_is_active,
    service_main_pid,
    tap_cleanup_candidates,
    tap_evidence_digest,
    write_json,
)

DEFAULT_PLAN = Path(__file__).resolve().parent.parent / "assets" / "default-plan.json"
DEFAULT_TOOLBOX = Path("/usr/local/services/cubetoolbox")
SERVICES = (
    "cube-sandbox-cubemaster.service",
    "cube-sandbox-cube-api.service",
    "cube-sandbox-cubelet.service",
    "cube-sandbox-cube-templatecenter.service",
    "cube-sandbox-cube-proxy.service",
)
SMOKE_CODE = """
from cubesandbox import Sandbox
import os
sb = Sandbox.create(template=os.environ['CUBE_TEMPLATE_ID'])
try:
    result = sb.run_code(\"print('Hello from CubeSandbox preflight')\")
    print(result)
    error = getattr(result, 'error', None)
    if error:
        raise RuntimeError(f'guest run_code failed: {error}')
    print('SMOKE_OK')
finally:
    sb.kill()
"""


class Checks:
    def __init__(self):
        self.items = []
        self.context = {}

    def add(self, name: str, status: str, detail: str):
        self.items.append({"name": name, "status": status, "detail": detail})

    def pass_(self, name: str, detail: str):
        self.add(name, "PASS", detail)

    def warn(self, name: str, detail: str):
        self.add(name, "WARN", detail)

    def fail(self, name: str, detail: str):
        self.add(name, "FAIL", detail)

    @property
    def ok(self) -> bool:
        return not any(item["status"] == "FAIL" for item in self.items)

    def payload(self) -> dict:
        return {
            "ok": self.ok,
            "context": self.context,
            "checks": self.items,
            "failures": sum(item["status"] == "FAIL" for item in self.items),
            "warnings": sum(item["status"] == "WARN" for item in self.items),
        }

    def render(self) -> str:
        lines = []
        for item in self.items:
            lines.append(f"{item['status']:<4} {item['name']}: {item['detail']}")
        lines.append(
            f"预检结果：{'PASS' if self.ok else 'FAIL'}；"
            f"FAIL={self.payload()['failures']}，WARN={self.payload()['warnings']}"
        )
        return "\n".join(lines)


def approval_payload(args, plan: dict, sections: list[str]) -> dict:
    """Bind user approval to every execution- and side-effect-relevant input."""
    operation = (
        args.command
        if args.command in ("run", "prepare")
        else getattr(args, "operation", "run")
    )
    preparing = bool(getattr(args, "prepare", False)) or operation == "prepare"
    return {
        "approval_schema": 1,
        "operation": operation,
        "plan": plan,
        "sections": sections,
        "prepare": preparing,
        "repo": str(args.repo),
        "toolbox": str(args.toolbox),
        "api_url": args.api_url.rstrip("/"),
        "template_id": args.template_id,
        "numa_node": args.numa_node,
        "tap_target": args.tap_target,
        "tap_evidence_sha256": args.tap_evidence,
        "cubelet_wait": args.cubelet_wait,
        "busy_threshold": args.busy_threshold,
        "python": args.python,
    }


def approval_digest(args, plan: dict, sections: list[str]) -> str:
    return plan_digest(approval_payload(args, plan, sections))


def approval_summary(args, plan: dict, sections: list[str]) -> str:
    scope = approval_payload(args, plan, sections)
    environment = {
        key: value for key, value in scope.items() if key not in ("plan", "approval_schema")
    }
    return (
        "批准范围（任何一项变化都必须重新确认）：\n"
        + json.dumps(environment, ensure_ascii=False, indent=2)
        + f"\nAPPROVAL_SHA256：{approval_digest(args, plan, sections)}"
    )


def require_confirmation(args, plan: dict, sections: list[str]):
    if not args.yes:
        raise BenchError("正式操作需要 --yes；Agent 只能在用户明确确认范围与副作用后传入")
    if not args.exclusive_confirmed:
        raise BenchError("正式操作需要 --exclusive-confirmed；请由用户确认独占窗口且其他任务已停止")
    if not args.template_id:
        raise BenchError("正式操作必须显式指定已确认的 --template-id，不能使用运行时才解析的模板")
    if args.numa_node is None:
        raise BenchError("正式操作必须显式指定已确认的 --numa-node，避免运行期间 NUMA 目标漂移")
    preparing = bool(getattr(args, "prepare", False)) or args.command == "prepare"
    if preparing and not re.fullmatch(r"[0-9a-f]{64}", args.tap_evidence):
        raise BenchError(
            "prepare 需要 --tap-evidence <check输出的TAP_EVIDENCE_SHA256>；"
            "它绑定精确的超额 (ifindex,name) 集合"
        )
    expected = approval_digest(args, plan, sections)
    if args.approved_plan_hash != expected:
        raise BenchError(
            f"批准哈希不匹配。当前最终执行范围为 {expected}；"
            "请用相同参数运行 show-plan 并向用户重新展示"
        )


def effective_numa_node(args, checks: Checks, topology: dict[int, set[int]]) -> int:
    if args.numa_node is not None:
        return args.numa_node
    master = script_bindings(
        args.toolbox / "scripts/systemd/cubemaster-start.sh", "CUBEMASTER_BIN"
    )
    cubelet = script_bindings(
        args.toolbox / "scripts/systemd/cubelet-start.sh", "CUBELET_BIN"
    )
    bindings = {node for node in master + cubelet if node >= 0}
    if len(bindings) == 1:
        return bindings.pop()
    if len(topology) > 1:
        checks.fail(
            "numa.selection",
            "两个启动脚本没有一致绑定；多 NUMA 机器必须由用户确认 --numa-node，暂按 0 展示其余检查",
        )
    else:
        checks.warn("numa.selection", "未检测到一致绑定；单 NUMA 暂按 node0 预检")
    return 0


def run_preflight(args, env: dict, template_id: str, sections: list[str]) -> Checks:
    checks = Checks()
    checks.context.update(
        {
            "repo": str(args.repo),
            "api_url": args.api_url,
            "template_id": template_id,
            "tap_target": args.tap_target,
            "sections": sections,
        }
    )
    if is_root():
        checks.pass_("privilege", "当前为 root，可执行后续准备操作")
    else:
        checks.fail("privilege", "需要 root；只读采集也应在部署主机上执行")

    repo = args.repo
    bench_dir = repo / "examples/cube-bench"
    snapshot_dir = repo / "examples/snapshot-rollback-clone"
    if bench_dir.is_dir() and snapshot_dir.is_dir():
        checks.pass_("source", f"官方测试源码齐全：{repo}")
        checks.context["repo_commit"] = repo_commit(repo)
    else:
        checks.fail(
            "source",
            f"{repo} 不是完整 CubeSandbox 源码树，缺少 examples/cube-bench 或 snapshot-rollback-clone",
        )

    needs_cube_bench = any(name in sections for name in ("template_create", "density"))
    cube_bench = bench_dir / "bin/cube-bench"
    if needs_cube_bench:
        if cube_bench.is_file() and os.access(cube_bench, os.X_OK):
            checks.pass_("cube-bench", str(cube_bench))
        else:
            checks.fail("cube-bench", f"{cube_bench} 不可执行；用 run --prepare 自动 make，或手工构建")
    missing_scripts = [
        PYTHON_SCRIPTS[name]
        for name in sections
        if name in PYTHON_SCRIPTS and not (snapshot_dir / PYTHON_SCRIPTS[name]).is_file()
    ]
    if missing_scripts:
        checks.fail("snapshot.scripts", "缺少官方脚本：" + ", ".join(missing_scripts))
    elif any(name in PYTHON_SCRIPTS for name in sections):
        checks.pass_("snapshot.scripts", "所选 Snapshot 生命周期官方脚本齐全")

    imported, detail = python_imports(args.python, "cubesandbox", env=env)
    if imported:
        checks.pass_("python.sdk", detail)
    else:
        checks.fail(
            "python.sdk",
            f"{args.python} 无法 import cubesandbox：{detail}；"
            f"可执行 {args.python} -m pip install -r {snapshot_dir / 'requirements.txt'}",
        )

    for service in SERVICES:
        if service_is_active(service, env=env):
            checks.pass_(f"service.{service}", "active")
        else:
            checks.fail(f"service.{service}", "不是 active；准备后必须重新预检")

    template_list = capture(["cubemastercli", "tpl", "list"], env=env, timeout=30)
    if template_list.returncode != 0:
        checks.fail("template.ready", f"cubemastercli tpl list 失败：{template_list.stdout.strip()}")
    elif not template_id:
        checks.fail("template.ready", "未选定 CUBE_TEMPLATE_ID/--template-id")
    else:
        matching = [line for line in template_list.stdout.splitlines() if template_id in line]
        if matching and any("READY" in line.upper() for line in matching):
            checks.pass_("template.ready", f"{template_id} 全局状态 READY")
        else:
            checks.fail("template.ready", f"模板列表中找不到 READY 状态的 {template_id}")
        template_info = capture(
            ["cubemastercli", "tpl", "info", "--json", template_id], env=env, timeout=30
        )
        if template_info.returncode != 0:
            checks.fail("template.replica", f"tpl info 失败：{template_info.stdout.strip()}")
        else:
            try:
                info = json.loads(template_info.stdout)
                replicas = info.get("replicas") or []
                replica_ready = info.get("status") == "READY" and any(
                    replica.get("status") == "READY" and replica.get("phase") == "READY"
                    for replica in replicas
                    if isinstance(replica, dict)
                )
                if replica_ready:
                    checks.pass_("template.replica", "至少一个节点副本 status/phase 均为 READY")
                else:
                    checks.fail("template.replica", "没有 status/phase 均为 READY 的节点副本")
            except (json.JSONDecodeError, AttributeError) as exc:
                checks.fail("template.replica", f"tpl info --json 输出无法解析：{exc}")

    api = CubeAPI(args.api_url, env["E2B_API_KEY"])
    healthy, detail = api.health()
    if healthy:
        checks.pass_("api.health", detail)
    else:
        checks.fail("api.health", f"{args.api_url}/health 直连失败：{detail}")
    try:
        ids = api.list_ids()
        checks.context["existing_sandboxes"] = len(ids)
        if ids:
            checks.fail(
                "sandbox.empty",
                f"API 可见 {len(ids)} 个既有沙箱；本技能不擅自全删，请先由用户清理",
            )
        else:
            checks.pass_("sandbox.empty", "API 可见沙箱为 0")
    except Exception as exc:
        checks.fail("sandbox.empty", f"无法可靠列出沙箱，不能按空环境处理：{exc}")

    try:
        processes = process_inventory(env=env)
        shims = [p for p in processes if p["comm"] == "containerd-shim"]
        checks.context["shim_processes"] = len(shims)
        if shims:
            checks.fail(
                "runtime.shims",
                f"仍有 {len(shims)} 个 containerd-shim；需先完成残留生命周期清理",
            )
        else:
            checks.pass_("runtime.shims", "containerd-shim 为 0")
        benchmarks = [
            p
            for p in processes
            if p["pid"] != os.getpid()
            and (
                p["comm"] == "cube-bench"
                or "tests/perf/cubebench.sh" in p["args"]
                or any(script in p["args"] for script in PYTHON_SCRIPTS.values())
            )
        ]
        if benchmarks:
            checks.fail(
                "runtime.benchmarks",
                "检测到其他基准进程：" + ", ".join(str(p["pid"]) for p in benchmarks[:8]),
            )
        else:
            checks.pass_("runtime.benchmarks", "无并发运行的其他 CubeSandbox 基准")
        busy = [
            p
            for p in processes
            if p["cpu_percent"] >= args.busy_threshold and not is_cube_or_system_process(p)
        ]
        # Keep evidence bounded and never include command-line credentials in reports.
        checks.context["busy_processes"] = [
            {"pid": p["pid"], "comm": p["comm"], "cpu_percent": p["cpu_percent"]}
            for p in busy[:20]
        ]
        if busy:
            checks.fail(
                "host.exclusive",
                f"检测到 {len(busy)} 个非 Cube 高 CPU 进程（阈值 {args.busy_threshold}%）："
                + ", ".join(f"{p['comm']}[{p['pid']}]" for p in busy[:8]),
            )
        else:
            checks.pass_("host.exclusive", f"未发现 CPU≥{args.busy_threshold}% 的非 Cube 进程")
    except BenchError as exc:
        checks.fail("host.processes", str(exc))

    load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    cpu_count = os.cpu_count() or 1
    load_limit = max(4.0, cpu_count * 0.05)
    checks.context["load1"] = load1
    checks.context["load1_limit"] = load_limit
    if load1 > load_limit:
        checks.fail("host.load", f"load1={load1:.2f} > {load_limit:.2f}，存在资源争用风险")
    else:
        checks.pass_("host.load", f"load1={load1:.2f} ≤ {load_limit:.2f}；仍需用户确认独占窗口")

    try:
        available = available_memory_mb()
        required = plan_density_floor(args, sections)
        checks.context["available_memory_mb"] = available
        if available < required:
            checks.fail("host.memory", f"MemAvailable={available} MiB，低于要求 {required} MiB")
        else:
            checks.pass_("host.memory", f"MemAvailable={available} MiB；density 仍逐批设安全水位")
    except BenchError as exc:
        checks.fail("host.memory", str(exc))

    data_path = Path("/data")
    if data_path.exists():
        free_gb = shutil.disk_usage(data_path).free / (1024**3)
        if free_gb < 20:
            checks.fail("host.disk", f"/data 可用空间仅 {free_gb:.1f} GiB，Snapshot 测试至少需要 20 GiB")
        else:
            checks.pass_("host.disk", f"/data 可用 {free_gb:.1f} GiB")
    else:
        checks.fail("host.disk", "/data 不存在，不是预期 CubeSandbox 部署目录")

    topology = numa_topology()
    if not topology:
        checks.fail("numa.topology", "无法从 sysfs 读取 NUMA 拓扑")
        node = args.numa_node if args.numa_node is not None else 0
    else:
        node = effective_numa_node(args, checks, topology)
        checks.context["numa_node"] = node
        checks.context["numa_nodes"] = {str(k): len(v) for k, v in topology.items()}
        if node not in topology:
            checks.fail("numa.node", f"NUMA node{node} 不存在；可选 {sorted(topology)}")
        else:
            for role, service, script, variable in (
                (
                    "cubemaster",
                    "cube-sandbox-cubemaster.service",
                    args.toolbox / "scripts/systemd/cubemaster-start.sh",
                    "CUBEMASTER_BIN",
                ),
                (
                    "cubelet",
                    "cube-sandbox-cubelet.service",
                    args.toolbox / "scripts/systemd/cubelet-start.sh",
                    "CUBELET_BIN",
                ),
            ):
                bindings = script_bindings(script, variable)
                if bindings and all(value == node for value in bindings):
                    checks.pass_(f"numa.{role}.script", f"{script} 所有启动行绑定 node{node}")
                else:
                    checks.fail(
                        f"numa.{role}.script",
                        f"{script} 绑定为 {bindings or '未绑定'}，期望 node{node}；run --prepare 可幂等修正",
                    )
                actual = process_cpus(service_main_pid(service, env=env))
                if actual and actual == topology[node]:
                    checks.pass_(f"numa.{role}.process", f"运行进程 CPU 亲和性等于 node{node}")
                else:
                    checks.fail(
                        f"numa.{role}.process",
                        "运行进程未绑定到目标 NUMA 节点；准备阶段会重启对应服务并长时间等待",
                    )

    try:
        configured_target, network = cubelet_network_config(args.toolbox)
        checks.context["tap_cidr"] = str(network)
        checks.context["configured_tap_target"] = configured_target
        if configured_target == args.tap_target:
            checks.pass_("network.tap.config", f"Cubelet tap_init_num={configured_target}，cidr={network}")
        else:
            checks.fail(
                "network.tap.config",
                f"Cubelet tap_init_num={configured_target}，与 --tap-target={args.tap_target} 不一致",
            )
        interfaces = list_ip_interfaces(env=env)
        unverified = [
            item["name"] for item in interfaces
            if item["address"] in network and not item["verified_tap"]
        ]
        if unverified:
            checks.fail(
                "network.tap.type",
                f"Cube CIDR 内存在未验证为 persistent TAP 的接口：{unverified[:8]}",
            )
        else:
            checks.pass_("network.tap.type", "CIDR 内接口均有 ip -d 的 tun type tap + persist 证据")
        taps = list_taps(env=env, network=network)
        count = len(taps)
        candidates = tap_cleanup_candidates(taps, args.tap_target)
        evidence = tap_evidence_digest(taps, args.tap_target)
        checks.context["tap_count"] = count
        checks.context["tap_cleanup_candidates"] = candidates
        checks.context["tap_evidence_sha256"] = evidence
        if count == args.tap_target:
            checks.pass_("network.tap", f"Cube CIDR 内已验证 TAP 数量为 {count}；TAP_EVIDENCE_SHA256={evidence}")
        else:
            checks.fail(
                "network.tap",
                f"TAP={count}，目标={args.tap_target}；超额需清理，清理/不足均需重启 cubelet；"
                f"TAP_EVIDENCE_SHA256={evidence}",
            )
    except BenchError as exc:
        checks.fail("network.tap", str(exc))

    present = [key for key in PROXY_VARIABLES if os.environ.get(key)]
    checks.context["proxy_variables_removed"] = present
    checks.pass_(
        "network.proxy",
        "执行器已移除全部 HTTP/HTTPS/ALL proxy 变量"
        + (f"（原环境存在：{', '.join(present)}）" if present else "（原环境未设置）"),
    )
    return checks


def plan_density_floor(args, sections: list[str]) -> int:
    if "density" not in sections:
        return 1024
    try:
        return max(1024, int(args._plan["density"]["min_available_mb"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        return 1024


def ensure_prepare_safety(args, env: dict, network) -> list[tuple[int, str]]:
    if not is_root():
        raise BenchError("prepare 需要 root")
    api = CubeAPI(args.api_url, env["E2B_API_KEY"])
    try:
        ids = api.list_ids()
    except Exception as exc:
        raise BenchError(f"prepare 前无法证明沙箱为空，拒绝清 TAP/重启：{exc}") from exc
    if ids:
        raise BenchError(f"prepare 前仍有 {len(ids)} 个沙箱，拒绝清 TAP/重启")
    processes = process_inventory(env=env)
    shims = [p for p in processes if p["comm"] == "containerd-shim"]
    if shims:
        raise BenchError(f"prepare 前仍有 {len(shims)} 个 shim，拒绝删 TAP；请先完成残留清理")
    running = [
        p
        for p in processes
        if p["pid"] != os.getpid()
        and (
            p["comm"] == "cube-bench"
            or "tests/perf/cubebench.sh" in p["args"]
            or any(script in p["args"] for script in PYTHON_SCRIPTS.values())
        )
        and "cubesandbox_bench.py" not in p["args"]
    ]
    if running:
        raise BenchError("检测到其他基准进程，拒绝 prepare：" + ", ".join(str(p["pid"]) for p in running))
    unverified = [
        item["name"] for item in list_ip_interfaces(env=env)
        if item["address"] in network and not item["verified_tap"]
    ]
    if unverified:
        raise BenchError(f"Cube CIDR 内有接口缺少 persistent TAP 证据，拒绝删除：{unverified[:8]}")
    taps = list_taps(env=env, network=network)
    return taps


def build_numa_patch(path: Path, variable: str, node: int):
    """Render and validate one patch without modifying the target."""
    original = path.read_text(encoding="utf-8")
    updated, changed = rewrite_numa_launches(original, variable, node)
    if not changed:
        bindings = script_bindings(path, variable)
        if bindings and all(binding == node for binding in bindings):
            return None
        raise BenchError(f"无法识别 {path} 中的 {variable} 启动行，未修改")
    return {"path": path, "original": original, "updated": updated, "node": node}


def atomic_script_write(path: Path, content: str):
    original_stat = path.stat()
    temporary = path.with_name(f"{path.name}.kpbot.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(stat.S_IMODE(original_stat.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def commit_numa_patch(patch: dict, backup_dir: Path) -> Path:
    path = patch["path"]
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.name}.{time.time_ns()}.bak"
    shutil.copy2(path, backup)
    atomic_script_write(path, patch["updated"])
    log(f"已更新 {path} 为 NUMA node{patch['node']}；备份 {backup}")
    return backup


def patch_numa_script(path: Path, variable: str, node: int, backup_dir: Path) -> bool:
    patch = build_numa_patch(path, variable, node)
    if patch is None:
        return False
    commit_numa_patch(patch, backup_dir)
    return True


def restart_service(service: str, env: dict):
    log(f"重启 {service}")
    result = capture(["systemctl", "restart", service], env=env, timeout=180)
    if result.returncode != 0:
        raise BenchError(f"重启 {service} 失败：{result.stdout.strip()}")


def wait_cubelet_ready(args, env: dict, node: int, network) -> dict:
    """Wait for slow TAP pool reconstruction; never mistake systemd active for readiness."""
    deadline = time.monotonic() + args.cubelet_wait
    last_message = 0.0
    last = {}
    while True:
        active = service_is_active("cube-sandbox-cubelet.service", env=env)
        count = len(list_taps(env=env, network=network))
        affinity = process_cpus(service_main_pid("cube-sandbox-cubelet.service", env=env))
        topology = numa_topology()
        affinity_ok = node in topology and affinity == topology[node]
        healthy, health_detail = CubeAPI(args.api_url, env["E2B_API_KEY"]).health()
        last = {
            "cubelet_active": active,
            "tap_count": count,
            "tap_target": args.tap_target,
            "numa_affinity_ok": affinity_ok,
            "api_healthy": healthy,
            "api_detail": health_detail,
        }
        if active and count == args.tap_target and affinity_ok and healthy:
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BenchError(
                f"等待 cubelet 最优就绪超时（{args.cubelet_wait}s）：{last}；"
                "请检查 Cubelet/CubeVmm 日志后重跑预检，不要带慢环境压测"
            )
        now = time.monotonic()
        if now - last_message >= 30:
            log(f"cubelet 仍在恢复，剩余等待 {int(remaining)}s：{last}")
            last_message = now
        time.sleep(min(5, max(0.1, remaining)))


def prepare_environment(args, env: dict, run_dir: Path) -> dict:
    actions = []
    journal_path = run_dir / "prepare.json"
    write_json(journal_path, {"status": "in_progress", "actions": actions})
    try:
        payload = _prepare_environment_impl(args, env, run_dir, actions)
    except BaseException as exc:
        write_json(
            journal_path,
            {"status": "failed", "error": str(exc) or type(exc).__name__, "actions": actions},
        )
        raise
    write_json(
        journal_path,
        {"status": "completed", "result": payload, "actions": actions},
    )
    return payload


def journal_action(run_dir: Path, actions: list[dict], action: str, detail=None):
    actions.append(
        {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "detail": detail,
        }
    )
    write_json(run_dir / "prepare.json", {"status": "in_progress", "actions": actions})


def _prepare_environment_impl(args, env: dict, run_dir: Path, actions: list[dict]) -> dict:
    configured_target, network = cubelet_network_config(args.toolbox)
    if configured_target != args.tap_target:
        raise BenchError(
            f"Cubelet tap_init_num={configured_target} 与批准目标 {args.tap_target} 不一致；"
            "本技能不擅自改配置，请先由用户调整部署配置"
        )
    journal_action(
        run_dir, actions, "config_verified",
        {"tap_target": configured_target, "cidr": str(network)},
    )
    taps = ensure_prepare_safety(args, env, network)
    actual_evidence = tap_evidence_digest(taps, args.tap_target)
    if actual_evidence != args.tap_evidence:
        raise BenchError(
            "TAP 精确集合在 check 后已变化，拒绝删除："
            f"approved={args.tap_evidence}, actual={actual_evidence}；请重新 check 和确认"
        )
    journal_action(
        run_dir, actions, "safety_verified",
        {
            "sandboxes": 0, "shims": 0, "verified_cube_taps": len(taps),
            "tap_evidence_sha256": actual_evidence,
        },
    )
    topology = numa_topology()
    node = args.numa_node
    if node is None:
        bindings = {
            binding
            for binding in (
                script_bindings(args.toolbox / "scripts/systemd/cubemaster-start.sh", "CUBEMASTER_BIN")
                + script_bindings(args.toolbox / "scripts/systemd/cubelet-start.sh", "CUBELET_BIN")
            )
            if binding >= 0
        }
        if len(bindings) == 1:
            node = bindings.pop()
        elif len(topology) > 1:
            raise BenchError("多 NUMA 且现有启动脚本未一致绑定，必须显式传入已确认的 --numa-node")
        else:
            node = 0
    if node not in topology:
        raise BenchError(f"NUMA node{node} 不存在；可选 {sorted(topology)}")

    backup_dir = run_dir / "prepare-backups"
    master_path = args.toolbox / "scripts/systemd/cubemaster-start.sh"
    cubelet_path = args.toolbox / "scripts/systemd/cubelet-start.sh"
    # Render both first: an unrecognized cubelet launch must not leave a partial master edit.
    master_patch = build_numa_patch(master_path, "CUBEMASTER_BIN", node)
    cubelet_patch = build_numa_patch(cubelet_path, "CUBELET_BIN", node)
    attempted = []
    try:
        for patch in (master_patch, cubelet_patch):
            if patch is not None:
                # Register before commit so an interrupt after atomic replace is rolled back.
                attempted.append(patch)
                journal_action(
                    run_dir, actions, "numa_script_commit_started", str(patch["path"])
                )
                backup = commit_numa_patch(patch, backup_dir)
                journal_action(
                    run_dir, actions, "numa_script_committed",
                    {"path": str(patch["path"]), "backup": str(backup)},
                )
    except BaseException:
        for patch in reversed(attempted):
            atomic_script_write(patch["path"], patch["original"])
        journal_action(run_dir, actions, "numa_scripts_rolled_back", len(attempted))
        raise
    master_changed = master_patch is not None
    cubelet_changed = cubelet_patch is not None
    master_affinity_bad = process_cpus(
        service_main_pid("cube-sandbox-cubemaster.service", env=env)
    ) != topology[node]
    cubelet_affinity_bad = process_cpus(
        service_main_pid("cube-sandbox-cubelet.service", env=env)
    ) != topology[node]

    tap_changed = len(taps) != args.tap_target
    deleted = []
    if len(taps) > args.tap_target:
        # list_taps is ifindex ordered; retain the oldest target interfaces.
        excess = tap_cleanup_candidates(taps, args.tap_target)
        log(f"沙箱/shim 均为空，删除 {len(excess)} 个已批准的 Cube CIDR TAP，保留 {args.tap_target} 个")
        journal_action(
            run_dir, actions, "tap_cleanup_started",
            {"retain": args.tap_target, "planned_delete": excess},
        )
        for candidate in excess:
            name = candidate["name"]
            journal_action(run_dir, actions, "tap_delete_started", candidate)
            result = capture(["ip", "link", "delete", "dev", name], env=env, timeout=20)
            if result.returncode != 0:
                journal_action(
                    run_dir, actions, "tap_cleanup_failed",
                    {"deleted": deleted, "failed": candidate, "error": result.stdout.strip()},
                )
                raise BenchError(f"删除 TAP {name} 失败：{result.stdout.strip()}")
            deleted.append(name)
            journal_action(run_dir, actions, "tap_delete_completed", candidate)
        journal_action(run_dir, actions, "tap_cleanup_completed", {"deleted": deleted})
        if len(list_taps(env=env, network=network)) != args.tap_target:
            raise BenchError("TAP 清理后数量仍未达到目标，拒绝重启后继续")
    elif len(taps) < args.tap_target:
        log(f"TAP={len(taps)} 少于目标 {args.tap_target}，将通过重启 cubelet 重建资源池")

    if master_changed or master_affinity_bad:
        journal_action(run_dir, actions, "cubemaster_restart_started")
        restart_service("cube-sandbox-cubemaster.service", env=env)
        journal_action(run_dir, actions, "cubemaster_restart_completed")
    if cubelet_changed or cubelet_affinity_bad or tap_changed:
        journal_action(run_dir, actions, "cubelet_restart_started")
        restart_service("cube-sandbox-cubelet.service", env=env)
        journal_action(run_dir, actions, "cubelet_restart_completed")
        readiness = wait_cubelet_ready(args, env, node, network)
        journal_action(run_dir, actions, "cubelet_ready", readiness)
    else:
        readiness = {
            "tap_count": len(list_taps(env=env, network=network)),
            "tap_target": args.tap_target,
            "numa_affinity_ok": not cubelet_affinity_bad,
        }

    cube_bench = args.repo / "examples/cube-bench/bin/cube-bench"
    if not cube_bench.is_file():
        log("cube-bench 不存在，使用官方 Makefile 构建")
        result = capture(
            ["make", "-C", str(args.repo / "examples/cube-bench")], env=env, timeout=600
        )
        if result.returncode != 0 or not cube_bench.is_file():
            raise BenchError(f"cube-bench 构建失败：{result.stdout[-4000:]}")
        journal_action(run_dir, actions, "cube_bench_built", str(cube_bench))

    payload = {
        "numa_node": node,
        "cubemaster_script_changed": master_changed,
        "cubelet_script_changed": cubelet_changed,
        "tap_before": len(taps),
        "tap_deleted": len(deleted),
        "readiness": readiness,
        "proxy_variables_removed": [key for key in PROXY_VARIABLES if key in os.environ],
    }
    return payload


def make_jobs(args, plan: dict, sections: list[str], run_dir: Path) -> list[dict]:
    jobs = []
    for section_name in sections:
        section = plan[section_name]
        if section_name == "template_create":
            for case_index, case in enumerate(section["cases"], 1):
                for round_no in range(1, case["rounds"] + 1):
                    stem = f"c{case['concurrency']}_n{case['requests']}_r{round_no}"
                    report = run_dir / "template-create" / f"{stem}.json"
                    command = [
                        str(args.repo / "examples/cube-bench/bin/cube-bench"),
                        "-c",
                        str(case["concurrency"]),
                        "-n",
                        str(case["requests"]),
                        "-w",
                        str(section["warmup"]),
                        "-m",
                        section["mode"],
                        "--no-tui",
                        "-o",
                        str(report),
                    ]
                    jobs.append(
                        {
                            "section": section_name,
                            "case": f"case{case_index}-round{round_no}",
                            "command": command,
                            "cwd": args.repo / "examples/cube-bench",
                            "log": run_dir / "template-create" / f"{stem}.log",
                            "json": report,
                            "settle": section["settle_secs"],
                            "requests": case["requests"],
                            "concurrency": case["concurrency"],
                            "mode": section["mode"],
                        }
                    )
        elif section_name == "density":
            for index, case in enumerate(section["batches"], 1):
                report = run_dir / "density" / f"batch{index}.json"
                jobs.append(
                    {
                        "section": section_name,
                        "case": f"batch{index}",
                        "command": [
                            str(args.repo / "examples/cube-bench/bin/cube-bench"),
                            "-c",
                            str(case["concurrency"]),
                            "-n",
                            str(case["requests"]),
                            "-w",
                            "0",
                            "-m",
                            "create-only",
                            "--no-tui",
                            "-o",
                            str(report),
                        ],
                        "cwd": args.repo / "examples/cube-bench",
                        "log": run_dir / "density" / f"batch{index}.log",
                        "json": report,
                        "settle": 0,
                        "requests": case["requests"],
                        "concurrency": case["concurrency"],
                        "mode": "create-only",
                        "keep_alive": True,
                    }
                )
        else:
            script = args.repo / "examples/snapshot-rollback-clone" / PYTHON_SCRIPTS[section_name]
            for index, case in enumerate(section["cases"], 1):
                if section_name == "dirty_page":
                    parameters = ["-d", str(case["dirty_mb"]), "-n", str(case["rounds"])]
                    label = f"d{case['dirty_mb']}"
                elif section_name == "clone":
                    parameters = [
                        "-n",
                        str(case["num"]),
                        "-c",
                        str(case["concurrency"]),
                        "--rounds",
                        str(case["rounds"]),
                        "-d",
                        str(section["dirty_mb"]),
                    ]
                    label = f"n{case['num']}_c{case['concurrency']}"
                else:
                    parameters = ["-c", str(case["concurrency"]), "-n", str(case["rounds"])]
                    if section_name == "rollback":
                        parameters += ["-d", str(section["dirty_mb"])]
                    label = f"c{case['concurrency']}"
                parameters += ["-s", str(section["settle_secs"])]
                jobs.append(
                    {
                        "section": section_name,
                        "case": f"case{index}-{label}",
                        "command": [args.python, str(script), *parameters],
                        "cwd": script.parent,
                        "log": run_dir / section_name / f"case{index}-{label}.log",
                        "settle": 0,
                    }
                )
    return jobs


def stream_command(command: list[str], cwd: Path, env: dict, log_path: Path) -> tuple[int, float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    log(f"执行：{shlex.join(command)}")
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                output.write(line)
                output.flush()
                print(line, end="", flush=True)
            return_code = process.wait()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
    duration = time.monotonic() - started
    return return_code, duration


def wait_api_baseline(api: CubeAPI, baseline: set[str], timeout: int = 300) -> None:
    """Wait until the API view exactly returns to this job's baseline."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            current = api.list_ids()
            if current == baseline:
                return
            last_error = (
                f"new={sorted(current - baseline)}, missing={sorted(baseline - current)}"
            )
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise BenchError(f"等待沙箱恢复到 baseline 超时：{last_error}")


def cube_json_metrics(
    path: Path,
    expected_requests: int = 0,
    expected_concurrency: int = 0,
    expected_mode: str = "",
    expected_template: str = "",
) -> tuple[dict, list[str]]:
    """Extract metrics only after validating cube-bench's report invariants."""
    if not path.is_file():
        return {}, ["未生成 JSON 报告"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, [f"JSON 报告无效：{exc}"]
    if not isinstance(payload, dict):
        return {}, ["JSON 根节点不是对象"]
    summary = payload.get("summary")
    create = payload.get("create")
    config = payload.get("config")
    raw = payload.get("raw")
    problems = []
    if not isinstance(summary, dict) or not isinstance(create, dict) or not isinstance(config, dict):
        return {}, ["JSON 缺少对象类型的 summary/create/config"]
    if not isinstance(raw, list):
        return {}, ["JSON 缺少数组类型的 raw 逐请求明细"]
    metrics = {
        "successful": summary.get("successful"),
        "errors": summary.get("errors"),
        "success_rate": summary.get("success_rate"),
        "total_time_s": summary.get("total_time_s"),
        "throughput_qps": summary.get("throughput_qps"),
        "create_avg_ms": create.get("avg"),
        "create_p95_ms": create.get("p95"),
        "create_p99_ms": create.get("p99"),
    }
    successful = summary.get("successful")
    errors = summary.get("errors")
    total = config.get("total")
    rate = summary.get("success_rate")
    count = create.get("count")
    for name, value in (("successful", successful), ("errors", errors), ("config.total", total), ("create.count", count)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{name} 缺失或不是非负整数")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate):
        problems.append("success_rate 缺失或不是有限数值")
    if expected_requests and total != expected_requests:
        problems.append(f"config.total={total!r}，与本次请求数 {expected_requests} 不符")
    if expected_concurrency and config.get("concurrency") != expected_concurrency:
        problems.append(
            f"config.concurrency={config.get('concurrency')!r}，与批准档位 {expected_concurrency} 不符"
        )
    if expected_mode and config.get("mode") != expected_mode:
        problems.append(f"config.mode={config.get('mode')!r}，与批准模式 {expected_mode} 不符")
    if expected_template and config.get("template") != expected_template:
        problems.append("config.template 与本次批准模板不符")
    for name, value in (
        ("summary.total_time_s", summary.get("total_time_s")),
        ("summary.throughput_qps", summary.get("throughput_qps")),
        ("create.avg", create.get("avg")),
        ("create.p95", create.get("p95")),
        ("create.p99", create.get("p99")),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            problems.append(f"{name} 缺失或不是有限非负数")
    raw_errors = 0
    # Per-request delete_ms only exists when the run actually deletes (create-delete).
    # The documented in-repo schema for cube-bench raw is {seq, create_ms[, error]};
    # create-only reports therefore carry no delete latency and must not fail here.
    required_fields = ("create_ms", "delete_ms") if expected_mode == "create-delete" else ("create_ms",)
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"raw[{index}] 不是对象")
            continue
        error = item.get("error", "")
        if not isinstance(error, str):
            problems.append(f"raw[{index}].error 不是字符串")
        elif error:
            raw_errors += 1
        for field in required_fields:
            value = item.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                problems.append(f"raw[{index}].{field} 缺失或不是有限非负数")
    if isinstance(errors, int) and not isinstance(errors, bool) and raw_errors != errors:
        problems.append(f"raw 错误数={raw_errors}，summary.errors={errors}")
    if not problems:
        if successful + errors != total:
            problems.append(f"successful({successful}) + errors({errors}) != total({total})")
        if len(raw) != total:
            problems.append(f"raw 数量={len(raw)}，total={total}，报告不完整")
        if count != successful:
            problems.append(f"create.count={count}，successful={successful}")
        if errors != 0:
            problems.append(f"cube-bench errors={errors}")
        if rate != 1:
            problems.append(f"success_rate={rate}")
    return metrics, problems


def analyze_create_reports(args, env: dict, run_dir: Path) -> str:
    analyzer = (
        Path(__file__).resolve().parent.parent.parent
        / "cubesandbox-bench-protocol/scripts/analyze_bench_results.py"
    )
    report_dir = run_dir / "template-create"
    if not report_dir.is_dir():
        return ""
    if not analyzer.is_file():
        raise BenchError(f"缺少配套 create 分析器：{analyzer}")
    output = run_dir / "create-verdict.json"
    result = capture(
        [args.python, str(analyzer), "--dir", str(report_dir), "--output", str(output)],
        env=env,
        timeout=120,
    )
    if result.returncode != 0:
        raise BenchError(
            f"create 结果分析器退出码 {result.returncode}，不能判为完整通过：{result.stdout[-1000:]}"
        )
    if not output.is_file():
        raise BenchError("create 分析器未生成 create-verdict.json")
    return str(output)


def write_run_report(run_dir: Path, metadata: dict, records: list[dict], verdict: str):
    lines = [
        "# CubeSandbox 生命周期性能测试报告",
        "",
        f"- 主机：`{metadata['hostname']}`",
        f"- 内核：`{metadata['kernel']}`",
        f"- 源码 commit：`{metadata['repo_commit']}`",
        f"- Template：`{metadata['template_id']}`",
        f"- 计划 SHA256：`{metadata['plan_sha256']}`",
        f"- 批准范围 SHA256：`{metadata['approval_sha256']}`",
        f"- 代理：执行子进程已 unset HTTP/HTTPS/ALL proxy",
        "- 清理：仅删除相对各测试基线新增的 API 沙箱 ID",
        "",
        "所有时间原始单位以官方脚本输出为准（通常为 ms）。任何非零退出码、请求错误或清理失败都不得判为完整通过。",
        "",
    ]
    for record in records:
        lines.append(f"## {SECTION_TITLES.get(record['section'], record['section'])} / {record['case']}")
        lines.append("")
        lines.append(f"- 状态：**{'PASS' if record['ok'] else 'FAIL'}**")
        lines.append(f"- 耗时：{record['duration_s']:.3f}s；退出码：{record['return_code']}")
        lines.append(f"- 日志：[{Path(record['log']).name}]({Path(record['log']).relative_to(run_dir)})")
        if record.get("json"):
            lines.append(f"- JSON：[{Path(record['json']).name}]({Path(record['json']).relative_to(run_dir)})")
        if record.get("metrics"):
            lines.append(f"- 指标：`{json.dumps(record['metrics'], ensure_ascii=False)}`")
        if record.get("output_tail"):
            lines.extend(["", "```text", record["output_tail"], "```"])
        if record.get("problems"):
            lines.append("- 问题：" + "；".join(record["problems"]))
        cleanup = record.get("cleanup") or []
        if cleanup:
            lines.append(f"- 差集清理：{len(cleanup)} 个沙箱")
        lines.append("")
    if metadata.get("density_cleanup") is not None:
        density_cleanup = metadata["density_cleanup"]
        failed = sum(not item["ok"] for item in density_cleanup)
        lines.append(
            f"Density 最终差集清理：{len(density_cleanup)} 个沙箱，失败 {failed} 个；"
            "逐 ID 结果见 results.json。"
        )
        lines.append("")
    if verdict:
        lines.append(f"Create 分布/多轮判读：[create-verdict.json]({Path(verdict).relative_to(run_dir)})")
        lines.append("")
    lines.append("Snapshot/template 残留不属于 API 沙箱差集；若脚本异常，请按日志中的 ID 审核并清理。")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def dry_run(args, plan: dict, sections: list[str]) -> int:
    print(plan_summary(plan, sections))
    print(approval_summary(args, plan, sections))
    print("DRY-RUN：只做计划展开，不访问 API、不修改 NUMA/TAP、不重启、不创建沙箱。")
    if args.prepare:
        print("正式执行时将先 prepare（需 --yes 与独占确认）。")
    placeholder = Path("<run-dir>")
    for job in make_jobs(args, plan, sections, placeholder):
        print(f"[{job['section']} / {job['case']}] {shlex.join(job['command'])}")
    return 0


def run_benchmark(args, plan: dict, sections: list[str]) -> int:
    require_confirmation(args, plan, sections)
    digest = plan_digest(plan)
    approved_digest = approval_digest(args, plan, sections)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = args.output_root / f"{timestamp}_{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "approved-plan.json", plan)
    env = sanitized_environment(args.template_id, args.api_url)
    template_id = resolve_template(env, args.template_id)
    env["CUBE_TEMPLATE_ID"] = template_id
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "kernel": " ".join(os.uname()) if hasattr(os, "uname") else "unknown",
        "repo": str(args.repo),
        "repo_commit": repo_commit(args.repo),
        "template_id": template_id,
        "api_url": args.api_url,
        "plan_sha256": digest,
        "approval_sha256": approved_digest,
        "approval_scope": approval_payload(args, plan, sections),
        "sections": sections,
        "numa_node_arg": args.numa_node,
        "tap_target": args.tap_target,
        "prepare": args.prepare,
        "proxy_policy": "unset " + ",".join(PROXY_VARIABLES),
    }
    write_json(run_dir / "metadata.json", metadata)
    log(f"结果目录：{run_dir}")

    args._plan = plan
    if args.prepare:
        prepare_environment(args, env, run_dir)
    checks = run_preflight(args, env, template_id, sections)
    write_json(run_dir / "preflight.json", checks.payload())
    print(checks.render(), flush=True)
    if not checks.ok:
        write_run_report(run_dir, metadata, [], "")
        raise BenchError("预检未通过，未启动任何基准；详见 preflight.json")

    api = CubeAPI(args.api_url, env["E2B_API_KEY"])
    records = []
    density_rows = []
    density_live = 0
    density_baseline_available = None
    density_baseline_ids = set()
    density_cleanup = None
    density_cleanup_complete = False
    overall_error = None
    try:
        smoke_job = {
            "section": "smoke",
            "case": "create-run_code-delete",
            "command": [args.python, "-c", SMOKE_CODE],
            "cwd": args.repo / "examples/snapshot-rollback-clone",
            "log": run_dir / "smoke.log",
            "settle": 1,
        }
        jobs = [smoke_job] + make_jobs(args, plan, sections, run_dir)
        for job_index, job in enumerate(jobs):
            is_density = job["section"] == "density"
            if is_density and density_baseline_available is None:
                # Arm final cleanup only after a complete baseline has been read.
                candidate_ids = api.list_ids()
                if candidate_ids:
                    raise BenchError("density 基线不是空沙箱环境")
                candidate_available = available_memory_mb()
                density_baseline_ids = candidate_ids
                density_baseline_available = candidate_available
            if is_density and available_memory_mb() < plan["density"]["min_available_mb"]:
                raise BenchError("density 下一批前 MemAvailable 低于 min_available_mb，停止扩批并清理")
            baseline = density_baseline_ids if is_density else api.list_ids()
            problems = []
            metrics = {}
            cleanup = []
            job_error = None
            return_code = 125
            job_started = time.monotonic()
            try:
                return_code, duration = stream_command(
                    job["command"], job["cwd"], env, job["log"]
                )
                if job.get("json"):
                    metrics, problems = cube_json_metrics(
                        job["json"],
                        expected_requests=job.get("requests", 0),
                        expected_concurrency=job.get("concurrency", 0),
                        expected_mode=job.get("mode", ""),
                        expected_template=args.template_id,
                    )
            except KeyboardInterrupt as exc:
                job_error = exc
                return_code = 130
                problems.append("用户中断；已终止该 job 的进程组")
            except Exception as exc:
                job_error = exc
                problems.append(f"执行/日志异常：{exc}")
            finally:
                duration = time.monotonic() - job_started
                # Also clean and persist failure when Popen/streaming is interrupted.
                if not is_density:
                    try:
                        cleanup = api.cleanup_difference(baseline)
                        failed_cleanup = [item for item in cleanup if not item["ok"]]
                        if failed_cleanup:
                            problems.append(f"{len(failed_cleanup)} 个沙箱差集清理失败")
                        wait_api_baseline(api, baseline)
                    except BaseException as exc:
                        if isinstance(exc, KeyboardInterrupt):
                            job_error = exc
                            return_code = 130
                        elif job_error is None:
                            job_error = exc
                            return_code = 125
                        problems.append(f"差集清理/等待异常：{type(exc).__name__}: {exc}")
            if is_density:
                try:
                    # Sample settled memory and actual API delta, never requested count.
                    time.sleep(plan["density"]["settle_secs"])
                    current_ids = api.list_ids()
                    previous_live = density_live
                    density_live = len(current_ids - density_baseline_ids)
                    actual_new = density_live - previous_live
                    successful = metrics.get("successful")
                    if isinstance(successful, int) and successful != actual_new:
                        problems.append(
                            f"API 实际新增={actual_new}，cube-bench successful={successful}"
                        )
                    current_available = available_memory_mb()
                    overhead = (
                        (density_baseline_available - current_available) / density_live
                        if density_live
                        else None
                    )
                    density_rows.append(
                        {
                            "batch": job["case"],
                            "live_sandboxes": density_live,
                            "actual_new": actual_new,
                            "available_mb": current_available,
                            "baseline_available_mb": density_baseline_available,
                            "available_delta_per_vm_mb": (
                                round(overhead, 3) if overhead is not None else ""
                            ),
                        }
                    )
                    with (run_dir / "density.csv").open("w", newline="", encoding="utf-8") as stream:
                        writer = csv.DictWriter(stream, fieldnames=list(density_rows[0]))
                        writer.writeheader()
                        writer.writerows(density_rows)
                    if current_available < plan["density"]["min_available_mb"]:
                        problems.append("本批 settle 后低于内存安全水位；停止后续扩批")
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        job_error = exc
                        return_code = 130
                    elif job_error is None:
                        job_error = exc
                        return_code = 125
                    problems.append(f"density 采样异常：{type(exc).__name__}: {exc}")
            try:
                log_text = (
                    job["log"].read_text(encoding="utf-8", errors="replace")
                    if job["log"].is_file()
                    else ""
                )
            except BaseException as exc:
                log_text = ""
                if isinstance(exc, KeyboardInterrupt):
                    job_error = exc
                    return_code = 130
                elif job_error is None:
                    job_error = exc
                    return_code = 125
                problems.append(f"读取日志失败：{type(exc).__name__}: {exc}")
            if job["section"] == "smoke" and "SMOKE_OK" not in log_text:
                problems.append("冒烟日志缺少 SMOKE_OK 或 Guest 执行返回错误")
            if re.search(r"warmup\s*\[\d+/\d+\]\s*failed", log_text, flags=re.IGNORECASE):
                problems.append("cube-bench warm-up 存在失败")
            nonempty = [line for line in log_text.splitlines() if line.strip()]
            record = {
                **{key: str(value) if isinstance(value, Path) else value for key, value in job.items()
                   if key not in ("command", "cwd", "settle", "keep_alive", "requests")},
                "command": shlex.join(job["command"]),
                "return_code": return_code,
                "duration_s": duration,
                "metrics": metrics,
                "problems": problems,
                "cleanup": cleanup,
                "output_tail": "\n".join(nonempty[-4:]),
                "ok": job_error is None and return_code == 0 and not problems,
            }
            records.append(record)
            write_json(run_dir / "results.json", {"metadata": metadata, "records": records})
            if isinstance(job_error, KeyboardInterrupt):
                raise job_error
            if not record["ok"]:
                raise BenchError(f"{job['section']}/{job['case']} 失败，停止后续测试")
            is_last_density_job = is_density and (
                job_index + 1 == len(jobs) or jobs[job_index + 1]["section"] != "density"
            )
            if is_last_density_job:
                # Density is a retained-sandbox section; drain it before any next section.
                density_cleanup = api.cleanup_difference(density_baseline_ids)
                failed_cleanup = [item for item in density_cleanup if not item["ok"]]
                if failed_cleanup:
                    raise BenchError(
                        f"density 章节结束清理有 {len(failed_cleanup)} 个删除请求失败"
                    )
                wait_api_baseline(
                    api, density_baseline_ids, timeout=max(300, density_live * 5)
                )
                density_cleanup_complete = True
            if job["settle"]:
                time.sleep(job["settle"])
    except (Exception, KeyboardInterrupt) as exc:
        overall_error = str(exc) if isinstance(exc, Exception) else "用户中断"
        log(f"ERROR: {overall_error}")
    finally:
        if density_baseline_available is not None and not density_cleanup_complete:
            try:
                density_cleanup = api.cleanup_difference(density_baseline_ids)
                failed_density_cleanup = [item for item in density_cleanup if not item["ok"]]
                for item in failed_density_cleanup:
                    log(f"ERROR: density 清理失败：{item}")
                if failed_density_cleanup:
                    overall_error = (
                        f"{overall_error or ''}; "
                        f"density 有 {len(failed_density_cleanup)} 个删除请求失败"
                    ).strip("; ")
                wait_api_baseline(
                    api, density_baseline_ids, timeout=max(300, density_live * 5)
                )
            except BaseException as exc:
                overall_error = f"{overall_error or ''}; density 清理异常：{type(exc).__name__}: {exc}"
                log(f"ERROR: density 清理异常：{type(exc).__name__}: {exc}")

    try:
        verdict = analyze_create_reports(args, env, run_dir)
    except Exception as exc:
        verdict = ""
        overall_error = f"{overall_error or ''}; {exc}".strip("; ")
    metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
    metadata["overall_error"] = overall_error
    metadata["density_cleanup"] = density_cleanup
    write_json(run_dir / "metadata.json", metadata)
    write_json(
        run_dir / "results.json",
        {
            "metadata": metadata,
            "records": records,
            "density": density_rows,
            "density_cleanup": density_cleanup,
            "create_verdict": verdict,
            "ok": overall_error is None and all(record["ok"] for record in records),
        },
    )
    write_run_report(run_dir, metadata, records, verdict)
    log(f"汇总报告：{run_dir / 'report.md'}")
    if overall_error:
        raise BenchError(overall_error)
    return 0


def add_common(parser: argparse.ArgumentParser, include_plan=True):
    if include_plan:
        parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="测试计划 JSON")
        parser.add_argument("--sections", default="all", help="all 或逗号分隔章节名/编号")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(os.environ.get("CUBE_SANDBOX_ROOT", os.getcwd())),
        help="既有 CubeSandbox 源码树（也可设 CUBE_SANDBOX_ROOT）",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("CUBE_API_URL", os.environ.get("E2B_API_URL", "http://127.0.0.1:3000")),
    )
    parser.add_argument("--template-id", default=os.environ.get("CUBE_TEMPLATE_ID", ""))
    parser.add_argument("--python", default=os.environ.get("PYTHON_BIN", "python3"))
    parser.add_argument("--toolbox", type=Path, default=DEFAULT_TOOLBOX)
    parser.add_argument("--tap-target", type=int, default=500)
    parser.add_argument("--tap-evidence", default="", help="check 输出的精确超额 TAP 集合 SHA256")
    parser.add_argument("--numa-node", type=int, default=None, help="多 NUMA 机器建议显式确认")
    parser.add_argument("--cubelet-wait", type=int, default=1800, help="cubelet/TAP 恢复最长等待秒数")
    parser.add_argument("--busy-threshold", type=float, default=10.0, help="非 Cube 进程 CPU 告警阈值")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show-plan", help="展示矩阵、执行范围和确认哈希，无副作用")
    add_common(show)
    show.add_argument("--prepare", action="store_true", help="将 run 的准备副作用纳入批准范围")
    show.add_argument("--operation", choices=("run", "prepare"), default="run")

    check = subparsers.add_parser("check", help="只读预检，不修改系统")
    add_common(check)
    check.add_argument("--json", action="store_true", help="输出 JSON 而非逐行文本")
    check.add_argument("--output", type=Path, help="同时将预检 JSON 写入指定 preflight.json")

    prepare = subparsers.add_parser("prepare", help="确认后修正 NUMA/TAP、重启并等待就绪")
    add_common(prepare)
    prepare.add_argument("--output-root", type=Path, default=Path.cwd() / "cubesandbox-results")
    prepare.add_argument("--yes", action="store_true")
    prepare.add_argument("--exclusive-confirmed", action="store_true")
    prepare.add_argument("--approved-plan-hash", default="", help="show-plan --prepare 的批准范围 SHA256")

    run = subparsers.add_parser("run", help="预检、可选准备、冒烟和完整基准")
    add_common(run)
    run.add_argument("--output-root", type=Path, default=Path.cwd() / "cubesandbox-results")
    run.add_argument("--prepare", action="store_true", help="先执行 NUMA/TAP 准备和 cube-bench 构建")
    run.add_argument("--yes", action="store_true", help="确认创建/删除测试资源及 --prepare 副作用")
    run.add_argument("--exclusive-confirmed", action="store_true", help="用户已确认独占测试窗口")
    run.add_argument("--approved-plan-hash", default="", help="show-plan 输出的 SHA256")
    run.add_argument("--dry-run", action="store_true", help="只展开命令，不访问或修改环境")
    return parser


def normalize_args(args) -> dict:
    plan = load_plan(args.plan)
    sections = select_sections(plan, args.sections)
    if args.tap_target < 0:
        raise BenchError("--tap-target 必须是非负整数")
    if args.cubelet_wait < 1:
        raise BenchError("--cubelet-wait 必须是正整数")
    if not math.isfinite(args.busy_threshold) or args.busy_threshold <= 0:
        raise BenchError("--busy-threshold 必须是有限的正数")
    args.repo = args.repo.expanduser().resolve()
    args.toolbox = args.toolbox.expanduser().resolve()
    if hasattr(args, "output_root"):
        args.output_root = args.output_root.expanduser().resolve()
    if getattr(args, "output", None) is not None:
        output = args.output.expanduser()
        # The parent is the trusted output directory. Do not resolve the final
        # component: a pre-existing report symlink must reach write_json intact.
        args.output = output.parent.resolve() / output.name
    return {"plan": plan, "sections": sections}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    normalized = normalize_args(args)
    plan, sections = normalized["plan"], normalized["sections"]
    if args.command == "show-plan":
        print(plan_summary(plan, sections))
        print(approval_summary(args, plan, sections))
        return 0
    if args.command == "run" and args.dry_run:
        return dry_run(args, plan, sections)
    if args.command == "run":
        require_confirmation(args, plan, sections)
        with host_lock():
            return run_benchmark(args, plan, sections)
    if args.command == "prepare":
        require_confirmation(args, plan, sections)

    env = sanitized_environment(args.template_id, args.api_url)
    resolution_error = ""
    try:
        template_id = resolve_template(env, args.template_id)
    except BenchError as exc:
        if args.command != "check":
            raise
        template_id = args.template_id
        resolution_error = str(exc)
    env["CUBE_TEMPLATE_ID"] = template_id
    args._plan = plan
    if args.command == "prepare":
        with host_lock():
            run_dir = args.output_root / f"prepare_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
            run_dir.mkdir(parents=True, exist_ok=False)
            prepare_environment(args, env, run_dir)
            checks = run_preflight(args, env, template_id, sections)
            write_json(run_dir / "preflight.json", checks.payload())
            print(checks.render())
            log(f"准备证据：{run_dir}")
            return 0 if checks.ok else 1

    checks = run_preflight(args, env, template_id, sections)
    if resolution_error:
        checks.fail("template.resolve", resolution_error)
    payload = checks.payload()
    if args.output:
        write_json(args.output, payload, reject_symlink=True)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(checks.render())
    return 0 if checks.ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("ERROR: 用户中断；请检查日志和差集清理结果", file=sys.stderr)
        sys.exit(130)
