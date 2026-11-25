#!/usr/bin/env python3
"""Compare precision between before/after optimization runs from the precision-eval tool.

Loads the merged result JSON (from the `merge/` subfolder of each run's output dir),
extracts a final answer from each prediction with tolerance, compares against `gold`,
computes per-run accuracy, and reports per-case before-vs-after changes.

Usage:
    python3 analyze_precision.py <before_dir> <after_dir> [--dataset-name <json_name>]

Exit status:
    0 - comparable runs, no regression
    1 - comparable runs, at least one correct->wrong flip (gate failed, always
        returned regardless of the legacy --fail-on-regression flag)
    2 - runs are not comparable (gold mismatch or case ids differ), so the
        verdict is undecidable and must not be read as a pass

Item schema (shared by all datasets in this tool):
    {"origin_prompt": str, "prediction": str, "gold": str, "exec_times": int}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BOXED_RE = re.compile(r"\\boxed\s*{([^{}]*(?:{[^{}]*}[^{}]*)*)}")
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
LETTER_RE = re.compile(r"\b[A-D]\b")
ANSWER_RE = re.compile(r"ANSWER\s*:\s*([A-D])", re.IGNORECASE)
TEXT_WRAP_RE = re.compile(r"\\text(?:rm|bf|it)?\s*\{([^{}]*)\}")
THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
UNIT_RE = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
UNIT_SECONDS = {
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}


def find_json(run_dir: Path, dataset_name=None) -> Path | None:
    """Locate the merged result json inside a run directory.

    Never falls back silently: a `--dataset-name` that does not exist is an error,
    and several json files under one folder require an explicit `--dataset-name`,
    otherwise two runs could be compared across different datasets.
    """
    name = None
    if dataset_name:
        name = dataset_name if dataset_name.endswith(".json") else f"{dataset_name}.json"
    folders = [d for d in (run_dir / "merge", run_dir) if d.is_dir()]
    if name:
        for folder in folders:
            exact = folder / name
            if exact.is_file():
                return exact
        raise FileNotFoundError(
            f"--dataset-name {name} 在 {run_dir} 下不存在（已查找 merge/ 与目录根）"
        )
    for folder in folders:
        cands = sorted(folder.glob("*.json"))
        if len(cands) > 1:
            raise ValueError(
                f"{folder} 下有多个结果 json，无法确定分析哪一个；"
                "请用 --dataset-name 显式指定"
            )
        if cands:
            return cands[0]
    return None


def is_truncated(text: str) -> bool:
    """True when the tail of the generation has an unclosed `\\boxed{...}`.

    A truncated tail like `\\boxed{21` or `\\boxed{\\frac{1}{2` would otherwise be
    read as a number and silently counted as an answer, which inflates accuracy or
    fakes a regression instead of being reported as an unfinished answer. Earlier
    complete boxes do not mask a truncated tail.
    """
    idx = text.rfind("\\boxed")
    if idx < 0:
        return False
    boxes = list(BOXED_RE.finditer(text))
    return not boxes or boxes[-1].start() != idx


def extract_boxes(text: str) -> list[str]:
    """Return the contents of every \\boxed{...} occurrence, in order."""
    return [m.group(1) for m in BOXED_RE.finditer(text)]


def dedecorate(text: str) -> str:
    """Normalize the cosmetic noise models add to answers: `\\text{}`, thousands
    separators (`1,000`), escaped percent signs, and non-breaking spaces."""
    s = plainify(text).replace("{,}", ",").replace("\\%", "%").replace("\xa0", " ")
    return THOUSANDS_RE.sub("", s)


def normalize_answer(raw: str, gold: str) -> str:
    """Normalize a candidate answer so harmless formatting does not count as wrong.

    The model often writes units or surrounding prose inside the boxed answer
    (e.g. `204\\text{ minutes}` for gold `204`), which strict string matching in the
    eval tool reports as a false negative. We therefore align the comparison on the
    answer's *value*: a number for numeric gold, a single letter for letter gold.
    """
    s = dedecorate(raw.strip())
    if not s:
        return s

    if NUMBER_RE.fullmatch(gold.strip()):
        nums = NUMBER_RE.findall(s)
        return nums[-1] if nums else s

    if LETTER_RE.fullmatch(gold.strip()):
        letters = LETTER_RE.findall(s)
        return letters[0] if letters else s

    return s.strip(" \t\r\n.,;:!?")


def plainify(text: str) -> str:
    """Drop LaTeX text wrappers like `\\text{ minutes}` so words can be parsed."""
    return TEXT_WRAP_RE.sub(r"\1", text)


def unit_values(text: str) -> list[str]:
    """Convert a pure time expression into its value in seconds, minutes and hours.

    AIME-style golds are plain integers, but models answer with time expressions:
    `3 hours and 24 minutes`, `1.4 hours`, `204 minutes`. Read naively, the first two
    yield `24` and `1.4` and a correct answer becomes a false regression. We only
    convert when *every* number in the text carries a time unit, so mixed prose such
    as `5 apples in 3 hours` is left alone. Returning several units lets the caller
    accept the one that equals gold, whichever unit the dataset uses.
    """
    matches = list(UNIT_RE.finditer(text))
    if not matches:
        return []
    if len(matches) != len(NUMBER_RE.findall(text)):
        return []
    total = 0.0
    for m in matches:
        total += float(m.group(1)) * UNIT_SECONDS[m.group(2).lower()]
    if total <= 0:
        return []
    return [fmt(total / 3600), fmt(total / 60), fmt(total)]


def fmt(value: float) -> str:
    return str(int(round(value))) if abs(value - round(value)) < 1e-9 else f"{value:g}"


def answers_match(answer: str, gold: str) -> bool:
    """Compare an extracted answer with gold on value, not on spelling."""
    a, g = normalize_answer(answer, gold), normalize_answer(gold, gold)
    if a == g:
        return True
    try:
        return float(a) == float(g)
    except ValueError:
        return False


def answer_of(prediction: str, gold: str) -> tuple[str, str]:
    """Return (extracted_answer, source) for a prediction.

    A generation whose tail leaves a `\\boxed{...}` unclosed is treated as truncated
    and yields no answer, even when an earlier complete box exists. The only exception
    is an explicit final `ANSWER: X` line, which is a complete answer on its own.

    Source preference depends on the gold type:
    - numeric gold: the LAST boxed value that contains a value; if nothing is boxed,
      the last number in the whole prediction (the model sometimes states the value
      outside \\boxed{}).
    - letter gold (GPQA): the model is told to end with `ANSWER: X`, so take the LAST
      `ANSWER: X` match. Only if that is absent fall back to the last boxed letter,
      then the last A-D letter — the first letter is usually a distractor inside the
      reasoning.
    - text gold: the LAST non-empty boxed content.
    """
    gold_num = NUMBER_RE.fullmatch(gold.strip())
    gold_letter = LETTER_RE.fullmatch(gold.strip())
    truncated = is_truncated(prediction)

    if gold_num:
        if truncated:
            return "", "none"
        boxes = extract_boxes(prediction)
        for cand in (reversed(boxes) if boxes else [prediction]):
            cand = dedecorate(cand)
            for converted in unit_values(cand):
                if answers_match(converted, gold):
                    return converted, "unit"
            nums = NUMBER_RE.findall(cand)
            if nums:
                return nums[-1], "boxed" if boxes else "plain"
        return "", "none"

    if gold_letter:
        matches = ANSWER_RE.findall(prediction)
        if matches:
            return matches[-1].upper(), "answer"
        if truncated:
            return "", "none"
        boxes = extract_boxes(prediction)
        for cand in (reversed(boxes) if boxes else [prediction]):
            letters = LETTER_RE.findall(dedecorate(cand))
            if letters:
                return letters[-1], "boxed" if boxes else "plain"
        return "", "none"

    if truncated:
        return "", "none"
    boxes = extract_boxes(prediction)
    candidates = reversed(boxes) if boxes else [prediction]
    for cand in candidates:
        norm = normalize_answer(cand, gold)
        if norm:
            return norm, "boxed" if boxes else "plain"
    return "", "none"


def analyze(results: dict) -> dict:
    out = {"total": 0, "correct": 0, "no_answer": 0, "cases": []}
    gold_seen = ""
    for key in sorted(results, key=lambda k: int(k)):
        item = results[key]
        gold = str(item["gold"]).strip()
        gold_seen = gold_seen or gold
        ans, src = answer_of(str(item["prediction"]), gold)
        if not ans:
            out["no_answer"] += 1
            out["cases"].append(
                {"id": str(key), "gold": gold, "answer": "", "source": "none", "correct": False}
            )
            continue
        correct = answers_match(ans, gold)
        out["cases"].append(
            {"id": str(key), "gold": gold, "answer": ans, "source": src, "correct": correct}
        )
    out["total"] = len(out["cases"])
    out["correct"] = sum(1 for c in out["cases"] if c["correct"])
    out["accuracy"] = round(100.0 * out["correct"] / out["total"], 2) if out["total"] else 0.0
    out["gold_type"] = (
        "numeric" if NUMBER_RE.fullmatch(gold_seen)
        else "letter" if LETTER_RE.fullmatch(gold_seen)
        else "text"
    )
    return out



def compare_runs(before: dict, after: dict) -> dict:
    """Compute per-case before/after flips once, shared by markdown and JSON output."""
    bmap = {c["id"]: c for c in before["cases"]}
    amap = {c["id"]: c for c in after["cases"]}
    regressions, improvements, rows, gold_mismatch = [], [], [], []
    same = 0
    only_one_side = sorted(set(bmap) ^ set(amap), key=int)
    for cid in sorted(set(bmap) & set(amap), key=int):
        b, a = bmap[cid], amap[cid]
        rows.append((cid, b, a))
        if b["gold"] != a["gold"]:
            gold_mismatch.append(cid)
            continue
        if b["correct"] and not a["correct"]:
            regressions.append(cid)
        elif not b["correct"] and a["correct"]:
            improvements.append(cid)
        else:
            same += 1
    return {
        "bmap": bmap, "amap": amap, "rows": rows,
        "regressions": regressions, "improvements": improvements, "same": same,
        "gold_mismatch": gold_mismatch, "only_one_side": only_one_side,
        "incomparable": bool(gold_mismatch or only_one_side),
    }


def build_result(before_dir, after_dir, before: dict, after: dict, before_json, after_json) -> dict:
    """Structured, machine-readable result for CI / downstream validation_plan."""
    cmp = compare_runs(before, after)
    if cmp["incomparable"]:
        exit_code, verdict = 2, "undecidable"
    elif cmp["regressions"]:
        exit_code, verdict = 1, "fail"
    else:
        exit_code, verdict = 0, "pass"

    def side(res, d, jf):
        return {
            "dir": str(d), "json": str(jf), "gold_type": res["gold_type"],
            "total": res["total"], "correct": res["correct"],
            "no_answer": res["no_answer"], "accuracy": res["accuracy"],
        }

    def flip(cid):
        b, a = cmp["bmap"][cid], cmp["amap"][cid]
        return {"id": cid, "gold": b["gold"],
                "before_answer": b["answer"], "after_answer": a["answer"]}

    return {
        "schema_version": 1,
        "before": side(before, before_dir, before_json),
        "after": side(after, after_dir, after_json),
        "status_flip": {
            "same": cmp["same"],
            "regressions": list(cmp["regressions"]),
            "improvements": list(cmp["improvements"]),
        },
        "regressions": [flip(c) for c in cmp["regressions"]],
        "improvements": [flip(c) for c in cmp["improvements"]],
        "gold_mismatch": list(cmp["gold_mismatch"]),
        "only_one_side": list(cmp["only_one_side"]),
        "comparable": not cmp["incomparable"],
        "verdict": verdict,
        "exit_code": exit_code,
    }


def render_md(result: dict, before: dict, after: dict) -> str:
    """Human-readable report; text stays identical to the pre-JSON renderer."""
    cmp = compare_runs(before, after)
    before_dir, after_dir = result["before"]["dir"], result["after"]["dir"]
    before_json, after_json = result["before"]["json"], result["after"]["json"]
    rows, bmap, amap = cmp["rows"], cmp["bmap"], cmp["amap"]
    regressions, improvements = cmp["regressions"], cmp["improvements"]
    same, gold_mismatch, only_one_side = cmp["same"], cmp["gold_mismatch"], cmp["only_one_side"]

    lines = [
        "## 精度对比分析",
        "",
        f"- 优化前：`{before_dir}` (json: {before_json}, gold_type: {before['gold_type']})",
        f"- 优化后：`{after_dir}` (json: {after_json}, gold_type: {after['gold_type']})",
        "",
        "| 指标 | 优化前 | 优化后 |",
        "|------|--------|--------|",
        f"| 题数 | {before['total']} | {after['total']} |",
        f"| 正确 | {before['correct']} | {after['correct']} |",
        f"| 未答/截断 | {before['no_answer']} | {after['no_answer']} |",
        f"| 准确率 | {before['accuracy']}% | {after['accuracy']}% |",
        "",
    ]
    lines.append(f"状态翻转：无变化 {same} 题，回归 {len(regressions)} 题，改善 {len(improvements)} 题")
    if gold_mismatch:
        lines.append(
            f"⚠️ 题 {', '.join(gold_mismatch)} 两次运行的 gold 不一致，已跳过翻转判定"
            "（不是同一批题，不能直接比较）"
        )
    if only_one_side:
        lines.append(f"⚠️ 题 {', '.join(only_one_side)} 只出现在其中一次运行，未参与对比")
    lines.append("")

    lines.append("### 逐题明细")
    lines.append("| ID | gold | 优化前答案 | 优化前 | 优化后答案 | 优化后 |")
    lines.append("|----|------|-----------|--------|-----------|--------|")
    for cid, b, a in rows:
        lines.append(
            f"| {cid} | {b['gold']} | {b['answer'] or '-'} | {'OK' if b['correct'] else 'WRONG'} | "
            f"{a['answer'] or '-'} | {'OK' if a['correct'] else 'WRONG'} |"
        )
    lines.append("")

    if regressions:
        lines.append(f"### ⚠️ 回归项 ({len(regressions)})")
        for cid in regressions:
            b, a = bmap[cid], amap[cid]
            lines.append(
                f"- 题 {cid}: gold={b['gold']}，优化前答案 `{b['answer']}`（对）→ "
                f"优化后 `{a['answer'] or '未抽取到答案'}`（错）"
            )
    else:
        lines.append("### 回归检查")
        lines.append("优化后未出现由正确变错误的题目，**无精度回归**。")
    lines.append("")

    if improvements:
        lines.append(f"### 改善项 ({len(improvements)})")
        for cid in improvements:
            b, a = bmap[cid], amap[cid]
            lines.append(
                f"- 题 {cid}: gold={b['gold']}，优化前 `{b['answer'] or '未抽取到答案'}` → "
                f"优化后 `{a['answer']}`"
            )
        lines.append("")

    if gold_mismatch:
        std = "未达标（存在回归）" if regressions else "不可判定（gold 不一致）"
    elif only_one_side:
        std = "未达标（存在回归）" if regressions else "不可判定（题号不一致）"
    else:
        std = "达标" if not regressions else "未达标（存在回归）"
    lines.append("### 结论")
    lines.append(
        f"精度标准：**{std}**。准确率 {before['accuracy']}% → {after['accuracy']}%"
        f"（{before['correct']}/{before['total']} → {after['correct']}/{after['total']}）。"
    )
    if gold_mismatch:
        lines.append(
            f"其中 {before['total'] - len(gold_mismatch)} 题可按题号对比；"
            f"{len(gold_mismatch)} 题 gold 不一致，需先确认是否同一份数据集。"
        )
    if cmp["incomparable"]:
        lines.append(
            "两次运行不可比（gold 或题号不一致），脚本以退出码 2 结束；"
            "退出码 2 不是达标，需先确认两次跑的是同一份数据集。"
        )
    return "\n".join(lines)


def load_dir(run_dir, dataset_name=None):
    run_dir = Path(run_dir)
    jf = find_json(run_dir, dataset_name)
    if not jf:
        raise FileNotFoundError(f"no result json under {run_dir}")
    with open(jf, encoding="utf-8") as f:
        return json.load(f), jf


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--dataset-name", default=None, help="merged json filename hint")
    ap.add_argument("--json", action="store_true",
                    help="emit a structured JSON result instead of markdown (exit code unchanged)")
    ap.add_argument("--out", default=None, help="also write the output to this file")
    ap.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="deprecated no-op kept for compatibility; a regression always exits 1",
    )
    args = ap.parse_args()

    before_dir = Path(args.before)
    after_dir = Path(args.after)
    try:
        before_raw, before_json = load_dir(before_dir, args.dataset_name)
        after_raw, after_json = load_dir(after_dir, args.dataset_name)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"error: {exc}\n提示：确认两次运行的目录；结果 json 通常在 <run_dir>/merge/ 下")

    before_res, after_res = analyze(before_raw), analyze(after_raw)
    result = build_result(before_dir, after_dir, before_res, after_res, before_json, after_json)
    if args.json:
        out = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    else:
        out = render_md(result, before_res, after_res) + "\n"
    print(out, end="")
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
    sys.exit(result["exit_code"])


if __name__ == "__main__":
    main()
