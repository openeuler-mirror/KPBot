# TurboQuant 算法与源码导航

## 能力确认

搜索目标源码实际提供的量化器枚举、类型和测试注册，例如 `QT_4bit_tq` 或相关 TurboQuant 类。未提供时说明能力缺口，不将其他实现的路径当作当前源码。

## 路径拆分

在采用 MSE/QJL 组合的实现中，分别追踪：
1. 训练与模型参数：旋转/投影、centroid、缩放和校正因子。
2. 编码：MSE 索引、可选 QJL 位、向量范数及因子的保存顺序。
3. 查询预处理：query 变换、范数、投影和可选量化位平面。
4. 距离估计：码字解码、centroid 查表、QJL 校正及误差界处理。
5. 索引扫描、候选选择与可选精排。

## 源码定位

可从 `IndexIVFScalarQuantizer*`、`ScalarQuantizer*` 进入，搜索实际 quantizer/DistanceComputer 的 `set_query`、`query_to_code` 或等价接口。文件是否位于 `quantizers.h`、`sq-dispatch.h` 或其他目录，以检出内容为准。

## 分派与扩展

单独核对查询位宽、整数/浮点校正模式、位平面与缓存布局、SIMD 路径和阈值筛选。缓存字段、固定维度特化或宏列表属于具体实现，使用前检查构建定义和调用条件，不预置为通用能力。
