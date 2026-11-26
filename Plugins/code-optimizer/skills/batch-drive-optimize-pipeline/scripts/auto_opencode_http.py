"""HTTP-driven opencode worker runner.

Replaces the pty/TUI injection driver (auto_claude_logged) for opencode:
spawns a dedicated `opencode serve` bound to the target workdir, drives the
session via the v2 HTTP API (prompt / permission auto-approve / message poll),
and writes the same output contract as auto_claude_logged (auto_result.json,
session_meta.json, transcript.md, events.jsonl, raw_terminal.log placeholder)
so downstream collection works unchanged.

Completion detection: watch optimization_reports/run_*/batch_result.json and
FINAL_SUMMARY.md inside the workdir, or a worker message containing the final
markers. On stall (no new messages and no pending permission for N seconds),
send a nudge with the answer bank.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from safe_env import child_env  # noqa: E402

ANSWER_BANK_FILE = Path(".batch_optimize_answer_bank.md")
FINAL_MARKERS = ("SESSION_COMPLETE", "BATCH_END", "SESSION_END")
# Markers only count when the token appears as a standalone word on its own
# (or at least word-bounded) line -- a worker *mentioning* a marker literal in
# prose ("将输出 SESSION_COMPLETE 标记") must NOT complete the session.
_MARKER_RE = {m: re.compile(rf"^\s*{m}\s*$", re.M) for m in FINAL_MARKERS}


def _matched_marker(text: str):
    """Return the first final marker that appears as a standalone line in text."""
    for marker, rx in _MARKER_RE.items():
        if rx.search(text):
            return marker
    return None
PERMISSION_POLL_SECONDS = 4.0
MESSAGE_POLL_SECONDS = 5.0
STALL_NUDGE_SECONDS = 240.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenCodeHttpSession:
    def __init__(self, workdir: Path, log_dir: Path, timeout_minutes: int, idle_timeout_minutes: int):
        self.workdir = workdir
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.deadline = time.time() + timeout_minutes * 60
        self.idle_timeout_seconds = idle_timeout_minutes * 60
        self.port = _free_port()
        self.password = f"pw{random.getrandbits(96):024x}"
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.events: list[dict] = []
        self.transcript_lines: list[str] = []
        self.status = "running"
        self.error: str | None = None
        self.started_at = time.time()
        self.ended_at: float | None = None

    # ---- infra ----
    def _start_server(self) -> None:
        # Use the same credential-filtered environment as the claude worker
        # (safe_env.child_env) instead of leaking the parent environment into
        # the serve subprocess, then add what opencode needs on top.
        env = child_env(None)
        env["TERM"] = os.environ.get("TERM", "xterm-256color") or "xterm-256color"
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        self.proc = subprocess.Popen(
            ["opencode", "serve", "--port", str(self.port)],
            cwd=str(self.workdir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._api("GET", "/api/status")[0] == 200:
                return
            if self.proc.poll() is not None:
                raise RuntimeError("opencode serve exited during startup")
            time.sleep(0.5)
        raise RuntimeError("opencode serve did not become ready in 30s")

    def _api(self, method: str, path: str, body: dict | None = None, timeout: float = 30.0):
        auth = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()[:400]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, str(exc)

    # ---- session ----
    def _create_session(self) -> None:
        status, resp = self._api("POST", "/api/session", {"directory": str(self.workdir)})
        if status != 200 or not isinstance(resp, dict):
            raise RuntimeError(f"failed to create opencode session: {status} {str(resp)[:200]}")
        self.session_id = resp["data"]["id"]
        self._log_event("session_created", {"session_id": self.session_id})

    def _prompt(self, text: str) -> None:
        self._api("POST", f"/api/session/{self.session_id}/prompt", {"text": text})
        self._log_event("prompt_sent", {"text": text[:160]})

    def _messages(self) -> list[dict]:
        status, resp = self._api("GET", f"/api/session/{self.session_id}/message")
        if status != 200 or not isinstance(resp, dict):
            return []
        return resp.get("data", []) or []

    def _permissions(self) -> list[dict]:
        status, resp = self._api("GET", f"/api/session/{self.session_id}/permission")
        if status != 200 or not isinstance(resp, dict):
            return []
        return resp.get("data", []) or []

    # ---- logging ----
    def _log_event(self, kind: str, detail: dict | None = None) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind}
        if detail:
            entry.update(detail)
        self.events.append(entry)
        with (self.log_dir / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _append_transcript(self, role: str, text: str) -> None:
        if not text.strip():
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.transcript_lines.append(f"## {stamp} {role}\n\n```text\n{text}\n```\n")
        with (self.log_dir / "transcript.md").open("a") as fh:
            fh.write(self.transcript_lines[-1])

    # ---- artifacts contract ----
    def _write_outputs(self) -> None:
        (self.log_dir / "auto_result.json").write_text(json.dumps({
            "status": self.status,
            "error": self.error,
            "worker": "opencode-http",
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at or time.time(),
            "reply_counts": dict(self.reply_counts),
            "last_message": self.last_text[:400] if self.last_text else None,
        }, ensure_ascii=False, indent=2))
        (self.log_dir / "session_meta.json").write_text(json.dumps({
            "worker": "opencode-http",
            "session_id": self.session_id,
            "directory": str(self.workdir),
            "started_at": self.started_at,
        }, ensure_ascii=False))
        (self.log_dir / "transcript.md").write_text("\n".join(self.transcript_lines))

    # ---- main loop ----
    def run(self) -> dict:
        self.reply_counts: dict[str, int] = {}
        self.last_text = ""
        last_msg_count = 0
        last_progress_ts = time.time()
        nudges = 0
        seen_permissions: set[str] = set()
        completed_msg_ids: set[str] = set()

        try:
            self._start_server()
            self._create_session()
            self._prompt("请运行 kpbot-code-optimizer 技能，按自动模式对本项目执行完整优化流水线，把结果写入 optimization_reports/。遇到需要选择时直接选自动模式。")
            self.reply_counts["start_pipeline"] = 1

            while time.time() < self.deadline:
                # permissions
                for perm in self._permissions():
                    pid = perm.get("id")
                    if pid and pid not in seen_permissions:
                        self._log_event("permission", {"action": perm.get("action"), "resources": perm.get("resources")})
                        status, _ = self._api(
                            "POST", f"/api/session/{self.session_id}/permission/{pid}/reply",
                            {"response": "once", "save": True},
                        )
                        if status and 200 <= status < 300:
                            seen_permissions.add(pid)
                        # On failure: leave pid unseen so the next poll retries
                        # the reply instead of waiting forever for a grant that
                        # never landed.
                        last_progress_ts = time.time()
                    if pid:
                        last_progress_ts = time.time()

                # messages
                messages = self._messages()
                for msg in messages:
                    mid = msg.get("id")
                    if msg.get("type") != "assistant" or not mid or mid in completed_msg_ids:
                        continue
                    content = msg.get("content", [])
                    completed = bool(msg.get("time", {}).get("completed"))
                    text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                    tools = [c.get("name") for c in content if isinstance(c, dict) and c.get("type") == "tool"]
                    if text or tools:
                        if completed or tools:
                            completed_msg_ids.add(mid)
                    if text:
                        self._append_transcript("assistant", text)
                        self.last_text = text
                        last_progress_ts = time.time()
                        marker = _matched_marker(text)
                        if marker:
                            self._log_event("final_marker", {"marker": marker})
                        if any(k in text for k in ("请选择", "QUESTION", "question", "需要确认")):
                            self._auto_answer(text)
                    if tools:
                        last_progress_ts = time.time()
                        self._log_event("tools", {"names": tools[:4]})
                if len(messages) != last_msg_count:
                    last_msg_count = len(messages)
                    last_progress_ts = time.time()

                # completion artifacts
                if self._pipeline_complete():
                    self.status = "completed"
                    self._log_event("pipeline_complete", {})
                    break

                # stall / nudge
                stalled_for = time.time() - last_progress_ts
                if stalled_for > STALL_NUDGE_SECONDS:
                    self._send_nudge(nudges)
                    nudges += 1
                    self.reply_counts[f"nudge_{nudges}"] = 1
                    last_progress_ts = time.time()

                if not self._api("GET", "/api/status")[0]:
                    self.status = "server_lost"
                    self.error = "opencode server stopped responding"
                    break
                time.sleep(MESSAGE_POLL_SECONDS)
            else:
                self.status = "timeout"
                self.error = f"global timeout after {int((time.time() - self.started_at))}s"
        except Exception as exc:  # noqa: BLE001 - runner must always write its contract
            self.status = self.status if self.status != "running" else "runner_error"
            self.error = str(exc)
        finally:
            self.ended_at = time.time()
            self._write_outputs()
            self._shutdown()

        return {
            "status": self.status,
            "error": self.error,
            "reply_counts": dict(self.reply_counts),
            "last_message": self.last_text[:400] if self.last_text else None,
        }

    def _auto_answer(self, text: str) -> None:
        bank = ANSWER_BANK_FILE
        answer = ""
        if (self.workdir / bank).exists():
            content = (self.workdir / bank).read_text()
            for line in content.splitlines():
                if "自动" in line or "auto" in line.lower():
                    answer = line.strip()
                    break
        reply = answer or "自动模式。请继续执行并完成全部阶段。"
        self._prompt(reply)
        self.reply_counts["auto_answer"] = self.reply_counts.get("auto_answer", 0) + 1

    def _send_nudge(self, n: int) -> None:
        self._prompt("继续。如果流水线已完成，请写入 optimization_reports/run_*/FINAL_SUMMARY.md 并输出 SESSION_COMPLETE。")
        self._log_event("nudge", {"n": n})

    def _pipeline_complete(self) -> bool:
        if _matched_marker(self.last_text):
            return True
        for batch in sorted(self.workdir.glob("optimization_reports/run_*/batch_result.json")):
            try:
                data = json.loads(batch.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("pipeline_status") == "complete" and data.get("quality_status") in ("verified", "blocked"):
                self._log_event("batch_result_seen", {"path": str(batch)})
                return True
        return False

    def _shutdown(self) -> None:
        if self.session_id:
            self._api("POST", f"/api/session/{self.session_id}/interrupt", timeout=5)
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--idle-timeout-seconds", type=int, default=300)
    args = parser.parse_args()

    session = OpenCodeHttpSession(
        workdir=Path(args.project_path).resolve(),
        log_dir=Path(args.log_dir),
        timeout_minutes=max(1, args.timeout_seconds // 60),
        idle_timeout_minutes=max(1, args.idle_timeout_seconds // 60),
    )
    result = session.run()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in ("completed", "pipeline_complete") else 1


if __name__ == "__main__":
    sys.exit(main())
