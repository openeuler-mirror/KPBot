"""Compile the embedded async writer and patched Put/Delete against DB stubs."""

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from validation_environment import child_environment, require_executable

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go")
    args = parser.parse_args()
    git = require_executable("git", "Git")
    go = require_executable(args.go, "Go")
    case = Path(__file__).resolve().parent
    skill = case.parents[1]
    specification = importlib.util.spec_from_file_location("replica_runner", case.parent / "replica-schedulability/run.py")
    runner = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(runner)
    data = (skill / "references/scheme2-rsi-cache.patch").read_bytes()
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise ValueError("reference patch must be UTF-8 without BOM and use LF")
    patch = data.decode("utf-8")
    store_patch = runner.file_patch(patch, "CubeMaster/pkg/sandboxspec/store.go")
    store_patch = store_patch.replace("a/CubeMaster/pkg/sandboxspec/store.go", "a/store.go.txt")
    store_patch = store_patch.replace("b/CubeMaster/pkg/sandboxspec/store.go", "b/store.go.txt")
    with tempfile.TemporaryDirectory(prefix="spec-write-order-") as directory:
        root = Path(directory)
        environment = child_environment(root / "runtime", executables=[git, go])
        git_environment = dict(environment, GIT_CEILING_DIRECTORIES=str(root.parent))
        (root / "store.go.txt").write_bytes((case / "fixtures/store.go.txt").read_bytes().replace(b"\r\n", b"\n"))
        for options in [["--check"], []]:
            subprocess.run([git, "apply", *options, "-"], input=store_patch.encode(), cwd=root,
                           env=git_environment, check=True)
        applied = (root / "store.go.txt").read_text(encoding="utf-8")
        functions = "\n\n".join(runner.declaration(applied, name) for name in ["Put", "Delete"])
        header = '''package sandboxspec
import (
    "context"
    "errors"
    "strings"
    sandboxtypes "github.com/tencentcloud/CubeSandbox/CubeMaster/pkg/service/sandbox/types"
)
'''
        (root / "entrypoints.go").write_bytes((header + functions + "\n").encode("utf-8"))
        async_patch = runner.file_patch(patch, "CubeMaster/pkg/sandboxspec/async.go")
        async_code = "\n".join(line[1:] for line in async_patch.splitlines()
                               if line.startswith("+") and not line.startswith("+++"))
        (root / "async.go").write_bytes((async_code + "\n").encode("utf-8"))
        (root / "async_test.go").write_bytes((case / "harness_test.go.txt").read_bytes())
        stubs = {
            "pkg/base/constants/constants.go": 'package constants\nconst SandboxSpecTableName = "specs"\n',
            "pkg/base/db/models/model.go": 'package models\ntype SandboxSpec struct { SandboxID, RequestJSON string }\n',
            "pkg/base/log/log.go": 'package log\nimport "context"\ntype logger struct{}\nfunc G(context.Context) logger { return logger{} }\nfunc (logger) Warnf(string, ...interface{}) {}\n',
            "pkg/service/sandbox/types/request.go": 'package types\ntype CreateCubeSandboxReq struct { Payload string }\n',
        }
        for name, content in stubs.items():
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content.encode("utf-8"))
        (root / "go.mod").write_text("module github.com/tencentcloud/CubeSandbox/CubeMaster\n\ngo 1.20\n", encoding="utf-8")
        return subprocess.run([go, "test", "-count=1", "-timeout=30s", "-v", "."], cwd=root, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
