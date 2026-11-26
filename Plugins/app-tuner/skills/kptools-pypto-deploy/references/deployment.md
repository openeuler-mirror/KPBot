# Ascend 真机启动与 CannLab 排障

## 前置和普通 Serving

用 Linux 目标主机检查 npu-smi info、实际分配的设备、模型 config 与权重分片、CANN 和模型支持的平台。使用当前 Serving pin 的 pypto-lib 及匹配的 PyPTO、Simpler、PTOAS 源构建，确认 Python 加载到的是匹配版本。加载该环境的 Ascend set_env.sh。P/D 模型节点还需要 os.pidfd_open、Mooncake Ascend、owner-local service 和 retain/release hooks。按需要从目标 checkout 执行 python -m pip install -e '.[pd]'，再确认设备运行时已经按模型文档安装。

先设置 MODEL、PLATFORM、DEVICES 为实际值，再补模型要求的拓扑、内存与上下文参数。

~~~bash
pypto-serving --model "$MODEL" --backend npu --platform "$PLATFORM" \
  --devices "$DEVICES" --host 127.0.0.1 --port 8000 \
  --generate-config '{"max_new_tokens":16,"temperature":0}'
curl -fsS http://127.0.0.1:8000/health
~~~

服务放在独立终端或目标环境的任务管理器中。只在明确有设备 broker 时使用 task-submit 包装已验证的命令，遵守 broker 分配的设备。不要在已有共享实例上覆盖端口或进程。

## P/D 真机启动

1. 复制 examples/pd.json，填实际 P/D 的 host、port 和 role。需要时设置 control_port、transfer_hostname。P、D、Router 共用配置。检查节点 HTTP、控制、Mooncake 数据平面可达，以及一致的权重和兼容的缓存布局。
2. 下面是 **DSpark K7 每节点 EP16/TP4/DP4** 模板。两台节点都设置 MODEL、PD_CONFIG、DEVICES、PD_ROLE、PD_NODE_ID；设备来自各自实际分配，PD_NODE_ID 命中配置中的节点名或 host:port。

~~~bash
pypto-serving --model "$MODEL" --served-model-name dsv4 \
  --backend npu --platform a2a3 --devices "$DEVICES" \
  --ep 16 --tp 4 --dp 4 --block-size 32 \
  --max-model-len 655360 --max-num-batched-tokens 8192 \
  --long-prefill-token-threshold 8192 \
  --speculative-config '{"method":"dspark","num_speculative_tokens":7}' \
  --ring-heap 1073741824,1073741824,1073741824,1073741824 \
  --generate-config '{"max_new_tokens":128,"temperature":0}' \
  --pd-config "$PD_CONFIG" --pd-role "$PD_ROLE" --pd-node-id "$PD_NODE_ID" \
  --host 0.0.0.0 --port 8000
~~~

3. P 和 D 的 /health 与 /internal/pd/descriptor 就绪后，启动 Router。Router 使用外部已启动节点。

~~~bash
pypto-serving-router --replicas "$PD_CONFIG" --host 127.0.0.1 --port 8110
curl -fsS http://127.0.0.1:8110/health
curl -fsS -N http://127.0.0.1:8110/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dsv4","messages":[{"role":"user","content":"你好"}],"temperature":0,"max_tokens":16,"stream":true}'
~~~

检查生成内容、SSE 事件和终态。仅有 HTTP 200 可能仍含流内错误。P 侧按需启用 PYPTO_SERVING_PD_DIAG=1，并用 PYPTO_SERVING_PD_DIAG_LOG 指定独立日志路径。

## 可调配置

| 参数 | 选择依据 |
|---|---|
| devices / dp / ep / tp / lm-head-tp | DSpark 当前要求 EP 为 8 或 16，设备数等于 EP，DP=EP/attention TP。LM head TP 独立。按当前模型和硬件检查。 |
| speculative-config | DSpark P/D 两边的启动参数都填 K7。P 在内部用 target-only Prefill，D 保留 K7。 |
| max-model-len / max-num-seqs / max-num-batched-tokens / long-prefill-token-threshold | 分别控制上下文、并发槽、调度 token 预算和 P chunk。上下文包含输入与输出。D 可用独立 token 预算。按当前版本的 D 侧阈值行为和 HBM 容量配置。 |
| ring-heap / ring-task-window / ring-dep-pool | 从已验证配方开始，按编译与 runtime 实际报错选择。PYPTO_PROG_BUILD_DIR 按版本和拓扑隔离。 |
| prefix cache / DDR offload | P 和 D 可独立选择 prefix caching。DDR offload 有模型与后端约束。复用超过一个完整块的前缀才能检验本地 lookup 和 restore。 |
| handoff_timeout / request_timeout / capacity | 由 PD JSON 控制。handoff 用于具体控制或传输，request 用于完整 Prefill 等待和请求。先定位是哪一段超时。 |
| --profile / PD_DIAG | SA trace 与请求耗时日志是独立采集路径。 |

## CannLab 连接与排障

- 从本机 OpenCode 的 cannlab agent 配置读取当前 SSH 别名、转发程序和工具入口。典型路径为 Windows 经 VS Code CannLab 隧道 → 持久 CPU 容器 → remote_tool.sh → 临时 NPU Pod。SSH 别名出现 Connection closed 时先查是否有禁用的 ProxyCommand、监听是否存在；用已有官方扩展转发恢复，然后再验证 SSH。维持当前主机校验，不输出凭据。
- CPU 上按环境配置加载 DEVENV_API_BASE。先用 remote_tool.sh exec --no-sync 探测设备、权重和代码版本。默认同步会覆盖 NPU 并可能删除 Pod 独有文件。只在范围和状态明确时同步。使用独立测试目录，结果当次拉回 CPU 持久层，再取回本机。复杂 Bash 脚本先以 LF 上传以免 PowerShell 与 SSH 多层引号变形。
- 本次 CannLab Pod 查到两张健康的 Ascend 910 且模型目录为空，不能启动当前完整真机 DSpark P/D。其他 Pod 重新核查。CPU 的 pyfix312 是特定 Python ABI 的兼容措施，不能继承进 NPU Python 3.12.9。检查依赖加载位置、编译器、运行库与权限。根据实际错误才处理 /dev/shm、AICPU 目录或执行超时，保留匹配的 ABI 检查。
- 遇到启动慢要区分 Pod 开机、SSH、依赖导入、权重 staging、内核编译和 Worker warmup。历史日志里提到的设备 stepping 和同步 core 数限制必须以当前设备与真实日志确认。不要用模拟数据证明实机性能。



## CannLab 链路

CannLab 三级链路（SSH 隧道/DEVENV_API_BASE/remote_tool.sh/rsync 同步/pyfix312 ABI 边界/dev/shm/AICPU 目录）的完整操作手册与踩坑实录见仓内`kptools-cannlab-env` skill——本手册不重复，遇到链路问题直接路由。
