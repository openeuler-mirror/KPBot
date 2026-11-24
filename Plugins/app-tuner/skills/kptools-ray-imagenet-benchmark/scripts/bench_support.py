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


def effective_cpus():
    """Return affinity/cgroup-aware visible CPUs and source evidence."""
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = os.cpu_count() or 1
    evidence = {"affinity_cpus": affinity}
    visible = affinity
    try:
        quota_text, period_text = (
            Path("/sys/fs/cgroup/cpu.max").read_text(encoding="utf-8").split()
        )
        evidence["cgroup_version"] = "v2"
        if quota_text == "max":
            evidence["cgroup_cpu"] = "unlimited"
        else:
            quota = int(quota_text)
            period = int(period_text)
            quota_cpus = max(1, quota // period)
            visible = min(visible, quota_cpus)
            evidence.update({"quota": quota, "period": period})
    except (OSError, ValueError):
        try:
            quota = int(
                Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
                .read_text(encoding="utf-8")
                .strip()
            )
            period = int(
                Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
                .read_text(encoding="utf-8")
                .strip()
            )
            evidence["cgroup_version"] = "v1"
            if quota > 0:
                quota_cpus = max(1, quota // period)
                visible = min(visible, quota_cpus)
                evidence.update({"quota": quota, "period": period})
            else:
                evidence["cgroup_cpu"] = "unlimited"
        except (OSError, ValueError, ZeroDivisionError):
            evidence["cgroup_cpu"] = "unavailable"
    evidence["effective_cpus"] = visible
    return visible, evidence


def memory_available_bytes():
    """Estimate available memory from procfs and cgroup v1/v2 limits."""
    available = None
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    evidence = {"proc_mem_available_bytes": available}
    try:
        maximum_text = (
            Path("/sys/fs/cgroup/memory.max")
            .read_text(encoding="utf-8")
            .strip()
        )
        evidence["cgroup_version"] = "v2"
        if maximum_text == "max":
            evidence["cgroup_memory"] = "unlimited"
        else:
            maximum = int(maximum_text)
            current = int(
                Path("/sys/fs/cgroup/memory.current")
                .read_text(encoding="utf-8")
                .strip()
            )
            remaining = max(0, maximum - current)
            available = (
                remaining if available is None else min(available, remaining)
            )
            evidence.update(
                {
                    "cgroup_memory_max": maximum,
                    "cgroup_memory_current": current,
                }
            )
    except (OSError, ValueError):
        try:
            maximum = int(
                Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
                .read_text(encoding="utf-8")
                .strip()
            )
            current = int(
                Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
                .read_text(encoding="utf-8")
                .strip()
            )
            evidence["cgroup_version"] = "v1"
            if maximum < 1 << 60:
                remaining = max(0, maximum - current)
                available = (
                    remaining
                    if available is None
                    else min(available, remaining)
                )
                evidence.update(
                    {
                        "cgroup_memory_max": maximum,
                        "cgroup_memory_current": current,
                    }
                )
            else:
                evidence["cgroup_memory"] = "unlimited"
        except (OSError, ValueError):
            evidence["cgroup_memory"] = "unavailable"
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
