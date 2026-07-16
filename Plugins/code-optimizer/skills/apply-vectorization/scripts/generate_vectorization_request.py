#!/usr/bin/env python3
"""Generate a canonical apply-vectorization request JSON file."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


BLAS_CASES: dict[str, dict[str, Any]] = {
    "cblas_sdot": {
        "target_function": "cblas_sdot",
        "loop_variable": "index",
        "iteration_count": "n",
        "body_operations": ["accum += x[index] * y[index]"],
        "dependencies": ["unit-stride fast path gated by incx == 1 && incy == 1"],
        "data_types": ["float32"],
    },
    "cblas_saxpy": {
        "target_function": "cblas_saxpy",
        "loop_variable": "index",
        "iteration_count": "n",
        "body_operations": ["y[index] += alpha * x[index]"],
        "dependencies": ["unit-stride fast path gated by incx == 1 && incy == 1"],
        "data_types": ["float32"],
    },
    "cblas_sscal": {
        "target_function": "cblas_sscal",
        "loop_variable": "index",
        "iteration_count": "n",
        "body_operations": ["x[index] *= alpha"],
        "dependencies": ["unit-stride fast path gated by incx == 1"],
        "data_types": ["float32"],
    },
    "cblas_scopy": {
        "target_function": "cblas_scopy",
        "loop_variable": "index",
        "iteration_count": "n",
        "body_operations": ["y[index] = x[index]"],
        "dependencies": ["unit-stride fast path gated by incx == 1 && incy == 1"],
        "data_types": ["float32"],
    },
    "cblas_sswap": {
        "target_function": "cblas_sswap",
        "loop_variable": "index",
        "iteration_count": "n",
        "body_operations": [
            "float temp = x[index]",
            "x[index] = y[index]",
            "y[index] = temp",
        ],
        "dependencies": ["unit-stride fast path gated by incx == 1 && incy == 1"],
        "data_types": ["float32"],
    },
    "cblas_srot": {
        "target_function": "cblas_srot",
        "loop_variable": "index",
        "iteration_count": "n",
        "body_operations": [
            "float temp = (c * x[index]) + (s * y[index])",
            "y[index] = (c * y[index]) - (s * x[index])",
            "x[index] = temp",
        ],
        "dependencies": ["unit-stride fast path gated by incx == 1 && incy == 1"],
        "data_types": ["float32"],
    },
    "cblas_sgemv": {
        "target_function": "cblas_sgemv",
        "loop_variable": "col",
        "iteration_count": "n",
        "body_operations": ["accum += a[row * lda + col] * x[col]"],
        "dependencies": [
            "row-major no-trans fast path",
            "unit-stride vectors gated by incx == 1 && incy == 1",
        ],
        "data_types": ["float32"],
    },
    "cblas_sger": {
        "target_function": "cblas_sger",
        "loop_variable": "col",
        "iteration_count": "n",
        "body_operations": ["a[row * lda + col] += scaled_x * y[col]"],
        "dependencies": [
            "row-major fast path",
            "unit-stride vectors gated by incx == 1 && incy == 1",
        ],
        "data_types": ["float32"],
    },
    "cblas_ssymv": {
        "target_function": "cblas_ssymv",
        "loop_variable": "col",
        "iteration_count": "n",
        "body_operations": ["accum += a[row * lda + col] * x[col]"],
        "dependencies": [
            "row-major upper fast path",
            "target upper-row loop has contiguous A and X reads",
        ],
        "data_types": ["float32"],
    },
    "cblas_ssyr": {
        "target_function": "cblas_ssyr",
        "loop_variable": "col",
        "iteration_count": "n",
        "body_operations": ["a[row * lda + col] += scaled_x * x[col]"],
        "dependencies": [
            "row-major upper fast path",
            "target upper-row loop updates contiguous A row entries",
        ],
        "data_types": ["float32"],
    },
    "cblas_sgemm": {
        "target_function": "cblas_sgemm",
        "loop_variable": "col",
        "iteration_count": "n",
        "body_operations": ["c[row * ldc + col] += scaled_a * b[depth * ldb + col]"],
        "dependencies": [
            "row-major no-trans fast path",
            "col loop updates contiguous C and B rows",
        ],
        "data_types": ["float32"],
    },
    "cblas_ssyrk": {
        "target_function": "cblas_ssyrk",
        "loop_variable": "depth",
        "iteration_count": "k",
        "body_operations": ["accum += a[row * lda + depth] * a[col * lda + depth]"],
        "dependencies": [
            "row-major upper no-trans fast path",
            "depth reduction is local to one upper-triangle output element",
        ],
        "data_types": ["float32"],
    },
}

REQUEST_REQUIRED_KEYS = {"target_function", "loop_info", "target_arch", "data_types"}
LOOP_REQUIRED_KEYS = {"file_path", "start_line", "end_line"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a canonical apply-vectorization request JSON file."
    )
    parser.add_argument(
        "--arch",
        required=True,
        choices=("neon", "sve", "sme"),
        help="Target architecture.",
    )
    parser.add_argument(
        "--case",
        choices=sorted(BLAS_CASES),
        help="Runtime BLAS case name. When provided, the target loop is inferred from the scalar source.",
    )
    parser.add_argument("--source-file", help="Path to the source file.")
    parser.add_argument("--target-function", help="Target function name.")
    parser.add_argument("--start-line", type=int, help="Loop start line.")
    parser.add_argument("--end-line", type=int, help="Loop end line.")
    parser.add_argument(
        "--data-type",
        dest="data_types",
        action="append",
        help="Repeat for each data type, for example --data-type float32.",
    )
    parser.add_argument("--loop-variable", help="Loop variable name.")
    parser.add_argument("--iteration-count", help="Loop iteration count expression.")
    parser.add_argument(
        "--body-operation",
        action="append",
        default=[],
        help="Repeat for each body operation description.",
    )
    parser.add_argument(
        "--dependency",
        action="append",
        default=[],
        help="Repeat for each dependency description.",
    )
    parser.add_argument(
        "--semantic-contract-json",
        help="Semantic contract JSON object or path to a JSON file.",
    )
    parser.add_argument(
        "--aliasing",
        help="Aliasing contract, for example restrict, no_overlap, unknown.",
    )
    parser.add_argument(
        "--index-property",
        action="append",
        default=[],
        help="Repeat for each index property, for example readonly, unique, in_bounds.",
    )
    parser.add_argument(
        "--math-mode",
        help="Math contract, for example strict, reassociation_allowed, fast_math_allowed.",
    )
    parser.add_argument(
        "--requires-bit-exact",
        action="store_true",
        help="Mark that the request requires bit-exact results.",
    )
    parser.add_argument(
        "--allows-reassociation",
        action="store_true",
        help="Mark that floating-point reassociation is allowed.",
    )
    parser.add_argument(
        "--repo-root",
        help="Repository root for relative file paths. Defaults to the parent of the skill directory.",
    )
    parser.add_argument("--output", help="Write JSON to this path instead of stdout.")
    return parser.parse_args()


def load_semantic_contract(raw_value: str) -> dict[str, Any]:
    """Load a semantic contract from an inline JSON object or a JSON file."""

    stripped = raw_value.lstrip()
    if stripped.startswith("{"):
        text = raw_value
    else:
        try:
            candidate_path = Path(raw_value)
            if candidate_path.exists():
                text = candidate_path.read_text(encoding="utf-8")
            else:
                text = raw_value
        except OSError:
            text = raw_value
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[错误] semantic_contract JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("[错误] semantic_contract 必须是 JSON 对象")
    return payload


def build_semantic_contract(args: argparse.Namespace) -> dict[str, Any] | None:
    """Build the optional semantic contract payload."""

    contract: dict[str, Any] = {}
    if args.semantic_contract_json:
        contract.update(load_semantic_contract(args.semantic_contract_json))
    if args.aliasing:
        contract["aliasing"] = args.aliasing
    if args.index_property:
        contract["index_properties"] = args.index_property
    if args.math_mode:
        contract["math_mode"] = args.math_mode
    if args.requires_bit_exact:
        contract["requires_bit_exact"] = True
    if args.allows_reassociation:
        contract["allows_reassociation"] = True
    return contract or None


def infer_repo_root(script_path: Path, repo_root_arg: str | None) -> Path:
    if repo_root_arg:
        return Path(repo_root_arg).resolve()
    return script_path.resolve().parents[2]


def normalize_file_path(source_file: Path, repo_root: Path) -> str:
    try:
        relative = source_file.resolve().relative_to(repo_root)
        return relative.as_posix()
    except ValueError:
        return source_file.resolve().as_posix()


def normalize_for_match(text: str) -> str:
    """Collapse whitespace for source matching."""

    return " ".join(text.split())


def find_function_bounds(lines: list[str], target_function: str) -> tuple[int, int]:
    """Return 1-based inclusive line bounds of one function definition."""

    signature_index = None
    signature_token = f"{target_function}("
    for line_index, line in enumerate(lines):
        if signature_token in line:
            signature_index = line_index
            break

    if signature_index is None:
        raise SystemExit(f"[错误] 源文件中找不到目标函数: {target_function}")

    body_started = False
    brace_depth = 0
    for line_index in range(signature_index, len(lines)):
        line = lines[line_index]
        open_count = line.count("{")
        close_count = line.count("}")
        if open_count > 0 and not body_started:
            body_started = True
        if body_started:
            brace_depth += open_count
            brace_depth -= close_count
            if brace_depth == 0:
                return signature_index + 1, line_index + 1

    raise SystemExit(f"[错误] 目标函数大括号不完整: {target_function}")


def find_loop_end(lines: list[str], start_index: int, search_end_index: int) -> int:
    """Return the 0-based line index of the matching closing brace for one loop."""

    body_started = False
    brace_depth = 0
    for line_index in range(start_index, search_end_index + 1):
        line = lines[line_index]
        open_count = line.count("{")
        close_count = line.count("}")
        if open_count > 0 and not body_started:
            body_started = True
        if body_started:
            brace_depth += open_count
            brace_depth -= close_count
            if brace_depth == 0:
                return line_index

    raise SystemExit(f"[错误] 无法闭合目标循环: line {start_index + 1}")


def infer_case_loop_lines(source_file: Path, case_meta: dict[str, Any]) -> tuple[int, int]:
    """Infer the case loop line range from function name and operation patterns."""

    lines = source_file.read_text(encoding="utf-8").splitlines()
    function_start, function_end = find_function_bounds(lines, case_meta["target_function"])
    loop_variable = str(case_meta["loop_variable"])
    iteration_count = normalize_for_match(str(case_meta["iteration_count"]))
    normalized_ops = [normalize_for_match(op) for op in case_meta["body_operations"]]
    loop_pattern = re.compile(r"\bfor\s*\(")
    variable_pattern = re.compile(rf"\b{re.escape(loop_variable)}\b")
    candidates: list[tuple[int, int, bool]] = []

    for line_index in range(function_start - 1, function_end):
        header_line = lines[line_index]
        if not loop_pattern.search(header_line):
            continue

        loop_end_index = find_loop_end(lines, line_index, function_end - 1)
        header_norm = normalize_for_match(header_line)
        body_norm = normalize_for_match("\n".join(lines[line_index : loop_end_index + 1]))
        if not variable_pattern.search(header_norm):
            continue
        if not all(operation in body_norm for operation in normalized_ops):
            continue

        candidates.append((line_index + 1, loop_end_index + 1, iteration_count in header_norm))

    if not candidates:
        raise SystemExit(
            f"[错误] 无法自动定位 case 目标循环: {source_file}\n"
            f"[提示] target_function={case_meta['target_function']} loop_variable={loop_variable}\n"
            f"[提示] body_operations={case_meta['body_operations']}"
        )

    preferred = [candidate for candidate in candidates if candidate[2]]
    ranked = preferred or candidates
    ranked.sort(key=lambda candidate: (candidate[1] - candidate[0], candidate[0]))
    best = ranked[0]
    same_span = [candidate for candidate in ranked if candidate[:2] == best[:2]]
    if len(same_span) > 1:
        raise SystemExit(f"[错误] 自动定位 case 循环存在歧义: {source_file}")

    return best[0], best[1]


def validate_payload(payload: dict[str, Any], repo_root: Path) -> None:
    missing = REQUEST_REQUIRED_KEYS - payload.keys()
    if missing:
        raise SystemExit(f"[错误] 请求缺少字段: {sorted(missing)}")

    if payload["target_arch"] not in {"neon", "sve", "sme"}:
        raise SystemExit(f"[错误] 非法 target_arch: {payload['target_arch']}")

    data_types = payload["data_types"]
    if not isinstance(data_types, list) or not data_types:
        raise SystemExit("[错误] data_types 必须是非空数组")

    loop_info = payload["loop_info"]
    if not isinstance(loop_info, dict):
        raise SystemExit("[错误] loop_info 必须是对象")

    loop_missing = LOOP_REQUIRED_KEYS - loop_info.keys()
    if loop_missing:
        raise SystemExit(f"[错误] loop_info 缺少字段: {sorted(loop_missing)}")

    source_path = Path(loop_info["file_path"])
    if not source_path.is_absolute():
        source_path = repo_root / source_path
    if not source_path.exists():
        raise SystemExit(f"[错误] 源文件不存在: {source_path}")

    start_line = loop_info["start_line"]
    end_line = loop_info["end_line"]
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        raise SystemExit("[错误] loop_info.start_line/end_line 必须是整数")
    if start_line <= 0 or end_line < start_line:
        raise SystemExit("[错误] 循环行号范围非法")

    total_lines = len(source_path.read_text(encoding="utf-8").splitlines())
    if end_line > total_lines:
        raise SystemExit("[错误] 循环行号超出源文件范围")

    if payload["target_arch"] == "neon" and payload.get("vector_width") != 128:
        raise SystemExit("[错误] neon request 必须包含 vector_width=128")
    semantic_contract = payload.get("semantic_contract")
    if semantic_contract is not None:
        if not isinstance(semantic_contract, dict):
            raise SystemExit("[错误] semantic_contract 必须是对象")
        for bool_key in ("requires_bit_exact", "allows_reassociation"):
            if bool_key in semantic_contract and not isinstance(semantic_contract[bool_key], bool):
                raise SystemExit(f"[错误] semantic_contract.{bool_key} 必须是布尔值")
        if "index_properties" in semantic_contract and not isinstance(
            semantic_contract["index_properties"], list
        ):
            raise SystemExit("[错误] semantic_contract.index_properties 必须是数组")


def build_case_payload(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    case_meta = BLAS_CASES[args.case]
    source_file = (
        repo_root
        / "apply-vectorization"
        / "assets"
        / "real-source-cases"
        / f"{args.case}_scalar.c"
    ).resolve()
    if not source_file.exists():
        raise SystemExit(f"[错误] case 源文件不存在: {source_file}")

    start_line, end_line = infer_case_loop_lines(source_file, case_meta)
    loop_info: dict[str, Any] = {
        "file_path": normalize_file_path(source_file, repo_root),
        "start_line": start_line,
        "end_line": end_line,
        "loop_variable": case_meta["loop_variable"],
        "iteration_count": case_meta["iteration_count"],
        "body_operations": case_meta["body_operations"],
        "dependencies": case_meta["dependencies"],
    }
    payload: dict[str, Any] = {
        "target_function": case_meta["target_function"],
        "loop_info": loop_info,
        "target_arch": args.arch,
        "data_types": case_meta["data_types"],
    }
    if args.arch == "neon":
        payload["vector_width"] = 128
    semantic_contract = build_semantic_contract(args)
    if semantic_contract is not None:
        payload["semantic_contract"] = semantic_contract
    validate_payload(payload, repo_root)
    return payload


def build_explicit_payload(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    if not args.source_file or not args.target_function:
        raise SystemExit("[错误] 非 case 模式下必须提供 --source-file 和 --target-function")
    if args.start_line is None or args.end_line is None:
        raise SystemExit("[错误] 非 case 模式下必须提供 --start-line 和 --end-line")
    if not args.data_types:
        raise SystemExit("[错误] 非 case 模式下必须至少提供一个 --data-type")

    source_file = Path(args.source_file).resolve()
    if not source_file.exists():
        raise SystemExit(f"[错误] 源文件不存在: {source_file}")

    loop_info: dict[str, Any] = {
        "file_path": normalize_file_path(source_file, repo_root),
        "start_line": args.start_line,
        "end_line": args.end_line,
    }
    if args.loop_variable:
        loop_info["loop_variable"] = args.loop_variable
    if args.iteration_count:
        loop_info["iteration_count"] = args.iteration_count
    if args.body_operation:
        loop_info["body_operations"] = args.body_operation
    if args.dependency:
        loop_info["dependencies"] = args.dependency

    payload: dict[str, Any] = {
        "target_function": args.target_function,
        "loop_info": loop_info,
        "target_arch": args.arch,
        "data_types": args.data_types,
    }
    if args.arch == "neon":
        payload["vector_width"] = 128
    semantic_contract = build_semantic_contract(args)
    if semantic_contract is not None:
        payload["semantic_contract"] = semantic_contract
    validate_payload(payload, repo_root)
    return payload


def main() -> int:
    args = parse_args()
    repo_root = infer_repo_root(Path(__file__), args.repo_root)

    if args.case:
        payload = build_case_payload(args, repo_root)
    else:
        payload = build_explicit_payload(args, repo_root)

    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
