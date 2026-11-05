#!/usr/bin/env bash
# 校验 agent 是否按场景四要素与决策表产出正确的 nginx.conf 关键决策。
# 检查项全部来自 skill 文档中经真机实测验证的决策内容。
set -uo pipefail

cd /workspace
mkdir -p results

python3 - <<'PY'
import json
import os
import re

text = ""
if os.path.exists("results/decisions.md"):
    text = open("results/decisions.md", encoding="utf-8", errors="replace").read()
low = text.lower()

checks = []

# 场景判定：识别为 https_short（题面：HTTPS 网关、短连接、4 核 VM）
def has(*pats):
    return any(re.search(p, low) for p in pats)

# 取值级校验：错误决策（200000/shared/443+reuseport/worker=3/仅1.2/选用RSA/误判长连接）必须判负，
# 不允许靠关键词存在性得分（检视 P2：断言不得退化为关键词匹配）
checks.append(("scene_https_short",
               has(r"https[_ -]?short", r"(判定|识别|场景)[^。\n]*短连接") and "https" in low
               and not re.search(r"(判定|识别|场景为|属于)[^。\n]*(长连接|https[_ -]?long)", low)))
# worker 数：short = N = 4（不是 N-1）——要求对 4 的肯定取值
checks.append(("worker_n_not_n_minus_1",
               bool(re.search(r"worker_processes?\s*[:=为是]?\s*4(?![0-9])", low))
               or bool(re.search(r"worker\s*(数|数取|数配|数设置)?\s*(为|=|:)\s*4(?![0-9])", low))))
# ssl_session_cache：短连接 off（排除 shared/on 等错误取值；tickets off 不得替代本项）
checks.append(("ssl_session_cache_off",
               bool(re.search(r"ssl_session_cache\s+off", low))
               and not re.search(r"ssl_session_cache\s+(shared|on|builtin)", low)))
# keepalive_requests：短连接量级 2000（负向断言排除 200000）
checks.append(("keepalive_requests_short",
               bool(re.search(r"keepalive_requests\s*[:=]?\s*2000(?![0-9])", low))))
# reuseport：https_short 的正确决策是 443 ssl 不加 reuseport——
# 肯定语义（443 ssl 监听或显式不加表述）+ 否定错误取值（443 ssl 行直接挂 reuseport）
checks.append(("reuseport_https_short_off_on_443",
               (bool(re.search(r"listen\s+443\s+ssl", low))
                or bool(re.search(r"(不加|不使用|不启用|不开启|不用)[^。\n]{0,30}reuseport", low)))
               and not re.search(r"listen\s+443\s+ssl\s+reuseport", low)))
# ECDSA 证书优先（排除"使用/选用 RSA"的错误取值）
checks.append(("ecdsa_cert",
               has(r"ecdsa", r"ecc[^。\n]{0,6}证书")
               and not re.search(r"(使用|选用|选择|改回)\s*rsa", low)))
# TLS 协议：必须同时覆盖 1.2 与 1.3（决策表"只留 1.2/1.3"）
checks.append(("tls_1_2_1_3",
               has(r"tls\s*v?1\.2[^。\n]*1\.3", r"tls\s*v?1\.3", r"1\.2\s*/\s*1\.3")
               and bool(re.search(r"1\.2", low)) and bool(re.search(r"1\.3", low))))
# 绑核：二进制掩码语义（核0=1、核1=10；或明确写"二进制"）
checks.append(("affinity_binary_mask",
               bool(re.search(r"1\s+10\s+100\s+1000", text))
               or "二进制" in text
               or bool(re.search(r"100000000", text))))
# 压测纪律：多轮均值
checks.append(("benchmark_rounds", has(r"3\s*轮", r"三轮", r"≥\s*3", r"多轮", r"均值")))

passed = sum(1 for _, ok in checks if ok)
detail = [{"check": n, "pass": bool(ok)} for n, ok in checks]

result = {
    "success": passed == len(checks),
    "passed": passed,
    "total": len(checks),
    "checks": detail,
    "artifact": "results/decisions.md",
}
json.dump(result, open("results/result.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False))
PY
