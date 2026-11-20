"""Core engine for the v0.27 TTFT skill.

Responsibilities:
  - discover imported vllm/vllm_ascend source roots in a target interpreter
    (stdlib only; never imports vllm in-process)
  - build strict contextual plans from payload manifests (before/after text);
    any tree that does not contextually match the "before" text refuses,
    preserving unrelated local edits
  - apply plans transactionally with a rollback state file (backups + hashes)
  - roll back to the exact pre-apply state

CLI entrypoint: python ttft_skill.py <subcommand> (see main()).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent
PAYLOAD_DIR = SKILL_ROOT.parent / "payload"


class SkillError(Exception):
    """Refusal or verification failure; the message is user-facing."""


# ---------------------------------------------------------------- discovery

DISCOVERY_CODE = r"""
import json
import sys
from pathlib import Path

result = {
    'python': sys.executable,
    'version': list(sys.version_info[:3]),
    'platform': sys.platform,
    'modules': {},
}
for name in ('vllm', 'vllm_ascend'):
    try:
        import importlib.util
        spec = importlib.util.find_spec(name)
    except Exception as exc:
        result['modules'][name] = {'error': repr(exc)}
        continue
    if spec is None or spec.origin is None:
        result['modules'][name] = {'found': False}
        continue
    path = Path(spec.origin).resolve()
    root = path.parents[1] if path.name == '__init__.py' else path.parent
    result['modules'][name] = {'found': True, 'origin': str(path), 'root': str(root)}
print(json.dumps(result))
"""


def inspect_environment(python: str) -> dict:
    """Run the target interpreter to resolve imported vllm/ascend roots."""
    proc = subprocess.run(
        [python, "-c", DISCOVERY_CODE],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise SkillError(f"环境识别失败（{python}）：{proc.stderr.strip()[:500]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SkillError(f"环境识别输出无法解析：{exc}\n{proc.stdout[:500]}") from exc


def require_roots(env: dict, repo: str) -> Path:
    key = "vllm_ascend" if repo == "ascend" else repo
    mod = env["modules"].get(key, {})
    if not mod.get("found"):
        raise SkillError(
            f"目标 Python 未安装 {key}；请在运行 vLLM 服务的同一环境/容器中执行。"
        )
    return Path(mod["root"])


@dataclass
class FilePlan:
    root: Path
    path: str
    before: str | None
    after: str
    already_applied: bool = False


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def diff_hunks(before: str, after: str) -> list[dict]:
    """Line-numbered replacement records (patch-style). Each hunk stores its
    baseline line span plus verification context."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    hunks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append(
            {
                "before_start": i1,
                "before_end": i2,
                "context": "".join(before_lines[max(0, i1 - 2) : i1]),
                "old": "".join(before_lines[i1:i2]),
                "new": "".join(after_lines[j1:j2]),
            }
        )
    return hunks


def instrument_exact(before: str, after: str) -> str:
    """Reconstruct `after` from `before` via a single SequenceMatcher opcode
    walk. Exact by construction when the current tree matches the pristine
    baseline — no anchor search, no overlap ambiguity."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(before_lines[i1:i2])
        else:
            out.extend(after_lines[j1:j2])
    return "".join(out)


def _normalize_newlines(text: str) -> str:
    """Accept CRLF files (Windows-edited) by comparing LF-normalized text.
    The instrumented output keeps LF endings, matching the payload."""
    return text.replace("\r\n", "\n")


def file_is_instrumented(current: str, before: str, after: str) -> bool:
    """True when the current text already contains every diagnostic hunk's
    inserted/replacement text near its baseline position (allowing unrelated
    local-edit line drift). Used for idempotent re-apply detection."""
    current = _normalize_newlines(current)
    hunks = diff_hunks(before, after)
    lines = current.splitlines(keepends=True)
    for hunk in hunks:
        expected = hunk["new"]
        expected_lines = len(expected.splitlines())
        lo = max(0, hunk["before_start"] - 2 - 200)
        hi = min(len(lines), hunk["before_start"] + 400)
        if not any(
            "".join(lines[cand : cand + expected_lines]) == expected
            for cand in range(lo, hi)
        ):
            return False
    return True


def build_file_plan(root: Path, entry: dict) -> FilePlan:
    """Plan one file. Pristine trees take the exact opcode reconstruction;
    trees with unrelated local edits keep them via per-hunk window search;
    anything matching neither baseline nor instrumented state refuses."""
    target = root / entry["path"]
    current = None
    if target.exists():
        current = _normalize_newlines(target.read_text(encoding="utf-8"))
    before, after = entry["before"], entry["after"]
    if current == after:
        return FilePlan(root, entry["path"], before, after, already_applied=True)
    if before is None:
        if current is not None:
            raise SkillError(
                f"{entry['path']}: 打点会新建该文件，但目标已存在；拒绝执行。"
            )
        return FilePlan(root, entry["path"], None, after)
    if current is None:
        raise SkillError(
            f"{entry['path']}: 基线文件不存在；目标源码与 0.27 打点基线不符。"
        )
    if file_is_instrumented(current, before, after):
        return FilePlan(root, entry["path"], before, current, already_applied=True)
    if current == before:
        # Pristine baseline: reconstruction is byte-identical to this
        # revision's payload after text by construction.
        new_text = instrument_exact(before, after)
    else:
        # Unrelated local edits elsewhere: keep them via three-way mapping.
        try:
            new_text = apply_mapped(current, before, after)
        except SkillError as exc:
            raise SkillError(
                f"{entry['path']}: 当前文件与 0.27 基线不匹配"
                f"（版本不符或含未知本地修改）；拒绝执行。（{exc}）"
            ) from exc
    return FilePlan(root, entry["path"], before, new_text)


def apply_mapped(current: str, before: str, after: str) -> str:
    """Three-way instrumentation for trees with unrelated local edits.

    Aligns the current file against the baseline with SequenceMatcher, maps
    every hunk's (context + old) span onto current line indices, verifies it
    byte-for-byte there, then rewrites each span with (context + new).
    Local edits outside hunk regions pass through untouched. Overlapping
    contexts between adjacent hunks are emitted once. Refuses when a local
    edit lands inside a hunk region or breaks its contiguity.
    """
    current = _normalize_newlines(current)
    cur_lines = current.splitlines(keepends=True)
    b_lines = before.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, b_lines, cur_lines, autojunk=False)
    fwd: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                fwd[i1 + k] = j1 + k
    out = []
    src = 0
    for hunk in diff_hunks(before, after):
        ctx_n = len(hunk["context"].splitlines())
        span = ctx_n + hunk["before_end"] - hunk["before_start"]
        idxs = [fwd.get(hunk["before_start"] - ctx_n + k) for k in range(span)]
        if any(i is None for i in idxs):
            raise SkillError("本地修改落在打点区域内")
        if not all(idxs[k + 1] == idxs[k] + 1 for k in range(len(idxs) - 1)):
            raise SkillError("打点区域在当前文件中不连续")
        cpos = idxs[0]
        expected = hunk["context"] + hunk["old"]
        n = len(expected.splitlines())
        if "".join(cur_lines[cpos : cpos + n]) != expected:
            raise SkillError("映射后上下文不匹配")
        out.extend(cur_lines[src:cpos])
        ctx_lines = hunk["context"].splitlines(keepends=True)
        out.extend(ctx_lines[max(0, src - cpos) :])
        out.append(hunk["new"])
        src = cpos + n
    out.extend(cur_lines[src:])
    return "".join(out)


# ---------------------------------------------------------------- apply/rollback


def plan_entries(root: Path, entries: list[dict]) -> list[FilePlan]:
    return [build_file_plan(root, e) for e in entries]


def apply_transaction(plans: list[FilePlan], state_dir: Path, metadata: dict) -> Path:
    """Write all planned files atomically; state file enables rollback."""
    state_path = state_dir / "state.json"
    if state_path.exists():
        raise SkillError(
            f"回滚状态已存在，拒绝覆盖：{state_path}；请使用新的独立 --state-dir。"
        )
    pending = [p for p in plans if not p.already_applied]
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "metadata": metadata,
        "files": [
            {
                "root": str(p.root),
                "path": p.path,
                "backup": (p.root / p.path).read_text(encoding="utf-8")
                if (p.root / p.path).exists()
                else None,
                "backup_sha256": sha256_text(
                    (p.root / p.path).read_text(encoding="utf-8")
                )
                if (p.root / p.path).exists()
                else None,
                "after_sha256": sha256_text(p.after),
            }
            for p in pending
        ],
    }
    # Verify every plan still matches before writing anything.
    for p in pending:
        rebuilt = build_file_plan(
            p.root, {"path": p.path, "before": p.before, "after": p.after}
        )
        if rebuilt.already_applied:
            continue
    for p in pending:
        target = p.root / p.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(p.after, encoding="utf-8", newline="\n")
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return state_path


def rollback_state(state_path: Path) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    # Preflight: verify every file still holds exactly what we wrote
    # (after_sha256) BEFORE restoring anything. A mismatch means someone
    # edited files post-apply; refuse rather than clobber their changes —
    # and refuse atomically, so no file is left half-restored.
    for record in state["files"]:
        target = Path(record["root"]) / record["path"]
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if (
            record["after_sha256"] is not None
            and current is not None
            and sha256_text(current) != record["after_sha256"]
        ):
            raise SkillError(
                f"{record['path']}: 应用后文件被再次修改，拒绝回滚；请人工检查。"
            )
    # All hashes verified — safe to restore the whole tree.
    for record in state["files"]:
        target = Path(record["root"]) / record["path"]
        if record["backup"] is None:
            if target.exists():
                target.unlink()
        else:
            target.write_text(record["backup"], encoding="utf-8", newline="\n")


def load_manifest(name: str) -> dict:
    path = PAYLOAD_DIR / name
    if not path.exists():
        raise SkillError(f"找不到 payload：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- CLI


def register_cli(subparsers) -> None:
    """Mount core subcommands on an external parser (see scripts/ttft.py)."""
    p_inspect = subparsers.add_parser("inspect", help="识别目标环境的源码位置")
    p_inspect.add_argument("--python", default=sys.executable)
    p_inspect.set_defaults(func=cmd_inspect_cli)

    p_plan = subparsers.add_parser("plan", help="生成打点计划（只读）")
    p_plan.add_argument("--python", default=sys.executable)
    p_plan.add_argument("--manifest", default="vllm-ascend-timing.json")
    p_plan.add_argument("--repo", choices=("vllm", "ascend"), default="vllm")
    p_plan.set_defaults(func=cmd_plan_cli)
    p_apply = subparsers.add_parser("apply", help="执行打点（可回滚）")
    p_apply.add_argument("--python", default=sys.executable)
    p_apply.add_argument("--manifest", default="vllm-ascend-timing.json")
    p_apply.add_argument("--repo", choices=("vllm", "ascend"), default="vllm")
    p_apply.add_argument("--state-dir", required=True)
    p_apply.set_defaults(func=cmd_apply_cli)

    p_rollback = subparsers.add_parser("rollback", help="回滚到应用前状态")
    p_rollback.add_argument("--state-file", required=True)
    p_rollback.set_defaults(func=cmd_rollback_cli)


def cmd_rollback_cli(args) -> int:
    rollback_state(Path(args.state_file))
    print("回滚完成。")
    return 0


def cmd_inspect_cli(args) -> int:
    env = inspect_environment(args.python)
    print(json.dumps(env, ensure_ascii=False, indent=1))
    return 0


def cmd_plan_cli(args) -> int:
    env = inspect_environment(args.python)
    root = require_roots(env, args.repo)
    manifest = load_manifest(args.manifest)
    profile = next(p for p in manifest["profiles"] if args.repo in p["repos"])
    plans = plan_entries(root, profile["repos"][args.repo])
    applied = sum(1 for p in plans if p.already_applied)
    print(f"{args.repo} root: {root}")
    print(f"文件 {len(plans)} 个，其中 {applied} 个已包含打点。")
    print(
        f"内容检查通过（payload: {profile['name']}）；plan/apply 不以 Git HEAD 为门禁。"
    )
    print(
        "此结果不是版本/HEAD 认证或真实服务验证；apply 会重新检查内容，仍需用户确认。"
    )
    for p in plans:
        mark = "OK " if p.already_applied else "   "
        print(f"  {mark}{p.path}")
    return 0


def cmd_apply_cli(args) -> int:
    state_path = Path(args.state_dir) / "state.json"
    if state_path.exists():
        raise SkillError(
            f"回滚状态已存在，拒绝覆盖：{state_path}；请使用新的独立 --state-dir。"
        )
    env = inspect_environment(args.python)
    root = require_roots(env, args.repo)
    manifest = load_manifest(args.manifest)
    profile = next(p for p in manifest["profiles"] if args.repo in p["repos"])
    plans = plan_entries(root, profile["repos"][args.repo])
    pending = [p for p in plans if not p.already_applied]
    if not pending:
        print("所有文件已包含打点，无需修改。")
        return 0
    state_path = apply_transaction(
        plans,
        Path(args.state_dir),
        {"repo": args.repo, "manifest": args.manifest, "profile": profile["name"]},
    )
    print(f"已写入 {len(pending)} 个文件。回滚状态：{state_path}")
    print(f'回滚命令：python ttft.py rollback --state-file "{state_path}"')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register_cli(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
