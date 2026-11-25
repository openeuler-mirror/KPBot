"""Self-contained prerequisite and output helpers for the Ray smoke skill."""

import importlib
import inspect
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys


EXPECTED_VERSIONS = {
    "ray": "2.56.0",
    "torch": "2.13.0",
    "torchvision": "0.28.0",
    "numpy": "2.1.3",
    "pyarrow": "23.0.1",
    "Pillow": "13.0.0",
}
IMPORT_NAMES = {
    "ray": "ray",
    "torch": "torch",
    "torchvision": "torchvision",
    "numpy": "numpy",
    "pyarrow": "pyarrow",
    "Pillow": "PIL",
}
SECRET_ENV_NAMES = (
    "RAY_CLIENT_AUTH_TOKEN",
    "RAY_AUTH_TOKEN",
    "KPBOT_RAY_AUTH_TOKEN",
)


def base_version(value):
    """Ignore a wheel-local suffix such as +cpu for profile comparison."""
    return str(value).split("+", 1)[0]


def sanitize_text(value):
    """Remove declared credentials and URL userinfo from diagnostics."""
    text = str(value)
    for name in SECRET_ENV_NAMES:
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "<redacted>")
    return re.sub(r"(ray://)[^/@\s]+@", r"\1<redacted>@", text)


def sanitize_value(value):
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def classify_error(message):
    """Map runtime failures to stable, non-secret categories."""
    text = str(message).lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    auth_words = (
        "unauthorized",
        "permission denied",
        "forbidden",
        "authentication",
        "auth token",
        "certificate",
        "ssl error",
        "401",
        "403",
    )
    if any(word in text for word in auth_words):
        return "auth"
    if "no module named" in text or "not found" in text:
        return "absent"
    connection_words = (
        "connection refused",
        "failed to connect",
        "name resolution",
        "network is unreachable",
        "no route to host",
    )
    if any(word in text for word in connection_words):
        return "unreachable"
    return "runtime_error"


def probe(name, status, reason, evidence=None, mandatory=True):
    return {
        "name": name,
        "mandatory": mandatory,
        "status": status,
        "reason": reason,
        "evidence": evidence or {},
    }


def write_bytes_exclusive(path, content):
    """Create one mode-0600 file and reject every existing directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def write_text_exclusive(path, content):
    write_bytes_exclusive(path, content.encode("utf-8"))


def write_json_exclusive(path, document):
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    write_text_exclusive(path, content)


def validate_version_profile(versions):
    """Return mismatches for the runtime profile exercised by this skill."""
    problems = []
    python_actual = versions.get("python")
    if python_actual is None or not str(python_actual).startswith("3.11."):
        problems.append(f"python: actual={python_actual!r}, required=3.11.x")
    for name, expected in EXPECTED_VERSIONS.items():
        actual = versions.get(name)
        if actual is None:
            problems.append(f"{name}: absent, required base={expected}")
        elif base_version(actual) != expected:
            problems.append(
                f"{name}: actual={actual}, required base={expected}"
            )
    return problems


def _unescape_mountinfo_path(value):
    """Decode octal escapes used by procfs mountinfo paths."""
    return re.sub(
        r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value
    )


def _cgroup_memberships(proc_root):
    """Return this process's v2 and per-controller v1 cgroup paths."""
    memberships = {}
    for line in (proc_root / "self/cgroup").read_text(
        encoding="utf-8"
    ).splitlines():
        fields = line.strip().split(":", 2)
        if len(fields) != 3 or not fields[2].startswith("/"):
            continue
        hierarchy, controllers, path = fields
        if hierarchy == "0" and not controllers:
            memberships["v2"] = path
        for controller in controllers.split(","):
            if controller in {"cpu", "memory"}:
                memberships[controller] = path
    return memberships


def _cgroup_mounts(proc_root):
    """Map cgroup controllers to their mount roots and visible mount points."""
    mounts = {"v2": [], "cpu": [], "memory": []}
    for line in (proc_root / "self/mountinfo").read_text(
        encoding="utf-8"
    ).splitlines():
        left, separator, right = line.partition(" - ")
        fields = left.split()
        details = right.split()
        if not separator or len(fields) < 6 or len(details) < 3:
            continue
        root = _unescape_mountinfo_path(fields[3])
        mount_point = _unescape_mountinfo_path(fields[4])
        filesystem = details[0]
        if filesystem == "cgroup2":
            mounts["v2"].append((root, Path(mount_point)))
        elif filesystem == "cgroup":
            controllers = set(details[2].split(","))
            controllers.update(fields[5].split(","))
            for controller in ("cpu", "memory"):
                if controller in controllers:
                    mounts[controller].append((root, Path(mount_point)))
    return mounts


def _visible_cgroup_paths(membership, mounts):
    """Translate a procfs cgroup path through each matching mount namespace."""
    paths = []
    for mount_root, mount_point in mounts:
        prefix = mount_root.rstrip("/")
        if membership != mount_root and not membership.startswith(prefix + "/"):
            continue
        relative = membership[len(mount_root):].lstrip("/")
        current = mount_point / relative if relative else mount_point
        visible = []
        while True:
            visible.append(current)
            if current == mount_point:
                break
            current = current.parent
        paths.extend(visible)
    return paths


def _cgroup_paths(proc_root, controller):
    """Return current and visible ancestor directories for one controller."""
    memberships = _cgroup_memberships(proc_root)
    mounts = _cgroup_mounts(proc_root)
    if "v2" in memberships:
        paths = _visible_cgroup_paths(memberships["v2"], mounts["v2"])
        if paths:
            return "v2", paths
    if controller not in memberships:
        return controller, []
    return controller, _visible_cgroup_paths(
        memberships[controller], mounts[controller]
    )


def effective_cpus(proc_root=Path("/proc")):
    """Return affinity and current/visible-ancestor cgroup CPU limits."""
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = os.cpu_count() or 1
    evidence = {"affinity_cpus": affinity}
    visible = affinity
    try:
        hierarchy, paths = _cgroup_paths(proc_root, "cpu")
        evidence["cgroup_version"] = hierarchy
        observations = []
        for path in paths:
            if hierarchy == "v2":
                fields = (path / "cpu.max").read_text(
                    encoding="utf-8"
                ).split()
                if len(fields) != 2:
                    raise ValueError("invalid cpu.max")
                quota_text, period_text = fields
                quota = None if quota_text == "max" else int(quota_text)
                period = int(period_text)
            else:
                quota = int(
                    (path / "cpu.cfs_quota_us").read_text(
                        encoding="utf-8"
                    ).strip()
                )
                period = int(
                    (path / "cpu.cfs_period_us").read_text(
                        encoding="utf-8"
                    ).strip()
                )
                if quota < 0:
                    quota = None
            observation = {"path": str(path), "period": period}
            if quota is not None:
                if period <= 0:
                    raise ValueError("cgroup CPU period must be positive")
                quota_cpus = max(1, quota // period)
                visible = min(visible, quota_cpus)
                observation.update(quota=quota, effective_cpus=quota_cpus)
            else:
                observation["quota"] = "unlimited"
            observations.append(observation)
        evidence["cgroup_cpu_hierarchy"] = observations
        if not paths:
            evidence["cgroup_cpu"] = "unavailable"
    except (OSError, ValueError, ZeroDivisionError) as exc:
        evidence["cgroup_cpu"] = "unavailable"
        evidence["cgroup_cpu_error"] = sanitize_text(
            f"{type(exc).__name__}: {exc}"
        )
    evidence["effective_cpus"] = visible
    return visible, evidence


def memory_available_bytes(proc_root=Path("/proc")):
    """Limit procfs memory by current and visible-ancestor cgroup limits."""
    available = None
    try:
        for line in (proc_root / "meminfo").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    evidence = {"proc_mem_available_bytes": available}
    try:
        hierarchy, paths = _cgroup_paths(proc_root, "memory")
        evidence["cgroup_version"] = hierarchy
        observations = []
        for path in paths:
            if hierarchy == "v2":
                maximum_text = (path / "memory.max").read_text(
                    encoding="utf-8"
                ).strip()
                maximum = (
                    None if maximum_text == "max" else int(maximum_text)
                )
                current = int(
                    (path / "memory.current").read_text(
                        encoding="utf-8"
                    ).strip()
                )
            else:
                maximum = int(
                    (path / "memory.limit_in_bytes").read_text(
                        encoding="utf-8"
                    ).strip()
                )
                current = int(
                    (path / "memory.usage_in_bytes").read_text(
                        encoding="utf-8"
                    ).strip()
                )
                if maximum >= 1 << 60:
                    maximum = None
            observation = {"path": str(path), "current": current}
            if maximum is not None:
                remaining = max(0, maximum - current)
                available = (
                    remaining if available is None else min(available, remaining)
                )
                observation.update(maximum=maximum, remaining=remaining)
            else:
                observation["maximum"] = "unlimited"
            observations.append(observation)
        evidence["cgroup_memory_hierarchy"] = observations
        if not paths:
            evidence["cgroup_memory"] = "unavailable"
    except (OSError, ValueError) as exc:
        evidence["cgroup_memory"] = "unavailable"
        evidence["cgroup_memory_error"] = sanitize_text(
            f"{type(exc).__name__}: {exc}"
        )
    evidence["effective_available_bytes"] = available
    return available, evidence


def runtime_prerequisites(args):
    """Recheck live packages, real transforms and resources in this process."""
    probes = []
    versions = {"python": platform.python_version()}
    modules = {}
    for distribution, import_name in IMPORT_NAMES.items():
        try:
            module = importlib.import_module(import_name)
            modules[distribution] = module
            versions[distribution] = str(module.__version__)
        except (ImportError, OSError, RuntimeError, AttributeError) as exc:
            probes.append(
                probe(
                    f"runtime.package.{distribution}",
                    "blocked",
                    classify_error(exc),
                    {"error": sanitize_text(f"{type(exc).__name__}: {exc}")},
                )
            )
    mismatches = validate_version_profile(versions)
    probes.append(
        probe(
            "runtime.tested_version_profile",
            "ready" if not mismatches else "blocked",
            "ok" if not mismatches else "wrong_version",
            {
                "actual": versions,
                "required_base": EXPECTED_VERSIONS,
                "mismatches": mismatches,
            },
        )
    )
    torch = modules.get("torch")
    if torch is not None:
        cuda_build = getattr(torch.version, "cuda", "unknown")
        cpu_only = cuda_build is None
        probes.append(
            probe(
                "runtime.torch_cpu_build",
                "ready" if cpu_only else "blocked",
                "ok" if cpu_only else "wrong_version",
                {"torch_version_cuda": cuda_build},
            )
        )
    try:
        import numpy as np
        from PIL import Image
        import ray
        from ray.data import ActorPoolStrategy
        from torchvision.models import ResNet18_Weights

        from_items = inspect.signature(ray.data.from_items).parameters
        map_batches = inspect.signature(
            ray.data.Dataset.map_batches
        ).parameters
        if "override_num_blocks" not in from_items:
            raise ValueError("Ray from_items lacks override_num_blocks")
        required = {"batch_size", "batch_format", "compute", "num_cpus"}
        if not required.issubset(map_batches):
            raise ValueError(
                "Ray Dataset.map_batches lacks exercised arguments"
            )
        strategy = ActorPoolStrategy(size=1)
        bounds = tuple(
            getattr(strategy, name, None)
            for name in ("min_size", "max_size", "initial_size")
        )
        if bounds != (1, 1, 1):
            raise ValueError(
                "ActorPoolStrategy(size=1) bounds were not honored"
            )
        transform = ResNet18_Weights.DEFAULT.transforms()
        pixels = bytes((index * 17) % 256 for index in range(32 * 32 * 3))
        image = Image.frombytes("RGB", (32, 32), pixels)
        first = transform(image.convert("RGB")).numpy()
        second = transform(image.convert("RGB")).numpy()
        if first.shape != (3, 224, 224) or first.dtype != np.float32:
            raise ValueError("unexpected ResNet18 preprocessing tensor")
        if not np.isfinite(first).all() or not np.array_equal(first, second):
            raise ValueError("preprocessing is not finite/deterministic")
        probes.append(
            probe(
                "runtime.exercised_apis",
                "ready",
                "ok",
                {
                    "tensor_shape": list(first.shape),
                    "tensor_dtype": str(first.dtype),
                    "weight_download": False,
                },
            )
        )
    except (
        ImportError,
        OSError,
        RuntimeError,
        ValueError,
        AttributeError,
    ) as exc:
        probes.append(
            probe(
                "runtime.exercised_apis",
                "blocked",
                classify_error(exc),
                {"error": sanitize_text(f"{type(exc).__name__}: {exc}")},
            )
        )
    linux = sys.platform.startswith("linux")
    probes.append(
        probe(
            "resource.linux",
            "ready" if linux else "blocked",
            "ok" if linux else "runtime_error",
            {"actual": sys.platform},
        )
    )
    cpus, cpu_evidence = effective_cpus()
    cpu_ok = cpus >= args.cpus
    cpu_evidence["required_cpus"] = args.cpus
    probes.append(
        probe(
            "resource.cpus",
            "ready" if cpu_ok else "blocked",
            "ok" if cpu_ok else "resource",
            cpu_evidence,
        )
    )
    if not args.address:
        required_shm = args.object_store_mib * 1024 * 1024
        try:
            usage = shutil.disk_usage("/dev/shm")
            shm_ok = usage.total >= required_shm and usage.free >= required_shm
            probes.append(
                probe(
                    "resource.shared_memory",
                    "ready" if shm_ok else "blocked",
                    "ok" if shm_ok else "resource",
                    {
                        "total_bytes": usage.total,
                        "free_bytes": usage.free,
                        "required_bytes": required_shm,
                    },
                )
            )
        except OSError as exc:
            probes.append(
                probe(
                    "resource.shared_memory",
                    "blocked",
                    "absent",
                    {"error": sanitize_text(exc)},
                )
            )
    available, memory_evidence = memory_available_bytes()
    required_memory = args.min_memory_mib * 1024 * 1024
    memory_evidence["required_bytes"] = required_memory
    if available is None:
        memory_status = "unknown"
        memory_reason = "unknown"
    else:
        memory_status = "ready" if available >= required_memory else "blocked"
        memory_reason = "ok" if available >= required_memory else "resource"
    probes.append(
        probe("resource.memory", memory_status, memory_reason, memory_evidence)
    )
    return probes, versions
