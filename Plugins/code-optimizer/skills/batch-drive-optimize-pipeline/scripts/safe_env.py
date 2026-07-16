"""Safe child-process environment construction for batch-drive workers."""

from __future__ import annotations

import os
from pathlib import Path


SAFE_CHILD_ENV_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}
SENSITIVE_ENV_KEYWORDS = (
    "ACCESS_TOKEN",
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


def extra_path_dirs() -> list[Path]:
    home = Path.home()
    paths = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
        home / ".npm" / "bin",
        home / ".yarn" / "bin",
        home / ".config" / "yarn" / "global" / "node_modules" / ".bin",
        home / ".bun" / "bin",
        home / ".deno" / "bin",
        home / ".cargo" / "bin",
    ]
    for name in ("HOMEBREW_PREFIX", "LOCAL_PREFIX", "BREW_PREFIX"):
        value = os.environ.get(name)
        if value:
            paths.append(Path(value) / "bin")
    return paths


def child_env(claude_bin: str | None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_CHILD_ENV_KEYS
        and not any(marker in key.upper() for marker in SENSITIVE_ENV_KEYWORDS)
    }
    parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    extras = [str(path) for path in extra_path_dirs() if path.is_dir()]
    seen: set[str] = set()
    env["PATH"] = os.pathsep.join([p for p in extras + parts if not (p in seen or seen.add(p))])
    if claude_bin:
        env["CLAUDE_BIN"] = claude_bin
    env.setdefault("DISABLE_UPDATES", "1")
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env.setdefault("NO_UPDATE_NOTIFIER", "1")
    env.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
    env.setdefault("HOMEBREW_NO_ENV_HINTS", "1")
    return env
