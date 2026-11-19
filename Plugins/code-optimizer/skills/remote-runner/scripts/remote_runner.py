#!/usr/bin/env python3
"""KPBot 远端目标模式：把本地工作树同步到受限 SSH 主机，执行白名单脚本并回传日志。

只依赖 Python 3 标准库与本地 ssh/scp；本地/远端都可用 rsync 时走增量同步，
否则回退为「临时目录 + scp 全量 + 整体替换」。不做指标解析，日志交给 Agent 读取。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ENGINE_VERSION = "1.0.0"

CONFIG_REL = Path(".kpbot") / "remote" / "config.json"
REMOTE_REL = Path(".kpbot") / "remote"
RUNS_REL = Path(".kpbot") / "runs"
LAST_SYNC_REL = Path(".kpbot") / "remote" / "last_sync.json"

ACTIONS = ("build", "test", "bench", "collect")
DEFAULT_TIMEOUT = 3600
DEFAULT_TRANSFER_TIMEOUT = 3600
DEFAULT_PULL_MAX_MB = 2048
TAIL_LINES = 40

SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
)

# 同步与过期检测共用同一套排除规则。
SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", ".opencode", ".claude",
    "build", "dist", "out",
}
CREDENTIAL_DIR_NAMES = {
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".azure",
    "credentials", "secrets",
}
CREDENTIAL_FILE_PATTERNS = (
    ".env", ".env.*",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.ppk",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "credentials*", "secrets*",
    "opencode.json", "opencode.jsonc", "auth.json",
)
RSYNC_EXCLUDES = [
    "--exclude=.git/",
    "--exclude=.kpbot/runs/",
    "--exclude=.kpbot/hosts/",
    "--exclude=node_modules/",
    "--exclude=__pycache__/",
    "--exclude=.venv/",
    "--exclude=.opencode/",
    "--exclude=.claude/",
    "--exclude=build/",
    "--exclude=dist/",
    "--exclude=out/",
    "--exclude=last_sync.json",
    "--exclude=.sync.lock",
    "--exclude=.kpbot/remote/config.json",
]
CREDENTIAL_RSYNC_EXCLUDES = [
    f"--exclude={name}/" for name in sorted(CREDENTIAL_DIR_NAMES)
] + [f"--exclude={pattern}" for pattern in CREDENTIAL_FILE_PATTERNS]

CONTENT_SCAN_MAX_BYTES = 256 * 1024
CONTENT_SCAN_SUFFIXES = {
    ".env", ".yaml", ".yml", ".json", ".toml", ".ini", ".conf", ".cfg",
    ".properties", ".txt", ".md",
}
SECRET_CONTENT_PATTERNS = (
    re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"ASIA[0-9A-Z]{16}"),
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
)

FORBIDDEN_EXACT = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64",
    "/media", "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv",
    "/sys", "/tmp", "/usr", "/var",
}
FORBIDDEN_PREFIX = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root",
    "/run", "/sbin", "/sys", "/tmp", "/usr", "/var",
)

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
FORBIDDEN_REMOTE_CHARS = {
    chr(92), '"', "'", "`", "$", ";", "&", "|", "<", ">",
    "(", ")", "{", "}", "[", "]", "*", "?", "!", "#", "~", "%", ":",
}

SYSTEM_PROJECT_PATHS = {
    "/usr", "/etc", "/var", "/boot", "/proc", "/sys", "/dev",
    "/bin", "/sbin", "/lib", "/lib64", "/run",
}


class UserError(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        write_text_lf(temp_path, json.dumps(data, ensure_ascii=False, indent=2) + chr(10))
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def json_print(data: Dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UserError(f"找不到文件：{path}")
    except (OSError, ValueError) as exc:
        raise UserError(f"读取失败：{path}（{exc}）")


def tail(text: str, limit: int = TAIL_LINES) -> str:
    lines = text.rstrip("\n").splitlines()
    return "\n".join(lines[-limit:])


def tail_text_file(path: Path, limit: int = TAIL_LINES) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(deque(handle, maxlen=limit))
    except OSError:
        return ""


def _windows_local_path(value: str) -> str:
    """wrapper 设置 MSYS_NO_PATHCONV=1 后，把本地 /c/... 还原成 C:/...。"""
    if os.name != "nt":
        return value
    match = re.match(r"^/([A-Za-z])/(.*)$", value)
    if not match:
        return value
    candidate = f"{match.group(1).upper()}:/{match.group(2)}"
    return candidate if Path(candidate).exists() else value


def project_path(value: str) -> Path:
    path = Path(_windows_local_path(value)).expanduser().resolve()
    if not path.is_dir():
        raise UserError(f"项目目录不存在：{path}")
    if path == Path(path.anchor):
        raise UserError("--project 不能是文件系统根目录")
    try:
        if path == Path.home().resolve():
            raise UserError("--project 不能是用户主目录")
    except OSError:
        pass
    if str(path) in SYSTEM_PROJECT_PATHS:
        raise UserError(f"--project 不能是系统目录：{path}")
    if os.name == "nt":
        protected = {
            os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")),
            os.path.normcase(os.environ.get("ProgramFiles", r"C:\Program Files")),
            os.path.normcase(
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            ),
        }
        if os.path.normcase(str(path)) in protected:
            raise UserError(f"--project 不能是系统目录：{path}")
    return path


def project_id(project: Path) -> str:
    key = str(project.resolve())
    if os.name == "nt":
        key = key.lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def _unmangle_git_bash_path(value: str) -> str:
    """还原 Git Bash/MSYS 对以 / 开头的参数做的 Windows 路径转换。"""
    for pattern in (
        r"^[A-Za-z]:/Program Files/Git/(.+)$",
        r"^[A-Za-z]:/Program Files \(x86\)/Git/(.+)$",
        r"^[A-Za-z]:/msys(?:32|64)/(.+)$",
        r"^[A-Za-z]:/Git/(.+)$",
    ):
        match = re.match(pattern, value)
        if match:
            return "/" + match.group(1)
    return value


def validate_remote_chars(value: str, label: str) -> None:
    for ch in value:
        if (
            ch.isspace()
            or ord(ch) < 32
            or ord(ch) == 127
            or ch in FORBIDDEN_REMOTE_CHARS
        ):
            raise UserError(f"{label} 不能包含空白或 shell 特殊字符：{ch!r}")

def normalize_workspace(value: str) -> str:
    ws = _unmangle_git_bash_path((value or "").strip())
    if not ws:
        raise UserError("workspace 不能为空")
    if "\\" in ws:
        raise UserError("workspace 必须是远端 POSIX 绝对路径（不能使用反斜杠）")
    if re.match(r"^[A-Za-z]:", ws):
        raise UserError("workspace 必须是远端 POSIX 绝对路径，不能是 Windows 盘符路径")
    if not ws.startswith("/"):
        raise UserError("workspace 必须是绝对路径，例如 /ssd/KBot-workspace")
    if any(ch.isspace() for ch in ws):
        raise UserError("workspace 不能包含空白字符")
    normalized = posixpath.normpath(ws)
    if normalized in FORBIDDEN_EXACT:
        raise UserError(f"workspace 不能是系统目录：{normalized}")
    for prefix in FORBIDDEN_PREFIX:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            raise UserError(f"workspace 不能位于系统目录下：{normalized}")
    validate_remote_chars(normalized, "workspace")
    if len([part for part in normalized.split("/") if part]) < 2:
        raise UserError("workspace 必须是专用目录（至少两级，例如 /ssd/KBot-workspace）")
    return normalized


def normalize_remote_relative(value: str) -> str:
    raw = (value or "").strip().replace("\\", "/")
    if not raw:
        raise UserError("pull 需要 --path")
    normalized = posixpath.normpath(raw)
    if raw.startswith("/") or normalized in (".", "..") or normalized.startswith("../"):
        raise UserError("--path 必须是项目内相对路径，不能是绝对路径或包含 ..")
    validate_remote_chars(normalized, "--path")
    return normalized


def validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise UserError("配置必须是 JSON 对象")

    host = str(raw.get("host", "")).strip()
    if not HOST_RE.match(host):
        raise UserError("host 无效（只支持 IPv4/主机名，不允许空格或参数前缀）")

    user = str(raw.get("user", "")).strip()
    if not USER_RE.match(user):
        raise UserError("user 无效（只允许字母、数字、下划线、点、短横线）")

    try:
        port = int(raw.get("port", 22))
    except (TypeError, ValueError):
        raise UserError("port 必须是整数")
    if not 1 <= port <= 65535:
        raise UserError("port 必须在 1-65535 之间")

    workspace = normalize_workspace(str(raw.get("workspace", "")))
    return {"host": host, "user": user, "port": port, "workspace": workspace}


def config_file(project: Path) -> Path:
    return project / CONFIG_REL


def load_config(project: Path) -> Dict[str, Any]:
    path = config_file(project)
    if not path.is_file():
        raise UserError(f"未找到 {CONFIG_REL.as_posix()}；请先运行 init", 2)
    return validate_config(read_json(path))


def remote_root(cfg: Dict[str, Any], project: Path) -> str:
    return f"{cfg['workspace'].rstrip('/')}/.kpbot/projects/{project_id(project)}"


def remote_src(cfg: Dict[str, Any], project: Path) -> str:
    return remote_root(cfg, project) + "/src"


def remote_build(cfg: Dict[str, Any], project: Path) -> str:
    return remote_root(cfg, project) + "/build"


def ssh_target(cfg: Dict[str, Any]) -> str:
    return f"{cfg['user']}@{cfg['host']}"


def ssh_base(cfg: Dict[str, Any]) -> List[str]:
    return ["ssh", *SSH_OPTS, "-p", str(cfg["port"]), ssh_target(cfg)]


def ssh_e(cfg: Dict[str, Any]) -> str:
    return " ".join(["ssh", *SSH_OPTS, "-p", str(cfg["port"])])


def scp_base(cfg: Dict[str, Any]) -> List[str]:
    return ["scp", "-r", *SSH_OPTS, "-P", str(cfg["port"])]


def q(value: str) -> str:
    return shlex.quote(value)


def run_ssh(
    cfg: Dict[str, Any], command: str, timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ssh_base(cfg) + [command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_scp(
    cfg: Dict[str, Any],
    sources: Iterable[str],
    target: str,
    cwd: Path,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        scp_base(cfg) + [*sources, target],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def remote_has(cfg: Dict[str, Any], name: str) -> bool:
    try:
        proc = run_ssh(cfg, f"command -v {q(name)} >/dev/null 2>&1", timeout=15)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ssh_error(cfg: Dict[str, Any], proc: subprocess.CompletedProcess) -> UserError:
    detail = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode == 255:
        hint = ""
        if "Permission denied" in detail:
            hint = "；请先配置 SSH 免密登录"
        elif "Host key verification failed" in detail:
            hint = "；主机指纹校验失败，请先确认主机指纹"
        elif (
            "Could not resolve" in detail
            or "Connection refused" in detail
            or "timed out" in detail
        ):
            hint = "；请检查 host/port 与网络"
        message = f"SSH 连接失败（exit=255）{hint}"
    else:
        message = f"远端命令失败（exit={proc.returncode}）"
    if detail:
        message += f"\n{tail(detail)}"
    return UserError(message, 4)


def is_credential(rel: str, is_dir: bool = False) -> bool:
    parts = [part.lower() for part in rel.split("/")]
    if any(part in CREDENTIAL_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1]
    if is_dir and name in CREDENTIAL_DIR_NAMES:
        return True
    return any(
        fnmatch.fnmatch(name, pattern.lower()) for pattern in CREDENTIAL_FILE_PATTERNS
    )


def has_secret_content(path: Path, rel: str) -> bool:
    suffix = Path(rel).suffix.lower()
    if suffix and suffix not in CONTENT_SCAN_SUFFIXES:
        return False
    try:
        if path.stat().st_size > CONTENT_SCAN_MAX_BYTES:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if bytes([0]) in data:
        return False
    return any(pattern.search(data) for pattern in SECRET_CONTENT_PATTERNS)


def find_credential_files(project: Path) -> List[Tuple[Path, str]]:
    found = []
    for full, rel in iter_source_files(project, include_credentials=True):
        if is_credential(rel) or has_secret_content(full, rel):
            found.append((full, rel))
    return found


def find_symlink_escapes(project: Path) -> List[Tuple[str, str]]:
    project = project.resolve()
    escapes: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(project, followlinks=False):
        rel_root = os.path.relpath(root, project)
        rel_parts = () if rel_root == "." else tuple(Path(rel_root).parts)
        for name in list(dirs) + files:
            if name in dirs and _skip_dir(rel_parts, name, include_credentials=True):
                continue
            path = Path(root) / name
            try:
                if not path.is_symlink():
                    continue
                target = path.resolve()
                if target != project and project not in target.parents:
                    rel = (
                        name
                        if rel_root == "."
                        else f"{Path(rel_root).as_posix()}/{name}"
                    )
                    escapes.append((rel, str(target)))
            except OSError:
                continue
        dirs[:] = sorted(
            name
            for name in dirs
            if not _skip_dir(rel_parts, name, include_credentials=True)
        )
    return escapes


def _skip_dir(rel_parts: Tuple[str, ...], name: str, include_credentials: bool) -> bool:
    if name in SKIP_DIR_NAMES:
        return True
    if not include_credentials and name.lower() in CREDENTIAL_DIR_NAMES:
        return True
    return rel_parts == (".kpbot",) and name in ("runs", "hosts")


def iter_source_files(
    project: Path, include_credentials: bool = False
) -> Iterable[Tuple[Path, str]]:
    project = project.resolve()
    for root, dirs, files in os.walk(project):
        rel_root = os.path.relpath(root, project)
        rel_parts = () if rel_root == "." else tuple(Path(rel_root).parts)
        dirs[:] = sorted(
            name
            for name in dirs
            if not _skip_dir(rel_parts, name, include_credentials)
        )
        for name in sorted(files):
            if name in (LAST_SYNC_REL.name, ".sync.lock") or name.endswith(".pyc"):
                continue
            full = Path(root) / name
            rel = name if rel_root == "." else str(Path(rel_root) / name).replace("\\", "/")
            if rel == CONFIG_REL.as_posix():
                continue
            if not include_credentials and is_credential(rel):
                continue
            yield full, rel


def source_fingerprint(project: Path) -> Tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for full, rel in iter_source_files(project):
        try:
            stat = full.stat()
        except OSError:
            continue
        digest.update(f"{rel}\t{stat.st_size}\t{stat.st_mtime_ns}\n".encode("utf-8"))
        count += 1
    return digest.hexdigest(), count


def assert_fresh(project: Path, allow_stale: bool) -> Tuple[str, int]:
    marker = project / LAST_SYNC_REL
    if not marker.is_file():
        raise UserError("尚未同步：请先运行 sync", 7)
    try:
        data = read_json(marker)
    except UserError as exc:
        raise UserError(f"同步标记损坏：{exc}", 7) from exc
    marker_fingerprint = str(data.get("fingerprint", ""))
    if not marker_fingerprint:
        raise UserError("同步标记缺少指纹：请重新运行 sync", 7)
    fingerprint, count = source_fingerprint(project)
    if allow_stale:
        return fingerprint, count
    if fingerprint != marker_fingerprint:
        raise UserError(
            "本地源码与上次同步的指纹不一致；请先运行 sync（或确认风险后加 --allow-stale）",
            7,
        )
    return fingerprint, count


def is_sync_excluded(
    rel: str,
    include_credentials: bool,
    is_dir: bool = False,
    credential_paths: Optional[set] = None,
) -> bool:
    parts = rel.split("/")
    if any(part in SKIP_DIR_NAMES for part in parts):
        return True
    if rel == CONFIG_REL.as_posix():
        return True
    if parts and parts[0] == ".kpbot" and len(parts) > 1 and parts[1] in ("runs", "hosts"):
        return True
    if parts and (
        parts[-1] in (LAST_SYNC_REL.name, ".sync.lock")
        or parts[-1].endswith(".pyc")
    ):
        return True
    if credential_paths and rel in credential_paths:
        return True
    if not include_credentials and is_credential(rel, is_dir=is_dir):
        return True
    return False


def create_sync_tarball(
    project: Path, include_credentials: bool, credential_paths: Optional[set] = None
) -> Path:
    fd, name = tempfile.mkstemp(prefix="kpbot-sync-", suffix=".tar.gz")
    os.close(fd)
    path = Path(name)

    def filter_func(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        rel = info.name[2:] if info.name.startswith("./") else info.name
        if rel in ("", "."):
            return info
        if is_sync_excluded(
            rel,
            include_credentials,
            is_dir=info.isdir(),
            credential_paths=credential_paths,
        ):
            return None
        return info

    try:
        with tarfile.open(path, "w:gz") as archive:
            archive.add(str(project), arcname=".", filter=filter_func)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def ensure_remote_dir(cfg: Dict[str, Any], path: str, timeout: Optional[float] = 60) -> None:
    proc = run_ssh(cfg, f"mkdir -p {q(path)}", timeout=timeout)
    if proc.returncode != 0:
        raise UserError(f"无法创建远端目录 {path}\n{tail(proc.stderr or proc.stdout)}", 6)


def known_host_fingerprints(cfg: Dict[str, Any]) -> List[str]:
    """读取 known_hosts 中该主机的指纹，供用户在首次 scp 前确认。"""
    if not shutil.which("ssh-keygen"):
        return []
    lookup = cfg["host"] if cfg["port"] == 22 else f"[{cfg['host']}]:{cfg['port']}"
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-l", "-F", lookup],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def git_worktree(project: Path) -> bool:
    if not shutil.which("git"):
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def tracked_kpbot_files(project: Path) -> List[str]:
    if not git_worktree(project):
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "ls-files", "--", ".kpbot"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def ensure_kpbot_gitignore(project: Path, verbose: bool = False) -> None:
    if not git_worktree(project):
        if verbose:
            print("[info] 非 git 工作区，跳过 .gitignore 更新", file=sys.stderr)
        return
    path = project / ".gitignore"
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[warn] 无法读取 {path}：{exc}", file=sys.stderr)
            return
    entries = {line.strip() for line in existing.splitlines()}
    if ".kpbot/" in entries or ".kpbot" in entries:
        if verbose:
            print("[ok] .gitignore 已包含 .kpbot/", file=sys.stderr)
    else:
        try:
            with path.open("a", encoding="utf-8", newline="") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                if existing:
                    handle.write("\n")
                handle.write("# KPBot remote state (host/config/logs; do not commit)\n")
                handle.write(".kpbot/\n")
        except OSError as exc:
            print(f"[warn] 无法写入 {path}：{exc}", file=sys.stderr)
            return
        print(f"[ok] 已将 .kpbot/ 加入 {path}", file=sys.stderr)
    warn_if_kpbot_tracked(project)


def warn_if_kpbot_tracked(project: Path) -> None:
    if tracked_kpbot_files(project):
        print(
            "[warn] .kpbot/ 已被 git 跟踪，host/config/logs 可能外泄；"
            "建议执行 git rm -r --cached .kpbot",
            file=sys.stderr,
        )


def write_last_sync(
    project: Path,
    cfg: Dict[str, Any],
    method: str,
    count: int,
    fingerprint: str,
    include_credentials: bool,
    credential_files: int,
) -> None:
    write_json(
        project / LAST_SYNC_REL,
        {
            "epoch": time.time(),
            "iso": utc_now().isoformat(),
            "method": method,
            "files": count,
            "fingerprint": fingerprint,
            "include_credentials": include_credentials,
            "credential_files": credential_files,
            "project_id": project_id(project),
            "remote_src": remote_src(cfg, project),
        },
    )
