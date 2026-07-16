# SVE 通算场景指南

本指南补齐压缩、加密、byte/bit 子循环在 `target_arch=sve` 下的保守规则。它只定义静态安全边界和编译级验收口径；没有 SVE 真机 correctness/perf 数据时，不能把结果描述成已完成性能优化。

## 1. 先查指令资产

`references/arm_intrinsics_db/` 仍是首选 curated DB。若 curated DB 未覆盖压缩或加密指令，`query_arm_intrinsics.py` 会回退到 `references/arm_instruction_assets/`：

```bash
python3 scripts/query_arm_intrinsics.py lookup --instruction PMULL --isa sve --json
python3 scripts/query_arm_intrinsics.py search --instruction AES --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group crypto --isa sve --json
python3 scripts/query_arm_intrinsics.py search --group compression --isa sve --json
```

资产命中只说明 Arm 指令存在，不等于有可直接使用的 ACLE intrinsic。生成代码前仍必须确认头文件、feature macro、谓词、类型宽度和编译 flags。

## 2. 压缩场景

可以尝试：

- 固定块 byte map，例如独立字节 XOR、大小写归一化、范围 mask 生成。
- compare/hash 子循环，例如每个 lane 独立比较、散列混合、checksum 辅助循环。
- table lookup 或 permute 子循环，例如 `TBL/TBX` 风格的固定表变换。
- 已证明输入输出不重叠、索引边界明确、每个输出位置只写一次的 SVE gather/scatter。

必须拒绝：

- LZ match copy、字典回填、`dst[i] = dst[i - offset]` 这类可能重叠的写入。
- 变量长度 token parser，例如 literal length、match length、offset 解析驱动循环步进。
- 当前 iteration 依赖前一个 match/token 状态的循环。
- 无法证明 `indices` in-bounds、scatter unique 或 aliasing no-overlap 的间接寻址。

## 3. 加密场景

可以尝试：

- 独立 block 的 XOR、rotate、permute、byte swap、bit select。
- PMULL/GHASH 子块，但必须证明 block 独立、bit-exact 且 feature gate 可用。
- AES/SM4 辅助指令路径，但必须明确 `isa_extensions` 或 feature macro，并通过 compile-only 检查。

必须拒绝：

- CBC/CFB/feedback 这类跨 block 严格依赖的主体循环。
- 任何改变 bit-exact 顺序或有符号溢出语义的重排。
- 未证明移位范围的变量移位。
- 只有指令资产命中、但没有 ACLE intrinsic 映射或编译 flags 证明的路径。

## 4. SVE 静态规则

`validate-snippet --isa sve` 必须挡住常见错误：

- `svcntb/h/w/d` 必须匹配操作元素宽度和指针步长。
- load/store 必须使用来自当前循环边界的 `svwhilelt_*` predicate；尾部不能用裸 `svptrue_*`。
- gather/scatter 必须通过 `--semantic-contract` 或 request 证明 `readonly`、`in_bounds`、`aliasing=no_overlap`；scatter 或 read-modify-write scatter 还必须证明 `unique`。
- byte/bit/crypto 代码出现变量长度 parser、重叠 match copy、未证明变量移位或有符号溢出风险时，默认拒绝。

## 5. 编译级验收

本机不支持 SVE 运行时也可以做编译级验收：

```bash
bash scripts/test_compile.sh --arch sve --compile-only --source ./candidate_sve.c
```

compile-only 只证明语法、头文件和目标架构 flags 可被编译器接受，不代表 correctness 或性能收益。若需要性能结论，必须在真实 SVE 机器上运行同一 request/response/generated 链路。
