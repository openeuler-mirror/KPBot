#!/usr/bin/env python3
"""生成确定性的合成 torch_npu trace_view.json（chrome trace 格式，供 agent-analyze-vllm-trace 用例使用）。

输出:
  <out-dir>/trace_view.json   单行 chrome trace（格式A键序: ph,name,pid,tid,ts,dur,cat）
  <out-dir>/ground_truth.json 各问题窗口的四分类占比（%）

事件键序/字段与 ascend-vllm-profiler/scripts/build_index.py 的解析正则严格对齐:
  {"ph": "X", "name": N, "pid": P, "tid": T, "ts": "<us 3位小数字符串>", "dur": <us>, "cat": C}

时间线（us，UI 零点 = 1_000_000.000，即 ProfilerStep#1 起点）:
  step1 [0, 10ms]     设备忙闲各半: computing 纯 3.5ms + communication 1.5ms(不重叠) + free 5ms
  step2 [10, 20ms]    host bound:    computing 0.5ms + free 9.5ms, 无通信
"""
import argparse, json, os

ZERO_US = 1_000_000.000
# pids 按 SKILL.md 规范按内容识别（build_index 从 meta 动态判定，不依赖具体值）
PID_PY, PID_CANN, PID_DEV, PID_OA = 100, 200, 300, 400
TID_PY, TID_CANN, TID_COMPUTE, TID_COMM, TID_OA = 10, 20, 1, 2, 40


def us(x):
    return ZERO_US + x


def ev(name, pid, tid, ts, dur, cat, task_type=None):
    e = {"ph": "X", "name": name, "pid": pid, "tid": tid,
         "ts": f"{ts:.3f}", "dur": float(dur), "cat": cat}
    if task_type:
        e["args"] = {"Task Type": task_type}
    return e


def build_events():
    evs = []
    for pid, pname in ((PID_PY, "python"), (PID_CANN, "cann host"),
                       (PID_DEV, "device"), (PID_OA, "Overlap Analysis")):
        evs.append({"name": "process_name", "ph": "M", "pid": pid, "tid": 0,
                    "args": {"name": pname}})

    # ---- step1: ProfilerStep#1 [0, 10ms] ----
    evs.append(ev("ProfilerStep#1", PID_PY, TID_PY, us(0), 10_000.000, "user_annotation"))
    evs.append(ev("model_runner_v1.py(100): _model_forward", PID_PY, TID_PY, us(0), 10_000.000, "python_function"))
    evs.append(ev("model_runner_v1.py(120): _prepare_inputs", PID_PY, TID_PY, us(1_000.000), 2_000.000, "python_function"))
    evs.append(ev("model_runner_v1.py(300): forward_core", PID_PY, TID_PY, us(3_000.000), 6_000.000, "python_function"))
    evs.append(ev("sample.py(50): sample_tokens", PID_PY, TID_PY, us(9_000.000), 2_000.000, "python_function"))
    evs.append(ev("aten::mm", PID_PY, TID_PY, us(3_000.000), 1_000.000, "cpu_op"))
    for i, t in enumerate((3_000.000, 6_000.000, 9_000.000)):
        evs.append(ev("AscendCL@aclrtLaunchKernel", PID_CANN, TID_CANN, us(t), 30.000, "host_api"))
    evs.append(ev("AscendCL@aclrtSynchronizeDevice", PID_CANN, TID_CANN, us(9_500.000), 400.000, "host_api"))
    # 设备: 计算流 tid=1（AI 内核覆盖 [3,8]ms），通信流 tid=2（hcom 覆盖 [4,5.5]ms）
    evs.append(ev("matmul_kernel", PID_DEV, TID_COMPUTE, us(3_000.000), 3_000.000, "kernel", "AI_CORE"))
    evs.append(ev("vector_kernel", PID_DEV, TID_COMPUTE, us(6_000.000), 2_000.000, "kernel", "AI_VECTOR_CORE"))
    evs.append(ev("hcom AllReduce", PID_DEV, TID_COMM, us(4_000.000), 1_500.000, "kernel", "AI_CPU"))
    # Overlap Analysis: Computing=纯计算（扣除与通信重叠段）; Computing+CommNO+Free=100%
    evs.append(ev("Computing", PID_OA, TID_OA, us(3_000.000), 1_000.000, "OverlapAnalysis"))
    evs.append(ev("Computing", PID_OA, TID_OA, us(5_500.000), 2_500.000, "OverlapAnalysis"))
    evs.append(ev("Communication", PID_OA, TID_OA, us(4_000.000), 1_500.000, "OverlapAnalysis"))
    evs.append(ev("Communication(Not Overlapped)", PID_OA, TID_OA, us(4_000.000), 1_500.000, "OverlapAnalysis"))
    evs.append(ev("Free", PID_OA, TID_OA, us(0), 3_000.000, "OverlapAnalysis"))
    evs.append(ev("Free", PID_OA, TID_OA, us(8_000.000), 2_000.000, "OverlapAnalysis"))

    # ---- step2: ProfilerStep#2 [10, 20ms] host bound（设备几乎空闲）----
    evs.append(ev("ProfilerStep#2", PID_PY, TID_PY, us(10_000.000), 10_000.000, "user_annotation"))
    evs.append(ev("model_runner_v1.py(100): _model_forward", PID_PY, TID_PY, us(10_000.000), 10_000.000, "python_function"))
    evs.append(ev("model_runner_v1.py(120): _prepare_inputs", PID_PY, TID_PY, us(10_100.000), 8_000.000, "python_function"))
    evs.append(ev("AscendCL@aclrtLaunchKernel", PID_CANN, TID_CANN, us(10_100.000), 30.000, "host_api"))
    evs.append(ev("AscendCL@aclrtSynchronizeDevice", PID_CANN, TID_CANN, us(19_800.000), 1.500, "host_api"))
    evs.append(ev("small_kernel", PID_DEV, TID_COMPUTE, us(10_100.000), 500.000, "kernel", "AI_CORE"))
    evs.append(ev("Computing", PID_OA, TID_OA, us(10_100.000), 500.000, "OverlapAnalysis"))
    evs.append(ev("Free", PID_OA, TID_OA, us(10_000.000), 100.000, "OverlapAnalysis"))
    evs.append(ev("Free", PID_OA, TID_OA, us(10_600.000), 9_400.000, "OverlapAnalysis"))
    return evs


GROUND_TRUTH = {
    # ProfilerStep#1 窗口 [0,10ms]: computing 3.5 + comm 1.5 + free 5.0
    "step1": {"computing": 35.0, "communication": 15.0, "comm_not_overlapped": 15.0, "free": 50.0},
    # ProfilerStep#2 窗口 [10,20ms]: computing 0.5 + free 9.5
    "step2": {"computing": 5.0, "communication": 0.0, "comm_not_overlapped": 0.0, "free": 95.0},
    # forward_core 实例 [3,9ms]: computing 3.5 + comm 1.5 + free 1.0
    "forward_core": {"computing": 58.33, "communication": 25.0, "comm_not_overlapped": 25.0, "free": 16.67},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', default='input')
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    trace = json.dumps({"traceEvents": build_events()}, ensure_ascii=False, separators=(', ', ': '))
    with open(os.path.join(args.out_dir, 'trace_view.json'), 'w') as f:
        f.write(trace)
    with open(os.path.join(args.out_dir, 'ground_truth.json'), 'w') as f:
        json.dump(GROUND_TRUTH, f, ensure_ascii=False, indent=2)
    print(f"trace_view.json ({len(trace)} bytes) + ground_truth.json -> {args.out_dir}/")


if __name__ == '__main__':
    main()
