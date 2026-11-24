# SIMD 向量化方法总结

本目录沉淀可跨项目复用的 SIMD 向量化改写方法。所有自动总结只保留场景、
修改要点和代码切片，不记录来源仓库、文件路径、行号、作者或性能数据。
方法库是增量积累的：每次扫描只追加新方法或新示例，不表示已经覆盖某个项目的全部函数。

## 自动总结

| 方向       | 导航                                   |
| ---------- | -------------------------------------- |
| C → NEON   | [summary.md](auto/C_NEON/summary.md)   |
| C → SVE    | [summary.md](auto/C_SVE/summary.md)    |
| NEON → SVE | [summary.md](auto/NEON_SVE/summary.md) |

## 手动总结

- [manual/](manual/)：人工补充的总结，技能不会修改。

## 目录约定

```text
summary/
├── README.md              # 本导航
├── manual/                # 人工维护，技能只创建、不修改
└── auto/
    ├── C_NEON/
    │   ├── summary.md     # 场景与规则索引
    │   └── patterns/      # 每个方法的要点与代码示例
    ├── C_SVE/
    └── NEON_SVE/
```
