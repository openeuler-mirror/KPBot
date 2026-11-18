"""Construct credential-free child environments for local package validation.

This controls environment inheritance and configuration lookup, not filesystem
access or network access. Run untrusted repository code in an isolated runner.
"""

import os
from pathlib import Path
import shutil
import sys


def require_executable(name, label):
    resolved = shutil.which(name)
    if not resolved:
        raise ValueError(f"required {label} executable not found: {name}; validation cannot be skipped")
    return str(Path(resolved).resolve())


def child_environment(directory, *, executables=()):
    """Start from an empty map; only OS support values may come from the caller."""
    root = Path(directory).resolve()
    paths = {name: root / name for name in ("home", "tmp", "go-cache", "go-path", "config", "cache", "data")}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    tool_directories = [str(Path(value).resolve().parent) for value in [sys.executable, *executables]]
    environment = {
        "HOME": str(paths["home"]), "USERPROFILE": str(paths["home"]),
        "TMPDIR": str(paths["tmp"]), "TMP": str(paths["tmp"]), "TEMP": str(paths["tmp"]),
        "XDG_CONFIG_HOME": str(paths["config"]), "XDG_CACHE_HOME": str(paths["cache"]),
        "XDG_DATA_HOME": str(paths["data"]),
        "GOCACHE": str(paths["go-cache"]), "GOPATH": str(paths["go-path"]),
        "GOMODCACHE": str(paths["go-path"] / "pkg/mod"),
        "GOENV": "off", "GOFLAGS": "", "GOWORK": "off", "GOTOOLCHAIN": "local",
        "GOPROXY": "off", "GOSUMDB": "off", "CGO_ENABLED": "0",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    if os.name == "nt":
        windows = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:/Windows").resolve()
        environment.update({
            "SystemRoot": str(windows), "WINDIR": str(windows),
            "COMSPEC": str(windows / "System32/cmd.exe"), "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "APPDATA": str(paths["home"] / "AppData/Roaming"),
            "LOCALAPPDATA": str(paths["home"] / "AppData/Local"),
            "HOMEDRIVE": paths["home"].drive,
            "HOMEPATH": str(paths["home"])[len(paths["home"].drive):],
        })
        tool_directories.extend([str(windows / "System32"), str(windows)])
    else:
        environment.update(LANG="C.UTF-8", LC_ALL="C.UTF-8")
        tool_directories.extend(["/usr/bin", "/bin"])
    environment["PATH"] = os.pathsep.join(dict.fromkeys(tool_directories))
    return environment
