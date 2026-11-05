---
name: kptools-ascend-profiling
description: >
  采集 NPU 算子级与服务框架级性能数据：算子执行时间、kernel 数据、HCCL 通信
  分析、CPU-NPU 异构 trace、训练迭代分解、请求生命周期/KVCache/Batch 调度。
  当用户需要分析 torch_npu 算子热点、HCCL 通信耗时、vLLM 服务性能剖析、
  训练 step 耗时分解、Ascend PyTorch Profiler、MS Service Profiler、
  MindStudio Insight 数据采集、NPU 算子 profiling 时触发。
license: Proprietary
compatibility: aarch64 + CANN toolkit + torch_npu；需 NPU 设备
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: npu_profiling
  version: "1.0.0"
---

# kptools-ascend-profiling — NPU 算子级/服务框架级 profiling

## 工作流程

1. 检查组件能力：`bash scripts/discover.sh`（NPU 设备 / torch_npu / msserviceprofiler / vLLM 可选探测）
2. 选择方案：算子级深度分析 → 方案一（Ascend PyTorch Profiler）；服务框架流程 → 方案二（MS Service Profiler）；可并用
3. 执行采集控制（见下）
4. 离线解析：`pt-analyse` / `msp-parse`
5. 解读：配合 kptools-py-spy（CPU/Python 侧）做异构分析

## 采集命令

### 方案一（Ascend PyTorch Profiler，API 控制；服务需以 --profiler-config 启动）

```bash
# 启动采集
bash scripts/collect.sh pt-start -u http://<vllm-host>:<port> -o ./ascend-profiling-evidence/x
# ... 发送业务请求 ...
# 停止采集
bash scripts/collect.sh pt-stop -u http://<vllm-host>:<port>
# 离线解析（需 torch_npu）
bash scripts/collect.sh pt-analyse -d ./vllm_profile/*_ascend_pt/
```

### 方案二（MS Service Profiler，配置文件控制）

```bash
# 启用/停用采集开关（首次启用需以 SERVICE_PROF_CONFIG_PATH 重启服务）
bash scripts/collect.sh msp-enable -c ms_service_profiler_config.json
bash scripts/collect.sh msp-disable -c ms_service_profiler_config.json
# 解析（需 msserviceprofiler）
bash scripts/collect.sh msp-parse -d /root/.ms_server_profiler/<id>/
# 可采集 domain 清单
bash scripts/collect.sh msp-domains
```

## 输出

- stdout: 请求结果 / 产物路径 / 解析 CSV（operator_details.csv、kernel_details.csv、op_statistic.csv、step_trace_time.csv、trace_view.json；chrome_tracing.json、request.csv、kvcache.csv、batch.csv）
- stderr: 诊断与指引（服务未注入 profiler、组件缺失、首次启用需重启）
- 退出码: 0 成功；2 依赖缺失；3 参数错误；1 请求/命令失败

## 约束

- 采集依赖目标服务已按 profiler 配置启动；未注入的服务输出 --profiler-config 改造指引，本 SKILL 不自动改造启动命令
- profiling 期间 kptools-ascend-smi 设备指标失真（AICore(%) 返回 0 无意义）
- 方案一控制粒度为 API 开关；方案二 enable 开关翻转后需服务读取（部分场景需重启）
- trace_view.json / chrome_tracing.json 需 MindStudio Insight 或 Chrome Tracing 打开
- 真机（910B + CANN 8.x）命令参数差异待核对（设计文档 §0）

## 错误处理

- `NPU device: NO_ASCEND_HARDWARE` → 先用 kptools-ascend-smi 诊断驱动/设备
- `torch_npu MISSING` → 安装 torch + torch_npu（方案一）
- `msserviceprofiler MISSING` → CANN 自带或源码构建（方案二）
- `POST /start_profile 404` → 服务未注入 profiler-config，按 stderr 指引重启服务
- `vLLM 服务不可达` → 核对 URL/端口

## 参考

- [Ascend profiling 使用指南](references/ascend-profiling-guide.md)
