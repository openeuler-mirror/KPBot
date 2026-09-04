#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
PACKAGE_ROOT="$(dirname "$SCRIPT_DIR")"

LEVEL="project"
TOOL="claude"
INSTALL_PATH=""

for arg in "$@"; do
    case "$arg" in
        global|project) LEVEL="$arg" ;;
        claude|opencode) TOOL="$arg" ;;
        -h|--help)
            echo "Usage: kpbot uninstall [global|project] [claude|opencode] [path]"
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

SKILLS=$(python3 -c "
import json
m = json.load(open('$MANIFEST'))
for s in m.get('installed_skills', []):
    print(s)" 2>/dev/null || true)

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
