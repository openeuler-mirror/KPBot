#!/usr/bin/env python3
"""Validate a vectorization_result response and materialize generated sources."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


REQUEST_REQUIRED_KEYS = {"target_function", "loop_info", "target_arch", "data_types"}
LOOP_REQUIRED_KEYS = {"file_path", "start_line", "end_line"}
CODEGEN_STYLES = {"auto", "intrinsics", "inline_asm", "assembly"}
REPLACEMENT_KINDS = {"full_function", "function_body", "loop_body", "translation_unit"}
APPLICATION_MODES = {"materialize_to_generate", "inplace_replace"}
ARTIFACT_LANGUAGES = {"c", "c_header", "asm", "assembly"}
ASSEMBLY_SUFFIXES = {".s", ".asm"}
CPP_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh"}
RESULT_REQUIRED_KEYS = {
    "success",
    "modified_file",
    "original_loop",
    "vectorized_code",
    "intrinsics_used",
    "epilogue_handling",
    "expected_speedup",
    "safety_checks",
    "error_message",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a vectorization_result response and materialize generated sources."
    )
    parser.add_argument("--request-json", required=True, help="Canonical request JSON path.")
    parser.add_argument("--response-json", required=True, help="Response JSON path.")
    parser.add_argument("--output-source", required=True, help="Materialized C source path.")
    parser.add_argument(
        "--repo-root",
        help="Repository root for resolving loop_info.file_path. Defaults to the parent of the skill directory.",
    )
    parser.add_argument(
        "--syntax-check",
        action="store_true",
        help="Run a host-side syntax-only compile check after materialization.",
    )
    parser.add_argument(
        "--syntax-compiler",
        default="cc",
        help="Compiler used for --syntax-check. Default: cc.",
    )
    return parser.parse_args()


def infer_repo_root(script_path: Path, repo_root_arg: str | None) -> Path:
    if repo_root_arg:
        return Path(repo_root_arg).resolve()
    return script_path.resolve().parents[2]


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"[错误] 找不到 JSON 文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[错误] JSON 解析失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"[错误] JSON 顶层必须是对象: {path}")
    return data


def validate_request(data: dict[str, Any]) -> None:
    missing = REQUEST_REQUIRED_KEYS - data.keys()
    if missing:
        raise SystemExit(f"[错误] 请求缺少字段: {sorted(missing)}")
    if data["target_arch"] not in {"neon", "sve", "sme"}:
        raise SystemExit(f"[错误] 非法 target_arch: {data['target_arch']}")
    data_types = data["data_types"]
    if not isinstance(data_types, list) or not data_types:
        raise SystemExit("[错误] 请求中的 data_types 必须是非空数组")
    loop_info = data["loop_info"]
    if not isinstance(loop_info, dict):
        raise SystemExit("[错误] 请求中的 loop_info 必须是对象")
    loop_missing = LOOP_REQUIRED_KEYS - loop_info.keys()
    if loop_missing:
        raise SystemExit(f"[错误] 请求中的 loop_info 缺少字段: {sorted(loop_missing)}")
    if not isinstance(loop_info["start_line"], int) or not isinstance(loop_info["end_line"], int):
        raise SystemExit("[错误] 请求中的 loop_info.start_line/end_line 必须是整数")
    if loop_info["start_line"] <= 0 or loop_info["end_line"] < loop_info["start_line"]:
        raise SystemExit("[错误] 请求中的循环行号范围非法")
    if data["target_arch"] == "neon" and data.get("vector_width") != 128:
        raise SystemExit("[错误] neon request 必须包含 vector_width=128")
    codegen_style = data.get("codegen_style", "auto")
    if codegen_style not in CODEGEN_STYLES:
        raise SystemExit(f"[错误] 非法 codegen_style: {codegen_style}")
    optimization_level = data.get("optimization_level")
    if optimization_level is not None and optimization_level not in {"intrinsics", "asm"}:
        raise SystemExit(f"[错误] 非法 optimization_level: {optimization_level}")
    semantic_contract = data.get("semantic_contract")
    if semantic_contract is not None:
        validate_semantic_contract(semantic_contract)


def validate_semantic_contract(semantic_contract: Any) -> None:
    """Validate optional semantic proof metadata attached to the request."""

    if not isinstance(semantic_contract, dict):
        raise SystemExit("[错误] semantic_contract 必须是对象")
    for bool_key in ("requires_bit_exact", "allows_reassociation"):
        if bool_key in semantic_contract and not isinstance(semantic_contract[bool_key], bool):
            raise SystemExit(f"[错误] semantic_contract.{bool_key} 必须是布尔值")
    if "index_properties" in semantic_contract and not isinstance(
        semantic_contract["index_properties"], list
    ):
        raise SystemExit("[错误] semantic_contract.index_properties 必须是数组")


def validate_response(data: dict[str, Any]) -> dict[str, Any]:
    if "vectorization_result" not in data:
        raise SystemExit("[错误] 响应缺少 vectorization_result")
    result = data["vectorization_result"]
    if not isinstance(result, dict):
        raise SystemExit("[错误] vectorization_result 必须是对象")
    missing = RESULT_REQUIRED_KEYS - result.keys()
    if missing:
        raise SystemExit(f"[错误] 响应缺少字段: {sorted(missing)}")
    if not result["success"]:
        raise SystemExit("[错误] 响应 success=false，不能物化")
    result_style = result.get("codegen_style")
    if result_style is not None and result_style not in CODEGEN_STYLES - {"auto"}:
        raise SystemExit(f"[错误] 非法 vectorization_result.codegen_style: {result_style}")
    replacement_kind = result.get("replacement_kind")
    if replacement_kind is not None and replacement_kind not in REPLACEMENT_KINDS:
        raise SystemExit(f"[错误] 非法 vectorization_result.replacement_kind: {replacement_kind}")
    application_mode = result.get("application_mode")
    if application_mode is not None and application_mode not in APPLICATION_MODES:
        raise SystemExit(f"[错误] 非法 vectorization_result.application_mode: {application_mode}")

    vectorized_code = result["vectorized_code"]
    if not isinstance(vectorized_code, str) or not vectorized_code.strip():
        raise SystemExit("[错误] 响应缺少非空 vectorized_code")
    artifacts = result.get("artifacts", [])
    if artifacts is None:
        artifacts = []
        result["artifacts"] = artifacts
    if not isinstance(artifacts, list):
        raise SystemExit("[错误] vectorization_result.artifacts 必须是数组")
    for index, artifact in enumerate(artifacts):
        validate_artifact(artifact, index)
    return result


def validate_artifact(artifact: Any, index: int) -> None:
    """Validate one optional generated artifact entry."""

    if not isinstance(artifact, dict):
        raise SystemExit(f"[错误] artifacts[{index}] 必须是对象")
    required = {"path_suffix", "language", "role", "content"}
    missing = required - artifact.keys()
    if missing:
        raise SystemExit(f"[错误] artifacts[{index}] 缺少字段: {sorted(missing)}")
    path_suffix = artifact["path_suffix"]
    if not isinstance(path_suffix, str) or not path_suffix.strip():
        raise SystemExit(f"[错误] artifacts[{index}].path_suffix 必须是非空字符串")
    path = Path(path_suffix)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise SystemExit(f"[错误] artifacts[{index}].path_suffix 不能是绝对路径或包含 '..'")
    if artifact["language"] not in ARTIFACT_LANGUAGES:
        raise SystemExit(f"[错误] artifacts[{index}].language 不受支持: {artifact['language']}")
    if not isinstance(artifact["role"], str) or not artifact["role"].strip():
        raise SystemExit(f"[错误] artifacts[{index}].role 必须是非空字符串")
    if not isinstance(artifact["content"], str) or not artifact["content"].strip():
        raise SystemExit(f"[错误] artifacts[{index}].content 必须是非空字符串")


def resolve_source_path(loop_info: dict[str, Any], repo_root: Path) -> Path:
    source_path = Path(loop_info["file_path"])
    if not source_path.is_absolute():
        source_path = repo_root / source_path
    if not source_path.exists():
        raise SystemExit(f"[错误] 源文件不存在: {source_path}")
    return source_path.resolve()


def infer_source_style(source_path: Path, source_text: str) -> str:
    """Infer the default code-generation style from the input source shape."""

    suffix = source_path.suffix.lower()
    if suffix in ASSEMBLY_SUFFIXES:
        return "assembly"
    if suffix in CPP_SUFFIXES and re.search(r"\b(?:__asm__|asm)\b", source_text):
        return "inline_asm"
    return "intrinsics"


def requested_style(request_data: dict[str, Any]) -> str:
    """Resolve request-level style, preserving old optimization_level compatibility."""

    codegen_style = request_data.get("codegen_style", "auto")
    if codegen_style != "auto":
        return str(codegen_style)
    optimization_level = request_data.get("optimization_level")
    if optimization_level == "intrinsics":
        return "intrinsics"
    if optimization_level == "asm":
        return "inline_asm"
    return "auto"


def resolve_codegen_style(request_data: dict[str, Any], source_path: Path, source_text: str) -> str:
    """Resolve the effective code-generation style and reject unsafe mismatches."""

    inferred = infer_source_style(source_path, source_text)
    requested = requested_style(request_data)
    if requested == "auto":
        return inferred
    if inferred == "assembly" and requested != "assembly":
        raise SystemExit(
            "[错误] codegen_style 与源码形态不匹配：纯汇编输入必须使用 assembly 或 auto"
        )
    if inferred == "inline_asm" and requested == "intrinsics":
        raise SystemExit(
            "[错误] codegen_style 与源码形态不匹配：含 inline asm 的 C/C++ 输入不能按 intrinsics 物化"
        )
    return requested


def find_function_bounds(source_text: str, target_function: str) -> tuple[int, int, int]:
    match = re.search(rf"\b{re.escape(target_function)}\s*\(", source_text)
    if match is None:
        raise SystemExit(f"[错误] 在源文件中找不到目标函数: {target_function}")

    function_start = source_text.rfind("\n", 0, match.start()) + 1
    open_brace = source_text.find("{", match.end())
    if open_brace == -1:
        raise SystemExit(f"[错误] 无法定位函数体起始大括号: {target_function}")

    depth = 0
    for index in range(open_brace, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return function_start, open_brace, index

    raise SystemExit(f"[错误] 无法定位函数体结束大括号: {target_function}")


def looks_like_full_function(code: str, target_function: str) -> bool:
    return bool(
        re.search(rf"\b{re.escape(target_function)}\s*\(", code) and "{" in code and "}" in code
    )


def strip_outer_braces(snippet: str) -> str:
    stripped = snippet.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped[1:-1].strip()
    return stripped


def indent_block(snippet: str, indent: str) -> str:
    lines = snippet.splitlines()
    return "\n".join(f"{indent}{line.rstrip()}" if line else "" for line in lines)


def materialize_from_body(source_text: str, target_function: str, snippet: str) -> str:
    function_start, open_brace, close_brace = find_function_bounds(source_text, target_function)
    body = strip_outer_braces(snippet)
    if not body:
        raise SystemExit("[错误] 无法将空代码片段包裹成可编译函数")

    header = source_text[function_start : open_brace + 1]
    suffix = source_text[close_brace:]
    materialized = source_text[:function_start] + header + "\n" + indent_block(body, "    ")
    if not materialized.endswith("\n"):
        materialized += "\n"
    materialized += suffix
    return materialized


def materialize_from_loop(source_text: str, loop_info: dict[str, Any], snippet: str) -> str:
    """Replace the requested loop line range in a full source copy."""

    body = strip_outer_braces(snippet)
    if not body:
        raise SystemExit("[错误] 无法将空 loop_body 物化到源码副本")

    lines = source_text.splitlines()
    start_line = loop_info["start_line"]
    end_line = loop_info["end_line"]
    if end_line > len(lines):
        raise SystemExit("[错误] 请求中的循环行号超出源文件范围")

    original_indent = re.match(r"\s*", lines[start_line - 1]).group(0)
    replacement = indent_block(body, original_indent).splitlines()
    materialized_lines = lines[: start_line - 1] + replacement + lines[end_line:]
    materialized = "\n".join(materialized_lines)
    if source_text.endswith("\n"):
        materialized += "\n"
    return materialized


def validate_materialized_text(materialized: str, target_function: str) -> None:
    if target_function not in materialized:
        raise SystemExit("[错误] 物化结果中缺少目标函数名")
    if materialized.count("{") != materialized.count("}"):
        raise SystemExit("[错误] 物化结果的大括号不平衡，无法形成可编译函数")


def infer_replacement_kind(result: dict[str, Any], vectorized_code: str, target_function: str) -> str:
    replacement_kind = result.get("replacement_kind")
    if replacement_kind:
        return str(replacement_kind)
    if looks_like_full_function(vectorized_code, target_function):
        return "translation_unit"
    return "function_body"


def validate_style_contract(result: dict[str, Any], selected_style: str) -> None:
    """Validate optional response fields against the selected code-generation style."""

    result_style = result.get("codegen_style")
    if result_style is not None and result_style != selected_style:
        raise SystemExit(
            f"[错误] response codegen_style={result_style} 与 request/source 推导结果 {selected_style} 不一致"
        )
    artifacts = result.get("artifacts", [])
    has_assembly_artifact = any(
        artifact["language"] in {"asm", "assembly"}
        or Path(artifact["path_suffix"]).suffix.lower() in ASSEMBLY_SUFFIXES
        for artifact in artifacts
    )
    if selected_style == "assembly" and not has_assembly_artifact:
        raise SystemExit("[错误] assembly codegen_style 必须在 artifacts 中提供 .S/.s/.asm 产物")


def materialize_artifacts(output_source: Path, artifacts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Write optional generated artifacts next to the main output source."""

    written: list[dict[str, str]] = []
    output_dir = output_source.parent.resolve()
    for artifact in artifacts:
        artifact_path = (output_dir / artifact["path_suffix"]).resolve()
        if output_dir not in artifact_path.parents and artifact_path != output_dir:
            raise SystemExit(f"[错误] artifact 输出路径逃逸输出目录: {artifact_path}")
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        content = artifact["content"].rstrip() + "\n"
        artifact_path.write_text(content, encoding="utf-8")
        written.append(
            {
                "path": artifact_path.as_posix(),
                "language": artifact["language"],
                "role": artifact["role"],
            }
        )
    return written


def maybe_run_syntax_check(
    output_source: Path, compiler: str, materialized_text: str
) -> None:
    if "#include <arm_" in materialized_text:
        return
    try:
        completed = subprocess.run(
            [compiler, "-std=c11", "-fsyntax-only", str(output_source)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"[错误] 找不到语法检查编译器: {compiler}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown compile error"
        raise SystemExit(f"[错误] 物化结果无法形成可编译函数: {stderr}")


def main() -> int:
    args = parse_args()
    repo_root = infer_repo_root(Path(__file__), args.repo_root)
    request_path = Path(args.request_json).resolve()
    response_path = Path(args.response_json).resolve()
    output_source = Path(args.output_source).resolve()

    request_data = load_json(request_path)
    response_data = load_json(response_path)
    validate_request(request_data)
    result = validate_response(response_data)

    target_function = request_data["target_function"]
    source_path = resolve_source_path(request_data["loop_info"], repo_root)
    source_text = source_path.read_text(encoding="utf-8")
    total_lines = len(source_text.splitlines())
    if request_data["loop_info"]["end_line"] > total_lines:
        raise SystemExit("[错误] 请求中的循环行号超出源文件范围")
    selected_style = resolve_codegen_style(request_data, source_path, source_text)
    validate_style_contract(result, selected_style)
    vectorized_code = result["vectorized_code"].strip() + "\n"
    replacement_kind = infer_replacement_kind(result, vectorized_code, target_function)
    application_mode = result.get("application_mode", "materialize_to_generate")

    if replacement_kind in {"full_function", "translation_unit"}:
        if not looks_like_full_function(vectorized_code, target_function):
            raise SystemExit(
                f"[错误] replacement_kind={replacement_kind} 要求 vectorized_code 包含完整目标函数 "
                f"'{target_function}'"
            )
        materialized_text = vectorized_code
        mode = replacement_kind
    elif replacement_kind == "function_body":
        if looks_like_full_function(vectorized_code, target_function):
            raise SystemExit("[错误] replacement_kind=function_body 不能提供完整函数定义")
        if "#include" in vectorized_code:
            raise SystemExit("[错误] function_body 片段不能包含 #include")
        materialized_text = materialize_from_body(source_text, target_function, vectorized_code)
        mode = "wrapped_function_body"
    elif replacement_kind == "loop_body":
        if looks_like_full_function(vectorized_code, target_function):
            raise SystemExit("[错误] replacement_kind=loop_body 不能提供完整函数定义")
        if "#include" in vectorized_code:
            raise SystemExit("[错误] loop_body 片段不能包含 #include")
        materialized_text = materialize_from_loop(source_text, request_data["loop_info"], vectorized_code)
        mode = "wrapped_loop_body"
    else:
        raise SystemExit(f"[错误] 不支持的 replacement_kind: {replacement_kind}")

    validate_materialized_text(materialized_text, target_function)
    output_source.parent.mkdir(parents=True, exist_ok=True)
    output_source.write_text(materialized_text, encoding="utf-8")
    artifact_outputs = materialize_artifacts(output_source, result.get("artifacts", []))

    if args.syntax_check:
        maybe_run_syntax_check(output_source, args.syntax_compiler, materialized_text)

    summary = {
        "validated": True,
        "materialization_mode": mode,
        "output_source": output_source.as_posix(),
        "request_json": request_path.as_posix(),
        "response_json": response_path.as_posix(),
        "target_function": target_function,
        "codegen_style": selected_style,
        "replacement_kind": replacement_kind,
        "application_mode": application_mode,
        "outputs": [
            {
                "path": output_source.as_posix(),
                "language": "c",
                "role": "primary_translation_unit",
            },
            *artifact_outputs,
        ],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
