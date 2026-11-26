"""Exercise the packaged async writer and patched Put/Delete using DB stubs."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile


CASE = Path(__file__).resolve().parent
SKILL = CASE.parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from validation_environment import child_environment, require_executable


def declaration(source, name):
    match = re.search(rf"(?ms)^func {re.escape(name)}\(.*?^}}", source)
    if not match:
        raise ValueError(f"missing declaration: {name}")
    return match.group()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", default="git")
    parser.add_argument("--go", default="go")
    args = parser.parse_args()
    git = require_executable(args.git, "Git")
    go = require_executable(args.go, "Go")
    patch = (SKILL / "references/async-spec-write.patch").read_bytes()
    if patch.startswith(b"\xef\xbb\xbf") or b"\r" in patch:
        raise ValueError("reference patch must be UTF-8 with LF line endings")

    with tempfile.TemporaryDirectory(prefix="cube-spec-write-") as directory:
        root = Path(directory)
        environment = child_environment(root / "runtime", executables=[git, go])
        git_environment = dict(environment, GIT_CEILING_DIRECTORIES=str(root.parent))
        source = root / "CubeMaster/pkg/sandboxspec"
        source.mkdir(parents=True)
        store = source / "store.go"
        store.write_bytes((CASE / "fixtures/store.go.txt").read_bytes().replace(b"\r\n", b"\n"))
        template_store = root / "CubeMaster/pkg/templatecenter/store.go"
        template_store.parent.mkdir()
        template_store.write_bytes((CASE / "fixtures/templatecenter-store.go.txt").read_bytes().replace(b"\r\n", b"\n"))
        for mode in (["--check"], []):
            subprocess.run([git, "apply", *mode, "-"],
                           input=patch, cwd=root, env=git_environment, check=True)

        applied = store.read_text(encoding="utf-8")
        if "return deleteRecordOrdered(ctx, sandboxID)" not in applied:
            raise ValueError("expected patched Delete entrypoint")
        functions = "\n\n".join(declaration(applied, name) for name in ("Put", "Get", "decodeSpec", "Delete"))
        header = '''package sandboxspec
import (
    "context"
    "encoding/json"
    "errors"
    "strings"
    "github.com/tencentcloud/CubeSandbox/CubeMaster/pkg/base/constants"
    "github.com/tencentcloud/CubeSandbox/CubeMaster/pkg/base/db/models"
    sandboxtypes "github.com/tencentcloud/CubeSandbox/CubeMaster/pkg/service/sandbox/types"
    "gorm.io/gorm"
)
'''
        (root / "entrypoints.go").write_bytes((header + functions + "\n").encode("utf-8"))
        (root / "async.go").write_bytes((source / "async.go").read_bytes())
        (root / "async_test.go").write_bytes((CASE / "harness_test.go.txt").read_bytes())
        files = {
            "go.mod": 'module github.com/tencentcloud/CubeSandbox/CubeMaster\n\ngo 1.20\n\nrequire gorm.io/gorm v0.0.0\nreplace gorm.io/gorm => ./gorm\n',
            "gorm/go.mod": 'module gorm.io/gorm\n\ngo 1.20\n',
            "gorm/errors.go": 'package gorm\nimport "errors"\nvar ErrRecordNotFound = errors.New("record not found")\n',
            "pkg/base/constants/constants.go": 'package constants\nconst SandboxSpecTableName = "specs"\n',
            "pkg/base/db/models/model.go": 'package models\ntype SandboxSpec struct { SandboxID, RequestJSON, Backend string }\n',
            "pkg/base/log/log.go": 'package log\nimport "context"\ntype logger struct{}\nfunc G(context.Context) logger { return logger{} }\nfunc (logger) Warnf(string, ...interface{}) {}\nfunc (logger) Debugf(string, ...interface{}) {}\n',
            "pkg/service/sandbox/types/request.go": 'package types\ntype CreateCubeSandboxReq struct { Payload, Backend, NetworkPolicy string; Annotations map[string]string }\n',
        }
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        return subprocess.run([go, "test", "-count=1", "-timeout=30s", "-v", "."], cwd=root,
                              env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
