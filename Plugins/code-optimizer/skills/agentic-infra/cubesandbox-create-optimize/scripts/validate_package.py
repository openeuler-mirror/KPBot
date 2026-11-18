"""Validate raw reference patches and run every required local package check."""

import argparse
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validation_environment import child_environment, require_executable

HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
REQUIRED_GO_CASES = ("replica-schedulability", "spec-write-order")


def check_patch_bytes(path):
    """Check the delivered bytes, without silently normalizing line endings."""
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path.name}: UTF-8 BOM is not valid patch input")
    if b"\r" in data:
        raise ValueError(f"{path.name}: expected LF bytes; re-export the original patch or use its eol=lf checkout rule")
    data.decode("utf-8")
    reports = []
    pending = None
    target = "<unknown>"

    def finish():
        nonlocal pending
        if pending is None:
            return
        context, deleted, added = pending["context"], pending["deleted"], pending["added"]
        old_count, new_count = context + deleted, context + added
        if (old_count, new_count) != (pending["old_count"], pending["new_count"]):
            raise ValueError(
                f"{path.name}:{pending['line']} {pending['target']}: "
                f"header old/new={pending['old_count']}/{pending['new_count']}, "
                f"body old/new={old_count}/{new_count} "
                f"(context={context}, deleted={deleted}, added={added})")
        reports.append(pending)
        pending = None

    for number, line in enumerate(data.splitlines(), 1):
        if line.startswith(b"diff --git "):
            finish()
            target = line.decode("utf-8")
            continue
        header = HUNK.match(line)
        if header:
            finish()
            pending = {
                "line": number, "target": target,
                "old_count": int(header[2]) if header[2] is not None else 1,
                "new_count": int(header[4]) if header[4] is not None else 1,
                "context": 0, "deleted": 0, "added": 0,
            }
        elif line.startswith(b"@@"):
            raise ValueError(f"{path.name}:{number}: malformed hunk header")
        elif pending is not None:
            if line.startswith(b" "):
                pending["context"] += 1
            elif line.startswith(b"-"):
                pending["deleted"] += 1
            elif line.startswith(b"+"):
                pending["added"] += 1
            elif line == b"\\ No newline at end of file":
                continue
            else:
                finish()
    finish()
    if not reports:
        raise ValueError(f"{path.name}: no unified-diff hunks found")
    print(f"PATCH {path.name} sha256={hashlib.sha256(data).hexdigest()}", flush=True)
    for report in reports:
        print(f"  hunk@{report['line']}: old/new={report['old_count']}/{report['new_count']} "
              f"context={report['context']} deleted={report['deleted']} added={report['added']} OK", flush=True)
    return data


def run(command, *, cwd, env, data=None):
    print("RUN " + " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, input=data, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go", help="Go executable for the toolchain-dependent cases")
    parser.add_argument("--git", default="git", help="Git executable for patch parsing")
    parser.add_argument("--cargo", default="cargo", help="Cargo executable for the preconnect lifecycle case")
    parser.add_argument("--rustc", default="rustc", help="Rust compiler for the preconnect lifecycle case")
    parser.add_argument("--offline", action="store_true", help="forbid Cargo registry access")
    parser.add_argument("--vendor-directory", type=Path, help="pre-fetched cargo vendor directory")
    parser.add_argument("--linker", help="explicit linker executable for a portable Rust toolchain")
    parser.add_argument("--link-self-contained", action="store_true", help="use Rust's self-contained target libraries with --linker")
    parser.add_argument("--static-only", action="store_true", help="parse all packaged patches only; do not run cases, Go, or Rust code")
    args = parser.parse_args()
    if not args.static_only and args.link_self_contained and not args.linker:
        parser.error("--link-self-contained requires --linker")
    skill = Path(__file__).resolve().parents[1]
    git = require_executable(args.git, "Git")
    go = None if args.static_only else require_executable(args.go, "Go")
    comparison = skill / "cases/comparison-contract/test_compare.py"
    environment_case = skill / "cases/environment-contract/test_environment.py"
    runners = [skill / "cases" / name / "run.py" for name in REQUIRED_GO_CASES]
    lifecycle = skill / "cases/preconnect-lifecycle/run.py"
    for entrypoint in ([] if args.static_only else [environment_case, comparison, *runners, lifecycle]):
        if not entrypoint.is_file():
            raise ValueError(f"required package entrypoint is missing: {entrypoint.relative_to(skill)}")
    patches = sorted((skill / "references").glob("*.patch"))
    if not patches:
        raise ValueError("package contains no reference patch")
    with tempfile.TemporaryDirectory(prefix="cube-package-check-") as directory:
        temporary = Path(directory)
        environment = child_environment(temporary / "runtime", executables=[git, *([go] if go else [])])
        patch_environment = dict(environment, GIT_CEILING_DIRECTORIES=str(temporary.parent))
        for patch in patches:
            data = check_patch_bytes(patch)
            run([git, "apply", "--numstat", "-"], cwd=temporary, env=patch_environment, data=data)
            run([git, "apply", "--stat", "-"], cwd=temporary, env=patch_environment, data=data)
        if args.static_only:
            print("STATIC PASS: raw patch format and Git parsing only; no case, Go/Rust code, source-tree apply check, or performance test executed", flush=True)
            return 0
        # Resolve trusted rustup proxies before discarding the caller's HOME.
        # Static mode intentionally never imports or executes a case module.
        specification = importlib.util.spec_from_file_location("cube_preconnect_runner", lifecycle)
        lifecycle_runner = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(lifecycle_runner)
        cargo = lifecycle_runner.rust_tool(args.cargo, "cargo", temporary)
        rustc = lifecycle_runner.rust_tool(args.rustc, "rustc", temporary)
        linker = lifecycle_runner.tool_path(args.linker) if args.linker else None
        environment = child_environment(temporary / "runtime", executables=[git, go, cargo, rustc, *([linker] if linker else [])])
        run([sys.executable, str(environment_case)], cwd=skill, env=environment)
        run([go, "version"], cwd=skill, env=environment)
        run([sys.executable, str(comparison)], cwd=skill, env=environment)
        for runner in runners:
            run([sys.executable, str(runner), "--go", go], cwd=skill, env=environment)
        command = [sys.executable, str(lifecycle), "--cargo", str(cargo), "--rustc", str(rustc)]
        if args.offline:
            command.append("--offline")
        if args.vendor_directory:
            command += ["--vendor-directory", str(args.vendor_directory.resolve())]
        if linker:
            command += ["--linker", str(linker)]
        if args.link_self_contained:
            command.append("--link-self-contained")
        run(command, cwd=skill, env=environment)
    print("PASS: raw patch format, Git parsing, environment contract, comparison tests, required Go cases, and Rust lifecycle case", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, UnicodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(2)
    except subprocess.CalledProcessError as error:
        print(f"FAIL: command exited {error.returncode}: {error.cmd}", file=sys.stderr)
        raise SystemExit(1)
