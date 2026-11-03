---
name: openeuler-agentenv
description: 在 openEuler 上安装和排障 AgentENV（kvcache-ai）。在 openEuler 上部署 AgentENV、运行其 benchmark、编译/加载 ublk 内核模块，或遇到这些报错时使用：rustup 下载超时、protoc "timestamp.proto File not found"、ublk add_dev "Permission denied"、overlaybd "CLI::ValidationError"、sandbox 启动 "Connection refused"、benchmark RocksDB 锁冲突。
---

# AgentENV on openEuler

已在 openEuler 24.03 LTS-SP3 aarch64（鲲鹏，内核 6.6）实测通过。AgentENV 文档要求
内核 6.8+，但 openEuler 6.6 自带**打过补丁的** ublk 驱动和 uapi（zero-copy 位于
bit 0，与 AgentENV 的 `storage/ublk/src/ublk_caps.rs` 定义一致）——在 6.6 上可以直接安装。

## 安装步骤

### 0. 获取源码

```bash
git clone https://github.com/kvcache-ai/AgentENV.git
```

本网络下 github.com 的 git-over-HTTPS 可用（raw.githubusercontent.com 和 ghcr blob
很慢，但 git 不受影响）——cargo 的 git 依赖拉取（`git-fetch-with-cli = true`，
github 上的 `ublk-sys` crate）也能成功。clone 卡住就重试；git 断点协商恢复能力
比 HTTP range 下载好得多。

**完成判据**：仓库在磁盘上且 `.git` 完整（根 `build.rs` 会嵌入 commit hash；
缺 `.git` 会退化为 "unknown"，可接受）。

### 1. 国内网络镜像（后续所有下载都依赖这步）

rustup 从 static.rust-lang.org 下载会超时；Docker Hub registry API 直连不通，
但 `docker pull` 能用（daemon 走了 `/etc/docker/daemon.json` 里的镜像加速器）。
ghcr.io blob 只有 ~20 KB/s。

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh
export RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
sh /tmp/rustup-init.sh -y --profile minimal --component clippy,rustfmt --default-toolchain stable
```

`~/.cargo/config.toml`：

```toml
[source.crates-io]
replace-with = 'rsproxy-sparse'

[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"

[net]
git-fetch-with-cli = true
```

任何经 regctl/reqwest 解析镜像的工具（AgentENV 镜像解析、benchmark）遇 `docker.io`
引用都会挂死——改成镜像加速器地址：`docker.m.daocloud.io/library/ubuntu:24.04`
（在 `config/default.toml` 里设 `default_image`）。GitHub/ghcr 依赖下载慢时，用 curl
（重试 + 断点续传）预下载到日志里打印的目标路径；AgentENV 的 `download_file` 会跳过
已存在的非空文件，预先放好即可。

**完成判据**：`cargo build` 成功，镜像引用解析不再挂起。

### 2. 构建依赖 —— openEuler 包名有差异

```bash
dnf install -y gcc make git curl perl bc dnf-plugins-core \
               clang clang-devel protobuf-compiler protobuf-devel \
               flex bison elfutils-libelf-devel openssl-devel
```

- 前一行是基础工具链：cargo 链接要 gcc、内核模块编译要 make/perl/bc、步骤 0 和
  预下载要 git/curl、步骤 3 的 `dnf download --source` 要 dnf-plugins-core。
  实测主机上这些恰好预装了，新机器上必须显式安装。
- openEuler 没有 `libclang-devel`，对应包是 `clang-devel`。
- 缺 `protobuf-devel` 时 protoc 报 `google/protobuf/timestamp.proto: File not found`
  （该包提供 `/usr/include/google/protobuf/*.proto`）。

**完成判据**：`cargo build --release -p agentenv --bin server -p uvm-ublk -p uvm-ublk-daemon`
成功（根 `build.rs` 经 tonic 调用 protoc）。

### 3. ublk 内核模块

openEuler 出厂 `# CONFIG_BLK_DEV_UBLK is not set`——没有模块，也没有运行期开关。
必须按**运行内核的精确版本**编译：

1. `dnf install kernel-devel-$(uname -r)` —— 必须钉住版本；update 源里有更新的
   内核 devel，vermagic 不匹配将无法加载。
2. 取驱动源码：`dnf download --source kernel-<运行版本>` →
   `rpm2cpio | cpio -id` → 从 `kernel.tar.gz` 解出 `kernel/drivers/block/ublk_drv.c`
   （`kernel-devel` 只有头文件和 Makefile，没有 `.c` 源码）。
3. 用最小目录编译——直接 `M=drivers/block` 会连带匹配 LOOP/NBD/VIRTIO_BLK
   （都是 =m 且无源码）导致失败：

   ```bash
   mkdir build && cp .../ublk_drv.c build/ && echo 'obj-m += ublk_drv.o' > build/Makefile
   make -C /usr/src/kernels/$(uname -r) M=$PWD/build modules
   ```

4. 加载并持久化：

   ```bash
   insmod build/ublk_drv.ko        # 出现 /dev/ublk-control
   cp build/ublk_drv.ko /lib/modules/$(uname -r)/extra/ && depmod $(uname -r)
   echo ublk_drv > /etc/modules-load.d/ublk_drv.conf
   echo "options ublk_drv ublks_max=256" > /etc/modprobe.d/ublk_drv.conf
   ```

`ublks_max` 默认 64，50 并发的 benchmark 会超。内核编译 GCC 小版本不匹配的警告
（如 12.3.1-105 对 -111）无害。

**完成判据**：`/dev/ublk-control` 存在（重启后经 modules-load 自动加载）。

### 4. Server 启动 —— 顺序解决鸡生蛋问题

```bash
useradd -r -m -s /usr/sbin/nologin aenv   # 必须先做，否则 "runtime user does not exist: aenv"
./target/release/server --setup-only      # 下载 firecracker/kernel/overlaybd-tools/regctl；并自动 dnf 安装缺失的运行时命令包（umoci/zstd/jq 等）
./target/release/server --setup-host      # 生成 /etc/overlaybd/overlaybd.json + 网络内核参数
./target/release/server --setup-only      # 此时 agentenv-tools 镜像转换才能成功
API_ADDR=0.0.0.0:8000 ./target/release/server
```

第二次 `--setup-only` 是必需的：`overlaybd-apply` 对 `--service_config_path` 做硬校验
（CLI11 文件存在性检查），`/etc/overlaybd/overlaybd.json` 缺失时抛
`CLI::ValidationError` 中止——而该文件只由 `--setup-host` 生成，setup-host 又依赖
第一次 setup-only 下载的依赖。

**完成判据**：`curl http://127.0.0.1:8000/health` 返回 204。

## 症状 → 根因 → 修复

| 症状 | 根因 | 修复 |
|---|---|---|
| rustup 安装挂起 | static.rust-lang.org 不可达 | rsproxy.cn 环境变量（步骤 1） |
| protoc: `google/protobuf/timestamp.proto: File not found` | 缺 `protobuf-devel` | 安装它（步骤 2） |
| setup-only 报 `CLI::ValidationError ... overlaybd.json` | 配置文件只由 setup-host 生成 | 按 setup-only → setup-host → setup-only 顺序 |
| 依赖下载 `connection error / timed out`（vmlinux 等） | ghcr/github blob 慢，reqwest 无重试 | curl 预下载到日志打印的目标路径后重跑 |
| ublk add_dev: `Permission denied (os error 13)` | 被杀 daemon 留下的孤儿设备占满 `ublks_max` 配额；孤儿表现为有 `/dev/ublkc*` 但**没有** `/dev/ublkb*` | `rmmod ublk_drv && modprobe ublk_drv`；两种节点都要检查，调大 `ublks_max` |
| sandbox 启动: `Connection refused (os error 111)` | 死掉的 daemon 残留 `/run/aenv/ublk-daemon.sock`；文件存在时 server 不会重新拉起 daemon | 删掉 socket，重启 server |
| benchmark 打印 `Skipping ...: RocksDB ... metadata/LOCK: Resource temporarily unavailable` | bench 和 server 都要独占打开 `/var/lib/aenv/image-cache/metadata` | 跑 `cargo bench` 前停掉 server |
| 镜像解析永久挂起 | `default_image`（ubuntu:24.04）经不可达的 docker.io 解析 | 在 `config/default.toml` 里改成镜像加速器地址 |
| `aenv auth` 报 `No such device or address (os error 6)` | 需要交互式 TTY | 直接写 `~/.config/aenv/credentials` TOML：`url = "..."`、`api_key = "$(cat /var/lib/aenv/secrets/api-key)"` |

## 共享主机的进程陷阱

- **自杀式 kill**：`pkill -f`/`pgrep -f` 会匹配到调用方自己的 `bash -c` 命令行
  （模式串出现在其中），把自己的 shell 杀掉。用括号模式（如
  `pgrep -f "release/[s]erver"`）或把 kill 逻辑放进脚本文件。
- **主机上可能有其他运行时**（自己的 firecracker 在 `/fc-versions` 下，约 450 个
  veth、约 10 万条 iptables 规则）。只按完整二进制路径杀——我们的是
  `/var/lib/aenv/deps/firecracker/...`——绝不按裸进程名。server 的 warm-pool VM
  也在同一路径下，随 server 一起停止。
- 从会话里启动**长命 daemon** 需要 `setsid` 加完整 fd 重定向
  （`>log 2>&1 </dev/null`）；否则会随会话进程组一起被杀，还会占住输出管道不释放。
