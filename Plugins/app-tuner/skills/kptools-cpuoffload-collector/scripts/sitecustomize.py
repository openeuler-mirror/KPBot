#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kptools-cpuoffload-collector L1 埋点注入器（sitecustomize 机制）。

**文件名必须是 sitecustomize.py**——Python 解释器启动时自动 import 该
名字的模块（见 Python 文档 site 模块节）；拷到注入目录后改名即失效。

用法（零改码）：
  1. 拷贝到注入目录（目录名任意，文件名不能改）：
     mkdir -p /tmp/cohook
     cp scripts/sitecustomize.py /tmp/cohook/
     export PYTHONPATH=/tmp/cohook:$PYTHONPATH
     export CPUOFFLOAD_HOOK=1
     export CPUOFFLOAD_HOOK_OUT=/mnt/workspace/co_hooks
  2. 正常启动 torchtitan-npu 训练。目标模块首次 import 完成后立即 patch：
     - PinnedFlatPool._grow / .allocate / .pin_copy  （fsdp chunk：alloc/copy）
     - CpuMemoryPool._allocate_slab / .allocate / .free（nova slab：alloc/free）
     - PinnedCpuStorage.allocate / .free（classmethod，owner 生命周期）
     - torch.optim.Optimizer.step（opt_step_begin/end mark）
  3. 产出（CPUOFFLOAD_HOOK_OUT 目录，逐 pid）：
     alloc.<pid>.jsonl  kind=alloc|copy|free, source, method, size,
                        result_ptr/arg_ptr（配对 alloc↔free）, tid, caller, dur_us
     marks.<pid>.jsonl  kind=hook_installed|pinned_pool_patched|mempool_patched|
                        optimizer_step_patched|opt_step_begin|opt_step_end|patch_error

设计纪律：
  - hook 只记日志并调用原函数；任何异常吞掉，绝不影响训练；
  - meta_path finder 插在 sys.meta_path **首位**并只包 4 个目标模块的
    loader（此前版本 append 到末尾，位于 PathFinder 之后永不被查询——
    已修复）；非目标 import 一律原样放行（返回 None）；
  - 无 LD_PRELOAD——两个 Python 池类已是所有 pin 的唯一出口。
"""
import functools
import importlib.machinery
import inspect
import json
import os
import sys
import threading
import time
import traceback

__version__ = "1.1.0"

ENABLED = os.environ.get("CPUOFFLOAD_HOOK", "0") == "1"
OUT_DIR = os.environ.get("CPUOFFLOAD_HOOK_OUT", "/tmp/cpuoffload_hooks")


def _now_ns():
    """time.time_ns 需 py3.7；3.6 走毫秒级 fallback。"""
    tns = getattr(time, "time_ns", None)
    return tns() if callable(tns) else int(time.time() * 1000000000)


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
        rec["t_ns"] = _now_ns()
        rec["pid"] = os.getpid()
        with _LOCK:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _mark(kind, **kw):
        _log(_MARKS, {"kind": kind, **kw})

    _mark("hook_installed", version=__version__)

    _PENDING = set()

    # ------------------------------------------------------------ patch 安装
    def _wrap(cls, method_name, kind):
        """包一个方法：调用后按 kind 记一条 alloc 事件。

        kind 由注册方传入（alloc/copy/free——free 误记 alloc 的修复）；
        result_ptr/arg_ptr 提供 alloc↔free 的配对标识。
        """
        raw = inspect.getattr_static(cls, method_name)
        is_classmethod = isinstance(raw, classmethod)
        # classmethod 必须取原始函数：getattr 返回的是已绑定 cls 的方法，
        # 再经 wrapper(self=cls, ...) 调用会双重绑定
        fn = raw.__func__ if is_classmethod else getattr(cls, method_name)
        if fn is None or getattr(fn, "_cpuoffload_hooked", False):
            return False

        @functools.wraps(fn)
        def wrapper(self, *a, **kw):
            t0 = _now_ns()
            result = fn(self, *a, **kw)
            try:
                size = a[0] if a else None
                if hasattr(size, "numel"):
                    size = size.numel() * size.element_size()
                elif not isinstance(size, (int, float, type(None))):
                    size = None
                result_ptr = None
                if hasattr(result, "data_ptr"):
                    try:
                        result_ptr = hex(result.data_ptr())
                    except Exception:
                        pass
                arg_ptr = None
                if a and hasattr(a[0], "data_ptr"):
                    try:
                        arg_ptr = hex(a[0].data_ptr())
                    except Exception:
                        pass
                src = self.__name__ if isinstance(self, type) \
                    else type(self).__name__
                rec = {"kind": kind, "source": src, "method": method_name,
                       "size": size, "tid": threading.get_ident(),
                       "caller": _caller(),
                       "dur_us": (_now_ns() - t0) // 1000}
                if result_ptr:
                    rec["result_ptr"] = result_ptr
                if arg_ptr:
                    rec["arg_ptr"] = arg_ptr
                _log(_ALLOC, rec)
            except Exception:
                pass
            return result
        wrapper._cpuoffload_hooked = True
        setattr(cls, method_name,
                classmethod(wrapper) if is_classmethod else wrapper)
        return True

    def _caller():
        stack = traceback.extract_stack(limit=6)
        for fr in reversed(stack[:-1]):
            if "cpu_offload" in fr.filename or "novaswap" in fr.filename:
                return "%s:%d" % (fr.filename, fr.lineno)
        return stack[-2].filename if len(stack) > 1 else "?"

    def _patch_pinned_pool(mod):
        cls = mod.PinnedFlatPool
        n = 0
        for m, k in (("_grow", "alloc"), ("allocate", "alloc"),
                     ("pin_copy", "copy")):
            n += 1 if _wrap(cls, m, k) else 0
        _mark("pinned_pool_patched", hooks=n)  # 逐个安装，无 or 短路

    def _patch_mem_pool(mod):
        n = 0
        pool_cls = getattr(mod, "CpuMemoryPool", None)
        if pool_cls is not None:
            for m, k in (("_allocate_slab", "alloc"), ("allocate", "alloc"),
                         ("free", "free")):
                n += 1 if _wrap(pool_cls, m, k) else 0
        storage_cls = getattr(mod, "PinnedCpuStorage", None)
        if storage_cls is not None:
            for m, k in (("allocate", "alloc"), ("free", "free")):
                n += 1 if _wrap(storage_cls, m, k) else 0
        _mark("mempool_patched", hooks=n)

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

    _PATCHERS = {
        "torchtitan_npu.extensions.cpu_offload.pinned_pool": _patch_pinned_pool,
        "torchtitan_npu.extensions.novaswap.storage.pinned_cpu_memory_pool":
            _patch_mem_pool,
        "torch.optim": _patch_optimizer_step,
    }
    _PENDING.update(_PATCHERS)

    # ---------------------------------------------------- import 时机 patch
    def _apply_patcher(name, mod):
        try:
            _PATCHERS[name](mod)
        except Exception:
            _mark("patch_error", module=name,
                  err=traceback.format_exc(limit=3))

    class _WrapLoader:
        """真 loader 的代理：exec_module 完成后立即执行该模块的 patch。"""

        def __init__(self, real, fullname):
            self._real, self._name = real, fullname

        def create_module(self, spec):
            cm = getattr(self._real, "create_module", None)
            return cm(spec) if callable(cm) else None

        def exec_module(self, module):
            self._real.exec_module(module)
            _apply_patcher(self._name, module)

    class _PostImportFinder:
        """目标模块 import 时返回包过 loader 的 spec。

        必须插在 sys.meta_path 首位：默认 meta_path 末尾是 PathFinder，
        append 在其后永远不会被询问。非目标模块返回 None（原样放行，
        交给 Builtin/Frozen/PathFinder 处理）。
        """

        def find_spec(self, fullname, path=None, target=None):
            if fullname not in _PENDING:
                return None
            _PENDING.discard(fullname)
            mod = sys.modules.get(fullname)
            if mod is not None:  # 已导入（site 晚于目标的罕见场景）
                _apply_patcher(fullname, mod)
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or getattr(spec, "loader", None) is None:
                return None
            spec.loader = _WrapLoader(spec.loader, fullname)
            return spec

    # site 运行时已导入的目标模块直接补装
    for _n in list(_PENDING):
        if _n in sys.modules:
            _PENDING.discard(_n)
            _apply_patcher(_n, sys.modules[_n])
    sys.meta_path.insert(0, _PostImportFinder())
