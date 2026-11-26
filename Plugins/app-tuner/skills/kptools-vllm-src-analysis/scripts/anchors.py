#!/usr/bin/env python3
"""按版本生成源码锚点，供 profiling 分析 skill 做调用栈定位。

不是 trace 分析：这里只从 git tag 里读源码、用 ast 解析出「哪个文件/类/方法在第几行」，
并支持两个版本之间 diff（新增/消失/行号漂移）。不 checkout、不执行、不修改工作区。

用法：
  python3 scripts/anchors.py --repo /path/vllm-ascend --version v0.30.0rc1 --json a.json
  python3 scripts/anchors.py --repo /path/vllm-ascend --version v0.30.0rc1 \
      --diff v0.19.1rc1 --json a.json
"""

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

ASCEND_PREFIXES = [
    "vllm_ascend/worker",
    "vllm_ascend/attention",
    "vllm_ascend/platform",
    "vllm_ascend/ops",
    "vllm_ascend/distributed",
    "vllm_ascend/compilation",
]
VLLM_PREFIXES = [
    "vllm/v1/worker",
    "vllm/v1/executor",
    "vllm/v1/core/sched",
    "vllm/v1/attention",
    "vllm/v1/engine",
]
EXCLUDE_PARTS = ("/tests/", "/test/", "/benchmarks/", "/examples/")


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


def _resolve_commit(repo, version):
    proc = _git(repo, "rev-parse", f"{version}^{{commit}}")
    if proc.returncode != 0:
        raise RuntimeError(f"未知版本/tag: {version}")
    return proc.stdout.strip()


def _default_prefixes(repo, version):
    proc = _git(repo, "ls-tree", "--name-only", version)
    top = {line.strip() for line in proc.stdout.splitlines()}
    if "vllm_ascend" in top:
        return ASCEND_PREFIXES
    if "vllm" in top:
        return VLLM_PREFIXES
    return ["."]


def _list_files(repo, version, prefixes):
    proc = _git(repo, "ls-tree", "-r", "--name-only", version, "--", *prefixes)
    if proc.returncode != 0:
        raise RuntimeError(f"无法列出文件: {version}")
    files = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path.endswith(".py"):
            continue
        if any(part in path for part in EXCLUDE_PARTS):
            continue
        files.append(path)
    return sorted(files)


def _decorators(node):
    names = []
    for dec in getattr(node, "decorator_list", []):
        try:
            names.append(ast.unparse(dec))
        except Exception:
            names.append("?")
    return names


def _record_functions(nodes):
    out = []
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", None),
                    "async": isinstance(node, ast.AsyncFunctionDef),
                    "decorators": _decorators(node),
                }
            )
    return out


def _record_classes(nodes):
    out = []
    for node in nodes:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                bases.append("?")
        out.append(
            {
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", None),
                "bases": bases,
                "decorators": _decorators(node),
                "methods": _record_functions(node.body),
            }
        )
    return out


def _read(repo, version, path):
    proc = _git(repo, "show", f"{version}:{path}")
    if proc.returncode != 0:
        raise RuntimeError(f"无法读取 {version}:{path}")
    return proc.stdout


def scan(repo, version, includes=None, max_files=800):
    if not _is_repo(repo):
        raise RuntimeError(f"不是 git 仓库: {repo}")
    commit = _resolve_commit(repo, version)
    prefixes = includes or _default_prefixes(repo, version)
    all_paths = _list_files(repo, version, prefixes)
    paths = list(all_paths)
    degradations = []
    if len(paths) > max_files:
        degradations.append(f"文件数 {len(paths)} 超过 max_files={max_files}，已截断")
        paths = paths[:max_files]

    files, parse_errors = [], []
    for path in paths:
        try:
            source = _read(repo, version, path)
            tree = ast.parse(source)
        except SyntaxError as exc:
            parse_errors.append({"path": path, "error": f"SyntaxError: {exc.msg} (line {exc.lineno})"})
            continue
        except Exception as exc:
            parse_errors.append({"path": path, "error": str(exc)})
            continue
        files.append(
            {
                "path": path,
                "classes": _record_classes(tree.body),
                "functions": _record_functions(tree.body),
                "lines": len(source.splitlines()),
            }
        )

    class_count = sum(len(f["classes"]) for f in files)
    method_count = sum(len(c["methods"]) for f in files for c in f["classes"])
    return {
        "schema_version": 1,
        "repo": str(Path(repo).resolve()),
        "version": version,
        "commit": commit[:12],
        "includes": prefixes,
        "files": files,
        "file_paths": all_paths,
        "parse_errors": parse_errors,
        "degradations": degradations,
        "counts": {
            "files": len(files),
            "classes": class_count,
            "methods": method_count,
            "functions": sum(len(f["functions"]) for f in files),
        },
    }


def _symbol_index(scan_result):
    """(path, kind, name) -> line；方法名用 Class.method。"""
    index = {}
    for f in scan_result["files"]:
        path = f["path"]
        for fn in f["functions"]:
            index[(path, "function", fn["name"])] = fn["line"]
        for cls in f["classes"]:
            index[(path, "class", cls["name"])] = cls["line"]
            for m in cls["methods"]:
                index[(path, "method", f"{cls['name']}.{m['name']}")] = m["line"]
    return index


def diff_scans(base, cand):
    # 文件增删用完整 git 文件清单（不受 AST 解析失败/截断影响）。
    base_files = set(base.get("file_paths") or [f["path"] for f in base["files"]])
    cand_files = set(cand.get("file_paths") or [f["path"] for f in cand["files"]])
    # 符号差异只比较两侧都成功解析的文件；解析失败/被截断的文件不产生幻影增删。
    base_parsed = {f["path"] for f in base["files"]}
    cand_parsed = {f["path"] for f in cand["files"]}
    b_idx, c_idx = _symbol_index(base), _symbol_index(cand)

    added, removed, moved = [], [], []
    for key in sorted(set(c_idx) - set(b_idx)):
        path = key[0]
        # 新增文件：路径不在 base 文件清单即可报新增；路径存在但 base 未解析成功则跳过。
        if path in cand_parsed and (path not in base_files or path in base_parsed):
            added.append({"path": path, "kind": key[1], "name": key[2], "line": c_idx[key]})
    for key in sorted(set(b_idx) - set(c_idx)):
        path = key[0]
        if path in base_parsed and (path not in cand_files or path in cand_parsed):
            removed.append({"path": path, "kind": key[1], "name": key[2], "line": b_idx[key]})
    for key in sorted(set(b_idx) & set(c_idx)):
        if key[0] in base_parsed and key[0] in cand_parsed and b_idx[key] != c_idx[key]:
            moved.append(
                {
                    "path": key[0],
                    "kind": key[1],
                    "name": key[2],
                    "base_line": b_idx[key],
                    "cand_line": c_idx[key],
                    "delta": c_idx[key] - b_idx[key],
                }
            )
    return {
        "base_version": base["version"],
        "cand_version": cand["version"],
        "added_files": sorted(cand_files - base_files),
        "removed_files": sorted(base_files - cand_files),
        "added_symbols": added,
        "removed_symbols": removed,
        "moved_symbols": moved,
        "counts": {
            "added_files": len(cand_files - base_files),
            "removed_files": len(base_files - cand_files),
            "added_symbols": len(added),
            "removed_symbols": len(removed),
            "moved_symbols": len(moved),
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="按版本生成源码锚点")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--diff", dest="diff_version")
    ap.add_argument("--include", action="append", dest="includes")
    ap.add_argument("--max-files", type=int, default=800)
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    try:
        result = scan(args.repo, args.version, args.includes, args.max_files)
        if args.diff_version:
            base = scan(args.repo, args.diff_version, args.includes, args.max_files)
            result["diff"] = diff_scans(base, result)
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
    c = result["counts"]
    print(f"{result['version']} @ {result['commit']}: files={c['files']} classes={c['classes']} methods={c['methods']}")
    if "diff" in result:
        d = result["diff"]["counts"]
        print(
            f"diff {result['diff']['base_version']}..{result['diff']['cand_version']}: "
            f"+sym={d['added_symbols']} -sym={d['removed_symbols']} moved={d['moved_symbols']}"
        )
    for note in result["degradations"]:
        print(f"[degraded] {note}", file=sys.stderr)
    if result["parse_errors"]:
        print(f"[warn] {len(result['parse_errors'])} 个文件解析失败（见 JSON）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
