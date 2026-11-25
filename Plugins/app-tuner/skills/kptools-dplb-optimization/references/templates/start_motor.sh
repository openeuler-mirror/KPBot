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

# 安全门禁（不要删除）：绑定地址非回环 ⇒ 管理面 API Key 必须已开启且密钥文件存在，
# 否则管理端口（注册/注销后端实例的接口）会在所有网卡上无鉴权裸暴露，任何同网段
# 可达主体都能注册自有"后端实例"劫持推理流量。校验不过直接拒绝启动。
python3 - <<'EOF' || exit 1
import json, os, sys
cfg = json.load(open(os.environ['USER_CONFIG_PATH']))
api = cfg['motor_coordinator_config']['api_config']
kc = cfg['motor_coordinator_config']['mgmt_api_key_config']
host = str(api.get('coordinator_api_host', ''))
if host not in ('127.0.0.1', '::1', 'localhost'):
    if not kc.get('enable_api_key') or not kc.get('api_key_file'):
        sys.exit(
            '拒绝启动：coordinator_api_host={} 为非回环地址且未开启管理面 API Key，'
            '管理端口 {} 将对同网段任一主机无鉴权暴露（可被注册/注销后端实例）。'
            '请二选一：① user_config.json 的 api_config.coordinator_api_host 改回 127.0.0.1；'
            '② 按 SKILL.md 管理面鉴权要点设置 mgmt_api_key_config'
            '（enable_api_key=true + 非空 api_key_file），'
            '并在 register.sh 追加 --mgmt-api-key-file。'.format(
                host, api.get('coordinator_api_mgmt_port')))
    key_file = kc['api_key_file']
    if not os.path.isfile(key_file):
        sys.exit('拒绝启动：api_key_file 指向的密钥文件不存在: {}，'
                 '请先生成密钥文件（权限 600、目录 700）再启动。'.format(key_file))
EOF

# 已有实例识别（必须在打开/截断日志之前）：PID 文件里的旧实例仍在运行时拒绝启动。
# 防止：新进程因端口冲突退出后，旧服务仍在运行，PID 文件却被新进程覆盖、
# 原日志又被截断——旧服务失去停服入口，无从排障与回滚。
if [ -f ./log/motor_coordinator.pid ]; then
    old_pid="$(cat ./log/motor_coordinator.pid 2>/dev/null)"
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null \
       && ps -p "$old_pid" -o args= 2>/dev/null | grep -q 'motor\.coordinator\.main'; then
        echo "拒绝启动：已有 coordinator 实例在运行 (pid: $old_pid)。请先核对其归属并停服后重试：" >&2
        echo "  ps -p $old_pid -o args=   # 确认为 python3 -m motor.coordinator.main" >&2
        echo "  kill $old_pid             # 确认后停旧实例" >&2
        exit 1
    fi
fi

nohup python3 -m motor.coordinator.main > ./log/motor_coordinator.log 2>&1 &
new_pid=$!

# 存活校验：启动 2 秒后确认进程还在，避免"秒退仍报成功"的假成功信号；
# 校验通过后才写 PID 文件——防止新实例因端口冲突退出时覆盖旧 PID 文件，
# 导致仍在运行的旧服务失去停服入口。
sleep 2
if kill -0 "$new_pid" 2>/dev/null; then
    echo "$new_pid" > ./log/motor_coordinator.pid
    echo "motor coordinator started (pid: $new_pid, log: ./log/motor_coordinator.log)"
else
    echo "motor coordinator 启动失败，查看日志定位：tail -50 ./log/motor_coordinator.log" >&2
    exit 1
fi
