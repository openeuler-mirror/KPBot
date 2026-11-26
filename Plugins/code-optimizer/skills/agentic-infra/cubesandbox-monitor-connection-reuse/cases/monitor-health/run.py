"""Apply the bundled patch to a v0.7.1 method and test it in a fixture."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


CASE = Path(__file__).resolve().parent
SKILL = CASE.parents[1]
SOURCE = Path("CubeShim/shim/src/sandbox/sb.rs")
PATCH = SKILL / "references/monitor-connection-reuse.patch"


def executable(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"Missing executable: {name}")
    return Path(path).absolute()


def clean_env(root, tools):
    home = root / "home"
    scratch = root / "tmp"
    home.mkdir()
    scratch.mkdir()
    path = [str(tool.parent) for tool in tools]
    env = {key: str(home) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME")}
    env.update({key: str(scratch) for key in ("TMP", "TEMP", "TMPDIR")})
    if os.name == "nt":
        system = Path(os.environ["SystemRoot"])
        env.update(SystemRoot=str(system), WINDIR=str(system))
        path.extend([str(system / "System32"), str(system)])
    else:
        path.extend(["/usr/bin", "/bin"])
    env.update(PATH=os.pathsep.join(dict.fromkeys(path)), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CEILING_DIRECTORIES=str(root.parent),
               CARGO_INCREMENTAL="0")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--rustc", default="rustc")
    parser.add_argument("--vendor-dir", type=Path, help="use verified cargo vendor sources offline")
    parser.add_argument("--linker", help="linker for a portable toolchain")
    parser.add_argument("--link-self-contained", action="store_true")
    args = parser.parse_args()
    if args.link_self_contained and not args.linker:
        parser.error("--link-self-contained requires --linker")
    git, cargo, rustc = map(executable, ["git", args.cargo, args.rustc])
    linker = executable(args.linker) if args.linker else None

    with tempfile.TemporaryDirectory(prefix="cube-rust-") as temp:
        root = Path(temp)
        if any((parent / ".cargo" / name).is_file() for parent in root.parents
               for name in ("config", "config.toml")):
            raise SystemExit("Choose a temporary directory without ancestor Cargo configuration")
        env = clean_env(root, [git, cargo, rustc] + ([linker] if linker else []))
        # A Rustup installation may expose proxies. Locate installed executables
        # before starting build/test subprocesses with a temporary Cargo home.
        rustup_path = shutil.which("rustup")
        if rustup_path:
            rustup = Path(rustup_path).absolute()
            lookup_env = dict(env, RUSTUP_HOME=os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")))
            def resolve(tool, name):
                if tool.samefile(rustup):
                    out = subprocess.check_output([rustup, "which", name], cwd=root, env=lookup_env, text=True)
                    return Path(out.strip())
                return tool
            cargo, rustc = resolve(cargo, "cargo"), resolve(rustc, "rustc")
            env["PATH"] = os.pathsep.join([str(cargo.parent), str(rustc.parent), env["PATH"]])
        src = root / SOURCE
        src.parent.mkdir(parents=True)
        fixture = (CASE / "original.rs.txt").read_bytes().replace(b"\r\n", b"\n")
        src.write_bytes(fixture)
        raw = PATCH.read_bytes()
        if b"\r" in raw or raw.startswith(b"\xef\xbb\xbf"):
            raise SystemExit("reference patch must use UTF-8 and LF")
        subprocess.run([git, "apply", "--check", "-"], cwd=root, env=env, input=raw, check=True)
        subprocess.run([git, "apply", "-"], cwd=root, env=env, input=raw, check=True)
        method = re.search(r"(?ms)^    async fn monitor_vm\(.*?^    }$", src.read_text(encoding="utf-8"))
        if not method or src.read_bytes() == fixture:
            raise SystemExit("patch did not replace the expected fixture method")
        harness = (CASE / "harness.rs").read_text(encoding="utf-8")
        if harness.count("// PATCHED_METHOD") != 1:
            raise SystemExit("missing or ambiguous fixture method marker")
        (root / "harness.rs").write_text(harness.replace("// PATCHED_METHOD", method.group()), encoding="utf-8")
        for name in ("Cargo.toml", "Cargo.lock"):
            shutil.copyfile(CASE / name, root / name)
        cargo_home = root / "cargo-home"
        cargo_home.mkdir()
        env.update(CARGO_HOME=str(cargo_home), CARGO_TARGET_DIR=str(root / "target"), RUSTC=str(rustc))
        flags = []
        if linker:
            flags.extend(["-C", f"linker={linker}"])
        if args.link_self_contained:
            flags.extend(["-C", "link-self-contained=yes"])
        if flags:
            env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
        command = [cargo, "test", "--lib", "--locked"]
        if args.vendor_dir:
            vendor = args.vendor_dir.resolve(strict=True)
            if not vendor.is_dir():
                parser.error("--vendor-dir must be a directory")
            (cargo_home / "config.toml").write_text(
                '[source.crates-io]\nreplace-with = "vendor"\n[source.vendor]\ndirectory = '
                + json.dumps(vendor.as_posix()) + "\n", encoding="utf-8")
            command.append("--offline")
        return subprocess.run(command, cwd=root, env=env, timeout=180).returncode


if __name__ == "__main__":
    raise SystemExit(main())
