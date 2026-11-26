# PNG 重建与校验手段

## PNG 行重建

以滤波字节步长 bpp 分派，逐字节结果模 256：

| 滤波器 | 实现 |
|---|---|
| None | 复制或直接交给消费者 |
| Up | 滤波字节与上一重建行同位置相加，行内 SIMD |
| Sub | 按 `index mod bpp` 分链做前缀加；向量内用 bpp、2bpp、4bpp…偏移相加，再加块前 bpp 个重建字节的链 carry |
| Average | 复用上一行装载，各 bpp 链按序计算 `floor((left+up)/2)` |
| Paeth | 缓存左 a、上 b、左上 c；用下式选择预测器，再与滤波字节相加模 256 |

Paeth 在扩宽有符号类型中计算：

```text
p=a+b-c; pa=abs(p-a); pb=abs(p-b); pc=abs(p-c)
pred = a if pa<=pb and pa<=pc else (b if pb<=pc else c)
```

首行上邻、每行缺失左邻取零；交错图每个 pass 单独初始化。Sub 的链 carry 对应上一块末尾各余数类，块长变化时按全局余数映射。

## CRC / Adler

CRC 按目标多项式选 slicing 表、硬件 CRC 或 carry-less multiply 折叠；分段合并按右段长度乘对应的 GF(2) 因子，再组合状态。反射、初值和最终异或与目标格式一致。

Adler-32 分块累加 `s1+=byte; s2+=s1`，在累加器溢出前统一模 65521。块长 n 的贡献可写成 `s1'=s1+Σb[i]`、`s2'=s2+n*s1+Σ(n-i)b[i]`，i 从 0 起，便于分链加权归约。
