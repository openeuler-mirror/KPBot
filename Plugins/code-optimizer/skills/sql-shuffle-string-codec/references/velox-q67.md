# Velox Q67 字符串与 LZ4 案例

来源：用户提供的 SQL67 skill 包随附案例摘要。本次导入未复跑真实引擎；以下历史数字不是当前环境的验收成绩，复用时应补齐提交、数据与原始日志。

输入文件是 Snappy Parquet。经 Velox 连接输出后，中间数据可以经 Gluten native Shuffle 传递。记录的压缩路径从 Arrow Lz4FrameCodec 到 LZ4 Frame，解压数据再由 Shuffle reader 重建为列。

## 可定位的源码参考

以下链接用于定位相关模块，已核对文件和对应类或函数存在。它们是本次补充的固定源码参考，不是已确认的历史跑分基线；各仓库的这些版本也不构成经验证的组合构建。历史摘要没有给出当时的提交号、补丁和完整依赖锁定，不能据此断言下表优化已包含在链接版本中。复现时应以目标工程锁定的 Gluten、Velox 和 LZ4 提交重新核对实现与补丁。

| 阶段 | 仓库 | 相对路径与固定链接 | 参考版本/提交 | 历史报告描述的变化 |
| --- | --- | --- | --- | --- |
| 连接输出 | `facebookincubator/velox` | [`velox/exec/RowContainer.cpp`](https://github.com/facebookincubator/velox/blob/ec565bff9527d35bebf2e81a2183040fc832a5ce/velox/exec/RowContainer.cpp)、[`velox/exec/RowContainer.h`](https://github.com/facebookincubator/velox/blob/ec565bff9527d35bebf2e81a2183040fc832a5ce/velox/exec/RowContainer.h) | `ec565bff9527d35bebf2e81a2183040fc832a5ce` | 内联串直接写槽位，长串复制到输出拥有的缓冲区 |
| 写入 | `apache/gluten` | [`cpp/velox/shuffle/VeloxHashShuffleWriter.cc`](https://github.com/apache/gluten/blob/v1.3.0/cpp/velox/shuffle/VeloxHashShuffleWriter.cc) | `v1.3.0` | 报告描述短字符串长度分支采用两段固定拷贝；20 字节的示例覆盖 1–16 与 5–20 字节 |
| 读取 | `apache/gluten` | [`cpp/velox/shuffle/VeloxShuffleReader.cc`](https://github.com/apache/gluten/blob/v1.3.0/cpp/velox/shuffle/VeloxShuffleReader.cc) | `v1.3.0` | 小端 AArch64 上至少有 16 个可读字节时使用 NEON；尾部回退 |
| 字符串表示 | `facebookincubator/velox` | [`velox/type/StringView.h`](https://github.com/facebookincubator/velox/blob/ec565bff9527d35bebf2e81a2183040fc832a5ce/velox/type/StringView.h) | `ec565bff9527d35bebf2e81a2183040fc832a5ce` | 读端重建时核对内联与非内联字符串表示和生命周期 |
| 压缩 | `lz4/lz4` | [`lib/lz4.c`](https://github.com/lz4/lz4/blob/v1.9.4/lib/lz4.c)，搜索 `LZ4_hashPosition` | `v1.9.4` | AArch64 使此前使用四字节的 byU16 路径也使用五字节查找候选 |

Gluten 使用现仓库名 `apache/gluten`；上述 Shuffle 文件位于 `cpp/velox/shuffle/`。`StringView.h` 位于 Velox 的 `velox/type/`，LZ4 实现位于 `lib/lz4.c`。若目标工程带有重命名或私有补丁，保留其实际路径及提交映射，不能将参考路径当作本地路径保证。

## 历史验证记录

报告记录了输出列 495,080 个值、Shuffle 读端约 102 万个值，以及 5,000 个常规块模式模糊用例和 Frame 兼容性。另一次 Sanitizer 模糊测试在两个版本同种子、同迭代处异常。结项版完整 SQL67 正式复测为 195.354 → 156.002 秒，包含其他 SQL 执行算子改动，不能用于为单项 Shuffle 或 LZ4 分配收益。
