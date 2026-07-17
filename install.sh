# ----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# KPBot install.sh — Install KPBot skills into AI coding tools (Claude Code / OpenCode)
# ----------------------------------------------------------------------------------------------------------

set -e

# --- Color & output helpers ---
if [ -t 1 ]; then
  GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
  CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; CYAN=''; BOLD=''; DIM=''; NC=''
fi

ok()   { echo -e "  ${DIM}${GREEN}✓${NC}${DIM} $*${NC}"; }
warn() { echo -e "  ${YELLOW}⚠${NC}${DIM} $*${NC}"; }
err()  { echo -e "  ${RED}✗${NC}${DIM} $*${NC}"; }
info() { echo -e "  ${DIM}${CYAN}→${NC}${DIM} $*${NC}"; }
step() { echo -e "${DIM}$*${NC}"; }

# Safe install config file with backup and conflict handling.
# $1 = generated temp file path
# $2 = target file path
# $3 = display name
# $4 = install level (global/project)
safe_install_file() {
    local tmpfile="$1"
    local target="$2"
    local name="$3"
    local level="$4"

    # Idempotency: skip if identical
    if [ -e "$target" ] && diff -q "$tmpfile" "$target" > /dev/null 2>&1; then
        info "$name already up to date"
        rm -f "$tmpfile"
        return 0
    fi

    # Backup existing file before overwriting
    if [ -e "$target" ] || [ -L "$target" ]; then
        local backup
        backup="${target}.bak.$(date +%Y%m%d_%H%M%S)"
        cp -a "$target" "$backup"
        warn "$name already exists, backed up to $(basename "$backup")"

        # Interactive prompt for global mode
        if [ "$level" = "global" ] && [ -t 0 ] && [ -t 1 ]; then
            echo ""
            echo -e "  ${BOLD}${YELLOW}⚠  $name 存在自定义内容，请选择操作：${NC}"
            echo -e "    ${BOLD}[O]${NC} 覆盖      - 用插件内容替换（原内容已备份）"
            echo -e "    ${BOLD}[M]${NC} 合并      - 插件内容置顶，保留原自定义内容"
            echo -e "    ${BOLD}[S]${NC} 跳过      - 保持现有文件不变"
            printf "  ${BOLD}${CYAN}→${NC} ${BOLD}请输入选择 [O/M/S]:${NC} "
            read -r choice < /dev/tty
            case "$choice" in
                [Mm]*)
                    cat "$tmpfile" > "${target}.new"
                    echo "" >> "${target}.new"
                    echo "<!-- === User custom content below === -->" >> "${target}.new"
                    echo "" >> "${target}.new"
                    cat "$target" >> "${target}.new"
                    mv "${target}.new" "$target"
                    ok "$name (merged with backup)"
                    rm -f "$tmpfile"
                    return 0
                    ;;
                [Ss]*)
                    info "$name skipped (backup preserved)"
                    rm -f "$tmpfile"
                    return 0
                    ;;
                *) ;; # default: overwrite
            esac
        fi
    fi

    # Overwrite (default for project mode or non-interactive)
    mv "$tmpfile" "$target"
    ok "$name installed"
}


BRAND="kpbot"
VERSION="0.1.0"

show_banner() {
  echo ""
  echo -e "${CYAN}"
  cat << 'BANNER'

██╗  ██╗██████╗ ██████╗  ██████╗ ████████╗
██║ ██╔╝██╔══██╗██╔══██╗██╔═══██╗╚══██╔══╝
█████╔╝ ██████╔╝██████╔╝██║   ██║   ██║   
██╔═██╗ ██╔═══╝ ██╔══██╗██║   ██║   ██║   
██║  ██╗██║     ██████╔╝╚██████╔╝   ██║   
╚═╝  ╚═╝╚═╝     ╚═════╝  ╚═════╝    ╚═╝   
                                          
BANNER
  echo -e "${NC}"
  echo -e "  ${BOLD}鲲鹏/ARM 高性能计算工具集${NC}"
  echo ""
}

show_help() {
    cat << EOF
KPBot - 鲲鹏/ARM 高性能计算工具集安装器

Usage: install.sh [level] [tool] [install_path]

Arguments:
  level        - Installation level: "project" (default) or "global"
  tool         - Target tool: "claude" (default) or "opencode"
  install_path - Project-level installation directory (default: current working directory)

Options:
  --help  - Show this help message

Examples:
  install.sh                              # Project-level, Claude Code
  install.sh project claude               # Project-level, Claude Code
  install.sh global claude                # Global-level, Claude Code
  install.sh project opencode             # Project-level, OpenCode
  install.sh global opencode              # Global-level, OpenCode
  install.sh project claude /path/to/proj # Project-level, Claude Code, custom path

Installation paths:
  Claude:   .claude/skills/     + CLAUDE.md in project root (project)
            ~/.claude/skills/   + ~/.claude/CLAUDE.md (global)
  OpenCode: .opencode/skills/   + AGENTS.md in project root (project)
            ~/.config/opencode/ + AGENTS.md (global)

After installation, launch directly:
  Claude:   claude
  OpenCode: opencode
EOF
}

LEVEL="project"
TOOL="claude"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$SCRIPT_DIR"
# Skills: code-optimizer plugin
SKILL_ROOT="$PLUGIN_ROOT/Plugins/code-optimizer/skills"
# OpenCode 覆盖层：仅含与 claude 版本有差异的文件（稀疏镜像 skills/ 结构）
OPENCODE_OVERLAY="$PLUGIN_ROOT/Plugins/code-optimizer/opencode"
# claude-only 的 skill：opencode 无对应工具，安装时跳过
OPENCODE_SKIP="drive-claude-optimize-pipeline batch-drive-optimize-pipeline"

# --- Parse arguments ---
for arg in "$@"; do
    case "$arg" in
        --help)            show_help; exit 0 ;;
        global|project)    LEVEL="$arg" ;;
        claude|opencode)   TOOL="$arg" ;;
    esac
done

# If last argument is not a known keyword, treat it as install_path
INSTALL_PATH=""
if [ $# -gt 0 ]; then
    last_arg="${!#}"
    case "$last_arg" in
        --help|global|project|claude|opencode) ;;
        *) INSTALL_PATH="$last_arg" ;;
    esac
fi

# --- Determine config root directory ---
if [ "$LEVEL" = "global" ]; then
    if [ "$TOOL" = "opencode" ]; then
        CONFIG_ROOT="$HOME/.config/opencode"
    else
        CONFIG_ROOT="$HOME/.claude"
    fi
else
    # Project-level: default to current directory, allow override via install_path arg
    if [ -n "$INSTALL_PATH" ]; then
        INSTALL_BASE="$(cd "$INSTALL_PATH" && pwd)"
    else
        INSTALL_BASE="$PWD"
    fi
    CONFIG_ROOT_BASE="$INSTALL_BASE"

    if [ "$TOOL" = "opencode" ]; then
        CONFIG_ROOT="$CONFIG_ROOT_BASE/.opencode"
    else
        CONFIG_ROOT="$CONFIG_ROOT_BASE/.claude"
    fi
fi

# --- Clean up legacy brand subdirectory ---
if [ -e "$CONFIG_ROOT/$BRAND" ] || [ -L "$CONFIG_ROOT/$BRAND" ]; then
    rm -rf "$CONFIG_ROOT/$BRAND"
fi

show_banner
echo "  Tool:      $TOOL"
echo "  Level:     $LEVEL"
echo "  Path:      $CONFIG_ROOT"
echo ""

# --- Step 0: Preview ---
step "[0/4] Checking items to be installed..."

# Collect skills
SKILL_COUNT=0
SKILLS_TO_INSTALL=""
for skill_dir in "$SKILL_ROOT"/*/; do
    [ -d "$skill_dir" ] || continue
    name=$(basename "$skill_dir")
    SKILLS_TO_INSTALL="$SKILLS_TO_INSTALL $name"
    SKILL_COUNT=$((SKILL_COUNT + 1))
done

echo ""
echo -e "${BOLD}以下内容将被安装/替换：${NC}"
echo ""

if [ "$SKILL_COUNT" -gt 0 ]; then
    echo -e "${CYAN}Skills (${SKILL_COUNT} 项)：${NC}"
    for name in $SKILLS_TO_INSTALL; do
        target="$CONFIG_ROOT/skills/$name"
        if [ -e "$target" ] || [ -L "$target" ]; then
            echo -e "  ${YELLOW}$name${NC}"
        else
            echo -e "  ${GREEN}$name${NC}"
        fi
    done
    echo ""
fi

# Config file preview
if [ "$TOOL" = "opencode" ]; then
    config_name="AGENTS.md"
else
    config_name="CLAUDE.md"
fi

if [ "$LEVEL" = "project" ]; then
    config_target="$INSTALL_BASE/$config_name"
else
    config_target="$CONFIG_ROOT/$config_name"
fi

echo -e "${CYAN}配置文件：${NC}"
if [ -e "$config_target" ] || [ -L "$config_target" ]; then
    echo -e "  ${YELLOW}$config_name${NC} (将被替换)"
else
    echo -e "  ${GREEN}$config_name${NC} (将创建)"
fi

echo ""
echo -e "${BOLD}${YELLOW}注意：仅替换上述 KPBot 相关内容，不影响其他已存在的 skills${NC}"
echo ""
ok "开始安装..."
echo ""

# --- Step 1: Copy skills (with opencode overlay) ---
step "[1/4] Setting up KPBot skills..."
mkdir -p "$CONFIG_ROOT/skills"

# 清理目标目录下旧内容（幂等重装）
rm -rf "$CONFIG_ROOT/skills"/*

# 基底：拷贝全部 skills（claude 格式）
cp -r "$SKILL_ROOT/"* "$CONFIG_ROOT/skills/"
skill_count=$(ls -d "$CONFIG_ROOT/skills"/*/ 2>/dev/null | wc -l | tr -d ' ')

if [ "$TOOL" = "opencode" ]; then
    # OpenCode 覆盖层：用 opencode/ 下的差异文件覆盖 claude 版本
    if [ -d "$OPENCODE_OVERLAY" ]; then
        cp -r "$OPENCODE_OVERLAY/"* "$CONFIG_ROOT/skills/" 2>/dev/null || true
        ok "OpenCode overlay applied"
    fi
    # 跳过 claude-only 的 skill（opencode 无对应工具）
    for skip_name in $OPENCODE_SKIP; do
        [ -e "$CONFIG_ROOT/skills/$skip_name" ] && rm -rf "$CONFIG_ROOT/skills/$skip_name"
        skill_count=$((skill_count - 1))
    done
    ok "Skills: $skill_count copied (opencode)"
else
    ok "Skills: $skill_count copied (claude)"
fi
echo ""

# --- Step 2: Install config file ---
step "[2/4] Installing configuration..."

config_src="$PLUGIN_ROOT/CLAUDE.md"

if [ "$TOOL" = "opencode" ]; then
    if [ "$LEVEL" = "project" ]; then
        config_target="$INSTALL_BASE/AGENTS.md"
    else
        config_target="$CONFIG_ROOT/AGENTS.md"
    fi
else
    if [ "$LEVEL" = "project" ]; then
        config_target="$INSTALL_BASE/CLAUDE.md"
    else
        config_target="$CONFIG_ROOT/CLAUDE.md"
    fi
fi

if [ ! -f "$config_src" ]; then
    # 配置文件不存在（仓库未提供 CLAUDE.md），跳过此步
    info "No CLAUDE.md in plugin root, skip config install"
elif [ "$config_src" = "$config_target" ]; then
    # Already at target location (running from plugin root with matching name)
    ok "$config_name already in place"
elif [ "$LEVEL" = "global" ] || { [ "$LEVEL" = "project" ] && [ "$INSTALL_BASE" != "$SCRIPT_DIR" ]; }; then
    # Need to rewrite relative paths to absolute
    PLUGIN_ROOT_ABS="$(realpath "$PLUGIN_ROOT")"
    ESCAPED_ROOT="$(echo "$PLUGIN_ROOT_ABS" | sed 's/#/\\#/g')"
    tmpfile=$(mktemp)
    sed \
      -e "s#](Plugins/#](${ESCAPED_ROOT}/Plugins/#g" \
      -e "s#\`Plugins/#\`${ESCAPED_ROOT}/Plugins/#g" \
      "$config_src" > "$tmpfile"
    safe_install_file "$tmpfile" "$config_target" "$config_name" "$LEVEL"
else
    # Project-level, same directory: create symlink
    if [ -e "$config_target" ] && [ ! -L "$config_target" ]; then
        backup="${config_target}.bak.$(date +%Y%m%d_%H%M%S)"
        cp -a "$config_target" "$backup"
        warn "$config_name already exists, backed up to $(basename "$backup")"
    fi
    ln -sf "$config_src" "$config_target"
    ok "$config_name"
fi
echo ""

# --- Step 3: Tool discovery ---
step "[3/4] Configuring tool discovery..."

if [ "$TOOL" = "opencode" ]; then
    # OpenCode auto-scans .opencode/skills/ — Step 1 already done
    ok "Auto-scan: skills/"
else
    # Claude: skills copied to .claude/skills/ from Step 1
    ok "Skills: $skill_count copied to .claude/skills/"
fi
echo ""

# --- Step 4: Health check & manifest ---
step "[4/4] Running health check..."
health_ok=true
health_errors=""

# Check skills directory
skills_dir="$CONFIG_ROOT/skills"
if [ -d "$skills_dir" ]; then
    count=$(ls -d "$skills_dir"/*/ 2>/dev/null | wc -l)
    [ "$count" -eq 0 ] && { health_errors="${health_errors}\n  ${YELLOW}⚠${NC} skills/ is empty"; }
else
    health_errors="${health_errors}\n  ${RED}✗${NC} skills/ missing"
    health_ok=false
fi

# Check config file (warning only — config is optional when plugin omits CLAUDE.md)
if [ "$TOOL" = "opencode" ]; then
    if [ "$LEVEL" = "project" ]; then
        [ -f "$INSTALL_BASE/AGENTS.md" ] || { health_errors="${health_errors}\n  ${YELLOW}⚠${NC} AGENTS.md not installed (no CLAUDE.md in plugin)"; }
    else
        [ -f "$CONFIG_ROOT/AGENTS.md" ] || { health_errors="${health_errors}\n  ${YELLOW}⚠${NC} AGENTS.md not installed (no CLAUDE.md in plugin)"; }
    fi
else
    if [ "$LEVEL" = "project" ]; then
        [ -f "$INSTALL_BASE/CLAUDE.md" ] || { health_errors="${health_errors}\n  ${YELLOW}⚠${NC} CLAUDE.md not installed (no CLAUDE.md in plugin)"; }
    else
        [ -f "$CONFIG_ROOT/CLAUDE.md" ] || { health_errors="${health_errors}\n  ${YELLOW}⚠${NC} CLAUDE.md not installed (no CLAUDE.md in plugin)"; }
    fi
fi

# Check for broken symlinks
broken=$(find "$CONFIG_ROOT/skills" -maxdepth 1 -type l ! -exec test -e {} \; -print 2>/dev/null | wc -l)
[ "$broken" -gt 0 ] && { health_errors="${health_errors}\n  ${YELLOW}⚠${NC} $broken broken symlinks in skills/"; }

# Generate manifest
MANIFEST="$CONFIG_ROOT/kpbot-manifest.json"

SKILLS_JSON="[]"
if [ -d "$CONFIG_ROOT/skills" ]; then
  SKILLS_JSON=$(ls -d "$CONFIG_ROOT/skills"/*/ 2>/dev/null | while read d; do
    basename "$d"
  done | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" 2>/dev/null || echo "[]")
fi

cat > "$MANIFEST" << MANIFEST_EOF
{
  "brand": "KPBot",
  "version": "$VERSION",
  "level": "$LEVEL",
  "tool": "$TOOL",
  "installed_skills": $SKILLS_JSON,
  "brand_dir": "$CONFIG_ROOT",
  "install_time": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
MANIFEST_EOF

[ -f "$MANIFEST" ] || { health_errors="${health_errors}\n  ${RED}✗${NC} Manifest generation failed"; health_ok=false; }

if [ "$health_ok" = true ] && [ -z "$health_errors" ]; then
  ok "All checks passed"
else
  echo -e "$health_errors"
  [ "$health_ok" = true ] && warn "Some warnings, see above" || err "Some checks failed, see above"
fi

# --- Summary & Quick Start ---
echo ""
echo -e "  ${GREEN}${BOLD}✓ KPBot installed successfully!${NC}"
echo ""
echo -e "  ${BOLD}Quick Start:${NC}"
if [ "$TOOL" = "opencode" ]; then
  echo -e "  ${CYAN}1.${NC} 启动 CLI: ${GREEN}opencode${NC}"
  echo -e "  ${CYAN}2.${NC} 告诉 KPBot: ${GREEN}${BOLD}帮我优化这个函数的性能，目标是 Kunpeng-0xd01 平台${NC}"
else
  echo -e "  ${CYAN}1.${NC} 启动 CLI: ${GREEN}claude${NC}"
  echo -e "  ${CYAN}2.${NC} 告诉 KPBot: ${GREEN}${BOLD}帮我优化这个函数的性能，目标是 Kunpeng-0xd01 平台${NC}"
fi
echo ""
