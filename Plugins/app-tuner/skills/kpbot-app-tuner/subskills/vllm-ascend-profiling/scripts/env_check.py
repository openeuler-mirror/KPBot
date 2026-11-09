#!/usr/bin/env python3
"""Stage 1 — check environment availability for vllm-ascend profiling.

Primary mode (stage-1 flow): connect to the docker container that runs the vLLM
service and verify the running process is profiling-ready.

  1. docker container reachable & running
  2. vllm process present  -> pid via `ps -ef | grep vllm`
  3. PYTHONMALLOC=malloc in `cat /proc/<vllm-pid>/environ`
  4. vllm_ttft_fork: PYTHONPATH of the vllm process resolves to a vllm
     checkout that carries the rosinL/vllm@5d1d24c "add ttft breakdown"
     markers (see references/vllm_fork.json), AND the pip vllm package
     reports the expected baseline version (prefix 0.23.0, known-good
     0.23.0+empty)
  5. vllm_ascend: installed vllm-ascend is the source build of
     vllm-project/vllm-ascend@7d73d9d63 (pip version contains +g7d73d9d63)
     AND carries the TTFT timing patch markers (references/vllm_ascend.json)
  6. config fields via `ps -ef | grep vllm` cmdline / referenced config file:
        required: torch_profiler_with_stack=true
        info only: max_iterations, warmup_iterations (reported, not gated)

Usage:
  python3 env_check.py --container <name_or_id> [--api-base URL] [--service]
  python3 env_check.py --local          # legacy host-side checks (optional)

Every result is JSON. Fixable issues are reported with `fix_hint`; this stage
never mutates the container or the vLLM process. Fixes (restart vLLM with the
right env/config) belong to the user.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

FORK_REF_PATH = Path(__file__).resolve().parent.parent / "references" / "vllm_fork.json"
VLLM_ASCEND_REF_PATH = Path(__file__).resolve().parent.parent / "references" / "vllm_ascend.json"


def _load_json_ref(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_fork_ref() -> dict[str, Any] | None:
    return _load_json_ref(FORK_REF_PATH)


def load_vllm_ascend_ref() -> dict[str, Any] | None:
    return _load_json_ref(VLLM_ASCEND_REF_PATH)


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def docker_exec(container: str, inner: str, timeout: int = 20) -> tuple[int, str]:
    # NOTE: bash -c (NOT -lc). A login shell sources /etc/profile, and a broken
    # profile (e.g. referencing a missing CANN set_env.sh) emits noise lines
    # that corrupt exact-match parsing. Probing never needs login-shell env.
    return _run(["docker", "exec", container, "bash", "-c", inner], timeout=timeout)


# ---------------------------------------------------------------------------
# Stage-1 checks (docker container)
# ---------------------------------------------------------------------------

def _resolve_container(explicit: str) -> str:
    """Use the explicit container or auto-detect the vLLM one (see container_utils)."""
    if explicit:
        return explicit
    import container_utils

    det = container_utils.detect_vllm_container()
    if "error" in det:
        print(
            json.dumps(
                {"error": det["error"], "candidates": det.get("candidates", []),
                 "hint": "pass --container explicitly"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        json.dumps({"container_detected": det["container"], "detail": det["matched_by"]}, ensure_ascii=False),
        file=sys.stderr,
    )
    return det["container"]


def check_docker_daemon() -> dict[str, Any]:
    code, out = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    ok = code == 0
    return {
        "name": "docker_daemon",
        "status": "pass" if ok else "fail",
        "detail": f"server={out}" if ok else out[:300],
        "fixable": False,
        "fix_hint": "" if ok else "Install/start Docker or fix DOCKER_HOST access",
    }


def check_container_running(container: str) -> dict[str, Any]:
    code, out = _run(
        ["docker", "inspect", "-f", "{{.State.Status}} {{.State.Running}}", container]
    )
    ok = code == 0 and "running true" in out
    return {
        "name": "container_running",
        "status": "pass" if ok else "fail",
        "detail": f"container={container} state={out}" if code == 0 else out[:300],
        "fixable": False,
        "fix_hint": (
            ""
            if ok
            else f"Container {container!r} not running. Start the vLLM docker container first."
        ),
    }


def list_vllm_processes(container: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    code, out = docker_exec(container, "ps -ef | grep vllm | grep -v grep")
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 7)
        # only accept real process rows: ps prints numeric pid/ppid; shell noise
        # lines (e.g. broken /etc/profile errors) must not become phantom pids
        if len(parts) < 8 or not parts[1].isdigit():
            continue
        rows.append(
            {"pid": parts[1], "ppid": parts[2], "cmdline": parts[7] if len(parts) > 7 else line}
        )
    ok = code == 0 and len(rows) > 0
    check = {
        "name": "vllm_process",
        "status": "pass" if ok else "fail",
        "detail": f"{len(rows)} vllm process(es); pids={[r['pid'] for r in rows]}"
        if ok
        else f"no vllm process inside container (ps -ef | grep vllm empty)",
        "fixable": False,
        "fix_hint": "" if ok else "Start the vLLM service inside the container first.",
    }
    return rows, check


def check_pythonmalloc(container: str, procs: list[dict[str, Any]]) -> dict[str, Any]:
    if not procs:
        return {
            "name": "PYTHONMALLOC",
            "status": "fail",
            "detail": "no vllm pid to inspect",
            "fixable": False,
            "fix_hint": "Start vLLM first, then re-run this check.",
        }

    details: list[str] = []
    any_ok = False
    for p in procs:
        pid = p["pid"]
        code, out = docker_exec(
            container,
            f"tr '\\0' '\\n' < /proc/{pid}/environ | grep -E '^PYTHONMALLOC=' || true",
        )
        val = ""
        for l in out.splitlines():
            if l.startswith("PYTHONMALLOC="):
                val = l.split("=", 1)[1]
        details.append(f"pid={pid} -> {val or '(missing)'}")
        if val == "malloc":
            any_ok = True

    return {
        "name": "PYTHONMALLOC",
        "status": "pass" if any_ok else "fail",
        "detail": "; ".join(details),
        "fixable": False,
        "fix_hint": (
            ""
            if any_ok
            else "Restart vLLM with PYTHONMALLOC=malloc (export PYTHONMALLOC=malloc before launch), "
            "or run the consent-based fix: python3 fix_pythonmalloc.py --container <name> "
            "(writes /etc/profile.d/vllm_pythonmalloc.sh inside the container; afterwards re-enter "
            "the container via a login shell and restart vLLM). "
            "This env var must be present in the vllm process at start."
        ),
    }


def _grep_markers_snippet(root: str, markers: dict[str, str]) -> str:
    parts = []
    for rel, marker in markers.items():
        parts.append(
            f"grep -qF -- {shlex.quote(marker)} {shlex.quote(root + '/' + rel)} 2>/dev/null"
            f" && echo 'ok {rel}' || echo 'missing {rel}'"
        )
    return "; ".join(parts)


def check_vllm_ttft_fork(container: str, procs: list[dict[str, Any]]) -> dict[str, Any]:
    ref = load_fork_ref()
    if not procs:
        return {
            "name": "vllm_ttft_fork",
            "status": "fail",
            "detail": "no vllm pid to inspect",
            "fixable": False,
            "fix_hint": "Start vLLM first, then re-run this check.",
        }
    if ref is None or not ref.get("markers"):
        return {
            "name": "vllm_ttft_fork",
            "status": "fail",
            "detail": f"fork reference missing or broken: {FORK_REF_PATH}",
            "fixable": False,
            "fix_hint": "Restore references/vllm_fork.json (repo/commit/markers) in the skill.",
        }

    markers = ref["markers"]
    fork_id = f"{ref.get('repo', '?')}@{str(ref.get('commit', '?'))[:7]}"
    details: list[str] = []
    any_ok = False
    for p in procs:
        pid = p["pid"]
        code, out = docker_exec(
            container,
            f"tr '\\0' '\\n' < /proc/{pid}/environ | grep -E '^PYTHONPATH=' || true",
        )
        pp = ""
        for l in out.splitlines():
            if l.startswith("PYTHONPATH="):
                pp = l.split("=", 1)[1]
        if not pp:
            details.append(f"pid={pid} -> PYTHONPATH missing")
            continue

        vllm_dir = ""
        for entry in pp.split(":"):
            entry = entry.strip()
            if not entry:
                continue
            code, out = docker_exec(
                container,
                f"test -f {shlex.quote(entry + '/vllm/__init__.py')} && echo FOUND || true",
            )
            if "FOUND" in out:
                vllm_dir = entry
                break
        if not vllm_dir:
            details.append(
                f"pid={pid} -> PYTHONPATH set but no entry provides vllm/ (site-packages vllm in use)"
            )
            continue

        code, out = docker_exec(container, _grep_markers_snippet(vllm_dir, markers), timeout=30)
        missing = [l for l in out.splitlines() if l.startswith("missing")]
        if code == 0 and not missing:
            any_ok = True
            details.append(f"pid={pid} -> vllm={vllm_dir} ({fork_id} markers ok)")
        else:
            details.append(f"pid={pid} -> vllm={vllm_dir} markers missing: {missing or out[:200]}")

    pip_ok = True
    pip_note = ""
    pkg = ref.get("pip_package", "vllm")
    prefix = ref.get("expected_pip_version_prefix", "")
    if prefix:
        py = _vllm_python(container, procs)
        pip_version, _ = _pip_show(container, py, pkg)
        pip_ok = bool(pip_version) and pip_version.startswith(prefix)
        pip_note = (
            f"; pip {pkg}={pip_version or '<not installed>'} "
            f"(expect prefix {prefix!r}, known-good {ref.get('known_good_pip_version', '')})"
        )

    ok = any_ok and pip_ok
    if ok:
        hint = ""
    elif not any_ok:
        hint = (
            f"The vllm process must load the modified build {fork_id} "
            f"('{ref.get('description', '')}', {ref.get('url')}) via PYTHONPATH. "
            "With user consent, run: python3 scripts/fix_pythonpath.py --container <name> "
            "(installs the fork from GitHub if absent and writes the profile.d export), "
            "then the user restarts vLLM and this check is re-run."
        )
    else:
        hint = (
            f"pip vllm version mismatch: expected a version starting with {prefix!r} "
            f"(known-good {ref.get('known_good_pip_version', prefix)!r}, the empty placeholder "
            "matching vllm-ascend 0.23.0rc2). Reinstall the correct vllm package (user action), "
            "then re-run this check."
        )

    return {
        "name": "vllm_ttft_fork",
        "status": "pass" if ok else "fail",
        "detail": "; ".join(details) + pip_note,
        "fixable": False,
        "fix_hint": hint,
    }


# ---------------------------------------------------------------------------
# vllm_ascend check: base version + TTFT timing patch markers
# ---------------------------------------------------------------------------


def _vllm_python(container: str, procs: list[dict[str, Any]]) -> str:
    for p in procs:
        tok = (p.get("cmdline") or "").split()
        if tok and "python" in os.path.basename(tok[0]) and tok[0].startswith("/"):
            return tok[0]
    return "python3"


def _pip_show(container: str, py: str, pkg: str) -> tuple[str, str]:
    code, out = docker_exec(
        container,
        f"{shlex.quote(py)} -m pip show {pkg} 2>/dev/null || pip3 show {pkg} 2>/dev/null || true",
    )
    version = location = ""
    for line in out.splitlines():
        if line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("Location:"):
            location = line.split(":", 1)[1].strip()
    return version, location


def check_vllm_ascend(container: str, procs: list[dict[str, Any]]) -> dict[str, Any]:
    ref = load_vllm_ascend_ref()
    if not procs:
        return _fail_simple("vllm_ascend", "no vllm pid to inspect", "Start vLLM first, then re-run this check.")
    if ref is None or not ref.get("patch_files"):
        return _fail_simple(
            "vllm_ascend",
            f"reference missing or broken: {VLLM_ASCEND_REF_PATH}",
            "Restore references/vllm_ascend.json in the skill.",
        )

    pkg = ref.get("pip_package", "vllm-ascend")
    substr = ref.get("expected_version_substr", "")
    py = _vllm_python(container, procs)
    version, location = _pip_show(container, py, pkg)
    version_ok = bool(version) and substr in version

    marker_details: list[str] = []
    markers_ok = True
    if version_ok and location:
        for relpath, spec in ref["patch_files"].items():
            target = f"{location}/{relpath}"
            for marker in spec.get("markers", []):
                code, out = docker_exec(
                    container,
                    f"grep -qF -- {shlex.quote(marker)} {shlex.quote(target)} 2>/dev/null && echo ok || echo missing",
                )
                ok = any(l.strip() == "ok" for l in out.splitlines())
                if not ok:
                    markers_ok = False
                marker_details.append(f"{relpath}::{marker[:40]} -> {out.strip() or 'no output'}")

    detail = f"py={py} version={version or '<not installed>'} location={location or '?'}; "
    detail += f"version_ok={version_ok}; " + "; ".join(marker_details)

    if not version_ok:
        return {
            "name": "vllm_ascend",
            "status": "fail",
            "detail": detail,
            "fixable": False,
            "fix_hint": (
                f"Installed vllm-ascend must be the source build of {ref.get('repo')}@{str(ref.get('commit', ''))[:7]} "
                f"(pip version containing '{substr}', e.g. 0.23.0rc2.dev38+g7d73d9d63). "
                "The auto-fix only replaces patched files on this exact base; reinstall the correct base first (user action)."
            ),
        }
    if not markers_ok:
        return {
            "name": "vllm_ascend",
            "status": "fail",
            "detail": detail,
            "fixable": False,
            "fix_hint": (
                "vllm-ascend base version ok but the TTFT timing patch is missing. "
                "With user consent, run: python3 scripts/fix_vllm_ascend.py --container <name> "
                "(replaces the two patched source files with backups), then the user restarts vLLM "
                "and this check is re-run."
            ),
        }
    return {
        "name": "vllm_ascend",
        "status": "pass",
        "detail": detail,
        "fixable": False,
        "fix_hint": "",
    }


def _fail_simple(name: str, detail: str, hint: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "fail",
        "detail": detail,
        "fixable": False,
        "fix_hint": hint,
    }


# required for the profiling run to include stack info
CONFIG_EXPECT = {
    "torch_profiler_with_stack": "true",
}
# reported when present, never a gate
CONFIG_INFO_ONLY = ("max_iterations", "warmup_iterations")


def _config_from_text(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    # match "key": value  /  key=value  forms inside cmdline or config json
    for key in (*CONFIG_EXPECT, *CONFIG_INFO_ONLY):
        m = re.search(rf'"{key}"\s*:\s*("?[^",}}\s]+"?)', text)
        if not m:
            m = re.search(rf"(?:^|\s){re.escape(key)}=(\"[^\"]+\"|\S+)", text)
        if m:
            val = m.group(1).strip().strip('"').lower()
            found[key] = val
    return found


def check_config(container: str, procs: list[dict[str, Any]], expect: dict[str, str]) -> dict[str, Any]:
    if not procs:
        return {
            "name": "vllm_config",
            "status": "fail",
            "detail": "no vllm cmdline to inspect",
            "fixable": False,
            "fix_hint": "Start vLLM first, then re-run this check.",
        }

    combined = "\n".join(p["cmdline"] for p in procs)
    found = _config_from_text(combined)

    # follow config file references in the cmdline (e.g. --config /path.json, *.json).
    # Captured tokens are shell-evaluated later (docker exec bash -c), so every
    # reference must survive a strict whitelist filter before it is interpolated.
    file_refs = set(re.findall(r"(/\S+\.(?:json|jsonc|yaml|yml))", combined))
    file_refs.update(re.findall(r"(?:--config|-c)\s+(\S+)", combined))
    file_refs = {
        r
        for r in file_refs
        if not any(ch in r for ch in (";", "|", "&", "$", "`", "\n", "\r", "(", ")", "'", '"', " ", "!", "\\", "*"))
    }
    file_details: list[str] = []
    for ref in sorted(file_refs):
        code, out = docker_exec(container, f"cat {shlex.quote(ref)} 2>/dev/null || true")
        if out.strip():
            file_details.append(shlex.quote(ref))
            found.update(_config_from_text(out))

    mismatches = {
        k: {"expect": expect[k], "got": found.get(k, "<missing>")}
        for k in expect
        if found.get(k) != expect[k]
    }
    ok = not mismatches
    info = {k: found.get(k, "<missing>") for k in CONFIG_INFO_ONLY if k in found or True}
    detail = f"found={found} info={info}"
    if file_details:
        detail += f" config_files={file_details}"
    if mismatches:
        detail += f" mismatches={mismatches}"

    return {
        "name": "vllm_config",
        "status": "pass" if ok else "fail",
        "detail": detail,
        "fixable": False,
        "fix_hint": (
            ""
            if ok
            else "Ensure the vLLM config includes: "
            + ", ".join(f'"{k}": {v}' for k, v in expect.items())
            + ". Restart vLLM after updating the config."
        ),
    }


def run_container_checks(container: str, expect: dict[str, str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    results.append(check_docker_daemon())
    results.append(check_container_running(container))
    if results[-1]["status"] != "pass":
        # cannot continue without the container
        return results

    procs, pcheck = list_vllm_processes(container)
    results.append(pcheck)
    results.append(check_pythonmalloc(container, procs))
    results.append(check_vllm_ttft_fork(container, procs))
    results.append(check_vllm_ascend(container, procs))
    results.append(check_config(container, procs, expect))
    return results


# ---------------------------------------------------------------------------
# Optional service endpoint check (still stage 1, after container checks)
# ---------------------------------------------------------------------------

def _probe_start_profile_open(api_base: str, timeout: float) -> dict[str, Any]:
    """Side-effect-free detection of the profiling API.

    vLLM exposes its HTTP API spec at /openapi.json: request it with GET and
    check whether /start_profile is declared. This never triggers profiling.
    """
    import urllib.error
    import urllib.request

    url = api_base.rstrip("/") + "/openapi.json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            try:
                paths = set(json.load(resp).get("paths", {}).keys())
            except (ValueError, AttributeError):
                return {
                    "name": "endpoint_start_profile",
                    "status": "fail",
                    "detail": f"/openapi.json returned non-JSON payload at {url}",
                    "fixable": False,
                    "fix_hint": "check vLLM version / --api-base endpoint",
                }
            has = "/start_profile" in paths
            return {
                "name": "endpoint_start_profile",
                "status": "pass" if has else "fail",
                "detail": (
                    f"declared in /openapi.json (GET only, profiling NOT started)" if has
                    else f"/start_profile not declared in /openapi.json; known: {sorted(paths)[:8]}"
                ),
                "fixable": False,
                "fix_hint": "" if has else "vLLM API server without start_profile; check server flags",
            }
    except urllib.error.HTTPError as e:
        status_note = "server error" if e.code >= 500 else "rejected"
        return {
            "name": "endpoint_start_profile",
            "status": "fail",
            "detail": f"/openapi.json HTTP {e.code} {url}: {e.reason}",
            "fixable": False,
            "fix_hint": f"vLLM {status_note}; cannot verify endpoint safely",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "name": "endpoint_start_profile",
            "status": "fail",
            "detail": f"{url}: {e}",
            "fixable": False,
            "fix_hint": "Start the vLLM API server and pass --api-base correctly",
        }


def _probe_start_profile_live(api_base: str, timeout: float) -> dict[str, Any]:
    """Explicit opt-in probe (slight side effect): starts then stops profiling.

    Only used with --probe-start-profile. The caller was warned by SKILL.md.
    """
    import urllib.error
    import urllib.request

    base = api_base.rstrip("/")

    def call(path: str, method: str) -> str:
        url = base + path
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=timeout) as resp:
                return f"{path} HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return f"{path} HTTP {e.code} ({e.reason})"
        except Exception as e:  # noqa: BLE001
            return f"{path} {e}"

    start = call("/start_profile", "POST")
    stop = call("/stop_profile", "POST")
    ok = "HTTP 2" in start and "HTTP 2" in stop
    return {
        "name": "endpoint_start_profile",
        "status": "pass" if ok else "fail",
        "detail": f"paired probe (start immediately stopped): {start} -> {stop}",
        "fixable": False,
        "fix_hint": "" if ok else "start/stop_profile rejected; check server logs",
    }


def check_service(api_base: str, timeout: float, probe_start: bool = False) -> list[dict[str, Any]]:
    import urllib.error
    import urllib.request

    results: list[dict[str, Any]] = []
    base = api_base.rstrip("/")

    def probe(path: str, name: str, method: str = "GET") -> dict[str, Any]:
        url = base + path
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(300).decode(errors="replace")
                return {
                    "name": name,
                    "status": "pass",
                    "detail": f"HTTP {resp.status} {url} body={body!r}",
                    "fixable": False,
                    "fix_hint": "",
                }
        except urllib.error.HTTPError as e:
            present = e.code < 500
            return {
                "name": name,
                "status": "pass" if present else "fail",
                "detail": f"HTTP {e.code} {url}: {e.reason}",
                "fixable": False,
                "fix_hint": "" if present else "vLLM API server error; check server logs",
            }
        except Exception as e:  # noqa: BLE001
            return {
                "name": name,
                "status": "fail",
                "detail": f"{url}: {e}",
                "fixable": False,
                "fix_hint": "Start the vLLM API server and pass --api-base correctly",
            }

    results.append(probe("/health", "service_health"))
    if results[-1]["status"] != "pass":
        results.append(probe("/v1/models", "service_models"))
    # Side-effect-free by default: the profiling API is detected via the API
    # spec (GET /openapi.json), never invoked. --probe-start-profile opts in
    # to a real start/stop pair (see SKILL.md for the side-effect note).
    if probe_start:
        results.append(_probe_start_profile_live(base, timeout))
    else:
        results.append(_probe_start_profile_open(base, timeout))
    return results


# ---------------------------------------------------------------------------
# Legacy host-side checks (kept as --local opt-in)
# ---------------------------------------------------------------------------

def check_profiler_dir(fix: bool) -> dict[str, Any]:
    raw = os.environ.get("VLLM_TORCH_PROFILER_DIR", "")
    if not raw:
        return {
            "name": "VLLM_TORCH_PROFILER_DIR",
            "status": "fail",
            "detail": "not set",
            "fixable": True,
            "fix_hint": "export VLLM_TORCH_PROFILER_DIR=<writable dir> before starting vLLM",
        }
    p = os.path.expanduser(raw)
    if os.path.isdir(p) and os.access(p, os.W_OK):
        return {
            "name": "VLLM_TORCH_PROFILER_DIR",
            "status": "pass",
            "detail": p,
            "fixable": True,
            "fix_hint": "",
        }
    if fix:
        os.makedirs(p, exist_ok=True)
        return {
            "name": "VLLM_TORCH_PROFILER_DIR",
            "status": "pass",
            "detail": f"created {p}",
            "fixable": True,
            "fix_hint": "",
            "fixed": True,
        }
    return {
        "name": "VLLM_TORCH_PROFILER_DIR",
        "status": "fail",
        "detail": f"{p} missing or not writable",
        "fixable": True,
        "fix_hint": f"mkdir -p {p} (or re-run with --fix after consent)",
    }


def check_python_deps(fix: bool) -> list[dict[str, Any]]:
    results = []
    for mod in ("jinja2",):
        code, out = _run([sys.executable, "-c", f"import {mod}; print({mod}.__version__)"], timeout=30)
        if code == 0:
            results.append({"name": f"dep:{mod}", "status": "pass", "detail": out, "fixable": True, "fix_hint": ""})
            continue
        if fix:
            pcode, pout = _run([sys.executable, "-m", "pip", "install", mod], timeout=180)
            results.append(
                {
                    "name": f"dep:{mod}",
                    "status": "pass" if pcode == 0 else "fail",
                    "detail": (pout or out)[:400],
                    "fixable": True,
                    "fix_hint": "" if pcode == 0 else f"pip install {mod}",
                    "fixed": pcode == 0,
                }
            )
        else:
            results.append(
                {
                    "name": f"dep:{mod}",
                    "status": "fail",
                    "detail": out[:300],
                    "fixable": True,
                    "fix_hint": f"pip install {mod} (or re-run with --fix after consent)",
                }
            )
    return results


def emit(results: list[dict[str, Any]], stage: str) -> int:
    payload = {
        "stage": stage,
        "results": results,
        "summary": {
            "pass": sum(1 for r in results if r["status"] == "pass"),
            "fail": sum(1 for r in results if r["status"] == "fail"),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["summary"]["fail"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--container",
        default="",
        help="docker container name/id running the vLLM service (stage-1 primary mode)",
    )
    ap.add_argument("--api-base", default="", help="also probe the vLLM HTTP API when set")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument(
        "--probe-start-profile",
        action="store_true",
        help=(
            "side-effect full check: POST /start_profile then immediately POST /stop_profile. "
            "Default path is side-effect-free (reads /openapi.json only)"
        ),
    )
    ap.add_argument("--local", action="store_true", help="legacy host-side checks")
    ap.add_argument("--fix", action="store_true", help="apply local fixes only (dirs/deps)")
    ap.add_argument(
        "--expect-config-json",
        default="",
        help='override expected config fields, e.g. \'{"max_iterations": 5}\'',
    )
    args = ap.parse_args()

    expect = dict(CONFIG_EXPECT)
    if args.expect_config_json:
        try:
            expect.update({k: str(v).lower() for k, v in json.loads(args.expect_config_json).items()})
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"invalid --expect-config-json: {e}"}), file=sys.stderr)
            return 2

    results: list[dict[str, Any]] = []
    stage = "1-env-check"

    if not args.container and not args.local:
        # auto-detect the vLLM container when not specified
        args.container = _resolve_container(args.container)
    if args.container:
        results.extend(run_container_checks(args.container, expect))

    if args.api_base:
        results.extend(check_service(args.api_base, args.timeout, probe_start=args.probe_start_profile))

    if args.local:
        results.append(check_profiler_dir(args.fix))
        results.extend(check_python_deps(args.fix))

    return emit(results, stage)


if __name__ == "__main__":
    sys.exit(main())
