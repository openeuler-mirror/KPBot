#!/usr/bin/env bash
# 构建 openwiki-sync 技能的 5 个测试用例 fixture 环境（每个用例两个独立 arm：with_skill / without_skill）。
# 用法: test/setup_cases.sh <iteration-dir>
# 目录结构:
#   eval-N-<name>/env/<arm>/code            代码仓（demo-api 项目）
#   eval-N-<name>/env/<arm>/wiki-remote.git wiki 远程裸仓
#   eval-N-<name>/<arm>/outputs/
set -euo pipefail

ITER_DIR="$1"
EVALS_DIR="$(cd "$(dirname "$0")" && pwd)"
MOCK="$EVALS_DIR/mock-bin/openwiki"
chmod +x "$MOCK"

ITER_DIR="$(mkdir -p "$ITER_DIR" && cd "$ITER_DIR" && pwd)"

# 清理全局注册表中本 iteration 路径下的残留（避免上一次运行的注册表影响场景判定）
python3 - "$ITER_DIR" <<'EOF'
import json, os, sys
iter_dir = sys.argv[1]
reg_path = os.path.expanduser("~/.kpbot/openwiki-links.json")
if os.path.isfile(reg_path):
    try:
        reg = json.load(open(reg_path))
    except ValueError:
        reg = {"version": 1, "links": {}}
    before = len(reg.get("links", {}))
    reg["links"] = {k: v for k, v in reg.get("links", {}).items() if iter_dir not in k and iter_dir not in json.dumps(v)}
    if len(reg["links"]) != before:
        os.makedirs(os.path.dirname(reg_path), exist_ok=True)
        json.dump(reg, open(reg_path, "w"), ensure_ascii=False, indent=2)
        print(f"registry pruned: {before} -> {len(reg['links'])}")
EOF

make_demo_project() {
  local dest="$1"
  mkdir -p "$dest/tests"
  cat > "$dest/README.md" <<'EOF'
# demo-api

一个极简的 HTTP 配置查询服务：解析本地配置文件并通过内存存储对外提供查询接口。
EOF
  cat > "$dest/app.py" <<'EOF'
"""HTTP 入口。main() 启动一个简易 WSGI 服务，路由见 ROUTES。"""
from parser import load_config
from store import Store

STORE = Store()

ROUTES = {"/config": "handle_config", "/health": "handle_health"}

def handle_config(environ, start_response):
    cfg = load_config("config.ini")
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [str(cfg).encode()]

def handle_health(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]

def main():
    from wsgiref.simple_server import make_server
    make_server("0.0.0.0", 8080, lambda e, s: handle_config(e, s)).serve_forever()

if __name__ == "__main__":
    main()
EOF
  cat > "$dest/parser.py" <<'EOF'
"""配置解析模块。load_config() 读取 ini 并返回 dict。"""
import configparser

def load_config(path):
    cp = configparser.ConfigParser()
    cp.read(path)
    return {s: dict(cp[s]) for s in cp.sections()}
EOF
  cat > "$dest/store.py" <<'EOF'
"""内存键值存储。Store 提供 get/set/list。"""

class Store:
    def __init__(self):
        self._data = {}
    def set(self, k, v):
        self._data[k] = v
    def get(self, k):
        return self._data.get(k)
    def list(self):
        return dict(self._data)
EOF
  cat > "$dest/tests/test_app.py" <<'EOF'
from store import Store

def test_store():
    s = Store(); s.set("a", 1)
    assert s.get("a") == 1
EOF
  git -C "$dest" init -q
  git -C "$dest" config user.email "dev@example.com"
  git -C "$dest" config user.name "dev"
  git -C "$dest" add -A
  git -C "$dest" commit -qm "feat: initial demo-api project"
}

seed_wiki_remote() {
  local code="$1" remote="$2"
  git init -q --bare "$remote"
  (cd "$code" && "$MOCK" --init -p >/dev/null)
  local tmp; tmp="$(mktemp -d)"
  git clone -q "$remote" "$tmp/clone" 2>/dev/null
  cp -R "$code/openwiki/." "$tmp/clone/"
  git -C "$tmp/clone" add -A
  git -C "$tmp/clone" -c user.email=wiki@example.com -c user.name=wiki \
    commit -qm "openwiki: initialize wiki from demo-api@seed"
  git -C "$tmp/clone" push -q origin "HEAD:refs/heads/$(git -C "$tmp/clone" branch --show-current)" 2>/dev/null \
    || git -C "$tmp/clone" push -q origin HEAD
  rm -rf "$tmp"
}

link_commit() {
  local code="$1" remote="$2"
  cat > "$code/.openwiki-link.json" <<EOF
{
  "version": 1,
  "code_repo": "local:$code",
  "wiki_repo": "$remote",
  "platform": "generic",
  "created_at": "2026-09-01T00:00:00Z",
  "created_by": "kpbot-openwiki-sync"
}
EOF
  printf 'openwiki/\n.openwiki-sync/\n' > "$code/.gitignore"
  git -C "$code" add .openwiki-link.json .gitignore
  git -C "$code" commit -qm "chore(openwiki): 建立代码仓与 wiki 仓关联"
}

# 构建 eval 的一个 arm 的环境
# $1=eval目录 $2=arm(with_skill|without_skill) $3=variant(initial|wiki-exists|linked-missing|linked-ready-dirty|linked-ready)
build_arm() {
  local edir="$1" arm="$2" variant="$3"
  local code="$edir/env/$arm/code" remote="$edir/env/$arm/wiki-remote.git"
  mkdir -p "$edir/env/$arm" "$edir/$arm/outputs"
  make_demo_project "$code"

  case "$variant" in
    initial)
      git init -q --bare "$remote"
      ;;
    wiki-exists)
      (cd "$code" && "$MOCK" --init -p >/dev/null)
      git init -q --bare "$remote"
      ;;
    linked-missing)
      seed_wiki_remote "$code" "$remote"
      rm -rf "$code/openwiki" "$code/.openwiki-sync"
      link_commit "$code" "$remote"
      ;;
    linked-ready-dirty)
      seed_wiki_remote "$code" "$remote"
      link_commit "$code" "$remote"
      cat > "$code/auth.py" <<'EOF'
"""鉴权模块。check_token() 校验 Bearer token。"""
def check_token(token):
    return token == "s3cret"
EOF
      git -C "$code" add auth.py
      ;;
    linked-ready)
      seed_wiki_remote "$code" "$remote"
      link_commit "$code" "$remote"
      ;;
  esac
}

build_eval() {
  local id="$1" name="$2" variant="$3"
  local edir="$ITER_DIR/eval-$id-$name"
  for arm in with_skill without_skill; do
    build_arm "$edir" "$arm" "$variant"
  done
}

build_eval 1 initial-setup        initial
build_eval 2 unlinked-wiki-exists wiki-exists
build_eval 3 fresh-machine-restore linked-missing
build_eval 4 commit-sync          linked-ready-dirty
build_eval 5 wiki-query           linked-ready

echo "fixtures ready under $ITER_DIR"
for d in "$ITER_DIR"/eval-*; do
  echo "$(basename "$d"): $(ls "$d/env")"
done
