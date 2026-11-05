#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""register-pressure-analysis 标签匹配回归测试（纯 assert 脚本，无 pytest 依赖）。

对应 PR #95 review 意见①（P1）修复的行为面回归：
- 段级尾部对齐：短路径（缺 crate 段）现在能命中真实 mangled 标签（E1 形态）；
- 全路径 / mangled 前缀 / C 标签匹配行为保持不变（E2/E3/C 形态）；
- 裸名对 mangled 标签照旧不命中（E4 形态，裸名支持在 mca 侧不在 spill 侧）；
- 同短路径多候选 first-match（首命中即止）；
- 匹配失败时错误信息列出候选标签（上限 10 + 计数）。

对应 PR #95 review round 2 修复（language/source_kind 产物推导）：
- derive_language_and_source_kind：rust 汇编→rust/rust_intrinsics、c 汇编→
  c_cpp/assembly、含 # APP 包裹段→rust_asm；
- CLI --language/--source-kind 期望值语义：与推导矛盾即报错退出（fail-closed，
  报错含 expected/derived 两值）；缺省时推导值填输出字段（语言/来源不再自报）。

fixture fixtures/synth.s 与远端验证用合成汇编同源
（标签 _ZN6kernel11microkernel7fma_4x417h0123456789abcdefE）。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import analyze_assembly_spill as spill  # noqa: E402

FIXTURE = TEST_DIR / "fixtures" / "synth.s"
LABEL = "_ZN6kernel11microkernel7fma_4x417h0123456789abcdefE"
SAFE_LABEL = "_ZN6kernel11microkernel12fma_4x4_safe17hfedcba9876543210E"

failed = []


def run_group(fn):
    print("== %s ==" % fn.__name__)
    try:
        fn()
        print("  [PASS] %s" % fn.__name__)
    except AssertionError as exc:
        print("  [FAIL] %s: %s" % (fn.__name__, exc))
        failed.append(fn.__name__)


def test_demangle_helpers():
    # 自包含复制的 mca 语义（两技能脚本互不 import）
    assert spill.demangle_legacy_segments("_ZN6kernel11microkernel7fma_4x4") == \
        ["kernel", "microkernel", "fma_4x4"]
    assert spill.demangle_legacy_segments(LABEL) == ["kernel", "microkernel", "fma_4x4"]
    assert spill.demangle_legacy_segments("_ZN4calc3sum") == ["calc", "sum"]
    assert spill.demangle_legacy_segments("plain_c_fn") == []            # 非 _ZN（C 标签）
    assert spill.demangle_legacy_segments("gemm_kernel:") == []
    assert spill.demangle_legacy_segments("_ZN6kernel11microk") == []    # 段长损坏
    assert spill.strip_rust_hash(LABEL) == "_ZN6kernel11microkernel7fma_4x4"
    assert spill.strip_rust_hash("plain_c_fn") == "plain_c_fn"


def test_short_path_tail_alignment():
    # E1 形态：短路径（缺 crate 段）经段级尾部对齐命中——本修复核心
    assert spill.rust_label_matches(LABEL, "microkernel::fma_4x4") is True
    assert spill.rust_label_matches(LABEL + ":", "microkernel::fma_4x4") is True
    # T-M1 形态正控制：calc::dot vs _ZN6kernel4calc3dot（含真实 hash 形状）
    assert spill.rust_label_matches(
        "_ZN6kernel4calc3dot17habcd0123456789efE", "calc::dot") is True
    # 尾段不同则不命中
    assert spill.rust_label_matches(LABEL, "microkernel::fma_4x4_safe") is False


def test_full_path_and_mangled_prefix_unchanged():
    # E2/E3 形态：全路径与 mangled 前缀原本即可命中，保持不变
    assert spill.rust_label_matches(LABEL, "kernel::microkernel::fma_4x4") is True
    assert spill.rust_label_matches(LABEL, "_ZN6kernel11microkernel7fma_4x4") is True
    assert spill.rust_label_matches(LABEL, "_ZN11microkernel7fma_4x4") is True  # 短 mangled 前缀
    assert spill.rust_label_matches(SAFE_LABEL, "kernel::microkernel::fma_4x4_safe") is True
    assert spill.rust_label_matches("_ZN4calc3sum17habcd0123456789efE", "_ZN4calc3sum") is True


def test_bare_name_behavior_unchanged():
    # E4 形态：裸名对 mangled 标签不命中（spill 保持原语义，不引入 mca 裸名支持）
    assert spill.rust_label_matches(LABEL, "fma_4x4") is False
    # 裸名对未 mangle 标签照旧整串命中
    assert spill.rust_label_matches("fma_4x4:", "fma_4x4") is True
    assert spill.rust_label_matches("fma_4x4", "fma_4x4") is True


def test_c_label_path_unchanged():
    # C 标签路径零变化：裸名/整串逻辑照旧，尾部对齐对非 _ZN 标签不生效
    assert spill.label_matches("gemm_kernel:", "gemm_kernel") is True
    assert spill.label_matches("gemm_kernel: // @gemm_kernel", "gemm_kernel") is True
    assert spill.label_matches("_gemm_kernel:", "gemm_kernel") is True
    assert spill.label_matches("other_fn:", "gemm_kernel") is False
    assert spill.label_matches(".LBB0_0:", "gemm_kernel") is False
    assert spill.label_matches("stp x29, x30, [sp, #-16]!", "gemm_kernel") is False
    # C 函数名与 mangled 标签互不误配
    assert spill.rust_label_matches(LABEL, "gemm_kernel") is False


def test_analyze_short_path_end_to_end():
    # E1→E2 等价：短路径与全路径/mangled 在 fixture 上产出同一统计（修复后 success）
    short = spill.analyze_assembly(FIXTURE, "microkernel::fma_4x4", "rust_intrinsics", 20, "rust")
    full = spill.analyze_assembly(FIXTURE, "kernel::microkernel::fma_4x4", "rust_intrinsics", 20, "rust")
    mangled = spill.analyze_assembly(FIXTURE, "_ZN6kernel11microkernel7fma_4x4", "rust_intrinsics", 20, "rust")
    assert short.success is True, "short path should succeed after tail-alignment fix"
    for r in (short, full, mangled):
        assert (r.spill_store_count, r.spill_reload_count, r.fma_count) == (1, 1, 4)
        assert r.spill_per_fma == 0.5
        assert r.pressure_level == "severe"
    # 函数边界正确截断：不吞下一个全局标签（fma_4x4_safe）的指令
    assert short.evidence[0].line == 5


def test_multi_candidate_first_match():
    # 同短路径多候选：首命中即止（已拍板 first-match 语义，与 mca 行为一致）
    asm = "\n".join([
        ".text",
        "_ZN6kernel3mka11microkernel7fma_4x417h1111111111111111E:",
        "\tstp x29, x30, [sp, #-16]!",
        "\tfmla v0, v0, v1",
        "\tret",
        "_ZN6kernel3mkb11microkernel7fma_4x417h2222222222222222E:",
        "\tstp x29, x30, [sp, #-16]!",
        "\tfmla v2, v2, v3",
        "\tret",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        asm_path = Path(tmp) / "multi.s"
        asm_path.write_text(asm + "\n", encoding="utf-8")
        r = spill.analyze_assembly(asm_path, "microkernel::fma_4x4", "rust_intrinsics", 20, "rust")
        assert r.success is True, "multi-candidate short path should hit first match"
        # 只统计首命中（mka 版本）的指令：store=1 reload=0 fma=1
        assert (r.spill_store_count, r.spill_reload_count, r.fma_count) == (1, 0, 1), \
            (r.spill_store_count, r.spill_reload_count, r.fma_count)
        assert r.evidence[0].line == 3, r.evidence[0]   # 首函数的 stp 行（标签行 2 的下一条）
        # 全路径仍可精确锁定第二个候选
        r2 = spill.analyze_assembly(asm_path, "kernel::mkb::microkernel::fma_4x4",
                                    "rust_intrinsics", 20, "rust")
        assert r2.success is True, "full path should pin the second candidate"
        assert (r2.spill_store_count, r2.fma_count) == (1, 1), \
            (r2.spill_store_count, r2.fma_count)
        assert r2.evidence[0].line == 7, r2.evidence[0]  # 第二函数的 stp 行（标签行 6 的下一条）


def test_match_failure_lists_candidates():
    # 匹配失败：错误信息列出候选标签（reviewer 建议，便于 agent 自纠）
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    selected, err = spill.select_lines(lines, "nonexistent::fn")
    assert selected == []
    assert err is not None
    assert err.startswith("function label not found: nonexistent::fn")
    assert "available labels:" in err
    assert LABEL in err                       # 候选标签带 hash 原样列出
    assert SAFE_LABEL in err
    # 上限 10 + 计数：12 个标签只列前 10 个，附 (+2 more)
    many = [".text"] + ["lbl_%02d:" % i for i in range(12)]
    _, err2 = spill.select_lines(many, "nonexistent::fn")
    assert "(+2 more)" in err2
    assert "lbl_00" in err2 and "lbl_09" in err2
    assert "lbl_10" not in err2 and "lbl_11" not in err2
    # 局部标签（.L 前缀）不进候选列表
    _, err3 = spill.select_lines(["foo:", ".LBB0_0:", "bar:"], "missing::fn")
    assert "foo" in err3 and "bar" in err3 and ".LBB0_0" not in err3


def test_c_analysis_unchanged():
    # C 标签分析路径零变化：命中照旧 success，未命中照旧失败（exit 语义不变）
    with tempfile.TemporaryDirectory() as tmp:
        c_asm = Path(tmp) / "c.s"
        c_asm.write_text("\t.text\nc_kernel:\n\tstp x29, x30, [sp, #-16]!\n"
                         "\tldr x9, [sp, #8]\n\tret\n", encoding="utf-8")
        r = spill.analyze_assembly(c_asm, "c_kernel", "c_intrinsics", 20, "c_cpp")
        assert r.success is True
        assert (r.spill_store_count, r.spill_reload_count, r.fma_count) == (1, 1, 0)
        assert r.pressure_level == "high"
        r2 = spill.analyze_assembly(c_asm, "c_missing", "c_intrinsics", 20, "c_cpp")
        assert r2.success is False
        # 核心错误前缀保持原样，仅追加候选列表
        assert r2.error_message.startswith("function label not found: c_missing")
        assert "available labels: [c_kernel]" in r2.error_message


def test_derive_language_and_source_kind():
    # 产物推导（PR #95 review-2 修复）：language/source_kind 从汇编文本推导，不再取自报
    rust_lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert spill.derive_language_and_source_kind(rust_lines) == ("rust", "rust_intrinsics")
    c_lines = (TEST_DIR / "fixtures" / "c_plain.s").read_text(encoding="utf-8").splitlines()
    assert spill.derive_language_and_source_kind(c_lines) == ("c_cpp", "assembly")
    asm_lines = (TEST_DIR / "fixtures" / "rust_inline_asm.s").read_text(encoding="utf-8").splitlines()
    assert spill.derive_language_and_source_kind(asm_lines) == ("rust", "rust_asm")
    # # APP 无 # NO_APP 不构成包裹段（半开不算 rust_asm）
    half_open = [line for line in asm_lines if "# NO_APP" not in line]
    assert spill.derive_language_and_source_kind(half_open) == ("rust", "rust_intrinsics")
    # _ZN 前缀但无 17h<16 位十六进制 hash 段（C++ Itanium 嵌套名形态）不算 rust 符号
    assert spill.derive_language_and_source_kind(["_ZN4calc3sumEi:"]) == ("c_cpp", "assembly")
    # hash 必须恰为 16 位十六进制（15 位不命中）
    assert spill.derive_language_and_source_kind(["_ZN4calc3sum17habcd01234567890E:"]) == \
        ("c_cpp", "assembly")
    assert spill.derive_language_and_source_kind(["_ZN4calc3sum17habcd0123456789efE:"]) == \
        ("rust", "rust_intrinsics")
    # C 的 asm() 内联块同样带 # APP/# NO_APP 标记，但无 rust 符号 → C 家族 assembly
    c_with_app = list(c_lines[:2]) + ["\t# APP", "\tfadd v0.4s, v0.4s, v1.4s", "\t# NO_APP"] + list(c_lines[2:])
    assert spill.derive_language_and_source_kind(c_with_app) == ("c_cpp", "assembly")


def _run_spill_cli(*extra):
    cmd = [sys.executable, str(SCRIPTS_DIR / "analyze_assembly_spill.py"),
           "--asm", str(FIXTURE)] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def test_cli_language_expectation_fail_closed():
    # CLI 期望值与推导矛盾 → 报错退出（fail-closed，报错含 expected 与 derived 两值）
    p = _run_spill_cli("--language", "c_cpp")
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    result = json.loads(p.stdout)["register_pressure_result"]
    assert result["success"] is False
    assert "expected=c_cpp" in result["error_message"], result["error_message"]
    assert "derived=rust" in result["error_message"], result["error_message"]
    # --source-kind 同理（旧自报值 assembly 与推导 rust_intrinsics 矛盾即拒）
    p = _run_spill_cli("--source-kind", "assembly")
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    result = json.loads(p.stdout)["register_pressure_result"]
    assert result["success"] is False
    assert "expected=assembly" in result["error_message"], result["error_message"]
    assert "derived=rust_intrinsics" in result["error_message"], result["error_message"]


def test_cli_derived_values_fill_output():
    # CLI 缺省 → 推导值入输出字段；期望与推导一致 → cli_verified 溯源
    p = _run_spill_cli()
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    result = json.loads(p.stdout)["register_pressure_result"]
    assert result["language"] == "rust"
    assert result["source_kind"] == "rust_intrinsics"
    assert result["language_source"] == "derived"
    assert result["source_kind_source"] == "derived"
    # 提供且与推导一致：通过校验并标注 cli_verified
    p = _run_spill_cli("--language", "rust", "--source-kind", "rust_intrinsics")
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    result = json.loads(p.stdout)["register_pressure_result"]
    assert result["language"] == "rust" and result["source_kind"] == "rust_intrinsics"
    assert result["language_source"] == "cli_verified"
    assert result["source_kind_source"] == "cli_verified"


def test_cli_c_side_source_kind_labels():
    # C 侧无文本特征标签（家族校验）：C 汇编上 c_intrinsics/inline_asm 通过并照记标签；
    # rust 汇编上提供即 fail-closed（derived=rust_intrinsics）
    c_asm = str(TEST_DIR / "fixtures" / "c_plain.s")
    def run_c(*extra):
        cmd = [sys.executable, str(SCRIPTS_DIR / "analyze_assembly_spill.py"),
               "--asm", c_asm] + list(extra)
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    p = run_c("--source-kind", "c_intrinsics")
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    r = json.loads(p.stdout)["register_pressure_result"]
    assert r["source_kind"] == "c_intrinsics", r
    assert r["source_kind_source"] == "cli_verified", r
    assert r["language"] == "c_cpp" and r["success"] is True, r
    p = run_c("--source-kind", "inline_asm")
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    r = json.loads(p.stdout)["register_pressure_result"]
    assert r["source_kind"] == "inline_asm" and r["source_kind_source"] == "cli_verified", r
    p = _run_spill_cli("--source-kind", "c_intrinsics")
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    r = json.loads(p.stdout)["register_pressure_result"]
    assert r["success"] is False
    assert "expected=c_intrinsics" in r["error_message"], r["error_message"]
    assert "derived=rust_intrinsics" in r["error_message"], r["error_message"]


def main():
    groups = [
        test_demangle_helpers,
        test_short_path_tail_alignment,
        test_full_path_and_mangled_prefix_unchanged,
        test_bare_name_behavior_unchanged,
        test_c_label_path_unchanged,
        test_analyze_short_path_end_to_end,
        test_multi_candidate_first_match,
        test_match_failure_lists_candidates,
        test_c_analysis_unchanged,
        test_derive_language_and_source_kind,
        test_cli_language_expectation_fail_closed,
        test_cli_derived_values_fill_output,
        test_cli_c_side_source_kind_labels,
    ]
    for group in groups:
        run_group(group)
    print("")
    print("总计: %d 组, 失败: %d" % (len(groups), len(failed)))
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
