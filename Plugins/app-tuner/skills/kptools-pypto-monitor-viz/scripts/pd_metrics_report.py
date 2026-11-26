# Copyright (c) PyPTO Contributors.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------

"""Render PyPTO prefill diagnostics as a standalone HTML report."""

import argparse
from collections import defaultdict
import html
from itertools import groupby
import json
import math
from pathlib import Path


MARKER = "[PD_DIAG] "
COLUMNS = (
    ("ttft", ("P 端", "Prefill TTFT")),
    ("req_resolve", ("请求入口", "req_resolve")),
    ("tokenizer", ("请求入口", "tokenizer")),
    ("add_request", ("请求入口", "add_request")),
    ("queue_time", ("schedule", "queue_time")),
    ("hash", ("schedule", "hash")),
    ("lookup", ("schedule", "lookup")),
    ("alloc", ("schedule", "alloc")),
    ("other", ("schedule", "other")),
    ("load_submit", ("start_load_kv", "submit")),
    ("load_wait", ("start_load_kv", "wait_for_transfer")),
    ("tp", ("模型", "TP")),
    ("device_forward", ("模型", "forward · Device")),
    ("device_sampling", ("模型", "sampling · Device")),
    ("sampling", ("模型", "sampling · Host 共享")),
    ("build_key", ("wait_for_save · Step 共享", "build_key")),
    ("save_lookup", ("wait_for_save · Step 共享", "lookup")),
    ("prepare", ("wait_for_save · Step 共享", "prepare")),
    ("sync", ("wait_for_save · Step 共享", "sync")),
    ("put_submit", ("wait_for_save · Step 共享", "put", "submit")),
    ("put_wait", ("wait_for_save · Step 共享", "put", "wait_for_transfer")),
    ("put_finalize", ("wait_for_save · Step 共享", "put", "finalize")),
    ("detokenizer", ("输出", "detokenizer")),
)
DEPTH = max(len(path) for _, path in COLUMNS)


def domain(row):
    return tuple(row.get(key) for key in ("host", "pid", "run_id"))


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def read_logs(paths):
    rows, errors = [], []
    for path in paths:
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            for line_no, line in enumerate(stream, 1):
                if MARKER not in line:
                    continue
                try:
                    row = json.loads(line.split(MARKER, 1)[1])
                    if not isinstance(row, dict) or row.get("version") != 2:
                        raise ValueError("expected PyPTO diagnostic version 2")
                    if not isinstance(row.get("event"), str):
                        raise ValueError("event must be a string")
                    for key in ("request_id", "role", "span", "host", "run_id"):
                        if row.get(key) is not None and not isinstance(row[key], str):
                            raise ValueError(f"{key} must be a string")
                    for key in ("pid", "step_id", "engine_index", "chunk", "tp_rank", "device_id"):
                        if row.get(key) is not None and type(row[key]) is not int:
                            raise ValueError(f"{key} must be an integer")
                    members = row.get("request_ids", [])
                    if not isinstance(members, list) or any(not isinstance(rid, str) for rid in members):
                        raise ValueError("request_ids must be a list of strings")
                except ValueError as exc:
                    errors.append(f"{path}:{line_no}: {exc}")
                    continue
                rows.append(row)
    return rows, errors


def interval(row):
    start, end = row.get("start_ns"), row.get("end_ns")
    if type(start) is int and type(end) is int and 0 <= start <= end:
        return start, end
    return None


def good(row):
    return row.get("status", "ok") == "ok"


def metrics(rows, name):
    return [row for row in rows if row.get("span") == name and good(row)]


def measure(rows):
    if not rows:
        return None
    ranges = [interval(row) for row in rows]
    if all(ranges) and len({domain(row) for row in rows}) == 1:
        total = right = 0
        for start, end in sorted(ranges):
            total += max(0, end - max(start, right))
            right = max(right, end)
        return total / 1e6
    if len(rows) == 1 and number(rows[0].get("duration_ns")):
        return rows[0]["duration_ns"] / 1e6
    return None


def request_cells(rows):
    return {
        column: measure(metrics(rows, span))
        for column, span in (
            ("ttft", "prefill_ttft"),
            ("req_resolve", "req_resolve"),
            ("tokenizer", "tokenizer"),
            ("add_request", "add_request"),
            ("queue_time", "schedule.queue_time"),
        )
    }


def schedule_cells(rows):
    schedules = metrics(rows, "schedule") + metrics(rows, "schedule.dispatch")
    cells = {"schedule": measure(schedules)}
    parts = []
    for key in ("hash", "lookup", "alloc"):
        events = metrics(rows, f"schedule.{key}")
        cells[key] = measure(events)
        parts.extend(events)
    parents = [interval(row) for row in schedules]
    ranges = [interval(row) for row in parts]
    if parents and all(parents) and all(
        part is not None and any(start <= part[0] <= part[1] <= end for start, end in parents)
        for part in ranges
    ):
        covered = measure(parts) if parts else 0.0
        if covered is not None and cells["schedule"] is not None:
            cells["other"] = max(0.0, cells["schedule"] - covered)
    return cells


def group_rows(rows):
    by_request = defaultdict(list)
    by_step = defaultdict(list)
    transfers = defaultdict(list)
    worker = defaultdict(list)
    devices = defaultdict(list)
    sampling = defaultdict(list)
    for row in rows:
        clock = domain(row)
        rid = row.get("request_id")
        step = row.get("step_id")
        event = row.get("span", "")
        if None in clock or row.get("role") != "prefill":
            continue
        if event == "sampling" and rid:
            sampling[rid, step].append(row)
        elif event == "worker.forward":
            for member in row.get("request_ids", []):
                worker[member, step].append(row)
        elif event in ("device.forward", "device.sampling") and step is not None and row.get("tp_rank") is not None:
            for member in row.get("request_ids", []):
                devices[member, step].append(row)
        elif rid and event.startswith("wait_for_save") and step is not None:
            transfers[clock, rid, step].append(row)
        elif rid:
            by_request[clock, rid].append(row)
        if step is not None and row.get("engine_index") is not None and event.startswith("schedule"):
            by_step[clock, row["engine_index"], step].append(row)

    by_member = defaultdict(list)
    for (clock, engine, step), events in sorted(by_step.items()):
        members = {rid for event in events for rid in event.get("request_ids", [])}
        for rid in members:
            by_member[clock, rid].append((engine, step, events))

    owners = defaultdict(list)
    for key, request in by_request.items():
        if metrics(request, "req_resolve"):
            owners[key[1]].append(key[0])

    output = []
    for index, (clock, rid) in enumerate(sorted(
        (clock, rid) for rid, clocks in owners.items() for clock in clocks
    ), 1):
        title = f"{rid} · {clock[0]} / {clock[1]} / {clock[2]}"
        steps = by_member[clock, rid]
        cells = request_cells(by_request[clock, rid])
        cells.update(schedule_cells([event for _, _, events in steps for event in events]))
        output.append((index, title, 0, None, cells, "请求入口与调度汇总"))
        for number, (engine, step, _) in enumerate(steps, 1):
            forward = worker[rid, step] if len(owners[rid]) == 1 else []
            # A request/step identifies at most one Worker process in this deployment.
            cells = {}
            if len(forward) == 1:
                samples = [row for row in sampling[rid, step] if domain(row) == domain(forward[0])]
                cells["sampling"] = measure(metrics(samples, "sampling"))
            handoff = transfers[clock, rid, step]
            for key, phase in (
                ("prepare", ".prepare"),
                ("put_submit", ".put.submit"), ("put_wait", ".put.wait_for_transfer"),
                ("put_finalize", ".put.finalize"),
            ):
                cells[key] = measure(metrics(handoff, "wait_for_save" + phase))
            note = f"engine {engine} · 原始 step ID {step}"
            ranks = defaultdict(list)
            if len(forward) == 1:
                for event in devices[rid, step]:
                    if domain(event) == domain(forward[0]) and good(event):
                        ranks[event["tp_rank"]].append(event)
            if not ranks:
                output.append((index, title, number, None, cells, note))
            for offset, (rank, observations) in enumerate(sorted(ranks.items())):
                rank_cells = dict(cells) if offset == 0 else {}
                rank_cells["device_forward"] = measure(metrics(observations, "device.forward"))
                rank_cells["device_sampling"] = measure(metrics(observations, "device.sampling"))
                device_ids = {row["device_id"] for row in observations if row.get("device_id") is not None}
                rank_note = f"{note} · device {next(iter(device_ids))}" if len(device_ids) == 1 else note
                output.append((index, title, number, rank, rank_cells, rank_note))
    return output


def header():
    rows = [[f'<th rowspan="{DEPTH}">请求</th><th rowspan="{DEPTH}">Step</th>']]
    rows.extend([] for _ in range(1, DEPTH))
    for depth, row in enumerate(rows):
        index = 0
        while index < len(COLUMNS):
            path = COLUMNS[index][1]
            prefix = path[:depth + 1]
            end = index + 1
            while end < len(COLUMNS) and COLUMNS[end][1][:depth + 1] == prefix:
                end += 1
            if len(path) > depth:
                height = DEPTH - depth if len(path) == depth + 1 else 1
                row.append(f'<th colspan="{end - index}" rowspan="{height}">{html.escape(path[depth])}</th>')
            index = end
    rows[0].append(f'<th rowspan="{DEPTH}">说明</th>')
    return "".join("<tr>" + "".join(row) + "</tr>" for row in rows)


def render(rows, errors):
    options = ['<option value="all">全部请求</option>']
    body = []
    for (index, step), group in groupby(rows, key=lambda row: (row[0], row[2])):
        step_rows = list(group)
        rowspan = f' rowspan="{len(step_rows)}"' if len(step_rows) > 1 else ""
        title = step_rows[0][1]
        request_id = title.split(" · ", 1)[0]
        short_id = request_id[:8] + ("…" if len(request_id) > 8 else "")
        short_id, title = map(html.escape, (short_id, title))
        if step == 0:
            options.append(f'<option value="{index}">{title}</option>')
        for offset, (_, _, _, rank, cells, note) in enumerate(step_rows):
            values = []
            if offset == 0:
                values.extend((
                    f'<td class="request-id" title="{title}"{rowspan}>{short_id}</td>',
                    f'<td class="step"{rowspan}>{step}</td>',
                ))
            for key, _ in COLUMNS:
                rank_column = key in ("tp", "device_forward", "device_sampling")
                if offset and not rank_column:
                    continue
                if key == "tp":
                    value = "—" if rank is None else str(rank)
                else:
                    duration = cells.get(key)
                    value = f"{duration:.3f}" if duration is not None else ""
                span = "" if rank_column else rowspan
                values.append(f'<td data-stage="{key}"{span}>{value}</td>')
            body.append(f'<tr data-request="{index}">' + "".join(values)
                        + f'<td class="note">{html.escape(note)}</td></tr>')
    if not body:
        body.append(f'<tr><td colspan="{len(COLUMNS) + 3}">没有识别到带 prefill 角色的请求日志</td></tr>')
    error_details = ""
    if errors:
        error_details = ("<details><summary>解析问题</summary><pre>"
                         + html.escape("\n".join(errors)) + "</pre></details>")
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PyPTO P 节点请求耗时</title><style>
body{font:14px/1.5 system-ui,sans-serif;color:#213047;margin:24px;background:#fff}
h1{font-size:24px;margin:0 0 8px}p{color:#536174}
.sheet{overflow:auto;max-height:76vh;margin-top:16px;border:1px solid #d5dfeb}
select{max-width:88%;padding:5px}
table{border-collapse:separate;border-spacing:0;white-space:nowrap}
th,td{padding:8px;border-right:1px solid #d5dfeb;border-bottom:1px solid #d5dfeb}
thead{position:sticky;top:0;z-index:3;background:#eaf1fa}
thead tr:nth-child(2){background:#eef7f1}thead tr:nth-child(3){background:#fff4df}
td[data-stage]{text-align:right;font-variant-numeric:tabular-nums}
.step,td[data-stage="tp"]{text-align:center}
.request-id,thead tr:first-child th:first-child{position:sticky;left:0;background:#f4f7fb}
.request-id{padding-inline:5px;font:12px ui-monospace,monospace}
.note{text-align:left}tbody tr:hover td{background:#f0f6ff} [hidden]{display:none!important}
</style></head><body><h1>PyPTO P 节点请求耗时</h1>
<p>单位 ms。req_resolve 从 P 端入口量到编码开始，add_request 从编码完成量到入队。</p>
<p>Step 0 展示入口和调度。每个 TP 显示自己的设备 forward 和独立采集的设备 sampling。Host sampling 与 prepare 之后的交接指标按 Step 共享。</p>
<p>空白表示没有可归属的证据或该阶段不适用。异步阶段可重叠，各列不相加。</p>
<label for="request">请求 </label><select id="request">""" + "".join(options) + """</select>
<div class="sheet"><table><thead>""" + header() + "</thead><tbody>" + "".join(body) + """</tbody></table></div>
""" + error_details + """<script>
const selector=document.getElementById("request");
selector.addEventListener("change",()=>{
  for(const row of document.querySelectorAll("[data-request]"))
    row.hidden=selector.value!=="all" && row.dataset.request!==selector.value;
});
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("-o", "--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.resolve() in {path.resolve() for path in args.logs}:
        parser.error("output must not overwrite an input log")
    rows, errors = read_logs(args.logs)
    report_rows = group_rows(rows)
    args.out.write_text(render(report_rows, errors), encoding="utf-8")
    requests = len({row[0] for row in report_rows})
    print(f"{requests} prefill request(s), {len(errors)} invalid diagnostic(s) -> {args.out}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
