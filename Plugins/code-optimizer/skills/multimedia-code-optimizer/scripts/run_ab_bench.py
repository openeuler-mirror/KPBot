#!/usr/bin/env python3
"""跑 A/B 基准，解析夹具输出，给出通过/不通过的判定。

夹具（或任何被测程序）只要在 stdout 打印这两行就行：
    CORRECT n=<len> ok=<0|1> mismatches=<n> first_bad=<idx>
    TIME ref_ns=<n> simd_ns=<n>

用法:
    python3 run_ab_bench.py --cmd "make -C out run"
    python3 run_ab_bench.py --cmd "make -C out run" --alt-cmd "make -C out run-flip" \
        --reps 7 --min-speedup 1.05

`--alt-cmd` 是一份"先测 simd 再测 ref"的构建。墙钟口径下 A/B 顺序会带来偏差，
两构建交替跑可以抵消；icount 口径下顺序无影响，可以不传。

门控不只看速度比：

- 有效轮数下限：默认 `min(reps, max(2, ceil(reps/2)))`，可用 `--min-rounds` 覆盖。
  不足时 `verdict` 为 `insufficient_measurements`，不给"更快"的结论。
- 墙钟口径的区间条件：`--method wall`（或 `auto` 下测得非确定性）要求 A/B 的
  [min, max] 区间不重叠，否则加速比必须 >= 1.10 才算 faster。
- 任一轮失败（超时、崩溃、没有 TIME 行、VL 不匹配）都不给通过结论。

退出码:
    0  通过（正确性 ok、有效轮数足够、speedup >= 阈值且满足区间条件）
    1  正确性通过但性能未达标
    2  正确性失败
    3  夹具/环境错误（有效轮数不足、有成轮失败、没有 TIME 行、VL 不匹配、超时等）
"""

import argparse
import json
import statistics
import subprocess
import sys

# 墙钟口径的"区间重叠但收益足够大"例外阈值，与 verification-gates.md 一致。
INTERVAL_SPEEDUP_OVERRIDE = 1.10


def parse_output(text):
    """从夹具输出里抽结构化字段。缺失的键不写，由调用方判断。"""
    info = {"correct": [], "time": [], "vl": None, "errors": []}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("CORRECT "):
            fields = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
            try:
                info["correct"].append({
                    "n": int(fields.get("n", -1)),
                    "ok": fields.get("ok") == "1",
                    "mismatches": int(fields.get("mismatches", -1)),
                    "first_bad": int(fields.get("first_bad", -1)),
                })
            except ValueError:
                info["errors"].append("unparsable CORRECT line: " + line)
        elif line.startswith("TIME ") and not line.startswith("TIMEBASE"):
            fields = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
            try:
                info["time"].append({
                    "ref_ns": int(fields["ref_ns"]),
                    "simd_ns": int(fields["simd_ns"]),
                })
            except (KeyError, ValueError):
                info["errors"].append("unparsable TIME line: " + line)
        elif line.startswith("VL bytes="):
            try:
                info["vl"] = int(line.split("=", 1)[1].split()[0])
            except (IndexError, ValueError):
                pass
        elif "vector length mismatch" in line:
            info["errors"].append("runtime vector length mismatch")
        elif line.startswith("FAILED"):
            info["errors"].append(line)
    return info


def run_once(cmd, timeout):
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout after %ss: %s" % (timeout, cmd)
    except OSError as exc:
        return None, "cannot run %r: %s" % (cmd, exc)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode not in (0, 1):
        # 夹具自己的失败码：0 成功，1 正确性失败，2 VL 不匹配
        return out, "command exited with %d: %s" % (proc.returncode, cmd)
    return out, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cmd", required=True, help="运行基准的命令（shell 字符串）")
    ap.add_argument("--alt-cmd", default="",
                    help="顺序翻转版本的运行命令；提供后与 --cmd 交替执行")
    ap.add_argument("--reps", type=int, default=5, help="轮数，默认 5")
    ap.add_argument("--min-rounds", type=int, default=0,
                    help="有效测量轮数下限；0 表示按 --reps 自动取 "
                         "min(reps, max(2, ceil(reps/2)))")
    ap.add_argument("--method", choices=["auto", "icount", "wall"], default="auto",
                    help="计时口径：icount 不做区间门，wall 强制区间门，"
                         "auto 按各轮离散度判断")
    ap.add_argument("--min-speedup", type=float, default=1.05)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--label", default="", help="写进结果的标签（函数名等）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    min_rounds = args.min_rounds or min(args.reps, max(2, (args.reps + 1) // 2))

    rounds = []
    errors = []
    vl_values = set()

    for i in range(args.reps):
        use_alt = bool(args.alt_cmd) and (i % 2 == 1)
        cmd = args.alt_cmd if use_alt else args.cmd
        out, err = run_once(cmd, args.timeout)
        if err:
            errors.append({"round": i + 1, "error": err})
            continue
        info = parse_output(out)
        if info["vl"] is not None:
            vl_values.add(info["vl"])
        if info["errors"]:
            errors.append({"round": i + 1, "error": "; ".join(info["errors"])})
        bad = [c for c in info["correct"] if not c["ok"]]
        if bad:
            errors.append({"round": i + 1,
                           "error": "correctness failed",
                           "cases": bad})
        if not info["time"]:
            errors.append({"round": i + 1, "error": "no TIME line in output",
                           "tail": out.strip().splitlines()[-6:]})
            continue
        for t in info["time"]:
            rounds.append({"round": i + 1, "flipped": use_alt, **t})

    if not rounds:
        payload = {
            "success": False,
            "label": args.label,
            "verdict": "harness_error",
            "valid_rounds": 0,
            "min_rounds": min_rounds,
            "errors": errors,
            "error_message": "no usable TIME measurement",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3

    refs = [r["ref_ns"] for r in rounds]
    sims = [r["simd_ns"] for r in rounds]
    ref_med = statistics.median(refs)
    sim_med = statistics.median(sims)
    speedup = (ref_med / sim_med) if sim_med else 0.0
    # 两份构建（正常/翻转）代码布局不同，允许极小差异；判据是相对离散度。
    rel_spread = max((max(refs) - min(refs)) / ref_med if ref_med else 0.0,
                     (max(sims) - min(sims)) / sim_med if sim_med else 0.0)
    deterministic = rel_spread <= 0.001

    correctness_ok = not any(e.get("error") == "correctness failed" for e in errors)

    # 墙钟口径的区间条件：A/B 的 [min, max] 区间不重叠，或加速比足够大（>= 1.10），
    # 才允许下"更快"的结论。icount 口径下各轮完全一致，不需要这条。
    use_interval_gate = (args.method == "wall"
                         or (args.method == "auto" and not deterministic))
    interval_overlap = use_interval_gate and not (max(sims) < min(refs)
                                                  or max(refs) < min(sims))

    if not correctness_ok:
        verdict = "correctness_failed"
    elif len(rounds) < min_rounds:
        verdict = "insufficient_measurements"
    elif interval_overlap and speedup < INTERVAL_SPEEDUP_OVERRIDE:
        verdict = "not_faster"
    elif speedup >= args.min_speedup:
        verdict = "faster"
    else:
        verdict = "not_faster"

    spread = {
        "ref_min": min(refs), "ref_max": max(refs),
        "simd_min": min(sims), "simd_max": max(sims),
        "ref_stdev": round(statistics.pstdev(refs), 2),
        "simd_stdev": round(statistics.pstdev(sims), 2),
    }

    warnings = []
    if not deterministic and max(spread["ref_stdev"], spread["simd_stdev"]) > \
            0.05 * max(ref_med, sim_med):
        warnings.append("计时离散度超过中位数的 5%%；墙钟口径下建议增加 --reps "
                        "并提供 --alt-cmd 抵消顺序偏差，否则不要把结论当作确定")
    if len(rounds) < min_rounds:
        warnings.append("有效测量轮数 %d < 下限 %d（--reps %d）；样本不足不能下结论"
                        % (len(rounds), min_rounds, args.reps))
    if interval_overlap:
        warnings.append("A/B 区间重叠（ref %d~%d，simd %d~%d）：加速比必须 >= %.2f "
                        "才允许判 faster" % (spread["ref_min"], spread["ref_max"],
                                             spread["simd_min"], spread["simd_max"],
                                             INTERVAL_SPEEDUP_OVERRIDE))
    if len(vl_values) > 1:
        warnings.append("多轮运行时向量长度不一致: %s" % sorted(vl_values))
    if vl_values and vl_values != {32}:
        warnings.append("运行时 VL 不是 32 字节(256 位)，当前结果与 256 位假设不符")

    success = (verdict == "faster") and correctness_ok and not errors

    if success:
        error_message = ""
    elif verdict == "insufficient_measurements":
        error_message = ("insufficient_measurements: %d/%d rounds usable, need >= %d"
                         % (len(rounds), args.reps, min_rounds))
    elif verdict == "correctness_failed":
        error_message = "correctness_failed"
    elif errors:
        error_message = ("some rounds failed (%d of %d); the performance verdict is "
                         "not trustworthy" % (len(errors), args.reps))
    elif verdict == "not_faster" and interval_overlap:
        error_message = ("interval_overlap: speedup %.4f < %.2f and A/B ranges overlap"
                         % (speedup, INTERVAL_SPEEDUP_OVERRIDE))
    else:
        error_message = ("not_faster: speedup %.4f < min_speedup %.2f"
                         % (speedup, args.min_speedup))

    payload = {
        "success": success,
        "label": args.label,
        "verdict": verdict,
        "correctness_ok": correctness_ok,
        "rounds": len(rounds),
        "valid_rounds": len(rounds),
        "min_rounds": min_rounds,
        "method": args.method,
        "interval_overlap": interval_overlap,
        "deterministic": deterministic,
        "relative_spread": round(rel_spread, 6),
        "ref_ns": ref_med,
        "simd_ns": sim_med,
        "speedup": round(speedup, 4),
        "min_speedup": args.min_speedup,
        "spread": spread,
        "runtime_vl_bytes": sorted(vl_values),
        "errors": errors,
        "warnings": warnings,
        "command": args.cmd,
        "alt_command": args.alt_cmd,
        "error_message": error_message,
    }

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if verdict == "correctness_failed":
        return 2
    if verdict == "insufficient_measurements":
        return 3
    if errors:
        return 3
    return 0 if verdict == "faster" else 1


if __name__ == "__main__":
    sys.exit(main())
