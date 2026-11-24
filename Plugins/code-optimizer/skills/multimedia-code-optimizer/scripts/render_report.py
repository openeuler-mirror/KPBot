#!/usr/bin/env python3
"""把 process/*/record.json 聚合成最终报告 simd_optimization_report.md。

脚本只负责机械部分（表格、统计、环境、参考方法）；判断性的段落来自
other/report-notes.md，按 `## 标题` 注入，所以报告可以反复重新生成。

生成物首行写入机器可读标记 `<!-- generated-by: render_report.py -->`，用来判断
既有报告是不是本脚本生成的：有标记就整体重写；没有标记且内容非空，则认为报告被
手改过或来自模板，拒绝覆盖并提示加 --force。手写内容放 other/report-notes.md，
不要直接改生成的报告。

用法:
    python3 render_report.py --workspace /path/to/simd_optimization_result
    python3 render_report.py --workspace ... --force   # 覆盖任何已存在的报告
"""

import argparse
import datetime
import glob
import json
import os
import sys

GENERATED_MARKER = "<!-- generated-by: render_report.py -->"

STATUS_ORDER = {"verified": 0, "validated": 1, "correct_only": 2,
                "unverified": 3, "rejected": 4}
STATUS_LABEL = {
    "verified": "已验收并写回",
    "validated": "已验证未写回",
    "correct_only": "仅正确性通过",
    "unverified": "未验证",
    "rejected": "已放弃（已回滚或未改动）",
}


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_notes(workspace):
    """读 other/report-notes.md，按 `## 标题` 切分成 {标题: 正文}。

    手写内容单独存放的理由：报告要能被反复重新生成（补一条 record、改一个数字），
    如果手写段落直接写在报告里，重跑一次就没了。把人写的和机器写的分开，
    脚本负责后者，前者永远不被覆盖。
    """
    path = os.path.join(workspace, "other", "report-notes.md")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    notes = {}
    current = None
    buf = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                notes[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        notes[current] = "\n".join(buf).strip()
    return notes


def collect_records(workspace):
    records = []
    for path in sorted(glob.glob(os.path.join(workspace, "process", "*", "record.json"))):
        rec = load_json(path)
        if not isinstance(rec, dict):
            rec = {"function": os.path.basename(os.path.dirname(path)),
                   "status": "unverified",
                   "notes": "record.json 无法解析，需人工检查",
                   "record_path": path}
        rec["_record_path"] = os.path.relpath(path, workspace)
        records.append(rec)
    records.sort(key=lambda r: (STATUS_ORDER.get(r.get("status"), 9),
                                r.get("function", "")))
    return records


def fmt_speedup(rec):
    perf = rec.get("performance") or {}
    sp = perf.get("speedup")
    if not sp:
        return "—"
    return "%.2fx" % sp


def section_env(workspace):
    env = load_json(os.path.join(workspace, "other", "env.json"), {}) or {}
    e = env.get("env", env)
    if not e:
        return ["## 环境", "", "> `other/env.json` 缺失：本次没有记录环境，结论不可复现。", ""]
    host = e.get("host", {})
    cc = e.get("cc", {})
    cc_desc = "; ".join(
        "%s %s (%s)" % (v.get("name", k), v.get("version", "?"), v.get("target", "?"))
        for k, v in cc.items() if v.get("armv9_6_probe", {}).get("ok"))
    sve = e.get("sve", {})
    lines = [
        "## 环境",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 宿主 | %s / %s |" % (host.get("os", "?"), host.get("arch", "?")),
        "| 机器能力探测 | `%s` |" % e.get("profile", "?"),
        "| 用户选的验证方式 | `%s` |" % (e.get("requested_method") or "—"),
        "| 编译器 | %s |" % (cc_desc or "未探测到可用的 aarch64 编译器"),
        "| QEMU | %s |" % ((e.get("qemu") or {}).get("system") or "—"),
        "| 编译期 VL | %s bit |" % (sve.get("compile_bits", "?")),
        "| 运行时 VL | %s 字节 |" % (sve.get("runtime_vl_bytes", "未实测")),
        "| perf | %s |" % ("可用" if (e.get("perf") or {}).get("available") else "不可用"),
        "",
    ]
    detail = e.get("method_detail") or {}
    if detail and not detail.get("feasible", True):
        lines += ["", "所选验证方式不可行，阻塞原因：%s"
                  % ", ".join(detail.get("blocking_reasons") or []), ""]
    if e.get("notes"):
        lines += ["> " + e["notes"], ""]
    return lines


def section_reference(workspace, records):
    lines = ["## 方法库参考", ""]
    digest = os.path.join(workspace, "other", "reference-digest.md")
    if os.path.isfile(digest):
        with open(digest, "r", encoding="utf-8") as fh:
            lines += [fh.read().strip(), ""]
        return lines
    used = {}
    for rec in records:
        ref = rec.get("reference") or {}
        for m in ref.get("matched_methods") or []:
            used.setdefault(m, []).append(rec.get("function", "?"))
    if used:
        lines += ["按 `record.json` 汇总的命中方法：", ""]
        for method, funcs in sorted(used.items()):
            lines.append("- **%s** — %s" % (method, ", ".join(funcs)))
        lines.append("")
    else:
        lines += ["> 没有任何函数记录引用了 summary/ 方法库中的方法。若本次确实读过方法库，"
                  "请补写 `other/reference-digest.md`；若没读，这次执行不合规。", ""]
    return lines


def section_hotspots(workspace):
    src = load_json(os.path.join(workspace, "other", "hotspots", "source.json"), {}) or {}
    lines = ["## 热点来源", ""]
    if not src:
        lines += ["> `other/hotspots/source.json` 缺失：无法说明优化顺序的依据。", ""]
        return lines
    origin = src.get("origin", "?")
    lines.append("来源：`%s`%s" % (origin,
                                  (" — " + src["path"]) if src.get("path") else ""))
    if src.get("total_overhead_covered"):
        lines.append("")
        lines.append("热点文件覆盖的采样占比：%.1f%%" % src["total_overhead_covered"])
    funcs = src.get("functions") or []
    if funcs:
        lines += ["", "| # | 函数 | 占比 | 位置 |", "| --- | --- | --- | --- |"]
        for f in funcs[:20]:
            loc = "%s:%s" % (f.get("file", "?"), f.get("line", "?")) \
                if f.get("file") else ("已解析" if f.get("resolved") else "未解析")
            lines.append("| %s | `%s` | %s | %s |" % (
                f.get("rank", "?"), f.get("symbol", "?"),
                ("%.1f%%" % f["share"]) if f.get("share") is not None else "—", loc))
    if src.get("notes"):
        lines += ["", src["notes"]]
    lines.append("")
    return lines


def section_results(records):
    lines = ["## 逐函数结果", ""]
    if not records:
        lines += ["> 没有任何 `process/<fn>/record.json`，本次没有产出可验收的结果。", ""]
        return lines
    lines += ["| 函数 | 状态 | 手法 | 正确性 | 加速比 | 说明 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for rec in records:
        corr = rec.get("correctness") or {}
        an = rec.get("analysis") or {}
        note = (rec.get("reject_reason") or rec.get("notes") or "").replace("|", "/")
        if len(note) > 60:
            note = note[:57] + "..."
        lines.append("| `%s` | %s | %s | %s | %s | %s |" % (
            rec.get("function", "?"),
            STATUS_LABEL.get(rec.get("status"), rec.get("status", "?")),
            (an.get("chosen_technique") or "—").replace("|", "/"),
            {"pass": "通过", "fail": "失败"}.get(corr.get("verdict"), "未跑"),
            fmt_speedup(rec),
            note or "—",
        ))
    lines.append("")

    verified = [r for r in records if r.get("status") in ("verified", "validated")]
    if verified:
        speeds = [r["performance"]["speedup"] for r in verified
                  if (r.get("performance") or {}).get("speedup")]
        if speeds:
            lines += ["通过两道门的函数 %d 个，加速比中位数 %.2fx（最小 %.2fx，最大 %.2fx）。" % (
                len(verified),
                sorted(speeds)[len(speeds) // 2], min(speeds), max(speeds)), ""]
    return lines


def section_failures(records):
    failed = [r for r in records if r.get("status") in ("rejected", "correct_only",
                                                        "unverified")]
    lines = ["## 未通过 / 未验证", ""]
    if not failed:
        lines += ["无。", ""]
        return lines
    for rec in failed:
        attempts = rec.get("attempts") or []
        lines.append("### `%s` — %s" % (
            rec.get("function", "?"),
            STATUS_LABEL.get(rec.get("status"), rec.get("status", "?"))))
        lines.append("")
        if rec.get("reject_reason"):
            lines.append("- 原因：%s" % rec["reject_reason"])
        for a in attempts:
            lines.append("- 尝试「%s」：%s（speedup %.2f）" % (
                a.get("technique", "?"), a.get("result", "?"),
                a.get("speedup") or 0.0))
        if rec.get("status") == "unverified":
            lines.append("- 缺什么才能验收：%s" %
                         (rec.get("notes") or "未说明"))
        lines.append("")
    return lines


def section_next(notes):
    body = notes.get("下一步建议", "").strip()
    lines = ["## 下一步建议", ""]
    if body:
        lines.append(body)
    else:
        lines.append("<!-- TODO(手写): 按收益/风险排序，最多 3 条 -->")
    lines.append("")
    return lines


def section_repro(records, workspace):
    lines = ["## 复现方式", ""]
    if not records:
        lines += ["本次没有可复现的基准。", ""]
        return lines
    for rec in records:
        perf = rec.get("performance") or {}
        if perf.get("command"):
            lines += ["**%s**" % rec.get("function", "?"), "", "```bash",
                      perf["command"], "```", ""]
    lines += ["结果目录：`%s`" % workspace, ""]
    return lines


def section_limits(records, notes):
    lines = ["## 限制与残留风险", ""]
    lines.append("- 基准只覆盖被改写的函数，不覆盖整机/整条流水线。")
    lines.append("- 交叉 + QEMU 档的计时是动态指令数，不含真实内存层次与分支预测效应。")
    lines.append("- 性能门控阈值 1.05；低于该值的差异不作为收益。")
    any_unverified = any(r.get("status") == "unverified" for r in records)
    if any_unverified:
        lines.append("- 存在未验证改动，未写回项目（见「未通过 / 未验证」）。")
    extra = notes.get("补充风险", "").strip()
    if extra:
        lines.append(extra)
    lines.append("")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--force", action="store_true",
                    help="强制覆盖已存在的报告（包括手改过的内容）")
    args = ap.parse_args(argv)

    workspace = os.path.realpath(os.path.expanduser(args.workspace))
    if not os.path.isdir(workspace):
        print(json.dumps({"success": False, "error_message": "workspace_not_found",
                          "workspace": workspace}, ensure_ascii=False, indent=2))
        return 2

    records = collect_records(workspace)
    notes = load_notes(workspace)
    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    project = ""
    state = load_json(os.path.join(workspace, "other", "state.json"), {}) or {}
    project = state.get("project", "")

    verified = sum(1 for r in records
                   if r.get("status") in ("verified", "validated"))
    lines = [
        GENERATED_MARKER,
        "",
        "# SIMD 优化报告",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 项目 | `%s` |" % (project or "未记录"),
        "| 生成时间 | %s |" % now,
        "| 函数数 | %d（已验收 %d） |" % (len(records), verified),
        "",
        "## 结论",
        "",
        notes.get("结论", "").strip() or
        ("<!-- TODO(手写): 结论。用 3~5 句话说清这次改了什么、收益多少、"
         "哪些没做成、建议下一步做什么。不要重复下面的表格。 -->"),
        "",
    ]
    lines += section_env(workspace)
    lines += section_reference(workspace, records)
    lines += section_hotspots(workspace)
    lines += section_results(records)
    lines += section_failures(records)
    lines += section_repro(records, workspace)
    lines += section_limits(records, notes)
    lines += section_next(notes)

    report_path = os.path.join(workspace, "simd_optimization_report.md")
    regenerated = os.path.exists(report_path)
    if regenerated and not args.force:
        with open(report_path, "r", encoding="utf-8") as fh:
            old = fh.read()
        # 只看机器标记：脚本自己生成的报告（含注入了 report-notes.md 内容的）都能重写，
        # 没有标记的非空报告才判为手改产物。用 TODO 文本猜手写状态会把“正常生成的报告”
        # 误判成手改，导致文档承诺的重复生成必然失败。
        if old.strip() and GENERATED_MARKER not in old:
            print(json.dumps({
                "success": False,
                "error_message": "report_exists_without_generated_marker",
                "report_path": report_path,
                "hint": "既有报告没有生成标记，视为手改内容：把手写段落移到 "
                        "other/report-notes.md，或加 --force 覆盖整份报告",
            }, ensure_ascii=False, indent=2))
            return 1

    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")

    print(json.dumps({
        "success": True,
        "report_path": report_path,
        "regenerated": regenerated,
        "functions": len(records),
        "verified": verified,
        "rejected": sum(1 for r in records if r.get("status") == "rejected"),
        "unverified": sum(1 for r in records if r.get("status") == "unverified"),
        "error_message": "",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
