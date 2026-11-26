# 状态与角色交接

## 所有权

primary 创建/更新 run_dir 根目录的 state.json；角色依据 handoff 执行并回传产物，不修改 state。run_dir 独立于技能/插件目录。

## 状态

stages 固定为 0–7，状态 pending/running/completed/blocked/failed。completed 形成前缀，后面至多一个活动或阻塞阶段。blocked 填写 blocker_reason。
`CP4_SKIPPED_NOT_REQUESTED` 仅是可选 diff 审查未请求的 decision，不是阶段状态，也不跳过实现。
REWORK 记录到 result.rework_events，将 return_to 及之后阶段置 pending；不把 REWORK 写入终态 result.verdict。

## 关键字段

- source：基线 commit、版本和 baseline_id；无可用 Git 提交时 commit 记 `unknown`，CP1 复核版本和归档/源码摘要，CP2 仍须明确 baseline_id。补丁/构建的详细身份记 manifest。
- history_policy：none 或 external；external 注明 source，仅表示可用输入，不声明已有候选结果。
- contract：algorithm 与 active_algorithm_skill；本协议支持 rabitq/turboquant/ivfpq/ivfpqfs/hnsw。
- selected_candidate：CP3-B 产出，CP4 完成前必须明确，selection_mode 为 user-confirmed 或 auto-ranked。
- result.candidate_commit：pending（执行中）、committed（有授权且已提交，完整 40/64 位 SHA、committed_by=coordinator）、not_authorized（已接受但未授权，列 pending_files）、not_applicable（拒绝或不确定）。
- result.verdict：仅终态 ACCEPT/ACCEPT_WITH_NOTES/REJECT/INCONCLUSIVE；report 为相对 run_dir 路径。

## 身份与路径

CP2 需要明确 baseline_id，已有补丁按实际记录。其他阶段仍按线性状态和各自证据要求推进，不因算法名字设置额外门槛。
stage/handoff 的 artifacts 均为相对 run_dir 路径，禁止绝对路径和..越界；源码/数据等输入的绝对路径放在相应 fields 中。

## 校验

使用 workflows/scripts/validate_state.py 和 validate_handoff.py 检查每次交接。Schema 定义见[state.schema.json](state.schema.json)与[handoff.schema.json](handoff.schema.json)；校验通过只表示结构/状态有效，不证明实验成功。
