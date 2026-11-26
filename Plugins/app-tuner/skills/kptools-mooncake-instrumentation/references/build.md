# Mooncake 容器内编译指南

> 在目标容器内编译打点后的源码。**禁止在容器外/异版本环境编译后直塞**——ABI、依赖库
> 版本必须与容器运行环境一致，否则替换后 Python 侧 import 或运行时崩溃。

## 1. 编译前置检查（探测步骤已产出）

| 检查项 | 命令 | 不满足时 |
|---|---|---|
| git / cmake / g++ | `git --version; cmake --version; g++ --version` | 停下问用户：装工具或改用带工具链的构建容器 |
| RDMA 依赖 | `ldconfig -p \| grep -E "ibverbs|rdma"` | 传输引擎编不过；停下确认传输类型（TCP 模式可无 RDMA） |
| 磁盘空间 | `df -h <源码目录>` < 5G | 清理或换目录 |
| 源码版本 | git log 与探测版本一致 | 停：版本错配 |

## 2. 编译步骤

```bash
cd <mooncake 源码根>          # 含打点 patch 的工作副本
mkdir -p build && cd build
cmake ..                      # 默认模式；集群用 etcd 时 -DSTORE_USE_ETCD
make -j$(nproc)
```

- 产物在 `build/mooncake-store/src/`（客户端库 + master 可执行文件）。
- 确认**实际要替换的 .so 产物名**：以探测到的容器现役 .so 文件名为准（如 `libmooncake-store.so` / `store_c` 系 / `mooncake/_store.*.so`，不同版本不同）。编译产物与现役文件名不一致时，先弄清 Python 侧加载方式（ctypes 文件名 / C 扩展模块名）再动，禁止想当然改名。
- 全量记录本次产物 md5：`md5sum build/mooncake-store/src/*.so* > /tmp/mkt_build_manifest.md5`。
- **产物交接**：编译产物 + md5 清单 + strings 校验确认（MKT 串 ≥2）一并交付给 `kptools-mooncake-log-trace`（配套 PR #404 交付）执行容器内替换；本 skill 不做 .so 替换与引擎重启。

## 3. 验证编译产物含打点

```bash
strings build/mooncake-store/src/<目标.so> | grep -c "MKT_TTF\|MKT_FINE"
```

应为 ≥2（两个格式串）。为 0 = 打点 patch 没编进目标库（挂错目标或没重新编），**禁止部署**。

## 4. 编译耗时与中断处理

- 全量编译约 5-20 分钟（视核数）；中断后直接重跑 `make -j$(nproc)`（增量续编）。
- 编译失败先看首个 error（后面的多为连锁）：常见为依赖缺失（缺 `-dev` 包）或版本错配（头文件对不上 = 源码版本与现役不一致，回到版本锚定）。

## 5. 版本锚定记录（编译过一次就更新）

| 日期 | 现役版本 | cmake 选项 | 目标 .so 产物名 | 编译耗时 | 结果 |
|---|---|---|---|---|---|
| — | 待锚定 | — | — | — | — |
