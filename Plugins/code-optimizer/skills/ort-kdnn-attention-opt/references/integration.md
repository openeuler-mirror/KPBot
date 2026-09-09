# ORT 集成:新增一个 KDNN 融合算子的完整 checklist

Phase 3 使用。假设代码库里已有 KDNN 域接入(或按 §0 从零接入);行号以本仓库 kdnn_* 系列实现为参照。

## 0. 一次性:引入 KDNN 域/库(若库里还没有任何 com.kdnn 算子)

| 文件 | 作用 |
|---|---|
| `include/onnxruntime/core/graph/constants.h` | `constexpr const char* kKdnnDomain = "com.kdnn.internal";`(私有域,不占 MS 域,不承诺 ONNX 兼容) |
| `cmake/CMakeLists.txt` | `option(onnxruntime_USE_KDNN ...)`;要求 contrib ops 开启;`KDNN_ROOT=core/kdnn`,`KDNN_INCLUDE_DIR=out/include`,`KDNN_LIBRARY=out/lib/libkdnn.so`,不存在则 FATAL_ERROR;`add_library(onnxruntime_kdnn SHARED IMPORTED GLOBAL)`;`add_compile_definitions(USE_KDNN=1)` |
| `cmake/onnxruntime_providers_cpu.cmake` | `target_include_directories(onnxruntime_providers PRIVATE ${KDNN_INCLUDE_DIR})` |
| `onnxruntime/core/session/environment.cc` | `domainToVersionRangeInstance.AddDomainToVersion(kKdnnDomain, 1, 1);`(在 call_once 块内,缺了图解析报 unknown domain) |

KDNN 必须先构建并 install 到 `onnxruntime/core/kdnn/out`(见 benchmarking.md §1)。

## 1. 每个新算子:7 处改动

1. **图变换 pass** `onnxruntime/core/optimizer/kdnn_<name>_fusion.{h,cc}`:
   `GraphTransformer` 子类,整个匹配+改写逻辑放匿名 namespace。锚点 Softmax,自顶向下匹配
   (结构见 pattern-recognition.md §1-2)。
2. **pass 注册** `onnxruntime/core/optimizer/graph_transformer_utils.cc`:
   `GenerateTransformers` 的 `case TransformerLevel::Level1:` 内、`#if defined(USE_KDNN)` 下
   `transformers.emplace_back(std::make_unique<KdnnXxxFusion>(no_limit_empty_ep_list));`。
   **位置语义**:必须排在 rule-based 通用改写和 Level2 的通用 AttentionFusion 之前——先于通用
   pass 抢到 tf2onnx 原始结构。**一个已知变体**(消融实验中验证可行):注册在 Level1 的
   ConstantFolding **之后**,可以吃到已被折叠成常量的 mask 链(直接读 initializer 值,全零 bias
   可安全丢弃)——代价是对 pass 顺序的耦合,更稳的做法是 pass 内自带小型常量求值器;若依赖
   顺序,务必同时支持"运行时 mask 生产节点保留接入"的兜底路径并测试覆盖。
3. **op schema** `onnxruntime/core/graph/contrib_ops/contrib_defs.cc`:
   `#if defined(USE_KDNN)` 内 `ONNX_CONTRIB_OPERATOR_SCHEMA(Xxx).SetDomain(kKdnnDomain).SinceVersion(1)`
   + Attr/Input/Output/TypeConstraint + **手写 TypeAndShapeInferenceFunction**(batch 维多向广播合并)。
4. **domain 版本**:见 §0 environment.cc(同域多算子只此一次)。
5. **kernel 实现** `onnxruntime/contrib_ops/cpu/kdnn/<name>.{h,cc}`:
   整个 .cc 包 `#if defined(USE_KDNN)`;`ONNX_OPERATOR_KERNEL_EX(Xxx, kKdnnDomain, 1,
   kCpuExecutionProvider, KernelDefBuilder().TypeConstraint("T", float), Xxx)`;ctor 读属性,
   `Compute(OpKernelContext*)` 实现并包 try/catch(KDNN 抛异常要转 ORT Status)。
6. **kernel 注册** `onnxruntime/contrib_ops/cpu/cpu_contrib_kernels.cc` **两处**:
   前向声明 `ONNX_OPERATOR_KERNEL_CLASS_NAME(...)`(不必 include 头文件,链接期绑定)+
   create-info 列表 `BuildKernelCreateInfo<...>`。
7. **测试** `onnxruntime/test/optimizer/kdnn_<name>_fusion_test.cc`(写法见 validation.md)。

**CMake:每个新算子零改动**——optimizer/graph/providers_cpu/test 四处源码树全是 glob 收集,新文件
自动编译。例外:minimal build 的 optimizer 清单是显式的。

## 2. 环境变量门控(三级时机,全部 raw getenv 不做 static 缓存)

| 改什么 | 读取时机 | 模式 |
|---|---|---|
| 图结构(融合开关) | **每次 session 创建**(ApplyImpl 首行) | `e != nullptr && e[0]!='\0' && e[0]!='0'`;关闭时 `return Status::OK();` 保持原图 |
| kernel 算法路径 | **每次 Compute()** | 字符串枚举匹配,**未知值静默回退默认**(A/B 安全);同 session 可中途切换 |
| 重型一次性状态(prepack) | kernel 构造函数一次 | session 生命周期内固定 |

为什么用 env 而不是 SessionOptions key:一份二进制即可跑 baseline/candidate 双 session A/B
(benchmark 里正是用 RAII setenv 包住 session 构造),测试里用 `ScopedEnvironmentVariables`
(`onnxruntime/test/util/include/scoped_env_vars.h`)成对出现——**每引入一个 gate,同步引入一个
scoped-env 单测**。注意:进程级状态是它的代价,靠"session 创建期读"缩小影响面;融合开关必须在
session 初始化**前**设置。

## 3. matcher 实现要点(两次踩坑换来的纪律)

- **删除纪律**:每个被吞节点先 `CanRemoveSingleConsumerNode`(`!NodeProducesGraphOutput &&
  CheckOutputEdges(graph, node, 1)`),不满足就 `continue` 放弃整个块——删共享节点会静默断流。
- **重连纪律**:先收集全部 out-edge 再改写(`ReplaceNodeInput` 会使边迭代器失效)。
- **显式删死代码**:被穿过的 Transpose、被替代的旧 mask 链必须 pass 内显式 drop——ORT Level1 的
  DCE 不可靠地收集它们;死 4D Transpose 实测 ~5.7ms/call。
- **插入节点必须继承 EP**:`SetExecutionProviderType(ep)`(被替换节点的),否则可能被派去别的
  provider。
- **mask 重建**:从原始常量和 pre-Tile keepmask 重建干净 `[B|1,1,Sk]` bias,与未融合图 bit-identical。
- **scale 省略规则**:图上 scale==1/sqrt(d)(相对误差<1e-6)时省略属性、kernel 运行时推导——两边
  用同一个公式,保证 bit-exact。
- 完成后打 INFO 日志(fused N blocks)当冒烟信号。

## 4. kernel Compute() 防御性校验顺序(逐层递进,全部 ORT_RETURN_IF_NOT)

1. 输入非空(Q/K/V 必填,mask 可选);rank-3 `[B,S,hidden]` 或 rank-4 `[B,S,H,d]` 且匹配;
2. rank-4 时 `Sq==1`、`dims[2]==num_heads`、head_dim 一致;rank-3 时 `hidden % num_heads == 0`;
3. batch 广播:自写 `broadcast_batch(lhs,rhs,out)`(相等/一方为 1),Q/K→score,再与 mask、V 依次
   合并,输出 batch;不可广播报错;
4. mask:最后维==Sk、中间维全 1;`mask_batch_stride = (mask_batch==1) ? 0 : Sk`;
5. 先调库的静态 `ValidateInput`(非抛异常版本);
6. 输出 shape(rank-4 `[B,H,Sq,d]` / rank-3 `[B,Sq,hidden]`);`output_batch==0 || Sq==0` 早退;
7. 每 Compute 读一次 options(所有 batch task 用同一算法);
8. `TryBatchParallelFor` 按 batch 并行,lambda 内 `RunMha(1, Sq, Sk, ...)`,Q/K/V 指针按各自
   batch stride 偏移(0=广播);
9. **异常运输**:`std::atomic<bool> have_exception` + `std::exception_ptr`,循环内 catch 后
   `exchange(true)` 只留第一个,循环外 `rethrow_exception`——TryBatchParallelFor 的 lambda 不能抛。
10. `RunMha` 内:构造 primitive(靠 cache 摊销)+ `thread_local std::vector<float> workspace`
    grow-only(`(GetWorkspaceSize()+3)/4` 元素)。

## 5. 私有域 vs MS 域

新 KDNN 算子统一走 `kKdnnDomain`(`ONNX_CONTRIB_OPERATOR_SCHEMA(...).SetDomain(kKdnnDomain)`)。
历史包袱:`ORT_ENABLE_FUSED_TENSORDOT_MATMUL` 用了 `ORT_ENABLE_` 前缀、算子落 kMSDomain——新代码
统一 `ORT_KDNN_*` 前缀 + 私有域。**保存 ORT format(.ort)模型会把融合算子烧进文件**,非对应 build
加载即失败且无回退——交付前想清楚目标环境。
