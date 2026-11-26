---
name: milvus-offline-build-packaging
description: >
  Milvus 离线构建与镜像打包：gcc-10 全量重编 + SVE 补丁 + simdjson 定制产裸离线产物，但打包成 runtime 镜像时必须
  "FROM 既有 runtime 基座 + COPY 只换 bin/milvus"，不能把裸离线产物直接 tag 成 runtime（缺 etcd/minio/entrypoint，
  启动即 RootCoord deadline exceeded）。适用：Milvus 离线构建产物要打包成可跑镜像、要复用既有 runtime 基座只换二进制、
  或排查"新镜像启动即崩/缺引导"。
  不适用反例：只改配置不重编（无需离线构建）；或离线产物就是完整 runtime 镜像（无裸产物问题）。
---

# Milvus 离线构建 + 只换二进制复用基座

## 何时使用

- 离线环境（无网络）要重编 Milvus 并打包成可跑镜像。
- 只改了 `bin/milvus` 一个二进制，想复用既有 runtime 基座而不是整套重打。
- 排查"新镜像启动即崩，报 RootCoord context deadline exceeded"。

## 核心原则

1. **离线构建产物 ≠ runtime 镜像**：`server/build.sh` 产出的是裸 `milvus-offline`，只有 `milvus run` 本体，
   缺 etcd/minio/entrypoint 引导。
2. **打包 = FROM 既有 runtime 基座 + COPY 只换二进制**：基座提供 ENTRYPOINT/etcd/minio/healthcheck，改动只覆盖
   `bin/milvus`。
3. **只加不覆盖 tag**：新镜像新 tag，严禁覆盖既有 runtime tag（回退不了）。

## 步骤

### 1. 离线重编（全量 + 补丁）

- gcc-10 全量重编 + SVE 补丁（HNSW SCANN prefetch）+ simdjson 定制（`simdjson-custom/` 源码树，开关与验证见
  references/offline-build-packaging.md §4），产出 `milvus-offline`（裸离线产物，缺引导）。
- 构建开关：`build.sh` 检测补丁目录存在即启用 SVE；补丁被改名（如 `.bak`）会静默回退 NEON，重建前先确认。
- **构建入口与来源（固定版本）**：脚本是 `server/build.sh`，位于 milvus 构建树
  `modules/milvus/milvus-arm64-work-v2.4.5/server/build.sh`（milvus **2.4.5** 基线）。工作目录 = 该构建树根：
  ```bash
  cd modules/milvus/milvus-arm64-work-v2.4.5
  bash server/build.sh          # 收集运行时产物到 <构建树>/milvus/{bin,configs,lib}，二进制 = <构建树>/milvus/bin/milvus
  ```
  离线构建包（gcc-10 toolchain / images/*.tar / tantivy vendor / conan 缓存，共 ~21G）只落盘不入 git，
  拿不到这些大件就无法离线重建——把"已解包的 milvus 构建树"列为外部必需输入，不要臆造目录。

### 2. 打包：只换二进制复用基座

```dockerfile
# 既有完整 runtime 基座（含 entrypoint.sh / healthcheck.sh / etcd / minio）
FROM <runtime-base>
COPY <构建树>/milvus/bin/milvus /milvus/bin/milvus
```

> 二进制实路径：构建树根（= `modules/milvus/milvus-arm64-work-v2.4.5`，即 `server/build.sh` 所在 `server/` 的上一级）
> 下执行 `bash server/build.sh` 后，`server/build.sh` 的 step 3 会 `cd milvus` 收集运行时产物，
> 二进制 = `<构建树>/milvus/bin/milvus`（不是 `<构建树>/bin/milvus`）。COPY 源写全路径，勿臆造。

> **⚠️ `#` 注释必须独占一行**：Dockerfile 的 `FROM <runtime-base>  # 说明文字` 会把行尾 `#` 及后续文字
> 一并当作 `FROM` 的镜像参数，`COPY` 执行前构建就失败。注释移到 `FROM` 上一行，别写在同一行行尾。

对比裸离线产物直接 tag 的错误路径：

| | 基线 runtime | 错误的裸产物 tag |
|---|---|---|
| ENTRYPOINT | `/tini -- /usr/local/bin/entrypoint.sh` | `/tini --`（裸） |
| entrypoint / healthcheck / etcd / minio | ✅✅✅✅ | ❌❌❌❌ |

### 3. 冒烟验证

- etcd / minio / milvus 三进程正常起、无 panic，行为与基线 runtime 完全一致，再打正式 tag。
- 裸产物直接 tag 会在 `milvus run standalone` 时报 `[ERROR] RootCoord starts error="context deadline exceeded"`。

## 门禁

- [ ] 补丁启用状态已确认（SVE 未静默回退）
- [ ] 打包 = FROM 基座 + COPY 只换二进制，非裸产物 tag
- [ ] 冒烟三进程正常起、无 panic
- [ ] 新 tag 不覆盖既有 runtime tag

## 结果（标定案例形态）

- 上一轮把裸 `milvus-offline` 直接 tag 成 runtime，缺 etcd/minio 启动即崩（top2-v1 作废）；修复 = 从
  runtime-v1 基座 + COPY `bin/milvus`，产出正确 top2-v2（2.35GB vs 裸产物 5.77GB，且行为与基线一致）。

## 参考

- `references/offline-build-packaging.md`：离线构建树结构、补丁式组织（基线分支不含补丁）、
  SVE + simdjson 开关机制、只换二进制的 Dockerfile、裸产物 vs runtime 基座的差异清单。