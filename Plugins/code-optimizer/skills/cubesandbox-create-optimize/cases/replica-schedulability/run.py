"""Apply the actual reference hunks and test their request-local decisions."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from validation_environment import child_environment, require_executable

def file_patch(patch, path):
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    matches = [section for section in sections if section.startswith(f"diff --git a/{path} b/{path}\n")]
    if len(matches) != 1:
        raise ValueError(f"missing or ambiguous patch file: {path}")
    return matches[0]


def declaration(source, name, kind="func"):
    match = re.search(rf"(?ms)^{kind} {re.escape(name)}(?:\b|\().*?^}}", source)
    if not match:
        raise ValueError(f"missing declaration: {name}")
    return match.group(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go", help="Go executable; no external modules are needed")
    args = parser.parse_args()
    case = Path(__file__).resolve().parent
    git = require_executable("git", "Git")
    go = require_executable(args.go, "Go")
    patch_path = case.parents[1] / "references/scheme2-rsi-cache.patch"
    patch_bytes = patch_path.read_bytes()
    if patch_bytes.startswith(b"\xef\xbb\xbf") or b"\r" in patch_bytes:
        raise ValueError("reference patch must be UTF-8 without BOM and use LF line endings")
    patch = patch_bytes.decode("utf-8")
    store = file_patch(patch, "CubeMaster/pkg/templatecenter/store.go")
    hunks = re.split(r"(?=^@@ )", store, flags=re.MULTILINE)[1:]
    selected = [hunk for hunk in hunks if "ResolveTemplateReadyReplica(" in hunk or
                "func isTemplateReplicaSchedulable(" in hunk.splitlines()[0]]
    # Git may combine adjacent changes into one hunk when the patch is
    # regenerated. Require both targets, rather than a particular hunk count.
    if not selected or not all(name in "".join(selected) for name in
                               ["ResolveTemplateReadyReplica(", "func isTemplateReplicaSchedulable("]):
        raise ValueError("expected patch hunks for both ready-list and single-replica selection")
    fixture_patch = "--- a/store.go.txt\n+++ b/store.go.txt\n" + "".join(selected)
    with tempfile.TemporaryDirectory(prefix="replica-schedulability-") as directory:
        root = Path(directory)
        # Run outside any enclosing checkout, even when TMP points inside one.
        environment = child_environment(root / "runtime", executables=[git, go])
        git_env = dict(environment, GIT_CEILING_DIRECTORIES=str(root.parent))
        # Parse the entire package patch, including the hunk challenged in review.
        subprocess.run([git, "apply", "--numstat", "-"], input=patch.encode(),
                       cwd=root, env=git_env, check=True)
        (root / "store.go.txt").write_bytes((case / "fixtures/store.go.txt").read_bytes().replace(b"\r\n", b"\n"))
        command = [git, "apply"]
        subprocess.run(command + ["--check", "-"], input=fixture_patch.encode(), cwd=root, env=git_env, check=True)
        subprocess.run(command + ["-"], input=fixture_patch.encode(), cwd=root, env=git_env, check=True)
        applied = (root / "store.go.txt").read_text(encoding="utf-8")
        names = ["ResolveTemplateReadyReplica", "selectReadyReplica", "isTemplateReplicaSchedulable",
                 "isReplicaSchedulableNow", "isReplicaSchedulable"]
        functions = "\n\n".join(declaration(applied, name) for name in names)
        # Type/table adapters replace the external DB model, not decision logic.
        functions = functions.replace("models.TemplateReplica", "ReplicaStatus")
        functions = functions.replace("constants.TemplateReplicaTableName", '"template_replicas"')
        functions = functions.replace("func isReplicaSchedulableNow(", "func isReplicaSchedulableNowBase(")
        (root / "selection.go").write_text(
            'package templatecenter\nimport ("context"; "strings")\n' + functions + "\n", encoding="utf-8")
        cache = file_patch(patch, "CubeMaster/pkg/templatecenter/resolve_cache.go")
        cache_source = "\n".join(line[1:] for line in cache.splitlines()
                                 if line.startswith("+") and not line.startswith("+++"))
        (root / "resolve_cache.go").write_text(cache_source + "\n", encoding="utf-8")
        for fixture, destination in [("fixtures/fetch_group.go.txt", "fetch_group.go"),
                                     ("harness_test.go.txt", "selection_test.go")]:
            (root / destination).write_text((case / fixture).read_text(encoding="utf-8"), encoding="utf-8")
        (root / "go.mod").write_text("module regression.local/replica\n\ngo 1.20\n", encoding="utf-8")
        result = subprocess.run([go, "test", "-count=1", "-timeout=30s", "-v", "."], cwd=root, env=environment)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
