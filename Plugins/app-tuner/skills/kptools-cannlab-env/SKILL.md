---
name: kptools-cannlab-env
description: >
  连接并操作 cannlab 分设环境，用于远程调试代码：本机（Windows/SSH 客户端）
  → CPU dev 容器（持久层，代码与产出的最终保存地）→ 昇腾 NPU Pod（算力
  租用层，自动关机/文件清理/IP 不固定）。覆盖：SSH 命令模板与 Windows
  引号规则、scp 文件传输、remote_tool.sh 全子命令（exec/pull/sync/check/
  config/shutdown）、NPU 开机预热（ensure-on API）、退出码契约（0/2/3/4/5/
  6/7/8/10）、rsync --delete 同步纪律与文件生命周期、长任务 nohup 轮询模式、
  NPU 环境配方（set_env/构建 python/pyfix312 禁令）、以及引号被吞/pyfix312
  边界/pip 陷阱/rsync 受害模式/共享内存与 AICPU 目录缺失等实战踩坑。
  当用户需要在 cannlab/分设环境上远程执行代码、调试代码、同步/上传文件、
  上板、真机验证、拉回结果、NPU Pod 开机关机、排查远程连接或同步问题、
  问 remote_tool.sh 怎么用、NPU 上跑测试或构建、CPU 容器上查文件时触发。
license: MulanPSL-2.0
compatibility: 已配置 cannlab 分设环境的本机（ssh config 内有 devenv 别名；认证 token 由 devenv 平台预置于 CPU 容器，路径见平台文档）；本 skill 不自带脚本，全部复用环境自带的 ssh/scp/remote_tool.sh
metadata:
  skill_type: tool_assisted
  category: environment
  domain: cannlab_remote_ops
  version: "1.1.0"
---

# kptools-cannlab-env — cannlab 分设环境连接与远程调试

cannlab 是三级链路的分设环境。本 skill 是操作它的完整手册：在哪层跑什么、
怎么传文件、怎么处理失败——**所有操作围绕一条铁律展开：CPU 容器是唯一可信存储，NPU 只是算力租用点**。

## 环境拓扑（必读）

```
本机 Windows (PowerShell)
    │  ssh 别名: <devenv-alias>          ← 本机 ~/.ssh/config 中的 Host 条目
    │  （经 VS Code 隧道，user=developer，aarch64 Linux）
    ▼
CPU 容器 (dev env)
    │  持久层：代码/数据/产出物一律以 /mnt/workspace 为准
    │  remote_tool.sh（先 export DEVENV_API_BASE，见下）
    ▼
NPU Pod (Ascend，会自动关机，文件会被清理，IP 不固定)
```

三条纪律直接来自拓扑：

1. **禁止直连 NPU IP**——IP 每次开机都变，`~/npu_info.cache` 只可读不可信，
   一律走 remote_tool.sh
2. **NPU 是临时层**——空闲自动关机、文件定时清理；产出物当次 `--pull-dir`
   拉回 CPU，不要指望下次还在
3. **CPU 是持久层**——任何要保留的东西最终落到 CPU `/mnt/workspace`；
   用户要的结果再 scp 回本机

## 工作流程（标准闭环）

```
1 准备：代码/脚本放 CPU /mnt/workspace（本机写的先 scp 上去）
2 执行：remote_tool.sh exec --command '...' --pull-dir '<产出目录>'
3 回收：确认产出已落盘 CPU；用户需要时 scp 回本机
4 收尾：不需要 NPU 时 shutdown；报告时注明命令在哪层执行（本机/CPU/NPU）
```

## 操作命令

### CPU 容器（任何命令都套这个模板）

```powershell
ssh -o BatchMode=yes <devenv-alias> '<命令>'
```

⚠️ **Windows 引号大坑（最重要的一条）**：PowerShell 传给 ssh 的远程命令中
**双引号会被吞掉**（远程收到时已丢失，参数解析错乱）；且 PowerShell
**双引号字符串会本地插值**（`$(...)`、`$?`、`$HOME` 在本地被求值后才发
出，见 pitfalls #2）。规则（一条铁律 + 三个推论）：

1. 外层 PowerShell **一律用单引号**包裹整条 ssh 命令；远程命令内部需要
   shell 引号的值也一律用单引号
2. **外层禁止用双引号**——双引号触发本地插值（`$(...)`、`$?`、`$HOME`
   都会在本地被求值）。需要**远端求值**的 `$`（如 ensure-on 的
   `$(cat <token-file>)`）保持整条命令在外层单引号里即可安全到达远端；
   需要**字面 `$`** 或嵌套复杂内容时走脚本文件（见 pitfalls #2）
3. 单引号串中需要嵌入单引号时，用 PowerShell 的 `''` 双写转义
   （**不是** POSIX bash 的"反斜杠续接三引号"写法——那在 PowerShell 中
   会解析成反斜杠 + 悬空引号，直接语法错误）
4. **复杂脚本不要内联**：本地写文件 → `scp` 上去 → `bash xxx.sh`。

### 文件传输（CPU ↔ 本机）

```powershell
scp -o BatchMode=yes "<devenv-alias>:/mnt/workspace/<路径>" "<本机路径>"   # CPU → 本机
scp -o BatchMode=yes "<本机路径>" "<devenv-alias>:/mnt/workspace/<路径>"   # 本机 → CPU
# 目录加 -r；scp 不保留可执行位，上去后 chmod +x
```

### NPU Pod（在 CPU 容器上执行，必须先 export）

```bash
export DEVENV_API_BASE=https://dev.gitcode.com/api/v1/proxy   # token 由平台预置（路径见 devenv 环境文档），不写入本文档
remote_tool.sh <子命令>
```

| 子命令 | 用途 |
|--------|------|
| `exec --command '<cmd>' [--pull-dir '<dir1>,<dir2>'] [--no-sync] [-w <workdir>] [-e 'K=V,K2=V2']` | **主力**：开机→SSH检测→正向同步→NPU执行→拉回结果，一条龙 |
| `pull '<dir1>,<dir2>'` | 仅把 NPU 上的文件/目录拉回 CPU（同名路径落盘） |
| `sync` | 仅正向同步 CPU→NPU |
| `check` | 检测 SSH 连通性（**不触发开机**） |
| `config` | 输出连接参数 HOST/PORT/USER/IDENTITY（**不触发开机**） |
| `shutdown` | 主动关机 NPU |

完整示例（在 CPU 容器上；NPU 层用构建版解释器 `python3.12`，非系统 python）：

```bash
remote_tool.sh exec -v --command 'cd /mnt/workspace && python3.12 run.py' \
  --pull-dir '/mnt/workspace/output'
```

### 单独开机（预热，不执行任务）

```bash
python3 <devenv_api.py 平台预置路径> ensure-on \
  --npu-id <npu-id> --cpu-id <cpu-id> \
  --token $(cat <token-file>) \
  --api-base https://dev.gitcode.com/api/v1/proxy
```

- `devenv_api.py`（ensure-on 的实现）与 token 文件均为 devenv 平台预置，
  具体路径见平台环境文档——本 skill 不硬编码机器内路径
- **凭据暴露提示**：`--token <值>` 形式（`$(cat ...)` 展开后）会把令牌
  写进 python 进程的命令行参数——同 Pod 的其他账号经 `ps` 能看到明文。
  平台工具若支持文件/标准输入传递（如 `--token-file`）优先之；只支持
  `--token` 时单账号 Pod 可接受，多账号环境建议跳过 ensure-on，直接用
  `remote_tool.sh exec` 触发开机（其开机链路自行读取预置凭据，不经 argv）
- `ensure-on`：开机 → 轮询直到 RUNNING（约 40s~5min）→ 输出 `pod_ip` 与
  `boot_triggered=true`；**不配置公钥、不做同步、不写缓存**——后续任何
  `remote_tool.sh exec` 会自动补齐
- 附属：`start`（仅发开机请求不轮询）/ `npu-status` / `wait-running`（只查/只等）
- 整条命令放在 PowerShell **单引号**里传给 ssh，`$(cat <token-file>)` 类
  远端求值由远端 bash 完成（单引号内不会被本地插值）

## 退出码契约

remote_tool.sh / exec 的退出码是排障的第一入口，**先看码再决定动作**：

| 码 | 含义 | 处理 |
|----|------|------|
| 0 | 成功 | - |
| 2 | 服务端异常（取IP/开机失败） | 重试一次；仍失败则报错 |
| 3 | SSH 连不上 | 重试；刚开机时 SSH 需就绪时间 |
| 4 | 同步/拉取失败 | 检查路径是否存在；文件被清理则重建 |
| 5 | 配置错误（token） | 停下来告知用户 |
| 6 | 参数错误 | 多半是引号被吞，检查引号规则 |
| 7 | 远端命令执行失败 | 看命令自身输出排错 |
| 8 | NPU 正在关机中（**非错误**） | 等 1-2 分钟重试即可 |
| 10 | 未知 | 重试或排查 |

## 关键机制与纪律

1. **自动开机**：NPU 处于 STOPPED 时，exec/pull/sync 自动触发开机并轮询等待，
   **首次开机可能 2-5 分钟**，调用务必设长超时（≥8 分钟）；`-v` 可看
   `[STAGE_*]` 阶段输出
2. **正向同步是破坏性的（rsync --delete）**：每次 exec/sync 把 CPU 的
   `/mnt/workspace/`、`~/Ascend/`、`~/.bashrc` 强制覆盖到 NPU，**NPU 上多出来
   的文件会被删掉**。因此：
   - NPU 上产生的产出物**当次就用 `--pull-dir` 拉回 CPU**，不要指望下次还在
   - NPU 上临时文件放 /mnt/workspace 之外也逃不过定时清理
   - NPU 上有任务在跑时**不要做同步 exec**（正在写的日志会被 unlink）
3. **长任务模式**：`nohup ... > log 2>&1 &` 后台启动并**保存 PID 与退出码
   文件**（`echo $! > run.pid` / 内层 `echo $? > run.exit`——按进程名
   `pgrep -f` 模糊匹配无法区分本次任务与同机其他训练，也不区分成功与
   崩溃）→ 用 `--no-sync` 的 exec 轮询日志/PID 存活/退出码 → 按
   `run.exit` 判定成功后回收产出。注意 NPU 有"最后使用时间"探活，长
   时间无活动可能被自动关机
4. **探活/查活**：`remote_tool.sh exec --no-sync --command 'cat /tmp/npu_last_use_time'`
   ——保活且不刷执行记录
5. **节约资源**：任务全部完成后主动 `remote_tool.sh shutdown`（CPU 容器不用关）

## NPU 环境配方（速查）

- 硬件：Ascend910（a3 系列双卡，各 64GB HBM；`npu-smi info` 查看）
- CANN：9.2.0（用户目录下 `~/Ascend/`（CANN 官方惯例路径），从 CPU 同步）
- Python：镜像预置的构建版 Python 3.12（**非系统 python**，路径见环境
  文档；自带 torch/torch_npu CPU 版、safetensors、pydantic、httpx、tokenizers）
- 标准环境：`source ~/Ascend/ascend-toolkit/set_env.sh; export TILE_FWK_DEVICE_ID=0`
  （**必须 export**——单独赋值只对当前 shell 内置命令生效，传不到下一行
  启动的 python 进程）
- ⚠️ **绝不要把 CPU 层预置的 pyfix312 兼容目录加进 NPU 构建版 python 的
  PYTHONPATH**——它会 shadow 标准库 C 扩展，pypto/torch/numpy 全部
  SIGSEGV（rc=139）。pyfix312 只给 **CPU 容器的系统 python 3.12** 用
  （两层语义相反，详见 pitfalls #5）
- NPU 有公网：pypi 镜像可达；github 可达但不稳定，clone/下载必须带重试循环
- 无 root 写系统路径权限；sudo 仅对个别命令免密（mount、mkdir 等），
  用 `sudo -n` 试探
- 默认工作目录 `/mnt/workspace`（devenv 平台的同步约定，以实际环境为准）

## 约束

- 本 skill 只管环境连接与远程执行；**在环境上跑什么**（采集/测试/构建）由
  具体任务 skill 决定
- ssh 别名、npu-id/cpu-id 是本机配置绑定的：别名在 `~/.ssh/config`，
  id 见本机 cannlab 配置文档；跨机复用时先核对
- token/私钥等凭据不进任何文档：认证 token 由 devenv 平台预置于 CPU 容器
  （路径见平台文档），命令中以 `$(cat <token-file>)` 形式引用；具体路径
  属于环境配置信息，本文档只引导方法不落机器细节
- 本地 write 工具写出的文件是纯 LF，scp 到 Linux 直接可用，**不需要**任何
  CRLF 转换（画蛇添足反而会出事故，见 pitfalls #4）

### 和同类 skill 的分界

- 环境上执行的**内容**：上板性能与 profiling 分析归
  `kptools-ascend-profiling-analysis`，NPU 设备状态采集归
  `kptools-ascend-smi`，系统级采集归 `kptools-os-collector`（均在本仓）；
  训练指标解析无仓内对应 skill 时由所在部署环境的指标工具承接——
  本 skill 只提供链路与操作纪律
- openEuler/AgentENV 沙箱环境连接排障由 `kptools-openeuler-agentenv` 覆盖；
  本 skill 专指 cannlab 分设环境（CPU 容器 + 昇腾 NPU Pod 链路）
- 远程环境上的只读采集（BIOS/OS/网络等）由各 kptools-* collector 在目标层
  执行；本 skill 是它们跑在 NPU 层时的承载层

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| exec 偶发静默无输出（连 `echo MARKER` 都没有） | 多为引号被吞或偶发失败。排查法：输出重定向到 NPU 上的日志文件再 `cat`，用成对 `echo A/B/C` 标记定位断点 |
| 远端命令含 `\|`/`(`/`$` 后行为诡异 | PowerShell 本地插值或引号吞掉：`$(...)`、`$?`、`$HOME` 都会在本地被求值。走脚本文件上传执行 |
| 退出码 6 | 参数错误，九成是引号规则违反 |
| 退出码 8 | NPU 关机中，非错误，等 1-2 分钟重试 |
| `grep -c`/管道在远端输出为空 | 检查双引号是否被吞（经典症状：模式里的 `\|` 被当管道、含空格模式被拆散） |
| scp 上去的 .sh 报 not executable | scp 不保留权限位：`chmod +x` 后再跑 |
| 刚 `ensure-on` 完 SSH 连不上 | SSH 就绪需要时间，用 `remote_tool.sh check` 轮询确认 |
| 下次上 NPU 文件没了 | rsync --delete 清掉了（CPU 没有同名路径）——产出当次 pull 回，纪律见上 |

## 参考资料

- [cannlab 操作指南](references/cannlab-ops-guide.md) — 开机/同步/长任务/文件生命周期/收尾的逐步详解
- [踩坑实录](references/pitfalls.md) — 五组 15 条实战坑：引号与命令传递/文件与脚本/Python 环境/同步与文件生命周期/NPU Pod 系统层（含每条的"症状→根因→解法"）
