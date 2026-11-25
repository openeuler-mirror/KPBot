"""llm_replay_cache 自测：record -> replay 等价性、replay 模式 miss 报错、CLI 合并。"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from llm_replay_cache import ReplayCache, cache_key  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, prompt, temperature=0.0):
        self.calls += 1
        return {"text": f"answer:{prompt}", "t": temperature}

    def sync_helper(self):
        return "plain-attr"


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    async def create_batch(self, texts):
        self.calls += 1
        return [[0.1, 0.2, float(len(t))] for t in texts]


def test_record_then_replay(tmp):
    path = os.path.join(tmp, "c.json")
    raw = FakeLLM()
    rec = ReplayCache(path, mode="record")
    llm = rec.wrap(raw, name="llm")
    out = asyncio.run(llm.complete("q1"))
    assert out["text"] == "answer:q1"
    assert raw.calls == 1
    rec.save()

    raw2 = FakeLLM()
    rep = ReplayCache(path, mode="replay")
    llm2 = rep.wrap(raw2, name="llm")
    assert asyncio.run(llm2.complete("q1")) == out
    assert raw2.calls == 0, "replay 不应调用真实客户端"
    assert rep.stats()["hit"] == 1 and rep.miss == 0

    # 不同 target 名 -> 不同键
    other = ReplayCache(path, mode="replay")
    try:
        asyncio.run(other.wrap(raw2, name="rerank").complete("q1"))
    except KeyError:
        pass
    else:
        raise AssertionError("target 名不同应视为 miss")

    # 参数不同 -> miss（replay 模式抛错）
    try:
        asyncio.run(llm2.complete("q2"))
    except KeyError:
        pass
    else:
        raise AssertionError("参数不同应视为 miss")

    # 同步属性透传
    assert llm2.sync_helper() == "plain-attr"


def test_auto_and_embedder(tmp):
    path = os.path.join(tmp, "c2.json")
    emb = FakeEmbedder()
    c = ReplayCache(path, mode="auto")
    w = c.wrap(emb, name="embed")
    a = asyncio.run(w.create_batch(["ab", "abc"]))
    b = asyncio.run(w.create_batch(["ab", "abc"]))
    assert a == b
    assert emb.calls == 1, "第二次应命中缓存"
    c.save()
    assert json.load(open(path)) and len(ReplayCache(path)) == 1


def test_record_overwrites_existing(tmp):
    path = os.path.join(tmp, "c_re.json")
    first = FakeLLM()
    rec = ReplayCache(path, mode="record")
    asyncio.run(rec.wrap(first, name="llm").complete("q1"))
    rec.save()
    assert first.calls == 1

    class ChangedLLM(FakeLLM):
        async def complete(self, prompt, temperature=0.0):
            self.calls += 1
            return {"text": f"v2:{prompt}", "t": temperature}

    second = ChangedLLM()
    rec2 = ReplayCache(path, mode="record")
    out = asyncio.run(rec2.wrap(second, name="llm").complete("q1"))
    assert second.calls == 1, "record 命中旧键也必须调用真实客户端"
    assert out["text"] == "v2:q1"
    rec2.save()

    rep = ReplayCache(path, mode="replay")
    assert asyncio.run(rep.wrap(FakeLLM(), name="llm").complete("q1")) == {"text": "v2:q1", "t": 0.0}


def test_none_result_roundtrip(tmp):
    class NoneLLM:
        def __init__(self):
            self.calls = 0

        async def complete(self, prompt):
            self.calls += 1
            return None

    path = os.path.join(tmp, "c_none.json")
    raw = NoneLLM()
    rec = ReplayCache(path, mode="record")
    assert asyncio.run(rec.wrap(raw, name="llm").complete("empty")) is None
    rec.save()

    raw2 = NoneLLM()
    rep = ReplayCache(path, mode="replay")
    assert asyncio.run(rep.wrap(raw2, name="llm").complete("empty")) is None
    assert raw2.calls == 0 and rep.hit == 1 and rep.miss == 0, "返回 None 也必须算命中"


def test_key_stability():
    k1 = cache_key("llm", "complete", ("q",), {"temperature": 0.0})
    k2 = cache_key("llm", "complete", ("q",), {"temperature": 0.0})
    k3 = cache_key("llm", "complete", ("q",), {"temperature": 0.1})
    assert k1 == k2 and k1 != k3 and len(k1) == 64


def test_cli_merge(tmp):
    a = os.path.join(tmp, "a.json")
    b = os.path.join(tmp, "b.json")
    m = os.path.join(tmp, "m.json")
    json.dump({"k1": 1}, open(a, "w"))
    json.dump({"k2": 2}, open(b, "w"))
    from llm_replay_cache import _cli

    assert _cli(["--merge", a, b, "-o", m]) == 0
    assert json.load(open(m)) == {"k1": 1, "k2": 2}
    assert _cli(["--verify", m]) == 0


def test_cli_verify_duplicates(tmp):
    p = os.path.join(tmp, "dup.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"k1": 1, "k1": 2}')
    from llm_replay_cache import _cli

    assert _cli(["--verify", p]) == 1, "重复键必须被检出并返回非零"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"k1": 1, "k2": 2}')
    assert _cli(["--verify", p]) == 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_record_then_replay(tmp)
        test_auto_and_embedder(tmp)
        test_record_overwrites_existing(tmp)
        test_none_result_roundtrip(tmp)
        test_key_stability()
        test_cli_merge(tmp)
        test_cli_verify_duplicates(tmp)
    print("all tests passed")
