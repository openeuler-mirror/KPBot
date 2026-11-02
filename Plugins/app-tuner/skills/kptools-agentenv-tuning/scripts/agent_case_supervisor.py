#!/usr/bin/env python3
"""Run the diagnostic case with a root-owned grader and an unprivileged agent."""

import json
import os
import pwd
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

WORKSPACE = Path("/workspace")
RESULTS = WORKSPACE / "results"
STAGING = Path("/staging/pkg")
MAX_OUTPUT = 16 * 1024 * 1024
MAX_REPORT = 1024 * 1024


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def fail(reason, mode):
    write_json(RESULTS / "result.json", {"success": False, "reason": reason, "skill_mode": mode})
    print("case failed: " + reason, flush=True)
    return 1


def pristine():
    return subprocess.run(["sha256sum", "-c", ".pristine.sha256"], cwd=WORKSPACE,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def prepare_skill(mode, package, inner):
    if mode != "with":
        return
    skills = WORKSPACE / ".opencode" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    # Evaluation inputs contain scoring expectations and must not reach the agent.
    shutil.copytree(STAGING, skills / package,
                    ignore=lambda source, names: {"evals"} if Path(source) == STAGING else set())
    if inner:
        target = skills / inner
        target.mkdir()
        shutil.copyfile(STAGING / "skills" / inner / "SKILL.md", target / "SKILL.md")
        for name in ("references", "scripts"):
            if (STAGING / name).is_dir():
                (target / name).symlink_to(Path("..") / package / name)


def prepare_home(uid, gid, auth):
    parent = Path(tempfile.mkdtemp(prefix="kpbot-agent-home-"))
    try:
        # The agent can access its home but cannot replace the root-owned parent.
        os.chmod(parent, 0o711)
        home = parent / "home"
        home.mkdir(mode=0o700)
        current = home
        os.chown(current, uid, gid)
        for part in (".local", "share", "opencode"):
            current = current / part
            current.mkdir(mode=0o700)
            os.chown(current, uid, gid)
        target = current / "auth.json"
        with auth.open("rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        os.chmod(target, 0o600)
        os.chown(target, uid, gid)
        return home
    except Exception:
        shutil.rmtree(parent)
        raise


def run_agent(uid, gid, home, prompt, model, mode, expected):
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "HOME": str(home), "USER": "analyzer", "LOGNAME": "analyzer",
           "LANG": "C.UTF-8", "SKILL_MODE": mode, "EXPECTED_SKILL": expected}

    def drop_privileges():
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    timeout = int(os.environ.get("AGENTENV_CASE_TIMEOUT", "900"))
    if not 1 <= timeout <= 3600:
        raise ValueError("invalid case timeout")
    process = subprocess.Popen(["opencode", "run", "--auto", "--format", "json", "-m", model, prompt],
                               cwd=WORKSPACE, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               preexec_fn=drop_privileges, start_new_session=True)
    selector = selectors.DefaultSelector()
    exceeded = False
    deadline = time.monotonic() + timeout
    exited_at = None
    with (RESULTS / "trace.jsonl").open("wb") as trace, (RESULTS / "opencode.log").open("wb") as log:
        for pipe, output in ((process.stdout, trace), (process.stderr, log)):
            selector.register(pipe, selectors.EVENT_READ, [output, 0])
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline or (exited_at is not None and now - exited_at > 2):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if now >= deadline:
                    exceeded = True
                break
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                output, size = key.data
                if size + len(chunk) <= MAX_OUTPUT:
                    output.write(chunk)
                else:
                    exceeded = True
                key.data[1] += len(chunk)
            if process.poll() is not None and exited_at is None:
                exited_at = time.monotonic()
        selector.close()
        process.wait()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return process.returncode, exceeded


def snapshot_report(uid):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(WORKSPACE / "opt" / "report.json", flags)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1 or info.st_size > MAX_REPORT:
            raise ValueError("report is not an owned, bounded regular file")
        data = source.read(MAX_REPORT + 1)
    if len(data) > MAX_REPORT:
        raise ValueError("report is too large")
    (RESULTS / "report.json").write_bytes(data)


def summarize_trace(expected, package):
    tools, skills, calls = [], [], []
    loaded = False
    installed = PurePosixPath(WORKSPACE.as_posix()) / ".opencode" / "skills"
    expected_paths = {installed / expected / "SKILL.md",
                      installed / package / "skills" / expected / "SKILL.md"}
    with (RESULTS / "trace.jsonl").open(encoding="utf-8", errors="replace") as trace:
        for line in trace:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            part = event.get("part") or {}
            if not isinstance(part, dict):
                continue
            if event.get("type") != "tool_use" or part.get("type") != "tool":
                continue
            tool = part.get("tool", "?")
            state = part.get("state") or {}
            if not isinstance(state, dict):
                continue
            status = state.get("status")
            inputs = state.get("input") or {}
            if not isinstance(inputs, dict):
                inputs = {}
            if tool not in tools:
                tools.append(tool)
            calls.append({"tool": tool, "status": status})
            if status != "completed":
                continue
            if tool == "skill":
                name = inputs.get("name")
                if isinstance(name, str) and name not in skills:
                    skills.append(name)
                loaded = loaded or name == expected
            elif tool == "read":
                for key in ("filePath", "file_path", "path"):
                    path = inputs.get(key)
                    if isinstance(path, str) and ".." not in PurePosixPath(path).parts:
                        read_path = PurePosixPath(path)
                        if not read_path.is_absolute():
                            read_path = PurePosixPath(WORKSPACE.as_posix()) / read_path
                        loaded = loaded or read_path in expected_paths
    return {"tools_used": tools, "skills_invoked": skills, "tool_calls": calls}, loaded


def main():
    mode = os.environ.get("SKILL_MODE", "with")
    if os.geteuid() != 0:
        print("case supervisor must run as root", flush=True)
        return 1
    RESULTS.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RESULTS, 0o700)
    if mode not in ("with", "without"):
        return fail("unknown SKILL_MODE", mode)
    package = os.environ.get("PKG_NAME", "kptools-agentenv-tuning")
    inner = os.environ.get("INNER_SKILL", "")
    expected = os.environ.get("EXPECTED_SKILL", package)
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (package, expected)) or (inner and not re.fullmatch(r"[A-Za-z0-9_.-]+", inner)):
        return fail("invalid skill name", mode)
    if not pristine():
        return fail("case integrity check failed", mode)
    auth = Path(os.environ.get("OPENCODE_AUTH_FILE", "/run/secrets/opencode-auth.json"))
    if not auth.is_file():
        return fail("opencode auth file not mounted", mode)
    user = pwd.getpwnam("analyzer")
    home = None
    try:
        prepare_skill(mode, package, inner)
        config = {"permission": {"bash": "deny", "webfetch": "deny", "websearch": "deny",
                                 "external_directory": "deny", "edit": "allow",
                                 "read": {"*": "allow", "*/auth.json": "deny", "/run/secrets/*": "deny"}}}
        (WORKSPACE / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
        home = prepare_home(user.pw_uid, user.pw_gid, auth)
        prompt = (WORKSPACE / "prompts" / (mode + "-skill.md")).read_text(encoding="utf-8")
        model = os.environ.get("MODEL", "deepseek/deepseek-v4-flash")
        rc, exceeded = run_agent(user.pw_uid, user.pw_gid, home, prompt, model, mode, expected)
        if exceeded:
            return fail("agent timeout or output limit exceeded", mode)
        if rc != 0:
            return fail("opencode run failed", mode)
        if not pristine():
            return fail("case integrity check failed", mode)
        try:
            snapshot_report(user.pw_uid)
        except (OSError, ValueError):
            return fail("report missing or not an owned regular file", mode)
        verifier_env = {**os.environ, "SKILL_MODE": mode, "AGENTENV_REPORT_PATH": str(RESULTS / "report.json")}
        with (RESULTS / "verify.log").open("wb") as log:
            verify = subprocess.run(["bash", str(WORKSPACE / "verify.sh")], cwd=WORKSPACE,
                                    env=verifier_env, stdout=log, stderr=subprocess.STDOUT)
        if not pristine():
            return fail("case integrity check failed", mode)
        try:
            result = json.loads((RESULTS / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fail("verifier did not produce a valid result", mode)
        summary, loaded = summarize_trace(expected, package)
        write_json(RESULTS / "trace_summary.json", summary)
        result.update({"skills_invoked": summary["skills_invoked"], "tools_used": summary["tools_used"],
                       "expected_skill": expected, "skill_loaded": loaded})
        if verify.returncode != 0 or not result.get("success"):
            result["success"] = False
        elif mode == "with" and not loaded:
            result.update(success=False, reason="expected skill was not invoked or read in trace")
        elif mode == "without" and loaded:
            result.update(success=False, reason="expected skill was loaded in without mode")
        write_json(RESULTS / "result.json", result)
        print("case success=" + str(result["success"]).lower(), flush=True)
        return 0 if result["success"] else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return fail("case supervisor error: " + type(exc).__name__, mode)
    finally:
        if home is not None:
            shutil.rmtree(home.parent)


if __name__ == "__main__":
    raise SystemExit(main())
