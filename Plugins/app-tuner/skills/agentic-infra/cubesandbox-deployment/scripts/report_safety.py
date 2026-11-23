#!/usr/bin/env python3
"""Validate and atomically write focused CubeSandbox deployment JSON reports."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

SCHEMA_VERSION = 1
MAX_EXISTING_REPORT_BYTES = 1024 * 1024


class ReportSafetyError(ValueError):
    """The requested report path is not safe to replace."""


def real_path(path):
    """Resolve symlinks in existing ancestors even when the leaf is absent."""
    return Path(os.path.realpath(os.path.abspath(os.fspath(path))))


def is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_report_path(report_path, skill, mode, input_env="", install_root=""):
    """Reject input/config/assets and unrelated existing files before writing."""
    path = Path(report_path)
    if not path.name:
        raise ReportSafetyError("report path must name a JSON file")
    if not path.parent.is_dir():
        raise ReportSafetyError("report parent directory does not exist: " + str(path.parent))
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ReportSafetyError("report path must be a regular, non-symlink file")

    resolved = real_path(path)
    if input_env:
        env_path = Path(input_env)
        if resolved == real_path(env_path):
            raise ReportSafetyError("report path must not overwrite the input environment file")
        if path.exists() and env_path.exists():
            try:
                if os.path.samefile(str(path), str(env_path)):
                    raise ReportSafetyError("report path must not overwrite the input environment file")
            except OSError as exc:
                raise ReportSafetyError("cannot compare report and environment paths safely: " + str(exc)) from exc

    if install_root:
        root = real_path(install_root)
        if is_within(resolved, root):
            raise ReportSafetyError("verification report must be outside INSTALL_ROOT and its installed assets")

    if path.exists():
        try:
            if path.stat().st_size > MAX_EXISTING_REPORT_BYTES:
                raise ReportSafetyError("existing report exceeds the 1 MiB safety limit")
            with path.open("r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except ReportSafetyError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReportSafetyError("refusing to overwrite an existing file that is not a readable JSON report: " + str(exc)) from exc
        if not isinstance(existing, dict):
            raise ReportSafetyError("existing JSON report must be an object")
        expected = {"skill": skill, "mode": mode}
        if type(existing.get("schema_version")) is not int or existing.get("schema_version") != SCHEMA_VERSION:
            raise ReportSafetyError(
                "existing JSON report identity mismatch for schema_version; refusing to overwrite an unrelated file"
            )
        for key, value in expected.items():
            if existing.get(key) != value:
                raise ReportSafetyError(
                    "existing JSON report identity mismatch for %s; refusing to overwrite an unrelated file" % key
                )
    return path


def atomic_write_report(report_path, report, input_env="", install_root=""):
    """Validate the old and new identities, then atomically install a 0600 report."""
    if not isinstance(report, dict):
        raise ReportSafetyError("new report must be a JSON object")
    skill = report.get("skill")
    mode = report.get("mode")
    if type(report.get("schema_version")) is not int or report.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(skill, str) or not isinstance(mode, str):
        raise ReportSafetyError("new report has an invalid schema identity")
    path = validate_report_path(report_path, skill, mode, input_env, install_root)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        # Repeat all checks immediately before replacement so a previously safe
        # path that became an input, asset, symlink, or unrelated file is refused.
        validate_report_path(path, skill, mode, input_env, install_root)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def blocked_report(skill, mode, reason):
    """Build the non-secret blocked verdict used when shared code is unavailable."""
    report = {
        "schema_version": SCHEMA_VERSION,
        "skill": skill,
        "mode": mode,
        "status": "blocked",
        "success": False,
        "reason": reason,
    }
    if mode == "preflight":
        report.update({
            "ready_for_install": False,
            "sha256_pinned": False,
            "side_effects": {
                "host_configuration_changed": False,
                "bundle_downloaded": False,
                "installation_started": False,
            },
        })
    elif mode == "verify":
        report.update({
            "quickcheck_exit_code": None,
            "checks": {
                "runtime_env": "blocked",
                "official_quickcheck": "blocked",
                "services_and_health_endpoints": "blocked",
            },
            "official_evidence": {
                "control_role": False,
                "systemd_units": False,
                "cube_api_health": False,
                "cube_ops_health": False,
                "official_completion": False,
            },
            "side_effects": {
                "release_discovery": False,
                "bundle_downloaded": False,
                "installation_or_restart": False,
                "host_configuration_changed": False,
            },
        })
    else:
        raise ReportSafetyError("unsupported blocked report mode: " + mode)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "blocked"))
    parser.add_argument("report")
    parser.add_argument("skill")
    parser.add_argument("mode", choices=("preflight", "verify"))
    parser.add_argument("--input-env", default="")
    parser.add_argument("--install-root", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    try:
        if args.action == "check":
            validate_report_path(args.report, args.skill, args.mode, args.input_env, args.install_root)
        else:
            report = blocked_report(args.skill, args.mode, args.reason or "shared implementation is unavailable")
            atomic_write_report(args.report, report, args.input_env, args.install_root)
    except (OSError, ReportSafetyError) as exc:
        print("report safety: ERROR: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
