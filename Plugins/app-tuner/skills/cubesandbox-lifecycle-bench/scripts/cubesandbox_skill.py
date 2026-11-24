#!/usr/bin/env python3
"""Route one focused CubeSandbox skill to the shared lifecycle executor."""

from __future__ import annotations

from pathlib import Path
import sys

import cubesandbox_bench as bench
from lib_cubesandbox import BenchError


SKILL_SECTIONS = {
    "cubesandbox-template-create-bench": "template_create",
    "cubesandbox-density-bench": "density",
    "cubesandbox-snapshot-bench": "snapshot",
    "cubesandbox-dirty-page-bench": "dirty_page",
    "cubesandbox-snapshot-create-bench": "snapshot_create",
    "cubesandbox-rollback-bench": "rollback",
    "cubesandbox-clone-bench": "clone",
    "cubesandbox-pause-resume-bench": "pause_resume",
}
SKILL_OPERATIONS = {
    "cubesandbox-bench-preflight": "check",
    "cubesandbox-bench-prepare": "prepare",
    **{name: "run" for name in SKILL_SECTIONS},
}


def _reject_option(args: list[str], option: str, message: str) -> None:
    if any(arg == option or arg.startswith(option + "=") for arg in args):
        raise BenchError(message)


def _normalize_run_operation(args: list[str]) -> list[str]:
    normalized = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--operation":
            if index + 1 >= len(args):
                raise BenchError("--operation 缺少值；单场景入口固定 --operation run")
            value = args[index + 1]
            index += 2
        elif arg.startswith("--operation="):
            value = arg.split("=", 1)[1]
            index += 1
        else:
            normalized.append(arg)
            index += 1
            continue
        if value != "run":
            raise BenchError(
                f"单场景入口固定 --operation run，不能改为 {value or '空值'}；"
                "独立准备请使用 cubesandbox-bench-prepare"
            )
    return normalized


def focused_argv(skill: str, argv) -> list[str]:
    """Translate a focused public entry into the legacy, fully guarded CLI."""
    if skill not in SKILL_OPERATIONS:
        raise BenchError(f"未知 CubeSandbox 聚焦 Skill：{skill}")
    args = list(argv)
    show_plan = "--show-plan" in args
    if show_plan:
        args.remove("--show-plan")
    operation = SKILL_OPERATIONS[skill]

    if operation == "check":
        if show_plan:
            raise BenchError("preflight 只执行只读 check，不支持 --show-plan")
        if not any(arg == "--output" or arg.startswith("--output=") for arg in args):
            args.extend(["--output", str(Path.cwd() / "preflight.json")])
        return ["check", "--json", *args]

    if operation == "prepare":
        _reject_option(
            args, "--operation", "prepare 入口固定 --operation prepare，不能改为 run"
        )
        if show_plan:
            return ["show-plan", *args, "--operation", "prepare"]
        return ["prepare", *args]

    section = SKILL_SECTIONS[skill]
    _reject_option(
        args,
        "--sections",
        f"单场景入口固定 section={section}；请使用 --plan 调整本场景参数，"
        "或改用 cubesandbox-lifecycle-bench 选择其他章节",
    )
    command = "show-plan" if show_plan else "run"
    args = _normalize_run_operation(args)
    translated = [command, *args, "--sections", section]
    if show_plan:
        translated.extend(["--operation", "run"])
    return translated


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise BenchError("缺少聚焦 Skill 名称")
    skill, args = args[0], args[1:]
    return bench.main(focused_argv(skill, args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("ERROR: 用户中断；请检查日志和差集清理结果", file=sys.stderr)
        sys.exit(130)
