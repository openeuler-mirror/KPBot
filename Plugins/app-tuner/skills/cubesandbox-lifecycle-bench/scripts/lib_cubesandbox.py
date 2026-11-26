#!/usr/bin/env python3
"""Shared, standard-library-only helpers for CubeSandbox lifecycle benchmarks."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Optional
import urllib.error
import urllib.request

SECTION_ORDER = (
    "template_create",
    "density",
    "snapshot",
    "dirty_page",
    "snapshot_create",
    "rollback",
    "clone",
    "pause_resume",
)
SECTION_TITLES = {
    "template_create": "3.2 基于 Template 创建沙箱（延迟与并发）",
    "density": "3.3 单机部署密度（内存开销）",
    "snapshot": "4.1 Snapshot 制作并发",
    "dirty_page": "4.2 Snapshot 与 Dirty Page",
    "snapshot_create": "4.3 基于 Snapshot 创建沙箱",
    "rollback": "4.4 Rollback",
    "clone": "4.5 Clone",
    "pause_resume": "4.6 Pause / Resume",
}
SECTION_ALIASES = {
    "3.2": "template_create",
    "create": "template_create",
    "3.3": "density",
    "4.1": "snapshot",
    "4.2": "dirty_page",
    "dirty-page": "dirty_page",
    "4.3": "snapshot_create",
    "snapshot-create": "snapshot_create",
    "4.4": "rollback",
    "4.5": "clone",
    "4.6": "pause_resume",
    "pause-resume": "pause_resume",
}
PYTHON_SCRIPTS = {
    "snapshot": "bench_snapshot_concurrency.py",
    "dirty_page": "bench_snapshot_dirty.py",
    "snapshot_create": "bench_create_concurrency.py",
    "rollback": "bench_rollback_concurrency.py",
    "clone": "bench_clone_concurrency.py",
    "pause_resume": "bench_pause_resume_concurrency.py",
}
PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)
TAP_NAME_RE = re.compile(r"^\d+:\s+(192\.168\.(?:\d{1,3}\.){1}\d{1,3}):")


class BenchError(RuntimeError):
    """A user-actionable benchmark error."""


def positive_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BenchError(f"{field} 必须是正整数，当前为 {value!r}")
    return value


def nonnegative_number(value, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise BenchError(f"{field} 必须是有限的非负数，当前为 {value!r}")
    return value


def require_keys(mapping: dict, required: set[str], allowed: set[str], where: str):
    if not isinstance(mapping, dict):
        raise BenchError(f"{where} 必须是 JSON 对象")
    missing = required - set(mapping)
    unknown = set(mapping) - allowed
    if missing:
        raise BenchError(f"{where} 缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        raise BenchError(f"{where} 含未知字段：{', '.join(sorted(unknown))}")


def validate_plan(plan: dict) -> dict:
    """Validate all sections present in a plan; unknown keys are rejected."""
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise BenchError("测试计划必须是 schema_version=1 的 JSON 对象")
    require_keys(
        plan,
        {"schema_version"},
        {"schema_version", "name", *SECTION_ORDER},
        "plan",
    )
    if not any(section in plan for section in SECTION_ORDER):
        raise BenchError("测试计划至少需要包含一个测试章节")

    if "template_create" in plan:
        section = plan["template_create"]
        require_keys(
            section,
            {"mode", "warmup", "settle_secs", "cases"},
            {"description", "mode", "warmup", "settle_secs", "cases"},
            "template_create",
        )
        if section["mode"] not in ("create-delete", "create-only"):
            raise BenchError("template_create.mode 仅支持 create-delete/create-only")
        nonnegative_number(section["warmup"], "template_create.warmup")
        nonnegative_number(section["settle_secs"], "template_create.settle_secs")
        if not isinstance(section["warmup"], int):
            raise BenchError("template_create.warmup 必须是整数")
        _validate_cases(section["cases"], "template_create", {"concurrency", "requests", "rounds"})

    if "density" in plan:
        section = plan["density"]
        require_keys(
            section,
            {"max_live", "max_batch", "min_available_mb", "settle_secs", "batches"},
            {"description", "max_live", "max_batch", "min_available_mb", "settle_secs", "batches"},
            "density",
        )
        positive_int(section["max_live"], "density.max_live")
        positive_int(section["max_batch"], "density.max_batch")
        positive_int(section["min_available_mb"], "density.min_available_mb")
        nonnegative_number(section["settle_secs"], "density.settle_secs")
        if not isinstance(section["batches"], list) or not section["batches"]:
            raise BenchError("density.batches 必须是非空数组")
        total = 0
        for index, case in enumerate(section["batches"]):
            where = f"density.batches[{index}]"
            require_keys(case, {"concurrency", "requests"}, {"concurrency", "requests"}, where)
            positive_int(case["concurrency"], f"{where}.concurrency")
            requests = positive_int(case["requests"], f"{where}.requests")
            if requests > section["max_batch"]:
                raise BenchError(
                    f"{where}.requests={requests} 超过 max_batch={section['max_batch']}；"
                    "大批次缺少批内内存保护，需拆分"
                )
            total += requests
        if total > section["max_live"]:
            raise BenchError(
                f"density 累计创建 {total} 个沙箱，超过 max_live={section['max_live']}"
            )

    for name in ("snapshot", "snapshot_create", "pause_resume"):
        if name in plan:
            section = plan[name]
            require_keys(
                section,
                {"settle_secs", "cases"},
                {"description", "settle_secs", "cases"},
                name,
            )
            nonnegative_number(section["settle_secs"], f"{name}.settle_secs")
            _validate_cases(section["cases"], name, {"concurrency", "rounds"})

    if "dirty_page" in plan:
        section = plan["dirty_page"]
        require_keys(
            section,
            {"settle_secs", "cases"},
            {"description", "settle_secs", "cases"},
            "dirty_page",
        )
        nonnegative_number(section["settle_secs"], "dirty_page.settle_secs")
        _validate_cases(section["cases"], "dirty_page", {"dirty_mb", "rounds"})

    for name in ("rollback", "clone"):
        if name not in plan:
            continue
        section = plan[name]
        require_keys(
            section,
            {"dirty_mb", "settle_secs", "cases"},
            {"description", "dirty_mb", "settle_secs", "cases"},
            name,
        )
        nonnegative_number(section["dirty_mb"], f"{name}.dirty_mb")
        if not isinstance(section["dirty_mb"], int):
            raise BenchError(f"{name}.dirty_mb 必须是整数")
        nonnegative_number(section["settle_secs"], f"{name}.settle_secs")
        fields = {"num", "concurrency", "rounds"} if name == "clone" else {"concurrency", "rounds"}
        _validate_cases(section["cases"], name, fields)
        if name == "clone":
            for index, case in enumerate(section["cases"]):
                if case["concurrency"] > case["num"]:
                    raise BenchError(f"clone.cases[{index}].concurrency 不能大于 num")
    return plan


def _validate_cases(cases, section: str, fields: set[str]):
    if not isinstance(cases, list) or not cases:
        raise BenchError(f"{section}.cases 必须是非空数组")
    for index, case in enumerate(cases):
        where = f"{section}.cases[{index}]"
        require_keys(case, fields, fields, where)
        for field in fields:
            if field == "dirty_mb":
                nonnegative_number(case[field], f"{where}.{field}")
                if not isinstance(case[field], int):
                    raise BenchError(f"{where}.{field} 必须是整数")
            else:
                positive_int(case[field], f"{where}.{field}")


def load_plan(path: Path) -> dict:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchError(f"无法读取测试计划 {path}: {exc}") from exc
    return validate_plan(plan)


def canonical_plan(plan: dict) -> str:
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(canonical_plan(plan).encode("utf-8")).hexdigest()


def select_sections(plan: dict, raw: Optional[str]) -> list[str]:
    if not raw or raw == "all":
        selected = [name for name in SECTION_ORDER if name in plan]
    else:
        names = []
        for token in raw.split(","):
            token = token.strip()
            name = SECTION_ALIASES.get(token, token)
            if name not in SECTION_ORDER:
                raise BenchError(f"未知测试章节 {token!r}；可选：{', '.join(SECTION_ORDER)}")
            names.append(name)
        selected = [name for name in SECTION_ORDER if name in names]
    missing = [name for name in selected if name not in plan]
    if missing:
        raise BenchError(f"测试计划缺少所选章节：{', '.join(missing)}")
    if not selected:
        raise BenchError("未选择任何测试章节")
    return selected


def plan_summary(plan: dict, sections: list[str]) -> str:
    lines = [f"计划：{plan.get('name', 'unnamed')}", f"PLAN_SHA256：{plan_digest(plan)}", ""]
    for name in sections:
        section = plan[name]
        lines.append(f"## {SECTION_TITLES[name]}")
        if name == "template_create":
            lines.append(
                f"mode={section['mode']}, warmup={section['warmup']}, "
                f"settle={section['settle_secs']}s"
            )
            lines.append("concurrency | requests/轮 | 独立 rounds")
            for case in section["cases"]:
                lines.append(
                    f"{case['concurrency']} | {case['requests']} | {case['rounds']}"
                )
        elif name == "density":
            total = sum(case["requests"] for case in section["batches"])
            lines.append(
                f"累计 max_live={total}/{section['max_live']}，max_batch={section['max_batch']}，"
                f"min_available={section['min_available_mb']} MiB"
            )
            lines.append("批次：concurrency x requests")
            lines.append(
                "；".join(f"{c['concurrency']}x{c['requests']}" for c in section["batches"])
            )
        elif name == "dirty_page":
            lines.append("dirty_MiB | rounds/档（另有热身/暖缓存步骤）")
            for case in section["cases"]:
                lines.append(f"{case['dirty_mb']} | {case['rounds']}")
        elif name == "clone":
            lines.append(f"dirty_mb={section['dirty_mb']}；num | concurrency | rounds/档")
            for case in section["cases"]:
                lines.append(f"{case['num']} | {case['concurrency']} | {case['rounds']}")
        else:
            if name in ("rollback",):
                lines.append(f"dirty_mb={section['dirty_mb']}")
            lines.append("concurrency | rounds/档（脚本自动热身）")
            for case in section["cases"]:
                lines.append(f"{case['concurrency']} | {case['rounds']}")
        lines.append("")
    return "\n".join(lines)


def sanitized_environment(template_id: str = "", api_url: str = "") -> dict[str, str]:
    """Return a child environment with every HTTP(S)/ALL proxy variable removed."""
    env = os.environ.copy()
    for key in PROXY_VARIABLES:
        env.pop(key, None)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    if api_url:
        env["CUBE_API_URL"] = api_url.rstrip("/")
        env["E2B_API_URL"] = api_url.rstrip("/")
    api_key = env.get("CUBE_API_KEY") or env.get("E2B_API_KEY") or "e2b_000000"
    env["CUBE_API_KEY"] = api_key
    env["E2B_API_KEY"] = api_key
    env.setdefault("CUBE_PROXY_NODE_IP", "127.0.0.1")
    env.setdefault("CUBE_PROXY_PORT_HTTP", "80")
    if template_id:
        env["CUBE_TEMPLATE_ID"] = template_id
    return env


def capture(command: list[str], env=None, timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        detail = exc.strerror or "No such file or directory"
        return subprocess.CompletedProcess(
            command,
            127,
            stdout=f"ERROR: 缺少命令 {command[0]}：{detail}",
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        output += f"\nERROR: command timed out after {timeout}s"
        return subprocess.CompletedProcess(command, 124, stdout=output)


def parse_cpu_set(value: str) -> set[int]:
    cpus: set[int] = set()
    for part in value.strip().split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            cpus.update(range(int(start), int(end) + 1))
        else:
            cpus.add(int(part))
    return cpus


def numa_topology(sys_node_root: Path = Path("/sys/devices/system/node")) -> dict[int, set[int]]:
    topology = {}
    for path in sys_node_root.glob("node[0-9]*"):
        if not path.name[4:].isdigit():
            continue
        try:
            topology[int(path.name[4:])] = parse_cpu_set(
                (path / "cpulist").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
    return topology


def script_bindings(path: Path, binary_variable: str) -> list[int]:
    """Return one entry per launch line; -1 marks an unbound launch."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    bindings = []
    variable = f'"${{{binary_variable}}}"'
    launch = re.compile(
        r"^\s*(?:exec\s+)?(?:numactl\s+.*?)?" + re.escape(variable)
    )
    for line in lines:
        if variable not in line or line.lstrip().startswith("#") or not launch.match(line):
            continue
        match = re.search(r"\bnumactl\s+.*?(?:-N\s*(\d+)|--node[=\s]+(\d+))", line)
        bindings.append(int(match.group(1) or match.group(2)) if match else -1)
    return bindings


def rewrite_numa_launches(text: str, binary_variable: str, node: int) -> tuple[str, int]:
    """Idempotently bind only launch lines, never ensure_executable/assignments."""
    output = []
    changed = 0
    variable = f'"${{{binary_variable}}}"'
    pattern = re.compile(
        r"^(?P<head>\s*(?:exec\s+)?)(?:(?:numactl)\s+.*?)?(?P<tail>"
        + re.escape(variable)
        + r".*)$"
    )
    for line in text.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        if variable not in body or body.lstrip().startswith("#"):
            output.append(line)
            continue
        match = pattern.match(body)
        if not match:
            output.append(line)
            continue
        replacement = (
            f"{match.group('head')}numactl -N {node} --localalloc "
            f"{match.group('tail')}{ending}"
        )
        output.append(replacement)
        changed += replacement != line
    return "".join(output), changed


def cubelet_network_config(toolbox: Path) -> tuple[int, ipaddress.IPv4Network]:
    """Read the Cube-owned TAP pool size and CIDR from the deployed config."""
    path = toolbox / "Cubelet/config/config.toml"
    try:
        text = path.read_text(encoding="utf-8")
        counts = re.findall(r"(?m)^\s*tap_init_num\s*=\s*(\d+)\s*$", text)
        cidrs = re.findall(r"(?m)^\s*cidr\s*=\s*[\"']([^\"']+)[\"']\s*$", text)
        if len(counts) != 1 or len(cidrs) != 1:
            raise ValueError(f"tap_init_num matches={len(counts)}, cidr matches={len(cidrs)}")
        network = ipaddress.ip_network(cidrs[0], strict=True)
        if network.version != 4 or network.network_address.packed[:2] != b"\xc0\xa8":
            raise ValueError("当前执行器仅支持 192.168.0.0/16 内的 IP 命名 TAP")
        return int(counts[0]), network
    except (OSError, ValueError) as exc:
        raise BenchError(f"无法解析 Cubelet TAP 配置 {path}：{exc}") from exc


def list_ip_interfaces(env=None) -> list[dict]:
    """List IP-named interfaces with in-kernel TAP evidence, without guessing ownership."""
    result = capture(["ip", "-d", "-o", "link", "show"], env=env)
    if result.returncode != 0:
        raise BenchError(f"无法列出接口：{result.stdout.strip()}")
    interfaces = []
    for line in result.stdout.splitlines():
        match = TAP_NAME_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        try:
            address = ipaddress.ip_address(name)
        except ValueError:
            continue
        interfaces.append(
            {
                "ifindex": int(line.split(":", 1)[0]),
                "name": name,
                "address": address,
                "verified_tap": "tun type tap" in line and "persist on" in line,
            }
        )
    return interfaces


def list_taps(env=None, network=None) -> list[tuple[int, str]]:
    """Return only persistent TAPs, optionally restricted to Cube's configured CIDR."""
    taps = []
    for interface in list_ip_interfaces(env=env):
        if not interface["verified_tap"]:
            continue
        if network is not None and interface["address"] not in network:
            continue
        taps.append((interface["ifindex"], interface["name"]))
    return sorted(taps)


def tap_cleanup_candidates(taps: list[tuple[int, str]], target: int) -> list[dict]:
    """Return the exact excess interface identities that prepare may delete."""
    return [
        {"ifindex": ifindex, "name": name}
        for ifindex, name in sorted(taps)[max(0, target):]
    ]


def tap_evidence_digest(taps: list[tuple[int, str]], target: int) -> str:
    candidates = tap_cleanup_candidates(taps, target)
    encoded = json.dumps(candidates, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def service_is_active(service: str, env=None) -> bool:
    result = capture(["systemctl", "is-active", service], env=env)
    return result.returncode == 0 and result.stdout.strip() == "active"


def service_main_pid(service: str, env=None) -> int:
    result = capture(
        ["systemctl", "show", service, "--property", "MainPID", "--value"], env=env
    )
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        return 0


def process_cpus(pid: int) -> set[int]:
    if pid <= 0:
        return set()
    try:
        return set(os.sched_getaffinity(pid))
    except (OSError, AttributeError):
        return set()


def process_inventory(env=None) -> list[dict]:
    result = capture(
        ["ps", "-eo", "pid=,comm=,pcpu=,args=", "--sort=-pcpu"], env=env
    )
    if result.returncode != 0:
        raise BenchError(f"无法读取进程列表：{result.stdout.strip()}")
    processes = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        try:
            processes.append(
                {
                    "pid": int(parts[0]),
                    "comm": parts[1],
                    "cpu_percent": float(parts[2]),
                    "args": parts[3],
                }
            )
        except ValueError:
            continue
    return processes


def is_cube_or_system_process(process: dict) -> bool:
    comm = process["comm"]
    args = process["args"]
    if any(
        token in args
        for token in (
            "/cubetoolbox/",
            "/data/cubelet/",
            "cube-sandbox",
            "containerd-shim-cube-rs",
        )
    ):
        return True
    if comm.startswith(("cube", "kvm", "kworker", "rcu", "irq/", "ksoftirqd")):
        return True
    return comm in {
        "systemd",
        "systemd-journal",
        "systemd-logind",
        "systemd-network",
        "systemd-resolve",
        "dbus-daemon",
        "NetworkManager",
        "sshd",
        "bash",
        "sh",
        "ps",
        "sleep",
        "cron",
        "crond",
        "containerd",
        "dockerd",
        "docker-proxy",
        "nginx",
        "mysqld",
        "redis-server",
    }


def available_memory_mb() -> int:
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            fields[key] = value.strip()
        return int(fields["MemAvailable"].split()[0]) // 1024
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise BenchError(f"无法读取 MemAvailable：{exc}") from exc


def resolve_template(env: dict, explicit: str = "") -> str:
    if explicit:
        return explicit
    result = capture(["cubemastercli", "tpl", "list"], env=env, timeout=30)
    if result.returncode != 0:
        raise BenchError(f"无法自动选择模板：{result.stdout.strip()}")
    candidates = []
    for line in result.stdout.splitlines():
        match = re.search(r"\btpl-[A-Za-z0-9-]+\b", line)
        if match and "READY" in line.upper():
            candidates.append(match.group(0))
    if not candidates:
        raise BenchError("cubemastercli tpl list 中没有 READY 模板；请创建模板或显式指定")
    unique = list(dict.fromkeys(candidates))
    if len(unique) > 1:
        preview = ", ".join(unique[:8])
        suffix = f" 等共 {len(unique)} 个" if len(unique) > 8 else ""
        raise BenchError(
            "检测到多个 READY 模板，必须由用户确认 --template-id，不能自动猜测："
            + preview
            + suffix
        )
    return candidates[0]


class CubeAPI:
    """Direct HTTP client: proxy handlers are always disabled for measurements."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, timeout: float = 30):
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        return self.opener.open(request, timeout=timeout)

    def health(self) -> tuple[bool, str]:
        try:
            with self.request("GET", "/health", timeout=5) as response:
                return 200 <= response.status < 300, f"HTTP {response.status}"
        except Exception as exc:
            return False, str(exc)

    def list_ids(self) -> set[str]:
        """Fail closed on unknown schemas, malformed entries or incomplete pages."""
        with self.request("GET", "/sandboxes", timeout=30) as response:
            payload = json.loads(response.read())

        def unwrap(value):
            if isinstance(value, list):
                return value, None
            if not isinstance(value, dict):
                raise BenchError("GET /sandboxes 根节点不是受支持的数组/对象")
            for key in ("sandboxes", "items", "data"):
                if key not in value:
                    continue
                nested = value[key]
                if isinstance(nested, list):
                    return nested, value
                if isinstance(nested, dict):
                    for nested_key in ("sandboxes", "items"):
                        if nested_key in nested and isinstance(nested[nested_key], list):
                            return nested[nested_key], nested
            raise BenchError("GET /sandboxes 未包含可识别的 sandboxes/items/data 数组")

        items, metadata = unwrap(payload)
        containers = [value for value in (payload, metadata) if isinstance(value, dict)]
        for container in containers:
            if container.get("error"):
                raise BenchError("GET /sandboxes 返回 error 字段，拒绝按完整列表处理")
            for key in ("next_token", "nextToken", "continuation_token", "has_more", "hasMore"):
                if container.get(key):
                    raise BenchError(f"GET /sandboxes 存在未完成分页字段 {key}，拒绝按完整列表处理")
            for key in ("total", "totalCount", "total_count"):
                if key in container:
                    try:
                        total = int(container[key])
                    except (TypeError, ValueError) as exc:
                        raise BenchError(f"GET /sandboxes 的 {key} 非整数") from exc
                    if total != len(items):
                        raise BenchError(
                            f"GET /sandboxes 分页/计数不完整：{key}={total}，本页={len(items)}"
                        )
        ids = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise BenchError(f"GET /sandboxes 第 {index} 项不是对象")
            sandbox_id = item.get("sandboxID") or item.get("sandboxId") or item.get("id")
            if not isinstance(sandbox_id, str) or not sandbox_id:
                raise BenchError(f"GET /sandboxes 第 {index} 项缺少有效沙箱 ID")
            ids.add(sandbox_id)
        return ids

    def delete(self, sandbox_id: str) -> tuple[str, bool, str]:
        try:
            with self.request("DELETE", f"/sandboxes/{sandbox_id}", timeout=30) as response:
                ok = response.status in (200, 202, 204)
                return sandbox_id, ok, f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return sandbox_id, True, "already absent"
            return sandbox_id, False, f"HTTP {exc.code}: {exc.reason}"
        except Exception as exc:
            return sandbox_id, False, str(exc)

    def cleanup_difference(self, baseline: set[str], workers: int = 16) -> list[dict]:
        created = self.list_ids() - baseline
        if not created:
            return []
        # Imports are local so read-only preflight does not need thread pools.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(self.delete, sorted(created)))
        return [
            {"sandbox_id": sid, "ok": ok, "message": message}
            for sid, ok, message in outcomes
        ]


@contextmanager
def host_lock(path: Path = Path("/run/lock/cubesandbox-lifecycle-bench.lock")):
    """Serialize every mutating prepare/run on one host, including cleanup."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BenchError(f"另一个 prepare/run 持有宿主机锁 {path}，拒绝并发执行") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_json(path: Path, payload, *, reject_symlink: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not reject_symlink:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        return

    if path.is_symlink():
        raise BenchError(f"拒绝写入符号链接报告：{path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        # mkstemp uses O_EXCL and mode 0600; os.replace publishes the directory
        # entry itself and therefore never follows a racing final symlink.
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        if path.is_symlink():
            raise BenchError(f"拒绝写入符号链接报告：{path}")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def command_text(command: list[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) for part in command)


def check_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise BenchError(f"缺少命令 {name}")
    return path


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def repo_commit(repo: Path) -> str:
    result = capture(["git", "-C", str(repo), "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def python_imports(python: str, module: str, env=None) -> tuple[bool, str]:
    result = capture([python, "-c", f"import {module}; print({module}.__file__)"], env=env)
    return result.returncode == 0, result.stdout.strip()
