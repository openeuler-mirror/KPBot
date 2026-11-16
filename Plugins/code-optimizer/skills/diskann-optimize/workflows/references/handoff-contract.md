# DiskANN 角色交接契约

主协调者派发和角色回传均使用 UTF-8 JSON，保存在 run_dir 内：

```json
{
  "run_id": "exp-example",
  "stage": "3",
  "role": "diskann-analyzer",
  "source_id": "sha256:SOURCE_DIGEST",
  "manifest": "manifest.json",
  "status": "completed",
  "artifacts": ["ANALYSIS.md"],
  "verdict": "CP3_PASS",
  "limitations": []
}
```

派发补充目标、允许路径、资源和输入证据；回传 status 为 completed/blocked/failed，说明失败原因与恢复位置。协调者检查 run_id、当前 source_id、manifest 一致，artifact 为 run_dir 内真实存在的相对路径，不能带盘符、绝对路径、.. 或越界链接，再更新 state。final review 必须给终态 verdict 或 REWORK；不得根据自由文本自动接受。只有主协调者写 state。
