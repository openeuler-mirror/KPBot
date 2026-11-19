"""Test the embedded patch's actual preconnect code on a real Tokio runtime."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from validation_environment import child_environment


def tool_path(name):
    found = shutil.which(name)
    if not found:
        raise ValueError(f"required executable not found: {name}")
    # Preserve the basename: resolving a Unix rustup symlink changes cargo to
    # rustup, whose command-line interface is different.
    return Path(os.path.abspath(found))


def rust_tool(name, kind, directory):
    """Resolve rustup proxies with a trusted tool, before executing any fixture."""
    executable = tool_path(name)
    rustup = shutil.which("rustup")
    is_proxy = executable.resolve().stem == "rustup"
    if rustup and not is_proxy:
        try:
            is_proxy = executable.samefile(rustup)
        except OSError:
            pass
    if not is_proxy:
        return executable
    rustup = tool_path(rustup or str(executable.with_name("rustup" + executable.suffix)))
    environment = child_environment(directory, executables=[rustup])
    environment["RUSTUP_HOME"] = str(Path(os.environ.get("RUSTUP_HOME", Path.home() / ".rustup")).resolve())
    result = subprocess.run([str(rustup), "which", kind], env=environment, cwd=directory,
                            check=True, capture_output=True, text=True)
    return tool_path(result.stdout.strip())


def reject_ancestor_cargo_config(directory):
    # Cargo searches cwd ancestors independently of CARGO_HOME. In particular,
    # a Windows per-user Temp directory can otherwise pick up ~/.cargo/config.
    current = Path(directory).resolve()
    for parent in (current, *current.parents):
        for name in ("config", "config.toml"):
            if (parent / ".cargo" / name).is_file():
                raise ValueError("temporary directory has ancestor Cargo configuration; select a clean TMPDIR/TEMP outside that configuration")


@contextmanager
def temporary_project():
    """Remove only our own temp tree, including long Cargo paths on Windows."""
    root = Path(tempfile.mkdtemp(prefix="cube-preconnect-check-")).resolve()
    expected_parent = root.parent
    try:
        yield root
    finally:
        if root.resolve() != root or root.parent != expected_parent or not root.name.startswith("cube-preconnect-check-"):
            raise ValueError("temporary project cleanup target changed")
        cleanup = str(root)
        if os.name == "nt":
            cleanup = "\\\\?\\UNC\\" + cleanup[2:] if cleanup.startswith("\\\\") else "\\\\?\\" + cleanup
        for attempt in range(6):
            try:
                shutil.rmtree(cleanup)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(0.1 * (attempt + 1))


def extract_patch_block(patch, name):
    in_shim = False
    new_lines = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_shim = line.endswith(" b/CubeShim/shim/src/sandbox/sb.rs")
        elif in_shim and line.startswith(("+", " ")) and not line.startswith("+++"):
            new_lines.append(line[1:])
    text = "\n".join(new_lines)
    start = f"        // BEGIN owned preconnect {name}"
    end = f"        // END owned preconnect {name}"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"missing or ambiguous patch block: {name}")
    return text[text.index(start):text.index(end) + len(end)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--rustc", default="rustc")
    parser.add_argument("--offline", action="store_true", help="forbid Cargo registry access")
    parser.add_argument("--vendor-directory", type=Path, help="pre-fetched crates produced by cargo vendor; no external Cargo home/config is loaded")
    parser.add_argument("--linker", help="explicit linker executable for a portable toolchain")
    parser.add_argument("--link-self-contained", action="store_true", help="use Rust's self-contained target libraries with the explicit linker")
    args = parser.parse_args()
    if args.link_self_contained and not args.linker:
        parser.error("--link-self-contained requires --linker")
    case = Path(__file__).resolve().parent
    patch_bytes = (case.parents[1] / "references/scheme1-rpc.patch").read_bytes()
    if patch_bytes.startswith(b"\xef\xbb\xbf") or b"\r" in patch_bytes:
        raise ValueError("reference patch must be UTF-8/LF without a BOM")
    patch = patch_bytes.decode("utf-8")
    harness = (case / "harness.rs").read_text(encoding="utf-8")
    for name in ("setup", "join"):
        placeholder = f"        // PATCH_{name.upper()}"
        if harness.count(placeholder) != 1:
            raise ValueError(f"missing or ambiguous harness placeholder: {name}")
        harness = harness.replace(placeholder, extract_patch_block(patch, name))
    result_dir = case / "results"
    result_dir.mkdir(exist_ok=True)
    with (result_dir / "patched_lifecycle.rs").open("w", encoding="utf-8", newline="\n") as output:
        output.write(harness)
    lock_bytes = (case / "Cargo.lock").read_bytes()
    print("Cargo.lock sha256=" + hashlib.sha256(lock_bytes).hexdigest(), flush=True)
    with temporary_project() as temporary:
        reject_ancestor_cargo_config(temporary)
        cargo = rust_tool(args.cargo, "cargo", temporary)
        rustc = rust_tool(args.rustc, "rustc", temporary)
        linker = tool_path(args.linker) if args.linker else None
        environment = child_environment(temporary / "runtime", executables=[cargo, rustc, *([linker] if linker else [])])
        project = temporary / "project"
        (project / "results").mkdir(parents=True)
        shutil.copyfile(case / "Cargo.toml", project / "Cargo.toml")
        (project / "Cargo.lock").write_bytes(lock_bytes)
        (project / "results/patched_lifecycle.rs").write_text(harness, encoding="utf-8")
        cargo_home = temporary / "cargo-home"
        cargo_home.mkdir()
        environment.update(CARGO_HOME=str(cargo_home), CARGO_TARGET_DIR=str(temporary / "target"),
                           RUSTC=str(rustc), CARGO_TERM_COLOR="never")
        rustdoc = rustc.with_name("rustdoc" + rustc.suffix)
        if rustdoc.is_file():
            environment["RUSTDOC"] = str(rustdoc)
        if linker:
            flags = ["-C", f"linker={linker}"]
            if args.link_self_contained:
                flags += ["-C", "link-self-contained=yes"]
            environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
        if args.vendor_directory:
            vendor = args.vendor_directory.resolve(strict=True)
            if not vendor.is_dir():
                raise ValueError("--vendor-directory must be a cargo vendor directory")
            # Only this generated source replacement is read; user credentials,
            # registry tokens and arbitrary CARGO_HOME configuration stay out.
            config = ('[source.crates-io]\nreplace-with = "vendored-sources"\n'
                      '[source.vendored-sources]\ndirectory = ' + json.dumps(str(vendor)) + '\n')
            (cargo_home / "config.toml").write_text(config, encoding="utf-8")
        command = [str(cargo), "test", "--locked", "--manifest-path", str(project / "Cargo.toml")]
        if args.offline:
            command.append("--offline")
        result = subprocess.run(command, env=environment, cwd=temporary)
        if (project / "Cargo.lock").read_bytes() != lock_bytes or (case / "Cargo.lock").read_bytes() != lock_bytes:
            raise ValueError("Cargo.lock changed during --locked validation")
        print("Cargo.lock unchanged after --locked", flush=True)
        return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(2)
