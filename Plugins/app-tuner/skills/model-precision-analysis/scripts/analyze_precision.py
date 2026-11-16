#!/usr/bin/env python3
"""Compare precision between before/after optimization runs from the precision-eval tool.

Loads the merged result JSON (from the `merge/` subfolder of each run's output dir),
extracts a final answer from each prediction with tolerance, compares against `gold`,
computes per-run accuracy, and reports per-case before-vs-after changes.

Usage:
    python3 analyze_precision.py <before_dir> <after_dir> [--dataset-name <json_name>]
                                 [--fail-on-regression]

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


def find_json(run_dir: Path, dataset_name=None):
    """Locate the merged result json inside a run directory."""
    merge = run_dir / "merge"
    if merge.is_dir():
        cands = sorted(merge.glob("*.json"))
        if dataset_name:
            name = dataset_name if dataset_name.endswith(".json") else f"{dataset_name}.json"
            exact = merge / name
            if exact.is_file():
                return exact
        if cands:
            return cands[0]
    cands = sorted(run_dir.glob("*.json"))
    return cands[0] if cands else None


def is_truncated(text: str) -> bool:
    """True when the generation was cut off in the middle of a `\\boxed{...}`.

    A truncated tail like `\\boxed{21` would otherwise be read as the number 21 and
    silently counted as a correct answer, which inflates accuracy instead of being
    reported as an unfinished answer.
    """
    return text.rfind("\\boxed") > text.rfind("}")


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

    Source preference depends on the gold type:
    - numeric gold: the LAST boxed value; if nothing is boxed, the last number in the
      whole prediction (the model sometimes states the value outside \\boxed{}).
    - letter gold (GPQA): the model is told to end with `ANSWER: X`, so take the LAST
      `ANSWER: X` match. Only if that is absent fall back to the last A-D letter —
      the first letter is usually a distractor inside the reasoning.
    """
    gold_num = NUMBER_RE.fullmatch(gold.strip())
    gold_letter = LETTER_RE.fullmatch(gold.strip())

    if gold_num:
        boxes = extract_boxes(prediction)
        if not boxes and is_truncated(prediction):
            return "", "none"
        for cand in (boxes or [prediction]):
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
        boxes = extract_boxes(prediction)
        for cand in (boxes or [prediction]):
            letters = LETTER_RE.findall(dedecorate(cand))
            if letters:
                return letters[-1], "boxed" if boxes else "plain"
        return "", "none"

    boxes = extract_boxes(prediction)
    if not boxes and is_truncated(prediction):
        return "", "none"
    candidates = boxes if boxes else [prediction]
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


def summarize(run_dir: Path, results: dict) -> dict:
    s = analyze(results)
    jf = find_json(run_dir)
    s["json"] = jf.name if jf else None
    return s


def report(before_dir, after_dir, before_res, after_res) -> tuple[str, int]:
    before, after = summarize(Path(before_dir), before_res), summarize(Path(after_dir), after_res)
    lines = [
        "## 精度对比分析",
        "",
        f"- 优化前：`{before_dir}` (json: {before['json']}, gold_type: {before['gold_type']})",
        f"- 优化后：`{after_dir}` (json: {after['json']}, gold_type: {after['gold_type']})",
        "",
        "| 指标 | 优化前 | 优化后 |",
        "|------|--------|--------|",
        f"| 题数 | {before['total']} | {after['total']} |",
        f"| 正确 | {before['correct']} | {after['correct']} |",
        f"| 未答/截断 | {before['no_answer']} | {after['no_answer']} |",
        f"| 准确率 | {before['accuracy']}% | {after['accuracy']}% |",
        "",
    ]

    bmap = {c["id"]: c for c in before["cases"]}
    amap = {c["id"]: c for c in after["cases"]}
    regressions, improvements, same = [], [], 0
    rows = []
    gold_mismatch = []
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
        std = "不可判定（gold 不一致）" if not regressions else "未达标（存在回归）"
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
    return "\n".join(lines), len(regressions)


def load_dir(run_dir, dataset_name=None):
    run_dir = Path(run_dir)
    jf = find_json(run_dir, dataset_name)
    if not jf:
        raise FileNotFoundError(f"no result json under {run_dir}")
    with open(jf, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--dataset-name", default=None, help="merged json filename hint")
    ap.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit with status 1 when any case flips from correct to wrong",
    )
    args = ap.parse_args()

    before_dir = Path(args.before)
    after_dir = Path(args.after)
    try:
        before = load_dir(before_dir, args.dataset_name)
        after = load_dir(after_dir, args.dataset_name)
    except FileNotFoundError as exc:
        sys.exit(f"error: {exc}\n提示：确认两次运行的目录；结果 json 通常在 <run_dir>/merge/ 下")
    text, n_regressions = report(before_dir, after_dir, before, after)
    print(text)
    if args.fail_on_regression and n_regressions:
        sys.exit(1)


if __name__ == "__main__":
    main()
