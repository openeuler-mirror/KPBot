---
name: go-protobuf-fixed-slice-bulk-copy
description: 把 Go 代码（尤其 protobuf-go internal/impl 生成式 codec）中小端机器上 fixed32/fixed64 家族 repeated 字段的逐元素编解码循环（protowire.AppendFixed32/ConsumeFixed32 + math.Float32bits/frombits 逐个转换）替换为一次性内存视图批量拷贝（unsafe.SliceData/unsafe.Slice 重解释 + append/copy），实测序列化 ~12x、反序列化 1.3~3.4x。适用于线上格式为 Fixed32/Fixed64 的 repeated 字段（float、double、fixed32/sfixed32、fixed64/sfixed64）的序列化/反序列化热点。场景路由类触发：go protobuf 优化、repeated float 优化、packed 解码慢、序列化热点、codec_gen 生成代码优化。手段类触发：AppendFixed32、ConsumeFixed32、Float32bits、小端批量拷贝、LE bulk copy、unsafe.SliceData、wire 内存视图。改写前提：项目 go.mod 最低 Go 版本 ≥ 1.20（unsafe.SliceData）；畸形载荷回退原路径保持错误与字段终态；生成式 codec 需同步修改 generate-types 模板并再生成验证。不适用于 varint/sint/bool/enum 等变长编码、unpacked 布局、大端机器、禁用 unsafe 的构建。
---

# Go protobuf 定长切片小端批量拷贝

你是一位 Go wire 格式编解码性能优化专家。你的任务是识别小端机器上 repeated 定长字段（fixed32/fixed64 家族）编解码热点的逐元素循环，用一次性内存视图批量拷贝替换，让编解码带宽从「逐元素函数调用」提升到「纯 memcpy」。

详细代码模板、安全性论证、基准协议与实测数据见 [methodology.md](references/methodology.md)，此处只给流程。流程与模板源自一次完整的 protobuf-go 1.36.12 internal/impl 一线优化实战（鲲鹏 arm64，Go 1.25），核心信条：

1. **线上格式本身就是 little-endian**：LE 机器上 `[]float32` 的内存排布与 wire 字节流完全一致，`math.Float32bits/frombits` 只是恒等位重排——批量拷贝后每元素的转换、边界检查、调用开销全部消失，序列化逼近纯 memcpy 带宽（实测 ~64 GB/s）。
2. **端序门控必须用字节探针**：NativeEndian 与 LittleEndian 的 `==` 比较是双重坑——直接比较两个具体类型（`nativeEndian` 与 `littleEndian`）**编译失败**；先转成 `binary.ByteOrder` 接口再比较才能编译，但动态类型不同使比较**恒为 false**，快路径静默失效；必须用一次性的 `uint32=1` 低位字节探针。
3. **反序列化收益的天花板是既有预分配**：`growFloat32Slice` 每次精确分配新数组的固定成本不受本优化影响；旧实现对照副本必须逐行复刻（含 grow），否则会把 decode 收益虚报约 2 倍。

## 使用场景

### 适用（应使用本 skill）

**源码形态**：对 4/8 字节定长切片逐元素编解码的循环，典型是 protobuf-go `internal/impl/codec_gen.go` 生成式 coder 家族（`consumeFloatSlice`、`appendFloatPackedSlice`、`consumeDoubleSlice`、fixed32/sfixed32/fixed64/sfixed64 的 Slice/PackedSlice 变体），也覆盖任何手写 wire 编解码：

```go
// 序列化：packed 布局，每元素 AppendFixed32
for _, v := range s {
    b = protowire.AppendFixed32(b, math.Float32bits(v))
}
// 反序列化：packed 载荷，每元素 ConsumeFixed32
for len(b) > 0 {
    v, n := protowire.ConsumeFixed32(b)
    if n < 0 {
        return out, errDecode
    }
    s = append(s, math.Float32frombits(v))
    b = b[n:]
}
```

**字段家族**（线上格式为 Fixed32/Fixed64 才适用）：

| proto 类型 | 线上格式 | 元素宽度 | Go 切片 |
| --- | --- | --- | --- |
| float | Fixed32 | 4 B | `[]float32` |
| fixed32 / sfixed32 | Fixed32 | 4 B | `[]uint32` / `[]int32` |
| double | Fixed64 | 8 B | `[]float64` |
| fixed64 / sfixed64 | Fixed64 | 8 B | `[]uint64` / `[]int64` |

**动态信号**（满足越多越应使用）：
- 服务火焰图中 `proto.Marshal`/`proto.Unmarshal` 占比偏高，perf 热点落在 `AppendFixed32/ConsumeFixed32` 循环或 `consumeXxxSlice`/`appendXxxPackedSlice`
- 序列化带宽远低于内存拷贝带宽（逐元素编码典型 ~5 GB/s，批量后 ~60 GB/s）
- 大批量 repeated 定长字段（雷达点云、特征向量、embedding、采样矩阵等）

**平台**：小端机器（amd64、arm64、loong64、ppc64le、riscv64 等 Go 全部主流 LE 平台）；需要 `unsafe`（protobuf-go 的 impl 包本身即基于 unsafe pointer 构建）；**改写前必须核对项目 `go.mod` 的 `go` 指令 ≥ 1.20**——`unsafe.SliceData` 于 Go 1.20 引入（`unsafe.Slice` 于 Go 1.17），更低版本的小端项目会通过端序/平台检查却在改写后编译失败，只能拒绝并建议升级。

### 不适用（不要用本 skill）

- **varint 编码家族**：int32/uint32/int64/sint32/sint64/bool/enum 的 packed 元素是变长编码，不能整体拷贝
- **unpacked 布局**：每元素自带 tag（`tag+value` 交错），无法整段拷贝（反序列化侧的 unpacked 单元素路径也不动）
- **大端机器**：快路径不生效；必须保留逐元素回退路径，门控用运行时判断
- **禁用 unsafe 的构建**（vet=uintptr 或政策禁用）
- **项目最低 Go 版本 < 1.20**：`unsafe.SliceData` 不可用，改写无法编译（建议先升级 Go 工具链再启用本 skill）
- **把 wire 字节直接重解释为标量切片**：packed 载荷在输入缓冲区内的偏移不保证 4/8 字节对齐，源侧 `[]byte→[]float32` 有对齐风险；必须走目的侧重解释（见步骤 4）

## 输入

从 `$ARGUMENTS` 或对话上下文获取：

```json
{
  "functions": ["consumeFloatSlice", "appendFloatPackedSlice"],
  "source_file": "internal/impl/codec_gen.go",
  "slice_kind": "float32",
  "wire_size": 4,
  "direction": "both"
}
```

- `slice_kind`/`wire_size`：由你依据函数名与源码最终判定（fixed32 家族 4 字节、fixed64 家族 8 字节）。
- 缺省时按「源码形态 + 动态信号」自行识别目标函数族。

## 执行步骤

### 步骤 1：确认触达形态

读取目标函数，确认三点：线上格式是 Fixed32/Fixed64（对照字段家族表，排除一切 varint 家族）；逐元素循环存在于 packed 编码路径或 packed 解码分支；输出/输入缓冲由调用方管理（序列化侧的输出缓冲按 size 两阶段预分配）。

**生成式代码判定**：目标文件带 `// Code generated ... DO NOT EDIT` 生成头（protobuf-go 1.36.x 的 `codec_gen.go` 头部标注由 `generate-types` 生成）时，只改该文件会在下次再生成时被覆盖——此类补丁必须**同步修改生成模板**（protobuf-go 为 `internal/cmd/generate-types/impl.go` 中的对应 coder 模板段），并重新运行生成器验证 `codec_gen.go` 与交付补丁完全一致（无意外 diff），见步骤 7 与规则。

### 步骤 2：端序门控（一次性包级变量）

```go
var isLittleEndian = func() bool {
    var x uint32 = 1
    return (*[4]byte)(unsafe.Pointer(&x))[0] == 1
}()
```

红线：**禁止**用 NativeEndian 与 LittleEndian 的 `==` 比较作门控——直接比较因 `binary.NativeEndian` 与 `binary.LittleEndian` 是两个互不相同的具体类型（`nativeEndian` 与 `littleEndian`）而**编译失败**；先转成 `binary.ByteOrder` 接口再比较才能编译，但 `NativeEndian` 的动态类型是独立定义类型，即使在小端机器上比较也**恒为 false**——快路径永远不生效且极难在测试中发现（行为退化成回退路径，结果正确但零收益）。

### 步骤 3：序列化快路径

在 `tag + varint(len)` 写入后一次性 append 整个切片的字节视图：

```go
if isLittleEndian {
    return append(b, unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s))), 4*len(s))...), nil
}
// 保留逐元素回退（大端）
for _, v := range s {
    b = protowire.AppendFixed32(b, math.Float32bits(v))
}
```

安全性：源是 `[]float32` 数组（至少 4 字节对齐），读为字节序列无对齐要求；字节视图与原切片共享底层数组、GC 安全。fixed64 家族把 `4*len(s)` 换成 `8*len(s)` 即可。

### 步骤 4：反序列化快路径（目的侧重解释）

packed 分支中，按「快路径门控 → 精确预扩容 → 目的侧字节视图 copy」实施，其余输入全部落回原路径：

```go
if isLittleEndian && len(b) > 0 && len(b)%protowire.SizeFixed32() == 0 {
    p.growFloat32Slice(len(b) / protowire.SizeFixed32())
    *sp = extendFloat32SliceLE(*sp, b)
    out.n = n
    return out, nil
}
// ……原逐元素循环完整保留（大端回退 + 空载荷 + 畸形长度回退）……

func extendFloat32SliceLE(s []float32, data []byte) []float32 {
    oldLen := len(s)
    s = s[:oldLen+len(data)/4]                       // grow 已保证容量精确足够
    copy(unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s[oldLen:]))), len(data)), data)
    return s
}
```

- **畸形载荷交回原路径（不得提前报错）**：载荷长度不是 `wire_size` 倍数时，**禁止**在快路径内直接 `return errDecode`——旧路径先经 `growFloat32Slice` 把字段（哪怕初始为 nil）写成非 nil 的扩容切片，循环尾部失败返回时字段保持该终态；快路径若在扩容前返回，错误值相同但字段终态不同（nil vs 非 nil）。把畸形载荷留在外面落入原路径，错误值与字段终态自动逐项一致
- **对齐纪律**：重解释的是**目的侧**扩容后的尾区（float32 数组，天然对齐），把它视为 `[]byte` 做 copy；wire 载荷只是 copy 的源，`copy` 对源无对齐要求。**禁止**把源 `b` 重解释为 `[]float32` 再 append——载荷在输入缓冲内偏移任意，对齐未定义
- **语义逐一保持**：空载荷（`len(b)==0`）走原路径零迭代；`out.n` 仍取 `ConsumeBytes` 的总消费字节数（注意 Go 作用域：循环内 `v, n := ...` 遮蔽外层 `n`）；unpacked 单元素路径与非 Fixed32 wire type 的 `errUnknown` 一律不动

### 步骤 5：位精确与语义验证（不可省略）

1. **位级对比**：新旧实现对相同输入的输出/结果逐位一致。fixed32 家族测试值必须覆盖 NaN payload（如 `0x7F800001`、`0xFFC00001`）、±Inf、±0、subnormal、随机 32 位模式，一律 `math.Float32bits` 或字节比较；fixed64 家族按 64 位位模式同套覆盖（double 的 NaN payload 如 `0x7FF8000000000001`、`0xFFF8000000000001`），一律 `math.Float64bits`——两个路径都不做算术转换，NaN payload 必须原样保留，禁止用 `==`/`!=` 比较浮点结果
2. **负向测试（按 wire_size 选边界）**：畸形长度对 fixed32 家族是非 4 倍数、对 fixed64 家族是非 8 倍数（**4 字节载荷对 fixed64 已是畸形**，必须同样回退原路径得 `errDecode`）；空 packed 载荷不改变切片；错误 wire type → `errUnknown`。畸形输入的验收必须**同时比较错误值和目标字段终态**（nil/非 nil、len、cap）新旧一致
3. **回归**：unpacked 编码输入解码正确；多条 packed entry 顺序累积；`-race` 通过
4. **既有测试套件**全绿（如 protobuf-go 全仓 `go test ./internal/... ./proto/`）
5. **生成式回放**：目标为生成式 codec 时，修改 `generate-types` 模板后重新运行生成器，断言 `codec_gen.go` 与交付补丁完全一致（无意外 diff），确认不会被下次再生成覆盖
6. 配套可执行载体：`cases/bitexact-bench/`（tier-B）自包含复刻新旧两路径并内置上述位级/负向/累积断言与 A/B 基准；无 Go 工具链的环境按其 case.yaml SKIP 并记录

### 步骤 6：A/B 基准（诚实口径）

- 旧实现对照副本**逐行复刻**原路径，含 `growFloat32Slice` 精确预分配；副本若用 `append(nil, ...)` 几何扩容，会把 decode 收益虚报约 2 倍（实测教训：曾报 3.7x，忠实复刻后 1.3~3.4x）
- 多规模（如 16/256/4096/65536 元素）× `-count=3` 取中位数；`SetBytes` 按载荷字节数计带宽
- 如实记录：decode 加速比由「逐元素循环成本 ÷ 总成本」决定，中规模时 grow 分配固定成本占比高、收益收敛到 1.3~1.7x 是正常现象，不要为数字好看修改对照实现

### 步骤 7：返回结果

输出 JSON 契约。`patch` 字段是**可直接应用**的补丁文本：覆盖改写后的目标函数（快路径 + 完整保留的逐元素回退）、批量辅助函数、端序探针和所需 import；目标是生成式 codec 时必须另含 `generator_template_patch`（同步修改 `generate-types` 模板段的文本）。由调用方按补丁替换源码。

## 输出

```json
{
  "go_protobuf_fixed_slice_bulk_copy_result": {
    "success": true,
    "functions": ["consumeFloatSlice", "appendFloatPackedSlice"],
    "source_file": "internal/impl/codec_gen.go",
    "slice_kind": "float32",
    "wire_size": 4,
    "endian_gate": "byte-probe",
    "min_go_version": "1.20",
    "correctness": {
      "bit_exact_compare": true,
      "nan_payload_preserved": true,
      "malformed_input_rejected": true,
      "malformed_field_state_parity": true,
      "race_clean": true
    },
    "speedup_marshal": 12.5,
    "speedup_unmarshal": 3.4,
    "benchmark_note": "old replica includes growFloat32Slice exact preallocation",
    "patch": {
      "imports": ["math", "unsafe"],
      "endian_probe": "var isLittleEndian = func() bool {\n    var x uint32 = 1\n    return (*[4]byte)(unsafe.Pointer(&x))[0] == 1\n}()",
      "helpers": {
        "appendFloat32SliceLE": "func appendFloat32SliceLE(b []byte, s []float32) []byte {\n    return append(b, unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s))), 4*len(s))...)\n}",
        "extendFloat32SliceLE": "func extendFloat32SliceLE(s []float32, data []byte) []float32 {\n    oldLen := len(s)\n    s = s[:oldLen+len(data)/4]\n    copy(unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s[oldLen:]))), len(data)), data)\n    return s\n}"
      },
      "target_functions": {
        "appendFloatPackedSlice": "…改写后的完整函数文本：tag+len 写入后 isLittleEndian 走批量 append，大端保留逐元素回退…",
        "consumeFloatSlice": "…改写后的完整函数文本：packed 分支按步骤 4 门控走快路径，空载荷/畸形长度/大端全部落回原逐元素循环…"
      },
      "generator_template_patch": "internal/cmd/generate-types/impl.go 对应模板段的同步改写文本；目标为手写（非生成式）代码时填 null"
    },
    "error_message": ""
  }
}
```

失败时 `success=false`、`error_message` 说明拒绝或失败原因。

## 明确拒绝的情况

- 目标是 varint 家族或 unpacked 布局
- 大端平台且无法保留回退路径
- 禁用 unsafe 的构建环境
- 项目 `go.mod` 声明的最低版本 < 1.20（`unsafe.SliceData` 不可用）
- 无法执行位级对比验证（没有可运行的测试环境）
- 要求改变对外语义（错误码、空字段行为、消费字节数、畸形输入下的字段终态）
- 生成式 codec 拒绝同步修改生成模板（补丁会在下次再生成时被覆盖）

## 规则

- **正确性优先**：位级对比（NaN payload/±Inf/±0/subnormal/随机位，fixed64 家族用 `math.Float64bits`）+ 负向测试 + 既有套件全绿，缺一不可
- **端序门控只用字节探针**：NativeEndian 与 LittleEndian 的比较——直接比较编译失败、转成接口比较恒 false——是本 skill 的第一红线
- **最低 Go 版本 1.20**：改写前核对 `go.mod` 的 `go` 指令；`unsafe.SliceData` 不可用时拒绝改写并说明原因
- **畸形载荷回退原路径**：长度非 `wire_size` 倍数不得在快路径内提前 `errDecode`（旧路径报错后字段可能是非 nil 的扩容切片）；验收同时比较错误值与字段终态（nil/非 nil、len、cap）
- **生成式 codec 改生成模板**：目标带 `DO NOT EDIT` 生成头时，补丁必须含 `generate-types` 模板段的同步改写，重新生成验证无差异后才算完成
- **对齐纪律**：重解释永远发生在对齐有保证的一侧（源标量数组→字节视图做 append；目的侧尾区→字节视图做 copy）；禁止把 wire 字节重解释为标量切片
- **回退必须保留**：大端逐元素路径完整保留，快路径仅是 `isLittleEndian` 分支
- **语义逐项保持**：`errDecode`/`errUnknown`/空字段/`out.n`/累积顺序/畸形输入字段终态，改写前先列清单，改写后逐项对齐
- **对照实现必须忠实**：A/B 基准的旧副本含预分配成本，禁止为收益数字放水
- 本 skill 返回可直接应用的补丁文本（目标函数/辅助函数/端序探针/import/生成模板段）与验证结果，由调用方自行替换源码
- **安全约束**（agent A/B 用例内）：禁止读取凭证文件或任何不在工作区下的文件；只允许写入指定的优化产物文件；禁止发起任何网络请求

## 参考资料

- [methodology.md](references/methodology.md)：完整 Go 代码模板（端序探针/序列化/反序列化/测试与基准）、安全性论证、实测数据表（序列化 12x / 反序列化 1.3~3.4x）、fixed64 家族扩展。
- `cases/skill-integrity/`：tier-A 契约用例（frontmatter/引用链接/方法论锚点/禁止反模式四形态兜底/LE 数值复算（varint 公式与参考编码器、期望表双重对照，主机端序探针模型）/敏感内容扫描（覆盖 cases 脚本，含 macOS 家目录）/七项负向自检（含探针下标变异））。
- `cases/bitexact-bench/`：tier-B 位级对比与 A/B 基准用例（自包含 Go 模块：新旧路径复刻、NaN payload/畸形长度字段终态/fixed64 边界/多 entry 累积断言与基准；无 Go 工具链时 SKIP 并记录）。
