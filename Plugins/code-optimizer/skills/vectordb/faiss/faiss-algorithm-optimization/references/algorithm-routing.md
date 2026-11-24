# 算法路由

| 索引/任务 | 技能 | 能力确认 |
| --- | --- | --- |
| IndexRaBitQ / IndexIVFRaBitQ | faiss-rabitq-optimization | 检查实际索引、位宽和变体 |
| TurboQuant / QT_4bit_tq | faiss-turboquant-optimization | 检查枚举、quantizer 和距离路径 |
| IndexIVFPQ | faiss-ivfpq-optimization | 普通 PQ 表、decoder 与倒排扫描 |
| IndexIVFPQFastScan / pqfs | faiss-ivfpqfs-optimization | packed block、整数表和分派 |
| IndexHNSW* | faiss-hnsw-optimization | 图结构、storage 和候选/visited |

以实际创建的类、源码和用户目标确定归属，不按某份工程的版本或后端绑定算法。其他索引可用公共源码/采样方法调查；CP0–CP7 状态协议未支持的算法不能伪装成表中类型建立实验。
