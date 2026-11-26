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

**以下校验/登记命令一律在源码根目录执行**（上一段已 `cd build`，先 `cd ..` 回根——
否则相对路径 `build/mooncake-store/src/` 会在 build/build/ 下找产物，散列与 strings 全部落空）。

- 产物在 `build/mooncake-store/src/`（客户端库 + master 可执行文件）。
- 确认**实际要替换的 .so 产物名**：以探测到的容器现役 .so 文件名为准（如 `libmooncake-store.so` / `store_c` 系 / `mooncake/_store.*.so`，不同版本不同）。编译产物与现役文件名不一致时，先弄清 Python 侧加载方式（ctypes 文件名 / C 扩展模块名）再动，禁止想当然改名。
- 全量记录本次产物 sha256（先确认产物存在再写清单——glob 落空时直接重定向会创建/清空空清单文件）：
  **打点目标子集**：清单内区分「打点目标 .so」（含 MKT 串、部署侧四 tag 门禁适用）与
  「伴生库 .so」（transfer engine 依赖，不含 MKT 串属正常、门禁只做 sha256 比对）——
  清单文件名旁标注（如 `# instrumented` 后缀或独立清单 `mkt_targets.txt`），
  部署侧（kptools-mooncake-log-trace，配套 PR #404）据此对非打点文件跳过四 tag 检查：

```bash
cd <mooncake 源码根>
ls build/mooncake-store/src/*.so* >/dev/null 2>&1 \
  && sha256sum build/mooncake-store/src/*.so* > /tmp/mkt_build_manifest.sha256 \
  || echo "[FATAL] build/mooncake-store/src/ 下未找到 .so 产物（编译失败或路径不对），禁止交付空清单"
```

- **产物交接**：编译产物 + sha256 清单 + strings 校验确认（四 tag MKT_TTF/MKT_FINE/MKT_PUT/MKT_PFINE 各 ≥1）一并交付给 `kptools-mooncake-log-trace`（配套 PR #404 交付）执行容器内替换；本 skill 不做 .so 替换与引擎重启。

## 3. 验证编译产物含打点

```bash
# 在源码根目录执行（不要在 build/ 内——相对路径会错位到 build/build/）
# 四个 MKT tag 分别计数（get/put 两链都必须编进目标库）
for t in MKT_TTF MKT_FINE MKT_PUT MKT_PFINE; do
  printf '%s=%s\n' "$t" "$(strings build/mooncake-store/src/<目标.so> | grep -c "$t")"
done
```

四项应各 ≥1。任一为 0 = 对应链打点没编进目标库（get 链 MKT_TTF/MKT_FINE、put 链 MKT_PUT/MKT_PFINE；挂错目标或没重新编），**禁止部署**。

## 4. 编译耗时与中断处理

- 全量编译约 5-20 分钟（视核数）；中断后直接重跑 `make -j$(nproc)`（增量续编）。
- 编译失败先看首个 error（后面的多为连锁）：常见为依赖缺失（缺 `-dev` 包）或版本错配（头文件对不上 = 源码版本与现役不一致，回到版本锚定）。

## 5. 版本锚定记录（编译过一次就更新）

| 日期 | 现役版本 | cmake 选项 | 目标 .so 产物名 | 编译耗时 | 结果 |
|---|---|---|---|---|---|
| — | 待锚定 | — | — | — | — |
