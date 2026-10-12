#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openwiki_sync.py — KPBot openwiki-sync skill 的确定性同步引擎。

职责（可脚本化的机械操作，不含 LLM 判断）：
  status      探测代码仓状态并输出场景判定（JSON）
  link        建立代码仓 <-> wiki 仓关联（仓内 .openwiki-link.json + 全局注册表）
  unlink      解除关联（保留本地 openwiki/ 内容）
  restore     从 wiki 仓还原 openwiki/ 到本地（新机器 / 新 clone 场景）
  push        把本地 openwiki/ 镜像推送到 wiki 仓并生成 commit
  create-repo 通过平台 API 创建 wiki 仓（gitcode / gitee / github）
  ignore      把 openwiki/ 与 .openwiki-sync/ 加入 .gitignore

设计要点：
  - wiki 仓是唯一持久存储；代码仓内 openwiki/ 为工作副本（gitignore 掉）。
  - OpenWiki 的增量更新依赖 openwiki/ 内的 .claims/、.last-update.json、
    .page-manifest.json，这些都会随镜像同步，不丢增量能力。
  - 输出统一为 JSON（stdout），错误信息走 stderr 且退出码非 0。
  - 不做任何需要 LLM 判断的事情；git commit message 由调用方（agent）组装。
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

LINK_FILENAME = ".openwiki-link.json"
CACHE_DIRNAME = ".openwiki-sync"
OPENWIKI_DIRNAME = "openwiki"
SOURCE_REPO_FILENAME = ".source-repo.json"
GLOBAL_DIR = os.path.join(os.path.expanduser("~"), ".kpbot")
GLOBAL_REGISTRY = os.path.join(GLOBAL_DIR, "openwiki-links.json")
REGISTRY_VERSION = 1

CONTRACT_START = "<!-- KPBOT-OPENWIKI-SYNC:START -->"
CONTRACT_END = "<!-- KPBOT-OPENWIKI-SYNC:END -->"


def sync_contract_block(wiki_repo):
    return (
        "%s\n\n## OpenWiki 同步契约\n\n"
        "本仓已关联独立 wiki 仓：%s（见 `.openwiki-link.json`）。\n"
        "任何产生代码 commit 的任务，commit 完成后必须同步 wiki：\n"
        "restore -> openwiki --update -> push（由 openwiki-sync 技能编排；无该技能时按 .openwiki-link.json 手动同步）。\n\n"
        "%s\n" % (CONTRACT_START, wiki_repo, CONTRACT_END)
    )


def update_agent_contract(repo, wiki_repo):
    """在 AGENTS.md / CLAUDE.md 追加/刷新同步契约区块（openwiki 只重写自己的管理块，互不冲突）。"""
    touched = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = os.path.join(repo, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            content = f.read()
        block = sync_contract_block(wiki_repo)
        if CONTRACT_START in content:
            start = content.index(CONTRACT_START)
            end = content.index(CONTRACT_END) + len(CONTRACT_END)
            content = content[:start] + block.rstrip("\n") + content[end:]
        else:
            content = content.rstrip("\n") + "\n\n" + block
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        touched.append(name)
    return touched


def remove_agent_contract(repo):
    touched = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = os.path.join(repo, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if CONTRACT_START in content:
            start = content.index(CONTRACT_START)
            end = content.index(CONTRACT_END) + len(CONTRACT_END)
            content = content[:start].rstrip("\n") + "\n" + content[end:].lstrip("\n")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            touched.append(name)
    return touched

PLATFORM_PATTERNS = [
    # (platform, regex on host)
    ("gitcode", re.compile(r"^(git@|https?://)(www\.)?(gitcode\.com|gitcode\.csdn\.net|atomgit\.com)[:/]")),
    ("gitee", re.compile(r"^(git@|https?://)(www\.)?gitee\.com[:/]")),
    ("github", re.compile(r"^(git@|https?://)(www\.)?github\.com[:/]")),
]

API_CREATE_REPO = {
    "gitcode": "https://api.gitcode.com/api/v5/user/repos",
    "gitee": "https://gitee.com/api/v5/user/repos",
    "github": "https://api.github.com/user/repos",
}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def eprint(*args):
    print(*args, file=sys.stderr)


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(msg, extra=None):
    payload = {"ok": False, "error": msg}
    if extra:
        payload.update(extra)
    out_json(payload)
    sys.exit(1)


def run(cmd, cwd=None, check=True, capture=True, env=None):
    """执行子进程；check=True 时非零退出抛异常。"""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=capture, text=True, env=merged_env
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            "command failed: %s\nstdout: %s\nstderr: %s"
            % (" ".join(cmd), proc.stdout[-2000:], proc.stderr[-2000:])
        )
    return proc


def git(repo, *args, check=True):
    return run(["git", "-C", repo] + list(args), check=check)


def git_ok(repo, *args):
    proc = git(repo, *args, check=False)
    return proc.returncode == 0, proc


# ---------------------------------------------------------------------------
# 仓库与平台识别
# ---------------------------------------------------------------------------

def repo_root(path):
    proc = run(["git", "-C", path, "rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def get_origin(repo):
    proc = git(repo, "remote", "get-url", "origin", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def detect_platform(url):
    if not url:
        return "generic"
    for platform, pattern in PLATFORM_PATTERNS:
        if pattern.match(url):
            return platform
    return "generic"


def normalize_remote_key(url):
    """把 ssh/https 形式的 remote URL 归一化为 owner/repo@host 作为注册表键。"""
    if not url:
        return None
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", url)
        if m:
            host, path = m.group(1), m.group(2)
        else:
            return url
    host = host.replace("www.", "")
    return "%s:%s" % (host.lower(), path.lower())


def to_https(url):
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", url or "")
    if m:
        return "https://%s/%s" % (m.group(1), m.group(2))
    return url


def host_of(url):
    m = re.match(r"^(?:git@([^:]+):|https?://([^/]+))", url or "")
    if m:
        return (m.group(1) or m.group(2) or "").replace("www.", "").lower()
    return ""


def validate_wiki_url(url, allow_local=False):
    """校验 wiki 仓地址：仅允许 https / ssh 形式。

    file:// 与本机路径可能经符号链接解引用把本地文件内容带进 wiki 仓并外推，
    默认拒绝；本地测试场景需显式 --allow-local。
    """
    if re.match(r"^(https?://|git@[^:]+:|ssh://)", url or ""):
        return
    if allow_local:
        return
    die(
        "wiki 仓地址仅支持 https:// 或 git@host:path（ssh）形式，当前为：%s。"
        "file:// 与本机路径存在本地文件外泄风险，默认拒绝；本地测试请显式加 --allow-local。"
        % url,
        {"stage": "invalid_wiki_url", "wiki_repo": url},
    )


# ---------------------------------------------------------------------------
# 关联文件与全局注册表
# ---------------------------------------------------------------------------

def link_path(repo):
    return os.path.join(repo, LINK_FILENAME)


def read_link(repo):
    path = link_path(repo)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_link(repo, wiki_repo, platform):
    data = {
        "version": 1,
        "code_repo": get_origin(repo) or "local:%s" % repo,
        "wiki_repo": wiki_repo,
        "platform": platform,
        "created_at": now_iso(),
        "created_by": "kpbot-openwiki-sync",
    }
    with open(link_path(repo), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return data


def load_registry():
    if not os.path.isfile(GLOBAL_REGISTRY):
        return {"version": REGISTRY_VERSION, "links": {}}
    try:
        with open(GLOBAL_REGISTRY, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("links"), dict):
            data["links"] = {}
        return data
    except (OSError, ValueError):
        return {"version": REGISTRY_VERSION, "links": {}}


def save_registry(reg):
    os.makedirs(GLOBAL_DIR, exist_ok=True)
    with open(GLOBAL_REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def registry_key(repo):
    origin = get_origin(repo)
    return normalize_remote_key(origin) if origin else "local:%s" % os.path.abspath(repo)


def registry_entry(repo):
    return load_registry()["links"].get(registry_key(repo))


def upsert_registry(repo, wiki_repo, platform):
    reg = load_registry()
    key = registry_key(repo)
    entry = reg["links"].get(key, {})
    entry.update(
        {
            "wiki_repo": wiki_repo,
            "platform": platform,
            "code_repo": get_origin(repo),
            "linked_at": entry.get("linked_at") or now_iso(),
        }
    )
    reg["links"][key] = entry
    save_registry(reg)
    return entry


def record_sync(repo, code_commit=None, wiki_commit=None):
    reg = load_registry()
    key = registry_key(repo)
    entry = reg["links"].get(key)
    if entry is None:
        # link 文件可能是手工/他人创建的（如新 clone）：push 时补建注册表条目
        link = read_link(repo) or {}
        entry = {
            "wiki_repo": link.get("wiki_repo"),
            "platform": link.get("platform"),
            "code_repo": get_origin(repo),
            "linked_at": now_iso(),
        }
        if not entry["wiki_repo"]:
            return None
        reg["links"][key] = entry
    entry["last_sync"] = {
        "code_commit": code_commit,
        "wiki_commit": wiki_commit,
        "at": now_iso(),
    }
    save_registry(reg)
    return entry["last_sync"]


# ---------------------------------------------------------------------------
# wiki 仓工作副本（缓存 clone）
# ---------------------------------------------------------------------------

def cache_dir(repo):
    return os.path.join(repo, CACHE_DIRNAME, "wiki")


def remote_default_branch(cache):
    """读取 wiki 仓远端默认分支名；空仓返回 None。"""
    proc = git(cache, "ls-remote", "--symref", "origin", "HEAD", check=False)
    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", proc.stdout or "")
    return m.group(1) if m else None


def ensure_wiki_clone(repo, wiki_url):
    """确保 .openwiki-sync/wiki/ 是 wiki 仓的最新 clone；返回 clone 路径。

    缓存是一次性镜像：远端是唯一事实源，每次硬对齐到远端默认分支，
    未提交的本地残留直接丢弃（openwiki/ 才是内容来源，缓存不会丢数据）。
    """
    cache = cache_dir(repo)
    need_clone = True
    if os.path.isdir(os.path.join(cache, ".git")):
        proc = git(cache, "remote", "get-url", "origin", check=False)
        if proc.returncode == 0 and normalize_remote_key(proc.stdout.strip()) == normalize_remote_key(wiki_url):
            git(cache, "fetch", "origin", check=False)
            need_clone = False
            # 同一仓库但地址形式可能变化（https <-> ssh）：以 link 记录为准刷新 origin，
            # 避免旧地址（如需要交互认证的 https）残留导致后续 push 失败
            if proc.stdout.strip() != wiki_url:
                git(cache, "remote", "set-url", "origin", wiki_url, check=False)
        else:
            shutil.rmtree(cache, ignore_errors=True)
    if need_clone:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        run(["git", "clone", wiki_url, cache])
    branch = remote_default_branch(cache)
    if branch:
        git(cache, "checkout", "-q", "-B", branch, "origin/%s" % branch, check=False)
    git(cache, "reset", "--hard", check=False)
    git(cache, "clean", "-qfd", check=False)
    return cache


def copytree_contents(src, dst):
    """把 src 目录内容（含隐藏文件）复制到 dst，dst 已存在则合并覆盖。

    安全约束：拒绝符号链接。恶意 wiki 仓可用软链（如 etc -> /etc）让
    shutil.copytree 的默认解引用行为把本机文件内容复制进 wiki 并随后推送到
    远端，造成本地文件外泄，因此遇到符号链接立即终止。
    """
    for name in os.listdir(src):
        if name == ".git":
            continue
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.islink(s):
            die(
                "wiki 内容包含符号链接 %s，拒绝复制：符号链接解引用可能把本地文件"
                "内容带入 wiki 仓并外推。请人工检查该 wiki 仓是否可信。"
                % name,
                {"stage": "symlink_rejected"},
            )
        if os.path.isdir(s):
            for r, _, fs in os.walk(s):
                for f in fs:
                    if os.path.islink(os.path.join(r, f)):
                        die(
                            "wiki 内容包含符号链接 %s，拒绝复制：符号链接解引用可能把"
                            "本地文件内容带入 wiki 仓并外推。请人工检查该 wiki 仓是否可信。"
                            % os.path.join(name, os.path.relpath(os.path.join(r, f), s)),
                            {"stage": "symlink_rejected"},
                        )
    for name in os.listdir(src):
        if name == ".git":
            continue
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            if os.path.isdir(d):
                shutil.rmtree(d)
            shutil.copytree(s, d, symlinks=True)
        else:
            shutil.copy2(s, d)


# ---------------------------------------------------------------------------
# openwiki CLI / 环境探测
# ---------------------------------------------------------------------------

def probe_environment():
    info = {"node": None, "openwiki_cli": None, "provider_configured": None}
    try:
        proc = run(["node", "--version"], check=False)
        if proc.returncode == 0:
            info["node"] = proc.stdout.strip().lstrip("v")
    except (OSError, RuntimeError):
        pass
    info["openwiki_cli"] = shutil.which("openwiki") is not None
    env_file = os.path.join(os.path.expanduser("~"), ".openwiki", ".env")
    info["provider_configured"] = os.path.isfile(env_file) or bool(
        os.environ.get("OPENWIKI_PROVIDER")
    )
    return info


# ---------------------------------------------------------------------------
# 场景判定
# ---------------------------------------------------------------------------

def cmd_status(args):
    path = os.path.abspath(args.repo or os.getcwd())
    root = repo_root(path)
    if root is None:
        out_json(
            {
                "ok": True,
                "scenario": "not_a_git_repo",
                "repo_root": path,
                "hints": [
                    "当前目录不是 git 仓库；openwiki-sync 需要在代码仓内运行，"
                    "请先 git init 或切换到代码仓根目录。"
                ],
            }
        )
        return

    origin = get_origin(root)
    platform = detect_platform(origin)
    link = read_link(root)
    reg_entry = registry_entry(root)
    ow = os.path.join(root, OPENWIKI_DIRNAME)
    ow_exists = os.path.isdir(ow)
    ow_has_claims = os.path.isdir(os.path.join(ow, ".claims"))
    ow_has_manifest = os.path.isfile(os.path.join(ow, ".page-manifest.json"))
    ow_tracked = False
    if ow_exists:
        proc = git(root, "ls-files", OPENWIKI_DIRNAME, check=False)
        ow_tracked = bool(proc.stdout.strip())
    cache = cache_dir(root)
    cache_exists = os.path.isdir(os.path.join(cache, ".git"))

    if link is None and reg_entry is not None:
        scenario = "registry_recovery_available"
        wiki_repo = reg_entry.get("wiki_repo")
        hints = [
            "仓内 %s 缺失但全局注册表仍有该代码仓的关联记录（wiki 仓：%s），"
            "可执行 restore 恢复，或用 link 重新确认。" % (LINK_FILENAME, wiki_repo)
        ]
    elif link is None and not ow_exists:
        scenario = "unlinked_no_wiki"
        wiki_repo = reg_entry.get("wiki_repo") if reg_entry else None
        hints = ["初次使用场景：先初始化 OpenWiki，再创建/关联 wiki 仓。"]
    elif link is None and ow_exists:
        scenario = "unlinked_wiki_exists"
        wiki_repo = None
        if ow_tracked:
            hints = [
                "openwiki/ 已被 git 跟踪：用户选择了仓内维护模式（模式 B），"
                "不需要关联 wiki 仓；按需 openwiki --update 后随代码一起提交即可。"
            ]
        else:
            hints = [
                "本地已有 openwiki/（未跟踪）但未关联 wiki 仓：先与用户确认存储模式——"
                "模式 A 独立 wiki 仓（link + push，勿重新 init），或模式 B 仓内维护（直接提交 openwiki/ 进仓）。"
            ]
    elif link is not None and not ow_exists:
        scenario = "linked_wiki_missing_local"
        wiki_repo = link.get("wiki_repo")
        hints = [
            "已关联但本地缺失 openwiki/（新机器或新 clone）：执行 restore 从 wiki 仓还原。"
        ]
    else:
        scenario = "linked_ready"
        wiki_repo = link.get("wiki_repo")
        hints = ["关联就绪：代码 commit 后执行同步流程（restore -> openwiki --update -> push）。"]

    out_json(
        {
            "ok": True,
            "scenario": scenario,
            "repo_root": root,
            "code_origin": origin,
            "code_origin_https": to_https(origin),
            "platform": platform,
            "link": {
                "established": link is not None,
                "file": LINK_FILENAME,
                "wiki_repo": (link or {}).get("wiki_repo"),
                "registry_entry": reg_entry,
            },
            "openwiki_dir": {
                "exists": ow_exists,
                "tracked_in_git": ow_tracked,
                "has_claims": ow_has_claims,
                "has_page_manifest": ow_has_manifest,
            },
            "wiki_cache": {"exists": cache_exists, "path": cache},
            "environment": probe_environment(),
            "hints": hints,
        }
    )


# ---------------------------------------------------------------------------
# link / unlink
# ---------------------------------------------------------------------------

def cmd_link(args):
    root = repo_root(os.path.abspath(args.repo or os.getcwd()))
    if root is None:
        die("当前目录不是 git 仓库，无法建立关联。")
    wiki_url = args.wiki_repo
    if not wiki_url:
        die("需要 --wiki-repo 指定 wiki 仓地址。")
    validate_wiki_url(wiki_url, allow_local=getattr(args, "allow_local", False))

    # 探测可克隆性（失败不阻断，仅警告——可能只是网络问题）
    reach = {"reachable": None, "error": None}
    proc = run(["git", "ls-remote", "--heads", wiki_url], check=False, env={
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
    })
    reach["reachable"] = proc.returncode == 0
    if proc.returncode != 0:
        reach["error"] = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else "unknown"

    platform = args.platform or detect_platform(wiki_url)
    link = write_link(root, wiki_url, platform)
    upsert_registry(root, wiki_url, platform)
    ensure_ignored(root)
    contract_files = update_agent_contract(root, wiki_url)
    out_json(
        {
            "ok": True,
            "action": "link",
            "repo_root": root,
            "link_file": os.path.join(root, LINK_FILENAME),
            "wiki_repo": wiki_url,
            "platform": platform,
            "remote_check": reach,
            "agent_contract_files": contract_files,
            "hints": [
                "已写入 %s 并更新全局注册表；已在 %s 写入同步契约区块；请把上述变更随代码仓 commit 以便团队共享。"
                % (LINK_FILENAME, "/".join(contract_files) or "AGENTS.md/CLAUDE.md")
            ],
        }
    )


def cmd_unlink(args):
    root = repo_root(os.path.abspath(args.repo or os.getcwd()))
    if root is None:
        die("当前目录不是 git 仓库。")
    link = link_path(root)
    removed_file = os.path.isfile(link)
    if removed_file:
        os.remove(link)
    contract_files = remove_agent_contract(root)
    reg = load_registry()
    key = registry_key(root)
    removed_reg = key in reg["links"]
    reg["links"].pop(key, None)
    save_registry(reg)
    out_json(
        {
            "ok": True,
            "action": "unlink",
            "removed_link_file": removed_file,
            "removed_contract_from": contract_files,
            "removed_registry_entry": removed_reg,
            "note": "本地 openwiki/ 与 .openwiki-sync/ 缓存已保留，如需彻底清理请手动删除。",
        }
    )


# ---------------------------------------------------------------------------
# restore / push
# ---------------------------------------------------------------------------

def resolve_wiki_url(root, args_url=None, allow_local=False):
    if args_url:
        # 显式传入的 URL 与 link 同等校验；link 文件/注册表中的地址为已确认状态
        validate_wiki_url(args_url, allow_local=allow_local)
        return args_url, "cli"
    link = read_link(root)
    if link and link.get("wiki_repo"):
        return link["wiki_repo"], "link_file"
    entry = registry_entry(root)
    if entry and entry.get("wiki_repo"):
        return entry["wiki_repo"], "registry"
    return None, None


def cmd_restore(args):
    root = repo_root(os.path.abspath(args.repo or os.getcwd()))
    if root is None:
        die("当前目录不是 git 仓库。")
    wiki_url, source = resolve_wiki_url(root, args.wiki_repo, allow_local=getattr(args, "allow_local", False))
    if not wiki_url:
        die(
            "未找到 wiki 仓关联（%s 与全局注册表均无记录）。请先 link，或用 --wiki-repo 显式指定。"
            % LINK_FILENAME,
            {"scenario_hint": "unlinked"},
        )
    cache = ensure_wiki_clone(root, wiki_url)
    # 还原到 openwiki/（wiki 仓根即 wiki 内容本体）
    ow = os.path.join(root, OPENWIKI_DIRNAME)
    os.makedirs(ow, exist_ok=True)
    copytree_contents(cache, ow)
    n_files = sum(len(files) for _, _, files in os.walk(ow) if ".git" not in _)
    out_json(
        {
            "ok": True,
            "action": "restore",
            "repo_root": root,
            "wiki_repo": wiki_url,
            "resolved_from": source,
            "openwiki_dir": ow,
            "file_count": n_files,
            "hints": ["已从 wiki 仓还原 openwiki/，可以执行 openwiki --update 或直接查询 wiki 内容。"],
        }
    )


def cmd_push(args):
    root = repo_root(os.path.abspath(args.repo or os.getcwd()))
    if root is None:
        die("当前目录不是 git 仓库。")
    ow = os.path.join(root, OPENWIKI_DIRNAME)
    if not os.path.isdir(ow):
        die(
            "本地不存在 openwiki/，无可推送内容。先 restore（若 wiki 仓已有内容）或 openwiki --init。",
            {"scenario_hint": "wiki_missing_local"},
        )
    wiki_url, source = resolve_wiki_url(root, args.wiki_repo, allow_local=getattr(args, "allow_local", False))
    if not wiki_url:
        die("未找到 wiki 仓关联。请先 link。", {"scenario_hint": "unlinked"})

    cache = ensure_wiki_clone(root, wiki_url)
    copytree_contents(ow, cache)

    # 反向关联元数据，供从 wiki 仓侧识别来源代码仓
    with open(os.path.join(cache, SOURCE_REPO_FILENAME), "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": 1,
                "code_repo": get_origin(root) or "local:%s" % root,
                "platform": detect_platform(get_origin(root)),
                "maintained_by": "kpbot-openwiki-sync",
            },
            f, ensure_ascii=False, indent=2,
        )
        f.write("\n")

    status_proc = git(cache, "status", "--porcelain")
    changed = status_proc.stdout.strip()
    new_commit = False
    if changed:
        git(cache, "add", "-A")
        commit_msg = args.message or "openwiki: update wiki content"
        if args.code_commit:
            commit_msg += "\n\nCode commit: %s\nSource repo: %s" % (
                args.code_commit, get_origin(root) or root
            )
        git(cache, "commit", "-m", commit_msg)
        new_commit = True

    # 检测未推送状态：本地 HEAD 领先于远端（含远端为空但本地有 commit 的情况，
    # 例如上次 push 因认证/网络失败留下的本地提交）
    head_proc = git(cache, "rev-parse", "HEAD", check=False)
    head_exists = head_proc.returncode == 0 and head_proc.stdout.strip()
    unpushed = False
    if head_exists:
        branch = remote_default_branch(cache)
        if branch:
            origin_proc = git(cache, "rev-parse", "origin/%s" % branch, check=False)
            if origin_proc.returncode != 0 or origin_proc.stdout.strip() != head_proc.stdout.strip():
                unpushed = True
        else:
            unpushed = True  # 远端空仓，本地提交从未推送

    if not new_commit and not unpushed:
        out_json(
            {
                "ok": True,
                "action": "push",
                "committed": False,
                "pushed": False,
                "reason": "no_changes",
                "wiki_repo": wiki_url,
                "hints": ["wiki 内容与 wiki 仓一致，无变更，跳过 commit（避免空提交）。"],
            }
        )
        return

    push_proc = git(cache, "push", "-u", "origin", "HEAD", check=False)
    if push_proc.returncode != 0:
        die(
            "推送到 wiki 仓失败：%s" % (push_proc.stderr or "").strip().splitlines()[-1],
            {"action": "push", "stage": "git_push", "wiki_repo": wiki_url},
        )
    head = git(cache, "rev-parse", "HEAD").stdout.strip()
    record_sync(root, code_commit=args.code_commit, wiki_commit=head)
    n_changed = len(changed.splitlines()) if changed else 0
    out_json(
        {
            "ok": True,
            "action": "push",
            "committed": new_commit,
            "pushed": True,
            "wiki_repo": wiki_url,
            "wiki_commit": head,
            "changed_files": n_changed,
            "recovered_unpushed": new_commit is False and unpushed,
            "resolved_from": source,
            "hints": ["wiki 仓已更新；全局注册表已记录本次同步。"],
        }
    )


# ---------------------------------------------------------------------------
# create-repo
# ---------------------------------------------------------------------------

def token_for(platform, explicit):
    if explicit:
        return explicit
    env_names = {
        "gitcode": ["GITCODE_TOKEN", "ATOMGIT_TOKEN"],
        "gitee": ["GITEE_TOKEN"],
        "github": ["GITHUB_TOKEN", "GH_TOKEN"],
    }.get(platform, [])
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name]
    return None


def cmd_create_repo(args):
    platform = args.platform
    if not platform:
        die("需要 --platform 指定平台（gitcode / gitee / github）。")
    token = token_for(platform, args.token)
    if not token:
        die(
            "缺少 %s 平台 API token。请通过 --token 传入，或设置环境变量 %s。"
            % (platform, {"gitcode": "GITCODE_TOKEN", "gitee": "GITEE_TOKEN", "github": "GITHUB_TOKEN"}[platform]),
            {"stage": "missing_token", "platform": platform,
             "manual_fallback": "也可以在平台上手动创建空仓库后，把地址通过 link 子命令关联。"},
        )

    name = args.name
    api = API_CREATE_REPO[platform]
    body = {"name": name, "description": args.description or "OpenWiki for %s" % name}
    if args.public:
        body["private"] = False
    else:
        body["private"] = True

    if platform == "gitee":
        body["access_token"] = token
        body["private"] = "true" if body.get("private") else "false"
        data, headers = urllib.parse.urlencode(body).encode(), {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        headers = {"Content-Type": "application/json", "PRIVATE-TOKEN": token}
        if platform == "github":
            headers = {"Content-Type": "application/json",
                       "Authorization": "Bearer %s" % token,
                       "Accept": "application/vnd.github+json"}
        data = json.dumps(body).encode()

    req = urllib.request.Request(api, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        die(
            "创建仓库失败（HTTP %d）：%s" % (e.code, detail),
            {"stage": "api_error", "platform": platform,
             "manual_fallback": "可在平台网页手动创建空仓库（不要初始化 README），然后用 link 关联。"},
        )
    except (urllib.error.URLError, OSError) as e:
        die("网络请求失败：%s" % e, {"stage": "network", "platform": platform})

    def pick(d, *keys):
        for k in keys:
            if d.get(k):
                return d[k]
        return None

    https_url = pick(payload, "https_url", "html_url", "clone_url")
    ssh_url = pick(payload, "ssh_url")
    web_url = pick(payload, "html_url", "web_url")
    full_path = pick(payload, "full_path", "path_with_namespace", "full_name")
    if not https_url and full_path:
        host = {"gitcode": "gitcode.com", "gitee": "gitee.com", "github": "github.com"}[platform]
        https_url = "https://%s/%s.git" % (host, full_path.lstrip("/"))
    if not ssh_url and full_path:
        host = {"gitcode": "gitcode.com", "gitee": "gitee.com", "github": "github.com"}[platform]
        ssh_url = "git@%s:%s.git" % (host, full_path.lstrip("/"))

    out_json(
        {
            "ok": True,
            "action": "create-repo",
            "platform": platform,
            "name": name,
            "web_url": web_url,
            "https_url": https_url,
            "ssh_url": ssh_url,
            "hints": [
                "仓库已创建。建议优先用 ssh_url 关联（推送免 token）；若 SSH 未配置则用 https_url。"
            ],
        }
    )


# ---------------------------------------------------------------------------
# ignore
# ---------------------------------------------------------------------------

IGNORE_ENTRIES = [OPENWIKI_DIRNAME + "/", CACHE_DIRNAME + "/"]


def ensure_ignored(root):
    gi = os.path.join(root, ".gitignore")
    existing = ""
    if os.path.isfile(gi):
        with open(gi, encoding="utf-8") as f:
            existing = f.read()
    missing = [e for e in IGNORE_ENTRIES if e not in existing]
    if not missing:
        return False
    with open(gi, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        if "# openwiki-sync: wiki 本体存独立仓，本地仅工作副本" not in existing:
            f.write("# openwiki-sync: wiki 本体存独立仓，本地仅工作副本\n")
        for e in missing:
            f.write(e + "\n")
    return True


def cmd_ignore(args):
    root = repo_root(os.path.abspath(args.repo or os.getcwd()))
    if root is None:
        die("当前目录不是 git 仓库。")
    changed = ensure_ignored(root)
    out_json({"ok": True, "action": "ignore", "updated": changed, "entries": IGNORE_ENTRIES})


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="openwiki-sync deterministic sync engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="探测场景状态（JSON）")
    p.add_argument("--repo", default=None)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("link", help="建立代码仓<->wiki仓关联")
    p.add_argument("--wiki-repo", required=False, default=None)
    p.add_argument("--platform", default=None)
    p.add_argument("--repo", default=None)
    p.add_argument("--allow-local", action="store_true", help="允许本地路径/file:// 形式的 wiki 仓（仅限本地测试）")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="解除关联（保留本地内容）")
    p.add_argument("--repo", default=None)
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("restore", help="从 wiki 仓还原 openwiki/")
    p.add_argument("--wiki-repo", default=None)
    p.add_argument("--repo", default=None)
    p.add_argument("--allow-local", action="store_true", help="允许本地路径/file:// 形式的 wiki 仓（仅限本地测试）")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("push", help="推送 openwiki/ 到 wiki 仓并 commit")
    p.add_argument("--message", default=None)
    p.add_argument("--code-commit", default=None)
    p.add_argument("--wiki-repo", default=None)
    p.add_argument("--repo", default=None)
    p.add_argument("--allow-local", action="store_true", help="允许本地路径/file:// 形式的 wiki 仓（仅限本地测试）")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("create-repo", help="通过平台 API 创建 wiki 仓")
    p.add_argument("--name", required=True)
    p.add_argument("--platform", default=None, choices=["gitcode", "gitee", "github"])
    p.add_argument("--description", default=None)
    p.add_argument("--public", action="store_true", help="创建公开仓（默认私有）")
    p.add_argument("--token", default=None)
    p.set_defaults(func=cmd_create_repo)

    p = sub.add_parser("ignore", help="把 openwiki/ 等加入 .gitignore")
    p.add_argument("--repo", default=None)
    p.set_defaults(func=cmd_ignore)

    args = parser.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
