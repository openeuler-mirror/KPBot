"""Create private analysis artifacts without following output symlinks."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def _directory_fd(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path, flags)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise PermissionError("output directory must be owned by the current user with mode 0700")
    return fd


def prepare_output_dir(path):
    directory = Path(path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(_directory_fd(directory))


def private_open(path, mode="w", *, encoding="utf-8", newline=None):
    if mode != "w":
        raise ValueError("private_open only creates new text files")
    target = Path(path)
    directory = _directory_fd(target.parent)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(target.name, flags, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        return os.fdopen(fd, mode, encoding=encoding, newline=newline)
    except BaseException:
        os.close(fd)
        raise
