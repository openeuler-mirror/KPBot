#!/usr/bin/env bash
# verify.sh —— agent-build-agentenv 的产物判定
#
# 判定 agent 是否完成了 AgentENV 的获取、构建环境准备与 release 编译：
#   1) opt/build_report.json 存在且为合法 JSON（agent 自述产物）
#   2) repo_present：report.repo_dir 存在且含 Cargo.toml
#   3) build_ok：三个 release 二进制（server / uvm_ublk / uvm_ublk_daemon）
#      均存在、非空且可执行；以 report.binaries 声明路径为准，
#      缺失时回退到 repo_dir/target/release/ 下按名探测
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

declared = report.get("binaries") or {}
fallback = {"server": "server", "uvm_ublk": "uvm-ublk", "uvm_ublk_daemon": "uvm-ublk-daemon"}
found, missing, resolved = [], [], {}
if repo_present:
    target_dir = os.path.join(repo_dir, "target", "release")
    for key, default_name in fallback.items():
        path = declared.get(key)
        if not path or not os.path.isfile(path):
            path = os.path.join(target_dir, default_name)
        if path and os.path.isfile(path) and os.access(path, os.X_OK) \
                and os.path.getsize(path) > 0:
            found.append(key)
            resolved[key] = path
        else:
            missing.append(key)
else:
    missing = list(fallback)

build_ok = 1 if repo_present and not missing else 0
success = bool(repo_present and build_ok)
reason = "ok" if success else "; ".join(filter(None, [
    "repo_absent" if not repo_present else "",
    ("binaries_missing:" + "+".join(missing)) if missing else ""]))

write(reason, success, repo_present, build_ok, len(found), {
    "binaries": resolved,
    "cargo_version": report.get("cargo_version", ""),
    "rustc_version": report.get("rustc_version", ""),
    "build_command": report.get("build_command", ""),
})
PY
