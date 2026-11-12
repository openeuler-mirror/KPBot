#!/usr/bin/env python3
"""Derive TTFT metrics: built-in container-log parser or external tool.

Built-in source (--container + --log-file): fetches the TTFT instrumentation
lines from the vLLM service log file inside the docker container (full log,
all requests since service start) and aggregates the TTFT breakdown emitted
by the patched stack:

  vllm (rosinL/vllm@5d1d24c):
    [TTFT_DEBUG][Tokenizer] render=… tokenize=… n=…
    [TTFT_DEBUG][EngineCore] load=… forward=… save=… sched_hash=…
      sched_ext=… sched_alloc=… sched_overhead=… num_new=… num_total=… update=…
  vllm-ascend (patched kv_transfer.py / pool_worker.py):
    start_load_kv cost time … buildkey … get …
    [TTFT] wait_for_save handle_requset total … build_key … lookup …
      prepare … sync … put …
    [TTFT] wait for save end, cost time … waiting …
    KV pool worker prepare get req=… token_len_chunk=… get_token_len=…
      vllm_cached=… kvpool_cached=…

The external tool path (--tool-cmd) is kept for deriving overall TTFT
values; placeholders: {input} / {output}.

Output metrics.json (extra keys are preserved):
  {"available": bool, "ttft_ms": float|null,
   "ttft_breakdown": {…} when the built-in source matched lines}
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

GREP_RE = "TTFT|start_load_kv cost|KV pool worker prepare get"

PATTERNS: dict[str, re.Pattern[str]] = {
    "tokenizer": re.compile(
        r"\[TTFT_DEBUG\]\[Tokenizer\] render=(?P<render>[\d.]+) "
        r"tokenize=(?P<tokenize>[\d.]+) n=(?P<n>\d+)"
    ),
    "engine_step": re.compile(
        r"\[TTFT_DEBUG\]\[EngineCore\] load=(?P<load>[\d.]+) forward=(?P<forward>[\d.]+) "
        r"save=(?P<save>[\d.]+) sched_hash=(?P<sched_hash>[\d.]+) sched_ext=(?P<sched_ext>[\d.]+) "
        r"sched_alloc=(?P<sched_alloc>[\d.]+) sched_overhead=(?P<sched_overhead>[\d.]+) "
        r"num_new=(?P<num_new>\d+) num_total=(?P<num_total>\d+) update=(?P<update>[\d.]+)"
    ),
    "kv_load": re.compile(
        r"start_load_kv cost time (?P<total>[\d.]+) buildkey (?P<buildkey>[\d.]+) get (?P<get>[\d.]+)"
    ),
    "kv_save": re.compile(
        r"\[TTFT\] wait_for_save handle_requset total (?P<total>[\d.]+) "
        r"build_key (?P<build_key>[\d.]+) lookup (?P<lookup>[\d.]+) prepare (?P<prepare>[\d.]+) "
        r"sync (?P<sync>[\d.]+) put (?P<put>[\d.]+)"
    ),
    "wait_for_save": re.compile(
        r"\[TTFT\] wait for save end, cost time (?P<cost>[\d.]+) waiting (?P<waiting>[\d.]+)"
    ),
    "prometheus_req": re.compile(
        r"\[TTFT_DEBUG\]\[Prometheus\] req=(?P<req>\S+) "
        r"hash=(?P<hash>[\d.]+) ext=(?P<ext>[\d.]+) alloc=(?P<alloc>[\d.]+) "
        r"overhead=(?P<overhead>[\d.]+) load=(?P<load>[\d.]+) forward=(?P<forward>[\d.]+) "
        r"save=(?P<save>[\d.]+) update=(?P<update>[\d.]+)"
    ),
    "prepare_get": re.compile(
        r"KV pool worker prepare get req=(?P<req>\S+) token_len_chunk=(?P<token_len_chunk>\d+) "
        r"get_token_len=(?P<get_token_len>\d+) vllm_cached=(?P<vllm_cached>\d+) "
        r"kvpool_cached=(?P<kvpool_cached>\d+)"
    ),
}

FLOAT_FIELDS = {
    "tokenizer": ("render", "tokenize"),
    "engine_step": ("load", "forward", "save", "sched_hash", "sched_ext", "sched_alloc", "sched_overhead", "update"),
    "kv_load": ("total", "buildkey", "get"),
    "kv_save": ("total", "build_key", "lookup", "prepare", "sync", "put"),
    "wait_for_save": ("cost", "waiting"),
}

COMPOSITION = [
    ("Tokenizer · render", "tokenizer", "render"),
    ("Tokenizer · tokenize", "tokenizer", "tokenize"),
    ("Scheduler · hash+本地cache查找", "engine_step", "sched_hash"),
    ("Scheduler · 外部KV查询", "engine_step", "sched_ext"),
    ("Scheduler · KV block分配", "engine_step", "sched_alloc"),
    ("Scheduler · 其他开销", "engine_step", "sched_overhead"),
    ("Worker · KV load", "engine_step", "load"),
    ("Worker · prefill forward", "engine_step", "forward"),
    ("Worker · KV save", "engine_step", "save"),
    ("EngineCore · update", "engine_step", "update"),
]

KVPOOL_ROWS = [
    ("KV load · 总计", "kv_load", "total"),
    ("KV load · 构造key/收集chunk", "kv_load", "buildkey"),
    ("KV load · 后端传输", "kv_load", "get"),
    ("KV save · 总计", "kv_save", "total"),
    ("KV save · key构造", "kv_save", "build_key"),
    ("KV save · 查重lookup", "kv_save", "lookup"),
    ("KV save · 地址准备", "kv_save", "prepare"),
    ("KV save · NPU event同步", "kv_save", "sync"),
    ("KV save · 后端写入put", "kv_save", "put"),
    ("wait_for_save · 总计", "wait_for_save", "cost"),
    ("wait_for_save · 队列等待", "wait_for_save", "waiting"),
]


def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def fetch_log_lines(container: str, log_file: str, timeout: float) -> tuple[int, str]:
    code, out = _run(
        ["docker", "exec", container, "bash", "-c", f"test -f {shlex.quote(log_file)} && echo FOUND || true"],
        timeout=30,
    )
    if "FOUND" not in out:
        return 1, f"log file not found in container {container!r}: {log_file}"
    code, out = _run(
        [
            "docker", "exec", container, "bash", "-c",
            f"grep -E {shlex.quote(GREP_RE)} {shlex.quote(log_file)} 2>/dev/null || true",
        ],
        timeout=timeout,
    )
    return code, out


def _stats_ms(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    vs = sorted(v * 1000.0 for v in values)

    def pick(p: float) -> float:
        i = round(p / 100 * (len(vs) - 1))
        return vs[min(len(vs) - 1, max(0, int(i)))]

    return {
        "count": len(vs),
        "mean_ms": sum(vs) / len(vs),
        "p50_ms": pick(50),
        "p95_ms": pick(95),
        "max_ms": vs[-1],
    }


def parse_log_lines(lines: list[str]) -> dict[str, Any]:
    floats: dict[str, dict[str, list[float]]] = {s: {f: [] for f in ff} for s, ff in FLOAT_FIELDS.items()}
    ints: dict[str, dict[str, list[int]]] = {
        "engine_step": {"num_new": [], "num_total": []},
        "tokenizer": {"n": []},
        "prepare_get": {"token_len_chunk": [], "get_token_len": [], "vllm_cached": [], "kvpool_cached": []},
    }
    # per-finished-request chain totals from [TTFT_DEBUG][Prometheus] lines
    chain_sums: list[float] = []
    matched = 0
    for line in lines:
        hit = False
        m = PATTERNS["prometheus_req"].search(line)
        if m:
            hit = True
            d = m.groupdict()
            chain_sums.append(
                sum(float(d[k]) for k in ("hash", "ext", "alloc", "overhead", "load", "forward", "save", "update"))
            )
        else:
            for sec, pat in PATTERNS.items():
                if sec == "prometheus_req":
                    continue
                mm = pat.search(line)
                if not mm:
                    continue
                hit = True
                d = mm.groupdict()
                for f in FLOAT_FIELDS.get(sec, ()):
                    if d.get(f) is not None:
                        floats[sec][f].append(float(d[f]))
                if sec in ints:
                    for f in ints[sec]:
                        if d.get(f) is not None:
                            ints[sec][f].append(int(d[f]))
                break
        if hit:
            matched += 1

    sections = {s: {f: _stats_ms(v) for f, v in ff.items()} for s, ff in floats.items()}

    def row(name: str, sec: str, field: str) -> dict[str, Any] | None:
        st = sections.get(sec, {}).get(field)
        if not st or not st.get("count"):
            return None
        return {"name": name, **st}

    composition = [r for r in (row(n, s, f) for n, s, f in COMPOSITION) if r]
    total_mean = sum(r["mean_ms"] for r in composition) or 1.0
    for r in composition:
        r["pct"] = r["mean_ms"] / total_mean * 100.0
    kvpool_rows = [r for r in (row(n, s, f) for n, s, f in KVPOOL_ROWS) if r]

    info: dict[str, Any] = {}
    for sec, ff in ints.items():
        for f, vals in ff.items():
            if vals:
                info[f"{sec}.{f}"] = {"count": len(vals), "mean": sum(vals) / len(vals)}

    chain: dict[str, Any] | None = None
    if chain_sums:
        st = _stats_ms(chain_sums)
        chain = {
            **st,
            "definition": (
                "per-finished-request sum of hash/ext/alloc/overhead/load/forward/"
                "save/update from [TTFT_DEBUG][Prometheus]; excludes queue wait, "
                "tokenizer and output stages"
            ),
        }

    return {
        "matched_lines": matched,
        "sections": sections,
        "composition": composition,
        "kvpool_rows": kvpool_rows,
        "token_stats": info,
        "request_chain": chain,
    }


def _resolve_container(explicit: str) -> str:
    """Use the explicit container or auto-detect the vLLM one (see container_utils)."""
    if explicit:
        return explicit
    import container_utils

    det = container_utils.detect_vllm_container()
    if "error" in det:
        print(
            json.dumps(
                {"error": det["error"], "candidates": det.get("candidates", []),
                 "hint": "pass --container explicitly"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        json.dumps({"container_detected": det["container"], "detail": det["matched_by"]}, ensure_ascii=False),
        file=sys.stderr,
    )
    return det["container"]


def _metrics_from_breakdown(breakdown: dict[str, Any], source: str) -> dict[str, Any]:
    chain = breakdown.get("request_chain")
    metrics: dict[str, Any] = {
        "available": breakdown["matched_lines"] > 0,
        "source": source,
        "ttft_ms": round(chain["p50_ms"], 2) if chain else None,
"ttft_breakdown": breakdown,
    }
    if chain:
        metrics["ttft_definition"] = (
            "TTFT = p50 of per-request instrumentation-chain totals "
            "(hash+ext+alloc+overhead+load+forward+save+update); "
            "queue wait / tokenizer / output not included"
        )
    if not metrics["available"]:
        metrics["reason"] = (
            "no TTFT instrumentation lines matched in the log — is the patched "
            "vllm (rosinL/vllm@5d1d24c) / vllm-ascend running and the log path correct?"
        )
    return metrics


def run_builtin(container: str, log_file: str, out_path: Path, timeout: float) -> int:
    code, out = fetch_log_lines(container, log_file, timeout)
    if code != 0:
        out_path.write_text(
            json.dumps({"available": False, "reason": out[:500], "ttft_ms": None}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(json.dumps({"out": str(out_path), "available": False, "reason": out[:500]}), file=sys.stderr)
        return 1

    breakdown = parse_log_lines(out.splitlines())
    # keep the fetched instrumentation lines as a run artifact next to metrics.json
    log_out = out_path.parent / "ttft_logs.txt"
    log_out.write_text(out if out.endswith("\n") or not out else out + "\n", encoding="utf-8")
    metrics = _metrics_from_breakdown(breakdown, f"container={container} log={log_file}")
    metrics["ttft_log"] = {"file": log_out.name, "matched_lines": breakdown["matched_lines"]}
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out_path),
                "ttft_log_out": str(log_out),
                "available": metrics["available"],
                "matched_lines": breakdown["matched_lines"],
                "segments": [r["name"] for r in breakdown["composition"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if metrics["available"] else 1


def run_local(log_path: Path, out_path: Path) -> int:
    """Parse a local instrumentation-log file (e.g. a transferred ttft_logs.txt)."""
    if not log_path.is_file():
        out_path.write_text(
            json.dumps({"available": False, "reason": f"log file not found: {log_path}", "ttft_ms": None}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(json.dumps({"out": str(out_path), "available": False, "reason": f"log file not found: {log_path}"}), file=sys.stderr)
        return 1
    breakdown = parse_log_lines(log_path.read_text(encoding="utf-8", errors="replace").splitlines())
    metrics = _metrics_from_breakdown(breakdown, f"local log={log_path}")
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out_path),
                "available": metrics["available"],
                "matched_lines": breakdown["matched_lines"],
                "segments": [r["name"] for r in breakdown["composition"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if metrics["available"] else 1


def run_external(args: argparse.Namespace, out_path: Path) -> int:
    if not args.tool_cmd:
        stub = {
            "available": False,
            "reason": "no metrics source configured (--container/--log-file or --tool-cmd)",
            "ttft_ms": None,
        }
        out_path.write_text(json.dumps(stub, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"out": str(out_path), **stub}, ensure_ascii=False, indent=2))
        return 0

    cmd_str = args.tool_cmd.format(input=args.input, output=str(out_path))
    argv = shlex.split(cmd_str)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=args.timeout)
    except Exception as e:  # noqa: BLE001
        stub = {"available": False, "reason": f"tool failed to launch: {e}", "ttft_ms": None}
        out_path.write_text(json.dumps(stub, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"out": str(out_path), **stub}), file=sys.stderr)
        return 1

    metrics: dict[str, Any] = {}
    if out_path.exists():
        try:
            metrics = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metrics = {}
    if not metrics and proc.stdout.strip():
        try:
            metrics = json.loads(proc.stdout)
        except json.JSONDecodeError:
            metrics = {"raw_stdout": proc.stdout[:2000]}

    metrics.setdefault("available", proc.returncode == 0 and bool(metrics))
    metrics.setdefault("ttft_ms", None)
    metrics["tool_cmd"] = cmd_str
    metrics["tool_returncode"] = proc.returncode
    if proc.stderr:
        metrics["tool_stderr"] = proc.stderr[:1000]

    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out_path), "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0 if metrics.get("available") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="", help="run dir / raw dir / log file for the external tool")
    ap.add_argument("--out", default="metrics.json")
    ap.add_argument(
        "--container",
        default="",
        help="docker container running the vLLM service (enables the built-in TTFT log parser)",
    )
    ap.add_argument(
        "--log-file",
        default="",
        help="path of the vLLM service log file INSIDE the container (ask the user when unknown)",
    )
    ap.add_argument(
        "--local-log",
        default="",
        help="local instrumentation-log file (e.g. a transferred ttft_logs.txt); no container needed",
    )
    ap.add_argument(
        "--tool-cmd",
        default="",
        help='external tool command template, e.g. "my_ttft_tool --in {input} --out {output}"',
    )
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.local_log:
        return run_local(Path(args.local_log).expanduser().resolve(), out_path)
    if args.log_file:
        container = _resolve_container(args.container)
        return run_builtin(container, args.log_file, out_path, args.timeout)
    return run_external(args, out_path)


if __name__ == "__main__":
    sys.exit(main())
