#!/usr/bin/env python3
"""创建 simd_optimization_result 工作区。

位置固定为被优化项目的**同级目录**，不放进项目内部：结果目录里有编译产物、
样本数据和 A/B 二进制，放进项目会污染 .gitignore、构建系统和代码搜索。

用法:
    python3 init_workspace.py --project /path/to/project
    python3 init_workspace.py --project /path/to/project --workspace /custom/dir

输出: 一行 JSON，含实际创建的路径。已存在的目录和文件不会被覆盖。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SUBDIRS = [
    "process",
    "test",
    "build",
    "build/logs",
    "other",
    "other/hotspots",
    "other/hotspots/raw",
    "other/web-search",
]

STATE_FILENAME = "state.json"


def fail(msg, **extra):
    payload = {"success": False, "error_message": msg}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="被优化的项目根目录")
    ap.add_argument("--workspace", default=None,
                    help="结果目录路径；缺省为项目同级目录下的 simd_optimization_result")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（默认即如此，保留兼容）")
    args = ap.parse_args(argv)

    project = os.path.realpath(os.path.expanduser(args.project))
    if not os.path.isdir(project):
        return fail("project_not_a_directory", project=project)

    if args.workspace:
        workspace = os.path.realpath(os.path.expanduser(args.workspace))
    else:
        parent = os.path.dirname(project)
        workspace = os.path.join(parent, "simd_optimization_result")

    if workspace == project or workspace.startswith(project + os.sep):
        return fail("workspace_inside_project",
                    workspace=workspace,
                    hint="结果目录必须在项目之外；默认是同级目录，请去掉 --workspace 或用其它路径")

    created, existed = [], []
    for rel in [""] + SUBDIRS:
        path = os.path.join(workspace, rel) if rel else workspace
        if os.path.isdir(path):
            existed.append(rel or ".")
        else:
            os.makedirs(path, exist_ok=True)
            created.append(rel or ".")

    # 结果目录自身不入库，避免被上层仓库意外收进去
    gitignore = os.path.join(workspace, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w", encoding="utf-8") as fh:
            fh.write("# 本目录是优化过程产物，不入库\n*\n")
        created.append(".gitignore")

    state_path = os.path.join(workspace, "other", STATE_FILENAME)
    if not os.path.exists(state_path):
        state = {
            "schema": 1,
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "project": project,
            "workspace": workspace,
            "run_started_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "mode": "",
            "hotspot_source": "",
            "verify_method": "",
            "answers": {},
            "queue": [],
            "cursor": 0,
            "completed": [],
            "rejected": [],
            "status": "initialized",
            "notes": "",
        }
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        created.append("other/" + STATE_FILENAME)

    print(json.dumps({
        "success": True,
        "project": project,
        "workspace": workspace,
        "created": created,
        "existed": existed,
        "report_path": os.path.join(workspace, "simd_optimization_report.md"),
        "state_path": state_path,
        "error_message": "",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
