# Go 定长切片小端批量拷贝 — 方法论与代码模板

> 源自 protobuf-go 1.36.12 `internal/impl/codec_gen.go` 的一线优化实战
> （`consumeFloatSlice` / `appendFloatPackedSlice`，鲲鹏 arm64，Go 1.25）。
> 本文给完整模板、安全性论证、验证矩阵与诚实基准协议。

## 1. 原理：为什么可以整段拷贝

protobuf wire 格式规定 Fixed32/Fixed64 一律**小端**。因此在小端机器上：

- 序列化：`[]float32` 的内存字节就是 wire 字节，`math.Float32bits` 是恒等位重排；
- 反序列化：wire 载荷字节就是 `[]float32` 的内存字节，`math.Float32frombits` 同理。

逐元素循环的成本构成：每元素一次函数调用（`AppendFixed32`/`ConsumeFixed32`，含长度边界检查）、一次位转换、一次 append 容量检查。批量拷贝后这些全部消失，只剩一次 memcpy，序列化带宽从 ~5 GB/s 提到 ~64 GB/s。

packed 布局是前提：`tag + varint(total_len) + 连续 N 个定长元素`——只有「一段连续定长数组」才能整段拷贝。unpacked 布局每元素带 tag，无法批量。

## 2. 端序门控（第一红线）

```go
var isLittleEndian = func() bool {
    var x uint32 = 1
    return (*[4]byte)(unsafe.Pointer(&x))[0] == 1
}()
```

**禁止**用 NativeEndian 与 LittleEndian 的 `==` 比较作门控：

- 直接写 `binary.NativeEndian` 与 `binary.LittleEndian` 的相等比较会**编译失败**——两者是互不相同的具体类型（`nativeEndian` 与 `littleEndian`），`==` 要求其一为接口类型或两侧可互相赋值；
- 先转成 `binary.ByteOrder` 接口再比较（例如赋给 `ByteOrder` 变量）才能编译，但 `NativeEndian` 的动态类型是独立定义的 `nativeEndian`，与 `littleEndian` 不同，即使在小端机器上比较也**恒为 `false`**；
- 后果：快路径永远不生效，且没有任何测试会失败（回退路径结果正确），收益静默归零。

字节探针是唯一可靠的一次性判定；用包级变量只求值一次，快路径分支完全可预测。

## 3. 序列化模板

原实现（protobuf-go 1.36.12 `appendFloatPackedSlice`）：

```go
b = protowire.AppendVarint(b, f.wiretag)
n := len(s) * protowire.SizeFixed32()
b = protowire.AppendVarint(b, uint64(n))
for _, v := range s {
    b = protowire.AppendFixed32(b, math.Float32bits(v))
}
```

> 目标文件若带 `// Code generated ... DO NOT EDIT` 生成头（protobuf-go `codec_gen.go` 由 `internal/cmd/generate-types` 生成），本节与第 4 节的改写必须**同步落到生成模板**（`internal/cmd/generate-types/impl.go` 的对应 coder 模板段），并重新运行生成器验证 `codec_gen.go` 与补丁一致，否则改动会在下次再生成时被覆盖。

优化后（小端一次 append，大端保留回退）：

```go
b = protowire.AppendVarint(b, f.wiretag)
n := len(s) * protowire.SizeFixed32()
b = protowire.AppendVarint(b, uint64(n))
if isLittleEndian {
    return appendFloat32SliceLE(b, s), nil
}
for _, v := range s {
    b = protowire.AppendFixed32(b, math.Float32bits(v))
}
return b, nil
```

批量 helper：

```go
func appendFloat32SliceLE(b []byte, s []float32) []byte {
    return append(b, unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s))), 4*len(s))...)
}
```

安全性论证：

- 源是 `[]float32` 底层数组（Go 分配至少 4 字节对齐），转成 `[]byte` 视图后按字节读取，任何架构无对齐问题；
- `unsafe.Slice` 生成的视图与 `s` 共享底层数组，append 期间 `s` 保持引用，GC 安全；
- `len(s)==0` 时 `SliceData` 返回 nil、`unsafe.Slice(nil, 0)` 合法返回空切片，天然安全（调用点通常已提前返回）。

性能前提：输出缓冲由调用方按 size 两阶段预分配（protobuf-go 的 size→marshal 设计），循环内 append 不再扩容——这是序列化收益能达到 ~12x 的另一半原因。

## 4. 反序列化模板（目的侧重解释）

原实现 packed 分支（`consumeFloatSlice`）：

```go
b, n := protowire.ConsumeBytes(b)          // n = 长度前缀 + 载荷总字节数
if n < 0 { return out, errDecode }
count := len(b) / protowire.SizeFixed32()
if count > 0 { p.growFloat32Slice(count) }  // 精确预扩容（新底层数组，容量 = 旧长 + count）
s := *sp
for len(b) > 0 {
    v, n := protowire.ConsumeFixed32(b)     // 注意：循环内 n 遮蔽外层 n
    if n < 0 { return out, errDecode }
    s = append(s, math.Float32frombits(v))
    b = b[n:]
}
*sp = s
out.n = n                                   // 外层 n：ConsumeBytes 的总消费数
```

优化后（快路径门控 → grow → 目的侧尾区字节视图 copy，其余输入落回原路径）：

```go
b, n := protowire.ConsumeBytes(b)
if n < 0 { return out, errDecode }
if isLittleEndian && len(b) > 0 && len(b)%protowire.SizeFixed32() == 0 {
    p.growFloat32Slice(len(b) / protowire.SizeFixed32())
    *sp = extendFloat32SliceLE(*sp, b)
    out.n = n
    return out, nil
}
// ……原逐元素循环完整保留（大端回退 + 空载荷 + 畸形长度回退）……
```

批量 helper：

```go
func extendFloat32SliceLE(s []float32, data []byte) []float32 {
    oldLen := len(s)
    s = s[:oldLen+len(data)/4]             // grow 已保证 cap 精确足够，reslice 不分配
    copy(unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s[oldLen:]))), len(data)), data)
    return s
}
```

**对齐纪律（第二红线）**：重解释的是**目的侧**——`s[oldLen:]` 位于 grow 精确分配的 float32 数组内，天然对齐，将其视为 `[]byte` 作 copy 的目的端；wire 载荷 `data` 只是 copy 的**源**，`copy` 对源字节无对齐要求（载荷在输入缓冲区内偏移任意）。

**禁止**把源重解释为标量切片：

```go
// 禁止：载荷偏移不保证 4 字节对齐，未对齐 float32 加载在部分架构上是未定义行为
s = append(s, unsafe.Slice((*float32)(unsafe.Pointer(unsafe.SliceData(b))), len(b)/4)...)
```

语义保持清单（改写前列表、改写后逐项核对）：

| 行为 | 旧路径 | 新路径 |
| --- | --- | --- |
| 载荷长度非 `wire_size` 倍数 | `growFloat32Slice` 先把字段写成非 nil 扩容切片（初始 nil 字段变 len0/cap1），循环尾部 `ConsumeFixed32` 失败 → `errDecode`，错误返回**跳过 `*sp = s`**，字段保持 grow 后的终态 | 不满足 `%wire_size == 0` 门控，**交回原路径**——错误值与字段终态（nil/非 nil、len、cap）逐项一致 |
| 空 packed 载荷 | count=0，循环零迭代，切片不变 | `len(b) > 0` 不满足，走原路径 |
| `out.n` | `ConsumeBytes` 总消费数 | 同值（快路径在外层 `n` 作用域内返回） |
| unpacked 单元素（Fixed32Type） | 逐元素路径 | 不动 |
| 其他 wire type | `errUnknown` | 不动 |
| 多 packed entry 累积 | append 语义 | grow+reslice+copy 保持 append 语义 |

## 5. 验证矩阵（需 Go 工具链，属 tier-B，可离线执行）

> 本节所有验证项都是 Go 测试（对比/基准/回归），依赖 Go 工具链；随包交付的可执行载体是 `cases/bitexact-bench/`（自包含 Go 模块，无 Go 环境时按其 case.yaml SKIP 并记录）。tier-A 契约用例见 `cases/skill-integrity/`（仅 python3 标准库，看护文档与不变量）。

测试值位模式（两路径均位精确，NaN payload 必须原样保留）：

```go
bits := []uint32{
    0x00000000, 0x80000000, // +0 / -0
    0x3F800000, 0xBF800000, // 1.0 / -1.0
    0x7F800000, 0xFF800000, // +Inf / -Inf
    0x7FC00000,             // quiet NaN
    0x7F800001, 0xFFC00001, // signaling/负 NaN payload
    0x00000001, 0x007FFFFF, // min/max subnormal
    0x7F7FFFFF,             // max normal
    0xFFFFFFFF,             // NaN payload
    // + 固定种子随机 uint32 若干
}
// fixed64 家族（float64）使用 64 位对应位模式：
// 0x7FF8000000000001（sNaN payload）、0xFFF8000000000001（负 NaN payload）、
// ±Inf、±0、subnormal、随机 uint64，全部经 math.Float64bits 比对。
```

- 序列化：新旧输出 `bytes.Equal` 逐字节一致；
- 反序列化：逐元素 `math.Float32bits` / `math.Float64bits` 比较（禁止浮点 `==`）；
- 负向（**按 `wire_size` 选边界**）：fixed32 家族非 4 倍数、fixed64 家族非 8 倍数（4 字节载荷对 fixed64 已是畸形）→ 与旧路径同样 `errDecode`，且**错误值与字段终态（nil/非 nil、len、cap）一致**；空载荷切片不变；错误 wire type → `errUnknown`；
- 回归：unpacked 输入、多条 packed entry 顺序累积、`-race`、既有全仓测试；
- 生成式回放：改 `generate-types` 模板后重新生成，`codec_gen.go` 与补丁无 diff。

## 6. A/B 基准协议（诚实口径）

- 旧实现副本**逐行复刻**原 packed 路径：`ConsumeBytes` + `count` + 精确预分配 `make([]float32, 0, count)` + 逐元素循环；
- 副本若改用 `append(nil, ...)` 会触发几何扩容重分配，把 decode 收益虚报约 2 倍（实战教训：曾报 3.7x，忠实复刻后 1.3~3.4x）；
- 多规模（16/256/4096/65536 元素）、`-count=3` 取中位数、`SetBytes` 计带宽；
- 如实记录：decode 的固定成本是 grow 精确分配（未优化、两路径相同），中规模收益收敛到 1.3~1.7x 属预期；
- 可复现载体：`cases/bitexact-bench/`（tier-B）内置同一协议的旧/新副本 A/B 基准，本地 `go test -bench` 即可复测（数字随机器浮动，验收门是位级等价与负向断言，不是加速比）。

## 7. 实测数据（鲲鹏 arm64，Go 1.25，-count=3 中位数）

序列化 `appendFloatPackedSlice`（输出缓冲预分配）：

| 元素数 | 旧 ns/op | 新 ns/op | 加速比 | 带宽 |
| ---: | ---: | ---: | ---: | --- |
| 16 | 19.0 | 8.6 | 2.2x | 7.5 GB/s |
| 256 | 211 | 24.7 | 8.5x | 41 GB/s |
| 4096 | 3192 | 268 | 11.9x | 61 GB/s |
| 65536 | 51100 | 4103 | 12.5x | 64 GB/s |

反序列化 `consumeFloatSlice` packed 分支（含每次 grow 精确分配）：

| 元素数 | 旧 ns/op | 新 ns/op | 加速比 |
| ---: | ---: | ---: | ---: |
| 16 | 176 | 59 | 3.0x |
| 256 | 898 | 667 | 1.35x |
| 4096 | 10227 | 6044 | 1.7x |
| 65536 | 145864 | 43232 | 3.4x |

负结果与边界：小切片（≤16 元素）收益收敛到 2~3x（固定开销占比高）；unpacked 序列化无法批量（tag 交错），未改动；`growFloat32Slice` 的每次精确分配是 decode 的收益天花板，属既有设计，本 skill 不改。

## 8. fixed64 家族扩展

float64/fixed64/sfixed64 同模板平移：元素宽度 8、`SizeFixed64()`、`4*len(s)`→`8*len(s)`、`len(data)/8`；校验边界相应变为 `len(b)%SizeFixed64() != 0`——**4 字节载荷对本家族已是畸形**（会漏过「非 4 倍数」检查），负向测试必须包含 4 字节反例并断言回退原路径得 `errDecode` 与字段终态一致。位级验证一律 `math.Float64bits`（NaN payload 如 `0x7FF8000000000001`）。Go 分配器保证 `[]float64` 底层数组至少 8 字节对齐，序列化侧字节视图同样安全。其余（端序探针、目的侧重解释、语义清单、验证矩阵、基准协议）完全一致。

## 9. 产物边界

本 skill 交付：可直接应用的补丁文本——改写后的目标函数（快路径 + 完整保留的逐元素回退）、批量 helper、端序探针、所需 import，目标为生成式 codec 时另含 `generate-types` 模板段的同步改写——以及验证与基准结果（JSON 契约）。改写前提：项目 `go.mod` 的 `go` 指令 ≥ 1.20（`unsafe.SliceData` 最低版本）。替换源码由调用方执行；建议同步提交位级对比测试（参照验证矩阵，或直接复用 `cases/bitexact-bench/`），并保留大端回退路径不动。
