#!/usr/bin/env bash
# verify.sh —— agent-build-agentenv 的产物判定
#
# 判定 agent 是否完成了 AgentENV 的获取、构建环境准备与 release 编译：
#   1) opt/build_report.json 存在且为合法 JSON（agent 自述产物）
#   2) repo_present：report.repo_dir 存在且含 Cargo.toml
#   3) report 必填字段：build_command 与 cargo_version 非空
#   4) build_ok：三个 release 二进制（server / uvm-ublk / uvm-ublk-daemon）
#      全部位于 repo_dir/target/release/ 内（realpath 解析，拒绝越界路径与
#      符号链接逃逸）、为 ELF 且可执行、非空。**不接受自述路径作为判据**——
#      声明 /bin/true 等系统可执行文件无法通过（评审 P2）。
#
# 输出：results/result.json（success / skill_mode / repo_present / build_ok /
#       binaries_found / cargo/rustc 版本 / build_command / elapsed_seconds / reason）
# 退出码：0 表示全部成立，非 0 表示未成立（供 case.yaml 的 exit_code 断言使用）。
# 说明：WORKSPACE_DIR 默认 /workspace（容器契约不变）；evals/ 在容器外用该变量
#       覆盖工作目录以复用同一判定逻辑做回归测试。
set -uo pipefail

cd "${WORKSPACE_DIR:-/workspace}"
mkdir -p results

SKILL_MODE="${SKILL_MODE:-with}"
export SKILL_MODE

python3 - <<'PY'
import json, os

skill_mode = os.environ.get("SKILL_MODE", "with")

# elapsed（entrypoint 记录，可选）
elapsed = None
try:
    for line in open("results/elapsed.txt"):
        parts = line.split()
        if len(parts) == 2 and parts[0] == "build_elapsed":
            elapsed = int(parts[1])
except Exception:
    pass

def write(reason, success, repo_present, build_ok, binaries_found, extra=None):
    d = {"success": success, "reason": reason, "skill_mode": skill_mode,
         "repo_present": repo_present, "build_ok": build_ok,
         "binaries_found": binaries_found, "elapsed_seconds": elapsed}
    if extra:
        d.update(extra)
    json.dump(d, open("results/result.json", "w"), ensure_ascii=False, indent=2)
    print("verify:", "PASS" if success else "FAIL", "reason=" + reason)
    raise SystemExit(0 if success else 1)

try:
    report = json.load(open("opt/build_report.json"))
except Exception:
    write("opt/build_report.json missing or invalid JSON", False, 0, 0, 0)

repo_dir = report.get("repo_dir") or ""
repo_present = 1 if repo_dir and os.path.isdir(repo_dir) \
    and os.path.isfile(os.path.join(repo_dir, "Cargo.toml")) else 0

build_command = report.get("build_command") or ""
cargo_version = report.get("cargo_version") or ""
report_fields_ok = 1 if (build_command and cargo_version) else 0

ELF_MAGIC = b"\x7fELF"
fallback = {"server": "server", "uvm_ublk": "uvm-ublk", "uvm_ublk_daemon": "uvm-ublk-daemon"}
target_root = os.path.realpath(os.path.join(repo_dir, "target", "release")) \
    if repo_present else ""

def artifact_ok(expected_name):
    """release 产物绑定判定：必须位于 repo_dir/target/release/ 内、ELF 且可执行。"""
    if not target_root:
        return None
    real = os.path.realpath(os.path.join(target_root, expected_name))
    if real != target_root and not real.startswith(target_root + os.sep):
        return None                                   # 越出构建目录 / 符号链接逃逸
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        return None
    if os.path.getsize(real) <= 0:
        return None
    try:
        with open(real, "rb") as f:
            if f.read(4) != ELF_MAGIC:
                return None                           # 非 ELF（如 shell 脚本）不通过
    except OSError:
        return None
    return real

found, missing, resolved = [], [], {}
if repo_present:
    for key, expected_name in fallback.items():
        path = artifact_ok(expected_name)
        if path:
            found.append(key)
            resolved[key] = path
        else:
            missing.append(key)
else:
    missing = list(fallback)

build_ok = 1 if repo_present and report_fields_ok and not missing else 0
success = bool(build_ok)
reason = "ok" if success else "; ".join(filter(None, [
    "repo_absent" if not repo_present else "",
    "report_fields_missing" if not report_fields_ok else "",
    ("binaries_missing:" + "+".join(missing)) if missing else ""]))

write(reason, success, repo_present, build_ok, len(found), {
    "binaries": resolved,
    "binaries_declared": report.get("binaries") or {},
    "cargo_version": cargo_version,
    "rustc_version": report.get("rustc_version", ""),
    "build_command": build_command,
})
PY
