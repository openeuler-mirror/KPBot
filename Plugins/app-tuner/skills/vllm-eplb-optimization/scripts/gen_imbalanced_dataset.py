#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_imbalanced_dataset.py -- 生成用于验证 EPLB 优化效果的"不均衡"压测数据集。

原理：MoE 模型里，同样/重复的内容会反复命中同一批专家。让每条请求都携带
超长的重复文本（默认 ~100k token），专家命中就会高度集中、负载严重不均衡，
这正是 EPLB（dynamic）能优化 TTFT 的场景。混码在线形态的流量本身接近均衡，
EPLB 收益体现不出来，不能用来验证优化效果。

输出 dumpjsonl 格式（每行一个 OpenAI chat 请求 JSON），配套
benchmark_serving.py --dataset-name dumpjsonl 使用。

用法：
    python3 gen_imbalanced_dataset.py --out imbalanced_100x100k.jsonl
    python3 gen_imbalanced_dataset.py --out x.jsonl --num-requests 100 \
        --target-tokens 100000 --model dsv41
"""

import argparse
import json
import random

# 重复母本：有实际语义的长文本（中英混合，含代码/术语），保证专家激活有代表性
BASE_TEXT = """以下是一段用于负载均衡验证的技术文档片段，会在后文原样重复多次。
【系统架构】P/D 分离部署中，prefill 节点负责长上下文预填充，decode 节点负责自回归解码。
Mooncake Transfer Engine 通过 RDMA 在 P/D 之间搬运 KV Cache，分布式 KV 连接器
负责分布式 KV 存储与查找。专家并行（EP）把 MoE 的不同专家切到不同的计算设备上，
路由器（router）按 token 级 top-k 选择专家。
【核心问题】DeepSeek 类 MoE 模型各专家负载天然不均衡：当输入高度重复时，
router 反复把 token 路由到同一批专家，形成热点 rank，TTFT 被最慢的 rank 拖住。
EPLB 通过冗余专家副本与周期性重平衡摊平负载：num_redundant_experts 控制副本数，
window_size 是负载统计窗口，step_interval 是重平衡间隔。
【示例代码】
def rebalance(expert_stats, num_redundant):
    hot = sorted(expert_stats, key=lambda e: -e.load)[:num_redundant]
    return [e.replicate() for e in hot]
【压测口径】32 并发，100 条请求，每条 ~100k token 重复长前缀；跑 4 轮剔除第 1 轮
预热值，取后 3 轮 mean TTFT 均值。force EPLB（强制均衡）仅作为均衡状态下的性能
参照上限，不作为优化手段部署。
"""

def build_prompt(target_tokens, chars_per_token, rng):
    """重复母本拼接到目标长度；请求间允许细微扰动避免逐字节全同。"""
    target_chars = int(target_tokens * chars_per_token)
    parts = [BASE_TEXT]
    n = len(BASE_TEXT)
    while n < target_chars:
        if rng.random() < 0.05:
            parts.append("（第 %d 次重复，内容不变，用于制造专家命中热点）\n"
                         % (len(parts)))
            n += 30
        parts.append(BASE_TEXT)
        n += len(BASE_TEXT)
    return "".join(parts)[:target_chars]

def main():
    ap = argparse.ArgumentParser(description='生成 EPLB 验证用不均衡 dumpjsonl 数据集')
    ap.add_argument('--out', required=True, help='输出 jsonl 路径')
    ap.add_argument('--num-requests', type=int, default=100,
                    help='请求条数（默认 100，对应参考场景）')
    ap.add_argument('--target-tokens', type=int, default=100000,
                    help='每条请求目标 token 数（默认 100000）')
    ap.add_argument('--chars-per-token', type=float, default=1.5,
                    help='token->字符 估算系数（默认 1.5，中文为主；无需精确，'
                         '机制上只要够长够重复即可）')
    ap.add_argument('--model', default='dsv41', help='payload 里的 model 字段（会被 benchmark 覆盖）')
    ap.add_argument('--max-tokens', type=int, default=5, help='每条请求输出长度（默认 5，压测 TTFT 用）')
    ap.add_argument('--seed', type=int, default=1024)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    with open(args.out, 'w', encoding='utf-8') as f:
        for i in range(args.num_requests):
            payload = {
                "model": args.model,
                "max_tokens": args.max_tokens,
                "messages": [
                    {"role": "user",
                     "content": build_prompt(args.target_tokens, args.chars_per_token, rng)}
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')
    est = args.target_tokens
    print('OK: 已生成 %s（%d 条，每条约 %d token 重复长前缀）' %
          (args.out, args.num_requests, est))
    print('口径参考: 32 并发跑该数据集，4 轮剔除第 1 轮，取后 3 轮 mean TTFT 均值')
    print('对比配置: 关闭EPLB（基线） vs dynamic EPLB（优化） vs force EPLB（均衡参照上限）')

if __name__ == '__main__':
    main()
