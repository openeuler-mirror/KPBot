"""Live mem0 backend integration tests (real service, opt-in).

Set KPBOT_MEM0_LIVE_CONFIG to a 0600 trusted configuration whose options
point at the self-hosted mem0 REST service. Use only a private
development/test configuration: tests persist synthetic records (marked by
a per-run run_id in task_id and content) in the configured projects and
never delete anything, so no production configuration should be used.
Normal unittest discovery skips these tests and does not contact any
service.

Run: KPBOT_MEM0_LIVE_CONFIG=/tmp/opencode/kpbot-mem0-live.json \
  PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest tests.memory.test_mem0_live -v
"""

import json
import os
import unittest
import uuid
from pathlib import Path
import sys

_SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from kpbot_memory.backend_cli import create_backend
from kpbot_memory.backend_config import load_config
from kpbot_memory.northbound import NorthboundMemory

STORE_TIMEOUT = 300.0
READ_TIMEOUT = 120.0


@unittest.skipUnless(os.environ.get("KPBOT_MEM0_LIVE_CONFIG"), "live mem0 configuration required")
class Mem0LiveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(os.environ["KPBOT_MEM0_LIVE_CONFIG"])

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
        record = {"kind": "experience", "project_id": "project-a", "task_id": f"exp-{self.run_id}",
                  "content": content}
        stored = await self.memory.remember(record, timeout=READ_TIMEOUT)
        self.assertTrue(stored["ok"], stored)
        self.assertTrue(stored["data"]["source_id"])
        self.assertEqual(stored["data"]["processing"], "ready")
        recall = await self.memory.recall({"query": "减少循环中的重复搬移", "project_ids": ["project-a"],
                                            "kinds": ["experience"], "limit": 5}, timeout=READ_TIMEOUT)
        self.assertTrue(recall["ok"], recall)
        matches = [item for item in recall["data"]["items"] if item["task_id"] == f"exp-{self.run_id}"]
        self.assertEqual(len(matches), 1, recall["data"]["items"])
        item = matches[0]
        self.assertEqual(item["content"], content)  # 完整原文（含结构与顺序）
        self.assertEqual(item["content_type"], "original")
        self.assertEqual(item["kind"], "experience")
        self.assertEqual((item["project_id"], item["owner_id"]), ("project-a", self.config["user_id"]))
        self.assertEqual(item["source_id"], stored["data"]["source_id"])

    async def test_task_process_original_and_derived(self):
        messages = [
            {"role": "user", "content": f"任务{self.run_id}：优化矩阵乘循环"},
            {"role": "assistant", "content": "分析发现内循环存在重复的行加载数据搬移"},
            {"role": "tool", "content": "基准测试：优化前 120ms"},
            {"role": "assistant", "content": "将不变量搬移出循环后 90ms，加速 1.33x"},
        ]
        record = {"kind": "task_process", "project_id": "project-b", "task_id": f"proc-{self.run_id}",
                  "content": messages}
        stored = await self.memory.remember(record, timeout=STORE_TIMEOUT)
        self.assertTrue(stored["ok"], stored)
        recall = await self.memory.recall({"query": "矩阵乘 循环 数据搬移", "project_ids": ["project-b"],
                                            "kinds": ["task_process"], "limit": 10}, timeout=READ_TIMEOUT)
        self.assertTrue(recall["ok"], recall)
        hits = [item for item in recall["data"]["items"] if item["task_id"] == f"proc-{self.run_id}"]
        self.assertTrue(hits, recall["data"]["items"])
        for item in hits:
            self.assertIn(item["content_type"], ("original", "excerpt", "derived"))
            self.assertEqual(item["kind"], "task_process")
            self.assertEqual(item["project_id"], "project-b")
        # 原文或派生记忆至少其一被语义命中即可
        texts = json.dumps(recall["data"]["items"], ensure_ascii=False)
        self.assertTrue("搬移" in texts or "矩阵" in texts, texts)

    async def test_cross_project_and_kind_filter(self):
        for project in ("project-a", "project-b"):
            record = {"kind": "experience", "project_id": project, "task_id": f"cp-{self.run_id}-{project}",
                      "content": {"summary": f"跨项目经验{self.run_id}：NEON 向量化需要保证内存对齐"}}
            # STORE_TIMEOUT：真实网关含图谱抽取，实测可能远超 15-25s（见交付文档 timeout 建议）
            stored = await self.memory.remember(record, timeout=STORE_TIMEOUT)
            self.assertTrue(stored["ok"], stored)
        recall = await self.memory.recall({"query": "NEON 向量化 内存对齐", "project_ids": ["project-a", "project-b"],
                                            "kinds": ["experience"], "limit": 10}, timeout=READ_TIMEOUT)
        self.assertTrue(recall["ok"], recall)
        projects_hit = {item["project_id"] for item in recall["data"]["items"]
                        if item["task_id"] and item["task_id"].startswith(f"cp-{self.run_id}")}
        self.assertEqual(projects_hit, {"project-a", "project-b"}, recall["data"]["items"])
        self.assertTrue(all(item["kind"] == "experience" for item in recall["data"]["items"]))

    async def test_close_then_not_ready(self):
        self.assertIsNone(await self.memory.close())
        result = await self.memory.remember({"kind": "experience", "project_id": "project-a",
                                             "task_id": "x", "content": "y"}, timeout=READ_TIMEOUT)
        self.assertEqual(result["error"]["code"], "NOT_READY")


if __name__ == "__main__":
    unittest.main()
