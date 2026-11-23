"""Compile the patched resolver and cache against a local store fixture."""

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


def one_section(patch, filename):
    prefix = f"diff --git a/{filename} b/{filename}\n"
    section, = (
        part for part in re.split(r"(?m)(?=^diff --git )", patch)
        if part.startswith(prefix)
    )
    return section


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--git", default="git")
    arguments.add_argument("--go", default="go")
    options = arguments.parse_args()
    git = require_executable(options.git, "Git")
    go = require_executable(options.go, "Go")
    patch_bytes = (SKILL / "references/replica-metadata-ttl.patch").read_bytes()
    patch = patch_bytes.decode("utf-8")
    base = "CubeMaster/pkg/templatecenter"
    store = one_section(patch, f"{base}/store.go")
    hunks = [
        hunk for hunk in re.split(r"(?m)(?=^@@ )", store)[1:]
        if "readReplicaMetadata" in hunk
    ]
    if len(hunks) != 2:
        raise ValueError(f"expected both resolver hunks, found {len(hunks)}")
    resolver_diff = "--- a/store.go.txt\n+++ b/store.go.txt\n" + "".join(hunks)

    with tempfile.TemporaryDirectory(prefix="replica-ttl-") as directory:
        root = Path(directory)
        environment = child_environment(root, executables=(git, go))
        environment["GIT_CEILING_DIRECTORIES"] = str(root.parent)

        def apply(args, content):
            subprocess.run(
                [git, "apply", *args, "-"], input=content, cwd=root,
                env=environment, check=True, timeout=30,
            )

        apply(["--numstat"], patch_bytes)
        excerpt = root / "store.go.txt"
        excerpt.write_bytes((CASE / "fixtures/store.go.txt").read_bytes())
        apply(["--check"], resolver_diff.encode("utf-8"))
        apply([], resolver_diff.encode("utf-8"))
        apply([], one_section(patch, f"{base}/ready_replica_ttl.go").encode("utf-8"))
        (root / base / "ready_replica_ttl.go").rename(root / "ready_replica_ttl.go")

        resolver = excerpt.read_text(encoding="utf-8")
        functions = [
            re.search(rf"(?ms)^func {name}\(.*?^}}$", resolver).group()
            for name in ("ResolveTemplateReadyReplica", "readReplicaMetadata")
        ]
        predicate = (CASE / "fixtures/predicate.go.txt").read_text(encoding="utf-8")
        source = 'package templatecenter\nimport ("context"; "strings")\n'
        functions.append(predicate.replace("func isReplicaSchedulableNow", "func baselinePredicate"))
        (root / "resolver.go").write_text(source + "\n\n".join(functions), encoding="utf-8")
        (root / "cache_test.go").write_bytes((CASE / "fixtures/harness_test.go.txt").read_bytes())
        (root / "go.mod").write_text("module replica-ttl-check\n\ngo 1.20\n", encoding="utf-8")
        subprocess.run(
            [go, "test", "-count=1", "-timeout=30s", "-v", "."], cwd=root,
            env=environment, check=True, timeout=180,
        )


if __name__ == "__main__":
    main()
