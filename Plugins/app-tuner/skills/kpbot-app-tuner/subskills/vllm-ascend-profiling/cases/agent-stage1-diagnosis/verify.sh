#!/usr/bin/env bash
# 判定脚本：独立探测目标容器计算地面真值（不复用 skill 脚本逻辑），
# 与 agent 产物 opt/diagnosis.json 的 failed_checks 严格比对，写 result.json。
#
# 地面真值检查集（与 skill env_check 同名的 7 项，探测实现为本文件自建）：
#   docker_daemon / container_running / vllm_process / PYTHONMALLOC /
#   vllm_ttft_fork / vllm_ascend / vllm_config
# 覆盖：EXPECTED_FAILED_CHECKS=CHECK1,CHECK2 直接钉死真值（真机调试用）；
#       EXPECTED_VLLM_ASCEND_OK=true|false 覆盖 vllm_ascend 一项。
set -uo pipefail

cd /workspace
mkdir -p results

python3 - <<'PY'
import json, os, re, shlex, subprocess, sys

target = os.environ.get("TARGET_CONTAINER", "").strip()
expected_env = os.environ.get("EXPECTED_FAILED_CHECKS", "").strip()
ascend_override = os.environ.get("EXPECTED_VLLM_ASCEND_OK", "auto").strip()
skill_mode = os.environ.get("SKILL_MODE", "?")

def sh(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"

def dex(inner: str, timeout: float = 30) -> tuple[int, str]:
    return sh(["docker", "exec", target, "bash", "-c", inner], timeout=timeout)

def gt_checks() -> tuple[set, list]:
    gt: set = set()

    code, out = sh(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        return {"docker_daemon", "container_running", "vllm_process", "PYTHONMALLOC",
                "vllm_ttft_fork", "vllm_ascend", "vllm_config"}, ["docker_daemon unreachable"]

    code, out = sh(["docker", "inspect", "-f", "{{.State.Status}} {{.State.Running}}", target])
    running = code == 0 and "running true" in out
    if not running:
        gt |= {"container_running", "vllm_process", "PYTHONMALLOC", "vllm_ttft_fork", "vllm_ascend", "vllm_config"}
        return gt, [f"container state={out!r}"]

    # vllm 进程（进程判定，过滤噪声行）
    code, out = dex("ps -ef | grep vllm | grep -v grep")
    pids: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        parts = line.split(None, 7)
        if len(parts) >= 8 and parts[1].isdigit():
            pids.append({"pid": parts[1], "cmdline": parts[7] if len(parts) > 7 else line})
    if not pids:
        gt |= {"vllm_process", "PYTHONMALLOC", "vllm_ttft_fork", "vllm_ascend", "vllm_config"}
        return gt, ["no vllm process"]

    # PYTHONMALLOC
    ok = False
    for p in pids:
        code, out = dex(f"tr '\\0' '\\n' < /proc/{p['pid']}/environ | grep -E '^PYTHONMALLOC=' || true")
        val = next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith("PYTHONMALLOC=")), "")
        ok = ok or val == "malloc"
    if not ok:
        gt.add("PYTHONMALLOC")

    # vllm_ttft_fork：PYTHONPATH 里找到带特征码的 vllm checkout + pip vllm 版本前缀
    fork_ref = json.load(open("/staging/skill/references/vllm_fork.json", encoding="utf-8")) \
        if os.path.isfile("/staging/skill/references/vllm_fork.json") else {}
    markers = fork_ref.get("markers", {}) or {}
    prefix = fork_ref.get("expected_pip_version_prefix", "")
    fork_ok = False
    notes: list[str] = []
    for p in pids:
        code, out = dex(f"tr '\\0' '\\n' < /proc/{p['pid']}/environ | grep -E '^PYTHONPATH=' || true")
        pp = next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith("PYTHONPATH=")), "")
        for entry in [e for e in pp.split(":") if e.strip()]:
            code, out = dex(f"test -f {shlex.quote(entry.rstrip('/') + '/vllm/__init__.py')} && echo FOUND || true")
            if "FOUND" not in out:
                continue
            g = "; ".join(f"grep -qF -- {shlex.quote(m)} {shlex.quote(entry.rstrip('/') + '/' + rel)} && echo 'ok {rel}' || echo 'missing {rel}'"
                          for rel, m in markers.items())
            code, out = dex(g)
            missing = [l for l in out.splitlines() if l.startswith("missing")]
            if code == 0 and not missing:
                fork_ok = True
                break
        if fork_ok:
            break
    if prefix:
        code, out = dex("python3 -m pip show vllm 2>/dev/null || pip3 show vllm 2>/dev/null || true")
        ver = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("Version:")), "")
        pip_ok = bool(ver) and ver.startswith(prefix)
        notes.append(f"pip vllm={ver!r} prefix_ok={pip_ok}")
    else:
        pip_ok = True
    if not (fork_ok and pip_ok):
        gt.add("vllm_ttft_fork")

    # vllm_ascend：pip 版本子串 + 补丁特征码（anchored on vllm_ascend.json）
    asc_ref = json.load(open("/staging/skill/references/vllm_ascend.json", encoding="utf-8")) \
        if os.path.isfile("/staging/skill/references/vllm_ascend.json") else {}
    substr = asc_ref.get("expected_version_substr", "")
    asc_ok, asc_note = None, []
    if substr:
        code, out = dex("python3 -m pip show vllm-ascend 2>/dev/null || pip3 show vllm-ascend 2>/dev/null || true")
        ver = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("Version:")), "")
        loc = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("Location:")), "")
        version_ok = bool(ver) and substr in ver
        mark_ok = True
        if version_ok and loc:
            for rel, spec in (asc_ref.get("patch_files") or {}).items():
                tgt = loc + "/" + rel
                for m in spec.get("markers", []):
                    code, out = dex(f"grep -qF -- {shlex.quote(m)} {shlex.quote(tgt)} && echo ok || echo missing")
                    if any(l.strip() == "ok" for l in out.splitlines()) is False:
                        mark_ok = False
                    asc_note.append(f"{rel}::{m[:32]}")
        asc_ok = version_ok and mark_ok
        notes.append(f"vllm-ascend={ver or '<none>'} version_ok={version_ok} markers_ok={mark_ok}")
    if ascend_override in ("true", "false"):
        asc_ok = ascend_override == "true"
    elif asc_ok is None:
        asc_ok = True   # 参考缺失时不误报
    if not asc_ok:
        gt.add("vllm_ascend")

    # vllm_config：cmdline / cmdline 引用的配置文件要求 torch_profiler_with_stack=true
    combined = "\n".join(p["cmdline"] for p in pids)
    found: dict[str, str] = {}
    for key in ("torch_profiler_with_stack",):
        m = re.search(rf'"{key}"\s*:\s*("?[^",}}\s]+"?)', combined) or \
            re.search(rf"(?:^|\s){re.escape(key)}=(\"[^\"]+\"|\S+)", combined)
        if m:
            found[key] = m.group(1).strip().strip('"').lower()
    refs = set(re.findall(r"(/\S+\.(?:json|jsonc|yaml|yml))", combined))
    refs.update(re.findall(r"(?:--config|-c)\s+(\S+)", combined))
    for ref in sorted(refs):
        code, out = dex(f"cat {shlex.quote(ref)} 2>/dev/null || true")
        if out.strip():
            for key in ("torch_profiler_with_stack",):
                m = re.search(rf'"{key}"\s*:\s*("?[^",}}\s]+"?)', out) or \
                    re.search(rf"(?:^|\s){re.escape(key)}=(\"[^\"]+\"|\S+)", out)
                if m:
                    found.setdefault(key, m.group(1).strip().strip('"').lower())
    if found.get("torch_profiler_with_stack") != "true":
        gt.add("vllm_config")
    notes.append(f"config={found} refs={sorted(refs)}")
    return gt, notes

def main() -> int:
    if not target:
        json.dump({"success": False, "reason": "TARGET_CONTAINER unset", "skill_mode": skill_mode},
                  open("results/result.json", "w"), indent=2)
        return 2

    # agent 产物
    diag_path = "opt/diagnosis.json"
    if not os.path.isfile(diag_path):
        json.dump({"success": False, "reason": "opt/diagnosis.json missing", "skill_mode": skill_mode},
                  open("results/result.json", "w"), indent=2)
        return 1
    try:
        diag = json.load(open(diag_path, encoding="utf-8"))
    except Exception as e:
        json.dump({"success": False, "reason": f"opt/diagnosis.json not valid JSON: {e}", "skill_mode": skill_mode},
                  open("results/result.json", "w"), indent=2)
        return 1
    reported = [str(c).strip() for c in diag.get("failed_checks", []) if str(c).strip() and str(c).strip().lower() != "none"]
    diag_container = str(diag.get("container", "")).strip()

    # 地面真值
    if expected_env:
        gt = {c.strip() for c in expected_env.split(",") if c.strip()}
        notes = ["EXPECTED_FAILED_CHECKS override"]
    else:
        gt, notes = gt_checks()

    container_matched = diag_container == target
    diagnosis_correct = set(reported) == gt and container_matched

    reported_set = set(reported)
    if diagnosis_correct:
        reason = "ok"
    else:
        diffs = []
        if reported_set - gt:
            diffs.append("extra reported (check names agent invented): " + ",".join(sorted(reported_set - gt)))
        if gt - reported_set:
            diffs.append("missing reported (agent's diagnosis diverges from ground truth): " + ",".join(sorted(gt - reported_set)))
        if not container_matched:
            diffs.append(f"diagnosis 'container' field {diag_container!r} != target {target!r}")
        reason = "; ".join(diffs)

    result = {
        "success": diagnosis_correct,
        "reason": reason,
        "skill_mode": skill_mode,
        "container": target,
        "diagnosis_container": diag_container,
        "container_matched": container_matched,
        "diagnosis_correct": int(diagnosis_correct),
        "expected_failed_checks": sorted(gt),
        "reported_failed_checks": reported,
        "ground_truth_notes": notes,
    }
    json.dump(result, open("results/result.json", "w"), ensure_ascii=False, indent=2)
    return 0 if diagnosis_correct else 1

sys.exit(main())
PY
rc=$?
echo "[verify] rc=${rc}"
exit "$rc"
