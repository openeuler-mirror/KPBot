# DiskANN 内存与布局优化

文中命令相对所属 skill 根目录执行。

## 执行流程

1. 需要计算大小时读取 [data-structure-formulas.md](data-structure-formulas.md)。
2. 需要分析稀疏/稠密 visited 或磁盘局部性时读取 [visited-and-physical-layout.md](visited-and-physical-layout.md)。
3. 在当前 commit 上核实结构体字段、padding、allocator 开销、对齐和序列化格式。
4. 移动数据层级或改变表示前，测量每查询访问频率和热点程度。
5. 进行布局修改时，一致更新所有携带 ID 的结构；必须保留外部 ID 时建立反向映射。
6. 使用 defrag/copy 对照比较逻辑重排，分离语义局部性收益与物理 extent 影响。

## 约束

- payload 公式只是下界；allocator、padding、metadata 和多 worker 副本必须单独报告。
- 即使访问节点很少，全局 bitmap 每查询仍需要 `ceil(N/8)` 字节。
- 输入行号通常成为 vertex ID，但必须在当前版本构建路径中重新证明。
- extent 数和 LBA 相邻描述物理放置，不代表向量相似度。

## 按需资源

- [data-structure-formulas.md](data-structure-formulas.md)：计算 PQ、scratch、visited、节点和索引占用时读取。
- [visited-and-physical-layout.md](visited-and-physical-layout.md)：分析 HashSet/bitmap、sector、extent 和逻辑/物理局部性时读取。
