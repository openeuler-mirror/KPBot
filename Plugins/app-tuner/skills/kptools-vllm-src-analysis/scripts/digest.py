#!/usr/bin/env python3
"""两个版本之间的提交归类与性能维度标记。

用于回答「这个版本改了什么、可能影响 NPU/CPU 哪些方面」。
证据级别明确标为 inferred_from_commit_subject：
提交标题只能说明「动了哪块」，不能证明性能变化幅度；
幅度必须另找 release note / benchmark 实测，见 references/evidence-policy.md。

用法：
  python3 scripts/digest.py --repo /path/vllm-ascend \
      --from v0.19.1rc1 --to v0.30.0rc1 --json digest.json
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SUBSYSTEM_RULES = [
    ("model_runner_v2", r"model[\s_-]?runner\s*v2|use_v2_model_runner|worker/v2|\bMRv2\b"),
    ("scheduler", r"\bschedul"),
    ("attention", r"attention|flashinfer|\bmla\b|paged[ _-]?att|\bfa[23]\b|\bfia\b"),
    ("moe", r"\bmoe\b|expert|\beplb\b|\bep\b"),
    ("compile_graph", r"torch\.compile|cuda[\s_-]?graph|acl[\s_-]?graph|graph capture|compilation"),
    ("kv_cache", r"kv[ _-]?cache|prefix[ _-]?cach|block[ _-]?table|paged"),
    ("quantization", r"quant|fp8|int4|w4a8|w8a8|awq|gptq"),
    ("distributed_comm", r"hccl|nccl|all[ _-]?reduce|distributed|\btp\b|\bpp\b|\bep\b|\bcomm"),
    ("spec_decode", r"speculative|spec[ _-]?decode|draft|\bmtp\b|eagle|medusa"),
    ("frontend_api", r"openai|api[ _-]?server|frontend|tokeniz|chat"),
    ("ascend_platform", r"ascend|\bnpu\b|torch_npu|910|310"),
    ("cpu", r"\bcpu\b|x86|aarch64|\barm\b"),
]

PERF_RULES = {
    "npu": r"ascend|\bnpu\b|torch_npu|910",
    "cpu": r"\bcpu\b|x86|aarch64|\barm\b",
    "memory": r"memory|\boom\b|kv[ _-]?cache|alloc|\bhbm\b",
    "comm": r"hccl|nccl|all[ _-]?reduce|communication",
    "latency": r"latency|\bttft\b|\btpot\b|\bitl\b|slow",
    "throughput": r"throughput|\bqps\b|\btps\b|perf|speedup|benchmark",
}


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_repo(repo):
    if not Path(repo).is_dir():
        return False
    proc = _git(repo, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _classify(subject, rules):
    hits = []
    low = subject.lower()
    for name, pattern in rules:
        if re.search(pattern, low):
            hits.append(name)
    return hits


def digest(repo, from_ref, to_ref, limit=200):
    if not _is_repo(repo):
        raise RuntimeError(f"不是 git 仓库: {repo}")
    for ref in (from_ref, to_ref):
        if _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").returncode != 0:
            raise RuntimeError(f"未知版本/tag: {ref}")

    proc = _git(
        repo, "log", "--no-merges", "--format=%H%x09%ad%x09%s", "--date=short",
        f"{from_ref}..{to_ref}",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log 失败: {proc.stderr.strip()}")

    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        rows.append(
            {
                "commit": sha[:12],
                "date": date,
                "subject": subject,
                "subsystems": _classify(subject, SUBSYSTEM_RULES),
                "perf_dims": _classify(subject, list(PERF_RULES.items())),
            }
        )

    by_subsystem, by_perf = {}, {}
    for row in rows:
        for name in row["subsystems"]:
            by_subsystem[name] = by_subsystem.get(name, 0) + 1
        for name in row["perf_dims"]:
            by_perf[name] = by_perf.get(name, 0) + 1

    listed = rows[:limit]
    degradations = []
    if len(rows) > limit:
        degradations.append(f"提交数 {len(rows)} 超过 limit={limit}，仅列出前 {limit} 条")

    return {
        "schema_version": 1,
        "repo": str(Path(repo).resolve()),
        "from": from_ref,
        "to": to_ref,
        "commit_range": f"{from_ref}..{to_ref}",
        "total_commits": len(rows),
        "listed_commits": len(listed),
        "analysis_level": "inferred_from_commit_subject",
        "commits": listed,
        "summary": {
            "by_subsystem": dict(sorted(by_subsystem.items())),
            "by_perf_dim": dict(sorted(by_perf.items())),
            "unclassified": sum(1 for r in rows if not r["subsystems"]),
        },
        "degradations": degradations,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="版本间提交归类")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--from", dest="from_ref", required=True)
    ap.add_argument("--to", dest="to_ref", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    try:
        result = digest(args.repo, args.from_ref, args.to_ref, args.limit)
    except Exception as exc:
        result = {"schema_version": 1, "status": "error", "error": str(exc)}
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 3

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"{result['commit_range']}: {result['total_commits']} commits")
    for name, count in result["summary"]["by_subsystem"].items():
        print(f"  subsystem {name}: {count}")
    for name, count in result["summary"]["by_perf_dim"].items():
        print(f"  perf_dim  {name}: {count}")
    print("注意：分类来自提交标题（inferred），性能幅度需另找实测证据", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
