# OpenZL 熵内核优化技术参考

> 来源：OpenZL KV Cache BF16 压缩路径优化（Kunpeng 920G，4 轮流水线，Encode 915→1979 MB/s、Decode 531→1827 MB/s，输出逐字节一致）。所有阈值与命中率为该轮实测，迁移时必须重新测量。

## 1. 熵解码：10-bit 根表（+132%）

### 1.1 原理

Huffman 解码每符号以位流顶端 tableLog 位索引解码表。大表（tableLog ≥ 16）展开后达 512KB～2MB，随机 gather 使每次查表吃内存延迟。利用前缀码**块恒等性质**：

> 长度 L ≤ rootBits 的码字在 tableLog 位索引空间中占据一整个对齐块（2^(tableLog-rootBits) 个连续槽位），块内所有槽位的表项相同——块首槽位即整块的权威表项。

预计算根表 `root[rootBits 位]`（10-bit = 1024 项 × 4B = 4KB，常驻 L1D）：`root[p] = dtable[p << (tableLog-rootBits)]`。命中（根表项 nbBits ≤ rootBits）直接使用；未命中回退全表（慢路径，罕见）。

### 1.2 骨架

表构建（解码入口一次性，O(1024)）与三重启用门：

```c
ZS_Huf16DElt root10[1024];
ZS_Huf16DElt const* root10Ptr = NULL;     /* root10Ptr 是唯一判据 */
int rootShift = 0;                         /* 与 root10Ptr 同生命周期赋值 */
if (tableLog >= 16) {                      /* 门1: 大表才值得建根表 */
    size_t const step = ((size_t)1) << (tableLog - 10);
    unsigned owned = 0;
    for (size_t p = 0; p < 1024; ++p) {
        root10[p] = dtable[p * step];      /* 块首槽位 = 权威表项 */
        if (root10[p].nbBits <= 10) ++owned;
    }
    if (owned * 100 >= 85 * 1024) {        /* 门2: 恒等块占比 ≥85% */
        bool safe = true;
        for (int i = 0; i < 4; ++i)
            safe &= dstreams[i].bitsConsumed <= 54u;  /* 门3: 位窗口可表达 */
        if (safe) { root10Ptr = root10; rootShift = tableLog - 10; }
    }
}
```

热路径（X4 四流主循环，命中路径 ~7 指令/符号）。**整个根表块必须以 `root10Ptr != NULL` 为入口判据，任一门不满足时 `root10Ptr` 保持 NULL，整段走原全表路径**——与 §1.3 门禁要点"任一门不满足自动回退全表路径"严格对应：

```c
if (root10Ptr != NULL) {
    int k10 = 54 - (int)dstreams[i].bitsConsumed;   /* 与 tableLog 无关 */
    for (int u = 0; u < kUnroll; ++u) {
        ZS_Huf16DElt entry;
        if (ZL_LIKELY(k10 >= 0)) {          /* 位窗口守卫：防负移位越界（见 §2 边界陷阱） */
            entry = root10[(dstreams[i].bitContainer >> k10) & 1023];
            if (entry.nbBits > 10) {        /* 慢路径：根表未命中，回退全表 */
                size_t const val = (dstreams[i].bitContainer
                                    >> (k10 - rootShift)) & mask;
                entry = dtable[val];
            }
        } else {                             /* 位窗口耗尽：回退全表 */
            entry = dtable[lookBits(&dstreams[i], tableLog) & mask];
        }
        *ptrs[i]++ = entry.value;
        k10 -= entry.nbBits;
    }
    dstreams[i].bitsConsumed = (unsigned)(54 - k10);
} else {
    /* 门1/2/3 任一不满足：原全表解码路径，语义与优化前完全一致 */
}
```

**实现注意（避免照抄骨架引入缺陷）**：

1. `root10Ptr` 与 `rootShift` 必须同生命周期赋值、同判据使用；**禁止只判 tableLog 而不判 `root10Ptr`**——恒等块占比（门2）是数据分布属性，同一 tableLog 下不同分布可能失败。
2. 循环内 `k10 >= 0` 守卫是硬要求：k10 为负时 `>> k10` 是未定义行为，且 `(k10 - rootShift)` 同样可能为负——两条移位都必须有非负保证（§2 的教训同样适用于根表路径）。
3. 骨架用结构体字段访问（`entry.nbBits` / `entry.value`）表达语义。若按 OpenZL 实际代码把表项按 u32 整字读取（`memcpy(&word, &entry, 4)` 再 `word >> 16` 取 nbBits），**必须先核对 `ZS_Huf16DElt` 的真实布局**（字段序、各字段宽度、总大小），确认 nbBits 恰落在高 16 位才允许该读取方式；布局不符时按真实结构取值，不得硬编码移位量。编码侧 ctable 的 `{u16 value; u16 nbBits}`（§3）不能外推到解码侧 DElt。

关键恒等式：根索引 `(val >> (tableLog-10)) & 1023` ≡ `(c >> (54 - bitsConsumed)) & 1023`，因此命中路径的移位量与 tableLog 无关，可复用补码 k 循环结构（见 §2）。

### 1.3 实测与门禁依据

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| Decode 吞吐 | 531 MB/s | 1232 MB/s（+132%，后续叠加其他优化至 1827） |
| 解码 IPC | 1.20 | 2.51 |
| 根表命中率 | — | 96.5%（KV 指数分布 Top16 覆盖 99.82%，码长多数 ≤ 10） |

门禁要点：门 2 用 O(1024) 实测恒等块占比而非假设（对抗审核曾推翻 ">99%" 假设，实测 96.5%）；门 3 保证 10-bit 窗口在位容器内可表达；任一门不满足自动回退全表路径。

## 2. 熵解码：循环携带补码移位（指令 -37.5%，收益边际）

原 `lookBits` 计算 `(c << b) >> (64 - tableLog)`——两次变量移位，且 `bitsConsumed` 每符号从流状态重建。改法：补码移位量 `k = 64 - tableLog - bitsConsumed` 作为**循环携带变量**，查表重写为 `(c >> k) & mask`；`bitsConsumed` 仅在每个展开块结束重建。每符号 8 → 5 条指令。

**教训**：该项单独提交后收益在噪声内——因为瓶颈是 dtable 访存而非指令数。但作为根表的基础结构保留（根表路径复用同一 k 结构）。指令优化在访存 bound 内核上不要单独作为候选主打；先归因（IPC 1.4 + L1 miss 12.7% → 访存类）。

**边界陷阱**（对抗审核发现）：补码 k 公式若漏 `& mask`，k 为负时移位量越界会读 dtable 越界——必须 `k >= 0` 守卫或回退通用路径。

## 3. 熵编码批次（合计 +13~22.5%）

四项独立可叠加，均要求输出位流逐字节不变：

1. **快速加位 `addBitsFast`**：规范 ctable（满足 Kraft 不等式，value-clean）下省去溢出掩码与检查。前置条件是表规范性；非规范表必须走通用 `addBits`。
2. **u32 packed ctable**：`{u16 value; u16 nbBits}` 两字段打包为 u32，一次 `ldr` 取双字段。所有消费点（含平台专属 avx2 路径）同步改造。
3. **删除冗余全趟预扫描**：某统计扫描的结果仅用于已被门控关闭的 avx2 分派（`allowAvx2==false && !bit mask`）；收紧门控后扫描无消费点，整趟删除。
4. **scratch arena**：每次调用 ~4MB `calloc` 产生的页故障改由调用方持有的 arena 提供；NULL 自动回退分配（线程安全），实测贡献 ~1% 但消除尾延迟抖动。

## 4. 熵编码：展开深度与残差冲刷（+8.4%）

u64 位容器硬约束：**kUnroll × maxCodeLen + 7 ≤ 64**（7 为 reload 对齐余量）。实测 kUnroll=3 × maxCodeLen=20 + 7 = 67 越界。解法是**残差冲刷**：按 maxCodeLen 分档，超限档位在展开中途先冲刷部分和再继续，使 3 路展开在 ≤ 18-bit 档位生效（+8.4%）。

推导模板：设展开 U 路、最大码长 L、容器宽 W（u64=64），安全条件 `U*L + margin ≤ W`；不满足时或降 U，或插入冲刷点把窗口拆成两段各自满足条件。

## 5. 直方图：4x 展开（+10.8%）与桶拆分（负结果）

- **4x 展开单桶**：同地址 4 路独立累积器展开（减小循环携带依赖），实测 +10.8%。
- **4×256KB 桶拆分**（多线程并行直方图的桶私有化方案）：工作集溢出 L2，实测回归（1428 vs 1457 MB/s）——回退。单线程场景下桶拆分只增加工作集没有并行收益。

结论：直方图优化优先做**单桶内多路独立累积**；任何增大工作集的方案必须实测回归测试。

## 6. 数据并行内核：手写 SIMD 无增量（负结果）

`float_deconstruct`（纯数据并行位拆装）手写 NEON 实测与 gcc 12.3.1 -O3 autovec 持平（decode isolated +3.8%、全链路 ≈0）→ 回退。判据：**先反汇编确认 autovec 已生效**（该内核 -O3 已生成 NEON 循环）。对照 §8：位操作/查表分支密集的熵内核 autovec 则是净负——两类内核的 SIMD 策略相反。

## 7. 场景层：缓冲复用（Encode/Decode 各 +50%）

unitBench KV 场景每迭代 `malloc/free` exponent/tokens 缓冲，页故障使内核态 `el0_da` 占 8~15%。改法：wrapper 静态/上下文持有缓冲跨迭代复用。要点：

- 复用缓冲必须保证首轮之后的迭代不被残留数据污染（每迭代完整覆写或显式清零）；
- 输出正确性依赖输出尺寸字段（nbElts 头部）而非缓冲内容；
- 该收益属场景层，与内核优化分开归因。

## 8. 自动向量化门禁（负结果，关闭该路线）

对熵内核（位拆包 + Huffman/直方图查表）开 `-march=armv8.2-a+sve2/sve/simd`（gcc 12 与 clang 17 组合实测）：指令数 +17%，IPC +14% 补不回，净收益 -4%~-36%。机制：向量化产生大量 gather/permute/谓词开销，而查表分支模式本身不可高效向量化。

判别规则：

| 内核形态 | SIMD 策略 |
|---------|----------|
| 纯数据并行、无查表分支（deconstruct/bitpack/delta） | 先查反汇编，autovec 已覆盖则不手写 |
| 位流串行依赖（Huffman X4 解码） | 禁止向量化，收益在指令削减与表布局 |
| 查表 + 分支密集（直方图、token 打包） | 禁止 `-march` 强制向量化，负收益 |

## 9. 熵 codec 选型（分布函数，勿写死）

实测对比（三机一致，全量真实 KV 数据，MB/s）：

| 数据 | 方案 | Encode | Decode | 压缩率 |
|------|------|--------|--------|--------|
| BF16 指数（8-bit，256 符号，熵 3.21 bit） | Huffman token2 | 958~1423 | 552~688 | x1.44 |
| BF16 指数 | FSE 1B 流 | 574~734 | **796~1119（快 43~63%）** | x1.43 |
| BF16 指数 | multi 分块 | 559~694 | 749~1015 | **x1.48** |
| FP8 E4M3 指数（4-bit，16 符号，熵 3.12 bit） | Huffman token（两两打包 u8） | **970~1249** | **1124~1393** | x1.13 |
| FP8 E4M3 指数 | FSE 1B 流 | 309~374 | 388~565 | x1.12 |

规律：

1. 符号基数大、分布倾斜（8-bit 指数）：FSE 解码显著快，Huffman 4 路位流分支依赖是各架构共同软肋；但编码方向相反（token 打包符号数减半 + 编码本身快）。
2. 符号基数小（4-bit 指数 16 符号）：两两打包后符号数进一步压缩，Huffman 快速位流收益凸显，Huffman 全方向胜出（FSE 慢近 4 倍）。
3. 编码快与解码快不可兼得时按部署形态取舍（KV 推理 decode 高频 → 优先解码吞吐）。
4. 追求压缩率：成对/成组打包编码联合分布（KV 相邻元素相关，联合熵 < 边际熵），加大打包宽度可继续提升 R，代价是吞吐。
5. 引擎 graph 场景的自动选型 + decoder fusion 可能优于任何手动 wrapper 组合，优化前先测 graph 基线。

## 10. 技术适用条件速查

| 技术 | 适用条件 | 硬约束/门禁 | 实测收益 |
|------|---------|------------|---------|
| 根表 | tableLog ≥ 16 大表；分布倾斜 | 恒等块占比门 + 位窗口门 + 回退路径 | Decode +132% |
| 循环携带补码 k | 位流查表循环 | k ≥ 0 守卫（防负移位越界） | 指令 -37.5%（访存 bound 下边际） |
| addBitsFast | ctable 规范（value-clean） | 非规范回退通用路径 | 编码批次合计 +13~22.5% |
| u32 packed 查表项 | 多字段查表项 | 全部消费点同步改 | （同上批次） |
| 删冗余趟 | 统计结果无活消费点 | 分派门控收紧后确认 | （同上批次） |
| scratch arena | 每调用大分配 | NULL 回退，线程安全 | ~1% + 尾延迟 |
| kUnroll + 残差冲刷 | 位容器内多符号展开 | U×L+7 ≤ 64 | +8.4% |
| 直方图多路展开 | 单线程直方图 | 不增大工作集 | +10.8% |
| 缓冲复用 | 场景循环每迭代分配 | 防残留污染 | Encode/Decode 各 +50% |
| 熵选型 | 按目标分布实测 | encode/decode 分方向评估 | 43%~4x（方向不定） |
