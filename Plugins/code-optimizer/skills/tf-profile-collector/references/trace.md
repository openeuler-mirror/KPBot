# op 级 trace 采集（必采）

op 级 trace 用于定位「哪个 op 耗时最多」「融合是否生效」「优化节省了多少」。与端到端压测（墙钟时间）互补：trace 给 op 累计耗时，压测给真实延迟/吞吐。

## 1. 抓 trace 流程

```bash
# 1. 启动 server（带目标优化 flag）
cd <MODEL_DIR> && setsid nohup <BIN_SERVER> \
  --flagfile=script/predictor_server.gflags \
  --enable_kdnn=true --annc_cf_matmul_batchnorm=2 --annc=true --annc_fused_matmul=true \
  > /tmp/server.log 2>&1 < /dev/null &

# 2. 等 ready（日志出现 setup logger successfully），再开 profiler
curl 'http://127.0.0.1:6001/flags/tf_enable_profiler_metedata?setvalue=true'

# 3. 压测抓 trace（QPS=1，timeout 11 约 11 请求）
cd <PRESS_DIR> && timeout 11 ./press --server=localhost:6001 \
  --input=<TEST_DATA> --qps=1

# 4. 收集 metadata（写到部署目录）
ls -t <MODEL_DIR>/metadata/   # 每请求一个 metadata_<pid>_<ts>.pb
```

关键点：
- port 固定 6001（`predictor_server.gflags`），与 benchmark 端口不同。
- trace 用 QPS=1（逐请求分析），别用高 QPS。
- metadata 写到 server 部署目录（相对路径），文件名 `metadata_<pid>_<timestamp>.pb`。
- 抓多套 trace 前，把上一套 mv 到独立目录（如 `metadata_<优化点id>`）区分版本。

## 2. 转 JSON + 聚合 op 耗时

用 base64 传 Python 脚本到远端执行（避免 expect 引号问题）：

```python
import tensorflow as tf
from tensorflow.python.client import timeline
import glob, json
from collections import defaultdict

def aggregate(files):
    agg = defaultdict(lambda: [0, 0.0])
    for f in files:
        rm = tf.compat.v1.RunMetadata()
        with open(f, 'rb') as fp:
            rm.ParseFromString(fp.read())
        tl = timeline.Timeline(rm.step_stats)
        d = json.loads(tl.generate_chrome_trace_format())
        evs = [e for e in d['traceEvents'] if e.get('ph')=='X' and e.get('dur',0)>0]
        for e in evs:
            agg[e['name']][0] += 1
            agg[e['name']][1] += e['dur']/1000.0   # ms
    return agg, len(files)

# 分别读 metadata（baseline）和 metadata_<优化点id>（opt），聚合后输出 Top N + 指定 op 对比
```

## 3. 判断优化是否生效

从 trace 判断融合是否生效，看两个信号：
1. **fused op 出现**（如 `KPFusedQKVProjection`、`_FusedMatMul`、`_FusedSigmoidMul`），且次数符合预期（= 请求数 × 融合位置数）。
2. **原 op 次数/耗时下降**（如 `MatMul`、`BatchMatMulV2` 减少），对应被融合吃掉的投影。

op 级净收益 = 原 op 减少耗时 − fused op 新增耗时（`tf-result-verify` 里做完整归因）。

## 4. 抓取时可能出现的问题

- **metadata 没产出**：server 日志有 `Profile queue is not running`（profiler_queue.cc），但最终 metadata 仍可能写出来。以 `ls -la metadata/` 的文件大小（~444KB）和修改时间为准。
- **`GPU trace was not collected`**：CPU-only 场景正常，忽略。
- **metadata 文件名 timestamp 不是抓取时间**：以文件 mtime 为准。

## 5. 踩过的坑速查

- **必须 `cd <PRESS_DIR>`**：press 用相对路径 `--input=<TEST_DATA>`。
- **分析脚本路径硬编码**：已有 `/tmp/pb2json.py`、`/tmp/compare.py` 读旧路径，使用前按实际 metadata 目录调整，或自己 base64 传新脚本。
- **server 抓完 trace 记得清理**：`pkill -9 -f predictor_server`，避免影响后续压测。

## 6. 内存布局/分配规划分析（按需）

对应「内存布局/分配规划」维度，从 `RunMetadata`（trace 已解析）进一步提取分配与内存规划信号，判断是否值得做 buffer 复用 / 布局连续化优化：

```python
# 复用 trace 的 RunMetadata（rm.step_stats），额外看：
# 1) cost graph：算子的临时 buffer 大小（bytes）与数量，找大分配/频繁分配
cg = rm.cost_graph
for node in cg.node:
    if node.temporary_memory_size > 0 or node.host_temp_memory_size > 0:
        # 记录 <op, temporary_memory_size, host_temp_memory_size>
# 2) allocator 事件：从 chrome trace 里筛选 memory 相关事件
#    （如 "OpKernelMemory"/"allocator" 命名的 memcpy/memset）
```

判断信号：

| 信号 | 含义 | 优化方向 |
|---|---|---|
| 大量小 buffer 频繁 alloc/free | BFC 碎片 / 分配开销 | 预分配、buffer 复用、arena 化 |
| 单个 op 临时 buffer 巨大 | 中间结果拷贝 | 算子融合消除中间结果、in-place |
| memcpy/memset 占比高 | 数据搬运 | 布局连续化、减少 AoS→SoA 转换 |
| 权重/激活未 prepack | 每次请求重复打包 | 离线 prepack（KDNN prepack） |

> 此维度只在「op 级 trace 显示 dispatch/memcpy 占比高」或「IPC 正常但耗时长」时定向深采，避免无谓开销。
