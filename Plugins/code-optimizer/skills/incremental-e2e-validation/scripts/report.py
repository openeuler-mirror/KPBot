"""Validate comparison evidence and render a Markdown result table."""
import argparse
import json
import math
import statistics
from pathlib import Path

from plan import plan, summarize


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text_value(value, field):
    require(isinstance(value, str) and bool(value.strip()), f"Missing {field}")
    return value


def number(value, field):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            f"{field} must be a finite nonnegative number")
    return value


def escape(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def evaluate(record, comparison, configurations, header):
    """Review supplied evidence assertions; raw logs still require inspection."""
    left = configurations[comparison["control"]]
    right = configurations[comparison["candidate"]]
    reasons = left["reasons"] + right["reasons"]
    if reasons:
        return {"status": "不适用", "reason": "; ".join(reasons)}
    if record is None:
        return {"status": "未运行", "reason": "缺少比较记录"}
    require(isinstance(record, dict), "Comparison record must be an object")
    status = record.get("status")
    if status in ("not_run", "failed"):
        reason = text_value(record.get("reason"), "reason")
        return {"status": "未运行" if status == "not_run" else "执行失败",
                "reason": reason}
    require(status == "measured", "Unknown comparison status")
    require(record.get("direction") == header["direction"], "Metric direction differs")
    require(record.get("unit") == header["unit"], "Metric unit differs")
    blocks = record.get("blocks")
    require(isinstance(blocks, list) and bool(blocks), "Missing measurement blocks")
    values = []
    evidence = []
    seen = set()
    for block in blocks:
        require(isinstance(block, dict), "Block must be an object")
        block_id = text_value(block.get("id"), "block id")
        require(block_id not in seen, "Duplicate block id")
        seen.add(block_id)
        sequence = block.get("sequence")
        require(sequence in ("AB", "BA", "ABBA", "BAAB"), "Invalid block sequence")
        for side, config in (("control", left), ("candidate", right)):
            sample = block.get(side)
            require(isinstance(sample, dict), "Missing sample")
            require(sample.get("config") == config["id"], "Sample config differs")
            active = sample.get("active")
            require(isinstance(active, list) and active == config["active"],
                    "Actual active optimizations differ")
            if sample.get("condition_id") != header["condition_id"]:
                return {"status": "条件不可比", "reason": f"{block_id} conditions differ"}
            if sample.get("correctness") != "passed":
                return {"status": "正确性未通过", "reason": f"{block_id} {side}"}
            if sample.get("completed") is not True:
                return {"status": "执行未完成", "reason": f"{block_id} {side}"}
            if sample.get("hit") is not True:
                return {"status": "未确认生效", "reason": f"{block_id} {side}"}
            for key in ("run_id", "artifact_id", "log", "correctness_log"):
                text_value(sample.get(key), key)
            evidence.append(sample["log"])
        values.append({"control": block["control"]["value"],
                       "candidate": block["candidate"]["value"]})
    result = summarize({"direction": header["direction"], "blocks": values})
    result["control_median"] = statistics.median(v["control"] for v in values)
    result["candidate_median"] = statistics.median(v["candidate"] for v in values)
    result["evidence"] = evidence
    result["status"] = "观察到改善" if result["gain_pct"] > 0 else "观察到下降"
    result["reason"] = "描述性方向，需结合原始证据复核"
    minimum = header["minimum_blocks"]
    noise = record.get("noise")
    if len(blocks) < minimum:
        result.update(status="未确定", reason="样本不足，可能存在波动")
    elif not isinstance(noise, dict):
        result.update(status="未确定", reason="波动依据不足")
    else:
        floor = number(noise.get("floor_pct"), "noise floor")
        text_value(noise.get("evidence"), "noise evidence")
        require(noise.get("condition_id") == header["condition_id"],
                "Noise evidence conditions differ")
        result["noise_floor_pct"] = floor
        result["noise_evidence"] = noise["evidence"]
        low, high = result["range_pct"]
        if low <= 0 <= high or abs(result["gain_pct"]) <= floor:
            result.update(status="未确定", reason="可能存在波动，方向未定")
    drift = record.get("environment_review")
    require(drift in ("stable", "drift", "unknown"), "Missing environment review")
    if drift != "stable":
        result.update(status="未确定", reason="环境漂移或环境证据不足，可能存在波动")
    return result


def render(data):
    require(isinstance(data, dict), "Input must be an object")
    matrix = plan(data)
    header = data["experiment"]
    require(isinstance(header, dict), "experiment must be an object")
    for key in ("scenario", "baseline", "condition_id", "metric", "unit",
                "performance_framework", "correctness_framework", "conditions_log"):
        text_value(header.get(key), key)
    require(header.get("direction") in ("lower", "higher"), "Invalid direction")
    minimum = header.get("minimum_blocks")
    require(type(minimum) is int and minimum >= 2, "minimum_blocks must be at least 2")
    records = data.get("results", {})
    require(isinstance(records, dict), "results must be an object")
    valid_keys = {str(i) for i in range(len(matrix["comparisons"]))}
    require(set(records) <= valid_keys, "Unknown comparison index")
    configurations = {config["id"]: config for config in matrix["configurations"]}
    summaries = []
    for index, comparison in enumerate(matrix["comparisons"]):
        summaries.append(evaluate(records.get(str(index)), comparison, configurations, header))
    lines = [
        f"# {escape(header['scenario'])} 增量对比结果",
        "",
        f"基线 {escape(header['baseline'])}。指标 {escape(header['metric'])}，单位 {escape(header['unit'])}。",
        f"改善方向 {escape(header['direction'])}。条件标识 {escape(header['condition_id'])}。",
        f"性能框架 {escape(header['performance_framework'])}。正确性框架 {escape(header['correctness_framework'])}。",
        f"条件记录 {escape(header['conditions_log'])}。",
        "",
        "| 优化点或组合 | 独立收益 | 本步增量 | 累计收益 |",
        "|---|---|---|---|",
    ]

    def cell(index):
        result = summaries[index]
        prefix = f"比较 {index}"
        if "gain_pct" not in result:
            return escape(f"{prefix}；{result['status']}；{result['reason']}")
        raw = f"{result['control_median']:.6g} → {result['candidate_median']:.6g}"
        return escape(f"{prefix}；{result['gain_pct']:+.3f}%（{raw}）；"
                      f"n={result['blocks']}；{result['status']}；{result['reason']}")

    for row in matrix["rows"]:
        lines.append(f"| {escape(row['optimization'])} | {cell(row['independent'])} | "
                     f"{cell(row['incremental'])} | {cell(row['cumulative'])} |")
    lines.append(f"| 全部组合 | — | — | {cell(matrix['all_combined'])} |")
    lines.extend(["", "收益为逐块相对变化的中位数，括号原值为两侧各自中位数。",
                  "观察范围为描述性范围。报告仍需核对配置与原始日志。",
                  "", "## 比较记录", ""])
    for index, (comparison, result) in enumerate(zip(matrix["comparisons"], summaries)):
        lines.append(f"- 比较 {index}，{comparison['control']} → {comparison['candidate']}。"
                     f"{escape(result['status'])}，{escape(result['reason'])}。")
        if "gain_pct" in result:
            lines.append(f"  逐块收益 {result['per_block_pct']}，范围 {result['range_pct']}%。")
            if "noise_evidence" in result:
                lines.append(f"  噪声参考 {result['noise_floor_pct']}%，"
                             f"依据 {escape(result['noise_evidence'])}。")
            lines.append(f"  日志 {escape(', '.join(result['evidence']))}。")
    lines.extend(["", "## 配置", ""])
    for config in matrix["configurations"]:
        lines.append(f"- {config['id']}，启用 {escape(config['active'])}，"
                     f"限制 {escape(config['reasons'])}。")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8-sig"))
        print(render(data), end="")
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.exit(2, f"Invalid input: {error}\n")


if __name__ == "__main__":
    main()
