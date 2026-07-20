#!/usr/bin/env bash
# KPBot 启动 Banner - 鲲鹏/ARM 高性能计算工具集
# KP 部分红色输出，用于 skill 启动时展示

RED=$'\033[31m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

printf "\n"
printf "%s██╗  ██╗██████╗%s ██████╗  ██████╗ ████████╗\n" "$RED" "$RESET"
printf "%s██║ ██╔╝██╔══██╗%s██╔══██╗██╔═══██╗╚══██╔══╝\n" "$RED" "$RESET"
printf "%s█████╔╝ ██████╔╝%s██████╔╝██║   ██║   ██║   \n" "$RED" "$RESET"
printf "%s██╔═██╗ ██╔═══╝ %s██╔══██╗██║   ██║   ██║   \n" "$RED" "$RESET"
printf "%s██║  ██╗██║     %s██████╔╝╚██████╔╝   ██║   \n" "$RED" "$RESET"
printf "%s╚═╝  ╚═╝╚═╝     %s╚═════╝  ╚═════╝    ╚═╝   \n" "$RED" "$RESET"
printf "\n  %sKPBot - 鲲鹏/ARM 性能优化AI Agent%s\n\n" "$BOLD" "$RESET"
