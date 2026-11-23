"""Report + log-preflight subcommand for the v0.27 TTFT skill.

Wraps the Python-only step report and checks producer markers before HTML
generation. Native libraries and thread mapping records are not required.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gen_ttft_report import ids, pf

SKILL_ROOT = Path(__file__).resolve().parent
GEN = SKILL_ROOT / "gen_ttft_report.py"

REQUIRED = (
    ("[TTFT][BREAKDOWN]", "vllm stats.py 请求级汇总"),
    ("[TTFT][QUEUE]", "vllm stats.py 排队锚点"),
    ("[TTFT_DEBUG][AscendStoreSave]", "ascend pool_worker/kv_transfer 保存分项"),
)


def scan_log(text: str) -> dict[str, int]:
    return {marker: text.count(marker) for marker, _ in REQUIRED}


def list_requests(log: Path) -> list[tuple[str, int]]:
    """(request_id, BREAKDOWN count) in first-appearance order."""
    seen: dict[str, int] = {}
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            _, marker, tail = line.partition(REQUIRED[0][0])
            if marker:
                names = ids(pf(tail))
                if len(names) == 1:
                    request = next(iter(names))
                    seen[request] = seen.get(request, 0) + 1
    return list(seen.items())


def cmd_check(args) -> int:
    log = Path(args.log)
    if not log.is_file():
        print(f"ERROR: 日志不存在：{log}", file=sys.stderr)
        return 1
    counts = scan_log(log.read_text(encoding="utf-8", errors="replace"))
    missing = [m for m, c in counts.items() if c == 0]
    for marker, count in counts.items():
        print(f"  {marker}: {count}")
    if missing:
        print(
            "ERROR: 缺少以上为 0 的标记；打点未生效或日志不完整，生成的报告将是空表。",
            file=sys.stderr,
        )
        return 1
    print("日志包含全部必需标记。")
    return 0


def cmd_report(args) -> int:
    log = Path(args.log)
    if not log.is_file():
        print(f"ERROR: 日志不存在：{log}", file=sys.stderr)
        return 1
    counts = scan_log(log.read_text(encoding="utf-8", errors="replace"))
    missing = [m for m, c in counts.items() if c == 0]
    for marker, count in counts.items():
        print(f"  {marker}: {count}")
    if missing:
        print(
            "ERROR: 缺少以上为 0 的标记；先确认打点已应用、服务已按原命令重启。"
            "仍要生成可用 --force。",
            file=sys.stderr,
        )
        if not args.force:
            return 1
    if counts[REQUIRED[0][0]] > 1:
        print("日志包含多条 BREAKDOWN；请截取单请求日志切片后重跑，"
              "--req 只核对切片中的请求 ID。", file=sys.stderr)
        for req, count in list_requests(log):
            print(f"  req={req}  BREAKDOWN={count} 条", file=sys.stderr)
        return 1
    requests = list_requests(log)
    if not args.req and len(requests) == 1:
        args.req = requests[0][0]
        print(f"  单一请求：{args.req}（已自动选中）")
    if args.req:
        matched = [r for r, _ in requests if r == args.req]
        if requests and not matched:
            known = ", ".join(r for r, _ in requests)
            print(
                f"ERROR: --req {args.req} 在日志中不存在。可用请求：{known}",
                file=sys.stderr,
            )
            return 1
    cmd = [sys.executable, str(GEN), str(log), "-o", str(args.out)]
    if args.req:
        cmd += ["--req", args.req]
    if args.mode:
        cmd += ["--mode", args.mode]
    if args.tp_rank is not None:
        cmd += ["--tp-rank", str(args.tp_rank)]
    if args.tokenizer_line is not None:
        cmd += ["--tokenizer-line", str(args.tokenizer_line)]
    return subprocess.run(cmd, check=False).returncode


def register(subparsers) -> None:
    p_check = subparsers.add_parser("check", help="日志预检：必需标记是否齐全")
    p_check.add_argument("log", type=Path)
    p_check.set_defaults(func=cmd_check)

    p_report = subparsers.add_parser("report", help="生成 TTFT HTML 报告")
    p_report.add_argument("log", type=Path)
    p_report.add_argument("-o", "--out", type=Path, default=Path("ttft_report.html"))
    p_report.add_argument("--req")
    p_report.add_argument("--mode", choices=("prefill", "all"))
    p_report.add_argument("--tp-rank", type=int)
    p_report.add_argument("--tokenizer-line", type=int)
    p_report.add_argument(
        "--force", action="store_true", help="缺少标记时仍生成（报告会是空表）"
    )
    p_report.set_defaults(func=cmd_report)
