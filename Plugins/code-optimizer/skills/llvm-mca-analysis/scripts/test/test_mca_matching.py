#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llvm-mca-analysis Rust 函数匹配回归测试（纯 assert 脚本，无 pytest 依赖）。

对应 PR #95 review 意见④（P2）修复的行为面回归：裸名支持带两道守卫——
(i) 仅 rust 语言路径启用（--source-language rust；c_cpp 裸名快路径行为完全不变）；
(ii) 唯一命中才过（同一裸名命中多个标签时报错列出全部）。
同时回归既有形态：路径名/全路径/mangled 前缀/calc::sum 正控制/objdump 形态。

本机无 llvm-mca 时 CLI 在 preflight 即退出，此处直接调用 --source/--binary
模式实际执行的匹配函数（mca_analyze.py 的 match_rust_label /
extract_function_from_compiler_asm / extract_function_from_objdump，
get_assembly 已把 source_language 传入 extract_*）。
"""
import sys
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import mca_analyze as mca  # noqa: E402

FIXTURE = TEST_DIR / "fixtures" / "synth2.s"
LABEL2 = "_ZN6kernel7example7dot_f3217h0123456789abcdefE"
ERR_CCPP_MISS = "在编译产物中找不到函数标签 'dot_f32:'"
ERR_OBJDUMP_MISS = "在反汇编结果中找不到函数 '<dot_f32>:'"

failed = []


def run_group(fn):
    print("== %s ==" % fn.__name__)
    try:
        fn()
        print("  [PASS] %s" % fn.__name__)
    except AssertionError as exc:
        print("  [FAIL] %s: %s" % (fn.__name__, exc))
        failed.append(fn.__name__)


def read_fixture():
    return FIXTURE.read_text(encoding="utf-8")


def test_guard_rust_only():
    # 守卫(i)：c_cpp 裸名快路径行为完全不变（含缺省语言参数，T-M2 修复前形态）
    asm = read_fixture()
    body, err = mca.extract_function_from_compiler_asm(asm, "dot_f32")
    assert err == ERR_CCPP_MISS and body is None, (err, body)
    body, err = mca.extract_function_from_compiler_asm(asm, "dot_f32", "c_cpp")
    assert err == ERR_CCPP_MISS and body is None, (err, body)
    # rust 裸名：唯一命中即通过（修复后新行为）
    body, err = mca.extract_function_from_compiler_asm(asm, "dot_f32", "rust")
    assert err == "", err
    assert body is not None
    instrs = body.splitlines()
    assert len(instrs) == 4
    assert instrs[0].strip() == "fmul s0, s1, s2"


def test_guard_unique_hit():
    # 守卫(ii)：同一裸名命中多个标签 -> 报错列出全部，不给首命中
    asm_dup = "\n".join([
        "\t.text",
        "_ZN6kernel7example7dot_f3217h0123456789abcdefE:",
        "\tfmul s0, s1, s2",
        "\tret",
        "\t.cfi_endproc",
        "_ZN6kernel6helper7dot_f3217h1111111111111111E:",
        "\tfmul s2, s3, s4",
        "\tret",
        "\t.cfi_endproc",
    ])
    body, err = mca.extract_function_from_compiler_asm(asm_dup, "dot_f32", "rust")
    assert body is None
    assert "命中 2 个标签" in err, err
    assert "_ZN6kernel7example7dot_f3217h0123456789abcdefE" in err
    assert "_ZN6kernel6helper7dot_f3217h1111111111111111E" in err
    # 路径形态不受唯一性守卫影响：首命中即止（原行为）
    body, err = mca.extract_function_from_compiler_asm(asm_dup, "example::dot_f32")
    assert err == "" and "fmul s0" in body


def test_bare_name_objdump_form():
    # --binary 模式（extract_function_from_objdump）同样两道守卫
    disasm = "\n".join([
        "0000000000000000 <%s>:" % LABEL2,
        "       0:\tfmul s0, s1, s2",
        "       4:\tret",
        "",
        "0000000000000010 <other_sym>:",
        "       0:\tret",
    ])
    # c_cpp 裸名快路径不变（T-M5 修复前形态）
    body, err = mca.extract_function_from_objdump(disasm, "dot_f32")
    assert err == ERR_OBJDUMP_MISS and body is None, (err, body)
    # rust 裸名唯一命中
    body, err = mca.extract_function_from_objdump(disasm, "dot_f32", "rust")
    assert err == "" and body is not None and "fmul s0" in body
    # rust 裸名多命中报错
    disasm2 = disasm + "\n0000000000000020 <_ZN6kernel6helper7dot_f3217h1111111111111111E>:\n       0:\tret\n"
    body, err = mca.extract_function_from_objdump(disasm2, "dot_f32", "rust")
    assert body is None and "命中 2 个符号" in err, err
    # 路径形态首命中（原行为）
    body, err = mca.extract_function_from_objdump(disasm, "example::dot_f32")
    assert err == "" and "fmul s0" in body


def test_path_forms_unchanged():
    # T-M3/T-M4：路径/全路径/mangled 前缀形态原本即可命中（语言缺省也走 rust 匹配路径）
    asm = read_fixture()
    for func in ("example::dot_f32", "kernel::example::dot_f32",
                 "_ZN6kernel7example7dot_f32"):
        body, err = mca.extract_function_from_compiler_asm(asm, func)
        assert err == "" and body is not None, (func, err)
    # c_cpp 语言 + 路径形态：原逻辑即走 rust 匹配路径，保持不变
    body, err = mca.extract_function_from_compiler_asm(asm, "example::dot_f32", "c_cpp")
    assert err == "" and body is not None


def test_calc_sum_positive_control():
    # T-M7 正控制：SKILL.md 声明的 calc::sum / _ZN4calc3sum 两种形态照旧成立
    assert mca.match_rust_label("_ZN4calc3sum17habcd0123456789efE", "calc::sum") is True
    assert mca.match_rust_label("_ZN4calc3sum17habcd0123456789efE", "_ZN4calc3sum") is True
    assert mca.match_rust_label("_ZN6kernel4calc3dot17habcd0123456789efE", "calc::dot") is True
    assert mca.match_rust_label("_ZN6kernel4calc3dot17habcd0123456789efE",
                                "kernel::calc::dot") is True
    assert mca.match_rust_label("_ZN4calc3sum17habcd0123456789efE", "calc::other") is False


def test_match_rust_label_bare_name_unit():
    # 模块级单元：裸名末段匹配（新增分支）
    assert mca.match_rust_label(LABEL2, "dot_f32") is True
    assert mca.match_rust_label("_ZN6kernel7example7other17h0123456789abcdefE",
                                "dot_f32") is False
    assert mca.match_rust_label("dot_f32", "dot_f32") is True   # 未 mangle 整串（原有）
    assert mca.match_rust_label(LABEL2, "fma_4x4") is False


def test_short_path_tail_alignment_regression():
    # T-M1 形态回归：mca 对短路径的尾部对齐原本即有，保持不变
    asm = "\n".join([
        "\t.text",
        "_ZN6kernel11microkernel7fma_4x417h0123456789abcdefE:",
        "\tstp x29, x30, [sp, #-16]!",
        "\tfmla v0, v0, v1",
        "\tret",
        "\t.cfi_endproc",
    ])
    assert mca.match_rust_label("_ZN6kernel11microkernel7fma_4x417h0123456789abcdefE",
                                "microkernel::fma_4x4") is True
    body, err = mca.extract_function_from_compiler_asm(asm, "microkernel::fma_4x4")
    assert err == "" and "fmla" in body
    # 裸名 + rust 同样命中（与 spill 修复后语义对称）
    body, err = mca.extract_function_from_compiler_asm(asm, "fma_4x4", "rust")
    assert err == "" and "fmla" in body
    # 裸名 + c_cpp 快路径不命中（守卫(i)）
    body, err = mca.extract_function_from_compiler_asm(asm, "fma_4x4")
    assert err == "在编译产物中找不到函数标签 'fma_4x4:'" and body is None


def test_c_cpp_fast_path_unmangled():
    # T-M8：未 mangle 标签下 c_cpp 快路径行为不变
    plain = "dot_f32:\n\tfmul s0, s1, s2\n\tret\n\t.cfi_endproc\n"
    body, err = mca.extract_function_from_compiler_asm(plain, "dot_f32")
    assert err == "" and body is not None
    # rust 裸名对未 mangle 标签整串命中且唯一（#[no_mangle] 场景）
    body, err = mca.extract_function_from_compiler_asm(plain, "dot_f32", "rust")
    assert err == "" and body is not None


def test_rustc_command_flags():
    # D2 回归：compile_rust_source_to_asm 构造的 rustc 命令必须带 --edition 2021
    # （`use core::arch::aarch64::*` 在默认 2015 edition 报 E0433）；
    # 库形态（无 fn main）追加 --crate-type=lib，bin 形态不追加。
    # 本组不真跑 rustc：拦截 run_cmd 记录命令并伪造成功产物，离线可跑。
    import shutil as _shutil
    if not _shutil.which("rustc"):
        print("  [SKIP] rustc 不可用，跳过命令形态断言")
        return
    import tempfile as _tempfile
    recorded = []

    def fake_run_cmd(cmd, input_text=None):
        recorded.append(list(cmd))
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\tdummy:\n\tret\n")
        return 0, "", ""

    real_run_cmd = mca.run_cmd
    mca.run_cmd = fake_run_cmd
    try:
        with _tempfile.TemporaryDirectory() as tmp:
            lib_src = Path(tmp) / "lib_form.rs"
            lib_src.write_text("pub mod m { use core::arch::aarch64::*; "
                               "pub fn f(a: f32) -> f32 { unsafe { vaddv_f32(vdupq_n_f32(a)) } } }\n",
                               encoding="utf-8")
            asm, ok, err = mca.compile_rust_source_to_asm(str(lib_src), "tsv110", [])
            assert ok is True, err
            cmd = recorded[-1]
            assert cmd[cmd.index("--edition") + 1] == "2021", cmd
            assert "--crate-type=lib" in cmd, cmd
            bin_src = Path(tmp) / "bin_form.rs"
            bin_src.write_text("fn main() { println!(\"hi\"); }\n", encoding="utf-8")
            asm, ok, err = mca.compile_rust_source_to_asm(str(bin_src), "tsv110", [])
            assert ok is True, err
            cmd = recorded[-1]
            assert cmd[cmd.index("--edition") + 1] == "2021", cmd
            assert "--crate-type=lib" not in cmd, cmd
    finally:
        mca.run_cmd = real_run_cmd


def main():
    groups = [
        test_guard_rust_only,
        test_guard_unique_hit,
        test_bare_name_objdump_form,
        test_path_forms_unchanged,
        test_calc_sum_positive_control,
        test_match_rust_label_bare_name_unit,
        test_short_path_tail_alignment_regression,
        test_c_cpp_fast_path_unmangled,
        test_rustc_command_flags,
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
