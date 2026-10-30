"""Live mem0 backend integration tests (real service, opt-in).

Set KPBOT_MEM0_LIVE_CONFIG to a 0600 trusted configuration whose options
point at the self-hosted mem0 REST service. Use only a private
development/test configuration: tests persist synthetic records (marked by
a per-run run_id in task_id and content) in the configured projects and
never delete anything, so no production configuration should be used.
Normal unittest discovery skips these tests and does not contact any
service. Base tests explicitly disable optional LLM derivation. Set
KPBOT_MEM0_LIVE_DERIVATION=1 to test extraction with a configured external
LLM. Set KPBOT_MEM0_LIVE_SECOND_CONFIG to a distinct user's trusted config
(with at least one common project) for the separate two-user isolation test;
without it that test is explicitly skipped, not counted as coverage.

Run: KPBOT_MEM0_LIVE_CONFIG=/tmp/opencode/kpbot-mem0-live.json \
  PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest tests.memory.test_mem0_live -v
"""

import os
import unittest
import uuid
from pathlib import Path
import sys

_SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from kpbot_memory.cli.backend import create_backend
from kpbot_memory.backend_config import load_config
from kpbot_memory.northbound import NorthboundMemory

STORE_TIMEOUT = 300.0
READ_TIMEOUT = 120.0


@unittest.skipUnless(os.environ.get("KPBOT_MEM0_LIVE_CONFIG"), "live mem0 configuration required")
class Mem0LiveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        config = load_config(os.environ["KPBOT_MEM0_LIVE_CONFIG"])
        cls.config = {**config, "options": {**config["options"], "derive_memories": False}}
        if len(config["allowed_projects"]) < 2:
            raise AssertionError("mem0 live configuration requires at least two allowed projects")
        cls.first_project, cls.other_project = config["allowed_projects"][:2]

    async def asyncSetUp(self):
        self.memory = NorthboundMemory(create_backend("mem0"))
        result = await self.memory.initialize(self.config, timeout=READ_TIMEOUT)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["state"], "ready")
        self.run_id = uuid.uuid4().hex[:12]

    async def asyncTearDown(self):
        # 合成记录按 run_id 标记后保留，不做任何删除（对齐 openviking live 测试约定）
        await self.memory.close()

    async def test_status_ready(self):
        result = await self.memory.status(timeout=READ_TIMEOUT)
        self.assertTrue(result["ok"] and result["data"]["state"] == "ready", result)

    async def test_experience_roundtrip_original_content(self):
        content = {"summary": f"经验{self.run_id}：减少循环中的重复搬移",
                   "conditions": ["输入规模较小"],
                   "evidence": ["基准测试前后对比数据"]}
        record = {"kind": "experience", "project_id": self.first_project, "task_id": f"exp-{self.run_id}",
                  "content": content}
        stored = await self.memory.remember(record, timeout=READ_TIMEOUT)
        self.assertTrue(stored["ok"], stored)
        self.assertTrue(stored["data"]["source_id"])
        self.assertEqual(stored["data"]["processing"], "ready")
        recall = await self.memory.recall({"query": "减少循环中的重复搬移", "project_ids": [self.first_project],
                                            "kinds": ["experience"], "limit": 5}, timeout=READ_TIMEOUT)
        self.assertTrue(recall["ok"], recall)
        matches = [item for item in recall["data"]["items"] if item["task_id"] == f"exp-{self.run_id}"]
        self.assertEqual(len(matches), 1, recall["data"]["items"])
        item = matches[0]
        self.assertEqual(item["content"], content)  # 完整原文（含结构与顺序）
        self.assertEqual(item["content_type"], "original")
        self.assertEqual(item["kind"], "experience")
        self.assertEqual((item["project_id"], item["owner_id"]), (self.first_project, self.config["user_id"]))
        self.assertEqual(item["source_id"], stored["data"]["source_id"])

    async def test_task_process_complete_original_without_derivation(self):
        messages = [
            {"role": "user", "content": f"任务{self.run_id}：优化矩阵乘循环"},
            {"role": "assistant", "content": "分析发现内循环存在重复的行加载数据搬移"},
            {"role": "tool", "content": "合成工具输出：保持换行\n和 emoji 🧪，不是实测结果"},
            {"role": "assistant", "content": "合成结论：检查循环不变量，不宣称实测加速"},
        ]
        record = {"kind": "task_process", "project_id": self.other_project, "task_id": f"proc-{self.run_id}",
                  "content": messages}
        stored = await self.memory.remember(record, timeout=STORE_TIMEOUT)
        self.assertTrue(stored["ok"], stored)
        recall = await self.memory.recall({"query": "矩阵乘 循环 数据搬移", "project_ids": [self.other_project],
                                            "kinds": ["task_process"], "limit": 10}, timeout=READ_TIMEOUT)
        self.assertTrue(recall["ok"], recall)
        hits = [item for item in recall["data"]["items"] if item["task_id"] == f"proc-{self.run_id}"]
        self.assertTrue(hits, recall["data"]["items"])
        self.assertEqual(stored["data"]["processing"], "ready")
        self.assertEqual(len(hits), 1, hits)
        item = hits[0]
        self.assertEqual(item["content"], messages)
        self.assertEqual(item["content_type"], "original")
        self.assertEqual(item["kind"], "task_process")
        self.assertEqual(item["project_id"], self.other_project)
        self.assertEqual(item["owner_id"], self.config["user_id"])
        self.assertEqual(item["source_id"], stored["data"]["source_id"])

    @unittest.skipUnless(os.environ.get("KPBOT_MEM0_LIVE_DERIVATION") == "1",
                         "LLM derivation not selected: set KPBOT_MEM0_LIVE_DERIVATION=1")
    async def test_opt_in_derivation_returns_attributable_memory(self):
        deriving = NorthboundMemory(create_backend("mem0"))
        config = {**self.config, "options": {**self.config["options"], "derive_memories": True}}
        try:
            self.assertTrue((await deriving.initialize(config, timeout=READ_TIMEOUT))["ok"])
            content = [{"role": "user", "content":
                f"我的开发偏好：始终使用 Python 3.12 和 SQLite 做本地原型。项目代号是 Quartz-{self.run_id}。"}]
            stored = await deriving.remember({"kind": "task_process", "project_id": self.other_project,
                "task_id": f"derive-{self.run_id}", "content": content}, timeout=STORE_TIMEOUT)
            self.assertTrue(stored["ok"], stored)
            self.assertEqual(stored["data"]["processing"], "ready", stored)
            recall = await deriving.recall({"query": f"Quartz-{self.run_id} Python SQLite 开发偏好",
                "project_ids": [self.other_project], "kinds": ["task_process"], "limit": 20}, timeout=READ_TIMEOUT)
            self.assertTrue(recall["ok"], recall)
            hits = [item for item in recall["data"]["items"]
                    if item["source_id"] == stored["data"]["source_id"] and item["content_type"] == "derived"]
            self.assertTrue(hits, "No derived hit attributable to this submission: " + str(recall))
            for item in hits:
                self.assertTrue(item["content"])
                self.assertEqual(item["owner_id"], self.config["user_id"])
                self.assertEqual(item["project_id"], self.other_project)
                self.assertEqual(item["task_id"], f"derive-{self.run_id}")
        finally:
            await deriving.close()

    @unittest.skipUnless(os.environ.get("KPBOT_MEM0_LIVE_SECOND_CONFIG"),
                         "two-user isolation not selected: set KPBOT_MEM0_LIVE_SECOND_CONFIG")
    async def test_opt_in_two_user_isolation_and_identity_rejections(self):
        other_config = load_config(os.environ["KPBOT_MEM0_LIVE_SECOND_CONFIG"])
        other_config = {**other_config, "options": {**other_config["options"], "derive_memories": False}}
        self.assertNotEqual(other_config["user_id"], self.config["user_id"])
        common = set(other_config["allowed_projects"]) & set(self.config["allowed_projects"])
        self.assertTrue(common, "Two-user fixtures require a common project to test owner isolation")
        project = sorted(common)[0]
        other = NorthboundMemory(create_backend("mem0"))
        try:
            self.assertTrue((await other.initialize(other_config, timeout=READ_TIMEOUT))["ok"])
            saved = []
            query = f"双用户隔离 循环优化 {self.run_id}"
            for memory, config in ((self.memory, self.config), (other, other_config)):
                result = await memory.remember({"kind": "experience", "project_id": project,
                    "task_id": f"isolation-{self.run_id}-{config['user_id']}",
                    "content": query + " " + config["user_id"]}, timeout=READ_TIMEOUT)
                self.assertTrue(result["ok"], result)
                saved.append(result["data"]["source_id"])
            for index, (memory, config) in enumerate(((self.memory, self.config), (other, other_config))):
                result = await memory.recall({"query": query, "project_ids": [project],
                    "kinds": ["experience"], "limit": 20}, timeout=READ_TIMEOUT)
                self.assertTrue(result["ok"], result)
                sources = {item["source_id"] for item in result["data"]["items"]}
                self.assertIn(saved[index], sources)
                self.assertNotIn(saved[1-index], sources)
                self.assertTrue(all(item["owner_id"] == config["user_id"] for item in result["data"]["items"]))
            forged = await self.memory.remember({"kind": "experience", "project_id": project,
                "task_id": "forged", "content": "synthetic", "owner_id": other_config["user_id"]})
            self.assertEqual(forged["error"]["code"], "ACCESS_DENIED")
            shared = await self.memory.recall({"query": query, "include_shared": True})
            self.assertEqual(shared["error"]["code"], "UNSUPPORTED")
        finally:
            await other.close()

    async def test_cross_project_and_kind_filter(self):
        for project in (self.first_project, self.other_project):
            record = {"kind": "experience", "project_id": project, "task_id": f"cp-{self.run_id}-{project}",
                      "content": {"summary": f"跨项目经验{self.run_id}：NEON 向量化需要保证内存对齐"}}
            stored = await self.memory.remember(record, timeout=STORE_TIMEOUT)
            self.assertTrue(stored["ok"], stored)
        recall = await self.memory.recall({"query": "NEON 向量化 内存对齐", "project_ids": [self.first_project, self.other_project],
                                            "kinds": ["experience"], "limit": 10}, timeout=READ_TIMEOUT)
        self.assertTrue(recall["ok"], recall)
        projects_hit = {item["project_id"] for item in recall["data"]["items"]
                        if item["task_id"] and item["task_id"].startswith(f"cp-{self.run_id}")}
        self.assertEqual(projects_hit, {self.first_project, self.other_project}, recall["data"]["items"])
        self.assertTrue(all(item["kind"] == "experience" for item in recall["data"]["items"]))

    async def test_close_then_not_ready(self):
        self.assertIsNone(await self.memory.close())
        result = await self.memory.remember({"kind": "experience", "project_id": self.first_project,
                                             "task_id": "x", "content": "y"}, timeout=READ_TIMEOUT)
        self.assertEqual(result["error"]["code"], "NOT_READY")


if __name__ == "__main__":
    unittest.main()
