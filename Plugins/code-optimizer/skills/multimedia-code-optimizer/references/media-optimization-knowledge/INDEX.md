# 媒体算法与通用计算优化

## 颜色与像素转换

| 算法 | 优化手段 |
|---|---|
| [RGB↔YUV](pixels.md#rgb-与-yuv-转换) | 色度乘积跨像素复用；转换与 2×2 色度采样合并；矩阵常数与偏置预计算 |
| [色度上／下采样与平面转换](pixels.md#平面布局与色度采样) | U/V 交织与解交织；色度相位处理；滤波见[多相分解](resampling.md#多相分解) |
| [位深转换与像素解包](pixels.md#位深与解包) | 位复制；宽乘加接移位窄化；解包融合后续计算 |
| [Alpha 合成](pixels.md#alpha-合成) | 颜色与 alpha 共用权重；精确舍入除 255；[预乘 Difference／Exclusion 消项](pixels.md#预乘-difference-与-exclusion-消项) |
| [Bayer 去马赛克](pixels.md#bayer-去马赛克) | 按奇偶相位分核；双线性和差复用；绿色边缘判向；RAW 处理融合 |
| [Gamma／PQ／HLG 曲线](color.md#一维曲线) | 有限输入域制表；曲线合成；非均匀采样与分段插值 |
| [3D LUT：三线性／四面体插值](color.md#三维插值) | 顶点寻址复用；通道共享权重；shaper 与立方体表分离 |

## 缩放与几何变换

| 算法 | 优化手段 |
|---|---|
| [最近邻](resampling.md#最近邻插值) | 索引预计算；重复像素按 run 广播；重复输出行复用 |
| [双线性](resampling.md#双线性插值) | 两槽横向结果缓存；权重共享；零相位删项；[二倍上采样四相位合并](resampling.md#二倍上采样的四相位合并) |
| [双三次](resampling.md#双三次插值) | 四槽行缓存；相位权重预计算；边界重复抽头合并 |
| [Lanczos](resampling.md#lanczos-插值) | 相位系数表；可分离计算；零抽头删除与对称系数复用 |
| [面积缩放](resampling.md#面积缩放) | 纵向宽和；横向前缀和或顺序归并；面积因子预计算 |
| [整数倍率缩放](resampling.md#整数倍率专用核) | 固定步长与相位专用核；box 宽累计后归一化 |
| [多相 FIR 重采样](resampling.md#多相分解) | 只计算有效抽头；预存相位周期与输入推进量；[点积后四相位插值](resampling.md#点积后四相位插值) |
| [双全通重采样](resampling.md#双全通重采样的乘积复用) | 既有全通节的输出与状态共享乘积 |
| [双线性最小二乘逆缩放](resampling.md#双线性最小二乘逆缩放) | 原逆问题的三对角 LU；局部右端项接前向消元；独立行并行回代 |
| [仿射／透视变换、畸变校正](geometry.md#坐标与采样) | 坐标递推；批量倒数；固定映射缓存；内部与边界分段 |
| [旋转与转置](geometry.md#旋转与转置) | 缓存分块与寄存器转置；按完整像素反转 |

## 滤波与图像增强

| 算法 | 优化手段 |
|---|---|
| [Box](filters.md#行环box-与行带) | 纵向列和与横向滚动和；行环；带 halo 的行带并行 |
| [Gaussian](filters.md#gaussian-滤波) | 可分离与对称抽头合并；固定核缓存；多次 box 近似 |
| [中值滤波](nonlinear.md#中值滤波) | 秩选择网络；列直方图；粗细两级查找；细桶按需更新与条带缓存 |
| [腐蚀／膨胀](nonlinear.md#腐蚀与膨胀) | 矩形分离；前后缀极值；单调队列；二值位图移位合并 |
| [双边滤波](nonlinear.md#非线性滤波与增强手段) | 空间／差值权重预计算；分子与分母共享权重 |
| [Guided Filter](nonlinear.md#guided-filter) | 共享窗口统计；缓存引导图方差倒数或协方差分解 |
| [Unsharp Mask](nonlinear.md#非线性滤波与增强手段) | 复用低通结果；融合残差增强与输出裁剪 |
| [CLAHE](nonlinear.md#clahe) | tile 私有直方图与 LUT；横向插值权重预计算 |
| [有序抖动／误差扩散](geometry.md#抖动) | 相位阈值表融合量化；两行误差缓冲 |

FIR 共用手段：[可分离、对称抽头、多输出与融合](filters.md#分解累计与融合)、[滑窗转矩阵](filters.md#滑窗转矩阵)。

## 变换与量化

| 算法 | 优化手段 |
|---|---|
| [DCT／IDCT、整数变换](transforms.md#蝶形与量化尺度) | 偶奇蝶形；归一化折入量化；全零／DC／稀疏支路专用路径 |
| [部分输出变换](transforms.md#输出裁剪) | 从目标输出反向裁剪依赖；压缩中间布局 |
| [FFT 与频域卷积](transforms.md#fft-与频域卷积) | 固定计划与核频谱复用；RFFT；overlap-add／overlap-save；[插零升采样短频谱复制](transforms.md#插零升采样的短频谱复制)；[零相位核实数乘法](transforms.md#零相位对称核的实数频谱乘法) |
| [小波](transforms.md#lifting) | lifting 预测／更新；邻行缓存；列方向并行 |
| [标量量化](quantization.md#标量量化) | 倒数乘法与商修正；死区阈值预计算；写回时同步生成位图 |
| [RDOQ](quantization.md#rdoq-增量代价) | 相邻等级失真增量；显著性代价缓存；组置零代价更新 |
| [系数扫描](quantization.md#零符号与位置位图) | 非零与符号位图；逐置位遍历；符号压紧；非零计数终止 |

## 预测与运动估计

| 算法 | 优化手段 |
|---|---|
| [DC／平面／方向预测](prediction.md#帧内与运动补偿) | DC 广播；平面行内递推；方向索引与权重预计算；[三模式变换域联合评分](metrics.md#预测模式的变换域联合评分) |
| [块匹配运动搜索](prediction.md#块匹配运动搜索) | 子块 SAD 组合；菱形／六边形新增候选；分级下界淘汰；多目标预筛 |
| [分数像素运动补偿](prediction.md#帧内与运动补偿) | 按整数／单轴／双轴相位分派；水平行缓存；双参考加权融合 |
| [Lucas–Kanade 光流](prediction.md#lucaskanade) | 固定模板梯度；结构矩阵分解复用；金字塔逐层初始化 |

## 视频重建与时域处理

| 算法 | 优化手段 |
|---|---|
| [Deblocking](loop.md#环路滤波与恢复手段) | 边界强度与阈值掩码；全无滤波跳过；弱／强滤波分核 |
| [SAO](loop.md#环路滤波与恢复手段) | band 偏移表；edge 比较符号复用；[编码端类别统计](statistics.md#小类别计数与残差) |
| [ALF](loop.md#环路滤波与恢复手段) | 按类别复用系数；对称抽头合并；裁剪邻居差值 |
| [CDEF](loop.md#环路滤波与恢复手段) | 块方向复用；固定邻域偏移；差值 constrain；[投影能量判向](loop.md#cdef-投影能量判向) |
| [Wiener／Self-guided 恢复](loop.md#环路滤波与恢复手段) | 可分离行缓存；局部和／平方和；多半径统计共享；[SGR 差分位图查表](core.md#差分位图压缩查表) |
| [时域降噪与融合](prediction.md#时域融合与去隔行) | 运动场跨通道复用；可靠性加权；遮挡参考剔除 |
| [去隔行](prediction.md#时域融合与去隔行) | 已有场行直通；缺失行滤波；内部与边缘分核 |
| [时域 IIR／有限窗口滤波](prediction.md#iir-与有限窗口) | 独立像素状态并行；环形帧引用与增减累计；[单路浮点二阶 IIR 时间展开](prediction.md#单路浮点二阶-iir-时间展开) |

## 熵编码与无损压缩

| 算法 | 优化手段 |
|---|---|
| [Huffman](huffman.md#码表与前瞻解码) | canonical 码表；主表／次级表；[长度与距离解释融合](huffman.md#表项融合长度与距离解释)；[双符号预解](huffman.md#单表项预解双符号)；[refill 计数置位](huffman.md#位缓冲计数的置位更新)；[批编码](huffman.md#幅值与批编码) |
| [CABAC](lossless.md#cabac) | 状态转移与归一化查表；延迟 refill；regular／bypass／terminate 分核 |
| [rANS／FSE](lossless.md#ans) | 状态查表；交错独立状态；压紧表项；[FSE 归一化阈值高位编码](lossless.md#fse-归一化阈值的高位编码) |
| [Rice／RLE／LZ](lossless.md#rice--rle--lz) | 一元码位扫描；变化位图生成 run；宽 XOR 匹配；短距离种子扩展 |
| [LZ 匹配搜索与解析](lossless.md#lz-匹配搜索与解析) | 最佳长度探测早筛；哈希链预算；lazy matching；有界最短路径解析 |
| [PNG 行预测重建](png.md#png-行重建) | Sub 分链前缀加；Up 行内并行；Average／Paeth 邻值复用 |
| [CRC／Adler](png.md#crc--adler) | CRC 折叠与分段合并；Adler 加权归约与延迟取模 |

## 图像度量与统计

| 算法 | 优化手段 |
|---|---|
| [SAD／SSD](metrics.md#归约与搜索) | 多候选共享源块；部分失真提前终止；组和下界；失真缓存 |
| [SATD](metrics.md#hadamard-末级消除) | 绝对值与最大值恒等式消除 Hadamard 末级；[水平／垂直／DC 联合评分](metrics.md#预测模式的变换域联合评分) |
| [SSIM](metrics.md#ssim) | 和／平方和／互乘共享；重叠窗口复用子块统计 |
| [直方图](nonlinear.md#非线性滤波与增强手段) | 线程或分块私有 bin 后归并；[少类别掩码归约](statistics.md#小类别计数与残差) |
| [积分图、局部均值／方差](statistics.md#前缀与共享统计) | 向量 scan；矩形四角查询；sum／sumsq／cross 一次读取 |

## 特征与区域分析

| 算法 | 优化手段 |
|---|---|
| [Sobel／Scharr](vision.md#梯度与描述子) | 平滑与差分分离；邻域共享；直接消费幅度或结构张量 |
| [Harris／Shi–Tomasi](vision.md#梯度与描述子) | 梯度乘积共享；积分图或可分离窗口统计 |
| [FAST／BRIEF／ORB](vision.md#梯度与描述子) | 圆周位图与早筛；采样偏移预计算；二值描述子 XOR／popcount |
| [Canny](vision.md#canny) | 免 atan2 方向判定；平方幅值；三行缓存；跨块滞后连接 |
| [连通域](vision.md#区域与候选) | 块内 union-find；边界归并；按根合并区域统计 |
| [Hough、NMS](vision.md#区域与候选) | 角度表与私有投票；稀疏点表；抑制位集 |
| [距离变换、细化](vision.md#距离与细化) | 一维抛物线下包络；邻域判定 LUT；子迭代掩码同步 |
| [Span 绘制](geometry.md#span-绘制) | 活性边递推；连续跨度写入；覆盖率与合成融合 |

## 共用计算手段

- [定点、归约、布局、循环与数据搬运](core.md)：跨算子共用；[差分位图压缩查表](core.md#差分位图压缩查表)。
- [卷积、BN 折叠、整数量化、分块 Attention](learned.md)：[像素指针展开卷积](learned.md#像素指针展开卷积)、[滑窗指针表压缩](learned.md#滑窗指针表压缩)、[四位权重高位点积](../arm-simd-optimization/arm-neon-methods.md#四位权重高位点积)。
- [ARM 指令与实现优化](../arm-simd-optimization/INDEX.md)：指令、向量化与硬件实现。
