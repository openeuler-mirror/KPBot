#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Generate a host/log-elapsed step report with TTFT and tokenizer columns.

Usage:
    python gen_ttft_report.py log_prefill.log -o report.html
    python gen_ttft_report.py log_prefill.log --req REQUEST_ID --tp-rank 7

The default prefill scope ends at BREAKDOWN. Missing associations are displayed
as dashes. Execution and task identities associate Python host measurements;
log ordering never manufactures an execution identity. No native phase logs,
native trace API, or thread mapping are required.

Deploy matching vLLM and vllm-ascend diagnostics. All observed TP ranks are shown
by default; --tp-rank filters TP rows. Put is the elapsed host duration around
the existing Python backend.put call, not pure device or network time.
Forward is one unsplit total: request totals come from BREAKDOWN.forward and
execution totals from EngineCore.forward, in their original seconds. Neither is
a per-TP or pure-device measurement. Unsupported legacy forward detail is ignored.
Outer wait_for_save/enqueue/join values require unique request/execution/TP/PID
ownership and remain separate from asynchronous task timings. Old waits without
execution identity are visible as unbound evidence, never guessed into steps.
Eligible unique business Tokenizer sources are applied before HTML is written;
the page explains rejected or partial sources and offers manual inspection.
Manual legacy selections are display-only and never deduct from residual.
Residual reconciliation does not infer causes from Worker intervals or render
diagnostic timelines.
"""

from __future__ import annotations

import argparse
import ast
import html
import math
import re
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

RE_KV = re.compile(r"(\w+)=(\[[^\]\n]*\]|\([^\)\n]*\)|\"[^\"\n]*\"|'[^'\n]*'|[^\s]+)")
RE_STORE = re.compile(r"\[TTFT_DEBUG\]\[AscendStoreSave[^\]]*\]\s*(.*)$")
RE_WORKER = re.compile(r"\(Worker[^)]*\)")
RE_TP = re.compile(r"_TP(\d+)")
RE_PID = re.compile(r"\bpid=(\d+)")
SCHEDULE = ("hash", "ext_lookup", "alloc", "overhead")
ENGINE = ("sched_hash", "sched_ext", "sched_alloc", "sched_overhead", "load", "forward")
SAVE = ("queue_wait", "build_key", "lookup", "prepare", "sync")


def pf(text):
    result = {}
    for match in RE_KV.finditer(text):
        value = match[2].rstrip(",")
        if value[:1] in ('"', "'"):
            with suppress(SyntaxError, ValueError):
                value = ast.literal_eval(value)
        result[match[1]] = value
    return result


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def total(records, field):
    if not records:
        return None
    values = [number(record.get(field)) for record in records]
    return None if any(value is None for value in values) else sum(values)


def ids(record):
    for key in ("req_id", "req", "request_id", "req_ids", "request_ids"):
        value = record.get(key)
        if value is None or str(value) in ("", "None", "null"):
            continue
        try:
            parsed = ast.literal_eval(str(value))
        except (SyntaxError, ValueError):
            parsed = value
        if isinstance(parsed, (tuple, list)):
            return {str(item) for item in parsed if str(item)}
        return {item.strip() for item in str(parsed).split(",") if item.strip()}
    return set()


def parse(path: Path):
    records = {
        kind: []
        for kind in (
            "bd",
            "queue",
            "tok",
            "steps",
            "saves",
            "wfs",
            "diag",
        )
    }
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            store_match = RE_STORE.search(line)
            if store_match:
                tail = store_match[1]
                fields = pf(tail)
                if "keys" in fields:
                    kind = "saves"
                elif "wait_for_save" in fields:
                    kind = "wfs"
                else:
                    continue
            else:
                for marker, kind in (
                    ("[TTFT][BREAKDOWN]", "bd"),
                    ("[TTFT][QUEUE]", "queue"),
                    ("[TTFT_DEBUG][Tokenizer]", "tok"),
                    ("[TTFT_DEBUG][EngineCore]", "steps"),
                    ("[TTFT_DIAG]", "diag"),
                ):
                    if marker in line:
                        tail = line.split(marker, 1)[1]
                        fields = pf(tail)
                        break
                else:
                    continue
            if kind == "diag" and fields.get("event") == "forward_breakdown":
                continue
            worker = RE_WORKER.search(line)
            pid = integer(fields.get("pid"))
            worker_pid = RE_PID.search(worker[0]) if worker else None
            if pid is None:
                pid_match = worker_pid or RE_PID.search(line[: len(line) - len(tail)])
                pid = int(pid_match[1]) if pid_match else None
            rank = integer(fields.get("tp_rank"))
            worker_rank = RE_TP.search(worker[0]) if worker else None
            if rank is None and worker_rank:
                rank = int(worker_rank[1])
            fields.update(
                _line=line_no,
                _source=str(path),
                _raw=line.rstrip(),
                _worker=(rank, pid) if rank is not None and pid is not None else None,
                _pid=pid,
            )
            records[kind].append(fields)
    return records


def choose_request(data, requested):
    breakdowns = data["bd"]
    if not breakdowns:
        raise ValueError("没有找到 [TTFT][BREAKDOWN]，无法确定请求总量和 step 数。")
    if len(breakdowns) != 1:
        raise ValueError(
            "本脚本用于单请求实验；日志有多条 BREAKDOWN，请先截取一次请求的日志。"
        )
    breakdown = breakdowns[0]
    names = ids(breakdown)
    if len(names) != 1:
        raise ValueError("BREAKDOWN 缺少唯一请求 ID。")
    request = next(iter(names))
    if requested and requested != request:
        raise ValueError(f"--req={requested} 与日志请求 {request} 不一致。")
    return request, breakdown


def execution(record):
    value = record.get("execution_id") or record.get("ttft_execution_id") or ""
    return "" if value in ("null", "None") else value


def select_steps(data, request, breakdown, mode, warnings):
    end = breakdown["_line"] if mode == "prefill" else math.inf
    records = [
        record
        for record in data["steps"]
        if record["_line"] < end
        and integer(record.get("num_total")) not in (None, 0)
        and integer(record.get("num_tokens")) not in (None, 0)
        and (
            mode != "prefill"
            or "prefill_request_ids" not in record
            or request in ids({"request_ids": record["prefill_request_ids"]})
        )
    ]
    expected = integer(breakdown.get("steps"))
    if expected is None or expected <= 0:
        raise ValueError("BREAKDOWN.steps 必须为正整数。")
    identified = [r for r in records if execution(r) and ids(r) == {request}]
    if identified:
        if mode == "prefill" and any(
            "prefill_request_ids" not in r for r in identified
        ):
            warnings.append(
                "执行日志缺少 prefill_request_ids；不能独立证明每轮均在首 token 前。"
            )
        grouped = defaultdict(list)
        for record in identified:
            grouped[execution(record)].append(record)
        steps = [group[0] for group in grouped.values() if len(group) == 1]
        if len(steps) != len(grouped):
            warnings.append("重复 execution_id 的 EngineCore 记录有歧义，未认领。")
        if mode == "prefill" and len(steps) != expected:
            warnings.append(
                f"BREAKDOWN.steps={expected}；实际唯一请求 execution={len(steps)}。"
                "保留真实 execution，不截取或补配其他空批次/decode。"
            )
    else:
        legacy = [r for r in records if not ids(r) or ids(r) == {request}]
        if (
            (len(legacy) == expected or (mode == "all" and len(legacy) > expected))
            and len({r["_pid"] for r in legacy}) == 1
            and not any(execution(r) for r in legacy)
        ):
            steps = legacy
            warnings.append(
                "旧 EngineCore 日志没有 execution_id；只展示原始行，不关联保存任务。"
            )
        else:
            steps = [{} for _ in range(expected)]
            warnings.append(
                "EngineCore 缺少唯一请求执行身份；保留缺失行，不按顺序猜测。"
            )
    if mode == "all":
        warnings.append("mode=all 包含首 token 后记录；总计行仍为 TTFT 请求总量。")
    return steps, end


def task_key(record):
    task = record.get("task_id")
    if task and execution(record) and record["_pid"] is not None:
        return record["_pid"], execution(record), task
    return None


def interval(record):
    if any(
        re.fullmatch(r"[0-9]+", str(record.get(field, ""))) is None
        for field in ("start_ns", "end_ns", "duration_ns")
    ):
        return None
    start, end = integer(record.get("start_ns")), integer(record.get("end_ns"))
    duration = integer(record.get("duration_ns"))
    if (
        start is None or end is None or duration is None
        or start < 0 or duration < 0 or end < start
    ):
        return None
    return (start, end) if duration == end - start else None


def save_rows(data, request, end, steps, requested_rank, warnings):
    executions = {execution(step) for step in steps if execution(step)}
    saves = [
        s
        for s in data["saves"]
        if ids(s) == {request} and (execution(s) in executions or s["_line"] < end)
    ]
    waits = [s for s in data["wfs"] if execution(s) in executions]
    workers = sorted(
        {
            s["_worker"]
            for s in saves
            + [w for w in waits if ids(w) == {request}]
            + [
                w for w in data["wfs"]
                if not execution(w) and ids(w) == {request} and w["_line"] < end
            ]
            + [
                r
                for r in data["diag"]
                if execution(r) in executions
                and (not ids(r) or ids(r) == {request})
            ]
            if s["_worker"] is not None
        }
    )
    if not workers:
        warnings.append("没有已关联 Worker 记录，保存明细缺失，不代表零耗时。")
    if any(sum(w[0] == rank for w in workers) > 1 for rank in {w[0] for w in workers}):
        raise ValueError("同一个 TP 有多个 Worker PID，请先截取同一次运行的日志。")
    displayed = [w for w in workers if requested_rank is None or w[0] == requested_rank]
    if requested_rank is not None and not displayed:
        raise ValueError(f"没有找到 TP{requested_rank} 的 Worker 日志。")
    if any(not execution(s) or not task_key(s) for s in saves):
        warnings.append(
            "旧保存记录缺少 execution/task 身份；仅统计真实数据量，"
            "不按 born_step 或 join 序号关联。"
        )
    rows = []
    for step in steps:
        exec_id = execution(step)
        all_tasks = [
            s for s in saves if exec_id and execution(s) == exec_id and task_key(s)
        ]
        ranks = []
        for worker in displayed or [None]:
            tasks = [
                s for s in all_tasks if worker is not None and s["_worker"] == worker
            ]
            candidates = [
                w for w in waits
                if execution(w) == exec_id and w["_worker"] == worker
                and (not ids(w) or request in ids(w))
            ]
            boundaries = (
                candidates
                if worker is not None and len(candidates) == 1
                and ids(candidates[0]) == {request}
                else []
            )
            reason = (
                "唯一请求/execution/TP/PID 边界"
                if boundaries
                else "边界重复/冲突，未认领"
                if len(candidates) > 1
                else "边界缺少独占请求身份，未认领"
                if candidates
                else "缺少唯一请求/execution/TP/PID 边界"
            )
            boundary_values = {
                field: number(boundaries[0].get(field)) if boundaries else None
                for field in ("wait_for_save", "enqueue", "join")
            }
            for field, value in boundary_values.items():
                if value is None or value < 0:
                    boundary_values[field] = None
                    if boundaries:
                        reason += f"；{field} 缺失/无效"
            if candidates and not boundaries:
                warnings.append(f"{exec_id} / {worker}: {reason}。")
            empty = (
                not tasks and bool(boundaries)
                and boundary_values["join"] == 0
                and boundary_values["enqueue"] is not None
            )
            keys = 0 if empty else total(tasks, "keys")
            byte_count = 0 if empty else total(tasks, "bytes")
            ranks.append(
                {
                    "worker": worker,
                    "values": [total(tasks, field) for field in SAVE],
                    "put": total(tasks, "put"),
                    "volume": [
                        keys,
                        None if byte_count is None else byte_count / 1048576,
                    ],
                    "empty": empty,
                    **boundary_values,
                    "boundary_sources": boundaries,
                    "boundary_reason": reason,
                }
            )
        rows.append({"ranks": ranks})
    return rows, displayed, saves


def _tokenizer_matches_request(record, request):
    names = ids(record)
    if len(names) != 1:
        return False
    external = next(iter(names))
    # InputProcessor.assign_request_id appends eight random hex characters.
    return (
        external == request
        or re.fullmatch(re.escape(external) + r"-[0-9a-f]{8}", request) is not None
    )


def tokenizer_reasons(record, request, end, *, span=False, manual=False):
    reasons = []
    if record["_line"] >= end:
        reasons.append("位于 BREAKDOWN 之后")
    purpose = record.get("purpose")
    if purpose != "business" and not (manual and not purpose):
        reasons.append(
            "预热，不计入请求" if purpose == "warmup"
            else "无业务归属，禁止认领" if purpose == "unscoped"
            else "purpose 缺失/非 business"
        )
    names = ids(record)
    if not _tokenizer_matches_request(record, request):
        if not (manual and not names):
            reasons.append(
                "缺少请求身份" if not names
                else "共享/多请求身份" if len(names) != 1
                else "其他请求"
            )
    n = str(record.get("n", ""))
    if re.fullmatch(r"[0-9]+", n) is None or int(n) <= 0:
        reasons.append("n 必须为正整数")
    if span:
        if interval(record) is None:
            reasons.append("start_ns/end_ns/duration_ns 区间缺失、负值或不一致")
    else:
        fields = [name for name in ("render", "tokenize") if name in record]
        if not fields:
            reasons.append("render/tokenize 均缺失")
        for name in fields:
            value = number(record[name])
            if value is None or value < 0:
                reasons.append(f"{name} 必须为非负有限秒数")
        if any(name in record for name in ("start_ns", "end_ns", "duration_ns")):
            if interval(record) is None:
                reasons.append("summary 区间无效")
    return reasons


def tokenizer_for_request(data, request, end, warnings):
    records = [
        r for r in data["tok"]
        if not tokenizer_reasons(r, request, end)
    ]
    if len(records) > 1:
        warnings.append(
            "Tokenizer 有多条合格 business summary，来源歧义，未自动应用："
            + ", ".join(str(r["_line"]) for r in records)
        )
        return {}
    result = dict(records[0]) if records else {}
    sources = [records[0]["_line"]] if records else []
    partial = []
    for name in ("render", "tokenize"):
        candidates = [
            r for r in data["diag"]
            if r.get("event") == name
            and _tokenizer_matches_request(r, request)
            and r["_line"] < end
        ]
        spans = [
            r for r in candidates
            if not tokenizer_reasons(r, request, end, span=True)
        ]
        if len(spans) == 1:
            result[name] = integer(spans[0]["duration_ns"]) / 1e9
            sources.append(spans[0]["_line"])
        elif len(spans) > 1:
            result.pop(name, None)
            partial.append(f"{name} business span 重复，未应用")
        for candidate in candidates:
            reasons = tokenizer_reasons(candidate, request, end, span=True)
            if reasons:
                partial.append(
                    f"行 {candidate['_line']} {name} 被拒绝：" + "；".join(reasons)
                )
        if number(result.get(name)) is None:
            partial.append(f"{name} 缺少唯一合格来源")
    if not any(number(result.get(name)) is not None for name in ("render", "tokenize")):
        rejected = [
            f"行 {r['_line']}：" + "；".join(tokenizer_reasons(r, request, end))
            for r in data["tok"]
            if _tokenizer_matches_request(r, request) and r["_line"] < end
        ]
        warnings.append(
            "Tokenizer 未自动应用："
            + ("；".join(rejected + partial) or "缺少唯一 business 请求身份")
        )
        return {}
    result["_sources"] = sorted(set(sources))
    result["_line"] = result["_sources"][0]
    result["_partial"] = partial
    warnings.extend("Tokenizer 部分应用：" + reason for reason in partial)
    return result


def escape(value):
    return html.escape(str(value), quote=True)


def fmt(value, digits=6):
    return "—" if value is None else f"{value:.{digits}f}"


CSS = """
:root{color-scheme:light;--fg:#182635;--muted:#526374;--border:#becad3;
--surface:#f5f8fa;--ec:#edf4ff;--wk:#eef7f1;--store:#f4f2f9;--put:#fff4df}
*{box-sizing:border-box}body{margin:0;padding:22px;color:var(--fg);
font:15px/1.65 'Segoe UI','Microsoft YaHei',sans-serif}h1{font-size:24px;margin:0 0 6px}
.meta,.note{color:var(--muted);font-size:14px}.request{padding:12px 16px;
border:1px solid var(--border);background:#f0f7fc;margin:12px 0}
.request b{margin-right:18px}
.scroll{overflow-x:auto;max-width:100%;border:1px solid var(--border)}
table{border-collapse:separate;border-spacing:0;width:100%;min-width:1800px;
font:13px/1.6 ui-monospace,Consolas,monospace;white-space:nowrap}
th,td{border-right:1px solid var(--border);border-bottom:1px solid var(--border);
padding:8px;text-align:right}
thead th{text-align:center;font-family:'Segoe UI',sans-serif}
th:first-child{position:sticky;left:0;text-align:left;background:var(--surface);z-index:1}
.ec{background:var(--ec)}.wk{background:var(--wk)}.store{background:var(--store)}
.put{background:var(--put)}.total td{text-align:center;font-weight:600}
.missing{color:#697b89}
.step-start>*{border-top:2px solid var(--border)}
.rank{text-align:left;background:var(--store)}
details{margin:12px 0;border:1px solid var(--border);padding:9px 14px}
summary{cursor:pointer}li{margin:5px 0}.warning{border-left:4px solid #b77719;
background:#fff8e8;padding:10px 16px;margin:12px 0;font-size:14px}
code{overflow-wrap:anywhere}a{color:#215b8e}
"""


def evidence(record):
    return (
        f"<details><summary>来源 {escape(record['_source'])}:"
        f"{record['_line']}</summary>"
        f"<code>{escape(record['_raw'])}</code></details>"
    )


def unbound_wait_evidence(data, request, end, requested_rank):
    rows = []
    for record in data["wfs"]:
        if execution(record) or request not in ids(record) or record["_line"] >= end:
            continue
        worker = record["_worker"]
        if requested_rank is not None and (
            worker is None or worker[0] != requested_rank
        ):
            continue
        label = "TP/PID 缺失" if worker is None else f"TP{worker[0]} / PID {worker[1]}"
        scope = (
            "独占当前请求；execution_id 缺失，未绑定 step"
            if ids(record) == {request}
            else "共享请求边界；不可作为当前请求独占耗时，未绑定 step"
        )
        cells = [f"<td>{escape(label)}</td>", f"<td>{escape(scope)}</td>"]
        for field in ("wait_for_save", "enqueue", "join"):
            value = number(record.get(field))
            cells.append(
                f'<td data-field="{field}">'
                + fmt(value if value is not None and value >= 0 else None) + "</td>"
            )
        cells.append("<td>" + evidence(record) + "</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        return ""
    return (
        '<h2>未绑定 step 的旧外层等待证据</h2>'
        '<p class="note">以下实测保留原请求范围；没有 execution_id，'
        '不按时间、行序或 join 顺序补配到执行轮次，不跨 TP 相加。</p>'
        '<div class="scroll"><table id="unbound-waits"><thead><tr>'
        '<th>Worker</th><th>归属范围</th><th>wait_for_save (s)</th>'
        '<th>enqueue (s)</th><th>join (s)</th><th>原始来源</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )


def render(
    data,
    out: Path,
    req_filter=None,
    mode="prefill",
    tp_rank=None,
    tokenizer_line=None,
):
    warnings = []
    request, bd = choose_request(data, req_filter)
    steps, end = select_steps(data, request, bd, mode, warnings)
    saves, workers, raw_saves = save_rows(data, request, end, steps, tp_rank, warnings)
    unbound_waits = unbound_wait_evidence(data, request, end, tp_rank)
    tok = tokenizer_for_request(data, request, bd["_line"], warnings)
    if tokenizer_line is not None:
        candidates = [t for t in data["tok"] if t["_line"] == tokenizer_line]
        reasons = (
            tokenizer_reasons(candidates[0], request, bd["_line"], manual=True)
            if len(candidates) == 1 else ["来源行不存在或不唯一"]
        )
        if reasons:
            raise ValueError("--tokenizer-line 不可用：" + "；".join(reasons))
        if not tok:
            tok = dict(candidates[0], _manual=True)
            warnings.append(
                "手工 Tokenizer 行仅供查看，不作为唯一自动业务来源，不用于 residual 扣减。"
            )
        elif tokenizer_line not in tok.get("_sources", [tok["_line"]]):
            raise ValueError("--tokenizer-line 与已自动关联的业务分词记录不一致。")
    queues = [q for q in data["queue"] if ids(q) == {request}]
    queue = queues[0] if len(queues) == 1 else {}
    queue_valid = (
        queue.get("initial_queue_valid") == "true"
        and queue.get("reason") == "available"
        and integer(queue.get("preemptions")) == 0
        and integer(queue.get("queued_events")) == 1
        and integer(queue.get("scheduled_events")) == 1
    )
    queue_time = number(queue.get("queue_wait_s")) if queue_valid else None
    if queue_time is not None and queue_time < 0:
        queue_time = None
    if queue_time is None:
        warnings.append(
            "首次 QUEUE 无有效完整证据或发生 preemption/requeue；"
            "不声称总排队，不从 residual 扣减。"
        )
    schedule_values = [number(bd.get(k)) for k in SCHEDULE]
    schedule = None if any(v is None for v in schedule_values) else sum(schedule_values)
    known_keys = SCHEDULE + ("load_kv", "forward", "save_kv", "update")
    if "kv_transfer" in bd:
        known_keys += ("kv_transfer",)
    known_values = [number(bd.get(k)) for k in known_keys]
    ttft = number(bd.get("ttft"))
    residual = number(bd.get("residual"))
    delta = None
    if (
        ttft is not None
        and residual is not None
        and all(v is not None for v in known_values)
    ):
        delta = ttft - sum(known_values) - residual
    body = []
    for index, (step, save) in enumerate(zip(steps, saves)):
        span = len(save["ranks"])
        shared = [
            (
                f'<th scope="rowgroup" rowspan="{span}" '
                f'title="{escape(execution(step) or "执行身份缺失")}">step{index}</th>'
            ),
            f'<td rowspan="{span}" title="请求级 TTFT 仅在总计行展示"></td>',
            f'<td rowspan="{span}" title="请求级 tokenizer 仅在总计行展示"></td>',
        ]
        for key in ENGINE:
            shared.append(
                f'<td rowspan="{span}" '
                f'title="EngineCore 日志行 {step.get("_line", "—")}">'
                f"{fmt(number(step.get(key)))}</td>"
            )
        for rank_index, rank in enumerate(save["ranks"]):
            worker = rank["worker"]
            label = "缺失" if worker is None else f"TP{worker[0]}"
            source = "Worker 身份缺失" if worker is None else f"Worker PID {worker[1]}"
            cells = list(shared) if rank_index == 0 else []
            cells.append(f'<td class="rank" title="{escape(source)}">{label}</td>')
            for field in ("wait_for_save", "enqueue", "join"):
                cells.append(
                    f'<td class="store" data-field="{field}" '
                    f'title="{escape(rank["boundary_reason"])}">{fmt(rank[field])}</td>'
                )
            sources = "".join(evidence(r) for r in rank["boundary_sources"])
            cells.append(
                '<td class="store" data-field="boundary-source">'
                + (sources or escape(rank["boundary_reason"])) + "</td>"
            )
            for value in rank["values"]:
                if rank["empty"]:
                    cells.append('<td class="missing">未执行</td>')
                else:
                    classes = "missing" if value is None else ""
                    cells.append(f'<td class="{classes}">{fmt(value)}</td>')
            put_label = (
                "未执行保存（无任务、join=0）" if rank["empty"] else fmt(rank["put"])
            )
            cells.append(f'<td class="put" data-field="put">{put_label}</td>')
            for field, value, digits in zip(("keys", "mib"), rank["volume"], (0, 1)):
                cells.append(f'<td data-field="{field}">{fmt(value, digits)}</td>')
            row_class = ' class="step-start"' if rank_index == 0 else ""
            body.append(f"<tr{row_class}>" + "".join(cells) + "</tr>")
    worker_text = ", ".join(f"TP{w[0]} / PID {w[1]}" for w in workers) or "未知 Worker"
    key_total = total(raw_saves, "keys")
    byte_total = total(raw_saves, "bytes")
    data_summary = (
        f"真实保存记录 {len(raw_saves)} 条；全部 TP 的 key 提交数 {fmt(key_total, 0)}；"
        f"数据量 {fmt(None if byte_total is None else byte_total / 1048576, 1)} MiB"
    )
    queue_source = "—" if not queue else str(queue["_line"])
    tokenizer_source = (
        "—" if not tok else ", ".join(map(str, tok.get("_sources", [tok["_line"]])))
    )
    residual_parts = [
        number(tok.get("render")),
        number(tok.get("tokenize")),
        queue_time,
    ]
    remaining = None
    if (
        residual is not None
        and not tok.get("_manual")
        and all(v is not None for v in residual_parts)
    ):
        remaining = residual - sum(residual_parts)
    residual_missing = [
        name for name, value in zip(("render", "tokenize", "首次排队"), residual_parts)
        if value is None
    ]
    tokenizer_status = (
        "人工选择，仅供查看；不据此扣减 residual"
        if tok.get("_manual")
        else "已自动应用当前请求来源"
        if tok
        else "未自动应用；请查看候选拒绝/歧义原因"
    )
    if tok and not tok.get("_manual") and residual_missing:
        tokenizer_status += "；residual 未扣减，缺少：" + "、".join(residual_missing)
    if tok and not tok.get("_manual") and residual is None:
        tokenizer_status += "；原始 residual 缺失"
    warning_html = ""
    options = [
        '<option value="" data-status="未自动应用；请查看候选拒绝/歧义原因">'
        '不应用 Tokenizer 来源</option>'
    ]
    if tok:
        options.append(
            '<option value="auto" selected '
            f'data-render="{fmt(number(tok.get("render")))}" '
            f'data-tokenize="{fmt(number(tok.get("tokenize")))}" '
            f'data-source="{escape(tokenizer_source)}" '
            f'data-remaining="{fmt(remaining)}" '
            f'data-manual="{str(bool(tok.get("_manual"))).lower()}" '
            f'data-status="{escape(tokenizer_status)}">'
            + ("人工命令行选择" if tok.get("_manual") else "自动关联当前请求")
            + "</option>"
        )
    for candidate in data["tok"]:
        reason = "；".join(
            tokenizer_reasons(candidate, request, bd["_line"], manual=True)
        )
        label = (
            f"行 {candidate['_line']} · render={fmt(number(candidate.get('render')))} s"
            f" · tokenize={fmt(number(candidate.get('tokenize')))} s"
            f" · {reason or '候选，人工确认'}"
        )
        options.append(
            f'<option value="{candidate["_line"]}" '
            f'data-render="{fmt(number(candidate.get("render")))}" '
            f'data-tokenize="{fmt(number(candidate.get("tokenize")))}" '
            f'data-source="{candidate["_line"]}" data-remaining="—" '
            'data-manual="true" '
            'data-status="人工选择，仅供查看；不据此扣减 residual"'
            + (" disabled" if reason else "")
            + f">{escape(label)}</option>"
        )
    tokenizer_control = (
        '<label for="tokenizer-select">Tokenizer 来源：</label>'
        '<select id="tokenizer-select" disabled>' + "".join(options) + "</select>"
        '<button id="tokenizer-apply" type="button" disabled>应用选择</button>'
        '<br><output id="tokenizer-selection" aria-live="polite">'
        f"当前显示：render={fmt(number(tok.get('render')))} s；"
        f"tokenize={fmt(number(tok.get('tokenize')))} s；来源行 {tokenizer_source}"
        "</output><br>"
        '<span id="tokenizer-status">'
        + escape(tokenizer_status)
        + "</span>"
    )
    tokenizer_diagnostics = []
    for candidate in data["tok"] + [
        r for r in data["diag"] if r.get("event") in ("render", "tokenize")
    ]:
        is_span = candidate.get("event") in ("render", "tokenize")
        reasons = tokenizer_reasons(candidate, request, bd["_line"], span=is_span)
        tokenizer_diagnostics.append(
            f"<li>行 {candidate['_line']} · "
            f"{escape(candidate.get('event', 'summary'))}："
            + escape("；".join(reasons) or "通过自动资格校验；实际应用来源见当前状态")
            + evidence(candidate) + "</li>"
        )
    tokenizer_diagnostics_html = (
        "<details><summary>Tokenizer 候选资格与拒绝原因</summary><ul>"
        + "".join(tokenizer_diagnostics) + "</ul></details>"
    )
    save_evidence = "".join(evidence(r) for r in raw_saves + [
        w for w in data["wfs"]
        if execution(w) in {execution(step) for step in steps if execution(step)}
        and (not ids(w) or request in ids(w))
        and (tp_rank is None or (w["_worker"] and w["_worker"][0] == tp_rank))
    ])
    queue_evidence = evidence(queue) if queue else "<p>QUEUE 证据缺失或多条歧义。</p>"
    tokenizer_evidence = "".join(
        evidence(r)
        for r in data["tok"] + data["diag"]
        if r["_line"] in tok.get("_sources", [tok.get("_line")])
    )
    if warnings:
        warning_html = (
            '<div class="warning"><b>日志完整性说明</b><ul>'
            + "".join(
                f"<li>{escape(message)}</li>" for message in dict.fromkeys(warnings)
            )
            + "</ul></div>"
        )
    reconciliation = "对账所需字段不完整，未计算误差。"
    if delta is not None:
        reconciliation = (
            f"原始 BREAKDOWN 对账误差 {delta:+.6f} s（保留日志舍入误差，不自动改数）。"
        )
    transfer_summary = ""
    if "kv_transfer" in bd:
        transfer_summary = f"、kv_transfer={fmt(number(bd['kv_transfer']))} s"
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TTFT 逐 step × TP 统计 · {escape(request)}</title>
<style>{CSS}</style></head><body>
<h1>TTFT 逐 step × TP 统计</h1>
<div class="meta">请求 <code>{escape(request)}</code> · mode={escape(mode)} ·
{len(steps)} steps ·
Worker 明细：{escape(worker_text)} · 主机 elapsed 时间 s，数据量 MiB</div>
<div class="request">render
<b><span id="render-value">{fmt(number(tok.get("render")))}</span> s</b>
首次 Scheduler 排队 <b>{fmt(queue_time)} s</b>
update <b>{fmt(number(bd.get("update")))} s</b>
原始 residual <b>{fmt(residual)} s</b>
尚未归因差额 <b id="remaining-value">{fmt(remaining)} s</b><br>{data_summary}</div>
<div class="request">{tokenizer_control}</div>
{tokenizer_diagnostics_html}
<p id="tokenizer-script-warning" class="warning">
当前来源状态和数值已在生成时确定；合格的自动来源无需点击应用。
禁用 JavaScript 不影响这些静态结果。
交互选择暂不可用；如需人工查看其他候选，请在浏览器允许脚本，
或使用 --tokenizer-line 重新生成。人工来源不扣减 residual。</p>
<p class="note">请求分解：TTFT = render + tokenize + 首次排队 + schedule
+ load_kv + forward + save_kv + update
{" + kv_transfer" if "kv_transfer" in bd else ""} + 剩余 residual。
仅对唯一关联的预处理与有效首次排队作扣减；缺失时差额显示 —。
尚未归因差额不等于调度间隙、Python 或设备时间，不再进一步分摊。
原始 BREAKDOWN 及其对账保留在下方来源中。</p>
{warning_html}
<div class="scroll" tabindex="0" aria-label="逐 step × TP 统计表，可横向滚动">
<table id="main-table"><thead>
<tr><th>行</th><th>TTFT (s)</th><th>tokenizer (s)</th>
<th colspan="4" class="ec">schedule (s)</th>
<th class="ec">load_kv (s)</th><th class="ec">forward (s)</th><th>TP</th>
<th colspan="4" class="store">外层保存边界（当前TP）</th>
<th colspan="8" class="store">异步保存任务（当前TP）</th></tr>
<tr class="total"><th>总计</th><td>{fmt(ttft)}</td>
<td id="tokenizer-value">{fmt(number(tok.get("tokenize")))}</td>
<td colspan="4">{fmt(schedule)}</td>
<td>{fmt(number(bd.get("load_kv")))}</td>
<td>{fmt(number(bd.get("forward")))} · BREAKDOWN.forward</td><td></td>
<td colspan="12" class="store">
{fmt(number(bd.get("save_kv")))} · BREAKDOWN.save_kv</td></tr>
<tr><th>执行轮次</th><th></th><th></th>
<th class="ec">hash</th>
<th class="ec">ext_lookup</th><th class="ec">alloc</th>
<th class="ec">other</th><th class="ec">load（执行总量）</th>
<th class="ec">forward（执行总量）</th><th>TP</th>
<th class="store">wait_for_save (s)</th>
<th class="store">enqueue (s)</th>
<th class="store">join (s)</th>
<th class="store">边界来源</th>
<th class="store">q_wait</th>
<th class="store">build_key</th>
<th class="store">lookup</th>
<th class="store">prepare</th>
<th class="store">sync</th>
<th class="put">put (s)</th>
<th class="store">keys (当前TP)</th>
<th class="store">MiB (当前TP)</th></tr>
</thead><tbody>{"".join(body)}</tbody></table></div>
{unbound_waits}
<p class="note">总计取请求级 BREAKDOWN；逐轮左侧取 EngineCore 记录；
put 仅显示 Python backend.put 主机总耗时，不展开存储内部阶段。
保存任务按 TP 独立分行，缺少某轮证据时保留缺失值；
不跨 TP 相加或逐列取最大值。keys/MiB 在各 TP 行独立展示，只累计该 TP 的保存提交；
顶部摘要仍汇总全部 TP。--tp-rank 筛选整行，不改变顶部总量。
未出现的 TP 无法从日志推断。</p>
<p class="note">forward 只展示原始总耗时：请求级取 BREAKDOWN.forward，
执行级取 EngineCore.forward（秒），不拆分子阶段，也不推断或分配到各 TP。
load 同样保留 EngineCore 执行级范围。执行值跨 TP 行合并展示，不重复计数。
旧 forward 分层诊断不参与数值、Worker 身份或对账。
这些值是主机/日志 elapsed，不是纯设备时间；缺失显示 —，实测 0 保留为 0。</p>
<p class="note">
外层 wait_for_save / enqueue / join 是该执行和 TP 的实测边界，唯一关联后才显示；
与异步任务 q_wait/build/lookup/prepare/sync/put 分开，不将各列相加冒充接口时间。
“—”表示缺失、无效或关联歧义，不表示 0。</p>
<details><summary>数据来源与统计规则</summary><ul>
<li>总计行、TTFT、原始 residual、update={fmt(number(bd.get("update")))} s
{transfer_summary}
取自 BREAKDOWN 第 {bd["_line"]} 行，不用 step 行拼造请求总量。</li>
<li>{reconciliation}</li>
<li>execution 只取源日志的 execution_id 与 request_ids。默认 prefill 在首 token 的
BREAKDOWN 前选取真实请求执行，不以空批次/decode 顺序补配。</li>
<li>保存任务通过独占请求及 execution_id/task_id/TP/PID 关联；
外层边界必须独占请求且 execution_id/TP/PID 唯一一致。
born_step、join 序号、邻近日志或时间戳不当作执行身份。</li>
<li>put 取 AscendStoreSave 的 put 秒数，即原有 backend.put 调用的主机 elapsed 时间；
不要求原生阶段日志、trace API 或线程映射，不推断内部传输阶段。</li>
<li>Tokenizer 来源行 <span id="tokenizer-source">{tokenizer_source}</span>，
QUEUE 来源行 {queue_source}。
原始 residual={fmt(residual)} s；扣除已关联 render、tokenize、首次排队后为
<span id="remaining-evidence">{fmt(remaining)}</span> s。
人工候选只供查看，不作为已证明的 residual 扣减依据。</li>
<li>QUEUE 仅有 initial_queue_valid=true、零 preemption、一次 QUEUED/SCHEDULED 时
用于首次排队扣减；这不是请求累计排队量，旧日志无有效性证据则不扣减。</li>
</ul>{evidence(bd)}{queue_evidence}{tokenizer_evidence}{save_evidence}</details>
<script>
const tokenizerSelect = document.getElementById("tokenizer-select");
function updateTokenizer() {{
    const option = tokenizerSelect.selectedOptions[0];
    if (!option || option.disabled) return;
    document.getElementById("render-value").textContent = option.dataset.render || "—";
    document.getElementById("tokenizer-value").textContent =
        option.dataset.tokenize || "—";
    document.getElementById("tokenizer-source").textContent =
        option.dataset.source || "—";
    document.getElementById("remaining-value").textContent =
        option.dataset.remaining || "—";
    document.getElementById("remaining-evidence").textContent =
        option.dataset.remaining || "—";
    document.getElementById("tokenizer-selection").textContent =
        "当前显示：render=" + (option.dataset.render || "—") +
        " s；tokenize=" + (option.dataset.tokenize || "—") +
        " s；来源行 " + (option.dataset.source || "—");
    document.getElementById("tokenizer-status").textContent =
        option.dataset.status || "未选择；未应用 Tokenizer 来源";
}}
tokenizerSelect.addEventListener("input", updateTokenizer);
tokenizerSelect.addEventListener("change", updateTokenizer);
document.getElementById("tokenizer-apply").addEventListener("click", updateTokenizer);
updateTokenizer();
tokenizerSelect.disabled = false;
document.getElementById("tokenizer-apply").disabled = false;
document.getElementById("tokenizer-script-warning").hidden = true;
</script></body></html>"""
    out.write_text(document, encoding="utf-8")
    print(f"OK -> {out} | req={request} steps={len(steps)} worker={worker_text}")
    print(
        f"TTFT={fmt(ttft)}s save_kv={fmt(number(bd.get('save_kv')))}s | {data_summary}"
    )
    for message in dict.fromkeys(warnings):
        print(f"WARNING: {message}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="一次请求的原始日志")
    parser.add_argument("-o", "--out", type=Path, default=Path("ttft_report.html"))
    parser.add_argument("--req", help="核对目标请求 ID；单请求日志可省略")
    parser.add_argument("--mode", choices=("prefill", "all"), default="prefill")
    parser.add_argument(
        "--tp-rank", type=int, help="仅显示指定 TP；默认平铺全部已识别 TP"
    )
    parser.add_argument(
        "--tokenizer-line",
        type=int,
        help="兼容旧日志的手工证据行；无业务身份不扣 residual",
    )
    args = parser.parse_args()
    try:
        render(
            parse(args.log),
            args.out,
            args.req,
            args.mode,
            args.tp_rank,
            args.tokenizer_line,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
