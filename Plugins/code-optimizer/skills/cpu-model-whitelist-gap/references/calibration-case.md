# 标定案例：OpenSSL AES-GCM 的型号白名单缺口

本案例用于说明缺口形态、验证路径和边界；不列主机信息与原始吞吐数字，案例**不是收益承诺**。

## 1. 背景

- 库：OpenSSL 的 master 分支（公开源码）；复现基线固定到 commit `01255d7529cd1db9755b661978850faba42b87fb`（核对时的 master 快照）。
- 白名单位置：`crypto/armcap.c` 的 aarch64 分支（`OPENSSL_cpuid_setup()`）中，`ARMV8_UNROLL8_EOR3` 由 `MIDR_IS_CPU_MODEL(...)` 链按型号设置；位定义和型号常量在 `include/arch/arm_arch.h`（`ARMV8_UNROLL8_EOR3 (1 << 12)`、`HISI_CPU_IMP 0x48`、`HISI_CPU_PART_KP920 0xD01`）。
- 使用位置：`providers/implementations/ciphers/cipher_aes_hw_armv8.c` 用 `IS_CPU_SUPPORT_UNROLL8_EOR3()` 在 AES-GCM 加解密时选择 `unroll8_eor3_aes_gcm_{enc,dec}_*` 内核。
- 目标平台：AArch64，HiSilicon part `0xD06`（以 MIDR 实读值为准）。
- 现象：AES-GCM 在较大数据块上没有达到该平台的理论吞吐，怀疑没有走已编入的 unroll8/EOR3 内核。

### 1.1 从同一基线构建 A/B

- 固定同一 commit、同一 `./Configure` 目标与选项；只对上述白名单打最小补丁，分别构建到两个前缀（例如 base 与 patched）。
- 构建前后记录 `git rev-parse HEAD`、`git diff` 和 Configure 参数；行号会随分支变化，按符号名与函数定位，不要按旧行号硬编码。
- 用同一 EVP 测试与 `openssl speed -evp aes-256-gcm` 做 A/B；原实测环境信息已脱敏，该提交用于复现同一代码形态。

## 2. 三层门判定

| 层 | 状态 | 证据 |
|---|---|---|
| 编译期 | pass | AES-GCM 的 ARMv8 汇编实现已编入 libcrypto（完整符号表或反汇编确认，不只看动态导出符号） |
| 能力探测 | pass | 硬件支持 EOR3/SHA3 指令，库的能力位为真；HWCAP_CPUID 已置位，库内 MIDR 已填充 |
| 型号启发式 | fail | 启用 unroll8 的型号白名单中没有 `0xD06` |

结论：缺口在第三层，属于典型的「实现已编入 + 能力已探测 + 型号未列入」形态。

## 3. 最小改动

只做两处改动：

1. 在 `include/arch/arm_arch.h` 中新增 `0xD06` 对应的 `HISI_CPU_PART_*` 常量（与 `HISI_CPU_PART_KP920 0xD01` 并列，implementer 为 `HISI_CPU_IMP 0x48`）。
2. 在 `crypto/armcap.c` 启用 `ARMV8_UNROLL8_EOR3` 的型号白名单中加入该常量，保留 `ARMV8_SHA3` 等原有能力位前置条件。

不修改实现、不扩大启用范围、不改算法。改动后确认位掩码只翻转目标位，其它位不变。

同一基线的 EOR3 家族还有 `ARMV8_UNROLL12_EOR3`（`1 << 16`，GCM CTR 流，`armv8_aes_gcm_initkey` 用 `AES_UNROLL12_EOR3_CAPABLE` 选择 `aes_v8_ctr32_encrypt_blocks_unroll12_eor3`）和 `ARMV8_HAVE_SHA3_AND_WORTH_USING`（`1 << 15`，SHA3 provider）。本案例只补 `ARMV8_UNROLL8_EOR3` 白名单；A/B 时固定其它位并记录其状态，报告中注明 GCM CTR 流同时依赖 UNROLL8/UNROLL12 两条白名单位，避免把未翻位路径的差异归因到 UNROLL8。

## 4. 实机验证

- 读取 MIDR，确认 implementer/part 与目标树常量一致。
- 用 probe 对比补丁前后的运行时常量：补丁前目标位为 0，补丁后为 1，差异只有目标位。
- 确认消费点：目标位决定 AES-GCM 加解密内核选择；补丁后热点落到对应 unroll8 内核。
- 正确性：运行 OpenSSL 自带的 EVP 测试与算法向量，全部通过；补丁只改变派发，不改变输出。

## 5. 性能形态

- 小数据块：与原来持平，个别尺寸出现小幅负收益，量级在噪声附近；原因是小输入走尾处理路径，固定开销占比高。
- 从约 1 KB 起出现正收益，随数据块增大收益上升，最大档达到两位数百分比；不同密钥长度、加密/解密方向幅度不同。
- 共享机 A/B 下，固定核、逐轮交替、污染守卫通过后，轮间波动与跨次复现都在可接受范围；`perf` 确认收益来自目标内核，而不是频率或缓存状态变化。
- 收益归因只归到本次翻位的 UNROLL8 路径；GCM CTR 流同时受 UNROLL12 白名单位影响，未同步翻位时需固定对照并在报告中说明。
- 验收按「尺寸×方向」报告，不用总平均掩盖小尺寸代价。

## 6. 边界与注意

- **型号命名**：用 `grep` 列出目标树中该 part 的全部常量定义，确认新增或复用哪一个，避免两套常量或漏加。
- **FIPS/合规**：实现文件可能进入模块源码清单或校验和；改动前确认认证与发布流程。
- **上游排除理由**：白名单可能因某型号性能回退或勘误而刻意排除该型号；提交前查注释、提交记录与 issue。
- **测量污染**：共享机上的 SMT 兄弟核、调度抢占和瞬时降频会制造均匀或分尺寸的假信号；必须用共享机 A/B 协议守卫，而不是单次对比。
- **适用边界**：本案例的结论只说明该库在该平台的这一条派发路径存在缺口；换成其它负载、其它库或其它 CPU 型号时，三层门要重新判定，收益要重新测量。
