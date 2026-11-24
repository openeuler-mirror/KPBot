#!/bin/bash

unset http_proxy
unset https_proxy
unset HTTPS_PROXY
unset HTTP_PROXY

mkdir -p ./log

# 上游只读 USER_CONFIG_PATH 环境变量定位配置文件：
# motor.coordinator.main 无 --config 参数，也不存在 MINDIE_MOTOR_CONFIG_PATH / MOTOR_LOG_LEVEL
# （日志级别由 user_config.json 的 logging_config.log_level 控制）。
export USER_CONFIG_PATH=./user_config.json

nohup python3 -m motor.coordinator.main > ./log/motor_coordinator.log 2>&1 &
echo $! > ./log/motor_coordinator.pid

# 存活校验：启动 2 秒后确认进程还在，避免"秒退仍报成功"的假成功信号。
sleep 2
if kill -0 "$(cat ./log/motor_coordinator.pid)" 2>/dev/null; then
  echo "motor coordinator started (pid: $(cat ./log/motor_coordinator.pid), log: ./log/motor_coordinator.log)"
else
  echo "motor coordinator 启动失败，查看日志定位：tail -50 ./log/motor_coordinator.log" >&2
  exit 1
fi
