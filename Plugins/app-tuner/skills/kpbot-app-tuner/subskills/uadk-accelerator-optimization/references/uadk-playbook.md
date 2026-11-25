# UADK Playbook

UADK（User space Accelerator Development Kit）加速器卸载的环境检查、安装部署、建议配置与验证 SOP。事实来源：openEuler 22.03 LTS SP4 官方文档 uadk_quick_start.md（atomgit.com/openeuler/docs，stable-22.03_LTS_SP4/docs/zh/tools/community_tools/uadk/uadk_quick_start.md）。本 playbook 所有安装/驱动/配置命令均为候选动作的素材，skill 分析阶段禁止直接执行（只读探测除外）。

## 1. 环境可行性检查（只读）

| 检查项 | 命令 | 通过标准 |
|---|---|---|
| CPU 型号 | `lscpu` | 鲲鹏（Kunpeng）9xx 系列 |
| OS 版本 | `cat /etc/os-release` | openEuler 22.03 / 24.03 及以上 |
| IOMMU | `ls /sys/kernel/iommu_groups/` | 非空 |
| UACCE 框架 | `ls /sys/class/uacce/` | 存在（驱动已加载） |
| 加速器设备 | `ls -l /dev/hisi_hpre /dev/hisi_sec /dev/hisi_zip` | 按命中算法所需在场 |
| 设备详情 | `cat /sys/class/uacce/<dev>/api`、`flags`、`available_instances` | api 为厂商注册值 |
| libwd | `rpm -q libwd`；`ldconfig -p \| grep libwd` | 已安装 |
| uadk_engine | `rpm -q uadk_engine`；`openssl engine -t uadk_engine` | 初始化成功 |
| 已加载驱动 | `lsmod \| grep -E 'hisi_(hpre\|sec2\|zip\|qm)\|uacce'` | uacce + hisi_qm + 对应模块 |

任一项无法检查时记 `unknown` 并降低 `confidence`；不中断流程。

## 2. 安装部署 SOP（变更动作，由主框架执行）

### 2.1 UADK（libwd 抽象算法库）

- yum 源（openEuler 22.03 LTS SP4 / 24.03 LTS 及以上）：`yum install libwd`
- RPM 包：src-openeuler/libwd（`rpmbuild --bb SPECS/warpdrive.spec` 后 `rpm -ivh RPMS/aarch64/libwd*.rpm`）
- 源码编译：github.com/Linaro/uadk，参见其 INSTALL

### 2.2 UADK engine（OpenSSL engine）

- yum 源：`yum install uadk_engine`
- RPM 包：src-openeuler/uadk_engine
- 源码编译：github.com/Linaro/uadk_engine

### 2.3 内核驱动加载（顺序强制）

```shell
modprobe uacce
modprobe hisi_qm
modprobe hisi_sec2 uacce_mode=1     # SEC：对称/摘要/AEAD/HMAC
modprobe hisi_hpre uacce_mode=1     # HPRE：RSA/DH
modprobe hisi_zip  uacce_mode=1     # ZIP：gzip/zlib
```

- 顺序：`uacce.ko` → `hisi_qm.ko` → 具体加速器模块；只加载命中算法对应的模块。
- `uacce_mode=1` 为 SVA 模式（用户态必需）；默认值 0 不支持用户态，no-SVA 场景为 2。
- 模块参数：`pf_q_num`（SEC 默认 256，HPRE/ZIP 默认 64）、`vfs_num`（默认 0）、`sgl_sge_nr`（默认 10）、`ctx_q_num`（默认 2）。参数加载后不可更新，如需修改必须先卸载驱动再重载。
- 查询参数：`cat /sys/bus/pci/drivers/<driver>/module/parameters/<param>`。
- 卸载：先停业务，`modprobe -r hisi_sec2`（或 `rmmod`），按加载逆序卸载。

## 3. 队列环境变量

```shell
export WD_RSA_CTX_NUM="sync:2@0,async:4@0"
export WD_DH_CTX_NUM="sync:2@0,async:4@0"
export WD_CIPHER_CTX_NUM="sync:2@2,async:4@2"
export WD_DIGEST_CTX_NUM="sync:2@2,async:4@2"
```

- 格式 `ctx_mode:ctx_num@node`：在 node 号 NUMA 节点上配置 ctx_num 个 ctx_mode 模式的队列。
- 只 export 命中算法对应的环境变量；node 与应用 worker 绑核 NUMA 节点一致。

## 4. 建议配置推导

| 输入 | 推导 |
|---|---|
| worker 线程数 W | async ctx 数 ≈ W（异步模式）；sync ctx 以 2-4 起步；总 ctx ≤ pf_q_num，不足时扩容 pf_q_num |
| 应用 NUMA node N | 全部 `WD_*_CTX_NUM` 写为 `@N` |
| 命中算法集合 | 仅加载对应驱动模块 + export 对应环境变量（SEC→CIPHER/DIGEST，HPRE→RSA/DH，ZIP→无环境变量、走 wd_comp） |
| 典型 pktlen L | `uadk_tool benchmark --pktlen L` 验证；L < 1KB 显式标注负收益风险（per-op 调度开销可能大于卸载收益） |
| 高并发/多实例 | 增大 `pf_q_num` 或启用 SR-IOV（`vfs_num`）后按 VF 分配队列 |
| OpenSSL 生态业务 | openssl.cnf 增加 uadk_engine 段（§5.1），engine id 固定为 uadk_engine |
| 自研实现业务 | 链接 libwd 使用 wd_* 原生 API；压缩只有该路径，无 engine |

## 5. 接入与验证

### 5.1 UADK engine（OpenSSL 业务）

openssl.cnf（一般在 /usr/local/ssl/）开头添加：

```text
openssl_cnf = openssl_def
[openssl_def]
engines = engine_section
[engine_section]
uadk_engine = uadk_section
[uadk_section]
UADK_CMD_ENABLE_RSA_ENV = 1
UADK_CMD_ENABLE_DH_ENV = 1
UADK_CMD_ENABLE_CIPHER_ENV = 1
UADK_CMD_ENABLE_DIGEST_ENV = 1
```

命令行功能示例：

```shell
openssl enc -aes-128-cbc -a -in data -out data.en -pass pass:123456 -K abc -iv abc -engine uadk_engine -p
openssl md5 -engine uadk_engine data.txt
openssl sm3 -engine uadk_engine data.txt
openssl genrsa -out prikey.pem -engine uadk_engine 1024
openssl rsautl -encrypt -in plain.txt -inkey pubkey.pem -pubin -out enc.txt -engine uadk_engine
```

### 5.2 uadk_tool 算子级性能基准

```shell
uadk_tool benchmark --alg md5 --mode sva --opt 0 --sync --seconds 5 --thread 2 --multi 1 --ctxnum 6
uadk_tool benchmark --alg sm3 --mode sva --opt 0 --sync --seconds 5 --thread 2 --multi 1 --ctxnum 6
uadk_tool benchmark --alg sha-512 --mode sva --opt 0 --sync --seconds 5 --thread 2 --multi 1 --ctxnum 6
uadk_tool benchmark --alg aes-128-cbc --mode sva --opt 0 --sync --pktlen 1024 --seconds 5 --multi 1 --thread 2 --ctxnum 6
uadk_tool benchmark --alg sm4-128-ecb --mode sva --opt 0 --sync --pktlen 1024 --seconds 5 --multi 1 --thread 2 --ctxnum 6
uadk_tool benchmark --alg 3des-128-ecb --mode sva --opt 0 --sync --pktlen 1024 --seconds 5 --multi 1 --thread 2 --ctxnum 6
```

- `--opt` 0=加密 1=解密；`--pktlen` 必须取业务典型值；更多参数以 `uadk_tool benchmark --help` 为准。

### 5.3 openssl speed 对比

```shell
# SW 基线
openssl speed -elapsed -evp aes-128-cbc
openssl speed -elapsed rsa2048
# HW（engine 接入后）
openssl speed -elapsed -engine uadk_engine -evp aes-128-cbc
openssl speed -elapsed -engine uadk_engine -async_jobs 10 rsa2048
```

### 5.4 验证与判定流程

1. **SW 基线**：`openssl speed`（或业务基线命令）记录原始输出。
2. **算子级对比**：同 pktlen、同 thread 数对齐，`uadk_tool benchmark` SW vs SVA，或 `openssl speed` ± `-engine uadk_engine`。
3. **业务级 A/B**：engine 接入前后各跑 ≥1 次业务压测（按证据等级 ≥1-3 次），取中位数。
4. **判定**：算子级与业务级均正收益才保留；仅算子级正收益而业务级持平或负收益时回退（调度开销未摊平，常见于小报文）。

## 6. 回退

1. 停业务进程，unset 全部 `WD_*_CTX_NUM`。
2. 还原 openssl.cnf 备份（改动前必须先 `cp openssl.cnf openssl.cnf.bak`）。
3. 按加载逆序卸载驱动：`modprobe -r hisi_sec2` / `hisi_hpre` / `hisi_zip` / `hisi_qm` / `uacce`。
4. 如需彻底还原：`yum remove uadk_engine libwd`。
5. 回退后复跑基线确认环境恢复。
