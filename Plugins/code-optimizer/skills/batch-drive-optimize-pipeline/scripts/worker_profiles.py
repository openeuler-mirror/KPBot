"""Worker adapter profiles for auto_claude_logged.

Two TUI workers are supported:

claude (default)   — Claude Code TUI.
opencode           — OpenCode TUI (bun/opentui), driven over pty the same way.

Each profile provides:
  command          — base argv to launch the TUI in the project directory
  ready_re         — regex indicating the TUI input box is ready (idle)
  busy_re          — regex indicating the worker is processing
  exit_cmd         — keystrokes to quit the TUI after completion
  noise_re         — TUI chrome that must not count as semantic output
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkerProfile:
    name: str
    command: list[str]
    ready_re: re.Pattern[str]
    busy_re: re.Pattern[str]
    exit_cmd: bytes
    noise_re: re.Pattern[str]
    # Message that triggers the optimization pipeline skill. claude accepts a
    # slash command; opencode treats "/x" as a built-in command, so the bare
    # skill name is used instead.
    start_pipeline_cmd: str = "/kpbot-code-optimizer"
    # opencode renders its input placeholder every frame; claude's "❯" is stable.
    ready_stable_seconds: float = 0.0


_CLAUDE = WorkerProfile(
    name="claude",
    command=["claude"],
    # Claude Code idle prompt: "❯" box / "What would you like" hint / cwd line.
    ready_re=re.compile(r"(What would you like|How can I help|Try\s*\"|❯|>)", re.I),
    busy_re=re.compile(
        r"(Thought\s+for\s+\d+|Thinking\s+for\s+\d+|Bash\(|esc\s+to\s+interrupt)",
        re.I,
    ),
    exit_cmd=b"/exit\r",
    noise_re=re.compile(r"(^$)", re.M),
)

_OPENCODE = WorkerProfile(
    name="opencode",
    command=["opencode", "--auto"],
    # OpenCode idle input box placeholder. It re-renders each frame, so also
    # require the status footer to be present to consider the UI ready.
    ready_re=re.compile(r"(Ask anything|What do you want to do)", re.I),
    busy_re=re.compile(
        r"(esc\s+(to\s+)?interrupt|Working|Running|Thinking|New\s+session\s+·)",
        re.I,
    ),
    exit_cmd=b"/exit\r",
    # OpenCode repaints the whole screen; spinner frames and repeated chrome
    # lines are noise.
    noise_re=re.compile(
        r"(New\s+session|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|Ask\s+anything|ctrl\+p|shift\+tab|"
        r"plugins?\s+failed|/plugins|/update|MCP|/mcps)",
        re.I,
    ),
    start_pipeline_cmd="请运行 kpbot-code-optimizer 技能，按自动模式执行完整优化流水线",
    # Input box repaints every frame: require it to be visible for a moment.
    ready_stable_seconds=2.0,
)

_PROFILES = {"claude": _CLAUDE, "opencode": _OPENCODE}


def get_profile(name: str, claude_bin: str | None = None) -> WorkerProfile:
    base = _PROFILES.get(name)
    if base is None:
        raise ValueError(f"unknown worker type: {name}; expected one of {sorted(_PROFILES)}")
    command = list(base.command)
    if name == "claude" and claude_bin:
        command = [claude_bin]
    return WorkerProfile(
        name=base.name,
        command=command,
        ready_re=base.ready_re,
        busy_re=base.busy_re,
        exit_cmd=base.exit_cmd,
        noise_re=base.noise_re,
        start_pipeline_cmd=base.start_pipeline_cmd,
        ready_stable_seconds=base.ready_stable_seconds,
    )
