#!/usr/bin/env python3
"""LLM / embedding / rerank 调用的录制-回放缓存。

用途：把"Agent 记忆系统"基准里的模型调用变成确定性输入，从而得到可反复对拍、
不受网络与采样抖动影响的延迟基线。缓存键是 (目标名, 方法名, 参数) 的 sha256，
与 API key、模型地址无关；命中即返回，不构造请求。

三种模式：
  record  每次都调用真实客户端并覆盖写入（即使键已存在；用于首建或重录）
  replay  只读缓存；miss 直接抛错（用于 CI 门禁，防止偷偷出网）
  auto    命中就返回，miss 才调用真实客户端并回填（默认）

用法 1（进程内包装，推荐）：
    from llm_replay_cache import ReplayCache
    cache = ReplayCache("cache/canon.json", mode="replay")
    llm = cache.wrap(raw_llm, name="llm")
    emb = cache.wrap(raw_embedder, name="embed")
    ...
    print(cache.stats())        # {'hit': 312, 'miss': 0}
    assert cache.miss == 0

用法 2（命令行维护）：
    llm_replay_cache.py --verify cache/canon.json
    llm_replay_cache.py --merge a.json b.json -o canon.json
    llm_replay_cache.py --md5 cache/canon.json

约束：被缓存的方法必须是 async（协程），返回值必须能 JSON 序列化。
不能序列化时传 encoder / decoder（值 -> dict，dict -> 值）。
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import threading

__all__ = ["ReplayCache", "cache_key"]


def cache_key(target, method, args, kwargs):
    payload = json.dumps(
        {"target": target, "method": method, "args": args, "kwargs": kwargs},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplayCache:
    def __init__(self, path, mode="auto", encoder=None, decoder=None):
        if mode not in ("record", "replay", "auto"):
            raise ValueError(f"unknown mode: {mode}")
        self.path = path
        self.mode = mode
        self.encoder = encoder or (lambda v: v)
        self.decoder = decoder or (lambda v: v)
        self._data = {}
        self._lock = threading.Lock()
        self.hit = 0
        self.miss = 0
        self._dirty = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)

    def __len__(self):
        return len(self._data)

    def stats(self):
        return {"hit": self.hit, "miss": self.miss, "entries": len(self._data)}

    def save(self):
        if not self._dirty:
            return
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.path)

    def wrap(self, client, name="client"):
        return _Wrapper(self, client, name)


class _Wrapper:
    """把任意对象的方法调用套上缓存；属性透传，异步方法被包裹。"""

    def __init__(self, cache, client, name):
        object.__setattr__(self, "_cache", cache)
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_name", name)

    def __getattr__(self, item):
        attr = getattr(object.__getattribute__(self, "_client"), item)
        if not asyncio.iscoroutinefunction(attr):
            return attr
        cache = object.__getattribute__(self, "_cache")
        name = object.__getattribute__(self, "_name")

        async def call(*args, **kwargs):
            key = cache_key(name, item, args, kwargs)
            if cache.mode == "record":
                # record 一律走真实客户端并覆盖旧值，否则重录会静默保留过期响应
                cache.miss += 1
                result = await attr(*args, **kwargs)
                with cache._lock:
                    cache._data[key] = cache.encoder(result)
                    cache._dirty = True
                return result
            with cache._lock:
                # 用 key 是否存在判命中：被缓存方法返回 None 时，值是 JSON null
                found = key in cache._data
                cached = cache._data.get(key)
            if found:
                cache.hit += 1
                return cache.decoder(cached)
            if cache.mode == "replay":
                cache.miss += 1
                raise KeyError(
                    f"cache miss in replay mode: target={name} method={item} "
                    f"key={key[:16]}… (entries={len(cache._data)})"
                )
            cache.miss += 1
            result = await attr(*args, **kwargs)
            with cache._lock:
                cache._data[key] = cache.encoder(result)
                cache._dirty = True
            return result

        return call


def _cli(argv=None):
    p = argparse.ArgumentParser(description="LLM 调用录制-回放缓存维护工具")
    p.add_argument("--verify", metavar="FILE", help="统计条目数与重复键")
    p.add_argument("--merge", nargs="+", metavar="FILE", help="合并多个缓存文件，后写覆盖先写")
    p.add_argument("-o", "--output", help="--merge 的输出文件")
    p.add_argument("--md5", metavar="FILE", help="打印 md5（用于跨机一致性核对）")
    a = p.parse_args(argv)

    if a.md5:
        h = hashlib.md5()
        with open(a.md5, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        print(h.hexdigest())
        return 0

    if a.verify:
        with open(a.verify, "r", encoding="utf-8") as f:
            d = json.load(f)
        keys = list(d.keys())
        print(f"entries: {len(keys)}  unique: {len(set(keys))}  size: {os.path.getsize(a.verify)}")
        return 0 if len(keys) == len(set(keys)) else 1

    if a.merge:
        if not a.output:
            p.error("--merge requires -o/--output")
        merged = {}
        for path in a.merge:
            with open(path, "r", encoding="utf-8") as f:
                merged.update(json.load(f))
        with open(a.output, "w", encoding="utf-8") as f:
            json.dump(merged, f)
        print(f"merged {len(a.merge)} file(s) -> {a.output}: {len(merged)} entries")
        return 0

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
