---
name: kptools-dplb-optimization
description: >
  连接远端机器部署 MindIE Motor 负载均衡器并对推理负载均衡进行优化。仅当用户明确
  提出部署/安装/配置负载均衡组件的动作时使用本技能：部署/安装 MindIE Motor（自备
  whl 或源码编译）、生成或修改 user_config.json / register.sh / start_motor.sh
  配置三件套、调整 vLLM 或 MindIE 多实例的 LB 策略配置。用户只描述负载不均衡、
  推理请求分发不均等现象而未提出部署/配置诉求的（如"负载不均想优化"），不要直接
  触发本技能——先向用户确认是否需要部署负载均衡组件；仅要采集负载指标、出均衡性
  分析或对比报告的诉求，改用 kptools-dplb-profiler。
license: Proprietary
compatibility: 远端 Linux（openEuler/EulerOS 等，aarch64/x86_64）；需要 SSH 访问远端机器；远端需有 Python >= 3.11 + pip（motor 3.x whl 元数据 Requires-Python >=3.11）；运行依赖按上游 requirements.txt 全量安装（whl 无 Requires-Dist，pip 不会自动安装任何依赖，见阶段 3 依赖清单）；方式 B（源码编译）还需 git、gcc、grpcio-tools（版本须与运行时 grpcio 一致）及 Rust 工具链（cargo，离线安装见 references/field-playbook-offline-build.md）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: load_balancing_optimization
  version: "1.0.0"
---

# kptools-dplb-optimization：MindIE Motor 负载均衡部署与优化

连接远端机器安装 MindIE Motor（方式 A 自备 whl / 方式 B 源码编译），生成 `user_config.json`、`register.sh`、`start_motor.sh` 三件套配置，并产出一份部署指南 `.md` 告诉用户如何启动 MindIE Motor。

核心原则：

- **配置生成必须基于 `references/templates/` 中的模板**，不要凭记忆手写配置字段——模板是经过验证的口径，字段名和层级错了 LB 起不来
- **配置文件名统一为 `user_config.json`**（`start_motor.sh` 通过 `USER_CONFIG_PATH` 环境变量引用它——上游只读这一个配置定位环境变量，`motor.coordinator.main` 无 `--config` 参数、也不存在 `MINDIE_MOTOR_CONFIG_PATH`）。不要叫 `user.json` 或 `user.config`

## 快速决策

1. 用户给了远端机器 + 要求部署/优化负载均衡 → 走完整流程（阶段 1 → 5）
2. 用户只要生成配置文件（远端已装好）→ 跳到阶段 4
3. 用户已有配置但负载仍不均 → 先读现有 `user_config.json`（端口/策略）与 `register.sh`（后端实例列表）检查字段，再决定是否重新生成；**部署后需要量化评估均衡效果时，衔接 `kptools-dplb-profiler` 技能**（采集 dp_load_metrics 出单池/A-B 对比报告，本 skill 负责部署、profiler 负责度量）
4. 用户只描述负载不均衡现象、未提部署/配置诉求 → 先向用户确认意图：要部署负载均衡组件（MindIE Motor）才走本 skill，只要采集指标/分析报告转 `kptools-dplb-profiler`；意图不明时不要直接生成部署配置

## 阶段 1：信息收集（缺什么问什么，不要猜）

- **远端机器**：SSH 地址（IP、用户名、认证方式）。多台时要明确装在哪台（通常 LB 独占一台，或与某个后端同机）
- **连接方式**：优先使用 `ssh-mcp-server` 的 `upload` / `execute-command` / `download` 工具；无 MCP 时回退本地 `ssh`/`scp` 命令
- **安装方式（二选一，让用户选择）**：
  - **方式 A：用户自备 whl 包**——用户本地已有 `.whl` 文件，直接上传到远端安装，跳过拉源码和编译。需确认 whl 的本地路径与目标架构（aarch64/x86_64）匹配
  - **方式 B：拉源码编译**——远端 clone 源码仓库后用官方入口 `bash build.sh` 编译再安装（**严禁 `pip3 wheel .`**，会产出缺 Rust 原生库的空壳 whl）。默认仓库 `https://gitcode.com/Ascend/MindIE-Motor`（分支默认 master），用户指定了其他仓库/分支/commit 时以用户为准；用户没说时先展示默认值并向用户确认，不要静默克隆
  - 用户两种都没提时，主动询问用户选哪种，不要替用户决定
- **后端实例列表**：LB 后面挂的推理实例（vLLM 或 MindIE，均可能），每个实例形如 `ip:port[,ip:port...]`——**同一实例的多个 DP endpoint 用逗号连接**（与 kptools-dplb-profiler 的 DP endpoint 口径一致），不同实例才分开列；询问后端引擎类型（用于网络预检与验证命令，如 vLLM 的 `/v1/models` 探活路径）
- **网络拓扑**：coordinator 与后端实例是同机、同容器网络还是跨容器/跨机——跨容器部署时，后端地址必须是 **coordinator 所在网络命名空间内可达**的地址（容器网 IP 或已发布端口），否则会出现注册显示健康、推理请求却 500 的情况；推理客户端需要跨机访问 coordinator 时，须把 `coordinator_api_host` 改绑非回环地址（实际 IP / `0.0.0.0`），**且必须先按阶段 4 的管理面鉴权要点开启管理面 API Key**（`start_motor.sh` 内置"非回环 ⇒ 鉴权必须开启"门禁，未开启会拒绝启动）
- **安装路径**：默认建议 `/opt/mindie-motor`，用户可指定
- **负载均衡策略**：询问用户期望——上游 `prefill_scheduler_type` / `decode_scheduler_type` 实际支持 4 个值：`load_balance`（负载感知均衡，默认）、`round_robin`（轮询）、`kv_cache_affinity`（KV 缓存亲和，需 kv-conductor）、`dynamic_bucket`（动态分桶）；无明确要求时用模板默认值 load_balance 并在部署指南中说明。**选 `kv_cache_affinity` 时的配套与启动后校验（缺一不可）**：`user_config.json` 需同步配置 `kv_conductor_config`（含 `conductor_service` 引擎名，以现场模板与上游文档字段为准）——上游 `CoordinatorConfig.from_json` 对未知字段与空值**静默通过**：拼错字段名、`conductor_service` 为空字符串都不会在启动时报错，服务会以空 conductor 空跑（亲和调度不生效但不报错）。因此该策略下配置错误**不会在启动时被发现**：启动后必须用 dplb 的 admin 接口（或 coordinator 日志）确认 `conductor_service` 非空、实际生效的引擎名与配置一致，空值或不匹配立即停止并回查配置，不得当作启动成功交付

## 阶段 2：环境检查（SSH 连通后先做，再动手装）

```
# 在远端依次确认，任一缺失先解决再继续：
python3 --version && pip3 --version   # Python >= 3.11（motor 3.x whl 元数据 Requires-Python >=3.11）
df -h <安装路径>                      # 磁盘余量
npu-smi info                          # 若后端在 NPU 上，确认驱动正常（可选）

# 运行依赖预检（whl 未声明 Requires-Dist，完整清单以上游 requirements.txt 为准；
# 此处探 coordinator 硬 import 的代表性模块）：
python3 -c "import OpenSSL, watchdog, grpc, fastapi, pydantic, zmq, msgspec, uvicorn" 2>/dev/null && echo DEPS_OK || echo DEPS_MISSING
# DEPS_MISSING 时按阶段 3 方式 A 第 4 步补装：在线 pip3 install -r requirements.txt；
# 离线按上游 standalone.md 附录 A（本地 pip download -r requirements.txt 全量下载后上传安装）

# 端口预检（计划端口被占用时整套换端口，如 7300/7301/7302，并同步 user_config.json 与 register.sh）：
ss -ltn 2>/dev/null | grep -E ':(7200|7201|7202)\b' || echo PORTS_FREE
# 容器化部署时 ss 只能看到本网络命名空间的监听，跨容器占用可辅以 curl 探测（有响应即被占）：
curl -m 3 -s -o /dev/null http://127.0.0.1:7200/ && echo PORT_7200_ANSWERED

# 仅当选择方式 B（拉源码编译）时才需要检查：
git --version
gcc --version                        # 编译 whl 需要（上游 build.sh/Makefile/setup.py 均不用 cmake，预检无需 cmake）
cargo --version                      # Rust 工具链：编译 libmindie_workload_shm.so（coordinator 必需，无 Python 兜底）
pip3 show grpcio-tools >/dev/null && echo GRPCIO_TOOLS_OK || echo GRPCIO_TOOLS_MISSING
python3 -c "import grpc; print('runtime grpcio:', grpc.__version__)"   # 决定 grpcio-tools 必须匹配的版本
```

- 编译环境缺失时，先与用户确认是否允许 `yum/dnf install` 安装依赖，不要静默装系统包
- 检查是否已有旧版本：`pip3 show motor`（**包名是 `motor` 不是 `mindie-motor`**——motor 3.x whl 元数据 `Name: motor`，用错名字永远查不到），有则询问覆盖还是并存
- **外网连通性检查**（方式 B 必做；方式 A 在线补依赖时同样适用）：`curl -m 5 -sI https://gitcode.com >/dev/null && echo OK || echo UNREACHABLE`，需要 pip 装依赖时同样先测 `pypi.org`。任一不可达都立即反馈用户"远端无法访问外网"，并给出替代建议（改用方式 A 上传 whl、依赖 whl 由本地经内网镜像下载后上传、用户提供离线源码包），让用户选择，不要自行切换方案后闷头继续

## 阶段 3：安装 MindIE Motor（按阶段 1 确认的方式二选一）

> **部署形态说明（务必让用户知晓）**：MindIE Motor 采用**独立部署 coordinator** 的方式——它只做负载均衡（请求分发），**不管理 vLLM/MindIE 实例的拉起与生命周期**。后端实例由用户原有方式启动和运维，Motor 通过 register.sh 动态注册感知它们。这样设计是为了让负载均衡器对原有部署方式的影响最小。用户如果以为装了 Motor 会帮忙拉起推理实例，要主动纠正这个预期。

### 依赖清单（分级）

| 级别 | 依赖 | 说明 |
| --- | --- | --- |
| 启动必需（Python 运行依赖） | 上游 `requirements.txt` 全量（23 项：fastapi、uvicorn、grpcio、protobuf、pydantic、msgspec、requests、httpx、pytest-asyncio、pyOpenSSL、watchdog、kubernetes、opentelemetry×2、pyzmq、uvloop、anyio、setproctitle、confluent_kafka、transformers、Pillow、ai-asset-obfuscate 等） | motor 3.x whl 元数据**无 Requires-Dist**（`setup.py` 的 `install_requires=[]`，这是 ISS-01/ISS-02 的根因），`pip3 install` whl 不会自动安装任何依赖；coordinator 硬 import fastapi/pydantic/zmq/msgspec/uvicorn 等，**只补 pyOpenSSL/watchdog 装不出可启动的 coordinator**。清单以上游对应版本 `requirements.txt` 为准 |
| 运行时已有、预检确认 | grpcio | motor 运行依赖 gRPC（实例注册与通信），实测环境通常随基础镜像已具备；预检一并探测，缺失同样启动失败——缺失时随 requirements.txt 一并补装（版本无特殊要求，不要与方式 B 的 grpcio-tools 版本匹配规则混淆），不要静默 pip install |
| 方式 B 编译依赖 | git、gcc、cargo（Rust 工具链）、grpcio-tools（+protobuf） | cargo 编译 `libmindie_workload_shm.so`（coordinator 必需）；grpcio-tools 供 `generate_proto.sh` 生成 proto，**版本必须与运行时 grpcio 一致**（如运行时 1.82.1 就装 1.82.1，用 1.85.3 会生成不匹配的 pb 文件）。kv-conductor 另需 libzmq 开发包，可用 `SKIP_KV_CONDUCTOR_BUILD=1` 跳过（可选组件） |
| 环境可选 | npu-smi、curl | NPU 驱动检查、端口/后端探活用 |

### 方式 A：用户自备 whl 包（跳过源码）

```
# 1. 上传用户提供的 whl 到远端（ssh-mcp-server upload 或 scp）
#    例：motor-xxx-py3-none-any.whl 或早期命名的 mindie_motor-xxx.whl -> /tmp/mindie-motor-whl/
#    注意：whl 元数据无 Requires-Dist，pip 安装它不会自动装任何运行依赖；
#    依赖按上游 requirements.txt 全量补（见第 4 步），缺模块时启动报 ModuleNotFoundError
# 2. 检查运行依赖是否已存在（完整清单以对应版本 requirements.txt 为准，此处探代表性硬 import）
python3 -c "import OpenSSL, watchdog, grpc, fastapi, pydantic, zmq, msgspec, uvicorn" 2>/dev/null && echo DEPS_OK || echo DEPS_MISSING
# 3. 安装前断言 whl 含原生库（用户拿到的"编译好的 whl"也可能是 pip wheel 空壳，同 ISS-09）。
#    文件名可能是 motor-*.whl 或 mindie_motor-*.whl，统一用 *motor*.whl 匹配，并防空 glob：
python3 -c "import zipfile,glob; w=glob.glob('/tmp/mindie-motor-whl/*motor*.whl'); assert w, '目录下找不到 motor whl（*motor*.whl）'; names=zipfile.ZipFile(w[0]).namelist(); assert any(n.endswith('libmindie_workload_shm.so') for n in names), '空壳 whl：缺 libmindie_workload_shm.so，coordinator 起不来，需换完整 whl'; print('native lib OK:', w[0])"
#    （用户自备的 whl 若确为官方/完整包，此断言秒过；空壳则当场拦截，避免装完才发现起不来）
# 4. 安装（DEPS_OK 时跳过依赖安装，只装 whl 本体）
#    在线环境装依赖：向用户索取/从源码仓库获取对应版本 requirements.txt 后
#      pip3 install -r requirements.txt
#    离线环境按上游 standalone.md 附录 A 全量下载后上传安装：
#      本地：pip download -r requirements.txt -d /tmp/offline_pkgs \
#              --python-version 3.11 --platform manylinux2014_<aarch64|x86_64> --only-binary=:all:
#      上传 /tmp/offline_pkgs 到远端后：pip3 install /tmp/offline_pkgs/*.whl
#    whl 本体安装（--force-reinstall 而非 --upgrade：同版本号换 whl 内容时 --upgrade 不会重装）：
pip3 install --no-deps --force-reinstall /tmp/mindie-motor-whl/*motor*.whl
# 5. 验证（3.x 包名为 motor——pip3 show motor；早期 1.x whl 包名可能是 mindie_motor，
#    对应 pip3 show mindie_motor；入口模块 motor.coordinator；在源码目录之外执行）
pip3 show motor && python3 -c "import motor; print(motor.__file__)"
python3 -c "import OpenSSL, watchdog, grpc, fastapi, pydantic, zmq, msgspec, uvicorn; print('deps ok')"
```

### 方式 B：拉源码、编译 whl、安装

> **严禁 `pip3 wheel .`（ISS-09 实测）**：`setup.py` 是**条件打包**——只把磁盘上已存在的 `motor/kv_conductor/bin/kv-conductor` 与 Rust 编译产物 `.so` 打进 wheel，而 `pip wheel` **不会调用 build.sh**、不编译 Rust 组件。实测产出 1.2MB 空壳 whl（官方 aarch64 whl 为 3.6MB），缺 `libmindie_workload_shm.so` 与 `kv-conductor`，**安装后 coordinator 无法启动**。必须走官方入口 `bash build.sh`。

```
# 1. 克隆源码（默认仓库 https://gitcode.com/Ascend/MindIE-Motor，以用户确认为准）
git clone https://gitcode.com/Ascend/MindIE-Motor.git -b <分支，默认 master> /tmp/mm-src
cd /tmp/mm-src
chmod +x build.sh scripts/*.sh     # 防执行位丢失（实测 generate_proto.sh Permission denied）
# 2. 前置装依赖：先按源码树自带 requirements.txt 全量安装（proto 生成等构建步骤与运行时都依赖它），
#    再装 grpcio-tools（版本必须与运行时 grpcio 一致，先查运行时版本）
pip3 install -r requirements.txt
python3 -c "import grpc; print(grpc.__version__)"
pip3 install 'grpcio-tools==<运行时版本>' 'protobuf>=7.35.1,<8.0.0'
#    离线环境：requirements.txt 依赖按上游 standalone.md 附录 A 全量下载后上传安装；
#    grpcio-tools 本地下载对应版本 whl 上传后 pip3 install --no-deps --force-reinstall 安装
# 3. 先跑一次看缺什么（缺 cargo 时秒级失败；cargo 已就绪时会直接完成编译，等于正式编译）：
SKIP_RUST_INSTALL=1 bash build.sh
#    只缺 cargo 时立即报 "[ERROR] refusing to emit dist/motor-*.whl: cargo is required
#    to compile libmindie_workload_shm.so"——据此决定装 Rust（见下）或走预编译产物
# 4. 正式编译
bash build.sh
#    离线/受限环境：
#    SKIP_RUST_INSTALL=1 SKIP_KV_CONDUCTOR_BUILD=1 CARGO_NET_OFFLINE=true bash build.sh
# 5. 选定并断言 wheel 含原生库（防空壳 whl 装上起不来）——build 可能同时产出
#    -none-any 与 -linux_<arch> 两个 wheel，本步会优先选出平台标签包，**记住它打印的文件名**：
python3 -c "import zipfile,glob; w=[f for f in glob.glob('dist/motor-*.whl') if 'none-any' not in f] or glob.glob('dist/motor-*.whl'); assert w, 'dist 下未找到 motor-*.whl'; names=zipfile.ZipFile(w[-1]).namelist(); assert any(n.endswith('libmindie_workload_shm.so') for n in names), '空壳 whl：缺 libmindie_workload_shm.so，请用 bash build.sh 重新编译'; print('native lib OK:', w[-1])"
#    kv-conductor 为可选组件（SKIP_KV_CONDUCTOR_BUILD=1 时不含），coordinator 无它可启动
# 6. 安装与验证——**用第 5 步打印的明确文件名安装**，不要 glob dist/motor-*.whl
#    （glob 会把 -any 与平台包先后装、相互覆盖，最终装上哪个取决于文件名字母序）；
#    离线加 --no-index（防 pip 尝试访问不可达 index 卡住）；import 验证必须在源码目录之外执行
pip3 install --no-deps --no-index --force-reinstall dist/<第5步选中的文件名>.whl
cd /opt/mindie-motor   # 或任意源码目录之外的位置
python3 -c "import motor; print(motor.__file__)"   # 应指向 site-packages/motor/__init__.py
pip3 show motor
```

**cargo 离线安装**（远端无外网时，完整步骤见 `references/field-playbook-offline-build.md`；空间预检：离线包 ~345MB + 上传/解压两份副本 + cargo target，`df -h /tmp /usr/local` 确认余量——容器 /tmp 常只有几百 MB）：

1. 本机从 Rust Forge 归档页（`https://forge.rust-lang.org/infra/archive-stable-version-installers.html`，本机代理可达、远端不可达）下载对应架构离线包（如 `rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz`，约 345MB），SHA256 校验
2. **分片上传**：MCP 单次 upload 300s 超时约 285MB——超过就切分（如 20MB/片）上传，远端 `cat` 顺序追加后 `sha256sum` 复核
3. 远端安装：`./install.sh --prefix=/usr/local/rust --without=rust-docs --disable-ldconfig`；`export PATH=/usr/local/rust/bin:$PATH`——**该 export 只在当前 SSH 会话有效**，`execute-command` 每次都是新 shell，下一条命令里 cargo 就"消失"：把 export 追加进 `~/.bashrc`，或在每条构建命令串里带上 `export PATH=/usr/local/rust/bin:$PATH && ...`
4. **离线 vendor libc**：源码无 Cargo.lock，cargo 离线解析不到 `libc`——本机从 crates.io 下载 `libc-0.2.191.crate`，上传解到 `vendor/libc`，补 `.cargo-checksum.json` 与 `.cargo/config.toml`（vendored-sources，详见 playbook）

**预编译产物直通**（无 cargo 且拿得到现成产物时）：`WORKLOAD_SHM_PREBUILT=<path>/libmindie_workload_shm.so`、`KV_CONDUCTOR_PREBUILT=<path>/kv-conductor` 传给 build.sh 直接打包。

注意：

- `execute-command` 的 timeout 给足（建议 ≥ 600000 ms）——Rust 编译本身很快（实测 libc+workload-shm 约 2s），但 proto 生成与打包有额外耗时；首次跑也可 nohup 后台再轮询日志
- 克隆/编译失败常见原因是网络不通（内网源）或缺编译依赖，失败时把完整日志贴给用户分析，不要盲目重试
- **流程中任何一步因无法访问外网而失败**（clone 超时、pip 下载依赖失败等），都要立即反馈用户并说明是网络原因，附上错误日志关键行，然后让用户决定后续方案（换内网源/上传离线包/换机器），不要静默重试或自作主张改方案
- 源码编译出的 whl 同样**不带依赖声明**（`install_requires=[]`），依赖按源码树自带的 `requirements.txt` 安装：**编译前先 `pip3 install -r requirements.txt`**（proto 生成等构建步骤与运行时都依赖它；离线按上游 standalone.md 附录 A 全量下载后上传安装），安装后按方式 A 第 2、5 步的探测/验证命令复核
- **升级/重装前先停旧 coordinator（先核对进程身份，匹配才 kill，等退出再重装）**：PID 文件 `./log/motor_coordinator.pid` 里的 PID 可能已退出并被系统复用，直接 `kill "$(cat ./log/motor_coordinator.pid)"` 会误停无关服务。正确顺序：① 读出 PID 后核对 `ps -p <PID> -o args=`，命令行确为 `python3 -m motor.coordinator.main`（且归属本部署实例）才 `kill <PID>`；不匹配则不要 kill，改用 `ps -ef | grep motor.coordinator.main` 找到真实进程再处理；② kill 后等待该进程真正退出再继续重装（`while kill -0 <PID> 2>/dev/null; do sleep 1; done`，长时间不退则人工介入）。`--force-reinstall` 只覆盖磁盘文件，**不会重启已运行的进程**：旧代码继续服务、端口仍被占用，装完再启动会报端口冲突，用户看到的"升级没生效"多半是旧进程没停
- **coordinator 重启后已注册实例清空**（注册表是内存态）：每次 start_motor.sh 之后都要重新执行 register.sh，否则推理请求报 `No instance available`——重启不是幂等的，"注册"必须跟在每次启动后面
- **解释器一致性**：pip3 装进哪个 Python（系统 / venv / 容器内自编译解释器），start_motor.sh 里的 `python3` 就必须解析到**同一个**——venv 安装 + 系统 python3 启动会 `ModuleNotFoundError: motor`；部署前 `which python3` 与 `pip3 show motor`（Location 字段）核对两者指向一致

> **严禁用 `--help` 做验证**：`python3 -m motor.coordinator.main --help` 不会打印帮助，而是打印 banner 后**直接启动 Coordinator Daemon**，占用内置默认端口 1025（推理）/ 1026（管理）/ 1027（观测）。验证安装只允许 `pip3 show motor` + `python3 -c "import motor; print(motor.__file__)"`；验证服务可用要等阶段 4 生成 `user_config.json` 并启动后，探活管理端口的 `/liveness`、`/readiness`。若已误启动：`ps -ef | grep motor.coordinator.main` 找到 PID 后 `kill <PID>` 清理，**不要用 `pkill -f motor.coordinator.main`**（会误杀命令行中含该模式的当前 shell）

## 阶段 4：生成配置三件套

读取 `references/templates/` 下的模板，替换为用户实际的后端实例列表与策略参数，生成到安装路径：

| 文件 | 作用 | 生成方式 |
| --- | --- | --- |
| `user_config.json` | coordinator 配置：监听端口（默认 7200 推理 / 7201 管理 / 7202 观测）、prefill/decode 调度策略（默认 load_balance）、管理面鉴权开关、PD 分离回退开关 | 由模板填充。可调字段：`coordinator_api_host`（模板默认 `127.0.0.1` 仅回环监听——推理客户端跨机访问时才改绑实际 IP / `0.0.0.0`，**改绑非回环必须同步开启管理面 API Key**，见下方"管理面鉴权"要点）、三个端口、`prefill_scheduler_type` / `decode_scheduler_type`（load_balance / round_robin / kv_cache_affinity / dynamic_bucket 之一，默认 load_balance）、`mgmt_api_key_config`、`endpoint_instance_score_weight`、`dp_stats_window`。版本号 `version` 与字段层级**不要动** |
| `register.sh` | 向 coordinator 注册后端实例。`--prefill` 可重复多实例（PD 分离的 P 节点），`--decode` 可重复多实例（D 节点），`--model-name` 为模型名；**一个参数 = 一个实例，同一实例的多个 DP endpoint 在该参数内用逗号连接** | 由模板填充实例地址。coordinator 地址默认 `http://127.0.0.1`、mgmt 端口 7201（与 user_config.json 的 `coordinator_api_mgmt_port` 保持一致；**换端口时必须同步改 `--mgmt-port`**）；**默认仅在 coordinator 同机执行**，跨机注册时把 `--coordinator` 改为 coordinator 实际 IP，且开启了管理面 API Key 时命令需追加 `--mgmt-api-key-file <密钥文件>`。模板为**前台执行**并 `tee` 到 `./log/register.log`——首次注册要看清每个实例的返回结果，不要后台化 |
| `start_motor.sh` | 启动 MindIE Motor coordinator | 基本照抄模板。脚本会 unset 代理变量、设 `USER_CONFIG_PATH`（上游唯一读取的配置定位环境变量）、**启动前执行管理面安全门禁：`coordinator_api_host` 非回环 ⇒ `mgmt_api_key_config` 必须已开启且密钥文件存在，否则拒绝启动并给出两选一提示（改回 127.0.0.1 或补齐鉴权），改脚本时不要删该校验**、自建 `./log` 目录、nohup 启动 `python3 -m motor.coordinator.main`，PID 落盘到 `./log/motor_coordinator.pid`（便于误启动后按 PID 清理），并 `kill -0` 校验进程存活、未存活即报失败。日志级别不要用环境变量调——由 user_config.json 的 `logging_config.log_level` 控制（DEBUG/INFO/WARNING/ERROR，模板未写该字段时用内置默认 INFO） |

生成时的要点：

- 后端实例**只出现在 `register.sh`**（--prefill / --decode 参数），`user_config.json` 里不写实例列表——实例是动态注册的，不是静态配置。**一个参数 = 一个实例**：同一实例的多个 DP endpoint 用逗号连接（如 `--prefill "192.0.2.11:8000,192.0.2.11:8001"`），多实例才重复传参——把同一实例的多个 endpoint 拆成多个参数会被注册成多个独立实例，分发口径就错了
- 用户没提 PD 分离时，先问清 P/D 节点划分再填 `--prefill`/`--decode`；纯 DP 部署（无 PD 分离）时两组都可能挂同批实例，以用户实际拓扑为准
- **生成前先做端口预检**：`ss -ltn 2>/dev/null | grep -E ':(7200|7201|7202)\b' || echo PORTS_FREE`（容器内可辅以 `curl -m 3 http://127.0.0.1:<port>/` 探测，有响应即被占）。被占用时整套换端口（如 7300/7301/7302），**user_config.json 的三个端口与 register.sh 的 `--mgmt-port` 必须同步修改**，部署指南中的端口一并更新
- **管理面鉴权（按实际 bind 地址与端口可达性判定，不按"同机部署"假设）**：模板默认 `coordinator_api_host: 127.0.0.1` + `enable_api_key: false`——管理端口仅监听回环，默认关闭鉴权是安全的。用户确需跨机访问推理端口而改绑非回环地址（实际 IP / `0.0.0.0`）时，**必须先开启管理面 API Key 再生成/执行 start_motor.sh**：生成密钥文件（`install -d -m 700 /opt/motor-coord/secrets && python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /opt/motor-coord/secrets/mgmt-api-key && chmod 600 /opt/motor-coord/secrets/mgmt-api-key`），在 user_config.json 的 `motor_coordinator_config.mgmt_api_key_config` 设 `enable_api_key: true` + 非空 `api_key_file`，register.sh 命令追加 `--mgmt-api-key-file`。生成 start_motor.sh 前必须校验**绑定地址非回环 ⇒ 鉴权已开启且密钥文件存在**，不满足即中止并请用户二选一（改回 127.0.0.1 / 补齐鉴权），不得无鉴权对外部署；start_motor.sh 模板已内置同口径门禁（改脚本时不要删），启动时兜底拦截
- `start_motor.sh` 和 `register.sh` 都把日志写到 `./log/`，模板已内置 `mkdir -p ./log` 自动建目录，无需手动预建
- **`start_motor.sh` 全部使用相对路径**（`./user_config.json`、`./log/`），必须在部署目录内执行（`cd /opt/mindie-motor && bash start_motor.sh`）；从其他目录 `bash /opt/mindie-motor/start_motor.sh` 会找不到配置文件
- `register.sh` 要在 coordinator 启动**之后**执行：先 `bash start_motor.sh`，探活与注册命令都按**实际绑定地址**生成——从 user_config.json 读 `coordinator_api_host`（配置只绑实际网卡 IP 时 `127.0.0.1` 不可达，同机探活也会失败），用 `curl -s http://<coordinator_api_host>:<coordinator_api_mgmt_port>/liveness` 与 `curl -s http://<coordinator_api_host>:<coordinator_api_mgmt_port>/readiness` 确认管理端口就绪，register.sh 的 `--coordinator` 与 `--mgmt-port` 同步取该地址与配置中的管理端口，再**前台**执行 `bash register.sh` 看清每个实例的注册输出
- **注册前做后端网络预检（必须在 coordinator 所在机器/容器内执行，不是宿主机）**：`curl -m 5 http://<backend_ip>:<port>/v1/models` 逐个确认可达。register.sh 的健康检查通过**不代表** coordinator 进程可达后端——register 与 coordinator 可能处于不同网络命名空间，宿主机可达 ≠ coordinator 容器内可达；跨容器端口未互通时，注册显示 healthy 但推理请求会 500
- 生成后必须在远端做语法级自检：`python3 -m json.tool user_config.json` + **逐个脚本** `bash -n register.sh`、`bash -n start_motor.sh`——不能用 `bash -n *.sh`（通配符展开后只检查第一个脚本，其余文件名成为该脚本的参数），任一失败即停止部署，全部通过后再进入阶段 5

## 阶段 5：输出部署指南

在用户指定目录（或安装路径）生成 `MindIE_Motor_部署指南.md`，包含：

1. 本次部署的信息摘要（机器、安装方式 A/B、coordinator 绑定地址与三个端口、prefill/decode 实例列表、模型名、调度策略）；**跨机部署必检项：管理面 API Key 已启用**（`mgmt_api_key_config.enable_api_key: true`、密钥文件存在且权限 600、register 命令带 `--mgmt-api-key-file`）——绑定地址非回环而未开启鉴权时 start_motor.sh 门禁会直接拒绝启动；同机回环部署（127.0.0.1）标注"仅本机监听，管理面未对外暴露"
2. **部署形态说明**：MindIE Motor 为独立部署的 coordinator，不管理 vLLM/MindIE 实例的拉起——后端实例需由用户按原有方式先行启动，再通过 register.sh 注册
3. 启动步骤：执行 `bash start_motor.sh`（脚本内置 `mkdir -p ./log` 与 PID 落盘），通过 `tail -f ./log/motor_coordinator.log` 与 `curl -s http://<coordinator_api_host>:<coordinator_api_mgmt_port>/liveness`、`curl -s http://<coordinator_api_host>:<coordinator_api_mgmt_port>/readiness` 确认管理端口就绪——地址与端口取 user_config.json 实际配置（绑实际网卡 IP 时用该 IP，不要写死 127.0.0.1）
4. 后端网络预检：在 coordinator 所在机器/容器内 `curl -m 5 http://<backend_ip>:<port>/v1/models` 逐个确认后端可达——这是端到端推理的前置条件，宿主机可达不代表 coordinator 容器内可达
5. 后端注册：前台执行 `bash register.sh`（输出同时 `tee` 到 `./log/register.log`），确认每个实例注册成功、`readiness` 返回 `"ready":true`——同一实例的多个 DP endpoint 在一个 `--prefill`/`--decode` 内逗号连接，多实例才重复传参；开启管理面 API Key 时命令需带 `--mgmt-api-key-file`
6. 验证方法：向 `http://<coordinator_ip>:<infer_port>`（推理端口）发一条测试请求（如 `/v1/chat/completions`）确认端到端连通；若返回 `No instance available`，按常见问题第 1 条排查
7. 常见问题（以下问题都要写进指南）：
   - **推理请求 HTTP 500 `{"detail":"No instance available for role PDRole.ROLE_P"}`，coordinator 日志出现 `All connection attempts failed` / `Connection refused`**：后端在 coordinator 所在网络不可达（典型为跨容器/网络命名空间端口未互通），**不是配置字段错误**。处理：在 coordinator 容器内 `curl http://<backend>:<port>/v1/models` 复核；将后端端口发布到 coordinator 所在网络，或改用容器网内可达地址；修复后复测端到端推理。注意 register.sh 健康检查通过不能证明 coordinator 可达后端
   - **端口占用**：`ss -ltn | grep -E ':(<三个端口>)\b'` 检查；被占用则整套换端口（如 7300/7301/7302），并同步 user_config.json 与 register.sh 的 `--mgmt-port`
   - **误启动占用 1025/1026/1027**（如误用 `--help`）：`ps -ef | grep motor.coordinator.main` 找到 PID 后 `kill <PID>`，勿用 `pkill -f`（会误杀当前 shell）
   - **离线依赖缺失**：`ModuleNotFoundError`（fastapi/pydantic/zmq/msgspec/OpenSSL/watchdog 等任意模块）→ whl 不带依赖声明，按上游 `requirements.txt` 全量补装（离线走 standalone.md 附录 A 的 pip download 方式），**不要只补报错的那一个模块**
   - **管理端口裸暴露**：`coordinator_api_host` 绑非回环地址（实际 IP / `0.0.0.0`）或 mgmt 端口可被本机以外访问而未开启管理面 API Key 时，任何可达主体都能操作管理接口（注册自有后端劫持推理流量 / 注销真实后端致推理 500）。模板默认绑 `127.0.0.1` 规避此问题；确需跨机时按上游 standalone.md 附录 E 配置 `mgmt_api_key_config`（生成密钥文件 + `enable_api_key: true` + `api_key_file`，权限 600/目录 700），register 命令追加 `--mgmt-api-key-file`。start_motor.sh 已内置"非回环 ⇒ 鉴权必须开启"门禁：若手动改绑后未开鉴权，启动被拒绝并给出二选一提示（改回 127.0.0.1 / 补齐鉴权），按提示处理后重试
   - **coordinator 启动报找不到 `libmindie_workload_shm.so` / 装的是 1.2MB 左右的小 whl**：装到了 `pip3 wheel .` 产出的空壳包（未含 Rust 原生库，coordinator 无 Python 兜底）。处理：`pip3 show motor` 确认后卸载，改用 `bash build.sh` 重新编译（含第 5 步原生库断言），重新安装；远端无 cargo 时按"cargo 离线安装"或预编译产物直通处理
   - **`import motor` 成功但路径指向源码目录**：在源码目录内执行 import 会命中源码而非 site-packages，验证安装结果必须 `cd` 到源码目录之外再执行
   - **readiness 返回 `{"status":"ok","ready":false}`**：coordinator 已存活但还没有后端注册——启动后、register.sh 执行前的**正常状态**，不是故障（liveness ok + readiness not ready = 等 register）；注册成功后才应变为 `"ready":true`
   - **重装/重启后"行为没变"**：两种常见原因——① 旧 coordinator 进程没停（按阶段 3 停服纪律同口径执行：读出 PID 文件后先 `ps -p <PID> -o args=` 核对命令行确为 `python3 -m motor.coordinator.main` 且归属本部署实例，核对通过才 `kill <PID>` 并等待其退出，再装再启——PID 文件中的 PID 可能已退出并被复用，直接按文件盲 kill 或 `kill -9` 会误停无关服务）；② 重启后忘记重新执行 register.sh（注册表是内存态，重启即清空）
   - **容器化部署的易失性**：whl 装在容器内 site-packages，容器重建即丢失——持久化要么固化进镜像，要么保留离线安装包与部署流程（方式 A/B 手册）随时可重建；把"容器重建后如何快速恢复"写进部署指南
   - **代理变量未清理导致注册失败**：start_motor.sh 与 register.sh 模板均已内置 unset 代理；手动执行命令（如直接 curl/pip）时需自查

指南中的命令必须与实际生成的文件路径和端口一致，不要写通用化占位路径。

## 模板

- `references/templates/user_config.json` — coordinator 配置模板（v2.0）
- `references/templates/register.sh` — 实例注册脚本模板
- `references/templates/start_motor.sh` — coordinator 启动脚本模板
- `references/field-playbook-offline-build.md` — 方式 B 离线编译实测手册（Rust 离线安装/分片上传/vendor libc/proto 版本匹配/build 参数与验证，ISS-09~ISS-11 实测记录）
