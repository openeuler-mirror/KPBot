#!/usr/bin/env python3
"""vLLM 分词后端迁移至 fastokens 并启用段缓存的检查工具。

功能包括环境探测与迁移判定、迁移前快照、合成会话生成与分词器层对比。
仅依赖 Python 3.9 及以上版本的标准库。应使用 vLLM 服务所用的 Python 解释器运行，
以使采集的 vllm、transformers 及 fastokens 信息同服务环境一致：

    <服务的 Python 解释器> fastokens_segment_cache.py probe --model <模型名或本地分词器路径>

子命令：
    probe          环境探测：版本、前端类型、生效的分词后端、目标版本与迁移判定
    snapshot       迁移前快照：依赖清单、环境变量、重新打包的 fastokens wheel 与回退说明
    make-sessions  生成合成多轮会话，作为回放数据
    verify         分词器层的两组对比：基线组与段缓存开启组
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from array import array
from pathlib import Path

RESULT_TAG = "@@SEGMENT_CACHE_RESULT@@ "
SWITCH_VAR = "VLLM_USE_FASTOKENS"
CACHE_VAR = "FASTOKENS_SEGMENT_CACHE"
RUST_VAR = "VLLM_USE_RUST_FRONTEND"
PYPI_URL = "https://pypi.org/pypi/fastokens/json"
MODULES = {
    "vllm": "vllm",
    "vllm-ascend": "vllm_ascend",
    "transformers": "transformers",
    "tokenizers": "tokenizers",
    "fastokens": "fastokens",
}
# 由安装工具（pip、uv）写入 dist-info、不属于原始 wheel 的文件
INSTALLER_FILES = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}
SECRET_FLAG = re.compile(r"(key|token|secret|passw)", re.I)
# vLLM 0.23 服务端以 tokenizer(prompts, add_special_tokens=False, truncation=..., max_length=...)
# 调用分词器（vllm/utils/async_utils.py）。max_length 取足够大的值，
# 使调用经过截断参数的处理路径而不发生实际截断。
CALL_KWARGS = {"add_special_tokens": False, "truncation": True, "max_length": 1 << 24}
# 官方发行版自 fastokens 0.3.5 起包含段缓存：0.3.5 的 README 含“Segment cache”一节，
# 0.3.4 的 README 不含该节
MIN_VERSION = "0.3.5"
ARM_LABELS = {
    "orig": "基线组",
    "on": "段缓存开启组",
}


# ---------------------------------------------------------------- 通用函数


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relevant_env(env) -> dict:
    keys = (SWITCH_VAR, RUST_VAR)
    return {k: v for k, v in sorted(env.items()) if k in keys or k.startswith("FASTOKENS_")}


def redact_url(text: str) -> str:
    return re.sub(r"//[^/@\s]+@", "//[已脱敏]@", text)


def redact_args(args) -> list:
    out, hide_next = [], False
    for arg in args:
        if hide_next:
            out.append("[已脱敏]")
            hide_next = False
        elif arg.startswith("-") and SECRET_FLAG.search(arg):
            if "=" in arg:
                out.append(arg.split("=", 1)[0] + "=[已脱敏]")
            else:
                out.append(arg)
                hide_next = True
        else:
            out.append(redact_url(arg))
    return out


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    index = int(round(q / 100 * (len(ordered) - 1)))
    return ordered[min(len(ordered) - 1, max(0, index))]


def median(values):
    return percentile(values, 50)


def peak_rss_mib():
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 1048576 if sys.platform == "darwin" else peak / 1024, 1)


def current_rss_mib():
    """返回当前进程的常驻内存（RSS，非峰值），通过 ps 获取，适用于 macOS 与 Linux。"""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return round(int(out.split()[0]) / 1024, 1)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def load_requests(path) -> list:
    requests = []
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            if not isinstance(request.get("messages"), list):
                raise SystemExit(f"{path}:{number} 缺少 messages 列表")
            request.setdefault("session", f"line{number}")
            requests.append(request)
    if not requests:
        raise SystemExit(f"{path} 不包含请求")
    return requests


def split_by_session(requests, thread_count) -> list:
    """按会话将请求分配到线程：同一会话的请求分配到同一线程，并保持原有顺序。"""
    slots, buckets = {}, [[] for _ in range(max(1, thread_count))]
    for index, request in enumerate(requests):
        slot = slots.setdefault(request["session"], len(slots) % len(buckets))
        buckets[slot].append(index)
    return [bucket for bucket in buckets if bucket]


# ---------------------------------------------------------------- probe


def find_distribution(name):
    for candidate in (name, name.replace("-", "_")):
        try:
            return metadata.distribution(candidate)
        except metadata.PackageNotFoundError:
            pass
    return None


def module_path(module):
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        return str(list(spec.submodule_search_locations)[0])
    return spec.origin


def native_libraries(package_dir) -> list:
    if not package_dir or not os.path.isdir(package_dir):
        return []
    libraries = []
    for name in sorted(os.listdir(package_dir)):
        if name.startswith("_native") and (".so" in name or name.endswith(".pyd")):
            path = os.path.join(package_dir, name)
            libraries.append({"path": path, "sha256": sha256_file(path)})
    return libraries


def package_info(name) -> dict:
    dist = find_distribution(name)
    path = module_path(MODULES[name])
    info = {"version": dist.version if dist else None, "module_dir": path}
    if dist is not None:
        dist_dir = getattr(dist, "_path", None)
        info["dist_info"] = str(dist_dir) if dist_dir else None
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            info["direct_url"] = json.loads(redact_url(direct_url))
        editable = info.get("direct_url", {}).get("dir_info", {}).get("editable")
        apart = (
            dist_dir and path and Path(dist_dir).parent.resolve() != Path(path).parent.resolve()
        )
        if apart and not editable:
            info["note"] = "元数据与模块位于不同目录（由 PYTHONPATH 叠加所致），以模块所在目录为准"
    if name == "fastokens":
        info["native"] = native_libraries(path)
    return info


def vllm_features(vllm_dir) -> dict:
    if not vllm_dir:
        return {}
    envs_file = Path(vllm_dir) / "envs.py"
    source = envs_file.read_text(errors="replace") if envs_file.exists() else ""
    return {
        "has_fastokens_switch": SWITCH_VAR in source,
        "has_rust_frontend": RUST_VAR in source,
        "rust_binary_built": (Path(vllm_dir) / "vllm-rs").exists(),
    }


def read_linux_processes() -> dict:
    processes = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = Path(f"/proc/{name}/cmdline").read_bytes()
            status = Path(f"/proc/{name}/status").read_text().splitlines()
            parent = next(int(ln.split()[1]) for ln in status if ln.startswith("PPid:"))
        except (OSError, StopIteration, ValueError):
            continue
        args = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
        processes[int(name)] = (args, parent)
    return processes


def scan_processes(pids) -> tuple:
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc"):
        return [], "仅在 Linux 上可读取运行中进程的环境变量与已加载的共享库"
    processes = read_linux_processes()
    if pids:
        chosen = set(pids)
    else:
        # vLLM 的 API 进程与 EngineCore 进程为子进程，命令行未必包含 vllm，
        # 因此一并纳入其各级子进程
        chosen = {
            pid
            for pid, (args, _) in processes.items()
            if any("vllm" in arg.lower() for arg in args)
        }
        grew = True
        while grew:
            grew = False
            for pid, (_, parent) in processes.items():
                if parent in chosen and pid not in chosen:
                    chosen.add(pid)
                    grew = True
    chosen -= {os.getpid(), os.getppid()}
    rows = []
    for pid in sorted(chosen)[:64]:
        args = processes.get(pid, ([], 0))[0]
        if args:
            rows.append(describe_process(pid, args))
    return rows, None


def describe_process(pid, args) -> dict:
    row = {"pid": pid, "cmdline": " ".join(redact_args(args))[:300]}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace")
        env = dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)
        row["env"] = relevant_env(env)
    except OSError as e:
        row["env"] = f"无法读取（{e.__class__.__name__}）"
    try:
        maps = Path(f"/proc/{pid}/maps").read_text(errors="replace").splitlines()
        row["fastokens_loaded"] = sorted(
            {line.split()[-1] for line in maps if "fastokens" in line and ".so" in line}
        )
    except OSError as e:
        row["fastokens_loaded"] = f"无法读取（{e.__class__.__name__}）"
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                row["rss_mib"] = round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return row


def pypi_latest(timeout=10) -> dict:
    with urllib.request.urlopen(PYPI_URL, timeout=timeout) as response:
        data = json.load(response)
    version = data["info"]["version"]
    files = data.get("releases", {}).get(version) or []
    uploaded = files[0].get("upload_time_iso_8601") if files else None
    return {"version": version, "uploaded": uploaded}


def run_worker(env, worker_args, timeout) -> dict:
    command = [sys.executable, os.path.abspath(__file__), "_worker", *worker_args]
    try:
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"运行超过 {timeout} 秒未结束（可能已死锁）"}
    for line in reversed(done.stdout.splitlines()):
        if line.startswith(RESULT_TAG):
            return json.loads(line[len(RESULT_TAG) :])
    tail = "\n".join((done.stderr or done.stdout or "").strip().splitlines()[-12:])
    return {"ok": False, "error": f"子进程异常退出，退出码 {done.returncode}", "stderr_tail": tail}


def model_args(args) -> list:
    out = ["--model", args.model, "--tokenizer-mode", args.tokenizer_mode]
    if args.revision:
        out += ["--revision", args.revision]
    if args.trust_remote_code:
        out.append("--trust-remote-code")
    return out


def with_overlay(env, overlay):
    env = dict(env)
    if overlay:
        old = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.path.abspath(overlay) + (os.pathsep + old if old else "")
    return env


def is_official(fastokens_info, target) -> bool:
    version = fastokens_info.get("version")
    return bool(version and target and version == target and not fastokens_info.get("direct_url"))


def version_key(version):
    """将版本号的前三段转换为整数元组；无法解析时返回 None。"""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(part) for part in match.groups()) if match else None


def below_minimum(version) -> bool:
    key = version_key(version)
    return key is not None and key < version_key(MIN_VERSION)


def cmd_probe(args):
    packages = {name: package_info(name) for name in MODULES}
    processes, process_note = scan_processes(args.pid)
    info = {
        "time": now(),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "libc": " ".join(platform.libc_ver()).strip() or None,
        },
        "packages": packages,
        "vllm_features": vllm_features(packages["vllm"]["module_dir"]),
        "env_current_shell": relevant_env(os.environ),
        "processes": processes,
        "processes_note": process_note,
    }
    target = args.target_version
    if not target and not args.offline:
        try:
            info["pypi_latest"] = pypi_latest()
            target = info["pypi_latest"]["version"]
        except Exception as e:  # noqa: BLE001 - 网络错误仅记录
            info["pypi_latest"] = {"error": f"{e.__class__.__name__}: {e}"}
    info["target_version"] = target

    if args.model:
        worker_args = ["--load-only", *model_args(args)]
        info["load_current"] = run_worker(dict(os.environ), worker_args, args.timeout)
        # 环境中既无 fastokens 也未指定候选安装目录时跳过加载测试，
        # 该情形记为提示，不计入不支持的原因
        if args.overlay or packages["fastokens"]["module_dir"]:
            candidate_env = with_overlay(os.environ, args.overlay)
            candidate_env[SWITCH_VAR] = "1"
            candidate_env.pop(CACHE_VAR, None)
            info["load_candidate"] = run_worker(candidate_env, worker_args, args.timeout)

    info["verdict"] = probe_verdict(info, target)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "probe.json").write_text(json.dumps(info, ensure_ascii=False, indent=1))
    print(probe_text(info))
    if args.out:
        print(f"\n完整结果：{Path(args.out) / 'probe.json'}")


def probe_verdict(info, target) -> dict:
    packages, features = info["packages"], info["vllm_features"]
    reasons, notes = [], []
    vllm = packages["vllm"]
    if not vllm["version"] and not vllm["module_dir"]:
        reasons.append(
            "当前 Python 环境中未找到 vLLM：应使用 vLLM 服务所用的 Python 解释器运行检查工具"
        )
    elif not features.get("has_fastokens_switch"):
        reasons.append(
            f"vLLM {vllm['version']} 不提供 {SWITCH_VAR} 开关（自 vLLM 0.22 起提供；"
            "0.21 通过 --tokenizer-mode fastokens 启用，不在本流程范围内）"
        )
    rust_on = info["env_current_shell"].get(RUST_VAR) == "1" or any(
        isinstance(p.get("env"), dict) and p["env"].get(RUST_VAR) == "1" for p in info["processes"]
    )
    if rust_on:
        reasons.append(
            f"服务使用 Rust 前端（{RUST_VAR}=1）：Rust 前端的 fastokens 在编译 vLLM 时静态链接，"
            "无法通过升级 Python 包切换"
        )
    candidate = info.get("load_candidate")
    if candidate is None and "load_current" not in info:
        notes.append("未指定 --model，未进行加载测试；切换前应完成该测试")
    elif candidate is None:
        notes.append(
            "环境中未安装 fastokens，且未指定 --overlay："
            "应将目标版本安装到候选安装目录后，以 --overlay 执行加载测试"
        )
    elif not candidate.get("ok"):
        reasons.append(f"fastokens 加载该模型的分词器失败：{candidate.get('error')}")
    elif not str(candidate.get("backend_module", "")).startswith("fastokens"):
        reasons.append(
            f"{SWITCH_VAR}=1 时生效的分词后端仍为 {candidate.get('backend_module')}："
            "当前分词模式不加载 HF fast tokenizer"
        )
    elif target and candidate.get("fastokens_version") != target:
        notes.append(
            f"加载测试使用的 fastokens 版本为 {candidate.get('fastokens_version')}，"
            f"与目标版本 {target} 不一致；应安装候选版本后，以 --overlay 重新执行加载测试"
        )

    if below_minimum(target):
        reasons.append(
            f"目标版本 fastokens {target} 不包含段缓存（官方发行版自 {MIN_VERSION} 起包含）"
        )

    fastokens_info = packages["fastokens"]
    if reasons:
        decision = "不支持"
    elif not target:
        decision = (
            f"缺少目标版本：无法访问 PyPI；应由用户提供 fastokens {MIN_VERSION} 或更高版本的 "
            "wheel 文件，并以 --target-version 指定该文件的版本"
        )
    elif is_official(fastokens_info, target):
        decision = f"已是最新官方发行版 {target}：不必更换版本，仅配置段缓存"
    else:
        current = fastokens_info["version"] or "未安装"
        if fastokens_info.get("direct_url"):
            current += "（非 PyPI 安装，可能为自行构建）"
        decision = (
            f"可切换至 fastokens=={target}（当前：{current}）；变更服务前应通过分词器层对比"
        )
    return {"decision": decision, "reasons": reasons, "notes": notes}


def effective_backend(info) -> str:
    processes = [p for p in info["processes"] if isinstance(p.get("env"), dict)]
    if processes:
        parts = []
        for process in processes:
            switched_on = process["env"].get(SWITCH_VAR) == "1"
            loaded = process.get("fastokens_loaded")
            if switched_on and isinstance(loaded, list) and loaded:
                state = "fastokens（开关为 1，已加载 fastokens 原生库）"
            elif switched_on:
                state = "开关为 1，但未检测到已加载的 fastokens 原生库"
            else:
                state = "HF tokenizers（未启用 fastokens 开关）"
            cache = process["env"].get(CACHE_VAR, "未设置")
            parts.append(f"pid {process['pid']}：{state}；{CACHE_VAR}={cache}")
        return "；".join(parts)
    current = info.get("load_current")
    if current and current.get("ok"):
        return (
            f"{current['backend_module']}.{current['backend_class']}（按当前 shell 环境加载判定）"
        )
    return "未知：未检测到运行中的 vLLM 进程，且未指定 --model"


def probe_text(info) -> str:
    packages, verdict = info["packages"], info["verdict"]
    lines = ["# fastokens 段缓存：探测结果", ""]
    lines.append(f"- 判定：{verdict['decision']}")
    lines += [f"  - 原因：{reason}" for reason in verdict["reasons"]]
    lines += [f"  - 提示：{note}" for note in verdict["notes"]]
    lines.append(f"- 生效的分词后端：{effective_backend(info)}")
    py = info["python"]
    lines.append(
        f"- Python：{py['version']}，{py['machine']}，{py['platform']}，"
        f"libc {py['libc'] or '未知'}"
    )
    for name in MODULES:
        package = packages[name]
        where = f"（{package['module_dir']}）" if package["module_dir"] else ""
        lines.append(f"- {name}：{package['version'] or '未安装'}{where}")
    lines += fastokens_lines(packages["fastokens"])
    features = info["vllm_features"]
    if features:
        switch = "有" if features["has_fastokens_switch"] else "无"
        lines.append(
            f"- vLLM 功能：{SWITCH_VAR} 开关{switch}；"
            f"Rust 前端{'有' if features['has_rust_frontend'] else '无'}"
            f"（可执行文件{'已构建' if features['rust_binary_built'] else '未构建'}）"
        )
    lines.append(f"- 当前 shell 的相关环境变量：{info['env_current_shell'] or '无'}")
    if info["processes_note"]:
        lines.append(f"- 运行中进程：{info['processes_note']}")
    for process in info["processes"]:
        lines.append(
            f"- 进程 {process['pid']}：RSS {process.get('rss_mib', '?')} MiB；"
            f"环境变量 {process.get('env')}；"
            f"已加载的 fastokens 原生库 {process.get('fastokens_loaded')}"
        )
        lines.append(f"  - 命令行：{process['cmdline']}")
    latest = info.get("pypi_latest")
    if latest:
        uploaded = f"（上传时间 {latest['uploaded']}）" if latest.get("uploaded") else ""
        lines.append(f"- PyPI 最新版本：{latest.get('version') or latest.get('error')}{uploaded}")
    lines.append(f"- 目标版本：{info['target_version'] or '未确定'}")
    labels = (("load_current", "按当前配置加载"), ("load_candidate", f"按 {SWITCH_VAR}=1 加载"))
    for key, label in labels:
        result = info.get(key)
        if result is not None:
            lines += load_lines(label, result)
    return "\n".join(lines)


def fastokens_lines(fastokens_info) -> list:
    if not fastokens_info["version"]:
        return []
    direct_url = fastokens_info.get("direct_url")
    source = direct_url.get("url", "") if direct_url else "PyPI 或镜像源"
    lines = [f"  - fastokens 安装来源：{source}"]
    for library in fastokens_info.get("native", []):
        lines.append(f"  - 原生库 sha256：{library['sha256']}（{library['path']}）")
    if fastokens_info.get("note"):
        lines.append(f"  - {fastokens_info['note']}")
    return lines


def load_lines(label, result) -> list:
    if result.get("ok"):
        return [
            f"- {label}：后端 {result['backend_module']}.{result['backend_class']}，"
            f"fastokens {result.get('fastokens_version') or '-'}，"
            f"段缓存统计接口{'有' if result.get('has_stats') else '无'}，"
            f"加载耗时 {result['load_s']} s"
        ]
    lines = [f"- {label}：失败，{result.get('error')}"]
    if result.get("stderr_tail"):
        lines.append("  ```\n  " + result["stderr_tail"].replace("\n", "\n  ") + "\n  ```")
    return lines


# ---------------------------------------------------------------- snapshot


def wheel_from_installed(name, out_dir, dist=None) -> dict:
    """按 RECORD 将已安装的包重新打包为 wheel，用于回退到同一构建。"""
    dist = dist if dist is not None else find_distribution(name)
    if dist is None:
        return {"installed": False}
    direct_url = json.loads(dist.read_text("direct_url.json") or "{}")
    source = redact_url(direct_url.get("url", "")) or None
    if direct_url.get("dir_info", {}).get("editable"):
        return {
            "installed": True,
            "version": dist.version,
            "error": "可编辑安装无法打包，回退时应从原源码目录重新安装",
            "direct_url": source,
        }
    dist_dir = Path(dist._path)
    root = dist_dir.parent
    tags = [
        line.split(":", 1)[1].strip()
        for line in (dist.read_text("WHEEL") or "").splitlines()
        if line.startswith("Tag:")
    ]
    if not tags:
        return {
            "installed": True,
            "version": dist.version,
            "error": "dist-info 的 WHEEL 文件中缺少 Tag 项，无法打包",
        }
    files = [
        path
        for path in (Path(str(p)) for p in dist.files or [])
        if keep_in_wheel(path, root, dist_dir)
    ]
    stem = dist_dir.name[: -len(".dist-info")]
    wheel = Path(out_dir) / f"{stem}-{tags[0]}.whl"
    record = []
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files, key=lambda p: (p.parts[0] == dist_dir.name, p.as_posix())):
            data = (root / path).read_bytes()
            hashed = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            archive.writestr(path.as_posix(), data)
            record.append(f"{path.as_posix()},sha256={hashed.decode()},{len(data)}")
        record_name = f"{dist_dir.name}/RECORD"
        record.append(f"{record_name},,")
        archive.writestr(record_name, "\n".join(record) + "\n")
    return {
        "installed": True,
        "version": dist.version,
        "wheel": str(wheel),
        "wheel_sha256": sha256_file(wheel),
        "files": len(files),
        "direct_url": source,
    }


def keep_in_wheel(path, root, dist_dir) -> bool:
    parts = path.parts
    if not parts or parts[0] == ".." or "__pycache__" in parts or path.suffix == ".pyc":
        return False
    if parts[0] == dist_dir.name and path.name in INSTALLER_FILES:
        return False
    return (root / path).is_file()


def cmd_snapshot(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pins = {
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    }
    (out / "packages.txt").write_text("\n".join(sorted(pins, key=str.lower)) + "\n")
    env = {"当前 shell": relevant_env(os.environ)}
    processes, _ = scan_processes(args.pid) if args.pid else ([], None)
    for process in processes:
        env[f"进程 {process['pid']}"] = process.get("env")
    (out / "env.json").write_text(json.dumps(env, ensure_ascii=False, indent=1))
    fastokens_info = wheel_from_installed("fastokens", out)
    fastokens_info["native"] = native_libraries(module_path("fastokens"))
    (out / "fastokens.json").write_text(json.dumps(fastokens_info, ensure_ascii=False, indent=1))
    (out / "RESTORE.md").write_text(restore_text(fastokens_info, env))
    print((out / "RESTORE.md").read_text())
    print(f"快照目录：{out.resolve()}")


def restore_text(fastokens_info, env) -> str:
    py = sys.executable
    lines = ["# 回退步骤", "", f"快照时间：{now()}；Python 解释器：{py}", ""]
    lines.append(
        "1. 恢复 fastokens（按该环境原有的安装方式执行；"
        "环境中无 pip 时改用 `uv pip install --python <python>`）："
    )
    if not fastokens_info.get("installed"):
        lines.append(f"   快照时未安装 fastokens，回退时卸载：`{py} -m pip uninstall -y fastokens`")
    elif fastokens_info.get("wheel"):
        wheel = Path(fastokens_info["wheel"]).resolve()
        lines.append(f"   `{py} -m pip install --no-deps --force-reinstall {wheel}`")
        lines.append(
            f"   （wheel sha256 {fastokens_info['wheel_sha256']}；相同版本号可能对应不同构建，"
            "回退时安装此 wheel 以恢复同一构建）"
        )
    else:
        lines.append(
            f"   自动打包失败：{fastokens_info.get('error')}。"
            f"原版本 {fastokens_info.get('version')}，安装来源 {fastokens_info.get('direct_url')}"
        )
    lines.append("2. 恢复环境变量（快照时不存在的变量应从服务配置中删除）：")
    for where, values in env.items():
        lines.append(f"   - {where}：{values if values else '无相关变量'}")
    lines.append("3. 重启 vLLM 服务（环境变量在创建分词器时读取，重启后生效）。")
    natives = ", ".join(lib["sha256"] for lib in fastokens_info.get("native", [])) or "无"
    lines.append(
        f"4. 核对：重新运行 probe，fastokens 原生库 sha256 应为 {natives}，"
        "环境变量应与上述记录一致。"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- make-sessions

SYLLABLES = [
    "ka",
    "to",
    "ri",
    "men",
    "sha",
    "lu",
    "po",
    "dei",
    "an",
    "qu",
    "zor",
    "bel",
    "fin",
    "tra",
    "vo",
    "ex",
    "im",
    "lo",
    "ne",
    "da",
]
HAN_CHARACTERS = (
    "的一是在不了有和人这中大为上个我以要他时来用们生到作地于出就分对成会可主发年动同工"
    "也能下过子说产种面而方后多定行学法所"
)


def generate_sessions(session_count, turns, seed, block_chars) -> list:
    rng = random.Random(seed)
    vocabulary = [
        "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(1, 3))) for _ in range(400)
    ]

    def words(count):
        return " ".join(rng.choice(vocabulary) for _ in range(count))

    def chinese(count):
        return "".join(rng.choice(HAN_CHARACTERS) for _ in range(count)) + "。"

    def tool_output():
        parts = []
        while sum(map(len, parts)) < block_chars:
            number = rng.randint(0, 999)
            parts.append(
                f"def {rng.choice(vocabulary)}_{number}({rng.choice(vocabulary)}, "
                f"{rng.choice(vocabulary)}):\n"
                f"    return {rng.choice(vocabulary)} + {number}  # {chinese(8)}\n"
            )
        return "".join(parts)

    system = "You are a coding agent working in a large repository. " + words(600)
    conversations = [[{"role": "system", "content": system}] for _ in range(session_count)]
    requests = []
    for turn in range(turns):
        for index, messages in enumerate(conversations):
            user = f"Turn {turn}: {words(rng.randint(20, 60))} {chinese(30)}"
            if turn % 2 == 0:
                user += "\nTool output:\n" + tool_output()
            messages.append({"role": "user", "content": user})
            requests.append({"session": f"s{index}", "messages": copy.deepcopy(messages)})
            reply = words(rng.randint(30, 80)) + " " + chinese(20)
            messages.append({"role": "assistant", "content": reply})
    return requests


def cmd_make_sessions(args):
    requests = generate_sessions(args.sessions, args.turns, args.seed, args.block_chars)
    with open(args.out, "w", encoding="utf-8") as f:
        for request in requests:
            f.write(json.dumps(request, ensure_ascii=False) + "\n")
    sizes = [sum(len(m["content"]) for m in r["messages"]) for r in requests]
    print(
        f"已写入 {args.out}：{len(requests)} 条请求，{args.sessions} 个会话，"
        f"每个会话 {args.turns} 轮，工具输出 {args.block_chars} 字符，随机种子 {args.seed}；"
        f"每条请求 {min(sizes)}–{max(sizes)} 字符"
    )


# ---------------------------------------------------------------- worker（子进程）


def import_get_tokenizer():
    try:
        from vllm.tokenizers import get_tokenizer
    except ImportError:
        from vllm.transformers_utils.tokenizer import get_tokenizer
    return get_tokenizer


def cmd_worker(args):
    result = {"ok": False}
    try:
        result.update(worker_body(args))
        result["ok"] = True
    except Exception as e:  # noqa: BLE001 - 所有异常均传回父进程
        result["error"] = f"{e.__class__.__name__}: {str(e)[:300]}"
    result["peak_rss_mib"] = peak_rss_mib()
    print(RESULT_TAG + json.dumps(result, ensure_ascii=False), flush=True)


def worker_body(args) -> dict:
    started = time.perf_counter()
    get_tokenizer = import_get_tokenizer()
    load_kwargs = {
        "tokenizer_mode": args.tokenizer_mode,
        "trust_remote_code": args.trust_remote_code,
    }
    if args.revision:
        load_kwargs["revision"] = args.revision
    tokenizer = get_tokenizer(args.model, **load_kwargs)
    backend = getattr(tokenizer, "backend_tokenizer", None) or getattr(
        tokenizer, "_tokenizer", None
    )
    stats_fn = getattr(backend, "segment_cache_stats", None)
    try:
        fastokens_version = metadata.version("fastokens")
    except metadata.PackageNotFoundError:
        fastokens_version = None
    result = {
        "load_s": round(time.perf_counter() - started, 2),
        "backend_module": type(backend).__module__,
        "backend_class": type(backend).__name__,
        "fastokens_version": fastokens_version,
        "has_stats": callable(stats_fn),
        "env": relevant_env(os.environ),
    }
    if args.load_only:
        return result

    requests = load_requests(args.sessions)
    template = Path(args.chat_template).read_text() if args.chat_template else None
    texts = render_all(tokenizer, requests, template, result)

    # 预热：首次调用包含一次性初始化开销，不计入计时；预热产生的缓存计数从统计中扣除
    warm_kwargs = {"tokenize": False, "add_generation_prompt": True}
    if template:
        warm_kwargs["chat_template"] = template
    warm_messages = [{"role": "user", "content": "warm up"}]
    tokenize(tokenizer, tokenizer.apply_chat_template(warm_messages, **warm_kwargs))
    tokenize(tokenizer, "warm up " * 8)
    stats_before = stats_fn() if callable(stats_fn) else None
    result["rss_after_load_mib"] = current_rss_mib()

    result.update(encode_all(tokenizer, texts, args.ids_out))
    result["rss_after_replay_mib"] = current_rss_mib()
    stats_after = stats_fn() if callable(stats_fn) else None  # 缓存关闭时返回 None
    if stats_after is not None:
        stats = dict(stats_after)
        for key in ("hits", "misses", "hit_bytes", "miss_bytes"):
            if key in stats and stats_before and key in stats_before:
                stats[key] -= stats_before[key]
        result["stats"] = stats

    if args.threads > 1:
        result["concurrency"] = run_concurrency(
            tokenizer, requests, texts, result["digests"], args.threads
        )
    return result


def render_all(tokenizer, requests, template, result) -> list:
    texts, started = [], time.perf_counter()
    for request in requests:
        kwargs = {"tokenize": False, "add_generation_prompt": True}
        if request.get("tools"):
            kwargs["tools"] = request["tools"]
        if template:
            kwargs["chat_template"] = template
        kwargs.update(request.get("chat_template_kwargs") or {})
        texts.append(tokenizer.apply_chat_template(request["messages"], **kwargs))
    result["render_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["text_sha"] = hashlib.sha256("\0".join(texts).encode()).hexdigest()[:16]
    return texts


def tokenize(tokenizer, text_or_texts):
    """以与 vLLM 服务端相同的方式调用分词器（__call__），支持单条与批量输入。"""
    return tokenizer(text_or_texts, **CALL_KWARGS)["input_ids"]


def digest_of(ids) -> str:
    return hashlib.sha256(array("I", ids).tobytes()).hexdigest()[:16]


def encode_all(tokenizer, texts, ids_path) -> dict:
    digests, lengths, times = [], [], []
    ids_file = open(ids_path, "wb") if ids_path else None
    try:
        for text in texts:
            started = time.perf_counter()
            ids = tokenize(tokenizer, text)
            times.append((time.perf_counter() - started) * 1000)
            packed = array("I", ids)
            digests.append(hashlib.sha256(packed.tobytes()).hexdigest()[:16])
            lengths.append(len(packed))
            if ids_file:
                packed.tofile(ids_file)
    finally:
        if ids_file:
            ids_file.close()
    return {"digests": digests, "lengths": lengths, "encode_ms": [round(t, 3) for t in times]}


def run_concurrency(tokenizer, requests, texts, digests, thread_count) -> dict:
    groups = split_by_session(requests, thread_count)
    mismatched, errors, lock = [], [], threading.Lock()

    def run(indices):
        try:
            local = copy.deepcopy(tokenizer)
        except Exception:  # noqa: BLE001 - 无法深拷贝时共用原分词器
            local = tokenizer
        for index in indices:
            try:
                if digest_of(tokenize(local, texts[index])) != digests[index]:
                    with lock:
                        mismatched.append(index)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(f"请求 {index}：{e.__class__.__name__}: {str(e)[:200]}")
        # vLLM 服务端会将排队的请求合并为批量调用，此处同样执行一次批量调用
        try:
            batch = tokenize(local, [texts[index] for index in indices])
            for index, ids in zip(indices, batch):
                if digest_of(ids) != digests[index]:
                    with lock:
                        mismatched.append(index)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"批量调用：{e.__class__.__name__}: {str(e)[:200]}")

    workers = [threading.Thread(target=run, args=(group,), daemon=True) for group in groups]
    started = time.perf_counter()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return {
        "threads": len(workers),
        "requests": len(texts),
        "mismatch": sorted(mismatched),
        "errors": errors[:10],
        "wall_s": round(time.perf_counter() - started, 2),
    }


# ---------------------------------------------------------------- verify


def parse_env_overrides(items) -> dict:
    overrides = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(
                f"--orig-env 的格式应为 KEY=VALUE（KEY= 表示删除该变量），实际为 {item!r}"
            )
        key, value = item.split("=", 1)
        overrides[key] = value
    return overrides


def apply_env(base, overrides) -> dict:
    env = dict(base)
    for key, value in overrides.items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    return env


def read_ids(path, offset, length):
    ids = array("I")
    with open(path, "rb") as f:
        f.seek(offset * ids.itemsize)
        ids.fromfile(f, length)
    return ids


def first_diff(left, right):
    for position, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return position
    return None if len(left) == len(right) else min(len(left), len(right))


def compare_arms(reference, other, reference_ids, other_ids, requests, limit=5) -> list:
    """逐条比较 token ID；不一致时从 ID 文件中定位首个不同位置。"""
    diffs, offset_ref, offset_other = [], 0, 0
    have_files = all(path and os.path.exists(path) for path in (reference_ids, other_ids))
    for index, (digest_ref, digest_other) in enumerate(
        zip(reference["digests"], other["digests"])
    ):
        length_ref, length_other = reference["lengths"][index], other["lengths"][index]
        if digest_ref != digest_other and len(diffs) < limit:
            position = None
            if have_files:
                position = first_diff(
                    read_ids(reference_ids, offset_ref, length_ref),
                    read_ids(other_ids, offset_other, length_other),
                )
            diffs.append(
                {
                    "request": index,
                    "session": requests[index]["session"],
                    "len_ref": length_ref,
                    "len_other": length_other,
                    "first_diff": position,
                }
            )
        elif digest_ref != digest_other:
            diffs.append({"request": index})
        offset_ref += length_ref
        offset_other += length_other
    return diffs


def cmd_verify(args):
    requests = load_requests(args.sessions)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = dict(os.environ)
    cache_on = with_overlay(base, args.overlay)
    cache_on[SWITCH_VAR] = "1"
    cache_on[CACHE_VAR] = str(args.cache_mib)
    arms = {
        "orig": apply_env(base, parse_env_overrides(args.orig_env)),
        "on": cache_on,
    }
    names = list(arms)

    common = model_args(args) + ["--sessions", os.path.abspath(args.sessions)]
    if args.chat_template:
        common += ["--chat-template", os.path.abspath(args.chat_template)]
    load_before = round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None
    runs = {name: [] for name in names}
    for round_index in range(args.rounds):
        shift = round_index % len(names)
        order = names[shift:] + names[:shift]
        for name in order:
            extra = ["--threads", str(args.threads if round_index == 0 else 1)]
            if round_index == 0:
                extra += ["--ids-out", str(out / f"{name}.ids")]
            print(
                f"[{now()}] 第 {round_index + 1}/{args.rounds} 轮：{ARM_LABELS[name]}",
                file=sys.stderr,
                flush=True,
            )
            runs[name].append(run_worker(arms[name], common + extra, args.timeout))
    load_after = round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None

    report = summarize(args, requests, runs, out, load_before, load_after)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    text = verify_text(report)
    (out / "report.md").write_text(text)
    if not args.keep_ids:
        for name in names:
            ids_file = out / f"{name}.ids"
            if ids_file.exists():
                ids_file.unlink()
    print(text)
    print(f"\n报告：{(out / 'report.md').resolve()}")
    sys.exit(0 if report["correctness_pass"] else 1)


def summarize_arm(name, results, request_count, problems) -> dict:
    first = results[0]
    per_request = [median([r["encode_ms"][i] for r in results]) for i in range(request_count)]
    totals = [sum(r["encode_ms"]) for r in results]
    arm = {
        "ok": True,
        "backend": f"{first['backend_module']}.{first['backend_class']}",
        "fastokens_version": first.get("fastokens_version"),
        "env": first.get("env"),
        "encode_total_ms": round(median(totals), 1),
        "encode_total_ms_rounds": [round(t, 1) for t in totals],
        "p50_ms": round(percentile(per_request, 50), 3),
        "p99_ms": round(percentile(per_request, 99), 3),
        "render_ms": first.get("render_ms"),
        "peak_rss_mib": max((r.get("peak_rss_mib") or 0) for r in results),
        "rss_after_load_mib": median([r.get("rss_after_load_mib") or 0 for r in results]),
        "rss_after_replay_mib": median([r.get("rss_after_replay_mib") or 0 for r in results]),
        "stats": first.get("stats"),
        "concurrency": first.get("concurrency"),
        "text_sha": first.get("text_sha"),
    }
    if any(r["digests"] != first["digests"] for r in results[1:]):
        problems.append(f"{ARM_LABELS[name]}各轮的 token ID 不一致")
    if name != "orig" and not first["backend_module"].startswith("fastokens"):
        problems.append(
            f"{ARM_LABELS[name]}生效的分词后端为 {first['backend_module']}，"
            "并非 fastokens（候选版本未生效）"
        )
    concurrency = first.get("concurrency")
    if concurrency and (concurrency["mismatch"] or concurrency["errors"]):
        problems.append(
            f"{ARM_LABELS[name]} {concurrency['threads']} 线程并发检查："
            f"{len(concurrency['mismatch'])} 条结果"
            f"不一致，{len(concurrency['errors'])} 个异常"
        )
    return arm


def summarize(args, requests, runs, out, load_before, load_after) -> dict:
    problems, arms = [], {}
    for name, results in runs.items():
        failed = next((r for r in results if not r.get("ok")), None)
        if failed:
            problems.append(f"{ARM_LABELS[name]}运行失败：{failed.get('error')}")
            arms[name] = {
                "ok": False,
                "error": failed.get("error"),
                "stderr_tail": failed.get("stderr_tail"),
            }
        else:
            arms[name] = summarize_arm(name, results, len(requests), problems)

    parity = {}
    if all(arms[name].get("ok") for name in runs):
        reference = runs["orig"][0]
        for name in ("on",):
            diffs = compare_arms(
                reference, runs[name][0], str(out / "orig.ids"), str(out / f"{name}.ids"), requests
            )
            parity[name] = diffs
            if diffs:
                problems.append(
                    f"{ARM_LABELS[name]}与基线组相比有 {len(diffs)} 条请求的 token ID 不一致"
                )
        if len({arms[name]["text_sha"] for name in runs}) > 1:
            problems.append(
                "两组渲染得到的模板文本不一致"
                "（可能原因：候选安装目录改变了 transformers 或聊天模板）"
            )
        stats = arms["on"].get("stats")
        if stats is None:
            problems.append(
                "段缓存开启组未提供 segment_cache_stats()，缓存命中情况无法确认"
            )
        elif not stats.get("hits"):
            problems.append(
                "段缓存开启组命中数为 0"
                "（可能原因：回放数据中无重复的文本段，或段缓存未启用）"
            )
        orig_total = arms["orig"]["encode_total_ms"]
        for name in runs:
            total = arms[name]["encode_total_ms"]
            arms[name]["speed_vs_orig"] = round(orig_total / total, 2) if total else None
    return {
        "time": now(),
        "model": args.model,
        "sessions_file": os.path.abspath(args.sessions),
        "requests": len(requests),
        "sessions": len({r["session"] for r in requests}),
        "rounds": args.rounds,
        "threads": args.threads,
        "cache_mib": args.cache_mib,
        "overlay": os.path.abspath(args.overlay) if args.overlay else None,
        "cpu_count": os.cpu_count(),
        "load1_before": load_before,
        "load1_after": load_after,
        "arms": arms,
        "parity": parity,
        "problems": problems,
        "correctness_pass": not problems,
    }


def cache_cell(stats) -> str:
    if not stats:
        return "-"
    hit_bytes, miss_bytes = stats.get("hit_bytes", 0), stats.get("miss_bytes", 0)
    rate = f"{hit_bytes / (hit_bytes + miss_bytes):.1%}" if hit_bytes + miss_bytes else "-"
    used = stats.get("bytes", 0) / 1048576
    return (
        f"命中 {stats.get('hits')} / 未命中 {stats.get('misses')}，"
        f"字节命中率 {rate}，占用 {used:.2f} MiB"
    )


def verify_text(report) -> str:
    lines = ["# fastokens 段缓存：分词器层对比", ""]
    lines.append(f"- 时间：{report['time']}；模型：{report['model']}")
    lines.append(
        f"- 回放数据：{report['requests']} 条请求，{report['sessions']} 个会话"
        f"（{report['sessions_file']}）；共 {report['rounds']} 轮，每轮轮换各组顺序；"
        f"并发检查线程数 {report['threads']}"
    )
    lines.append(
        f"- 候选安装目录：{report['overlay'] or '未指定（使用环境中已安装的 fastokens）'}；"
        f"段缓存开启组 {CACHE_VAR}={report['cache_mib']}"
    )
    lines.append(
        "- 测量层级：分词器层。调用 vLLM 的 get_tokenizer、apply_chat_template 与 tokenizer(...)"
        "（参数与 vLLM 服务端一致），不启动推理引擎；计时范围仅为分词调用"
    )
    lines.append(
        f"- 机器：{report['cpu_count']} 个 CPU；load1 开始时 {report['load1_before']}，"
        f"结束时 {report['load1_after']}（负载较高时计时结果不可靠）"
    )
    lines.append("")
    passed = report["correctness_pass"]
    lines.append(f"## 一致性与稳定性：{'通过' if passed else '未通过'}")
    lines += [f"- {problem}" for problem in report["problems"]]
    for name, diffs in report["parity"].items():
        for diff in diffs[:5]:
            if "len_ref" in diff:
                lines.append(
                    f"  - {ARM_LABELS[name]}：请求 {diff['request']}"
                    f"（会话 {diff['session']}），长度 "
                    f"{diff['len_ref']} / {diff['len_other']}，首个不同位置 {diff['first_diff']}"
                )
    if passed:
        lines.append(
            f"- 两组 token ID 在全部 {report['requests']} 条请求上逐条一致；"
            "各轮结果一致；并发检查未发现结果不一致或异常"
        )
    lines.append("")
    lines.append("## 分词耗时与内存")
    lines.append("")
    lines.append(
        "| 组 | 生效的分词后端 | fastokens | 分词总耗时（ms，各轮中位数） | 单请求 p50 / p99（ms） "
        "| 相对基线组的速度 | RSS（MiB）：加载后 → 回放后（峰值） | 段缓存 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, arm in report["arms"].items():
        if not arm.get("ok"):
            lines.append(f"| {ARM_LABELS[name]} | 运行失败：{arm.get('error')} | | | | | | |")
            continue
        ratio = arm.get("speed_vs_orig")
        rss = (
            f"{arm['rss_after_load_mib']} → {arm['rss_after_replay_mib']}（{arm['peak_rss_mib']}）"
        )
        lines.append(
            f"| {ARM_LABELS[name]} | {arm['backend']} | {arm.get('fastokens_version') or '-'} "
            f"| {arm['encode_total_ms']} | {arm['p50_ms']} / {arm['p99_ms']} "
            f"| {f'{ratio} 倍' if ratio else '-'} | {rss} | {cache_cell(arm.get('stats'))} |"
        )
    lines.append("")
    lines.append("说明：")
    lines.append("- 相对基线组的速度 = 基线组分词总耗时 ÷ 本组分词总耗时；各组计时前均预热一次。")
    lines.append("- 测量值是否满足要求，依据用户给定的阈值判断。")
    lines.append("- 段缓存命中计数仅统计回放部分，已扣除预热产生的计数。")
    lines.append(
        "- RSS 为单个分词器测试进程的常驻内存（含导入 vLLM 所占内存），与服务进程内存不同；"
        "服务内存采用切换前后 probe 采集的 vLLM 进程 RSS。"
    )
    if report["arms"].get("orig", {}).get("ok"):
        lines.append(f"- 基线组环境变量：{report['arms']['orig'].get('env') or '无相关变量'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 命令行


def add_model_options(parser):
    parser.add_argument(
        "--model",
        help="模型名或本地分词器路径，取值为服务的 --tokenizer 参数值"
        "（服务未指定 --tokenizer 时取 --model 的值）",
    )
    parser.add_argument(
        "--tokenizer-mode", default="auto", help="分词模式，取值同服务的 --tokenizer-mode"
    )
    parser.add_argument("--revision")
    parser.add_argument("--trust-remote-code", action="store_true")


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    probe = sub.add_parser("probe", help="环境探测与迁移判定")
    add_model_options(probe)
    probe.add_argument(
        "--overlay", help="候选安装目录，即候选版本 fastokens 所在的目录"
    )
    probe.add_argument("--pid", type=int, action="append", help="仅检查指定进程（Linux）")
    probe.add_argument("--target-version", help="目标版本；未指定时取 PyPI 最新版本")
    probe.add_argument("--offline", action="store_true", help="不访问 PyPI")
    probe.add_argument("--timeout", type=int, default=600)
    probe.add_argument("--out", help="将完整结果写入该目录下的 probe.json")
    probe.set_defaults(func=cmd_probe)

    snapshot = sub.add_parser("snapshot", help="迁移前快照与回退说明")
    snapshot.add_argument("--out", required=True)
    snapshot.add_argument(
        "--pid", type=int, action="append", help="同时记录指定进程的环境变量（Linux）"
    )
    snapshot.set_defaults(func=cmd_snapshot)

    sessions = sub.add_parser("make-sessions", help="生成合成多轮会话，作为回放数据")
    sessions.add_argument("--out", required=True)
    sessions.add_argument("--sessions", type=int, default=8, help="会话数")
    sessions.add_argument("--turns", type=int, default=12, help="每个会话的轮数")
    sessions.add_argument(
        "--block-chars", type=int, default=6000, help="每隔一轮附带的工具输出长度（字符）"
    )
    sessions.add_argument("--seed", type=int, default=0, help="随机种子；参数相同时生成的数据相同")
    sessions.set_defaults(func=cmd_make_sessions)

    verify = sub.add_parser("verify", help="分词器层的两组对比：基线组与段缓存开启组")
    add_model_options(verify)
    verify.add_argument(
        "--sessions", required=True, help="回放数据：make-sessions 生成的 JSONL 文件"
    )
    verify.add_argument(
        "--overlay", help="候选安装目录；未指定时使用环境中已安装的 fastokens"
    )
    verify.add_argument(
        "--orig-env", action="append", help="基线组环境变量，格式 KEY=VALUE；KEY= 表示删除该变量"
    )
    verify.add_argument("--cache-mib", type=int, default=256)
    verify.add_argument("--chat-template", help="聊天模板文件，取值同服务的 --chat-template")
    verify.add_argument("--rounds", type=int, default=3)
    verify.add_argument("--threads", type=int, default=8, help="并发检查线程数（在第 1 轮执行）")
    verify.add_argument(
        "--timeout", type=int, default=1800, help="每组每轮的超时时间（秒），超时按死锁处理"
    )
    verify.add_argument("--out", required=True)
    verify.add_argument("--keep-ids", action="store_true", help="保留各组的 token ID 文件")
    verify.set_defaults(func=cmd_verify)

    worker = sub.add_parser("_worker")
    add_model_options(worker)
    worker.add_argument("--load-only", action="store_true")
    worker.add_argument("--sessions")
    worker.add_argument("--chat-template")
    worker.add_argument("--threads", type=int, default=1)
    worker.add_argument("--ids-out")
    worker.set_defaults(func=cmd_worker)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "verify" and not args.model:
        parser.error("verify 子命令缺少 --model 参数")
    args.func(args)


if __name__ == "__main__":
    main()
