## 6. 函数多版本化

FMV 让编译器为不同硬件生成多个函数版本，并自动进行运行时选择。

### 基本用法

```c
// 编译器生成 3 个版本，运行时选择最佳版本
__attribute__((target_clones("sve", "simd", "default")))
void process(float *data, int n) {
    for (int i = 0; i < n; i++) {
        data[i] = data[i] * 2.0f + 1.0f;
    }
}
// 编译器使用相应的 ISA 对每个版本进行自动向量化
```

### 手动版本控制

```c
// 显式版本
int __attribute__((target_version("default"))) compute(int n) {
    return scalar_compute(n);
}

int __attribute__((target_version("simd"))) compute(int n) {
    return neon_compute(n);
}

int __attribute__((target_version("sve"))) compute(int n) {
    return sve_compute(n);
}

// 调用方直接调用 compute() — 运行时自动分发
```

### 目标版本字符串语法

**形式化语法：**
```
<target version string> := 'default'
                         | <version string>
<version string>        := <arch strings> ';' <priority string>
                         | <arch strings>
<priority string>       := 'priority=[1-255]'
<arch strings>          := <arch strings> '+' <arch extension>
                         | <arch extension>
```

其中 `<arch extension>` 是 FMV 映射表中的特性 **Name**（不是原始的 `-march` 标志）。

**示例：**
```
default                    # 回退版本
simd                       # Neon SIMD
sve                        # SVE
sve2                       # SVE2
dotprod                    # 点积扩展
dotprod+flagm              # 组合特性
sve;priority=5             # 带显式优先级
sve2+sme2;priority=23      # 组合特性并指定优先级
```

**优先级语义：**
- 优先级值范围从 1 到 255（越高越优先）
- 如果两个版本都指定了 `priority`，值高的胜出
- 如果只有一个指定了 `priority`，它具有优先权
- 如果都没有指定 `priority`（或值相同），优先权由**优先级最高的不同特性**
  决定（按映射表中的顺序）

**特性依赖关系（AArch64）— 被依赖的特性无需列出：**

| 特性 | 依赖于 |
|------|--------|
| `simd` | `fp` |
| `dotprod` | `simd` |
| `bf16` | `simd` |
| `i8mm` | `simd` |
| `sve` | `fp16` |
| `sve2` | `sve` |
| `sme` | `fp16`、`bf16` |
| `sme2` | `sme` |
| `sha3` | `sha2` |
| `fp16fml` | `simd`、`fp16` |

如果你写 `target_version("sve2")`，编译器会隐式要求 `fp16`（通过 `sve`）— 不需要列出它们。

### 规则

1. **必须提供 `default` 版本** — 当没有特定版本匹配时使用
2. **所有版本必须具有相同的签名**（返回类型、参数）
3. **默认声明必须在调用点可见**
4. **不能将 `target_clones` 与其他克隆属性混用**
5. **特性字符串来自 FMV 映射表**（不是原始的 `-march` 标志）
6. **选择对进程的生命周期是永久的**（在加载时决定）
7. **所有函数版本必须在同一作用域层级声明**
8. **无法识别的特性名称会被编译器忽略**（支持向前兼容 — 新代码可以用旧编译器编译）
9. **特性字符串在修饰名中按字典序排序**，重复项会被移除

---
