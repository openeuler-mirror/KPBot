"""Framework-neutral prompt capture runtime, extracted from the reference proxy.

See ../references/sources.md for provenance and adaptation boundaries.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import gzip
import hashlib
import json
import logging
import math
import os
import queue
import random
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any
from types import SimpleNamespace

logger = logging.getLogger(__name__)
Request = Any

CAPTURE_FORMAT = 1
_CAP_MAGIC = b"PCAP"
_CAP_FRAME = struct.Struct("<4sII")  # 魔数、meta 字节数、payload 字节数
_CAP_API = {"/chat/completions": "chat", "/completions": "completions"}
_CAP_IDS_CHUNK = 1024  # golden token ids 每 1024 个一块，按内容寻址：同一会话的共享前缀只存一次
_CAP_PTXT_CHUNK = 16384  # golden 模板后文本每 16384 个字符一块，道理同上
_CAP_PARAM_INLINE = 4096  # 其他请求参数序列化后超过这么多字节（如自定义 chat_template）就单独存成 blob
_CAP_META_INTS = frozenset({"max_tokens", "max_completion_tokens", "n", "seed", "top_k", "top_logprobs"})
_CAP_META_NUMBERS = frozenset({"temperature", "top_p", "min_p", "presence_penalty",
                              "frequency_penalty", "repetition_penalty"})
_CAP_META_BOOLS = frozenset({"stream", "echo", "logprobs", "ignore_eos"})
_CAP_META_ENUMS = {
    "tool_choice": frozenset({"none", "auto", "required"}),
    "reasoning_effort": frozenset({"none", "minimal", "low", "medium", "high"}),
}
_CAP_FOLLOW_SCOPE_KEYS = ("enabled", "stop_at_unix", "follow_sessions", "follow_new",
                          "follow_clients", "follow_idle_min", "session_headers",
                          "sample_by", "salt", "models", "endpoints")
_CAP_TEXT_PARTS = frozenset({"text", "input_text", "output_text"})
_CAP_PT_BUCKETS = ((1024, "<1k"), (4096, "1k-4k"), (16384, "4k-16k"), (32768, "16k-32k"), (65536, "32k-64k"),
                   (131072, "64k-128k"), (float("inf"), ">=128k"))

# [capture] 参数表：键 → (默认值, 取值要求)。配置文件里出现表外的键会被拒绝（防止拼错键名却以为生效了）。
_CAP_SCHEMA: dict[str, tuple[Any, Any]] = {
    "enabled": (False, bool),
    "metadata_only": (False, bool),
    "out_dir": ("", str),
    "meta_rate": (1.0, "rate"),
    "text_rate": (0.0, "rate"),
    "gold_rate": (0.0, "rate"),
    "sample_by": ("session", ("session", "request")),
    "salt": ("", str),
    "session_headers": (["x-session-id", "x-session-affinity", "session_id", "x-conversation-id"], "strs"),
    "keep_headers": (["user-agent", "x-session-id", "x-request-id"], "strs"),
    "record_client_ip": (False, bool),
    "stop_at_unix": (0.0, "nonneg"),
    "endpoints": (["chat", "completions"], ("chat", "completions")),
    "models": ([], "strs"),
    "max_body_mb": (64.0, "pos"),
    "keep_media": (False, bool),
    "output": ("stats", ("none", "stats", "text")),
    "max_output_kb": (1024.0, "pos"),
    "max_text_rps": (0.0, "nonneg"),
    # 跟踪会话连续记录请求全文；golden 和输出按配置采集，不受 text_rate/max_text_rps 影响。
    "follow_sessions": ([], "strs"),  # 会话 ID 或前缀；热改生效
    "follow_new": (0, "nonneg_int"),  # 每个 worker 自动跟踪它新看到的前 N 个会话（0 = 不自动跟踪）
    "follow_clients": ([], "strs"),  # 只自动跟踪这些客户端家族（如 opencode）；空 = 全部
    "follow_gold": (False, bool),
    "follow_output": ("stats", ("none", "stats", "text")),
    "follow_idle_min": (240.0, "pos"),  # 会话多少分钟没有请求就不再跟踪（释放名额与内存）
    "queue_max_mb": (256.0, "pos"),
    "rotate_mb": (128.0, "pos"),
    "rotate_min": (60.0, "pos"),
    "compress": (True, bool),
    "max_total_gb": (50.0, "pos"),
    "flush_s": (0.0, "nonneg"),
    "pending_max_min": (30.0, "pos"),
    "admin_local_only": (True, bool),
}


def capture_validate(raw: Any) -> dict[str, Any]:
    """[capture] 校验并补全配置：返回包含全部键的新 dict；有任何问题就抛 ValueError（列出全部问题）。"""
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    errs = [f"unknown key {k!r}" for k in raw if k not in _CAP_SCHEMA]
    out: dict[str, Any] = {}
    for key, (default, spec) in _CAP_SCHEMA.items():
        v = raw.get(key, default)
        if spec is bool or spec is str:
            ok = isinstance(v, spec)
        elif spec in ("rate", "pos", "nonneg"):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            ok = ok and (0.0 <= v <= 1.0 if spec == "rate" else v > 0 if spec == "pos" else v >= 0)
            if ok:
                v = float(v)
        elif spec == "nonneg_int":
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
        elif spec == "strs":
            ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
        elif isinstance(default, list):
            ok = isinstance(v, list) and all(x in spec for x in v)
        else:
            ok = v in spec
        if not ok:
            want = {"rate": "number in [0,1]", "pos": "number > 0", "nonneg": "number >= 0", "strs": "list of strings",
                    "nonneg_int": "integer >= 0"}
            errs.append(f"{key}={v!r} invalid (want {want.get(spec, spec.__name__ if isinstance(spec, type) else spec)})")
        out[key] = v
    sensitive = {"authorization", "proxy-authorization", "cookie", "set-cookie",
                 "x-api-key", "api-key", "x-auth-token"}
    for key in ("keep_headers", "session_headers"):
        if isinstance(out[key], list) and any(
                str(h).lower() in sensitive for h in out[key]):
            errs.append(f"{key} must not contain credential headers")
    if errs:
        raise ValueError("; ".join(errs))
    if out["enabled"] and out["metadata_only"]:
        conflicts = [key for key in ("text_rate", "gold_rate", "follow_sessions", "follow_new", "follow_gold")
                     if out[key]]
        conflicts += [key for key in ("output", "follow_output") if out[key] == "text"]
        if conflicts:
            raise ValueError("metadata-only capture conflicts with: " + ", ".join(conflicts))
    return out


class _CapCfg:
    """[capture] 不可变的配置快照。事件循环每个请求读一次 runtime.cfg，发送线程整体替换它，不需要锁。"""

    def __init__(self, d: dict[str, Any], config_path: str):
        self.raw = d
        for k, v in d.items():
            setattr(self, k, v)
        base = os.path.dirname(os.path.abspath(config_path))
        self.out_dir_abs = os.path.abspath(os.path.join(base, d["out_dir"] or "prompt_capture_data"))
        self.session_headers = [h.lower() for h in d["session_headers"]]
        self.keep_headers = [h.lower() for h in d["keep_headers"]]
        self.want_headers = frozenset(h.encode("latin-1") for h in self.session_headers + self.keep_headers)
        self.keep_set = frozenset(self.keep_headers)
        self.endpoints = frozenset(d["endpoints"])
        self.models = frozenset(d["models"])
        self.max_body = int(d["max_body_mb"] * 2**20)
        self.queue_max = int(d["queue_max_mb"] * 2**20)
        self.max_output = int(d["max_output_kb"] * 1024)
        self.follow_set = frozenset(d["follow_sessions"])
        self.follow_prefixes = tuple(x for x in d["follow_sessions"] if x)
        self.follow_client_set = frozenset(d["follow_clients"])
        self.follow_any = bool(self.follow_prefixes) or d["follow_new"] > 0
        self.follow_scope = _cap_h(_cap_dumps({k: d[k] for k in _CAP_FOLLOW_SCOPE_KEYS}))
        self.follow_epoch = object()
        self.version: list | None = None


def capture_config_path(args: argparse.Namespace) -> str | None:
    path = getattr(args, "capture_config", None) or os.environ.get("PROMPT_CAPTURE_CONFIG")
    if not path:
        path = os.path.join(tempfile.gettempdir(), f"vllm_lb_proxy_capture_{args.port}.json")
    if path.strip().lower() in ("off", "none", "disable", "disabled", "0"):
        return None
    return os.path.abspath(path)


def _cap_atomic_json(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def capture_prepare_config(args: argparse.Namespace) -> str | None:
    """[capture] 返回配置文件路径（off 时返回 None）；文件不存在就写一份默认配置（enabled=false）。"""
    path = capture_config_path(args)
    if path is None or os.path.exists(path):
        return path
    d = capture_validate({})
    d["out_dir"] = os.path.abspath("prompt_capture_data")  # 按启动时的当前目录解析成绝对路径写进文件
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _cap_atomic_json(path, d)
    return path


def _cap_dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass")


def _cap_h(b: bytes) -> str:
    return hashlib.blake2b(b, digest_size=10).hexdigest()


def _cap_hash01(s: str) -> float:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=8).digest(), "big") / 2.0**64


def _cap_meta_param_allowed(key: str, value: Any) -> bool:
    """Metadata records retain only known scalar generation settings and the model ID."""
    if key == "model":
        return isinstance(value, str)
    if key in _CAP_META_INTS:
        return type(value) is int
    if key in _CAP_META_NUMBERS:
        return type(value) is int or (type(value) is float and math.isfinite(value))
    if key in _CAP_META_BOOLS:
        return type(value) is bool
    return isinstance(value, str) and value in _CAP_META_ENUMS.get(key, ())


def _cap_exc_name(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if response is not None and isinstance(getattr(response, "status_code", None), int):
        return f"http_{response.status_code}"
    return type(exc).__name__


def _cap_fingerprint(messages: Any) -> str | None:
    """[capture] 没有会话头时的会话指纹：从第一条消息到第一条 user 消息（含）的内容哈希。"""
    if not isinstance(messages, list):
        return None
    h = hashlib.blake2b(digest_size=8)
    for m in messages:
        if not isinstance(m, dict):
            return None
        h.update(json.dumps(m, ensure_ascii=False, sort_keys=True).encode("utf-8", "surrogatepass"))
        if m.get("role") == "user":
            return "fp:" + h.hexdigest()
    return None


_CAP_LOOP_COUNTERS = ("seen", "skip_endpoint", "skip_model", "skip_sample", "meta", "text", "gold", "too_large",
                      "rate_limited", "follow", "drop_queue", "drop_queue_end", "drop_writer", "sent", "sent_bytes",
                      "hook_errors")
_CAP_HK_COUNTERS = ("writer_starts", "writer_deaths", "cfg_loads", "cfg_errors")


class _CaptureRuntime:
    """[capture] 每个 worker 一个。
    事件循环线程：抽样、把事件编成帧追加到内存缓冲，并用非阻塞 os.write 写进写盘进程的管道；
      管道满时登记 loop.add_writer，可写了再接着写。没有第二个线程参与每个请求，所以不会与事件循环争 GIL。
    后台线程（每 0.25 秒醒一次）：重读配置、写状态文件、启动/重启/关闭写盘进程（启动时的 fork 不在事件循环里做）。"""

    def __init__(self, path: str, loop: asyncio.AbstractEventLoop):
        self.path = path
        self.state_dir = path + ".state"
        self.loop = loop
        self.loop_tid = threading.get_ident()
        self.cfg = _CapCfg(capture_validate({}), path)
        self.cfg_error = ""
        self._sig: Any = None
        self.c = dict.fromkeys(_CAP_LOOP_COUNTERS, 0)  # 只由事件循环线程修改（键固定，后台线程拷贝它是安全的）
        self.cs = dict.fromkeys(_CAP_HK_COUNTERS, 0)  # 只由后台线程修改
        self._tokens = 0.0
        self._tok_t = time.monotonic()
        self._last_log = 0.0
        # 以下只在事件循环线程里读写
        self._buf: "collections.deque[bytes]" = collections.deque()  # 待写的完整帧
        self._buf_bytes = 0
        self._off = 0  # 队首帧已写出的字节数
        self._fd: int | None = None
        self._pipe: Any = None  # 写盘进程 stdin 的文件对象（保持引用，关的时候用它关）
        self._watching = False
        self._in_pump = False  # 防止 _pump 被重入（例如 GC 在 _pump 中途触发了别的回调）
        self._last_event = time.monotonic()
        self._out_bytes = 0  # 各 CaptureCtx 缓存的输出字节合计：与待写帧一起受 queue_max_mb 约束
        self._inflight = 0  # 已开始、尚未 finish 的被采集请求数
        # follow：被跟踪的会话 → 最近一次请求的 monotonic 时刻；auto 集合是 follow_new 自动选中的；seen 是最近见过的会话（有界）
        self._followed: dict[str, float] = {}
        self._follow_auto: set[str] = set()
        self._seen_sessions: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self._follow_cfg: _CapCfg | None = None
        self._follow_epoch: Any = None
        # 以下只在后台线程里读写
        self._proc: subprocess.Popen | None = None
        self._retired: list[subprocess.Popen] = []
        self._proc_t = -1e9
        self._next_start = 0.0
        self._backoff = 0.0
        self._stop = threading.Event()
        self._reload()
        self._hk = threading.Thread(target=self._housekeeping, name="prompt-capture-hk", daemon=True)
        self._hk.start()

    # ---- 事件循环线程 ------------------------------------------------------------------
    def hook_error(self, exc: BaseException) -> None:
        self.c["hook_errors"] += 1
        now = time.monotonic()
        if now - self._last_log > 60:
            self._last_log = now
            logger.warning("prompt capture hook error (request unaffected): %r", exc)

    def enqueue(self, meta: dict, payload: bytes) -> bool:
        if threading.get_ident() != self.loop_tid:  # 例如 CaptureCtx.__del__ 在别的线程被调用
            try:
                self.loop.call_soon_threadsafe(self.enqueue, meta, payload)
                return True
            except RuntimeError:  # 事件循环已关闭
                return False
        mb = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass")
        n = _CAP_FRAME.size + len(mb) + len(payload)
        if self._buf_bytes + self._out_bytes + n > self.cfg.queue_max or len(self._buf) >= 100000:
            return False
        self._buf.append(b"".join((_CAP_FRAME.pack(_CAP_MAGIC, len(mb), len(payload)), mb, payload)))
        self._buf_bytes += n
        self._last_event = time.monotonic()
        if not self._watching and not self._in_pump:
            self._pump()
        return True

    def _pump(self) -> None:
        fd = self._fd
        if fd is None or self._in_pump:
            return  # 写盘进程还没起来：帧留在缓冲里（受 queue_max_mb 约束），接上后再写
        buf = self._buf
        self._in_pump = True
        try:
            while buf:
                frame = buf[0]
                n = os.write(fd, memoryview(frame)[self._off:])
                self._off += n
                if self._off < len(frame):
                    break
                buf.popleft()
                self._buf_bytes -= len(frame)
                self._off = 0
                self.c["sent"] += 1
                self.c["sent_bytes"] += len(frame)
        except BlockingIOError:
            pass
        except OSError:  # BrokenPipe 等：写盘进程没了
            self._in_pump = False
            self._detach(dead=True)
            return
        finally:
            self._in_pump = False
        need = bool(buf)
        if need and not self._watching:
            self.loop.add_writer(fd, self._pump)
            self._watching = True
        elif not need and self._watching:
            self.loop.remove_writer(fd)
            self._watching = False

    def _attach(self, pipe: Any) -> None:
        """后台线程启动了新的写盘进程后，在事件循环线程里接上它的管道。"""
        if self._fd is not None:
            self._detach(dead=False)
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        if sys.platform.startswith("linux"):  # 管道默认 64 KB；调到 1 MB，大请求体一次写完，少进几次 add_writer
            try:
                import fcntl  # noqa: PLC0415

                fcntl.fcntl(fd, 1031, 1 << 20)  # F_SETPIPE_SZ
            except OSError:
                pass
        self._fd, self._pipe = fd, pipe
        self._pump()

    def _detach(self, dead: bool) -> None:
        fd, pipe = self._fd, self._pipe
        self._fd = self._pipe = None
        if fd is not None and self._watching:
            try:
                self.loop.remove_writer(fd)
            except Exception:
                pass
        self._watching = False
        if self._off:  # 写了一半的帧不能交给下一个写盘进程，丢掉
            frame = self._buf.popleft()
            self._buf_bytes -= len(frame)
            self._off = 0
            self.c["drop_writer"] += 1
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass

    def _take_token(self, rate: float) -> bool:
        now = time.monotonic()
        self._tokens = min(max(rate, 1.0), self._tokens + (now - self._tok_t) * rate)
        self._tok_t = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def begin(self, cfg: _CapCfg, api: str, request: Request, req_data: Any, req_body: bytes, t0: float):
        c = self.c
        c["seen"] += 1
        ep = _CAP_API.get(api)
        if ep is None or ep not in cfg.endpoints:
            c["skip_endpoint"] += 1
            return None
        if not isinstance(req_data, dict):
            return None
        if cfg.models and req_data.get("model") not in cfg.models:
            c["skip_model"] += 1
            return None
        found: dict[str, str] = {}  # 一次扫描原始请求头（ASGI 头名已是小写 bytes），同名头取第一个
        for k, v in request.scope.get("headers") or ():
            if k in cfg.want_headers:
                name = k.decode("latin-1")
                if name not in found:
                    found[name] = v.decode("latin-1")
        sess, src = None, ""
        for name in cfg.session_headers:
            v = found.get(name)
            if v:
                sess, src = v, "h:" + name
                break
        if sess is None and ep == "chat" and cfg.sample_by == "session":
            sess = _cap_fingerprint(req_data.get("messages"))
            src = "fp" if sess else ""
        key = sess if cfg.sample_by == "session" and sess else None
        fol = bool(sess) and cfg.follow_any and self._follow(cfg, sess, found.get("user-agent"))
        u = _cap_hash01(cfg.salt + "|" + key) if key else random.random()
        text = fol or u < cfg.text_rate
        if not text and u >= cfg.meta_rate:
            c["skip_sample"] += 1
            return None
        why = ""
        if text and len(req_body) > cfg.max_body:
            text, why = False, "too_large"
            c["too_large"] += 1
        if text and not fol and cfg.max_text_rps > 0 and not self._take_token(cfg.max_text_rps):
            text, why = False, "rate_limited"
            c["rate_limited"] += 1
        gold = False
        if text and fol:
            gold = cfg.follow_gold
        elif text and cfg.gold_rate > 0:
            gold = (_cap_hash01(cfg.salt + "|gold|" + key) if key else random.random()) < cfg.gold_rate
        omode = cfg.follow_output if (text and fol) else cfg.output
        cid = uuid.uuid4().hex
        meta = {
            "ev": "req", "id": cid, "ts": t0, "ep": ep, "lvl": "text" if text else "meta", "gold": gold,
            "sess": sess, "ss": src,
            "ip": request.client.host if cfg.record_client_ip and request.client else None,
            "hd": {k: v for k, v in found.items() if k in cfg.keep_set}, "worker": os.getpid(),
            "why": why, "out": omode, "fol": fol,
        }
        if not self.enqueue(meta, req_body):
            c["drop_queue"] += 1
            return None
        c["text" if text else "meta"] += 1
        if gold:
            c["gold"] += 1
        if fol:
            c["follow"] += 1
        ctx = CaptureCtx(self, cid, t0, gold, omode != "none", cfg.max_output, ep, meta["lvl"], omode)
        self._inflight += 1
        try:
            request.state.capture = ctx  # 供 with_cancellation 在客户端提前断开时标记状态
        except Exception:
            pass
        return ctx

    def _follow(self, cfg: _CapCfg, sess: str, ua: str | None) -> bool:
        """事件循环线程：这个会话是否被整会话跟踪。显式列表（ID 或前缀）总是跟踪；follow_new 只对「第一次见到」的会话生效，
        且每个 worker 最多自动选 follow_new 个（多 worker 时总数最多是 worker 数 × follow_new）。"""
        self._apply_follow_config(cfg)
        if cfg is not self.cfg or not cfg.enabled:
            return False
        now = time.monotonic()
        if sess in self._followed:
            self._followed[sess] = now
            return True
        seen = sess in self._seen_sessions
        self._seen_sessions[sess] = None
        if len(self._seen_sessions) > 20000:
            self._seen_sessions.popitem(last=False)
        if len(self._followed) >= 64 or (len(self._followed) % 16 == 0 and self._followed):
            idle = cfg.follow_idle_min * 60
            for k in [k for k, t in self._followed.items() if now - t > idle]:
                del self._followed[k]
                self._follow_auto.discard(k)
        if self.follow_prefix_match(cfg, sess):
            self._followed[sess] = now
            return True
        if cfg.follow_new > 0 and not seen and len(self._follow_auto) < cfg.follow_new and \
                (not cfg.follow_client_set or _cap_client_family(ua) in cfg.follow_client_set):
            self._followed[sess] = now
            self._follow_auto.add(sess)
            return True
        return False

    @staticmethod
    def follow_prefix_match(cfg: _CapCfg, sess: str) -> bool:
        return sess in cfg.follow_set or any(sess.startswith(p) for p in cfg.follow_prefixes)

    def _apply_follow_config(self, cfg: _CapCfg) -> None:
        """Event-loop thread: reset tracking when scope or capture window changes."""
        if cfg is not self.cfg:
            return
        if self._follow_epoch is not cfg.follow_epoch or not cfg.enabled:
            self._followed.clear()
            self._follow_auto.clear()
            self._seen_sessions.clear()
        self._follow_epoch = cfg.follow_epoch
        self._follow_cfg = cfg

    def _idle_check(self) -> None:
        """事件循环线程：关闭采集、5 秒没有新事件、缓冲已写空、在途请求都结束时，关掉管道让写盘进程收尾退出。"""
        idle = time.monotonic() - self._last_event
        if not self.cfg.enabled and self._fd is not None and not self._buf and idle > 5.0 and \
                (self._inflight <= 0 or idle > self.cfg.pending_max_min * 60):
            self._detach(dead=False)  # 等在途请求都结束（它们的结束记录还要写），最多等 pending_max_min

    # ---- 后台线程 ----------------------------------------------------------------------
    def _install_config(self, cfg: _CapCfg) -> None:
        if cfg.follow_scope == self.cfg.follow_scope:
            cfg.follow_epoch = self.cfg.follow_epoch
        self.cfg = cfg
        self._call(self._apply_follow_config, cfg)

    def _reload(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            if self._sig != "missing":
                self._sig = "missing"
                self.cfg_error = "config file missing: capture stopped"
                self._install_config(_CapCfg({**self.cfg.raw, "enabled": False}, self.path))
            return
        except OSError as exc:
            self.cfg_error = f"stat failed: {exc}"
            return
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        self._sig = sig
        try:
            with open(self.path, encoding="utf-8") as f:
                d = capture_validate(json.load(f))
            cfg = _CapCfg(d, self.path)
            cfg.version = list(sig)
            self._install_config(cfg)
            self.cfg_error = ""
            self.cs["cfg_loads"] += 1
        except Exception as exc:  # 保留上一份有效配置
            self.cfg_error = f"{type(exc).__name__}: {exc}"[:800]
            self.cs["cfg_errors"] += 1
            logger.warning("prompt capture: config %s rejected, keeping previous: %s", self.path, self.cfg_error)

    def _call(self, fn: Any, *a: Any) -> None:
        try:
            self.loop.call_soon_threadsafe(fn, *a)
        except RuntimeError:  # 事件循环已关闭
            pass

    def _housekeeping(self) -> None:
        next_cfg = next_stats = 0.0
        while not self._stop.wait(0.25):
            now = time.monotonic()
            try:
                if now >= next_cfg:
                    self._reload()
                    next_cfg = now + 1.0
                if self.cfg.enabled and self.cfg.stop_at_unix and time.time() >= self.cfg.stop_at_unix:
                    self._install_config(_CapCfg({**self.cfg.raw, "enabled": False}, self.path))
                self._manage_writer(now)
                if now >= next_stats:
                    self._write_stats()
                    if self.cfg.follow_any:
                        self._sync_follow()
                    next_stats = now + (1.0 if self.cfg.follow_any else 2.0)
            except Exception as exc:  # 后台线程永不退出
                logger.warning("prompt capture housekeeping error: %r", exc)

    def _sync_follow(self) -> None:
        """后台线程：读其他 worker 的状态文件，把它们正在跟踪的会话也加进本 worker 的跟踪集合。
        多 worker 时同一会话的请求会落到不同 worker，这样约 1 秒内所有 worker 都会跟踪同一批会话。"""
        cfg = self.cfg
        if not cfg.enabled or not cfg.follow_any:
            return
        names: set[str] = set()
        try:
            for fn in os.listdir(self.state_dir):
                if fn.startswith("worker-") and fn.endswith(".json") and fn != f"worker-{os.getpid()}.json":
                    try:
                        with open(os.path.join(self.state_dir, fn), encoding="utf-8") as f:
                            st = json.load(f)
                        if (time.time() - float(st.get("updated") or 0) < 30 and
                                st.get("enabled") and st.get("follow_version") == cfg.version):
                            names.update(st.get("followed") or [])
                    except Exception:
                        continue
        except OSError:
            return
        if names:
            self._call(self._adopt_follow, names, cfg)

    def _adopt_follow(self, names: set[str], cfg: _CapCfg) -> None:
        if cfg is not self.cfg or not cfg.enabled:
            return
        self._apply_follow_config(cfg)
        now = time.monotonic()
        for n in names:
            if n not in self._followed and (cfg.follow_new > 0 or self.follow_prefix_match(cfg, n)):
                self._followed[n] = now  # 别的 worker 选中的：不占本 worker 的 follow_new 名额

    def _manage_writer(self, now: float) -> None:
        self._retired = [p for p in self._retired if p.poll() is None]  # 回收已退出的写盘进程
        p = self._proc
        if p is not None and p.poll() is not None:  # 挂了（或关闭采集后正常退出）
            if now - self._proc_t > 60 or p.returncode == 0:  # 正常退出或运行一分钟以上才挂：立即可重启；
                self._backoff = 0.0  # 短时间内反复异常退出：1、2、4…60 秒退避
            if p.returncode != 0:
                self.cs["writer_deaths"] += 1
                logger.warning("prompt capture writer exited with %s", p.returncode)
            self._next_start = now + self._backoff
            self._backoff = min(max(self._backoff * 2, 1.0), 60.0)
            self._proc = None
            self._call(self._detach, True)
            return
        want = self.cfg.enabled or self._buf_bytes > 0
        if p is None and want and now >= self._next_start:
            try:
                self._proc = subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--capture-writer", self.path, self.state_dir,
                     str(os.getpid())],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                )
                self._proc_t = now
                self.cs["writer_starts"] += 1
                self._call(self._attach, self._proc.stdin)
            except Exception as exc:
                self._next_start = now + max(self._backoff, 1.0)
                self._backoff = min(max(self._backoff * 2, 1.0), 60.0)
                logger.warning("prompt capture: cannot start writer: %r", exc)
        elif p is not None and not self.cfg.enabled:
            self._call(self._idle_check)  # 管道关闭后写盘进程收尾退出，下一轮在上面被回收

    def _write_stats(self) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            p = self._proc
            cfg = self.cfg
            followed = sorted(self._followed)[:200] if self._follow_cfg is cfg else []
            st = {
                "kind": "worker", "pid": os.getpid(), "host": socket.gethostname(), "updated": time.time(),
                "config_path": self.path, "config_error": self.cfg_error, "enabled": self.cfg.enabled,
                "config_sig": list(self._sig) if isinstance(self._sig, tuple) else self._sig,
                "queue_items": len(self._buf), "queue_mb": round(self._buf_bytes / 2**20, 3),
                "writer_pid": p.pid if p is not None and p.poll() is None else None,
                "counters": {**self.c, **self.cs},
                "followed": followed,
                "follow_version": cfg.version if self._follow_cfg is cfg else None,
            }
            _cap_atomic_json(os.path.join(self.state_dir, f"worker-{os.getpid()}.json"), st)
        except Exception:
            pass

    def shutdown(self) -> None:
        """在事件循环线程里调用（lifespan 收尾）：停后台线程，把缓冲里的帧写完，关管道，等写盘进程最多 5 秒。"""
        self._stop.set()
        self._hk.join(timeout=5.0)
        deadline = time.monotonic() + 5.0
        if self._fd is not None and self._watching:
            self.loop.remove_writer(self._fd)
            self._watching = False
        try:
            import selectors  # noqa: PLC0415  （select.select 不支持 >=1024 的 fd）

            with selectors.DefaultSelector() as sel:
                fd = self._fd
                if fd is not None:
                    sel.register(fd, selectors.EVENT_WRITE)
                while self._fd is not None and self._buf and time.monotonic() < deadline:
                    if not sel.select(max(0.0, deadline - time.monotonic())):
                        break
                    self._pump()
                    if self._watching and self._fd is not None:
                        self.loop.remove_writer(self._fd)
                        self._watching = False
        except Exception:
            pass
        self._detach(dead=False)
        p = self._proc
        if p is not None:
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:
                pass  # 还在压缩最后一个分段：不等了，它写完会自己退出
        try:
            os.remove(os.path.join(self.state_dir, f"worker-{os.getpid()}.json"))
        except OSError:
            pass


class CaptureCtx:
    """[capture] 一个被抽中请求的旁路记录。所有方法都不抛异常、不 await，只在事件循环线程里调用。"""

    __slots__ = ("rt", "cid", "t0", "gold", "keep_out", "max_out", "ep", "lvl", "omode", "out", "out_n", "out_cut",
                 "tail", "tail_n", "p_t0", "p_t1", "p_err", "pt", "ct", "p_raw", "pn", "dn", "d_first", "recomputed",
                 "done", "rid", "__weakref__")
    TAIL = 65536  # 输出超出 max_output_kb 后仍保留最后 64 KB：usage、finish_reason、[DONE] 都在流的末尾

    def __init__(self, rt: _CaptureRuntime, cid: str, t0: float, gold: bool, keep_out: bool, max_out: int,
                 ep: str = "chat", lvl: str = "meta", omode: str = "stats"):
        self.rt, self.cid, self.t0, self.gold = rt, cid, t0, gold
        self.keep_out, self.max_out = keep_out, max_out
        self.ep, self.lvl, self.omode = ep, lvl, omode
        self.out: list[bytes] | None = []
        self.out_n = 0
        self.out_cut = False
        self.tail: "collections.deque[bytes]" = collections.deque()
        self.tail_n = 0
        self.p_t0 = self.p_t1 = self.d_first = None
        self.p_err = self.pn = self.dn = None
        self.pt = self.ct = None
        self.p_raw: bytes | None = None
        self.recomputed = 0
        self.done = False
        self.rid: str | None = None  # proxy 发给 P 与 D 的 X-Request-Id（vLLM 日志里是 chatcmpl-<rid> / cmpl-<rid>）

    def prefill_extra(self) -> dict | None:
        # golden：让 P 顺带返回 vLLM 实际用的 prompt token ids 与模板后文本（vLLM 不认识的字段会被忽略）
        return {"return_token_ids": True, "return_prompt_text": True} if self.gold else None

    def prefill_start(self, request_id: str | None = None) -> None:
        if self.p_t0 is None:
            self.p_t0 = time.time()
        if request_id and self.rid is None:
            self.rid = str(request_id)

    def prefill_done(self, response: Any, response_json: Any, key: str) -> None:
        try:
            if self.p_t1 is not None:
                return
            self.p_t1, self.pn = time.time(), key
            usage = response_json.get("usage") if isinstance(response_json, dict) else None
            if isinstance(usage, dict):
                self.pt = usage.get("prompt_tokens")
                details = usage.get("prompt_tokens_details")
                if isinstance(details, dict):
                    self.ct = details.get("cached_tokens")
            if self.gold:
                self.p_raw = response.content
        except Exception as exc:
            self.rt.hook_error(exc)

    def prefill_failed(self, exc: BaseException) -> None:
        try:
            if self.p_t1 is None:
                self.p_t1, self.p_err = time.time(), _cap_exc_name(exc)
        except Exception:
            pass

    def decode_started(self, key: str) -> None:
        if self.d_first is None:
            self.d_first, self.dn = time.time(), key

    def _keep(self, chunk: bytes) -> None:
        """记下一块输出：先存开头（最多 max_output_kb），超出后只保留最后 TAIL 字节；
        所有请求缓存的输出与待写帧合计不超过 queue_max_mb，超出同样只留尾部。"""
        rt, n = self.rt, len(chunk)
        if not self.out_cut and self.out is not None and self.out_n + n <= self.max_out and \
                rt._buf_bytes + rt._out_bytes + n <= rt.cfg.queue_max:
            self.out.append(chunk)
            self.out_n += n
            rt._out_bytes += n
            return
        self.out_cut = True
        available = max(0, min(self.TAIL, rt.cfg.queue_max - rt._buf_bytes -
                               rt._out_bytes + self.tail_n))
        chunk = chunk[-available:] if available else b""
        if chunk:
            self.tail.append(chunk)
            self.tail_n += len(chunk)
            rt._out_bytes += len(chunk)
        while self.tail_n > available:
            first = self.tail.popleft()
            removed = min(len(first), self.tail_n - available)
            if removed < len(first):
                self.tail.appendleft(first[removed:])
            self.tail_n -= removed
            rt._out_bytes -= removed

    async def wrap(self, agen: Any):
        status = "ok"
        try:
            async for chunk in agen:
                if self.keep_out and not self.done and isinstance(chunk, (bytes, bytearray)):
                    try:
                        self._keep(bytes(chunk))
                    except Exception as exc:
                        self.rt.hook_error(exc)
                yield chunk
        except GeneratorExit:
            status = "client_disconnect"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except BaseException as exc:
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            self.finish(status)  # 同步调用，不 await：不影响上游生成器自己的收尾逻辑

    def response_chunk(self, chunk: bytes) -> None:
        """Observe response bytes without modifying them (also for non-stream responses)."""
        try:
            if self.keep_out and not self.done:
                self._keep(chunk)
        except Exception as exc:
            self.rt.hook_error(exc)

    def finish(self, status: str) -> None:
        if self.done:
            return
        self.done = True
        rt = self.rt
        try:
            rt._inflight -= 1
            p = self.p_raw or b""
            o = b"".join(self.out) if self.out else b""
            t = b"".join(self.tail) if self.tail else b""
            rt._out_bytes -= self.out_n + self.tail_n
            self.out, self.tail, self.p_raw = None, collections.deque(), None
            self.out_n = self.tail_n = 0
            meta = {
                "ev": "end", "id": self.cid, "st": status, "ep": self.ep, "lvl": self.lvl, "out": self.omode,
                "t0": self.t0, "p_t0": self.p_t0, "p_t1": self.p_t1, "p_err": self.p_err, "pt": self.pt,
                "ct": self.ct, "pn": self.pn, "dn": self.dn, "d_first": self.d_first, "t_end": time.time(),
                "rc": self.recomputed, "p_len": len(p), "o_len": len(o), "t_len": len(t), "o_cut": self.out_cut,
                "rid": self.rid,
            }
            if not rt.enqueue(meta, p + o + t):
                rt.c["drop_queue_end"] += 1
        except Exception as exc:
            rt.hook_error(exc)

    def __del__(self):
        # 被回收时还没结束（例如客户端在拿到响应头之前断开）：不在这里直接写（GC 可能发生在任何线程、
        # 任何时刻，包括 _pump 中途），交给事件循环去做
        if not self.done:
            try:
                self.rt.loop.call_soon_threadsafe(self.finish, "abandoned")
            except Exception:
                pass


_capture_rt: _CaptureRuntime | None = None


def capture_begin(api: str, request: Request, req_data: Any, req_body: bytes, t0: float) -> CaptureCtx | None:
    rt = _capture_rt
    if rt is None:
        return None
    cfg = rt.cfg
    if not cfg.enabled or (cfg.stop_at_unix and time.time() >= cfg.stop_at_unix):
        return None
    try:
        return rt.begin(cfg, api, request, req_data, req_body, t0)
    except Exception as exc:
        rt.hook_error(exc)
        return None


def capture_on_disconnect(request: Request) -> None:
    try:
        ctx = getattr(request.state, "capture", None)
        if ctx is not None:
            ctx.finish("client_disconnect")
    except Exception:
        pass


def capture_startup(args: argparse.Namespace) -> None:
    global _capture_rt
    if _capture_rt is not None:
        return
    try:
        path = capture_prepare_config(args)
        if path is None:
            logger.info("prompt capture: off (--capture-config off)")
            return
        _capture_rt = _CaptureRuntime(path, asyncio.get_running_loop())
        logger.info("prompt capture: config=%s enabled=%s worker=%s", path, _capture_rt.cfg.enabled, os.getpid())
    except Exception as exc:
        logger.warning("prompt capture disabled for this worker: %r", exc)


def capture_shutdown() -> None:
    global _capture_rt
    rt, _capture_rt = _capture_rt, None
    if rt is not None:
        rt.shutdown()


def start(config_path: str) -> None:
    """Call once per worker inside its running asyncio event loop."""
    capture_startup(SimpleNamespace(capture_config=config_path))


def begin(endpoint: str, body: dict, raw_body: bytes,
          headers: list[tuple[bytes, bytes]] = (),
          arrival: float | None = None) -> CaptureCtx | None:
    """Capture an already-read body without a framework dependency.

    endpoint: /chat/completions or /completions; headers are lower-case ASGI pairs.
    Keep the returned context in the adapter for cancellation/non-stream finish.
    """
    request = SimpleNamespace(scope={"headers": headers}, client=None,
                              state=SimpleNamespace())
    return capture_begin(endpoint, request, body, raw_body,
                         time.time() if arrival is None else arrival)


def capture_collect_state(path: str) -> dict[str, Any]:
    """[capture] 汇总本机所有 worker 与写盘进程的状态文件（只算仍在运行的进程）。"""
    sd = path + ".state"
    workers, writers = [], []
    try:
        names = sorted(os.listdir(sd))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sd, name), encoding="utf-8") as f:
                st = json.load(f)
            os.kill(int(st["pid"]), 0)
        except Exception:
            continue
        (workers if st.get("kind") == "worker" else writers).append(st)
    totals: dict[str, int] = {}
    for st in workers + writers:
        for k, v in (st.get("counters") or {}).items():
            if isinstance(v, (int, float)):
                totals[k] = totals.get(k, 0) + v
    try:
        with open(path, encoding="utf-8") as f:
            cfg: Any = json.load(f)
    except Exception as exc:
        cfg = {"error": repr(exc)}
    return {"config_path": path, "config": cfg, "workers": workers, "writers": writers, "totals": totals}


def _cap_client_family(ua: str | None) -> str:
    u = (ua or "").lower()
    for key, name in (("opencode", "opencode"), ("claude-cli", "claude-code"), ("claude-code", "claude-code"),
                      ("cline", "cline"), ("roo", "roo-code"), ("kilo", "kilo-code"), ("cursor", "cursor"),
                      ("continue", "continue"), ("openai/python", "openai-python"), ("openai/js", "openai-node"),
                      ("langchain", "langchain"), ("litellm", "litellm"), ("curl/", "curl"),
                      ("python-requests", "python-requests"), ("python-httpx", "python-httpx"),
                      ("aiohttp", "aiohttp"), ("go-http-client", "go"), ("okhttp", "okhttp"), ("mozilla", "browser")):
        if key in u:
            return name
    return "other" if u else "none"


def _cap_msg_info(m: dict) -> tuple[str, int, int, int, int]:
    """[capture] 一条消息的 (role, 文本字符数, tool_calls 数, 非文本片段数, reasoning 字符数)。"""
    role = m.get("role") if isinstance(m.get("role"), str) else "?"
    chars = tc = media = 0
    content = m.get("content")
    if isinstance(content, str):
        chars += len(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                chars += len(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chars += len(part["text"])
            else:
                media += 1
    reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), str) else m.get("reasoning_content")
    rch = len(reasoning) if isinstance(reasoning, str) else 0
    chars += rch
    calls = m.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            tc += 1
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                if isinstance(fn.get("name"), str):
                    chars += len(fn["name"])
                args = fn.get("arguments")
                if isinstance(args, str):
                    chars += len(args)
                elif args is not None:
                    chars += len(json.dumps(args, ensure_ascii=False))
    return role, chars, tc, media, rch


def _cap_strip_big(o: Any) -> tuple[Any, int, int]:
    if isinstance(o, str) and len(o) > 256:
        return f"[stripped {len(o)} chars blake2b:{_cap_h(o.encode('utf-8', 'surrogatepass'))}]", 1, len(o)
    if isinstance(o, dict):
        out, n, b = {}, 0, 0
        for k, v in o.items():
            out[k], dn, db = _cap_strip_big(v)
            n, b = n + dn, b + db
        return out, n, b
    if isinstance(o, list):
        out_l, n, b = [], 0, 0
        for v in o:
            nv, dn, db = _cap_strip_big(v)
            out_l.append(nv)
            n, b = n + dn, b + db
        return out_l, n, b
    return o, 0, 0


def _cap_strip_media(m: dict) -> tuple[dict, int, int]:
    """[capture] keep_media=false 时把图片/音频等片段里的大字符串（base64、data URI）换成占位串。"""
    content = m.get("content")
    if not isinstance(content, list):
        return m, 0, 0
    new, n, b = None, 0, 0
    for i, part in enumerate(content):
        if isinstance(part, dict) and part.get("type") not in _CAP_TEXT_PARTS:
            np_, dn, db = _cap_strip_big(part)
            if dn:
                if new is None:
                    new = list(content)
                new[i] = np_
                n, b = n + dn, b + db
    if new is None:
        return m, 0, 0
    m2 = dict(m)
    m2["content"] = new
    return m2, n, b


def _cap_parse_output(raw: bytes, ep: str) -> dict[str, Any]:
    """[capture] 从 D 返回给客户端的原始字节里还原输出：SSE 逐行解析，或整段 JSON。"""
    text = raw.decode("utf-8", "replace")
    objs, bad, done, sse = [], 0, False, text.lstrip().startswith("data:")
    if sse:
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                continue
            try:
                objs.append(json.loads(data))
            except ValueError:
                bad += 1
    elif text.strip():
        try:
            objs.append(json.loads(text))
        except ValueError:
            bad += 1
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    finish = usage = err = None
    n_choice = 0
    for o in objs:
        if not isinstance(o, dict):
            continue
        if isinstance(o.get("usage"), dict):
            usage = o["usage"]
        if o.get("error") is not None and not o.get("choices"):
            err = o["error"]
        for ch in o.get("choices") or []:
            if not isinstance(ch, dict) or ch.get("index", 0) != 0:
                continue
            n_choice += 1
            if ep == "completions":
                if isinstance(ch.get("text"), str):
                    content.append(ch["text"])
            else:
                msg = ch.get("delta") or ch.get("message") or {}
                if isinstance(msg.get("content"), str):
                    content.append(msg["content"])
                r = msg.get("reasoning_content") if isinstance(msg.get("reasoning_content"), str) else msg.get("reasoning")
                if isinstance(r, str):
                    reasoning.append(r)
                for j, call in enumerate(msg.get("tool_calls") or []):
                    if not isinstance(call, dict):
                        continue
                    slot = calls.setdefault(call.get("index", j), {"name": "", "arguments": ""})
                    fn = call.get("function") or {}
                    if isinstance(fn.get("name"), str):
                        slot["name"] += fn["name"]
                    if isinstance(fn.get("arguments"), str):
                        slot["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    c, r = "".join(content), "".join(reasoning)
    return {
        "sse": sse, "done": done, "bad": bad, "n_choice": n_choice, "fr": finish, "usage": usage, "err": err,
        "content": c, "reasoning": r, "tool_calls": [calls[k] for k in sorted(calls)],
    }


def _cap_read_exact(f: Any, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _cap_gzip_file(path: str) -> None:
    tmp = path + ".gz.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw:
            with open(path, "rb") as src, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, path + ".gz")
        os.remove(path)
    except Exception as exc:
        print(f"[capture-writer] gzip {path} failed: {exc!r}", file=sys.stderr, flush=True)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _cap_dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


class _CapSeg:
    __slots__ = ("seq", "path", "f", "bytes", "t_open", "day", "known", "pending", "rotated_at")

    def __init__(self, seq: int, path: str, f: Any, day: str):
        self.seq, self.path, self.f, self.day = seq, path, f, day
        self.bytes = 0
        self.t_open = time.monotonic()
        self.known: set[str] = set()  # 本分段已写过的 blob/节点哈希：每个分段自带全部引用，可单独拿走分析
        self.pending: set[str] = set()  # 记录已写、结束信息还没到的请求
        self.rotated_at: float | None = None


class _CaptureWriter:
    def __init__(self, config_path: str, state_dir: str, parent: int):
        self.path, self.state_dir, self.parent = config_path, state_dir, parent
        self.pid, self.host = os.getpid(), socket.gethostname()
        self.cfg = capture_validate({})
        self.out_dir = _CapCfg(self.cfg, config_path).out_dir_abs
        self._sig: Any = None
        self.cfg_error = ""
        try:
            with open(os.path.abspath(__file__), "rb") as f:
                self.proxy_sha = hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            self.proxy_sha = ""
        self.segs: dict[int, _CapSeg] = {}
        self.cur: _CapSeg | None = None
        self.seq = 0
        self.pending: dict[str, tuple[int, str, str, str, float]] = {}  # id → (分段号, 级别, 端点, output, 到达时刻)
        self.expired: "collections.OrderedDict[str, None]" = collections.OrderedDict()  # 不再等结束信息的请求 id
        self.n = dict.fromkeys(("frames", "req", "end", "lines", "bytes", "blob_m", "blob_n", "blob_b", "dedup_m",
                                "orphan_end", "late_end", "drop_budget", "drop_nowrite", "bad_frame", "errors",
                                "write_errors", "segments", "segments_closed", "gold_ok", "gold_missing"), 0)
        self.live: dict[str, Any] = {"since": time.time(), "req": 0, "text": 0, "meta": 0, "gold": 0, "end": 0,
                                     "by_ep": {}, "by_model": {}, "by_client": {}, "by_status": {}, "pt_n": 0,
                                     "pt_sum": 0, "ct_sum": 0, "pt_hist": {}, "bb_sum": 0, "sessions": 0}
        self._sessions: set[str] = set()
        self.budget_ok, self.used = True, 0
        self.last_error = ""
        self._nowrite_until = 0.0
        self._compressors: list[threading.Thread] = []
        self._next = {"cfg": 0.0, "flush": 0.0, "stats": 0.0, "budget": 0.0, "sweep": 0.0}
        self._reload()

    def err(self, msg: str) -> None:
        self.last_error = f"{time.strftime('%H:%M:%S')} {msg}"[:800]
        print(f"[capture-writer {self.pid}] {msg}", file=sys.stderr, flush=True)

    def _reload(self) -> None:
        try:
            st = os.stat(self.path)
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            if sig == self._sig:
                return
            self._sig = sig
            with open(self.path, encoding="utf-8") as f:
                cfg = capture_validate(json.load(f))
        except Exception as exc:
            self.cfg_error = f"{type(exc).__name__}: {exc}"[:800]
            return
        self.cfg_error = ""
        new_dir = _CapCfg(cfg, self.path).out_dir_abs
        self.cfg = cfg
        if new_dir != self.out_dir:
            self.out_dir = new_dir
            self._rotate_current()  # 新数据写到新目录；旧分段等在途请求结束后关闭
            self._next["budget"] = 0.0

    # ---- 分段 ------------------------------------------------------------------------
    def _seg(self) -> _CapSeg:
        if self.cur is not None:
            return self.cur
        lt = time.localtime()
        day = time.strftime("%Y%m%d", lt)
        d = os.path.join(self.out_dir, day)
        os.makedirs(d, mode=0o700, exist_ok=True)
        self.seq += 1
        name = f"seg-{time.strftime('%Y%m%d-%H%M%S', lt)}-{self.host}-{self.pid}-{self.seq:04d}.jsonl"
        path = os.path.join(d, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        seg = _CapSeg(self.seq, path, os.fdopen(fd, "ab", buffering=1 << 20), day)
        self.segs[seg.seq] = seg
        self.cur = seg
        self.n["segments"] += 1
        self._w(seg, {"t": "h", "v": CAPTURE_FORMAT, "created": time.time(), "host": self.host,
                      "writer_pid": self.pid, "worker_pid": self.parent, "proxy_file": os.path.abspath(__file__),
                      "proxy_sha256_16": self.proxy_sha, "python": sys.version.split()[0], "cfg": self.cfg})
        return seg

    def _rotate_current(self) -> None:
        seg, self.cur = self.cur, None
        if seg is None:
            return
        seg.rotated_at = time.monotonic()
        if not seg.pending:
            self._close_seg(seg)

    def _close_seg(self, seg: _CapSeg) -> None:
        self.segs.pop(seg.seq, None)
        if self.cur is seg:
            self.cur = None
        for rid in seg.pending:
            self.pending.pop(rid, None)
            self._expire(rid)
        seg.pending.clear()
        try:
            seg.f.close()
        except OSError as exc:
            self.err(f"close {seg.path}: {exc!r}")
        self.n["segments_closed"] += 1
        if self.cfg["compress"] and os.path.exists(seg.path):
            t = threading.Thread(target=_cap_gzip_file, args=(seg.path,), daemon=False)
            t.start()
            self._compressors.append(t)

    def _expire(self, rid: str) -> None:
        self.expired[rid] = None
        while len(self.expired) > 200000:
            self.expired.popitem(last=False)

    def _w(self, seg: _CapSeg, obj: dict) -> None:
        try:
            b = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError:  # 含孤立代理项的字符串：退回 \\u 转义，json.loads 读回来完全一致
            b = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        seg.f.write(b + b"\n")
        seg.bytes += len(b) + 1
        self.n["lines"] += 1
        self.n["bytes"] += len(b) + 1

    def _blob(self, seg: _CapSeg, kind: str, h: str, d: Any) -> None:
        if h not in seg.known:
            seg.known.add(h)
            self._w(seg, {"t": "b", "h": h, "k": kind, "d": d})
            self.n["blob_b"] += 1

    # ---- 事件 ------------------------------------------------------------------------
    def handle(self, mb: bytes, pb: bytes) -> None:
        self.n["frames"] += 1
        try:
            meta = json.loads(mb)
        except ValueError:
            self.n["bad_frame"] += 1
            return
        if not self.budget_ok:
            self.n["drop_budget"] += 1
            return
        if time.monotonic() < self._nowrite_until:
            self.n["drop_nowrite"] += 1
            return
        try:
            if meta.get("ev") == "req":
                self._on_req(meta, pb)
            elif meta.get("ev") == "end":
                self._on_end(meta, pb)
        except OSError as exc:  # 磁盘满、目录不可写等：丢掉所有打开的分段，10 秒后再试
            self.n["write_errors"] += 1
            self.err(f"write error: {exc!r}")
            for seg in list(self.segs.values()):
                self._close_seg(seg)
            self._nowrite_until = time.monotonic() + 10.0
        except Exception as exc:
            self.n["errors"] += 1
            self.err(f"event error: {exc!r}")
        if self.cfg["flush_s"] <= 0:  # 默认每个事件写完就 flush：写盘进程被 kill -9 时最多丢在途的几个事件
            for seg in list(self.segs.values()):
                try:
                    seg.f.flush()
                except OSError as exc:
                    self.n["write_errors"] += 1
                    self.err(f"flush {seg.path}: {exc!r}")
                    self._close_seg(seg)

    def _on_req(self, meta: dict, body: bytes) -> None:
        self.n["req"] += 1
        text = meta.get("lvl") == "text"
        ep = meta.get("ep")
        rec: dict[str, Any] = {"t": "r"}
        for k in ("id", "ts", "ep", "lvl", "gold", "sess", "ss", "ip", "hd", "worker", "why"):
            rec[k] = meta.get(k)
        if meta.get("fol"):
            rec["fol"] = True
        rec["bb"] = len(body)
        seg = self._seg()
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            rec["err"] = "bad_json"
            self._w(seg, rec)
            return
        model = data.get("model")
        rec["model"] = model if isinstance(model, str) else None
        rec["stream"] = bool(data.get("stream", False))
        rec["ko"] = list(data.keys())
        if ep == "chat":
            msgs = data.get("messages")
            if isinstance(msgs, list):
                parent, ms_n, ms_b = None, 0, 0
                for i, m in enumerate(msgs):
                    if isinstance(m, dict) and not self.cfg["keep_media"]:
                        m, dn, db = _cap_strip_media(m)
                        ms_n, ms_b = ms_n + dn, ms_b + db
                    mb_ = _cap_dumps(m)
                    mh = _cap_h(mb_)
                    node = _cap_h(f"{parent}:{mh}".encode())
                    if text:
                        if mh not in seg.known:
                            seg.known.add(mh)
                            self._w(seg, {"t": "m", "h": mh, "d": m})
                            self.n["blob_m"] += 1
                        else:
                            self.n["dedup_m"] += 1
                    if node not in seg.known:
                        seg.known.add(node)
                        role, chars, tc, media, rch = _cap_msg_info(m) if isinstance(m, dict) else ("?", 0, 0, 0, 0)
                        self._w(seg, {"t": "n", "h": node, "p": parent, "m": mh, "i": i, "r": role, "c": chars,
                                      "b": len(mb_), "tc": tc, "md": media, "rc": rch})
                        self.n["blob_n"] += 1
                    parent = node
                rec["ml"], rec["nm"] = parent, len(msgs)
                if ms_n:
                    rec["ms"] = [ms_n, ms_b]
            else:
                rec["err"] = "no_messages"
        elif "prompt" in data:
            p = data["prompt"]
            pb_ = _cap_dumps(p)
            ph = _cap_h(pb_)
            if isinstance(p, str):
                pk, pc = "str", len(p)
            elif isinstance(p, list) and all(isinstance(x, str) for x in p):
                pk, pc = "strs", sum(len(x) for x in p)
            elif isinstance(p, list) and all(isinstance(x, int) for x in p):
                pk, pc = "ids", len(p)
            elif isinstance(p, list) and all(isinstance(x, list) for x in p):
                pk, pc = "idss", sum(len(x) for x in p)
            else:
                pk, pc = "other", len(pb_)
            if text:
                self._blob(seg, "prompt", ph, p)
            rec["prompt"], rec["pk"], rec["pc"], rec["pb"] = ph, pk, pc, len(pb_)
        tools = data.get("tools")
        if tools is not None:
            tb = _cap_dumps(tools)
            th = _cap_h(tb)
            if text:
                self._blob(seg, "tools", th, tools)
            rec["tools"], rec["nt"], rec["tb"] = th, len(tools) if isinstance(tools, list) else -1, len(tb)
        params: dict[str, Any] = {}
        param_meta: dict[str, Any] = {}
        for k, v in data.items():
            if k in ("messages", "prompt", "tools"):
                continue
            vb = _cap_dumps(v)
            if not text and not _cap_meta_param_allowed(k, v):
                param_meta[k] = {"type": type(v).__name__, "bytes": len(vb), "hash": _cap_h(vb)}
            elif len(vb) > _CAP_PARAM_INLINE:
                vh = _cap_h(vb)
                if text:
                    self._blob(seg, "param", vh, v)
                params[k] = {"$blob": vh, "$bytes": len(vb)}
            else:
                params[k] = v
        rec["params"] = params
        if param_meta:
            rec["param_meta"] = param_meta
        self._w(seg, rec)
        rid = str(meta.get("id"))
        seg.pending.add(rid)
        self.pending[rid] = (seg.seq, "text" if text else "meta", str(ep), str(meta.get("out")), time.monotonic())
        live = self.live
        live["req"] += 1
        live["text" if text else "meta"] += 1
        live["gold"] += 1 if meta.get("gold") else 0
        live["bb_sum"] += len(body)
        for key, val in (("by_ep", ep), ("by_model", rec["model"]),
                         ("by_client", _cap_client_family((meta.get("hd") or {}).get("user-agent")))):
            bucket = live[key]
            val = str(val)
            if val in bucket or len(bucket) < 50:
                bucket[val] = bucket.get(val, 0) + 1
        sess = meta.get("sess")
        if sess and len(self._sessions) < 200000 and sess not in self._sessions:
            self._sessions.add(sess)
            live["sessions"] = len(self._sessions)

    def _on_end(self, meta: dict, payload: bytes) -> None:
        self.n["end"] += 1
        rid = str(meta.get("id"))
        info = self.pending.pop(rid, None)
        seg = self.segs.get(info[0]) if info else None
        e: dict[str, Any] = {"t": "e", "id": rid, "st": meta.get("st")}
        if seg is None:
            if info is not None or self.expired.pop(rid, "no") is None:
                e["late"] = True  # 请求记录所在的分段已经不等它了（超过 pending_max_min）
                self.n["late_end"] += 1
            else:
                e["orphan"] = True  # 请求记录是之前那个写盘进程写的（写盘进程中途重启过）
                self.n["orphan_end"] += 1
            seg = self._seg()
        lvl, ep, out_mode = (info[1], info[2], info[3]) if info else \
            (str(meta.get("lvl") or "meta"), str(meta.get("ep") or "chat"), str(meta.get("out") or "stats"))
        t0 = meta.get("t0")
        for k in ("p_t0", "p_t1", "d_first", "t_end", "p_err", "pt", "ct", "pn", "dn", "rc", "o_cut"):
            e[k] = meta.get(k)
        if meta.get("rid"):
            e["rid"] = meta["rid"]

        def ms(a: Any, b: Any) -> float | None:
            return round((b - a) * 1000.0, 3) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None

        e["p_ms"], e["ttft_ms"], e["e2e_ms"] = ms(meta.get("p_t0"), meta.get("p_t1")), ms(t0, meta.get("d_first")), \
            ms(t0, meta.get("t_end"))
        p_len, o_len, t_len = int(meta.get("p_len") or 0), int(meta.get("o_len") or 0), int(meta.get("t_len") or 0)
        p_raw, o_raw = payload[:p_len], payload[p_len:p_len + o_len]
        t_raw = payload[p_len + o_len:p_len + o_len + t_len]
        if p_raw:
            self._gold(seg, e, p_raw)
        if o_raw or t_raw:
            o = _cap_parse_output(o_raw, ep)
            if t_raw:  # 输出超过 max_output_kb：开头与末尾之间有缺口；finish_reason、usage、[DONE] 取自末尾
                t = _cap_parse_output(t_raw, ep)
                o["fr"] = t["fr"] or o["fr"]
                o["usage"] = t["usage"] or o["usage"]
                o["done"] = t["done"] or o["done"]
                o["err"] = t["err"] if t["err"] is not None else o["err"]
                o["n_choice"] += t["n_choice"]
                o["bad"] += t["bad"]
                o["sse"] = o["sse"] or t["sse"]
            e["fr"], e["oc"], e["orc"], e["otc"] = o["fr"], len(o["content"]), len(o["reasoning"]), len(o["tool_calls"])
            e["o_chunks"], e["o_done"], e["o_bad"], e["o_sse"] = o["n_choice"], o["done"], o["bad"], o["sse"]
            if isinstance(o["usage"], dict):
                e["ot"] = o["usage"].get("completion_tokens")
            if o["err"] is not None:
                e["o_err"] = str(o["err"])[:500]
            if e["st"] == "ok" and not o["fr"]:
                e["st"] = "incomplete"  # 上游在流中途出错时会吞掉异常、正常结束响应；这里据输出内容补标
            if out_mode == "text" and lvl == "text":
                od = {"content": o["content"], "reasoning": o["reasoning"], "tool_calls": o["tool_calls"]}
                oh = _cap_h(_cap_dumps(od))
                self._blob(seg, "out", oh, od)
                e["out"] = oh
        self._w(seg, e)
        seg.pending.discard(rid)
        if seg.rotated_at is not None and not seg.pending:
            self._close_seg(seg)
        live = self.live
        live["end"] += 1
        st = str(e["st"])
        if st in live["by_status"] or len(live["by_status"]) < 50:
            live["by_status"][st] = live["by_status"].get(st, 0) + 1
        pt = meta.get("pt")
        if isinstance(pt, int):
            live["pt_n"] += 1
            live["pt_sum"] += pt
            live["ct_sum"] += meta.get("ct") if isinstance(meta.get("ct"), int) else 0
            label = next(lbl for lim, lbl in _CAP_PT_BUCKETS if pt < lim)
            live["pt_hist"][label] = live["pt_hist"].get(label, 0) + 1

    def _gold(self, seg: _CapSeg, e: dict, p_raw: bytes) -> None:
        try:
            j = json.loads(p_raw)
        except ValueError:
            e["gold_err"] = "bad_json"
            self.n["gold_missing"] += 1
            return
        ids = j.get("prompt_token_ids") if isinstance(j, dict) else None
        if ids is None and isinstance(j, dict) and isinstance(j.get("choices"), list) and j["choices"]:
            ch0 = j["choices"][0]
            ids = ch0.get("prompt_token_ids") if isinstance(ch0, dict) else None
        if isinstance(ids, list) and all(isinstance(x, int) for x in ids):
            hs = []
            for i in range(0, len(ids), _CAP_IDS_CHUNK):
                chunk = ids[i:i + _CAP_IDS_CHUNK]
                h = _cap_h(b"ids:" + _cap_dumps(chunk))
                self._blob(seg, "ids", h, chunk)
                hs.append(h)
            e["ids"], e["ids_n"] = hs, len(ids)
            self.n["gold_ok"] += 1
        else:
            e["gold_err"] = "no_prompt_token_ids"  # 后端 vLLM 版本不支持 return_token_ids 时就是这样
            self.n["gold_missing"] += 1
        ptxt = j.get("prompt_text") if isinstance(j, dict) else None
        if isinstance(ptxt, str):
            hs = []
            for i in range(0, len(ptxt), _CAP_PTXT_CHUNK):
                chunk = ptxt[i:i + _CAP_PTXT_CHUNK]
                h = _cap_h(b"ptxt:" + chunk.encode("utf-8", "surrogatepass"))
                self._blob(seg, "ptxt", h, chunk)
                hs.append(h)
            e["ptxt"], e["ptxt_n"] = hs, len(ptxt)

    # ---- 周期任务 ----------------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        nx = self._next
        if now >= nx["cfg"]:
            self._reload()
            nx["cfg"] = now + 1.0
        cur = self.cur
        if cur is not None:
            if (cur.bytes >= self.cfg["rotate_mb"] * 2**20 or now - cur.t_open >= self.cfg["rotate_min"] * 60
                    or time.strftime("%Y%m%d") != cur.day):
                self._rotate_current()
        if now >= nx["flush"]:
            for seg in list(self.segs.values()):
                try:
                    seg.f.flush()
                except OSError as exc:
                    self.n["write_errors"] += 1
                    self.err(f"flush {seg.path}: {exc!r}")
                    self._close_seg(seg)
            nx["flush"] = now + max(self.cfg["flush_s"], 1.0)
        if now >= nx["sweep"]:  # 在途太久的请求：不再等它的结束信息
            limit = self.cfg["pending_max_min"] * 60
            for seg in list(self.segs.values()):
                if seg.rotated_at is not None and now - seg.rotated_at > limit:
                    self._close_seg(seg)
            for rid in [r for r, v in self.pending.items() if now - v[4] > limit]:
                info = self.pending.pop(rid)
                self._expire(rid)
                seg = self.segs.get(info[0])
                if seg is not None:
                    seg.pending.discard(rid)
            self._compressors = [t for t in self._compressors if t.is_alive()]
            nx["sweep"] = now + 30.0
        if now >= nx["budget"]:
            self.used = _cap_dir_size(self.out_dir) if os.path.isdir(self.out_dir) else 0
            ok = self.used < self.cfg["max_total_gb"] * 2**30
            if ok != self.budget_ok:
                self.err(f"disk budget {'ok again' if ok else 'exceeded'}: {self.used / 2**30:.2f} GB used")
            self.budget_ok = ok
            nx["budget"] = now + 30.0
        if now >= nx["stats"]:
            self._write_stats()
            nx["stats"] = now + 2.0

    def _write_stats(self) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            cur = self.cur
            st = {
                "kind": "writer", "pid": self.pid, "parent": self.parent, "host": self.host, "updated": time.time(),
                "out_dir": self.out_dir, "open_segments": [s.path for s in self.segs.values()],
                "cur_segment_mb": round(cur.bytes / 2**20, 3) if cur else 0.0, "budget_ok": self.budget_ok,
                "used_gb": round(self.used / 2**30, 3), "pending": len(self.pending), "config_error": self.cfg_error,
                "last_error": self.last_error, "counters": self.n, "live": self.live,
            }
            _cap_atomic_json(os.path.join(self.state_dir, f"writer-{self.pid}.json"), st)
        except Exception:
            pass

    def close(self) -> None:
        for seg in list(self.segs.values()):
            self._close_seg(seg)
        for t in self._compressors:
            t.join()
        try:
            os.remove(os.path.join(self.state_dir, f"writer-{self.pid}.json"))
        except OSError:
            pass


def _capture_writer_main(argv: list[str]) -> int:
    config_path, state_dir, parent = argv[0], argv[1], int(argv[2])
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C 不打断写盘；父进程退出时管道 EOF，这里写完再退
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    w = _CaptureWriter(config_path, state_dir, parent)
    q: "queue.Queue[tuple[bytes, bytes] | None]" = queue.Queue(maxsize=16)  # 读前队列小：被 kill 时丢得少

    def reader() -> None:
        f = sys.stdin.buffer
        try:
            while True:
                head = _cap_read_exact(f, _CAP_FRAME.size)
                if head is None:
                    break
                magic, ml, pl = _CAP_FRAME.unpack(head)
                if magic != _CAP_MAGIC:
                    w.err("bad frame magic; stream out of sync, exiting")
                    break
                mb = _cap_read_exact(f, ml)
                pb = _cap_read_exact(f, pl) if pl else b""
                if mb is None or pb is None:
                    break
                q.put((mb, pb))
        except Exception as exc:
            w.err(f"reader error: {exc!r}")
        finally:
            q.put(None)

    threading.Thread(target=reader, name="capture-reader", daemon=True).start()
    while True:
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            item = ()
        if item is None:
            break
        if item:
            w.handle(*item)
        w.tick()
        if stop.is_set():
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item:
                    w.handle(*item)
            break
    w.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--capture-writer":
        sys.exit(_capture_writer_main(sys.argv[2:]))
    sys.exit("Import this module from the proxy; use capturectl.py for configuration.")
