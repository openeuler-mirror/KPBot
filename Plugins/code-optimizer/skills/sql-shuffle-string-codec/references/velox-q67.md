# Velox Q67 字符串与 LZ4 案例

来源：用户提供的 SQL67 skill 包随附案例摘要。本次导入未复跑真实引擎；以下历史数字不是当前环境的验收成绩，复用时应补齐提交、数据与原始日志。

输入文件是 Snappy Parquet。经 Velox 连接输出后，中间数据可以经 Gluten native Shuffle 传递。记录的压缩路径从 Arrow Lz4FrameCodec 到 LZ4 Frame，解压数据再由 Shuffle reader 重建为列。

| 阶段 | 源码线索 | 变化 |
| --- | --- | --- |
| 连接输出 | `RowContainer.cpp/.h` | 内联串直接写槽位，长串复制到输出拥有的缓冲区 |
| 写入 | `VeloxHashShuffleWriter.cc` | 报告描述短字符串长度分支采用两段固定拷贝；20 字节的示例覆盖 1–16 与 5–20 字节 |
| 读取 | `VeloxShuffleReader.cc`、`StringView.h` | 小端 AArch64 上至少有 16 个可读字节时使用 NEON；尾部回退 |
| 压缩 | `lz4.c`、`LZ4_hashPosition` | AArch64 使此前使用四字节的 byU16 路径也使用五字节查找候选 |

报告记录了输出列 495,080 个值、Shuffle 读端约 102 万个值，以及 5,000 个常规块模式模糊用例和 Frame 兼容性。另一次 Sanitizer 模糊测试在两个版本同种子、同迭代处异常。结项版完整 SQL67 正式复测为 195.354 → 156.002 秒，包含其他 SQL 执行算子改动，不能用于为单项 Shuffle 或 LZ4 分配收益。
