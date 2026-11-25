#!/usr/bin/env python3
"""Patch llvm-mca to add the -issue-trace view (real per-iteration issue data).

Usage:
  python3 patch_llvm_mca.py [LLVM_SOURCE_ROOT]   # default: current directory

It expects IssueTraceView.{h,cpp} to be copied into
<llvm-root>/llvm/tools/llvm-mca/Views/ first.
"""
import os
import re
import sys

root = sys.argv[1] if len(sys.argv) > 1 else "."
base = os.path.join(root, "llvm", "tools", "llvm-mca")
p = os.path.join(base, "llvm-mca.cpp")
s = open(p, encoding="utf-8").read()

inc_anchor = '#include "Views/ResourcePressureView.h"'
if "IssueTraceView.h" not in s:
    if inc_anchor not in s:
        sys.exit("ERROR: include anchor not found")
    s = s.replace(inc_anchor, '#include "Views/IssueTraceView.h"\n' + inc_anchor, 1)

flag_re = re.compile(r"(static cl::opt<bool> PrintTimelineView\(.*?\);)",
                     re.S)
if "PrintIssueTrace" not in s:
    m = flag_re.search(s)
    if not m:
        sys.exit("ERROR: flag anchor not found")
    flag_new = m.group(1) + """

static cl::opt<bool> PrintIssueTrace(
    "issue-trace",
    cl::desc("Print per-instruction issued resource trace"),
    cl::cat(ViewOptions), cl::init(false));"""
    s = s[: m.start(1)] + flag_new + s[m.end(1):]

reg_re = re.compile(
    r"(if \(PrintResourcePressureView\)\s*Printer\.addView\(\s*"
    r"std::make_unique<mca::ResourcePressureView>\(\*STI, \*IP, Insts\)\);)",
    re.S,
)
if "IssueTraceView>(*STI" not in s:
    m = reg_re.search(s)
    if not m:
        sys.exit("ERROR: registration anchor not found")
    reg_new = m.group(1) + """

    if (PrintIssueTrace)
      Printer.addView(std::make_unique<mca::IssueTraceView>(*STI, Insts));"""
    s = s[: m.start(1)] + reg_new + s[m.end(1):]

open(p, "w", encoding="utf-8").write(s)

p2 = os.path.join(base, "CMakeLists.txt")
c = open(p2, encoding="utf-8").read()
if "IssueTraceView.cpp" not in c:
    anchor = "  Views/InstructionView.cpp\n"
    if anchor not in c:
        sys.exit("ERROR: cmake anchor not found")
    c = c.replace(anchor, anchor + "  Views/IssueTraceView.cpp\n", 1)
open(p2, "w", encoding="utf-8").write(c)

print("patched ok")
