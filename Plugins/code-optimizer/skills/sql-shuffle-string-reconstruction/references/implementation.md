# 读端重建：边界、布局及寿命

## 源码参考

- `apache/gluten` `v1.3.0` 的 [`cpp/velox/shuffle/VeloxShuffleReader.cc`](https://github.com/apache/gluten/blob/v1.3.0/cpp/velox/shuffle/VeloxShuffleReader.cc)。
- `facebookincubator/velox` 提交 `ec565bff9527d35bebf2e81a2183040fc832a5ce` 的 [`velox/type/StringView.h`](https://github.com/facebookincubator/velox/blob/ec565bff9527d35bebf2e81a2183040fc832a5ce/velox/type/StringView.h)。

这些固定链接来自原 skill 最新检视修订时核对过的源码参考，用于定位模块；不代表 SQL67 历史跑分的真实基线，也不组成经验证的依赖版本。目标实施应记录工程锁定提交、补丁及实际加载库。

## 分派顺序

先验证值的 `[offset,offset+length)` 范围，再用一次实际指令的读取宽度判断能否进入候选。检查 `length <= buffer_size-offset` 可避免直接加法溢出；地址计算仍应使用目标接口定义的无溢出类型。

内联字符串长度小于指令宽度时，额外读取的字节也必须属于合法可读区。跨分配块、无填充尾部和损坏长度应使用原构造或原错误路径。不要从其他 split 或下一缓冲借用“通常存在”的字节。

本地模型固定 12 字节内联阈值，仅用来覆盖布局边界，目标阈值来自实际 StringView。204 组长度/偏移/尾空间组合核对值和分派；另外检查构建/运行时 ISA、字节序、布局、对齐、长串拥有方与非法范围。

## 内联零填充与原生判等

引用的 StringView 构造器清理内联填充，判等先比较长度和前缀的整字，内联长度超过前缀时还比较后段整字。宽读虽然合法，也必须屏蔽字符串长度之外的相邻内容；不能把后续 X/Y 字节留在内联槽位。两个输入均表示单字节 a，若槽位分别保存 `aXXX...` 与 `aYYY...`，只比较有效字节会错误地漏掉原生判等差异。

本地 Python 用例显式保存 12 字节内联区，并模拟长度/前缀及后段整字判等；长度 0–12 的不同尾内容重建后应等价，未清零的 a/X、a/Y 是反例。这个模型不证明真实 C++ ABI 或指令行为。

[native_inline_equality.cpp](../cases/reconstruction-contract/native_inline_equality.cpp) 包含真实 `velox/type/StringView.h`，调用其构造器和 `operator==`，同时检查未屏蔽宽读的反例。将该源文件放入已配置依赖的目标 Velox CMake 工程，以工程实际提供的类型/异常依赖构建独立测试目标；示例目标可使用 `target_link_libraries(kpbot_inline_eq PRIVATE velox_type)`，须以目标工程的依赖定义为准。执行测试二进制应退出 0 并输出 `native_StringView_operator_equal=true`，保留源码提交、构建命令和输出。

本机没有已配置的 C++/Velox 构建，故原生回归未执行；加入源文件不等于通过原生判等。本用例的宽读取使用 memcpy 表示固定读取范围，也不能替代目标 reader 的 NEON 分支、未对齐和 guard-page 检查。

## 目标端验收

测试 NULL、输出类型、实际原生向量槽位、ISA 回退和 ASan/UBSan/guard-page 读边界；Python bytes 不会执行 NEON。对长串，在 reader 输入释放后读输出，并覆盖缓冲复用；模型中的 memoryview retain 不能代替 C++ retain 证据。
