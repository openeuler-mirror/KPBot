#!/usr/bin/env python3
"""Generate or validate an OpenViking ov.conf.

The API key is read from a file and never printed. Generated files are written
with mode 0640; the installer adjusts ownership to root:<service group>.
"""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

PROVIDER_DEFAULTS = {
    "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
    "openai": "https://api.openai.com/v1",
    "ollama": "http://127.0.0.1:11434",
    "azure": "",
}

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def fail(message):
    print("[FAIL] " + message, file=sys.stderr)
    sys.exit(2)


def read_secret(path):
    p = Path(path)
    if not p.is_file():
        fail("密钥文件不存在: {}".format(path))
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        fail("密钥文件权限过宽（{:o}），请执行: chmod 600 {}".format(mode, path))
    value = p.read_text(encoding="utf-8").splitlines()
    if not value or not value[0].strip():
        fail("密钥文件首行为空: {}".format(path))
    return value[0].strip()


def validate(doc):
    problems = []
    if not isinstance(doc, dict):
        return ["配置根节点必须是 JSON 对象"]
    embedding = doc.get("embedding")
    if not isinstance(embedding, dict) or not isinstance(embedding.get("dense"), dict):
        problems.append("缺少 embedding.dense")
    else:
        dense = embedding["dense"]
        for field in ("provider", "model"):
            if not dense.get(field):
                problems.append("embedding.dense.{} 缺失".format(field))
        if not dense.get("api_key") and dense.get("provider") != "ollama":
            problems.append("embedding.dense.api_key 缺失（ollama 可为空）")
    vlm = doc.get("vlm")
    if not isinstance(vlm, dict):
        problems.append("缺少 vlm")
    else:
        for field in ("provider", "model"):
            if not vlm.get(field):
                problems.append("vlm.{} 缺失".format(field))
        if not vlm.get("api_key") and vlm.get("provider") not in ("ollama", "openai-codex"):
            problems.append("vlm.api_key 缺失（ollama/openai-codex 可为空）")
    storage = doc.get("storage")
    if not isinstance(storage, dict) or not storage.get("workspace"):
        problems.append("缺少 storage.workspace")
    return problems


def cmd_check(args):
    try:
        doc = json.loads(Path(args.check).read_text(encoding="utf-8"))
    except OSError as exc:
        fail("无法读取配置: {}".format(exc))
    except ValueError as exc:
        fail("配置不是合法 JSON: {}".format(exc))
    problems = validate(doc)
    if problems:
        for item in problems:
            print("[FAIL] " + item, file=sys.stderr)
        sys.exit(2)
    print("[ OK ] ov.conf 必需字段检查通过")
    return 0


def cmd_generate(args):
    if args.provider not in PROVIDER_DEFAULTS:
        fail(
            "共享参数只支持 volcengine/openai/azure/ollama；"
            "混合或特殊 provider 请用 --ov-conf 接管已有配置"
        )
    if not args.embedding_model or not args.vlm_model:
        fail("生成配置必须提供 --embedding-model 与 --vlm-model；不要猜测模型名")
    if not args.workspace:
        fail("生成配置必须提供 --workspace")
    api_base = args.api_base or PROVIDER_DEFAULTS[args.provider]
    if not api_base:
        fail("provider {} 必须显式提供 --api-base".format(args.provider))

    api_key = ""
    if args.provider != "ollama":
        if not args.api_key_file:
            fail("provider {} 需要 --api-key-file（0600），拒绝写入占位 key".format(args.provider))
        api_key = read_secret(args.api_key_file)

    root_api_key = ""
    if args.root_api_key_file:
        root_api_key = read_secret(args.root_api_key_file)
    if args.host not in LOOPBACK and not root_api_key:
        fail("监听 {} 不是回环地址，必须提供 --root-api-key-file 开启鉴权".format(args.host))

    dense = {
        "provider": args.provider,
        "api_base": api_base,
        "model": args.embedding_model,
    }
    if api_key:
        dense["api_key"] = api_key
    if args.dimension:
        dense["dimension"] = args.dimension
    if args.provider == "volcengine" and args.input_mode:
        dense["input"] = args.input_mode

    vlm = {
        "provider": args.provider,
        "api_base": api_base,
        "model": args.vlm_model,
    }
    if api_key:
        vlm["api_key"] = api_key
    if args.provider == "azure" and args.api_version:
        dense["api_version"] = args.api_version
        vlm["api_version"] = args.api_version

    doc = {
        "service_name": "openviking-server",
        "server": {
            "host": args.host,
            "port": args.port,
        },
        "storage": {
            "workspace": args.workspace,
            "agfs": {"backend": "local"},
            "vectordb": {"name": "context", "backend": "local"},
        },
        "embedding": {"dense": dense},
        "vlm": vlm,
    }
    if root_api_key:
        doc["server"]["root_api_key"] = root_api_key

    problems = validate(doc)
    if problems:
        fail("生成的配置未通过校验: " + "; ".join(problems))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, out)
    print("[ OK ] 已生成配置: {}".format(out))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Generate or validate OpenViking ov.conf")
    parser.add_argument("--check", help="validate an existing ov.conf and exit")
    parser.add_argument("--out", help="output ov.conf path")
    parser.add_argument("--workspace", help="storage.workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1933)
    parser.add_argument("--provider", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--vlm-model", default="")
    parser.add_argument("--dimension", type=int, default=0)
    parser.add_argument("--input-mode", default="")
    parser.add_argument("--api-version", default="")
    parser.add_argument("--api-key-file", default="")
    parser.add_argument("--root-api-key-file", default="")
    args = parser.parse_args()

    if args.check:
        return cmd_check(args)
    if not args.out:
        fail("未指定 --out（或使用 --check FILE）")
    return cmd_generate(args)


if __name__ == "__main__":
    sys.exit(main())
