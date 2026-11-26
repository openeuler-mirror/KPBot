#!/usr/bin/env python3
"""Read-only prerequisite probes for the Ray ImageNet smoke benchmark."""

import argparse
from datetime import datetime, timezone
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys


SCHEMA_VERSION = 1
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
CHILD_ENV_NAMES = frozenset(
    {
        "RAY_USE_TLS",
        "RAY_TLS_SERVER_CERT",
        "RAY_TLS_SERVER_KEY",
        "RAY_TLS_CA_CERT",
        "RAY_AUTH_TOKEN",
        "RAY_CLIENT_AUTH_TOKEN",
        "RAY_TMPDIR",
        "RAY_USAGE_STATS_ENABLED",
        "RAY_DEDUP_LOGS",
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "LD_LIBRARY_PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONHASHSEED",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    }
)


def child_environment(extra=None):
    """Build a fresh runtime-only environment without mutating os.environ."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in CHILD_ENV_NAMES
    }
    if extra:
        environment.update(extra)
    return environment


def probe(name, mandatory, status, reason, evidence=None):
    """Return one serializable probe result."""
    return {
        "name": name,
        "mandatory": mandatory,
        "status": status,
        "reason": reason,
        "evidence": evidence or {},
    }


def base_version(value):
    """Ignore a wheel-local suffix such as +cpu when comparing profiles."""
    return str(value).split("+", 1)[0]


def runtime_version_mismatches(versions):
    """Return every missing or unsupported package in a worker runtime."""
    mismatches = []
    if not str(versions.get("python", "")).startswith("3.11."):
        mismatches.append("python")
    for name, expected in EXPECTED_VERSIONS.items():
        if base_version(versions.get(name)) != expected:
            mismatches.append(name)
    return mismatches


def secret_values():
    """Read only declared Ray credential variables, never the whole env."""
    return [
        os.environ[name] for name in SECRET_ENV_NAMES if os.environ.get(name)
    ]


def sanitize_text(value, secrets=None):
    """Remove declared credentials and URL userinfo from diagnostic text."""
    text = str(value)
    for secret in secrets if secrets is not None else secret_values():
        text = text.replace(secret, "<redacted>")
    return re.sub(r"(ray://)[^/@\s]+@", r"\1<redacted>@", text)


def sanitize_value(value):
    """Recursively sanitize strings in a JSON-compatible value."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def classify_error(message):
    """Map failures to stable, non-secret diagnostic categories."""
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
    absent_words = ("no module named", "not found", "no such file")
    if any(word in text for word in absent_words):
        return "absent"
    connection_words = (
        "connection refused",
        "connection error",
        "failed to connect",
        "name resolution",
        "network is unreachable",
        "no route to host",
    )
    if any(word in text for word in connection_words):
        return "unreachable"
    return "error"


def kill_owned_group(process):
    """Terminate only a process group created by this script."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # The group leader may exit while a child still ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait()


def run_process(command, timeout):
    """Run a bounded subprocess without a shell and capture diagnostics."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=child_environment(),
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return {
            "ok": False,
            "reason": "absent"
            if isinstance(exc, FileNotFoundError)
            else "error",
            "error": sanitize_text(f"{type(exc).__name__}: {exc}"),
        }
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_owned_group(process)
        return {
            "ok": False,
            "reason": "timeout",
            "error": f"probe exceeded finite timeout {timeout} seconds",
        }
    return {
        "ok": process.returncode == 0,
        "reason": "ok" if process.returncode == 0 else "error",
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": sanitize_text(stderr[-8192:]),
    }


def decode_worker_json(stdout):
    """Decode the final JSON line, tolerating third-party import banners."""
    for line in reversed(stdout.splitlines()):
        if line.strip():
            return json.loads(line)
    raise ValueError("probe worker returned no JSON")


def run_worker(command, timeout, name, mandatory=True):
    """Run one JSON worker and normalize transport/worker failures."""
    outcome = run_process(command, timeout)
    if not outcome["ok"]:
        reason = outcome["reason"]
        detail = outcome.get("error", "")
        if not detail:
            detail = outcome.get("stderr", "probe worker failed")
            reason = classify_error(detail)
        return {
            "probes": [
                probe(
                    name,
                    mandatory,
                    "blocked",
                    reason,
                    {"error": sanitize_text(detail)},
                )
            ],
            "versions": {},
        }
    try:
        document = decode_worker_json(outcome["stdout"])
        if not isinstance(document.get("probes"), list):
            raise ValueError("worker JSON has no probes array")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "probes": [
                probe(
                    name,
                    mandatory,
                    "blocked",
                    "error",
                    {"error": sanitize_text(exc)},
                )
            ],
            "versions": {},
        }
    document["probes"] = sanitize_value(document["probes"])
    document["versions"] = sanitize_value(document.get("versions", {}))
    if outcome.get("stderr"):
        document["probes"].append(
            probe(
                f"{name}.diagnostics",
                False,
                "unknown",
                "worker_stderr",
                {"text": outcome["stderr"]},
            )
        )
    return document


def run_local_runtime(python_executable, timeout):
    """Inspect imports and actual API calls without starting Ray."""
    return run_worker(
        [python_executable, str(Path(__file__).resolve()), "--runtime-worker"],
        timeout,
        "runtime.worker",
    )


def run_cluster_probe(python_executable, address, timeout, cpus):
    """Connect to an explicitly named existing Ray service."""
    env = child_environment(
        {
            "KPBOT_RAY_PROBE_ADDRESS": address,
            "KPBOT_RAY_PROBE_TIMEOUT": str(timeout),
            "KPBOT_RAY_PROBE_CPUS": str(cpus),
        }
    )
    command = [
        python_executable,
        str(Path(__file__).resolve()),
        "--cluster-worker",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return {
            "probes": [
                probe(
                    "existing_cluster",
                    True,
                    "blocked",
                    "absent"
                    if isinstance(exc, FileNotFoundError)
                    else "error",
                    {"error": sanitize_text(exc)},
                )
            ],
            "versions": {},
        }
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_owned_group(process)
        return {
            "probes": [
                probe(
                    "existing_cluster",
                    True,
                    "blocked",
                    "timeout",
                    {"error": "existing-cluster probe timed out"},
                )
            ],
            "versions": {},
        }
    if process.returncode != 0:
        detail = sanitize_text(stderr[-8192:] or stdout[-8192:])
        return {
            "probes": [
                probe(
                    "existing_cluster",
                    True,
                    "blocked",
                    classify_error(detail),
                    {"error": detail, "returncode": process.returncode},
                )
            ],
            "versions": {},
        }
    try:
        document = decode_worker_json(stdout)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "probes": [
                probe(
                    "existing_cluster",
                    True,
                    "blocked",
                    "error",
                    {"error": sanitize_text(exc)},
                )
            ],
            "versions": {},
        }
    document = sanitize_value(document)
    if stderr.strip():
        document["probes"].append(
            probe(
                "existing_cluster.diagnostics",
                False,
                "unknown",
                "worker_stderr",
                {"text": sanitize_text(stderr[-8192:])},
            )
        )
    return document


def runtime_worker():
    """Execute package/interface probes in a disposable Python process."""
    probes = []
    versions = {}
    python_version = platform.python_version()
    versions["python"] = python_version
    python_ok = sys.version_info[:2] == (3, 11)
    probes.append(
        probe(
            "runtime.python",
            True,
            "ready" if python_ok else "blocked",
            "ok" if python_ok else "wrong_version",
            {"actual": python_version, "required": "3.11.x"},
        )
    )
    modules = {}
    for distribution, import_name in IMPORT_NAMES.items():
        try:
            module = importlib.import_module(import_name)
            modules[distribution] = module
            actual = str(module.__version__)
            versions[distribution] = actual
            matches = base_version(actual) == EXPECTED_VERSIONS[distribution]
            probes.append(
                probe(
                    f"runtime.package.{distribution}",
                    True,
                    "ready" if matches else "blocked",
                    "ok" if matches else "wrong_version",
                    {
                        "actual": actual,
                        "required_base": EXPECTED_VERSIONS[distribution],
                    },
                )
            )
        except (ImportError, OSError, RuntimeError, AttributeError) as exc:
            reason = classify_error(exc)
            probes.append(
                probe(
                    f"runtime.package.{distribution}",
                    True,
                    "blocked",
                    reason,
                    {"error": sanitize_text(f"{type(exc).__name__}: {exc}")},
                )
            )
    torch = modules.get("torch")
    if torch is not None:
        cuda_build = getattr(torch.version, "cuda", "unknown")
        cpu_only = cuda_build is None
        probes.append(
            probe(
                "runtime.torch_cpu_build",
                True,
                "ready" if cpu_only else "blocked",
                "ok" if cpu_only else "wrong_version",
                {
                    "torch_version_cuda": cuda_build,
                    "required": "CPU-only wheel (torch.version.cuda is null)",
                },
            )
        )
    try:
        import numpy as np
        from PIL import Image
        import ray
        from ray.data import ActorPoolStrategy
        from torchvision.models import ResNet18_Weights

        from_items_parameters = inspect.signature(
            ray.data.from_items
        ).parameters
        map_batches_parameters = inspect.signature(
            ray.data.Dataset.map_batches
        ).parameters
        required_from_items = {"override_num_blocks"}
        required_map_batches = {
            "batch_size",
            "batch_format",
            "compute",
            "num_cpus",
        }
        if not required_from_items.issubset(from_items_parameters):
            raise ValueError("Ray from_items lacks override_num_blocks")
        if not required_map_batches.issubset(map_batches_parameters):
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
        first = transform(image.convert("RGB"))
        second = transform(image.convert("RGB"))
        array = first.numpy()
        if array.shape != (3, 224, 224) or array.dtype != np.float32:
            raise ValueError(
                "ResNet18 preprocessing returned an invalid tensor"
            )
        if not np.isfinite(array).all() or not np.array_equal(
            array, second.numpy()
        ):
            raise ValueError(
                "ResNet18 preprocessing is not finite/deterministic"
            )
        probes.append(
            probe(
                "runtime.exercised_apis",
                True,
                "ready",
                "ok",
                {
                    "ray_interfaces": [
                        "ray.data.from_items(override_num_blocks=...)",
                        "Dataset.map_batches(batch_size,batch_format,compute,num_cpus)",
                        "ActorPoolStrategy(size=1)",
                    ],
                    "torchvision_transform": (
                        "ResNet18_Weights.DEFAULT.transforms()"
                    ),
                    "tensor_shape": list(array.shape),
                    "tensor_dtype": str(array.dtype),
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
                True,
                "blocked",
                classify_error(exc),
                {"error": sanitize_text(f"{type(exc).__name__}: {exc}")},
            )
        )
    print(json.dumps({"probes": probes, "versions": versions}))
    return 0


def cluster_worker():
    """Read existing-cluster resources and execute one tiny version task."""
    import ray

    address = os.environ["KPBOT_RAY_PROBE_ADDRESS"]
    timeout = float(os.environ["KPBOT_RAY_PROBE_TIMEOUT"])
    required_cpus = int(os.environ["KPBOT_RAY_PROBE_CPUS"])
    connected = False
    try:
        ray.init(
            address=address,
            include_dashboard=False,
            namespace=f"kptools-read-only-probe-{os.getpid()}",
        )
        connected = True

        @ray.remote
        def read_runtime():
            import importlib
            import platform

            import_names = {
                "ray": "ray",
                "torch": "torch",
                "torchvision": "torchvision",
                "numpy": "numpy",
                "pyarrow": "pyarrow",
                "Pillow": "PIL",
            }
            versions = {"python": platform.python_version()}
            errors = {}
            for distribution, import_name in import_names.items():
                try:
                    module = importlib.import_module(import_name)
                    versions[distribution] = str(module.__version__)
                except (ImportError, OSError, RuntimeError, AttributeError) as exc:
                    errors[distribution] = f"{type(exc).__name__}: {exc}"
            return {
                "versions": versions,
                "errors": errors,
                "marker": sum(range(8)),
            }

        evidence = ray.get(
            read_runtime.remote(), timeout=max(1.0, min(timeout, 30.0))
        )
        resources = dict(ray.cluster_resources())
        worker_versions = evidence.get("versions", {})
        actual_version = str(worker_versions.get("ray"))
        mismatches = runtime_version_mismatches(worker_versions)
        version_ok = not mismatches and not evidence.get("errors")
        marker_ok = evidence.get("marker") == 28
        resource_ok = float(resources.get("CPU", 0)) >= required_cpus
        ready = version_ok and marker_ok and resource_ok
        if not version_ok:
            reason = "wrong_version"
        elif not resource_ok:
            reason = "resource"
        elif not marker_ok:
            reason = "error"
        else:
            reason = "ok"
        result = {
            "probes": [
                probe(
                    "existing_cluster",
                    True,
                    "ready" if ready else "blocked",
                    reason,
                    {
                        "address": address,
                        "actual_ray": actual_version,
                        "required_ray_base": EXPECTED_VERSIONS["ray"],
                        "worker_versions": worker_versions,
                        "worker_import_errors": evidence.get("errors", {}),
                        "worker_version_mismatches": mismatches,
                        "remote_marker_correct": marker_ok,
                        "cluster_resources": resources,
                        "required_cpus": required_cpus,
                        "note": "tiny worker runtime task only; no business data",
                    },
                )
            ],
            "versions": {
                "cluster_ray": actual_version,
                "cluster_worker": worker_versions,
            },
        }
        print(json.dumps(result))
        # A structured wrong-version/resource result is still valid evidence;
        # the outer report, not this worker's exit status, decides readiness.
        return 0
    finally:
        if connected:
            # Disconnect this driver; never stop the existing Ray service.
            ray.shutdown()


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


def resource_probes(args):
    """Check only resources consumed by the bounded local benchmark."""
    probes = []
    linux = sys.platform.startswith("linux")
    probes.append(
        probe(
            "resource.linux",
            True,
            "ready" if linux else "blocked",
            "ok" if linux else "error",
            {"actual": sys.platform, "required": "Linux"},
        )
    )
    cpus, cpu_evidence = effective_cpus()
    cpu_ok = cpus >= args.cpus
    cpu_evidence["required_cpus"] = args.cpus
    probes.append(
        probe(
            "resource.cpus",
            True,
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
                    True,
                    "ready" if shm_ok else "blocked",
                    "ok" if shm_ok else "resource",
                    {
                        "path": "/dev/shm",
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
                    True,
                    "blocked",
                    "absent",
                    {"error": sanitize_text(exc), "required_bytes": required_shm},
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
        probe(
            "resource.memory",
            True,
            memory_status,
            memory_reason,
            memory_evidence,
        )
    )
    try:
        disk = shutil.disk_usage(args.output.parent)
        required_disk = 512 * 1024 * 1024
        disk_ok = disk.free >= required_disk
        probes.append(
            probe(
                "resource.temporary_disk",
                True,
                "ready" if disk_ok else "blocked",
                "ok" if disk_ok else "resource",
                {
                    "probe_directory": str(args.output.parent),
                    "free_bytes": disk.free,
                    "required_bytes": required_disk,
                },
            )
        )
    except OSError as exc:
        probes.append(
            probe(
                "resource.temporary_disk",
                True,
                "blocked",
                classify_error(exc),
                {"error": sanitize_text(exc)},
            )
        )
    return probes


def overall_status(probes):
    """Mandatory failures block; missing mandatory evidence is unknown."""
    if any(
        item["mandatory"] and item["status"] == "blocked" for item in probes
    ):
        return "blocked"
    if any(
        item["mandatory"] and item["status"] == "unknown" for item in probes
    ):
        return "unknown"
    return "ready"


def validate_address(address):
    """Reject local startup and endpoint-embedded credentials."""
    if address is None:
        return
    if (
        address == "local"
        or not address.strip()
        or any(char in address for char in ("@", "?", "#", "\n", "\r"))
    ):
        raise ValueError(
            "--address must name an existing Ray endpoint without userinfo, "
            "query or fragment; use secure Ray environment configuration"
        )


def write_json_exclusive(path, document):
    """Create parents and a new mode-0600 JSON file, never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def finite_timeout(value):
    number = float(value)
    if not math.isfinite(number) or not 0.1 <= number <= 120:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and in 0.1..120"
        )
    return number


def bounded_int(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be in {minimum}..{maximum}"
            )
        return number

    return parse


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable or "python3")
    parser.add_argument("--address")
    parser.add_argument("--timeout", type=finite_timeout, default=30.0)
    parser.add_argument("--cpus", type=bounded_int(2, 32), default=2)
    parser.add_argument(
        "--object-store-mib", type=bounded_int(75, 4096), default=256
    )
    parser.add_argument(
        "--min-memory-mib", type=bounded_int(1024, 65536), default=2048
    )
    return parser


def collect_report(args):
    """Collect all requested evidence before choosing the overall status."""
    probes = []
    versions = {}
    local = run_local_runtime(args.python, args.timeout)
    probes.extend(local["probes"])
    versions.update(local["versions"])
    probes.extend(resource_probes(args))
    if args.address:
        cluster = run_cluster_probe(
            args.python, args.address, args.timeout, args.cpus
        )
        probes.extend(cluster["probes"])
        versions.update(cluster["versions"])
    status = overall_status(probes)
    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "kptools-ray-imagenet-environment-check",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "existing-cluster" if args.address else "local-runtime",
        "status": status,
        "configuration": {
            "address": args.address,
            "cpus": args.cpus,
            "object_store_mib": args.object_store_mib,
            "min_memory_mib": args.min_memory_mib,
            "timeout_seconds": args.timeout,
            "secure_ray_environment_keys_present": [
                name for name in SECRET_ENV_NAMES if os.environ.get(name)
            ],
        },
        "versions": versions,
        "probes": probes,
        "warnings": [
            item["name"]
            for item in probes
            if not item["mandatory"] and item["status"] != "ready"
        ],
        "limitations": [
            "ready covers only the declared benchmark prerequisites",
            "local ready does not start Ray or prove end-to-end execution",
            "this is not a production-health, capacity or accuracy assessment",
        ],
    }


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["--runtime-worker"]:
        return runtime_worker()
    if argv == ["--cluster-worker"]:
        try:
            return cluster_worker()
        except (
            Exception
        ) as exc:  # Worker boundary must emit structured evidence.
            print(
                sanitize_text(f"{type(exc).__name__}: {exc}"),
                file=sys.stderr,
            )
            return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_address(args.address)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output.exists():
        print(f"output already exists: {args.output}", file=sys.stderr)
        return 2
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report = collect_report(args)
        write_json_exclusive(args.output, report)
    except FileExistsError:
        print(f"output already exists: {args.output}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(sanitize_text(f"cannot write report: {exc}"), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] == "ready":
        return 0
    if report["status"] == "unknown":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
