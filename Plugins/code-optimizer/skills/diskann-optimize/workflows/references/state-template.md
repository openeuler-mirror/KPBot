# DiskANN 状态契约

schema_version 为 vector-opt-run.v2，阶段 0..8 与旧版保持一致。主协调者更新状态；角色只回传 artifact 与结论。

- source.baseline_commit 为完整 SHA-1/SHA-256 或 unknown；unknown 只允许启动，CP2 manifest 必须另有可复现版本/源码摘要。
- experiment 固定 experiment_root、experiment_id、run_dir；wiki_root 默认为 null。run_dir 必须是 root/id，与 state.json 所在目录一致。
- contract.manifest 为 run_dir 内相对路径；frozen=true 时必须存在。CP2 完成要求 frozen=true。
- stages 使用 pending/running/completed/blocked/failed/skipped。最多一个 running；活动或完成阶段之前只能有 completed/skipped 阶段。完成阶段必须有真实 artifact。
- artifact 同时按 POSIX/Windows 检查，拒绝绝对路径、盘符、.. 和越界符号链接。
- 优化流程的 CP0–CP7 不能跳过（不执行独立 diff 审查也需要完成 CP4 的实现与自检）。results.verdict 必须与 CP7 终态一致，接受结论要求 results.correctness=true；REWORK 是返回阶段的非终态。
- 未请求 Wiki 时 CP8=skipped、knowledge_sync.status=not_applicable；即使没有 CP8 也可以完成本地 CP7。
- 请求 Wiki 时 CP8 只能在终态 CP7 后开始；完成需要本地 receipt、validation_receipt 真实存在，其他目标路径非空。外部同步内容仍需适配器独立验证。
- results.commit_status 记录 committed/not_authorized/not_applicable；只有已有任务授权才提交和保存 SHA。

每次更新执行 scripts/validate_state.py；正式 schema 检查形状，脚本额外检查磁盘和状态流转。v1 迁移先 --check，确认目录和产物后 --apply；迁移不移动实验或抹去旧档。
