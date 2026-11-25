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
