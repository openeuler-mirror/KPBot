#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-cpuoffload-collector L1 埋点注入器。

用法（零改码）：
  1. 把本文件与目录放入训练环境的 PYTHONPATH（如注入目录注入：
     mkdir -p /tmp/cohook && cp sitecustomize_cpuoffload.py /tmp/cohook/
     export PYTHONPATH=/tmp/cohook:$PYTHONPATH
     export CPUOFFLOAD_HOOK=1
     export CPUOFFLOAD_HOOK_OUT=/tmp/cpuoffload_hooks）
  2. 正常启动 torchtitan-npu 训练。模块首次被 import 时自动 patch：
     - PinnedFlatPool._grow / .pin_copy          （fsdp chunk 申请/请求）
     - CpuMemoryPool._allocate_slab / .free      （nova slab）
     - PinnedCpuStorage.allocate / .free         （owner 生命周期）
     - Optimizer.step（前后 mark，init/稳态边界）
     - AffinityExtension.start / .stop           （绑核窗口 mark）
  3. 产出（CPUOFFLOAD_HOOK_OUT 目录）：
     alloc.<pid>.jsonl   kind=alloc|req|free, source, size, va, tid, caller
     marks.<pid>.jsonl   kind=opt_step_begin/end, affinity_start/stop, hook_installed

设计纪律：所有 hook 只记日志并调用原函数，异常吞掉绝不影响训练；
不用 LD_PRELOAD——两个 Python 入口已是所有 pin 的唯一出口。
"""
import functools
import json
import os
import sys
import threading
import time
import traceback

ENABLED = os.environ.get("CPUOFFLOAD_HOOK", "0") == "1"
OUT_DIR = os.environ.get("CPUOFFLOAD_HOOK_OUT", "/tmp/cpuoffload_hooks")

if not ENABLED:
    pass  # 静默：未开开关时不做任何事
else:
    os.makedirs(OUT_DIR, exist_ok=True)
    _PID = str(os.getpid())
    _ALLOC = os.path.join(OUT_DIR, "alloc.%s.jsonl" % _PID)
    _MARKS = os.path.join(OUT_DIR, "marks.%s.jsonl" % _PID)
    _LOCK = threading.Lock()

    def _log(path, record):
        rec = dict(record)
        rec["t_ns"] = time.time_ns()
        rec["pid"] = os.getpid()
        with _LOCK:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _mark(kind, **kw):
        _log(_MARKS, {"kind": kind, **kw})

    _mark("hook_installed")

    _PENDING = set()
    _PATCHED = set()

    class _PostImportFinder:
        """目标模块首次 import 后立即执行其 patch 函数。"""

        def find_spec(self, fullname, path=None, target=None):
            if fullname in _PENDING and fullname not in _PATCHED:
                _PENDING.discard(fullname)
                _PATCHED.add(fullname)
                import importlib.util
                spec = importlib.util.find_spec(fullname)
                if spec is None:
                    return None
                orig_exec = spec.loader.exec_module

                def exec_module(module, _orig=orig_exec, _name=fullname):
                    _orig(module)
                    try:
                        _PATCHERS[_name](module)
                    except Exception:
                        _mark("patch_error", module=_name,
                              err=traceback.format_exc(limit=3))

                spec.loader.exec_module = exec_module
                return spec
            return None

    def _wrap(cls, method_name):
        fn = getattr(cls, method_name, None)
        if fn is None or getattr(fn, "_cpuoffload_hooked", False):
            return False

        @functools.wraps(fn)
        def wrapper(self, *a, **kw):
            t0 = time.time_ns()
            result = fn(self, *a, **kw)
            try:
                size = a[0] if a else 0
                if hasattr(size, "numel"):
                    size = size.numel() * size.element_size()
                va = None
                try:
                    va = hex(self._slabs[next(iter(self._slabs))].data_ptr()) \
                        if getattr(self, "_slabs", None) else None
                except Exception:
                    va = None
                _log(_ALLOC, {"kind": "alloc", "source": type(self).__name__,
                              "method": method_name, "size": size, "va": va,
                              "tid": threading.get_ident(),
                              "caller": _caller(), "dur_us": (time.time_ns() - t0) // 1000})
            except Exception:
                pass
            return result
        wrapper._cpuoffload_hooked = True
        setattr(cls, method_name, wrapper)
        return True

    def _caller():
        stack = traceback.extract_stack(limit=6)
        for fr in reversed(stack[:-1]):
            if "cpu_offload" in fr.filename or "novaswap" in fr.filename:
                return "%s:%d" % (fr.filename, fr.lineno)
        return stack[-2].filename if len(stack) > 1 else "?"

    def _patch_pinned_pool(mod):
        cls = mod.PinnedFlatPool
        changed = _wrap(cls, "_grow") or _wrap(cls, "allocate")
        if hasattr(mod, "_ORIG_PIN_MEMORY"):
            _mark("pinned_pool_patched")

    def _patch_mem_pool(mod):
        cls = mod.CpuMemoryPool
        _wrap(cls, "_allocate_slab")
        if hasattr(cls, "free"):
            _wrap(cls, "free")
        _mark("mempool_patched")

    def _patch_optimizer_step(mod):
        import torch
        _orig_step = torch.optim.Optimizer.step

        _state = {"n": 0}

        @functools.wraps(_orig_step)
        def step_wrapper(self, *a, **kw):
            _state["n"] += 1
            _mark("opt_step_begin", step=_state["n"])
            r = _orig_step(self, *a, **kw)
            _mark("opt_step_end", step=_state["n"])
            return r
        step_wrapper._cpuoffload_hooked = True
        torch.optim.Optimizer.step = step_wrapper
        _mark("optimizer_step_patched")

    def _patch_affinity(mod):
        ext = mod.AffinityExtension
        orig_start, orig_stop = ext.start, ext.stop
        if getattr(orig_start, "_cpuoffload_hooked", False):
            return

        @functools.wraps(orig_start)
        def start(cls):
            r = orig_start()
            _mark("affinity_start")
            return r
        start._cpuoffload_hooked = True

        @functools.wraps(orig_stop)
        def stop(self):
            _mark("affinity_stop")
            return orig_stop(self)
        stop._cpuoffload_hooked = True

        ext.start = classmethod(start)
        ext.stop = stop
        _mark("affinity_patched")

    _PATCHERS = {
        "torchtitan_npu.extensions.cpu_offload.pinned_pool": _patch_pinned_pool,
        "torchtitan_npu.extensions.novaswap.storage.pinned_cpu_memory_pool":
            _patch_mem_pool,
        "torch.optim": _patch_optimizer_step,
        "torchtitan_npu.extensions.affinity": _patch_affinity,
    }
    _PENDING.update(_PATCHERS)

    # meta_path 后置：追加到末尾（正常 import 不受影响，模块加载后立刻 patch）
    if _PostImportFinder not in [type(f) for f in sys.meta_path]:
        sys.meta_path.append(_PostImportFinder())
