# Trace 抓取 SOP（脱敏）

> 脱敏自 trace 抓取记录。`<MODEL_NAME>` 模型（port 6001）的 op 级耗时 trace（RunMetadata）抓取标准流程。

## 一、环境信息（占位符）

| 项 | 值 |
|---|---|
| 目标服务器 | `<TARGET_IP>`（鲲鹏 aarch64） |
| 部署目录 (MODEL_DIR) | `<MODEL_DIR>` |
| gflags | `script/predictor_server.gflags`（`-port=6001`、`-num_threads=30`、inter/intra=16） |
| trace 输出目录 | `<MODEL_DIR>/metadata/` |
| server 二进制 (opt) | `<BIN_SERVER_OPT>` |
| server 二进制 (baseline) | `<BIN_SERVER_BASELINE>` |
| 压测客户端 | `<PRESS>` |
| 测试数据 | `<TEST_DATA>` |

## 二、运行模式

| 模式 | 附加 flag |
|---|---|
| baseline | 无加速 flag |
| opt | `--enable_kdnn=true --annc_cf_matmul_batchnorm=2 --annc=true --annc_fused_matmul=true` |

## 三、抓取流程

```bash
# 1. 启动 server（opt 示例）
cd <MODEL_DIR> && nohup <BIN_SERVER_OPT> \
  --flagfile=script/predictor_server.gflags \
  --enable_kdnn=true --annc_cf_matmul_batchnorm=2 --annc=true --annc_fused_matmul=true \
  > /tmp/server_opt.log 2>&1 &

# 2. 开启 profiler
curl 'http://127.0.0.1:6001/flags/tf_enable_profiler_metedata?setvalue=true'

# 3. 压测抓 trace（QPS=1，11 请求）
cd <PRESS_DIR> && timeout 11 ./press --server=localhost:6001 --input=<TEST_DATA> --qps=1

# 4. 收集 metadata
ls -t <MODEL_DIR>/metadata/   # 每请求一个 metadata_<pid>_<timestamp>.pb
```

> 通过 `remote-exec.ps1` 启动时需用 `nohup ... &` + `setsid`，否则 expect ssh 断连会杀掉后台 server。

## 四、分析流程

```bash
python3 /tmp/pb2json.py    # RunMetadata .pb → timeline → Chrome-trace JSON
python3 /tmp/compare.py    # 聚合 op 耗时，baseline vs opt 对比
```

## 五、关键注意点

- 端口固定 6001（与 benchmark 端口不同）。
- trace 用 QPS=1（逐请求分析），`timeout 11` ≈ 11 个 trace。
- 分析脚本 `/tmp/pb2json.py`、`/tmp/compare.py` 内部硬编码旧路径，使用前按实际 metadata 目录调整 glob 路径。
- 抓 baseline/opt 两套 trace 后分别转 JSON，再 `compare.py` 对比（baseline 读 `metadata_baseline/`）。
