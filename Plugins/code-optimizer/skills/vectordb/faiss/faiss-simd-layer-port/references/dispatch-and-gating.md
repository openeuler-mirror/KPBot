# 分派与门控

## selector 与掩码

- 256/512-bit 类型选择器决定每个 SIMD 层用哪套内核：宽层可直通自有实现，或降级到窄层实现（如 512-bit 层复用 256-bit 内核）。新增定宽层时在 selector 里显式直通，否则静默降级，收益归零。
- 分派掩码枚举"该函数在哪些层有实现"。新增层后：新函数用扩展掩码；既有入口（如 256-bit 通用入口）决定是否把新层纳入；漏改掩码会让新层永远不被选中。

## 运行时门控

- 能力位（如 SVE 位）+ 指令集代次（如 SVE2 位）+ 向量长度（系统调用读取当前 VL）与编译宽度精确匹配才启用定宽层；任一方向的 VL 不匹配（偏小或偏大）都超出定宽产物的运行保证，一律走基线实现，不接受"由任务声明放行"的例外。
- VL 按线程维护（`PR_SVE_GET_VL` 读取的是调用线程的当前值）：选层校验必须发生在实际执行内核的线程上，未匹配走基线实现。仅在初始化时以主线程 VL 通过门控、之后按进程级配置（如 `SIMDConfig::level`）选层，会让 VL 不同的工作线程进入定宽内核。
- 分派入口与回退/窄层实现必须来自按基线 ISA 编译的翻译单元：整库或整目标带高层 flags 时，自动向量化会把 `ptrue` 等定宽指令混进同一翻译单元的普通循环（回退函数同样中招），非兼容主机上即 SIGILL；回退路径不得与定宽层共用翻译单元或 flags。
- 门控谓词写成纯函数（入参为能力位与 VL），便于离机单测各组合；主机探测与执行线程的分派时校验都只是它的调用方。

## 单层 vs 动态分发

- 动态分发：主库基线 + 逐层翻译单元，运行时按门控选层；"当前层"常量在各翻译单元按编译 flags 取值，须内部链接（如 static constexpr），避免链接器把不同 flags 的副本合并成一个。
- 单层构建：整库按一个层编译，产物只保证在兼容主机上运行；在不兼容主机上需要加载时，先选择按基线 ISA 单独编译的产物，不依赖运行时回退。
- 显式覆盖宏（如 THE_SIMD_LEVEL）让单层翻译单元声明自己的层，优先于通用推断。

## 层与内核形态选型

NEON/SVE 选型检查点（短输入、连续算术、查表、位运算、融合展开）与结论纪律由 `faiss-simd-optimization` 技能的决策参考承担，本技能不重复；定宽层落地时只补充门控与构建语义的判断。

## 外部参考

- [Arm ACLE：定宽 SVE 类型](https://arm-software.github.io/acle/main/acle.html#introduction-to-fixed-length-sve-types)
- [Linux PR_SVE_GET_VL：线程级 SVE 向量长度](https://man7.org/linux/man-pages/man2/PR_SVE_GET_VL.2const.html)
- [Faiss SIMD 动态分派迁移指南](https://github.com/facebookresearch/faiss/blob/main/faiss/docs/simd_dynamic_dispatch_migration.md)
