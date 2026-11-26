#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
PACKAGE_ROOT="$(dirname "$SCRIPT_DIR")"

case "${1:-}:${2:-}" in
    codex:*|project:codex|global:codex)
        exec bash "$PACKAGE_ROOT/bin/_codex.sh" uninstall "$@"
        ;;
esac

# Identify the tool position; a later "dsh" may be a legacy project path.
# Explicit DSH invocations must never fall through to the Claude uninstaller.
case "${1:-}:${2:-}" in
    dsh:*|project:dsh|global:dsh)
        exec node "$PACKAGE_ROOT/adapters/dsh/install.mjs" uninstall "$@"
        ;;
esac

LEVEL="project"
TOOL="claude"
INSTALL_PATH=""

for arg in "$@"; do
    case "$arg" in
        global|project) LEVEL="$arg" ;;
        claude|opencode) TOOL="$arg" ;;
        -h|--help)
            echo "Usage: kpbot uninstall [global|project] [claude|opencode|codex|dsh] [path]"
            exit 0
            ;;
        *) INSTALL_PATH="$arg" ;;
    esac
done

if [ "$LEVEL" = "global" ]; then
    if [ "$TOOL" = "opencode" ]; then
        CONFIG_ROOT="$HOME/.config/opencode"
    else
        CONFIG_ROOT="$HOME/.claude"
    fi
else
    if [ -n "$INSTALL_PATH" ]; then
        CONFIG_ROOT="$(cd "$INSTALL_PATH" && pwd)"
    else
        CONFIG_ROOT="$PWD"
    fi
    if [ "$TOOL" = "opencode" ]; then
        CONFIG_ROOT="$CONFIG_ROOT/.opencode"
    else
        CONFIG_ROOT="$CONFIG_ROOT/.claude"
    fi
fi

MANIFEST="$CONFIG_ROOT/kpbot-manifest.json"

if [ ! -f "$MANIFEST" ]; then
    echo "  ⚠ 未找到 KPBot 安装清单: $MANIFEST"
    echo "  可能未安装或已卸载"
    exit 0
fi

PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "KPBot uninstall requires Python 3 (tried python3 and python)" >&2
    exit 1
fi

manifest_path="$MANIFEST"
if command -v cygpath >/dev/null 2>&1; then
    manifest_path="$(cygpath -am "$MANIFEST")"
fi
SKILLS=$("$PYTHON_BIN" - "$manifest_path" "$TOOL" <<'PY'
import json, re, sys

def fail(reason, legacy=False):
    message = "KPBot manifest validation failed: " + reason + "\nManifest: " + sys.argv[1]
    if legacy:
        message += ("\nA legacy manifest may have recorded user-created skill directories."
                    "\nBack up the installation directory and manifest, then remove only the manifest file: "
                    + sys.argv[1] + "\nRetry the command after checking the backed-up skill directories.")
    else:
        message += "\nNo managed skills were removed. Restore a valid manifest from backup before retrying."
    sys.stderr.buffer.write((message + "\n").encode("utf-8"))
    raise SystemExit(1)

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        manifest = json.load(stream)
except (OSError, ValueError) as error:
    fail("Cannot read manifest: " + str(error))
if not isinstance(manifest, dict) or manifest.get("brand") != "KPBot" or manifest.get("tool") != sys.argv[2]:
    fail("Invalid KPBot installation manifest")
names = manifest.get("installed_skills")
if not isinstance(names, list):
    fail("Invalid installed_skills: expected an array")
for name in names:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        fail("Invalid installed_skills entry: " + repr(name), legacy=True)
if len(names) != len(set(names)):
    fail("Invalid duplicate installed_skills in KPBot manifest")
sys.stdout.buffer.write(("\n".join(names) + "\n").encode("utf-8"))
PY
)

removed=0
for name in $SKILLS; do
    target="$CONFIG_ROOT/skills/$name"
    if [ -e "$target" ] || [ -L "$target" ]; then
        rm -rf "$target"
        echo "  ✓ removed skill: $name"
        removed=$((removed + 1))
    fi
done

if [ -d "$CONFIG_ROOT/agents" ]; then
    find "$CONFIG_ROOT/agents" -maxdepth 1 -type l ! -exec test -e {} \; -delete 2>/dev/null || true
fi

config_name=$([ "$TOOL" = "opencode" ] && echo "AGENTS.md" || echo "CLAUDE.md")

rm -f "$MANIFEST"
echo ""
echo "  ✓ KPBot uninstalled ($removed skills removed)"
echo "  ℹ 配置文件 $config_name 未自动删除（可能含用户自定义内容），如需清理请手动 rm"
