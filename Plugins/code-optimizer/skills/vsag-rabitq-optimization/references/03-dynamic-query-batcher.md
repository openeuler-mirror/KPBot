# 阶段 3：DynamicQueryBatcher 实现

实现并发查询的预处理组批。固定长生命周期 worker 只负责 batch 形成和共享 ROM 变换，不串行执行完整检索。

## 执行模型

```text
并发 KnnSearch 调用者
        ↓ enqueue
DynamicQueryBatcher 队列
        ↓ batch size 或 timeout
固定 worker 执行共享 ROM 变换
        ↓ completion/exception
各调用者完成 normalize、quantize、LUT
        ↓
各调用者独立恢复 HGraph traversal
```

当前 batch 计算时队列仍须接收下一批请求，形成流水。避免 worker 在执行 SGEMM 或后处理时长期持有队列锁。

## 状态与生命周期约束

- deferred state 保留 query lifetime、PCA 输出、变换行、原始/变换维度、query stride、MRQ residual norm 和 caller index。
- 每个请求必须收到完成信号或原始异常；异常不得只终止 worker 而遗留阻塞调用者。
- shutdown/析构应排空或安全取消队列、唤醒等待者并 join worker。
- singleton、FHT、transform quantizer、batch disabled 和不满足组批条件时走正确 fallback。
- 环境变量解析覆盖 unset、empty、malformed、zero 和 minimum clamp。

## 并发约束

- batch size 和 gather timeout 是延迟/吞吐控制量，不是通用常量。
- 不为每个小 batch 创建 OpenMP team；BLAS 线程由阶段 6 联合调优。
- 队列锁、完成通知和异常传递必须有并发测试覆盖。

阶段 3 可以独立迭代，但每次修改后必须进入阶段 5，不可直接引用旧正确性结果。


