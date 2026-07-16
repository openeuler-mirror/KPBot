#!/usr/bin/env bash
set -euo pipefail

BACKUP_FILE="${1:-}"

cat <<EOF
# CPU 亲和性回滚说明

- 当前脚本默认不自动执行系统级回滚。
- 若已经提前保存进程亲和性或中断亲和性备份，请按以下顺序人工回滚：
  1. 停止或隔离当前测试流量
  2. 恢复服务进程 CPU 亲和性
  3. 恢复 IRQ 亲和性
  4. 恢复 NUMA 绑定策略
  5. 重启服务并重新验证
- 备份文件: ${BACKUP_FILE:-not_provided}
EOF
