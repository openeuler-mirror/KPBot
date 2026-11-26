#!/usr/bin/env python3
"""vLLM-Ascend 成熟版本登记。

口径（用户确认）：vllm-ascend 有发布 tag 的版本都算成熟版本。
对每个 tag 记录：日期、commit、ModelRunner v1/v2 是否存在、配对的 vllm 版本。

只读：只用 git 读取 tag/日期/文件存在性，不 checkout、不 fetch、不执行源码。

用法：
  python3 scripts/versions.py --ascend-repo /path/vllm-ascend \
      [--vllm-repo /path/vllm] [--json versions.json]

输出：versions.json（schema_version=1）+ 终端表格。
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?(?:\.post(\d+))?$")
MR_V1 = "vllm_ascend/worker/model_runner_v1.py"
MR_V2 = "vllm_ascend/worker/v2/model_runner.py"
PAIR_FILE = ".github/vllm-release-tag.commit"
POLICY_PATHS = [
    "docs/source/community/versioning_policy.md",
    "docs/source/developer_guide/versioning_policy.md",
]
ONLINE_URL = (
    "https://raw.githubusercontent.com/vllm-project/vllm-ascend/main/"
    "docs/source/user_guide/release_notes.md"
)
HEAD_RE = re.compile(r"^##\s+(v[0-9][0-9A-Za-z.\-+]*)")


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_repo(repo):
    p = Path(repo)
    if not p.is_dir():
        return False
    proc = _git(repo, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def semkey(tag):
    """语义版本排序键；同版本 final 排在 rc 之后。"""
    m = VERSION_RE.match(tag)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    rc, post = m.group(4), m.group(5)
    if post is not None:
        stage, num = 2, int(post)
    elif rc is not None:
        stage, num = 0, int(rc)
    else:
        stage, num = 1, 0
    return (major, minor, patch, stage, num)


def list_version_tags(repo):
    proc = _git(repo, "tag", "--list")
    if proc.returncode != 0:
        return []
    tags = [t.strip() for t in proc.stdout.splitlines() if semkey(t.strip())]
    return sorted(set(tags), key=semkey)


def _tag_date(repo, tag):
    proc = _git(repo, "log", "-1", "--format=%ad", "--date=short", tag)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _tag_commit(repo, tag):
    proc = _git(repo, "rev-parse", f"{tag}^{{commit}}")
    return proc.stdout.strip()[:12] if proc.returncode == 0 else None


def _has_file(repo, tag, path):
    return _git(repo, "cat-file", "-e", f"{tag}:{path}").returncode == 0


def _pair_commit(repo, tag):
    proc = _git(repo, "show", f"{tag}:{PAIR_FILE}")
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip().splitlines()
    return value[0].strip() if value and value[0].strip() else None


def _read_file(repo, tag, path):
    proc = _git(repo, "show", f"{tag}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def _parse_matrix(text):
    """解析 versioning_policy.md 的 release compatibility matrix。"""
    rows, header = {}, None
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or not cells:
            continue
        if header is None:
            if cells[0].lower().startswith("vllm ascend"):
                header = cells
            continue
        if not any(c and set(c) - set("-: ") for c in cells):
            continue
        if len(cells) != len(header):
            continue
        row = dict(zip(header, cells))
        version = cells[0]
        if not version.startswith("v"):
            continue
        rows[version] = {
            "vllm": row.get("vLLM"),
            "python": row.get("Python"),
            "cann": row.get("Stable CANN"),
            "torch_npu": row.get("PyTorch/torch_npu"),
            "triton_ascend": row.get("Triton Ascend"),
            "mooncake": row.get("Mooncake"),
        }
    return rows


def _compat_for_tag(repo, tag):
    for path in POLICY_PATHS:
        text = _read_file(repo, tag, path)
        if text:
            rows = _parse_matrix(text)
            if rows:
                return rows
    return {}


def _pair_version(ascend_tag, compat, vllm_tags, vllm_repo, ascend_repo):
    """返回 (paired_ref, basis, confidence)。"""
    cap = compat.get(ascend_tag) or {}
    cap_vllm = (cap.get("vllm") or "").split("/")[0].strip()
    if cap_vllm:
        return cap_vllm, "versioning_policy matrix", "high"
    commit = _pair_commit(ascend_repo, ascend_tag)
    if commit:
        return commit, PAIR_FILE, "high"
    if vllm_repo and ascend_tag in vllm_tags:
        return ascend_tag, "exact-tag", "high"
    if vllm_repo:
        key = semkey(ascend_tag)
        lower = [t for t in vllm_tags if semkey(t) <= key]
        if lower:
            return max(lower, key=semkey), "nearest-lower-tag", "medium"
    return None, None, "low"


def fetch_online_versions(url=ONLINE_URL, timeout=30, retries=2):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "kptools-vllm-src-analysis"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            return [m.group(1) for line in text.splitlines() if (m := HEAD_RE.match(line))]
        except Exception as exc:  # 网络抖动重试一次
            last = exc
    raise last


def diff_online(local_versions, online_versions):
    local = set(local_versions)
    return [v for v in online_versions if v not in local]


def collect(ascend_repo, vllm_repo=None, online=False, online_url=None):
    if not _is_repo(ascend_repo):
        raise RuntimeError(f"不是 git 仓库: {ascend_repo}")
    vllm_ok = bool(vllm_repo) and _is_repo(vllm_repo)
    vllm_tags = list_version_tags(vllm_repo) if vllm_ok else []
    degradations = []
    if vllm_repo and not vllm_ok:
        degradations.append(f"vllm 仓库不可读，配对降级为 low: {vllm_repo}")
    elif not vllm_repo:
        degradations.append("未提供 --vllm-repo：缺兼容矩阵/配对文件的版本配对置信度会降低")

    tags = list_version_tags(ascend_repo)
    if not tags:
        raise RuntimeError(
            f"没有 vX.Y.Z[rcN|.postN] 形式的 tag: {ascend_repo}"
        )
    # 早期 tag 的矩阵可能还没有自己的行，用最新 tag 的矩阵补全（不覆盖已有行）
    fallback = _compat_for_tag(ascend_repo, tags[-1]) if tags else {}
    rows = []
    for tag in tags:
        compat = dict(fallback)
        compat.update(_compat_for_tag(ascend_repo, tag))
        paired, basis, conf = _pair_version(tag, compat, vllm_tags, vllm_ok, ascend_repo)
        rows.append(
            {
                "version": tag,
                "date": _tag_date(ascend_repo, tag),
                "commit": _tag_commit(ascend_repo, tag),
                "paired_vllm_ref": paired,
                "pairing_basis": basis,
                "pairing_confidence": conf,
                "compat": compat.get(tag),
                "model_runner": {
                    "v1": _has_file(ascend_repo, tag, MR_V1),
                    "v2": _has_file(ascend_repo, tag, MR_V2),
                },
            }
        )
    if not any(r["compat"] for r in rows):
        degradations.append("未解析到 versioning_policy 兼容矩阵，配对降级为 tag 推断")

    online_info = None
    if online:
        url = online_url or ONLINE_URL
        try:
            online_versions = fetch_online_versions(url)
            missing = diff_online([r["version"] for r in rows], online_versions)
            online_info = {
                "source": url,
                "versions": online_versions,
                "missing_locally": missing,
            }
            if missing:
                degradations.append(
                    "线上存在本地 tag 未包含的版本: " + ", ".join(missing)
                )
        except Exception as exc:
            degradations.append(f"线上版本列表获取失败: {exc}")

    return {
        "schema_version": 1,
        "policy": "mature = vllm-ascend 有发布 tag 的版本（用户确认口径）",
        "sources": {
            "ascend_repo": str(Path(ascend_repo).resolve()),
            "vllm_repo": str(Path(vllm_repo).resolve()) if vllm_ok else None,
        },
        "model_runner_paths": {"v1": MR_V1, "v2": MR_V2},
        "versions": rows,
        "online": online_info,
        "degradations": degradations,
    }


def _print_table(result):
    print(f"{'version':<14}{'date':<12}{'v1':<4}{'v2':<4}{'paired_vllm':<22}basis")
    for row in result["versions"]:
        print(
            f"{row['version']:<14}{row['date'] or '?':<12}"
            f"{'Y' if row['model_runner']['v1'] else '-':<4}"
            f"{'Y' if row['model_runner']['v2'] else '-':<4}"
            f"{(row['paired_vllm_ref'] or '?'):<22}{row['pairing_basis'] or '?'}"
        )
    for note in result["degradations"]:
        print(f"[degraded] {note}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="vLLM-Ascend 成熟版本登记")
    ap.add_argument("--ascend-repo", required=True)
    ap.add_argument("--vllm-repo")
    ap.add_argument("--online", action="store_true", help="对比官方线上版本列表，标出本地缺失 tag")
    ap.add_argument("--online-url")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    try:
        result = collect(args.ascend_repo, args.vllm_repo, args.online, args.online_url)
    except Exception as exc:  # 降级：始终产出结构化结果
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
    _print_table(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
