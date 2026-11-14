# 预检、准备与故障处理

## 两种模式

- `check`：只读。不安装依赖、不改文件、不删接口、不重启、不创建沙箱。
- `run --prepare`：用户明确确认后才执行准备，再重新完整预检和冒烟；准备失败不会开测。
- 独立 `prepare`：只准备并复验，不跑基准。必须显式模板/NUMA，并带
  `--yes --exclusive-confirmed --approved-plan-hash <prepare-only哈希>`；哈希通过相同环境参数的
  `show-plan --operation prepare --tap-evidence <check证据>` 获取。证据写入 `--output-root/prepare_*`。

## NUMA 绑定

检查并按需修改：

```text
/usr/local/services/cubetoolbox/scripts/systemd/cubemaster-start.sh
/usr/local/services/cubetoolbox/scripts/systemd/cubelet-start.sh
```

只改写包含 `${CUBEMASTER_BIN}` / `${CUBELET_BIN}` 的实际启动行，统一为：

```bash
numactl -N <已确认节点> --localalloc <原二进制及参数>
```

不碰 `ensure_executable`、赋值或注释。已正确绑定时不改文件；两个脚本先分别渲染和校验，
都成功才提交，提交中失败会回滚已提交脚本。修改前在 `prepare-backups/` 留纳秒级唯一备份，
保留权限/属主，并将每步写入 `prepare.json`。除检查脚本外，还用 `os.sched_getaffinity` 验证运行中服务的
CPU 集合与目标 NUMA 节点完全一致；脚本正确但进程未生效同样 FAIL。

单 NUMA 默认可用 node0。多 NUMA 只有在两个现有脚本已经一致绑定时才沿用，否则必须显式
`--numa-node` 并由用户确认。90.19 的预期为 cubemaster/cubelet 均绑定 node0，但其他机器不得
照抄。不要根据当前瞬时空闲程度自动迁移 NUMA，也不要在测试中途改变节点。

## TAP=500

v0.7.x 默认网络池的 TAP 以 `192.168.x.x` 作为**接口名**。计数和删除均使用行首接口名锚定，
不会匹配 `enp*` 物理接口描述中的 IP，也不使用旧版 `z192.*` 规则。除名称外，prepare 还要求：

- 部署配置唯一的 `tap_init_num` 等于批准目标，且候选接口位于配置的 Cube CIDR；
- `ip -d -o link show` 明确包含 `tun type tap` 和 `persist on`；无法验证时拒绝删除；
- API 严格证明沙箱为空且 shim 为 0。以上共同构成 Cube 资源池归属证据，不删除 CIDR 外接口；
- check 对超额 `(ifindex,name)` 集合计算 `TAP_EVIDENCE_SHA256`，该证据进入最终批准哈希；
  prepare 删除前重新计算，任何集合漂移都拒绝。每个接口还有 started/completed/failed journal。
- 若该 CIDR 可能与其他业务共用，禁止准备；即使用户已批准，也只能删除证据集合内的接口。

- 数量 >500：只有 API 沙箱为 0 且 containerd-shim 为 0 时，prepare 才按 ifindex 保留较早的
  500 个并删除超额项；删除后重启 cubelet。
- 数量 <500：不手工伪造接口，重启 cubelet 让官方资源池逻辑重建。
- 数量 =500 但服务或运行 NUMA 不正确：仍按需要重启。
- 自定义 CIDR/非 IP 命名部署不能直接沿用该匹配器；先适配并增加契约测试。

不得把“systemd active”当作 cubelet 就绪。重启后默认最多等待 1800 秒，同时满足：
`cubelet active + TAP=500 + 运行进程 NUMA 正确 + CubeAPI /health 直连成功`。超时则停止，
查看 `/data/log/Cubelet/`、`/data/log/CubeVmm/` 和 prepare 证据。测试异常慢时优先复查
cubelet 是否仍在重建/清理，而不是立刻归因到被测版本。

## 资源独占

预检要求：

1. API 可见沙箱为 0；containerd-shim 为 0；无其他基准进程。
2. 非 Cube 进程 CPU 超过阈值（默认 10%）即 FAIL；load1 超过 `max(4, CPU数×5%)` 即 FAIL。
3. 用户仍须明确确认整个测试窗口独占。进程瞬时采样无法证明所有 IO/定时任务退出，Agent
   应审查 `busy_processes` 和已知业务，不得仅靠阈值宣布独占。
4. 技能不会自动 kill nebula、cannbot、数据库、监控或其他任务。由用户决定停止方式和维护窗口。
5. 所有正式 run/prepare 共用 `/run/lock/cubesandbox-lifecycle-bench.lock` 非阻塞排他锁，覆盖到最终
   清理结束；锁冲突直接失败。锁只防止本工具并发，仍需要用户保证其他客户端不创建沙箱。

## 代理与依赖

所有测量、冒烟、API 和准备子进程都会移除：
`http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY/all_proxy/ALL_PROXY`，同时移除 no_proxy 变量，
由 `CubeAPI` 使用空 `ProxyHandler` 强制直连。日志只记录变量名，不记录代理 URL 或 API Key。

需要与部署版本匹配的 `cubesandbox` Python SDK；缺失时按预检提示安装所选源码树
`examples/snapshot-rollback-clone/requirements.txt`。cube-bench 缺失时仅 `--prepare` 会用官方
Makefile 构建。依赖安装/构建不是性能测量窗口，不要把其网络耗时算入结果。

## 模板与 API

- `/health` 和 `GET /sandboxes` 必须可用；无法列出沙箱不等于沙箱为空。未知响应结构、非对象
  元素、缺 ID、`next_token/has_more` 或 total 与本页数量不一致都会 fail-closed，不会当成空列表。
- 模板必须在 `tpl list` 为 READY，且 `tpl info --json` 至少一个节点副本的 status/phase 均 READY。
- 用户未指定且只有一个 READY 模板时可自动选中；多个 READY 模板必须明确选择。
- 真实冒烟通过后才允许压测。冒烟失败属于环境失败，不生成功耗/性能结论。

## 常见现象

| 现象 | 处理 |
|---|---|
| TAP>500、API 502/超时 | 先恢复健康并证明沙箱/shim 为空；无法证明时 prepare 会拒绝，不能盲目删接口 |
| `no more resource` | 检查 host.quota、残留实例和容量；不要把容量错误当性能回归 |
| HTTP 408 / MemAvailable 快速下降 | 停止扩批，检查内存回收、kswapd 和 dirty writeback |
| 多轮 CV>10% | 检查其他负载与 cubelet 状态，环境稳定后重测 |
| 单实例就慢 | 先修复环境/模板/服务；不要直接扩大并发 |
| 仅并发出现长尾 | 按 create-verdict 的分布形态排查争用、串行排队或轮询周期 |
