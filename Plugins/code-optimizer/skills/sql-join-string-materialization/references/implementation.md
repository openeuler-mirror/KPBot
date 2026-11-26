# 物化实施与生命周期证据

## 源码定位

仓库 `facebookincubator/velox`，参考提交 `ec565bff9527d35bebf2e81a2183040fc832a5ce`：
- [`velox/exec/RowContainer.cpp`](https://github.com/facebookincubator/velox/blob/ec565bff9527d35bebf2e81a2183040fc832a5ce/velox/exec/RowContainer.cpp) 与 [头文件](https://github.com/facebookincubator/velox/blob/ec565bff9527d35bebf2e81a2183040fc832a5ce/velox/exec/RowContainer.h)：定位输出列提取、行容器及字符串堆的访问。
- [`velox/type/StringView.h`](https://github.com/facebookincubator/velox/blob/ec565bff9527d35bebf2e81a2183040fc832a5ce/velox/type/StringView.h)：核对内联表示、长度和非内联引用。

这些固定链接来自原 skill 最新检视修订时核对过的源码参考，用于定位模块；不代表 SQL67 历史跑分的真实基线，也不组成经验证的依赖版本。目标实施应记录工程锁定提交、补丁及实际加载库。

## 实施检查

沿 `输出行映射 → RowContainer 值 → 字符串堆/内联槽 → 输出向量` 建立拥有方表，列出下一批复用、哈希表清理及输出消费的先后关系。保留输入的方案须能指出实际 retain 的缓冲对象，独立复制方案须能指出输出所属的分配器。

同一输入映射可以出现多次。一次物化失败不能把已写长度推进到未初始化区域；沿原有分配失败处理退出或回退，不能留下部分可见输出。源和目标缓冲可能重叠时，先证明顺序安全或沿用具有相同重叠语义的拷贝。

## 目标端验证

先保存原始行值，再消费/销毁输入并验证输出。对外部缓冲引用、字典重复索引、长串跨分配块分别构造原生用例。输入复制到 Python bytes 后仍有效只说明本地所有权模型成立，不证明 C++ 指针和向量析构行为。

性能记录按输入长度分布分层。若通过延长输入缓冲寿命避免复制，应把保留内存计入对照，不能只统计分配次数下降。
