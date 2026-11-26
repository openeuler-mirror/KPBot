# 阶段 2：Pack 与布局

## 执行期 PackB 复用

plain-B 路径优先采用 `K chunk → N block → M block`，使一个 B tile 服务同一 M group。复用 key 必须包含 B 的物理 batch offset、N chunk 和 K chunk，才能正确处理 broadcast。根据空间并行度把 M chunk 从 4 逐级缩到 1，同分时优先更大复用组。verbose 必须记录 copies/reuses。

不要跨 Run 缓存用户权重指针；执行期复用只限当前调用。固定大 chunk 会压缩多线程 work，也不能用逻辑 batch index 代替物理 offset。

## kBlock 与 PackB 摊销

小 M（≤64）且大 K（>512）出现大量 K chunks 时，扫描 `kBlock ∈ {256,512,768,1024}`。已验证的 SVE256 M32 family 中，N512 常取 512、N256 常取 256；这不是跨平台常量。原始 `m<512 → kBlock=128` 曾使 32×512×7744 产生 61 个 K chunk，并导致 1.4–2.8 倍回退。

## 常量 B PrePack

只在 B 为常量且 packed buffer 可跨多次 Run 复用时启用。优先让 kernel 直接读取 blocked layout，使普通 Run 无锁。若提供外部 packed-buffer API，锁只能覆盖 buffer 租用；覆盖整个 Run 会让并发 Compute 串行化。

PrePack 生命周期不能做依赖 M 的打包，因为 session 初始化时 M 可能仍受动态 batch 影响。比较顺序应包含原后端、plain BRGEMM 和 blocked BRGEMM，不能假设 prepack 必然更快。

## Storage/compute 解耦

N64 物理块中的列连续时，可令 `storage_n_blk=64` 而 `compute_n_blk=32`，kernel 的 LDB 仍保持物理跨度。新权重可以按 family 选择 K16×N32，旧 N64 权重继续兼容。layout key 至少包含 dtype、K、N、kBlock、storage nBlock 和 ISA。

## 二次幂 stride

plain-B 的大二次幂 N 可进入 PackB。packed A/B 的 LDA 先对齐 64B；若元素跨度≥512 且为二次幂，再增加一个 64B cache line（F32 示例：512→528、1024→1040）。copy destination stride、scratch 大小和 BRGEMM descriptor LDA 必须同时更新。

验证必须覆盖 copies/reuses 断言、broadcast B、N/K tail、跨物理块 offset、plain/prepack 稳态时间以及 layout key 隔离。
