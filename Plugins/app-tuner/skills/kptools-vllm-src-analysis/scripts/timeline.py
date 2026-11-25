#!/usr/bin/env python3
"""特性 / 文件在 vLLM 版本线上的四态时间线。

"某个东西是哪版引入的" 必须拆成四态，否则一定答错：
  1. introduced_code  —— 代码提交引入（commit 级）
  2. first_release   —— 首个包含它的发布 tag
  3. default_on      —— 首个默认开启的 tag（需 --default-regex）
  4. removed         —— 首个移除它的 tag（如存在）

只读：git tag / git grep / git log -S，不 checkout、不 fetch、不执行源码。

用法：
  # 文件何时出现/消失
  python3 scripts/timeline.py --repo /path/vllm-ascend \
      --path vllm_ascend/worker/v2/model_runner.py --json tl.json
  # 符号何时引入、何时默认开、何时移除
  # 默认开关的默认值定义在 vllm 仓库 envs.py（vllm_ascend 侧只读取），
  # default_on 必须扫默认值所在仓库，否则永远是 unknown。
  python3 scripts/timeline.py --repo /path/vllm \
      --symbol VLLM_USE_V2_MODEL_RUNNER --scope vllm/envs.py \
      --default-regex 'VLLM_USE_V2_MODEL_RUNNER[[:space:]]*[:=][[:space:]]*.*True' --json tl.json
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?(?:\.post(\d+))?$")


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


def semkey(tag):
    m = VERSION_RE.match(tag)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    rc, post = m.group(4), m.group(5)
    if post is not None:      # 发布后补丁版排在正式版之后
        stage, num = 2, int(post)
    elif rc is not None:      # rc 排在正式版之前
        stage, num = 0, int(rc)
    else:
        stage, num = 1, 0
    return (major, minor, patch, stage, num)


def list_version_tags(repo):
    proc = _git(repo, "tag", "--list")
    tags = [t.strip() for t in proc.stdout.splitlines() if semkey(t.strip())]
    return sorted(set(tags), key=semkey)


def _grep_rc(repo, tag, pattern, fixed, scope):
    args = ["grep", "-q", "-I"]
    args.append("-F" if fixed else "-E")
    args += ["-e", pattern, tag, "--", scope or "."]
    return _git(repo, *args).returncode


def _grep(repo, tag, pattern, fixed, scope):
    return _grep_rc(repo, tag, pattern, fixed, scope) == 0


def _presence(repo, tag, path, symbol, scope):
    if path:
        return _git(repo, "cat-file", "-e", f"{tag}:{path}").returncode == 0
    return _grep(repo, tag, symbol, True, scope)


def _is_default(repo, tag, default_regex, scope):
    rc = _grep_rc(repo, tag, default_regex, False, scope)
    # git grep: 0=命中，1=未命中，>=2 为正则/执行错误，不能当“未命中”处理。
    if rc > 1:
        raise RuntimeError(
            f"git grep 执行失败（rc={rc}），请检查 --default-regex 是否为正则: {default_regex!r}"
        )
    return rc == 0


def _first_commit(repo, path, symbol, scope):
    if path:
        args = ["log", "--all", "--reverse", "--diff-filter=A", "--format=%H", "--", path]
    else:
        args = ["log", "--all", "--reverse", "--format=%H", "-S", symbol, "--", scope or "."]
    proc = _git(repo, *args)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


def _commit_info(repo, sha):
    if not sha:
        return None
    proc = _git(repo, "log", "-1", "--format=%H%x09%ad%x09%s", "--date=short", sha)
    if proc.returncode != 0:
        return None
    parts = proc.stdout.strip().split("\t", 2)
    if len(parts) != 3:
        return None
    return {"commit": parts[0][:12], "date": parts[1], "subject": parts[2]}


def _tags_containing(repo, sha, tags):
    proc = _git(repo, "tag", "--contains", sha)
    if proc.returncode != 0:
        return []
    containing = {t.strip() for t in proc.stdout.splitlines()}
    return [t for t in tags if t in containing]


def analyze(repo, path=None, symbol=None, default_regex=None, scope=None):
    if not _is_repo(repo):
        raise RuntimeError(f"不是 git 仓库: {repo}")
    if bool(path) == bool(symbol):
        raise ValueError("必须且只能指定 --path 或 --symbol 之一")

    tags = list_version_tags(repo)
    if not tags:
        raise RuntimeError(f"没有 vX.Y.Z[rcN] 形式的 tag: {repo}")

    present = {t: _presence(repo, t, path, symbol, scope) for t in tags}
    available = [t for t in tags if present[t]]

    intro_sha = _first_commit(repo, path, symbol, scope)
    intro_info = _commit_info(repo, intro_sha)
    containing = _tags_containing(repo, intro_sha, tags) if intro_sha else []
    # `git tag --contains` 会命中“文件在 tag 前已添加又在 tag 前撤回”的版本，
    # 因此 first_release 必须以逐 tag 验证过的 present 结果为准。
    first_release = available[0] if available else None

    default_on = None
    if default_regex:
        for tag in available:
            if _is_default(repo, tag, default_regex, scope):
                default_on = tag
                break

    # removed = 第一次出现「存在→缺失」的版本（不是最后一次出现之后）。
    removed = None
    for i in range(1, len(tags)):
        if present[tags[i - 1]] and not present[tags[i]]:
            removed = tags[i]
            break

    gaps = []
    if available:
        lo, hi = tags.index(available[0]), tags.index(available[-1])
        gaps = [t for t in tags[lo : hi + 1] if not present[t]]

    if not any(present.values()):
        confidence = "low"
    elif symbol or gaps or (first_release is None):
        confidence = "medium"
    else:
        confidence = "high"
    if default_regex and default_on is None:
        confidence = "medium" if confidence == "high" else confidence

    notes = []
    if first_release and intro_info is None:
        notes.append("引入提交早于仓库可追溯历史，introduced_code=unknown")
    if intro_info and not containing:
        notes.append("引入提交不在任何版本 tag 的祖先链上，first_release 取首个命中 tag，可信度降低")
    if default_regex and default_on is None and available:
        notes.append("未找到匹配 default-regex 的 tag，default_on=unknown")
    if gaps:
        notes.append("版本线存在中间缺失（曾出现→消失→再现），见 gaps")
    if not available:
        notes.append("所有 tag 均未命中，可能特性名/路径有误或早于首个 tag")

    return {
        "schema_version": 1,
        "query": {
            "repo": str(Path(repo).resolve()),
            "kind": "path" if path else "symbol",
            "path": path,
            "symbol": symbol,
            "scope": scope,
            "default_regex": default_regex,
        },
        "states": {
            "introduced_code": intro_info,
            "first_release": first_release,
            "default_on": default_on,
            "removed": removed,
        },
        "available_versions": available,
        "per_version": [{"version": t, "present": present[t]} for t in tags],
        "gaps": gaps,
        "confidence": confidence,
        "notes": notes,
        "evidence": [
            f"git tag --list ({len(tags)} 个版本 tag)",
            f"git grep {'-F' if symbol else 'cat-file'} per tag",
            f"git log -S / --diff-filter=A => {intro_info['commit'] if intro_info else 'unknown'}",
        ],
    }


def _print_summary(result):
    q = result["query"]
    subject = q.get("path") or q.get("symbol")
    st = result["states"]
    intro = st["introduced_code"]
    print(f"query: {q['kind']} {subject}")
    print(f"introduced_code: {intro['commit'] + ' ' + intro['date'] if intro else 'unknown'}")
    print(f"first_release:   {st['first_release'] or 'unknown'}")
    print(f"default_on:      {st['default_on'] or 'unknown'}")
    print(f"removed:         {st['removed'] or 'unknown'}")
    print(f"available:       {len(result['available_versions'])} 个版本")
    print(f"confidence:      {result['confidence']}")
    for note in result["notes"]:
        print(f"note: {note}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="特性四态时间线")
    ap.add_argument("--repo", required=True)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--path", help="仓库内相对路径，追踪其存在性")
    group.add_argument("--symbol", help="精确文本，追踪其出现/消失")
    ap.add_argument("--scope", help="git grep 的路径范围，如 vllm_ascend")
    ap.add_argument("--default-regex", help="判定默认开启的正则（-E）")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    try:
        result = analyze(args.repo, args.path, args.symbol, args.default_regex, args.scope)
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
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
