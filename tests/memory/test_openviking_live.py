"""Opt-in tests against a real local OpenViking service and local embedding.

Set KPBOT_OPENVIKING_LIVE_CONFIG and KPBOT_OPENVIKING_LIVE_SECOND_CONFIG
only to private development/test configurations. Tests persist synthetic records
in those configured projects; no production configuration should be used.
Normal unittest discovery skips these tests and does not contact any service.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from kpbot_memory.backend_config import load_config
from kpbot_memory.backends import OpenVikingBackend
from kpbot_memory.northbound import NorthboundMemory


@unittest.skipUnless(
    os.environ.get("KPBOT_OPENVIKING_LIVE_CONFIG")
    and os.environ.get("KPBOT_OPENVIKING_LIVE_SECOND_CONFIG"),
    "real OpenViking development configurations not explicitly selected",
)
class RealOpenVikingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config_path = Path(os.environ["KPBOT_OPENVIKING_LIVE_CONFIG"])
        self.config = load_config(self.config_path)
        self.second_config = load_config(os.environ["KPBOT_OPENVIKING_LIVE_SECOND_CONFIG"])
        self.instances = []
        self.marker = uuid.uuid4().hex
        self.assertNotEqual(self.config["user_id"], self.second_config["user_id"])
        self.assertGreaterEqual(len(self.config["allowed_projects"]), 2)

    async def asyncTearDown(self):
        for memory in reversed(self.instances):
            await memory.close()

    async def memory(self, config):
        memory = NorthboundMemory(OpenVikingBackend())
        self.instances.append(memory)
        before = await memory.status()
        self.assertEqual(before["data"]["state"], "uninitialized")
        result = await memory.initialize(config, timeout=120)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["state"], "ready")
        return memory

    async def test_real_roundtrip_cross_project_isolation_and_restart(self):
        memory = await self.memory(self.config)
        second = await self.memory(self.second_config)
        first_project, other_project = self.config["allowed_projects"][:2]
        experience = {
            "summary": "合成接口测试：循环中重复数据搬移，建议检查循环不变量。",
            "conditions": ["仅用于开发验证"],
            "evidence": [{"id": "synthetic", "content": "非真实优化测量 🧪\n保持原文"}],
            "marker": self.marker,
        }
        process = [
            {"role": "user", "content": "合成完整过程：减少循环中的重复搬移"},
            {"role": "assistant", "content": "合成执行步骤：检查循环不变量"},
            {"role": "tool", "content": "合成工具输出，不是实测\n包含中文和emoji 🧪"},
            {"role": "assistant", "content": "合成最终结果：仅验证记忆接口", "marker": self.marker},
        ]
        saved = []
        for kind, project, content in [
            ("experience", other_project, experience),
            ("task_process", first_project, process),
        ]:
            result = await memory.remember({
                "kind": kind, "project_id": project,
                "task_id": "synthetic-" + self.marker, "content": content,
            }, timeout=120)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["data"]["processing"], "ready", result)
            saved.append((result["data"]["source_id"], kind, project, content))

        secret = "合成其他用户私有内容：循环优化 " + self.marker
        private = await second.remember({
            "kind": "experience", "project_id": self.second_config["current_project"],
            "task_id": "synthetic-private-" + self.marker, "content": secret,
        }, timeout=120)
        self.assertTrue(private["ok"], private)

        request = {
            "query": "循环中重复搬移的数据怎样优化", "features": ["循环不变量"],
            "project_ids": [first_project, other_project],
            "kinds": ["experience", "task_process"], "limit": 20,
        }
        # Actual indexing was requested with wait=True. Poll only reads if the
        # server's visibility trails its successful indexing response.
        recalled = None
        for _ in range(5):
            recalled = await memory.recall(request, timeout=120)
            self.assertTrue(recalled["ok"], recalled)
            by_id = {item["source_id"]: item for item in recalled["data"]["items"]}
            if all(source in by_id for source, _, _, _ in saved):
                break
            await asyncio.sleep(0.25)
        self.assertFalse(recalled["data"]["partial"])
        for source, kind, project, content in saved:
            self.assertIn(source, by_id, "synthetic saved original must be recallable")
            item = by_id[source]
            self.assertEqual(item["content"], content)
            self.assertEqual(item["content_type"], "original")
            self.assertEqual(item["kind"], kind)
            self.assertEqual(item["project_id"], project)
        self.assertNotIn(private["data"]["source_id"], by_id)
        self.assertTrue(all(item["owner_id"] == self.config["user_id"] for item in by_id.values()))

        scoped = await memory.recall({**request, "project_ids": [other_project], "kinds": ["experience"]}, timeout=120)
        self.assertTrue(scoped["ok"], scoped)
        self.assertTrue(all(item["project_id"] == other_project and item["kind"] == "experience"
                            for item in scoped["data"]["items"]))
        shared = await memory.recall({**request, "include_shared": True})
        self.assertEqual(shared["error"]["code"], "UNSUPPORTED")
        forged = await memory.remember({"kind": "experience", "project_id": first_project,
                                        "task_id": "forged", "content": "forged",
                                        "owner_id": self.second_config["user_id"]})
        self.assertEqual(forged["error"]["code"], "ACCESS_DENIED")

        import httpx
        async with httpx.AsyncClient(base_url=self.config["options"]["endpoint"],
                                     headers={"X-API-Key": self.config["options"]["api_key"]},
                                     trust_env=False, follow_redirects=False) as client:
            denied = await client.get("/api/v1/content/download",
                                     params={"uri": private["data"]["source_id"]}, timeout=30)
            self.assertIn(denied.status_code, (403, 404))

        await memory.close()
        self.assertEqual((await memory.status())["data"]["state"], "closed")
        restarted = await self.memory(self.config)
        persisted = await restarted.recall(request, timeout=120)
        self.assertTrue(persisted["ok"], persisted)
        self.assertTrue(set(source for source, _, _, _ in saved).issubset(
            item["source_id"] for item in persisted["data"]["items"]))

    async def test_actual_cli_remember_and_recall(self):
        project = self.config["current_project"]
        record = {"kind": "experience", "project_id": project,
                  "task_id": "synthetic-cli-" + self.marker,
                  "content": "合成CLI验证：循环重复搬移优化 " + self.marker}
        request = {"query": "循环重复搬移优化", "project_ids": [project], "limit": 20}
        with tempfile.TemporaryDirectory(prefix="live-cli-", dir=self.config_path.parent) as directory:
            directory = Path(directory)
            async def invoke(operation, payload=None):
                args = [sys.executable, str(SCRIPTS / "kpbot-memory.py"), "backend", operation,
                        "--config", str(self.config_path), "--timeout", "120"]
                if payload is not None:
                    path = directory / (operation + ".json")
                    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    args += ["--input", str(path)]
                result = await asyncio.to_thread(subprocess.run, args, capture_output=True,
                                                 text=True, timeout=180,
                                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                value = json.loads(result.stdout)
                self.assertTrue(value["ok"], value)
                return value
            self.assertEqual((await invoke("initialize"))["data"]["state"], "ready")
            self.assertEqual((await invoke("status"))["data"]["state"], "ready")
            submitted = await invoke("remember", record)
            recalled = await invoke("recall", request)
            self.assertTrue(any(item["source_id"] == submitted["data"]["source_id"]
                                and item["content"] == record["content"]
                                for item in recalled["data"]["items"]))
            self.assertIsNone((await invoke("close"))["data"])


if __name__ == "__main__":
    unittest.main()
