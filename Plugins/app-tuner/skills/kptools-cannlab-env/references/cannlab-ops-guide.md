# cannlab 操作指南 — 连接、执行与文件生命周期

> SKILL.md 是速查；本文是逐步详解。命令层标注：**本机** = Windows PowerShell，
> **CPU** = dev 容器 shell，**NPU** = NPU Pod shell。

## 1. 链路与角色

```
本机 Windows ──ssh──▶ CPU 容器(dev env) ──remote_tool.sh──▶ NPU Pod(Ascend)
   交互入口              持久层(/mnt/workspace)         算力租用层(临时)
```

| 层 | 生命周期 | 角色 |
|----|---------|------|
| 本机 | - | 用户交互、写代码、收最终产物 |
| CPU 容器 | 持久 | 代码/数据/产出的**唯一可信存储**；remote_tool.sh 的运行位置 |
| NPU Pod | 临时（自动关机+文件清理） | 只当"算力租用点"；产出即拉即走 |

## 2. 连接参数从哪来

- **ssh 别名**：本机 `~/.ssh/config` 中的 Host 条目（devenv 隧道别名，
  user=developer）。示例形态：`devenvc_<xx>_cpu.<cpu-id>.atomgit.0`
  ——别名与 devenv 实例绑定，跨机不可复用
- **npu-id / cpu-id**：本机 cannlab 配置文档中的实例标识，
  供 `devenv_api.py ensure-on` 直连开机用
- **token**：devenv 平台预置于 CPU 容器（路径见平台文档，不要读出内容）
- **API**：`DEVENV_API_BASE=https://dev.gitcode.com/api/v1/proxy`

## 3. CPU 容器操作（本机 → CPU）

```powershell
ssh -o BatchMode=yes <devenv-alias> '<命令>'
```

引号规则（违反必出诡异 bug，详见 pitfalls #1-#3）：

- 远程命令**值**一律单引号；外层 PowerShell 双引号包整条
- 远程命令本身含单引号：`'\''` 转义，或转脚本文件
- **复杂脚本铁律**：本地写 → scp → 远端 `bash xxx.sh`，永远不要内联

### 文件传输

```powershell
scp -o BatchMode=yes "<本机路径>" "<devenv-alias>:/mnt/workspace/<路径>"    # 上传
scp -o BatchMode=yes -r "<devenv-alias>:/mnt/workspace/<目录>" "<本机路径>" # 下载目录
```

- scp 不保留可执行位与 mtime 语义差异，`.sh` 上去后 `chmod +x`
- 行尾：本地工具写出的是纯 LF，直接可用；**不要**做 CRLF 转换（pitfalls #4）

## 4. remote_tool.sh（CPU → NPU）

使用前（非交互 shell 不加载 .bashrc）：

```bash
export DEVENV_API_BASE=https://dev.gitcode.com/api/v1/proxy
```

### 4.1 exec —— 主力子命令

```bash
remote_tool.sh exec -v \
  --command 'cd /mnt/workspace && bash run.sh' \
  --pull-dir '/mnt/workspace/output' \
  [-w /mnt/workspace] [-e 'K=V,K2=V2'] [--no-sync]
```

一次调用内发生的完整序列：

```
[STAGE_开机判断] STOPPED → 自动 POST 开机并轮询（首次 2-5 分钟）
[STAGE_SSH检测] 就绪等待
[STAGE_正向同步] CPU /mnt/workspace、~/Ascend、~/.bashrc → NPU（rsync --delete!）
[STAGE_远端执行] ssh 到 NPU 执行 --command
[STAGE_拉回]    --pull-dir 指定的目录按同名路径拉回 CPU
```

- `--no-sync`：跳过正向同步（轮询长任务时必用，避免删掉任务正在写的文件）
- `-e`：注入环境变量
- 输出量超大时加 `-v` 看 `[STAGE_*]` 定位卡在哪一段

### 4.2 其他子命令

| 命令 | 场景 |
|------|------|
| `pull '<dir1>,<dir2>'` | 只要文件不要执行（补拉产出） |
| `sync` | 只推文件（如更新代码后） |
| `check` | 查 SSH 通不通，**不触发开机** |
| `config` | 打印 HOST/PORT/USER/IDENTITY，**不触发开机** |
| `shutdown` | 任务收尾主动关机（省资源） |

### 4.3 直接开机（预热）

```bash
python3 <devenv-api.py 平台预置路径> ensure-on \
  --npu-id <npu-id> --cpu-id <cpu-id> \
  --token $(cat <token-file>) \
  --api-base https://dev.gitcode.com/api/v1/proxy
```

- 输出 `pod_ip=... boot_triggered=true` 表示成功；实测 40s~5min
- 不配公钥/不同步/不写缓存——都由后续 exec 自动补齐
- 开机后用 `remote_tool.sh check` 确认 SSH 就绪
- 家族：`start`（只发请求）/ `npu-status`（只查）/ `wait-running`（只等）

## 5. 长任务模式（超过 exec 超时的任务）

```bash
# 1) 启动（CPU 上）
remote_tool.sh exec --command 'cd /mnt/workspace && nohup python3 train.py > train.log 2>&1 & echo started'

# 2) 轮询（不刷执行记录、不同步、保活）
remote_tool.sh exec --no-sync --command 'tail -20 /mnt/workspace/train.log; pgrep -f train.py'

# 3) 结束后回收
remote_tool.sh pull '/mnt/workspace/train.log,/mnt/workspace/output'
```

要点：

- 启动后立即返回（nohup + &），长跑交给后台
- 轮询一律 `--no-sync`——正向同步会 unlink 任务正在写的日志
- NPU 有"最后使用时间"探活（`/tmp/npu_last_use_time`），纯 sleep 的长间隔
  轮询本身也是保活；间隔过长（小时级）需注意可能被自动关机

## 6. 文件生命周期（最容易翻车的部分）

```
本机 ──scp──▶ CPU /mnt/workspace ──rsync --delete──▶ NPU /mnt/workspace
                    ▲                                   │
                    └──────── pull（同名路径）◀──────────┘
```

**每次带同步的 exec 都会把 NPU 上"CPU 没有的"文件删掉**。实测受害模式
（pitfalls #7）：pip --target 装的依赖、构建产物、clone 的仓库、日志、
缓存——下一次 exec 整目录消失。

纪律清单：

1. NPU 上每产生一个新目录/重要文件，**立刻** `remote_tool.sh pull` 回 CPU
2. pull 完成后才允许做下一次带同步的 exec
3. 反向同理：NPU 上有任务在跑时不要做同步 exec
4. NPU 上放 /mnt/workspace 之外（如 /tmp）也逃不过定时清理——没有"安全区"

## 7. NPU 环境配方详解

```bash
# 标准 Python 环境（exec 时自动 source /etc/profile 和 ~/.bashrc）
source ~/Ascend/ascend-toolkit/set_env.sh
TILE_FWK_DEVICE_ID=0
# 构建版解释器（镜像预置，路径见环境文档；非系统 python）：
python3.12
```

- set_env.sh 会自动把 CANN 的 python/site-packages 追加到 PYTHONPATH 尾部
- CPU 层预置的 pyfix312 兼容目录 **只能**给 CPU 容器的系统 python 3.12 用；
  **绝不能**进 NPU 构建版 python 的 PYTHONPATH（shadow stdlib C 扩展 →
  SIGSEGV rc=139，见 pitfalls #5——两层语义相反，先确认目标层再写配方）
- 网络与权限：NPU 有公网（pypi 镜像/ github 可达）；github 不稳定，clone/
  下载必须 `for i in 1..N` 重试 + `curl -fL --retry 3 -C -` 断点续传
- 磁盘/内存：/dev/shm 默认 64MB，大模型权重 staging 需
  `sudo -n mount -o remount,size=40G /dev/shm`（Pod 重启失效，写进环境脚本）
- 查看设备：`npu-smi info`（NPU 编号与逻辑 device 可能不同，注意映射）

## 8. 超时与重试（调用方纪律）

| 场景 | bash 工具超时 |
|------|--------------|
| 纯 CPU 命令 | 60s |
| exec/pull（NPU 已开机） | 120s |
| exec 涉及开机 | ≥480s（8 分钟） |

- 失败先对照退出码表（SKILL.md），按对应策略处理，**不要盲目重试超过 2 次**
- 退出码 2/3：重试一次；4：查路径；5：停下告知用户；6：查引号；
  8：等 1-2 分钟；10：换思路排查

## 9. 收尾与报告

1. 任务全部完成：`remote_tool.sh shutdown`（CPU 容器不关）
2. 报告必须注明：命令在本机/CPU/NPU 哪一层执行、产物最终落在哪
   （CPU 路径 + 本机路径）
3. 发现环境新机制/新坑 → 更新本 skill 的 pitfalls.md（经验沉淀闭环）
