#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""openwiki-sync skill 的程序化评分器。

用法: python3 evals/grade.py <iteration-dir>
读取每个 eval 的 <arm>/state.json 与 outputs/response.md，
按断言集合生成 <arm>/grading.json（字段: text/passed/evidence）与 timing.json 占位。
"""
import json
import os
import re
import sys

WIKI_CORE = {"index.md", "quickstart.md"}


def short_hashes(commits):
    return {c["hash"][:7] for c in commits} | {c["hash"][:8] for c in commits}


def subj_refers_code(wiki_commits, code_commits):
    """wiki commit 的 subject 是否引用了某个代码 commit hash 前缀。"""
    for w in wiki_commits:
        for h in short_hashes(code_commits):
            if h in (w["subject"] + w["body"]):
                return True, f"wiki commit {w['hash'][:7]} 引用了代码 commit {h}"
    return False, "未发现 wiki commit 引用代码 commit hash"


def grade_eval1(s):
    ow = set(s["code_repo"]["openwiki_files"])
    code_subj = [c["subject"] for c in s["code_repo"]["commits"]]
    wiki_files = set(s["wiki_repo"]["files_at_head"])
    wiki_subj = [c["subject"] for c in s["wiki_repo"]["commits"]]
    unrelated = [x for x in code_subj if not re.match(r"^(feat: initial|chore\(openwiki\)|docs:|chore: (add|建立))", x)]
    ok_ref, ev_ref = subj_refers_code(s["wiki_repo"]["commits"], s["code_repo"]["commits"])
    return [
        ("openwiki/ 已生成完整 wiki（index/quickstart/pages/增量元数据）",
         WIKI_CORE <= ow and any(f.startswith("pages/") for f in ow) and ".last-update.json" in ow,
         f"openwiki/ 文件: {sorted(ow)[:6]}..."),
        (".openwiki-link.json 已创建且被 git 跟踪",
         s["code_repo"]["link_file_tracked"] and s["code_repo"]["link_file"] is not None,
         f"tracked={s['code_repo']['link_file_tracked']}"),
        (".gitignore 排除了 openwiki/ 与 .openwiki-sync/",
         s["code_repo"]["gitignore"] and "openwiki/" in s["code_repo"]["gitignore"] and ".openwiki-sync/" in s["code_repo"]["gitignore"],
         f"gitignore={s['code_repo']['gitignore']!r}"),
        ("wiki 仓已获得含 wiki 页面的首个 commit",
         len(s["wiki_repo"]["commits"]) >= 1 and WIKI_CORE <= wiki_files,
         f"wiki commits={len(s['wiki_repo']['commits'])}, HEAD files 含 index/quickstart={WIKI_CORE <= wiki_files}"),
        ("wiki commit 与代码 commit 可互相追溯（message 引用 hash）", ok_ref, ev_ref),
        ("全局注册表已记录代码仓↔wiki仓关联", bool(s["registry_entry"]),
         f"registry_entry={'存在' if s['registry_entry'] else '缺失'}"),
        ("未为了验证流程而引入与任务无关的源码改动", len(unrelated) == 0,
         f"可疑提交: {unrelated}" if unrelated else "代码仓提交均为关联/初始化相关"),
    ]


def grade_eval2(s):
    mock = s["code_repo"]["mock_calls"]
    init_count = mock.count("--init") if mock else 0
    wiki_files = set(s["wiki_repo"]["files_at_head"])
    code_subj = [c["subject"] for c in s["code_repo"]["commits"]]
    unrelated = [x for x in code_subj if not re.match(r"^(feat: initial|chore\(openwiki\)|docs:)", x)]
    return [
        ("未重新执行 openwiki --init（保护已有 wiki，init 调用仅 fixture 预置 1 次）",
         init_count == 1, f"mock init 调用次数={init_count}"),
        (".openwiki-link.json 已创建且被 git 跟踪",
         s["code_repo"]["link_file_tracked"] and s["code_repo"]["link_file"] is not None,
         f"tracked={s['code_repo']['link_file_tracked']}"),
        ("wiki 仓获得首个 commit 且含全部 wiki 页面",
         len(s["wiki_repo"]["commits"]) >= 1 and WIKI_CORE <= wiki_files,
         f"wiki commits={len(s['wiki_repo']['commits'])}"),
        ("原有 wiki 内容未被覆盖（wiki 仓恰好 1 个新 commit，非重建历史）",
         len(s["wiki_repo"]["commits"]) == 1,
         f"wiki commits={len(s['wiki_repo']['commits'])}"),
        ("全局注册表已记录关联", bool(s["registry_entry"]),
         f"registry_entry={'存在' if s['registry_entry'] else '缺失'}"),
        ("代码仓无与任务无关的多余提交", len(unrelated) == 0,
         f"可疑提交: {unrelated}" if unrelated else "代码仓提交均为关联相关"),
    ]


def grade_eval3(s):
    ow = set(s["code_repo"]["openwiki_files"])
    wiki_files = set(s["wiki_repo"]["files_at_head"])
    mock = s["code_repo"]["mock_calls"] or ""
    resp = s.get("response_md") or ""
    no_extra = mock == "" or ("--init" not in mock and "--update" not in mock)
    meta_ok = {".claims/architecture.json", ".last-update.json", ".page-manifest.json"} <= ow
    return [
        ("openwiki/ 已还原且文件集合与 wiki 仓 HEAD 一致", ow == wiki_files,
         f"openwiki={len(ow)} 文件, wiki HEAD={len(wiki_files)} 文件, 一致={ow == wiki_files}"),
        ("增量元数据（.claims/.last-update.json/.page-manifest.json）完整还原", meta_ok,
         f"元数据齐全={meta_ok}"),
        ("纯恢复：未调用 openwiki --init/--update", no_extra, f"mock 调用记录: {mock!r}"),
        ("代码仓与 wiki 仓均未新增 commit（无副作用）",
         len(s["code_repo"]["commits"]) == 2 and len(s["wiki_repo"]["commits"]) == 1,
         f"code commits={len(s['code_repo']['commits'])}, wiki commits={len(s['wiki_repo']['commits'])}"),
        ("向用户说明了恢复来源与后续可用状态", ("wiki" in resp.lower() or "恢复" in resp) and len(resp) > 50,
         f"response.md {len(resp)} 字"),
    ]


def grade_eval4(s):
    mock = s["code_repo"]["mock_calls"] or ""
    resp = s.get("response_md") or ""
    code_commits = s["code_repo"]["commits"]
    wiki_commits = s["wiki_repo"]["commits"]
    auth_commit = next((c for c in code_commits if "auth" in c["subject"] and "initial" not in c["subject"] and "chore" not in c["subject"]), None)
    has_cn = bool(auth_commit and re.search(r"[\u4e00-\u9fff]", auth_commit["subject"]))
    ok_ref, ev_ref = subj_refers_code(wiki_commits, code_commits)
    resp_has_both = bool(auth_commit and auth_commit["hash"][:7] in resp) and any(
        w["hash"][:7] in resp for w in wiki_commits)
    return [
        ("auth.py 已以中文 message 提交到代码仓", bool(auth_commit) and has_cn,
         f"commit: {auth_commit['hash'][:7] if auth_commit else '无'} {auth_commit['subject'] if auth_commit else ''}"),
        ("wiki 仓因联动产生了新 commit", len(wiki_commits) > 1,
         f"wiki commits={len(wiki_commits)}"),
        ("wiki commit message 引用了对应代码 commit hash", ok_ref, ev_ref),
        ("wiki 内容被实际更新（调用了 openwiki --update）", "--update" in mock,
         f"mock 调用: {mock.strip()!r}"),
        ("最终答复报告了代码与 wiki 两侧 commit 的对应关系", resp_has_both,
         f"response 同时含代码与 wiki commit hash={resp_has_both}"),
    ]


def grade_eval5(s):
    resp = (s.get("response_md") or "").lower()
    code_commits = s["code_repo"]["commits"]
    wiki_commits = s["wiki_repo"]["commits"]
    modules = all(m in resp for m in ["app.py", "parser.py", "store.py"])
    discrepancy = any(k in resp for k in ["routes", "store", "端到端", "单测", "偏差", "未使用", "未接线", "不可达"])
    return [
        ("回答引用了 wiki 页面内容（以 wiki 为先验）",
         any(k in resp for k in ["wiki", "architecture.md", "quickstart"]),
         f"response {len(resp)} 字，含 wiki 引用={('wiki' in resp)}"),
        ("架构描述覆盖三个核心模块（app/parser/store）", modules,
         f"三模块齐全={modules}"),
        ("结合源码验证并指出 wiki 与代码的偏差", discrepancy,
         f"提到偏差={discrepancy}"),
        ("只读任务：未修改代码仓与 wiki 仓",
         len(code_commits) == 2 and len(wiki_commits) == 1,
         f"code commits={len(code_commits)}, wiki commits={len(wiki_commits)}"),
    ]


GRADERS = {
    "eval-1-initial-setup": grade_eval1,
    "eval-2-unlinked-wiki-exists": grade_eval2,
    "eval-3-fresh-machine-restore": grade_eval3,
    "eval-4-commit-sync": grade_eval4,
    "eval-5-wiki-query": grade_eval5,
}


def main():
    iter_dir = os.path.abspath(sys.argv[1])
    for eval_name, grader in GRADERS.items():
        edir = os.path.join(iter_dir, eval_name)
        if not os.path.isdir(edir):
            continue
        for arm in ["with_skill", "without_skill"]:
            state_path = os.path.join(edir, arm, "state.json")
            if not os.path.isfile(state_path):
                continue
            s = json.load(open(state_path))
            results = grader(s)
            expectations = [
                {"text": t, "passed": bool(p), "evidence": str(e)} for t, p, e in results
            ]
            passed = sum(1 for e in expectations if e["passed"])
            grading = {
                "expectations": expectations,
                "summary": {
                    "passed": passed,
                    "failed": len(expectations) - passed,
                    "total": len(expectations),
                    "pass_rate": round(passed / len(expectations), 2) if expectations else 0,
                },
            }
            with open(os.path.join(edir, arm, "grading.json"), "w") as f:
                json.dump(grading, f, ensure_ascii=False, indent=2)
            # 本环境子代理通知不含 token/时长，写占位 timing.json
            timing_path = os.path.join(edir, arm, "timing.json")
            if not os.path.isfile(timing_path):
                with open(timing_path, "w") as f:
                    json.dump({"total_tokens": None, "duration_ms": None, "total_duration_seconds": None}, f)
            print(f"{eval_name}/{arm}: {passed}/{len(expectations)} passed")


if __name__ == "__main__":
    main()
