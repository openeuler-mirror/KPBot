#!/usr/bin/env python3
"""Strictly parse a deployment dotenv file without evaluating shell code."""

import os
import re
import shlex
import stat
import sys

KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)=(.*)$")
SHELL_METACHARACTERS = frozenset("$`;&|<>")
ALLOWED_EXACT = {
    "CUBE_VERSION",
    "CUBE_PVM_ENABLE",
    "CUBE_MIN_MEMORY_KB",
    "MIRROR",
    "DATABASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
ALLOWED_PREFIXES = (
    "CUBE_SANDBOX_",
    "CUBE_PROXY_",
    "CUBE_API_",
    "CUBE_OPS_",
    "CUBE_MASTER_",
    "CUBEMASTER_",
    "CUBE_DATABASE_",
    "CUBE_EXTERNAL_",
    "CUBE_S3_",
    "CUBE_S3LVOL_",
    "CUBE_L7_",
    "CUBE_EGRESS_",
    "CUBE_TEMPLATE_",
    "CUBE_QUICKCHECK_",
    "ONE_CLICK_",
    "WEB_UI_",
    "RCOW_",
    "JWT_",
)
FORBIDDEN_KEYS = {
    "CUBE_SANDBOX_INSTALL_ROOT",
    "CUBE_GITHUB_API_BASE",
    "ONE_CLICK_SKIP_PRECHECK",
    "ONE_CLICK_SKIP_PVM_CHECK",
    "ONE_CLICK_RUN_QUICKCHECK",
    "ONE_CLICK_MODE",
    "ONE_CLICK_ASSUME_YES",
    "ONE_CLICK_ENV_FILE",
    "ONE_CLICK_ALLOW_DOWNGRADE",
    "ONE_CLICK_ALLOW_ROLE_CHANGE",
    "ONE_CLICK_PACKAGE_TAR",
}


class EnvError(ValueError):
    """The environment file violates the deployment input contract."""


def allowed_key(key):
    return key in ALLOWED_EXACT or key.startswith(ALLOWED_PREFIXES)


def validate_value(value, line_number):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise EnvError("line %d: control characters are not allowed" % line_number)
    found = sorted(set(value).intersection(SHELL_METACHARACTERS))
    if found:
        raise EnvError(
            "line %d: shell metacharacters are not allowed: %s"
            % (line_number, " ".join(found))
        )


def parse_value(raw_value, line_number):
    """Parse one shell-style quoted value, never performing expansions."""
    try:
        values = shlex.split(raw_value, comments=True, posix=True)
    except ValueError as exc:
        raise EnvError("line %d: invalid quoting: %s" % (line_number, exc)) from exc
    if not values:
        value = ""
    elif len(values) == 1:
        value = values[0]
    else:
        raise EnvError("line %d: quote values containing whitespace" % line_number)
    validate_value(value, line_number)
    return value


def parse_env(path):
    """Return validated key/value pairs; no line is ever executed or sourced."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EnvError("cannot open environment file safely: " + str(exc)) from exc

    with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
        file_stat = os.fstat(handle.fileno())
        if not stat.S_ISREG(file_stat.st_mode):
            raise EnvError("environment file must be a regular, non-symlink file")
        expected_uid = 0 if os.geteuid() == 0 else os.geteuid()
        if file_stat.st_uid != expected_uid:
            raise EnvError("environment file must be owned by the deploying user (root for deployment)")
        if stat.S_IMODE(file_stat.st_mode) & 0o022:
            raise EnvError("environment file must not be group/world writable; use chmod 600")
        if file_stat.st_size > 1024 * 1024:
            raise EnvError("environment file exceeds the 1 MiB limit")

        entries = []
        seen = set()
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = KEY_RE.fullmatch(line)
            if not match:
                raise EnvError("line %d: expected KEY=VALUE without export or surrounding spaces" % line_number)
            key, raw_value = match.groups()
            if key in FORBIDDEN_KEYS:
                raise EnvError("line %d: %s is managed by deploy.sh and cannot be overridden" % key)
            if not allowed_key(key):
                raise EnvError("line %d: %s is not an allowed deployment key" % (line_number, key))
            if key in seen:
                raise EnvError("line %d: duplicate key %s" % (line_number, key))
            value = parse_value(raw_value, line_number)
            seen.add(key)
            entries.append((key, value))
        return entries


def main():
    if len(sys.argv) != 2:
        print("usage: parse_env.py <environment-file>", file=sys.stderr)
        return 2
    try:
        entries = parse_env(sys.argv[1])
    except (EnvError, UnicodeError, OSError) as exc:
        print("deployment environment: ERROR: " + str(exc), file=sys.stderr)
        return 1

    # NUL delimiters preserve spaces and empty values without a shell encoding.
    for key, value in entries:
        sys.stdout.buffer.write(key.encode("ascii") + b"\0")
        sys.stdout.buffer.write(value.encode("utf-8") + b"\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
