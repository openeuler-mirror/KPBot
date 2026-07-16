## 7. NEON-SVE Bridge

Bridge 提供 3 族内联函数，用于在 Neon（128 位固定）和 SVE（可扩展）向量之间进行转换。

### svset_neonq — 将 Neon 嵌入 SVE

```c
// 从 Neon 向量设置 SVE 向量的前 128 位
svint32_t sve_v = svset_neonq_s32(svundef_s32(), neon_v);
// sve_v 的其余位未定义（来自 svundef）
```

### svget_neonq — 从 SVE 中提取 Neon

```c
// 获取 SVE 向量的前 128 位作为 Neon 向量
int32x4_t neon_v = svget_neonq_s32(sve_v);
```

### svdup_neonq — 将 Neon 广播到整个 SVE

```c
// 将 Neon 向量复制到 SVE 向量的所有 128 位子向量中
svint32_t sve_v = svdup_neonq_s32(neon_v);
// 如果 SVE VL = 512 位，则所有 4 个子向量包含相同的 Neon 数据
```

### 使用场景

- **增量迁移**：逐个函数将 Neon 代码转换为 SVE
- **混合算法**：前 128 位用 Neon，其余用 SVE
- **互操作性**：从 SVE 代码中调用 Neon 库函数

---
