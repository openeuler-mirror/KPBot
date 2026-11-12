#!/usr/bin/env python3
"""Run against installed KPBot, with kpknow already authorized in OpenCode.

Set OPENCODE_BIN, KPBot_TEST_PROJECT, MODEL and provider credentials in the
environment. Run only against an isolated project. Results never contain keys.
The checks verify tool execution evidence, not the correctness of Wiki claims.
"""
import json
import os
from pathlib import Path
import subprocess
import sys


def run():
    executable = os.environ.get("OPENCODE_BIN", "opencode")
    project = os.environ["KPBot_TEST_PROJECT"]
    model = os.environ.get("MODEL", "deepseek/deepseek-flash")
    results = Path(__file__).resolve().parent / "results"
    scenario = os.environ.get("KPBOT_TEST_SCENARIO", "lookup")
    if scenario not in ("lookup", "natural", "empty"):
        raise ValueError("KPBOT_TEST_SCENARIO must be lookup, natural or empty")
    results.mkdir(exist_ok=True, parents=True)
    result_path = results / "result.json"
    result_path.unlink(missing_ok=True)
    secrets = [os.environ.get(name, "") for name in (
        "DEEPSEEK_API_KEY",
    )]

    def clean(text):
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def execute(arguments, filename, timeout=300):
        process = subprocess.run(
            [executable] + arguments, cwd=project, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        output = clean(process.stdout)
        (results / filename).write_text(output, encoding="utf-8")
        (results / (filename + ".stderr")).write_text(
            clean(process.stderr), encoding="utf-8"
        )
        if process.returncode:
            raise RuntimeError("OpenCode failed; see sanitized " + filename)
        return output

    discovery = execute(["debug", "skill"], "discovery.json")
    discovered = "kunpeng-knowledge" in discovery
    prompt = (
        "请使用 kunpeng-knowledge 技能，从已配置的知识库查询 mmap "
        "匿名内存延迟清零。必须先搜索再读取命中的完整页面，最后给出简短结论、"
        "适用条件、标题、知识库ID、slug和来源局限。只读查询，不修改文件，"
        "不查看或输出任何环境变量值或密钥。"
    )
    if scenario == "natural":
        prompt = (
            "从已配置的知识库查一下 mmap 匿名内存延迟清零，"
            "读取匹配原文后给出结论和可追溯来源。只读查询，不修改文件，"
            "不查看或输出任何环境变量值或密钥。"
        )
    elif scenario == "empty":
        prompt = (
            "使用 kunpeng-knowledge 技能，在知识库搜索精确术语 "
            "kpbot_nonexistent_8d76f31a9c。未找到就明确说明未检索到依据，"
            "不要编造内容。只读查询，不修改文件，不输出密钥。"
        )
    trace = execute([
        "run", "--model", model, "--format", "json", prompt
    ], "trace.jsonl")
    tools = []
    final = []
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part", {})
        if event.get("type") == "tool_use":
            tools.append(part)
        if event.get("type") == "text":
            final.append(part.get("text", ""))
    loaded = any(
        part.get("tool") == "skill"
        and "kunpeng-knowledge" in json.dumps(part.get("state", {}))
        for part in tools
    )
    completed = [
        part for part in tools
        if part.get("state", {}).get("status") == "completed"
    ]
    search = next((i for i, part in enumerate(completed)
                   if part.get("tool") == "kpknow_wiki_search"), None)
    read = next((i for i, part in enumerate(completed)
                 if part.get("tool") == "kpknow_wiki_read_page"), None)
    observed = search is not None and read is not None and search < read
    errors = any(
        '"type":"error"' in line.replace(" ", "")
        for line in trace.splitlines()
    )
    empty_reported = any(
        term in "\n".join(final)
        for term in ("未检索到", "未找到", "无匹配", "没有找到")
    )
    zero_hit_observed = any(
        '<search_results count="0"' in
        part.get("state", {}).get("output", "").replace("\\", "")
        for part in completed
        if part.get("tool") == "kpknow_wiki_search"
    )
    behavior = observed if scenario != "empty" else (
        search is not None and read is None and empty_reported
        and zero_hit_observed
    )
    result = {
        "success": discovered and loaded and behavior and bool(final) and not errors,
        "scenario": scenario,
        "skill_discovered": discovered,
        "skill_loaded": loaded,
        "search_and_read_observed": observed,
        "empty_reported": empty_reported if scenario == "empty" else None,
        "zero_hit_observed": zero_hit_observed if scenario == "empty" else None,
        "model": model,
        "tools_used": [part.get("tool") for part in tools],
        "limitations": "需人工复核回答来源与适用性；本用例不验证 Wiki 知识本身正确性",
    }
    (results / "answer.md").write_text("\n".join(final), encoding="utf-8")
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(run())
