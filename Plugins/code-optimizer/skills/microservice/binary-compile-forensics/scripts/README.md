# scripts/inspect_build_flags.sh

二进制/库编译参数产物反推（纯 bash + binutils，只读）。

## 用法

```bash
# 单文件（静态库自动抽首成员）
bash scripts/inspect_build_flags.sh /path/to/libfoo.a

# 多文件对照
bash scripts/inspect_build_flags.sh app.bin libbar.so libbaz.a

# 静态库逐成员全查（定位混链构建的污染成员；大库耗时）
bash scripts/inspect_build_flags.sh -a /path/to/libfoo.a
```

## 输出契约

| 输出 | 含义 |
|------|------|
| `[编译器版本]` | .comment 段串；**多版本并用 = 混链构建**（本身就是取证发现） |
| `[实际编译参数]` | DW_AT_producer 逐条 + 解析（优化等级/目标架构/其他）；注明"仅前 5 个编译单元，未覆盖为未知"；无 -g 时如实降级标注并建议按 build ID 查找分离 debuginfo |
| `[SIMD 兜底探测]` | SVE/NEON 寄存器指令采样计数与判定（60s 超时保护）；零命中标"样本中未见" |
| `结论:` 行 | `完成取证文件数/总文件数` + `取得指纹文件数` 两个独立计数 |

## 退出码

| 码 | 语义 |
|----|------|
| 0 | 全部文件完成取证（含部分链降级——降级不是失败） |
| 4 | 部分文件完成（部分失败——stderr 有各文件失败原因） |
| 1 | 没有任何文件完成取证（三条链均无结论：路径全错/格式全不支持/全非 ELF） |
| 2 | 用法错误（无参数） |
| 3 | 依赖工具缺失（readelf/objdump/ar——环境前置问题） |

## 实测形态（有真实输入验证）

- .comment 多版本串混出（混链构建实证）
- DW_AT_producer 参数完整指纹（含 -O2 -mcpu=tsv110 -g）+ "仅前 5 编译单元"限定
- release 无 debug info → 建议按 build ID 查找分离 debuginfo → 自动降级 SIMD 采样
- SIMD 零命中 → "样本中未见"（不证伪启用）
- 静态库 .a 首成员抽取（-a 逐成员全查并标出成员名）
- 三条链均无结论的文件 → 不计入"完成取证"

## 边界

- 只认 ELF（含 .a/.so/可执行/重定位 .o）；其他格式三条链均无结论、计为未完成
- DW_AT_producer 只抽前 5 条去重——未覆盖的编译单元参数为未知
- 采样只取反汇编前 30 万行——是速度与覆盖的折中（全量采样不改变"样本中未见不证伪"的判定语义，但会增加耗时）
- 不执行被检文件、不写被检文件、不联网
