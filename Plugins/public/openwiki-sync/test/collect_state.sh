#!/usr/bin/env bash
# 采集一个 iteration 下所有 eval 两个 arm 的环境终态，输出 state.json 供评分。
# 用法: evals/collect_state.sh <iteration-dir>
set -euo pipefail
ITER_DIR="$(cd "$1" && pwd)"

for edir in "$ITER_DIR"/eval-*; do
  for arm in with_skill without_skill; do
    code="$edir/env/$arm/code"
    remote="$edir/env/$arm/wiki-remote.git"
    [ -d "$code" ] || continue
    CODE_ABS="$(cd "$code" && pwd)"
    python3 - "$code" "$remote" "$edir/$arm/state.json" "$CODE_ABS" <<'EOF'
import json, os, subprocess, sys

code, remote, out_path, code_abs = sys.argv[1:5]

def sh(cmd, cwd=None):
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None

def gitlog(gitdir):
    fmt = "%H%x1f%an%x1f%s%x1f%b%x00"
    out = sh(f'git --git-dir="{gitdir}" log --all --format="{fmt}"')
    if not out:
        return []
    records = []
    for chunk in out.split("\x00"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f", 3)
        while len(parts) < 4:
            parts.append("")
        records.append(
            {"hash": parts[0], "author": parts[1], "subject": parts[2], "body": parts[3]}
        )
    return records

state = {
    "code_repo": {
        "abs_path": code_abs,
        "commits": gitlog(os.path.join(code, ".git")),
        "status": sh("git status --porcelain", cwd=code) or "",
        "gitignore": open(os.path.join(code, ".gitignore")).read() if os.path.isfile(os.path.join(code, ".gitignore")) else None,
        "link_file": json.load(open(os.path.join(code, ".openwiki-link.json"))) if os.path.isfile(os.path.join(code, ".openwiki-link.json")) else None,
        "link_file_tracked": sh("git ls-files .openwiki-link.json", cwd=code) is not None and sh("git ls-files .openwiki-link.json", cwd=code) != "",
        "openwiki_files": sorted(
            os.path.relpath(os.path.join(r, f), os.path.join(code, "openwiki"))
            for r, _, fs in os.walk(os.path.join(code, "openwiki")) for f in fs
        ) if os.path.isdir(os.path.join(code, "openwiki")) else [],
        "mock_calls": open(os.path.join(code, ".openwiki-sync", "mock-openwiki.log")).read()
            if os.path.isfile(os.path.join(code, ".openwiki-sync", "mock-openwiki.log")) else "",
        "extra_files_at_root": sorted(
            f for f in os.listdir(code)
            if f not in {".git", "README.md", "app.py", "parser.py", "store.py", "tests", "openwiki", ".openwiki-sync", ".openwiki-link.json", ".gitignore", "auth.py"}
        ),
    },
    "wiki_repo": {
        "commits": gitlog(remote),
        "files_at_head": sh(f'git --git-dir="{remote}" ls-tree -r --name-only HEAD').splitlines()
            if sh(f'git --git-dir="{remote}" rev-parse HEAD') else [],
    },
    "registry_entry": None,
}

reg_path = os.path.expanduser("~/.kpbot/openwiki-links.json")
if os.path.isfile(reg_path):
    try:
        reg = json.load(open(reg_path))
        for k, v in reg.get("links", {}).items():
            if code_abs in k or code_abs in json.dumps(v):
                state["registry_entry"] = {"key": k, **v}
                break
    except ValueError:
        pass

resp = os.path.join(os.path.dirname(out_path), "outputs", "response.md")
state["response_md"] = open(resp).read() if os.path.isfile(resp) else None

with open(out_path, "w") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
print(f"collected {out_path}")
EOF
  done
done
