# Python proxy 接入

## 部署检查

检查用户指定节点、容器和路径中的代码及启动配置，确认文件是绑定挂载、容器内副本还是镜像组成部分。需要重建镜像、容器或修改其他资源时，向用户说明所需变更，取得授权后继续。不查找其他部署。

工作目录使用用户指定位置，目录权限为 0700，敏感文件权限为 0600。`run.json` 记录目标节点、容器、运行入口、原文件路径及 SHA-256、备份位置、改造后 SHA-256、新增模块、启动与重启方式、健康检查、采集配置、起止时间、执行阶段和恢复结果。命令中的凭证应脱敏，不保存完整环境变量。

备份包括所有将改动的源码、启动脚本、容器挂载及环境配置。部署前比对原文件哈希，恢复前比对部署后的文件哈希；不一致时停止覆盖。修改在工作副本中进行，授权部署前不写入运行路径，以免触发自动重载。

## 采集模块接口

部署文件包括修改后的 proxy 和 `capture_runtime.py`，后者应位于 proxy 可导入的位置。写盘进程使用当前 Python 解释器和模块的绝对路径启动，无需导入业务 proxy。配置通过文件控制，不注册 HTTP 管理接口。

```python
import capture_runtime as pc

# 每 worker 的异步启动阶段，配置使用容器内绝对路径。
pc.start("/data/prompt-capture/capture.json")
# 每 worker 退出阶段（放入 lifespan 的 finally）。
pc.capture_shutdown()
```

采集线程在各 worker 启动后创建，不在父进程 fork 前创建。已有 lifespan 中保留原来的清理、取消和信号处理，增加采集模块的启动与关闭调用。

请求体由业务读取后调用，复用已读取的内容，不再次消费请求流：

```python
ctx = pc.begin(
    "/chat/completions", parsed_body, raw_body,
    headers=[(b"x-session-id", b"session-example")],
    arrival=arrival_unix_seconds,
)
```

`headers` 传入实际请求头，头名为小写 bytes。endpoint 使用 `/chat/completions` 或 `/completions`，不含 `/v1`。采集关闭、请求被过滤、未抽中或钩子出错时返回 `None`。`arrival` 为接收请求时记录的 Unix 时间；未传入时以调用时刻计时，报告中应注明。

`ctx` 保存本次请求的采集状态。接入后，业务的鉴权、调度、重试、超时、连接池、响应头和客户端请求体保持不变。

## 普通转发 proxy

- 流式响应：在实际返回给客户端的字节生成器外使用 `ctx.wrap(generator)`。保持逐块字节不变，不额外调用 `decode()` 或重新序列化，不额外消费生成器。
- 非流式响应：调用 `ctx.response_chunk(response_body_bytes)`，随后 `ctx.finish("ok")`；原响应保持不变。HTTP 错误使用 `http_<status>`，异常使用 `error:<类型>`，取消用 `cancelled`，断连用 `client_disconnect`。
- 生成器开始迭代前发生断连或错误时，显式调用 `finish`；此时生成器的 finally 可能尚未执行。`finish` 可重复调用，不重复记录。
- 普通 proxy 无 P/D 阶段，不调用 `prefill_start/prefill_done/decode_started`。对应的 `prefill_ms` 与 TTFT 列为空，仍可分析 `e2e_ms` 和请求结构。首块响应数据不一定包含首 token。
- 基础输出统计可解析响应 JSON/SSE 中的部分 usage 数据，其字段含义应与 P 返回的 prompt/cache 指标分别核对。

## P/D 分离 proxy

存在独立 P/D 阶段时，增加以下调用：

```python
if ctx:
    ctx.prefill_start(actual_request_id)
    extra = ctx.prefill_extra()
# extra 仅可加入发给 P 的请求副本，且必须事先验证 P 支持这些字段。
# P 正常返回后：
if ctx:
    ctx.prefill_done(prefill_http_response, parsed_prefill_json, actual_prefill_node)
# P 失败时：ctx.prefill_failed(exc)
# D 第一块响应实际到达时：ctx.decode_started(actual_decode_node)
```

`prefill_done` 的 response 参数需提供 bytes 类型的 `.content`，采集模块不依赖 httpx。重算时保留第一次 P 结果，增加 `ctx.recomputed`，同一业务请求只记录一次。

golden 使用 `return_token_ids/return_prompt_text` 扩展字段。没有独立 P 响应、后端不支持或扩展字段会改变客户端响应时，保持 `gold_rate=0 follow_gold=false`。离线模板是否与服务一致，需核对实际文本、token IDs 和部署模板。

日志关联使用 proxy 实际发送的请求 ID，保留原有 ID 生成方式。该 ID 与采集模块内部生成的 UUID 不同。请求 ID 重复、重试重算及日志缺失情况应在报告中说明。

## 快速验证与恢复

在隔离端口和 mock 后端运行实际修改副本。相同确定性流量对比改造前、改造后关闭、改造后开启三组：状态码、关键响应头、响应体字节一致；覆盖 chat/completions、SSE、空响应、HTTP 错误、取消及断连。

另需验证配置生效、写盘出错时转发继续、开启后产生可读数据、关闭后不再接收新请求的记录。`tests/mock_proxy.py` 和本技能的测试验证采集模块行为；实际 proxy 修改后仍需单独测试。

部署前列出替换文件、影响实例、重启与健康检查命令、在途请求中断风险，以及失败时自动恢复的范围。经授权执行一次部署；失败时恢复并报告原因，流程终止，不反复重启。

恢复结果分别记录采集开关、磁盘文件和运行实例的状态。恢复文件后，需重启并核对运行入口，才可判定运行实例已恢复。旧进程退出后再删除本次新增的采集模块。

## 开发验证

```bash
python3 -m unittest discover -s <技能目录>/tests -v
```

测试使用临时目录和回环临时端口，不安装依赖。`cases/agent-offline-boundary/` 提供符合 KPBot case-spec 的 agent with/without 对照用例；`evals/` 提供 Python proxy 适配与不支持语言场景。agent 用例需要已有的模型工具和运行时凭证，执行前确认模型调用与镜像使用权限。

该 A/B 用例比较相同分析工具条件下的技能指导效果，不比较整个技能包是否可用。两臂均在 `/workspace/tools/` 获得相同的只读脚本和报告资源，并使用相同的两条分析命令；该目录不含 `SKILL.md` 或参考文档。仅 with 安装 `SKILL.md` 和 `references/`，without 不安装技能；两臂均不向 agent 提供评分用例或测试文件。

agent 用例的 `run_ab.sh` 接收预先构建的镜像和新的结果目录：

```bash
OPENCODE_BIN=<Linux ELF 路径> OPENCODE_AUTH=<auth.json 路径> \
  bash cases/agent-offline-boundary/run_ab.sh <镜像> <新结果目录>
```

二进制路径经 realpath 后限于 `/usr/local/bin/opencode`、系统 opencode 包目录及用户的 `.local/bin/`、`.local/share/opencode/`；认证文件限于用户 opencode 配置/数据目录或 `/etc/opencode/auth.json`。不存在、越界、非 ELF 路径均拒绝。设置 `OPENCODE_SHA256` 时必须与指纹一致。附加 Docker 参数仅接受 bridge/none 网络及 CPU、内存、进程数限制。

只挂载单个认证文件至 root 专用的 `/run/secrets/`，不挂载整个宿主配置目录。临时客户端 HOME 在 `/workspace` 外，认证副本权限为 0600，结束时删除。非 root agent 的读文件策略禁止凭证和外部目录，shell 只允许两条固定的分析命令，两臂一致；不支持任意 shell、自定义宿主 provider 配置或外部目录读写。

`docker_security.sh` 集中定义 capability、`no-new-privileges` 和资源限制，运行器与容器回归测试共同使用。保留的 `CHOWN` 供可信 supervisor 设置临时 HOME 和认证副本的归属，agent 进程仍按非 root 用户运行。

复用已有本地 Linux 镜像运行相同权限配置的回归测试：

```bash
python3 tests/run_container_tests.py --image <已有镜像> --platform <镜像平台>
```

镜像需已有 Python 3、bash 和 sha256sum。测试不拉取镜像或安装依赖，源码只读挂载，网络关闭，完成或中断后删除本次容器。集成测试以 stub client 验证两臂的真实降权、工具执行、结果收集及评分，不调用模型；不能代替真实模型的 A/B 评测。

agent 仅写 `/workspace/opt`。输入、技能、权限配置、验证器及其校验清单归 root 所有；运行前后校验评分链。可信运行器停止 agent 及其辅助进程后，只接收限定大小、归 agent 所有的普通文件，拒绝符号链接和硬链接，复制到 root 专用结果目录后验证。结果通过 `docker cp` 导出，不将宿主结果目录挂入容器。运行结束或中断时清理本次容器。
