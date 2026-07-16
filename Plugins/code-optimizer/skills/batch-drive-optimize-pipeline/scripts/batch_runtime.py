"""Runtime helpers shared by the batch-drive orchestration scripts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar
import time


T = TypeVar("T")


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 3
    initial_delay_seconds: float = 0.1
    max_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0


DEFAULT_RETRY_CONFIG = RetryConfig()


def retry_call(
    action: Callable[[], T],
    retry_exceptions: tuple[type[BaseException], ...],
    config: RetryConfig = DEFAULT_RETRY_CONFIG,
) -> T:
    attempts = max(1, config.attempts)
    delay = max(0.0, config.initial_delay_seconds)
    max_delay = max(delay, config.max_delay_seconds)
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return action()
        except retry_exceptions as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            if delay:
                time.sleep(delay)
            delay = min(max_delay, delay * config.backoff_multiplier if delay else max_delay)
    assert last_error is not None
    raise last_error


def read_limited(path: Path, max_bytes: int = 200_000) -> str:
    if max_bytes <= 0:
        return ""
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
