#!/usr/bin/env bash
# KPBot SessionStart Welcome Banner
# 直接输出纯文本到 stdout — 与 code-review-graph 相同的方式
set -euo pipefail

cat << 'BANNER'

  _  __  ____    ____     ___    _
 | |/ / |  _ \  | __ )   / _ \  | |_
 | ' /  |  _ \  |  _ \  | (_) | | __|
 | . \  |  __/  | |_) |  \___/  | |_
 |_|\_\ |_|     |____/           \__|

 鲲鹏/ARM 高性能计算工具集  v0.1.0
 📦 code-optimizer  ✅  已加载
 📋 36 个优化技能可用

 KPBot Marketplace 已加载。面向鲲鹏/ARM 生态的高性能计算工具集。
 当用户询问鲲鹏/ARM 代码优化、性能分析、向量化、汇编优化等问题时，
 请主动使用 code-optimizer 插件中的 skills。
BANNER

exit 0
