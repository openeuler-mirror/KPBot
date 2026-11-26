# Milvus 离线构建 + 只换二进制复用基座全量（标定案例）

## 1. 上一轮根因（top2-v1 启动即崩）

TOP2 实现子代理把 `server/build.sh` 产出的离线构建产物 `milvus-offline` 直接 `docker tag` 成
runtime tag。但离线产物是**裸 milvus runtime**：

| | 基线 runtime-v1 | 错误的 top2-v1 |
|---|---|---|
| ENTRYPOINT | `/tini -- /usr/local/bin/entrypoint.sh` | `/tini --`（裸） |
| entrypoint.sh / healthcheck.sh / etcd / minio | ✅✅✅✅ | ❌❌❌❌ |

裸 `milvus run standalone` 因缺 etcd/minio 报 `[ERROR] RootCoord starts error="context deadline exceeded"`。

## 2. 正确打包

```dockerfile
# 既有完整 runtime 基座（含 entrypoint.sh / healthcheck.sh / etcd / minio）
FROM <runtime-base>
COPY <构建树>/milvus/bin/milvus /milvus/bin/milvus
```

> 二进制实路径：`server/build.sh` 的 `WORKDIR` = 脚本所在 `server/` 的上一级（即构建树根
> `modules/milvus/milvus-arm64-work-v2.4.5`），step 3 `cd milvus` 后二进制在 `<构建树>/milvus/bin/milvus`。
> COPY 源写 `<构建树>/milvus/bin/milvus`，不是 `<构建树>/bin/milvus`。

> **⚠️ `#` 注释必须独占一行**：`FROM <runtime-base>  # 说明` 会把行尾 `#` 及后续文字一并当作 FROM 的镜像
> 参数（`<milvus-image:sve-runtime-v1>（完整 runtime 基座）` 混进镜像引用），`COPY` 执行前构建就失败。
> 注释移到 `FROM` 上一行，别写同一行行尾。

- 正确新镜像 tag = `<milvus-image:top2-v2>`（2.35GB，image id 短 hash）。
- 作废镜像 = `top2-v1`（裸 `milvus-offline`，5.77GB）。
- 冒烟：etcd/minio/milvus 三进程正常起，无 panic，行为与基线 runtime-v1 完全一致。

## 3. 补丁式组织（modules/milvus）

```
modules/milvus/
├── baseline.ref        # 基线版本锚点（milvus + knowhere tag/commit）
├── patches/            # 性能补丁
│   ├── 0001-hnsw-scann-sve-pf.patch   # SVE+PF
│   └── 0002-top2-go-alloc.patch       # TOP2 Go 分配削减（仅 opt/top2 分支）
├── build/              # 构建工程（与性能解耦）
│   ├── build.sh                        # 离线构建脚本的参考/留档副本（整理自 server/build.sh，逻辑未改）
│   ├── 0001-offline-build-deps.patch   # 构建/依赖改动
│   └── README.md                       # 离线大件清单 + tantivy vendor 搬运说明
└── manifest.yaml       # 镜像 tag → (baseline ref + patches) 映射
```

> **构建脚本入口（统一口径）**：实际离线构建入口是工作树里的 `server/build.sh`，位于 `.gitignore` 排除的
> 离线大件树 `modules/milvus/milvus-arm64-work-v2.4.5/`，在该树根执行 `bash server/build.sh`（`WORKDIR` = `server/` 上一级）
> → step 3 `cd milvus` 收集运行时产物，二进制 = `<构建树>/milvus/bin/milvus` → 打 `milvus-offline:v2.4.5-arm64`。
> `build/build.sh` 只是把 `server/build.sh` 原样整理的**参考/留档副本**：其 `WORKDIR` 取脚本所在目录的上一级，
> 直接放在 `modules/milvus/build/` 跑会指错根，要么放回 `server/` 再执行，要么只在工作树内以 `bash server/build.sh` 跑。

分支结构：`main`（基线，不含优化补丁）→ 对应 `<milvus-image:sve-runtime-v1>`；
`opt/top2`（main + TOP2 补丁）→ 对应 `top2-v2`。

## 4. SVE + simdjson 开关机制（易踩坑）

### 4.1 SVE

- `build.sh` 检测 `sve-patch/` 目录存在即启用 SVE（给 knowhere-src 打 0001 补丁）。
- 源树里 SVE 补丁被改名 `sve-patch.bak` 会静默回退 NEON，丢失 `fvec_L2sqr_sve_pf`，对比作废。
- 重建前必须先把 `.bak` 恢复，并 `objdump` 确认产物含 SVE `whilelo`。

### 4.2 simdjson 定制（与 SVE 同款可开关机制）

- `build.sh` 检测 `simdjson-custom/CMakeLists.txt` 存在即用 `MILVUS_SIMDJSON_SRC` 覆盖 FetchContent 构建
  （准备机放置的 v3.1.7 源码树，三项优化 `bit_indexer::write` 标量化 / `-mcpu=native` / `skip_child` bit-trick
  是直接改在源码树里的，非独立 patch 文件）。
- 回退基线：把 `simdjson-custom/` 改名/删除即可。
- **无 `whilelo` 等产物验证命令**：simdjson 定制是源码树覆盖，核对是否包含只能靠「构建日志打印
  `检测到 simdjson-custom/`」+ profile 里 simdjson stage1 占比变化；重建前先确认该目录存在、别误删。

## 5. 离线大件纪律

- gcc-10 toolchain / images/*.tar / tantivy vendor / conan 缓存等离线大件（连同 build 树）共 ~21G 只落盘搬运，
  不入 git。
- 离线构建目录 `.gitignore` 排除，避免误提交大件。

## 6. 坑点

- **裸产物直接 tag 成 runtime**：缺 etcd/minio/entrypoint，启动即崩 —— 这是本 skill 的核心教训。
- **覆盖既有 runtime tag**：只加不覆盖，否则无法回退。
- **SVE 补丁静默回退**：补丁目录被改名 → 回退 NEON → 对比作废，重建前先确认补丁状态。