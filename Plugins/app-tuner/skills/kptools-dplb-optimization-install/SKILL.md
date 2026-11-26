---
name: kptools-dplb-optimization-install
description: >-
  MindIE Motor 软件本体的安装/编译/升级/重装：方式 A 自备 whl 直装（空壳 whl 拦截）、
  方式 B 源码编译（官方入口 bash build.sh，严禁 pip3 wheel 空壳包）、cargo 离线安装、
  运行依赖按上游 requirements.txt 全量补装（whl 无 Requires-Dist）、重装前停旧 coordinator
  停服纪律。当用户提到以下场景时使用本技能：装 motor / 编译 motor / 出 motor whl / 升级
  重装 MindIE Motor、cargo 离线编译、空壳 whl（缺 libmindie_workload_shm.so）、
  ModuleNotFoundError 补依赖。本 skill 只做软件安装，交付终点 = 源码目录外 import motor
  成功 + pip3 show motor；user_config.json / register.sh / start_motor.sh 配置三件套、
  启动/注册/LB 策略调整归 kptools-dplb-optimization（配套 PR #443 交付）（装好后转它续接，配置启动类请求不要
  发给本 skill）；负载不均衡现象的分析归 kptools-dplb-profiler。
license: Proprietary
compatibility: >
  远端 Linux（openEuler/EulerOS 等，aarch64/x86_64）；需要 SSH 访问远端机器；远端需有
  Python >= 3.11 + pip（motor 3.x whl 元数据 Requires-Python >=3.11）；运行依赖按上游
  requirements.txt 全量安装（whl 无 Requires-Dist，pip 不会自动安装任何依赖，见依赖清单）；
  方式 B（源码编译）还需 git、gcc、grpcio-tools（版本须与运行时 grpcio 一致）及 Rust
  工具链（cargo，离线安装见 references/field-playbook-offline-build.md）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: mindie_motor_install
  version: "0.1.0"
---

# kptools-dplb-optimization-install：MindIE Motor 安装（whl 直装 / 源码编译）

安装 MindIE Motor 软件本体：方式 A（用户自备 whl 直装）或方式 B（拉源码用官方入口 `bash build.sh` 编译再安装），含运行依赖全量补装、空壳 whl 拦截、cargo 离线安装与重装停服纪律。**本 skill 的交付终点 = 源码目录之外 `import motor` 成功 + `pip3 show motor` 可查**；配置三件套、启动/注册、LB 策略调整由 `kptools-dplb-optimization`（配套 PR #443 交付） 承接（装好后转它续接，或由 deploy 主流程在安装前置确认后调用本 skill）。

## 快速决策

1. 用户给了 whl 包 → 走方式 A（直装 + 空壳拦截）
2. 用户要源码编译 / 内网无外网 → 走方式 B（bash build.sh，严禁 `pip3 wheel .`），无 cargo 时按 cargo 离线手册或预编译产物直通
3. 升级/重装 → **先停旧 coordinator**（停服纪律见下）再装；装好后提醒用户：必须回 `kptools-dplb-optimization`（配套 PR #443 交付） 重新生成配置并重新执行 register.sh（注册表是内存态，重启即清空）
4. 用户要的是配置三件套 / 启动 / 注册 / 改 LB 策略 → 转 `kptools-dplb-optimization`（配套 PR #443 交付）（其阶段 3 做安装前置确认，本 skill 只管软件本体）
5. 用户只描述负载不均衡现象 → 先确认意图：要部署负载均衡组件走 `kptools-dplb-optimization`（配套 PR #443 交付）（主入口，未装时它转本 skill），只要采集/分析转 `kptools-dplb-profiler`

## 阶段 1：环境检查（SSH 连通后先做，再动手装）

```
# 在远端依次确认，任一缺失先解决再继续：
python3 --version && pip3 --version   # Python >= 3.11（motor 3.x whl 元数据 Requires-Python >=3.11）
df -h <安装路径>                      # 磁盘余量（离线编译还需看 /tmp /usr/local：离线包 ~345MB + 两份副本 + cargo target）

# 运行依赖预检（whl 未声明 Requires-Dist，完整清单以上游 requirements.txt 为准；
# 此处探 coordinator 硬 import 的代表性模块）：
python3 -c "import OpenSSL, watchdog, grpc, fastapi, pydantic, zmq, msgspec, uvicorn" 2>/dev/null && echo DEPS_OK || echo DEPS_MISSING
# DEPS_MISSING 时按方式 A 第 4 步补装：在线 pip3 install -r requirements.txt；
# 离线按上游 standalone.md 附录 A（本地 pip download -r requirements.txt 全量下载后上传安装）

# 仅当选择方式 B（拉源码编译）时才需要检查：
git --version
gcc --version                        # 编译 whl 需要（上游 build.sh/Makefile/setup.py 均不用 cmake，预检无需 cmake）
cargo --version                      # Rust 工具链：编译 libmindie_workload_shm.so（coordinator 必需，无 Python 兜底）
pip3 show grpcio-tools >/dev/null && echo GRPCIO_TOOLS_OK || echo GRPCIO_TOOLS_MISSING
python3 -c "import grpc; print('runtime grpcio:', grpc.__version__)"   # 决定 grpcio-tools 必须匹配的版本

# 外网连通性（方式 B 必做；方式 A 在线补依赖时同样适用）：
curl -m 5 -sI https://gitcode.com >/dev/null && echo OK || echo UNREACHABLE   # 需要 pip 装依赖时同样先测 pypi.org
```

- 编译环境缺失时，先与用户确认是否允许 `yum/dnf install` 安装依赖，不要静默装系统包
- 检查是否已有旧版本：`pip3 show motor`（**包名是 `motor` 不是 `mindie-motor`**——motor 3.x whl 元数据 `Name: motor`，用错名字永远查不到），有则询问覆盖还是并存；覆盖（升级/重装）先按下方停服纪律停旧
  - **同名异包防误判**：PyPI 的 MongoDB 异步驱动同样叫 `motor`（`pip show motor` 与 `import motor` 均命中）——发现已有 `motor` 时，先核对 **MindIE 专属特征**（`python3 -c "import motor.coordinator"` 成功且 `motor/__file__` 同级有 `libmindie_workload_shm.so`）再判定是旧 MindIE Motor；仅命中 `pip show motor` 但无 coordinator 模块即为 MongoDB 驱动（其他应用的依赖），**禁止覆盖安装**——改用独立虚拟环境（`python3 -m venv /opt/motor-venv && source /opt/motor-venv/bin/activate` 后在 venv 内安装本 skill 的 motor），避免破坏其他应用
- 外网不可达时立即反馈用户"远端无法访问外网"，给出替代建议（改用方式 A 上传 whl、依赖 whl 由本地经内网镜像下载后上传、用户提供离线源码包），让用户选择，不要自行切换方案后闷头继续

## 阶段 2：安装（按用户选择的方式二选一）

> **部署形态说明（务必让用户知晓）**：MindIE Motor 采用**独立部署 coordinator** 的方式——它只做负载均衡（请求分发），**不管理 vLLM/MindIE 实例的拉起与生命周期**。后端实例由用户原有方式启动和运维，Motor 通过 register.sh 动态注册感知它们。用户如果以为装了 Motor 会帮忙拉起推理实例，要主动纠正这个预期（配置与注册流程见 `kptools-dplb-optimization`（配套 PR #443 交付））。

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
# 2. 检查运行依赖——用目标解释器核对 requirements.txt 全量（不只探测 8 个硬 import 模块）：
#    八个硬 import 只是快速探测（DEPS_OK/DEPS_MISSING）；**交付验证必须用完整清单核对**——
#    向用户索取/从源码仓库获取对应版本 requirements.txt 后，用目标解释器逐项核对：
python3 -c "import OpenSSL, watchdog, grpc, fastapi, pydantic, zmq, msgspec, uvicorn" 2>/dev/null && echo DEPS_PROBE_OK || echo DEPS_MISSING
#    完整核对（在目标解释器上，版本号以 requirements.txt 为准）：
pip3 install --dry-run -r requirements.txt 2>/dev/null || pip3 install -r requirements.txt
#    上述命令会解析全量依赖并补齐缺失项/版本不满足项（--dry-run 先预览再装，无 --dry-run 直接装）
# 3. 安装前断言 whl 含原生库（用户拿到的"编译好的 whl"也可能是 pip wheel 空壳，同 ISS-09）。
#    **选定一个明确的 whl 文件**（不要 glob——目录含多个 whl 时，校验的是第一个、安装却传全部，
#    冲突时安装可能失败；由用户指定或列出后选唯一匹配），校验和安装使用同一路径：
ls /tmp/mindie-motor-whl/*motor*.whl   # 列出后由用户选定或唯一匹配时自动选
python3 -c "import zipfile,sys; w=sys.argv[1]; names=zipfile.ZipFile(w).namelist(); assert any(n.endswith('libmindie_workload_shm.so') for n in names), '空壳 whl：缺 libmindie_workload_shm.so'; print('native lib OK:', w)" /tmp/mindie-motor-whl/<选定的文件名>.whl
# 4. 安装（依赖已按 requirements.txt 全量核对/补装后，只装 whl 本体）
#    在线环境装依赖：向用户索取/从源码仓库获取对应版本 requirements.txt 后
#      pip3 install -r requirements.txt
#    离线环境按上游 standalone.md 附录 A 全量下载后上传安装。
#    **先采集目标解释器版本/ABI/平台标签，据此生成 pip download 参数**（不要写死 Python 3.11）：
#      目标解释器版本：远端 python3 --version（如 3.12.3）
#      平台标签：     远端 python3 -c "import sysconfig; print(sysconfig.get_platform())"（如 linux-aarch64）
#      ABI 标签：     远端 python3 -c "import sysconfig; print(sysconfig.get_config_var('SOABI'))"（如 cpython-312-aarch64-linux-gnu）
#      本地：pip download -r requirements.txt -d /tmp/offline_pkgs \
#              --python-version <目标版本，如 3.12> --platform manylinux2014_<aarch64|x86_64> --only-binary=:all:
#      上传 /tmp/offline_pkgs 到远端后：pip3 install /tmp/offline_pkgs/*.whl
#    whl 本体安装（--force-reinstall 而非 --upgrade：同版本号换 whl 内容时 --upgrade 不会重装）；
#    **用第 3 步选定的明确文件名安装，不要 glob**（glob 会把多个匹配项都传给 pip，冲突时安装失败）：
pip3 install --no-deps --force-reinstall /tmp/mindie-motor-whl/<第 3 步选定的文件名>.whl
# 5. 验证（3.x 包名为 motor——pip3 show motor；早期 1.x whl 包名可能是 mindie_motor，
#    对应 pip3 show mindie_motor；入口模块 motor.coordinator；在源码目录之外执行）
#    **先确认外部目录存在再切**（/opt/mindie-motor 可能尚未创建——cd 失败后仍可在源码目录
#    导入源码版 motor，导致安装验收误报"成功"）；目录不存在时 mkdir 或换已存在的外部目录
mkdir -p /opt/mindie-motor   # 或用户指定的外部目录
cd /opt/mindie-motor   # 失败即停（set -e），不要回退到源码目录
python3 -c "import motor; print(motor.__file__)"   # 应指向 site-packages/motor/__init__.py
#    核对 motor.__file__ 位于所选解释器的 site-packages（不是源码目录）：
python3 -c "import motor, os; p=motor.__file__; assert 'site-packages' in p, f'安装验收失败：motor.__file__={p} 不在 site-packages（可能命中的是源码目录）'; print('verified:', p)"
pip3 show motor
#    依赖完整性——用目标解释器核对 requirements.txt 全量（不只 8 个硬 import 模块）：
pip3 install --dry-run -r requirements.txt 2>/dev/null || pip3 install -r requirements.txt
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
mkdir -p /opt/mindie-motor   # 确认外部目录存在再切（不存在时 cd 失败、后续仍可在源码目录导入源码造成误报）
cd /opt/mindie-motor   # 或任意已确认存在的源码目录之外位置；失败即停
python3 -c "import motor, os; p=motor.__file__; assert 'site-packages' in p, f'安装验收失败：motor.__file__={p} 不在 site-packages（可能命中的是源码目录）'; print('verified:', p)"
pip3 show motor
#    依赖完整性——用目标解释器核对 requirements.txt 全量（不只 8 个硬 import 模块）：
pip3 install --dry-run -r requirements.txt 2>/dev/null || pip3 install -r requirements.txt
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
- **coordinator 重启后已注册实例清空**（注册表是内存态）：每次 start_motor.sh 之后都要重新执行 register.sh，否则推理请求报 `No instance available`——重启不是幂等的，"注册"必须跟在每次启动后面（配置与注册流程见 `kptools-dplb-optimization`（配套 PR #443 交付））
- **解释器一致性**：pip3 装进哪个 Python（系统 / venv / 容器内自编译解释器），start_motor.sh 里的 `python3` 就必须解析到**同一个**——venv 安装 + 系统 python3 启动会 `ModuleNotFoundError: motor`；安装后 `which python3` 与 `pip3 show motor`（Location 字段）核对两者指向一致

> **严禁用 `--help` 做验证**：`python3 -m motor.coordinator.main --help` 不会打印帮助，而是打印 banner 后**直接启动 Coordinator Daemon**，占用内置默认端口 1025（推理）/ 1026（管理）/ 1027（观测）。验证安装只允许 `pip3 show motor` + `python3 -c "import motor; print(motor.__file__)"`；验证服务可用要等 `kptools-dplb-optimization`（配套 PR #443 交付） 生成 `user_config.json` 并启动后，探活管理端口的 `/liveness`、`/readiness`。若已误启动：`ps -ef | grep motor.coordinator.main` 找到 PID 后 `kill <PID>` 清理，**不要用 `pkill -f motor.coordinator.main`**（会误杀命令行中含该模式的当前 shell）

## 阶段 3：安装记录与交接

1. 产出《安装记录 `.md`》（用户指定目录或安装路径），内容：机器、安装方式 A/B、motor 版本（`pip3 show motor` 输出）、安装路径（site-packages Location）、依赖补装清单（requirements.txt 是否全量过）、验证结果（import motor 路径 + DEPS 探测）、容器化部署标注易失性（容器重建即丢，持久化要么固化进镜像，要么保留离线安装包与本手册随时重建）
2. **交接**：装好后转 `kptools-dplb-optimization`（配套 PR #443 交付） 续接（配置三件套 → 启动 → 注册 → 部署指南）；若本 skill 是被 deploy 主流程调用（deploy 阶段 3 移交），则直接返回其阶段 4 继续。升级/重装场景提醒：必须重新执行 register.sh（注册表是内存态）

## 边界与配套拆分说明

- **本 skill 只做软件安装交付面**（whl 直装/源码编译/依赖补装/空壳拦截/cargo 离线/重装停服纪律）；`kptools-dplb-optimization`（配套 PR #443 交付） 是部署主入口，在其阶段 3 做安装前置确认后移交本 skill，装好后回其阶段 4。两 PR 建议同批合入，避免过渡期安装触发面悬空。
- 本 skill 由原 `kptools-dplb-optimization`（配套 PR #443 交付） v1.0.0 的「安装+部署一体」形态拆分而来：阶段 3 安装（依赖清单、方式 A/B、cargo 离线手册）随拆分迁入本 skill；安装后的配置/启动/注册/策略调整仍在原 skill（其名不变）。
- 相邻边界：负载不均衡现象的量化评估归 `kptools-dplb-profiler` + `kptools-dplb-report`；端到端「部署负载均衡」诉求以 `kptools-dplb-optimization`（配套 PR #443 交付） 为主入口（它转本 skill 做安装），纯安装/编译/升级诉求直接进本 skill。

## 文件

- `references/field-playbook-offline-build.md` — 方式 B 离线编译实测手册（Rust 离线安装/分片上传/vendor libc/proto 版本匹配/build 参数与验证，ISS-09~ISS-11 实测记录）
- `evals/evals.json` — 行为评测集（源码编译 / whl 直装 / 升级停服 / 边界转介）
- `evals/eval_queries.json` — 触发评测集（正负例路由矩阵）
