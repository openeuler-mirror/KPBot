# monitor 连接复用的实施与复现

## 诊断与适用条件

基线中的 `SandBox::connect_agent` 建立 vsock/ttrpc 传输并保存 `self.conn`，业务客户端持有同一连接的 clone。`monitor_vm` 启动时独立执行 `AsyncUtils::connect_agent`。候选让 `HealthClient` 持有已安装传输的 clone；这样同一沙箱的健康与业务 RPC 共享传输，须验证多路请求、错误传播和结束时的所有权语义。

在 `CubeShim/shim/src/sandbox/sb.rs` 核对三个完成点：首次 CONNECT 成功、当前实例安装 `self.conn`、`monitor_vm` 启动。记录沙箱 ID、实例代、调用来源和建连起止时间。验证 `Client::clone()` 共享的是支持并发的传输，并确认 monitor 停止只释放自身句柄。发现共享通道会造成健康检查或业务 RPC 延迟相互放大时，保留独立通道。

## 最小实现与迁移

本补丁取代 `scheme1-rpc.patch` 中的 `monitor_vm` 修改。一个目标树只采用一种 monitor 实现。在干净的 v0.7.1 工作树中先应用下方独立补丁。如果还需要方案一的预连接等改动，在另一工作树应用原方案一，对照差异逐段迁入非 monitor 改动，跳过整个 `monitor_vm` hunk。检查最终 diff，只保留一次有界健康探测和探测失败后的 CONNECT 回退。已应用方案一时，在独立工作树把该方法中的直接复用分支替换为本补丁实现，再运行 monitor 验收。

参考补丁基于 CubeSandbox `v0.7.1`（`31d911e430fdf8a8879bd062b8e78066c1a8e89d`）。在 `monitor_vm` 中短暂加锁取得 `self.conn` 的 clone，再释放锁并用 100 ms ttrpc deadline 做一次 `Health.Check`。收到响应说明启动时传输可用。无句柄、超时或 RPC 失败时，执行原 CONNECT 并用 `?` 传播其失败。该回退使用新传输启动 monitor，保留了原周期检查与退出规则。将新增探测的 RTT 与省下的握手一起计入候选耗时。

创建和恢复在 v0.7.1 均调用 `monitor_vm(false)`。启动探测在 `false` 时也执行，周期 `client.check` 仍由 `check_agent` 控制。Guest 在业务 RPC 完成后退出时，即使 `self.conn` 尚存，探测仍会失败。随后建连失败将阻止调用方继续到成功返回。

在隔离源码工作树中执行，`SOURCE_ROOT` 与 `SKILL_DIR` 使用本机绝对路径。

```bash
cd "$SOURCE_ROOT"
git rev-parse HEAD
git apply --stat "$SKILL_DIR/references/monitor-connection-reuse.patch"
git apply --check "$SKILL_DIR/references/monitor-connection-reuse.patch"
git apply "$SKILL_DIR/references/monitor-connection-reuse.patch"
(cd CubeShim && cargo build --release --locked)
```

其他版本按调用关系迁移并重新检查。使用目标环境已有发布方式切换产物，记录基线和候选的运行中 Shim 哈希。对照实验保持 Guest 镜像、模板、健康检查配置和工具链一致。

## 独立验证

包内用例需要 Python 3、Git 和 Rust/Cargo。runner 向 v0.7.1 的 `monitor_vm` 片段应用随包补丁，在模拟传输上运行实际方法。

```bash
cd "$SKILL_DIR"
python3 cases/monitor-health/run.py
```

用例显式调用 `monitor_vm(true)`，先让业务请求挂在屏障上，再用 Tokio 虚拟时钟推进到周期检查。周期健康请求须在业务请求释放前进入同一模拟传输。另运行 `monitor_vm(false)` 的 Guest 退出窗口，确认失败的重连向调用方传播。离线测试可传已校验的 Cargo vendor 目录 `--vendor-dir /path/to/vendor`。

真实传输验收使用隔离构建。定位 `sb.rs` 中 `self.monitor_vm(false).await?` 的创建、恢复调用点，只在本轮要测的调用点临时改传 `true`。在监控启动后发起一个可受控延迟的业务 RPC，在周期 check 进入同一 Guest ttrpc 连接后才释放业务响应。分别记录请求进入与响应时间、沙箱实例、连接标识，核对两个在途时间区间相交。故障验证完成后恢复原参数并重新构建正式候选。

| 场景 | 验收点 |
|---|---|
| c1 创建、启动监控和正常删除 | 已安装连接探测成功时，monitor 启动不发起第二次 CONNECT。健康状态和退出通知与基线一致。 |
| 业务成功后、monitor 启动前 Guest 退出 | 已缓存的句柄探测失败；原 CONNECT 失败向调用方传播，创建或恢复不返回成功。 |
| 没有已安装连接的调用路径 | 仍走原建连，返回原有错误语义。 |
| 业务 RPC 和 health RPC 并发 | 请求各自正确收发，业务长尾、健康超时率和连接数符合约定。测试覆盖至少一次实际健康轮询。 |
| 停止 monitor、Guest 退出、实例 ID 重用 | 停止 monitor 不误关仍在用的业务连接。退出后句柄与 FD 可回收。新实例获得自己的连接。 |
| 连接中断 | 沿用原错误与状态转换，确认双方能观察到中断。记录目标版本是否还有外层重连。 |

正确性通过后，以相同 bench 版本和模板交错运行 A（原路径）与 B（候选），每组顺序 A/B/B/A，每次清理本轮实例后恢复相同起点。可从下列命令开始，先用目标 bench 的 `--help` 核对选项。

```bash
cube-bench -c 100 -n 100 -w 0 -m create-only --no-tui -o "$REPORT_JSON"
```

每份 JSON 使用同一明确单位的 `summary.total_time_s` 或 `summary.total_time_ms`，并确认 100 次成功、0 错误。按组分别取两次 A 与两次 B 的中位数，整批创建时间下降率为 `100 × (A − B) / A`。保存每组结果和跨组波动，同时对照 CONNECT 次数、monitor 启动时长、业务 p95 与健康失败率。只有机制证据成立且创建总耗时下降可稳定复现时，才登记实测收益。

需要回退时，用同一发布方式恢复基线 Shim。以新建实例核对运行哈希、健康轮询与删除结果，保留本轮报告用于比较。
