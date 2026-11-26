#!/bin/bash

unset http_proxy
unset https_proxy
unset HTTPS_PROXY
unset HTTP_PROXY

set -o pipefail

mkdir -p ./log

# 前台执行、输出同时落盘：首次注册要看清每个实例的返回结果，不要后台化。
# 实例语义：一个 --prefill/--decode = 一个实例；同一实例的多个 DP endpoint 用逗号连接
# （IP:PORT[,IP:PORT...]）；不同实例重复传参（见下方 --decode 示例）。
# --mgmt-port 必须与 user_config.json 的 coordinator_api_mgmt_port 一致（换端口时同步修改）。
# --coordinator 默认 127.0.0.1，仅适用于与 coordinator 同机执行；跨机注册时改为 coordinator 实际 IP。
# user_config.json 开启 mgmt_api_key_config 时，本命令需追加 --mgmt-api-key-file <密钥文件路径>。
python3 -m motor.coordinator.register \
  --coordinator "http://127.0.0.1" \
  --mgmt-port "7201" \
  --prefill "xx:9900,xx:9901" \
  --decode "yy:9162" \
  --decode "yy:9163,yy:9164" \
  --model-name "glm53" 2>&1 | tee ./log/register.log
