# AgentENV 功能基线验证

在环境证据完整、准备启动真实 ublk/沙箱测试时读取本文件。
本流程验证功能和建立基线，不自动授权部署内核、重启或执行模型 API。

## 1. 授权、资源所有权和隔离

- 核实当前项目的 runner、实际 UID、有效 capabilities 和测试命令。
  现成脚本包含 sudo 不等于已经授权。只在授权范围内使用 NET_ADMIN/SYS_ADMIN。
- 为本轮新建 home/runtime/config/cache、临时后端、日志和 daemon socket。
  旧 env 脚本可能指向共享状态，不能直接 source。已有 daemon、设备只读检查。
- 建立资源清单：原有 PID+启动时间、设备名称集合、网络接口/路由/防火墙摘要。
  只释放本轮创建且能够确认所有权的设备和进程，避免全局 kill 或清理命令。
- 状态目录隔离不能隔离宿主网络。若项目会安装全局 iptables 规则，使用已授权
  runner 配合 `unshare --net --mount --propagation private` 和已核实的测试命令，
  并验证 namespace ID 与宿主不同；不能隔离时阻塞，不冒险修改已有规则。
- 命名空间也不能隔离共享内核、驱动、调度器或 TLBI。它不能代替可恢复内核测试节点。
- 不启用不必要的 metrics 监听、预热池、P2P 或外部下载。优先复制已核实的本地资产，
  记录版本与 SHA-256；不要让测试自动安装依赖或更改 Host 前提。

## 2. 启动前检查

### 二进制、工具链与镜像

确认实际二进制身份，不把旧目录或同名进程当作当前运行版本。
复用 Rust rlib 时需匹配 rustc 版本及依赖指纹；出现 E0514 应先查已有匹配工具链，
不要对原仓库运行 `cargo clean`，也不要自动下载或重编整个环境。

OverlayBD sparse upper 不是任意 raw 稀疏文件。按当前版本 helper 或实际工具
初始化格式头，记录虚拟容量与物理占用。`header magic/type don't match` 首先检查
测试后端格式，不直接归因内核缺少 ublk。物理文件比较需要按已核实格式处理头部偏移。

### Unix socket 路径

检查最终 API、vsock、daemon socket 路径的 UTF-8 字节长度，而不只检查目录名。
Linux pathname Unix socket 通常要求小于 108 字节；为随机子目录和固定文件名留空间。
发现 `SUN_LEN`/路径过长时，为本轮分配短且私有的 work/runtime 路径，保持数据和日志
目录可追溯。不要删除公共 socket、覆盖旧目录或调整 HZ 来解决路径错误。

## 3. 真实功能检查

### ublk 存储路径

1. 确认所选设备号原先不存在，且在驱动容量范围内；创建冲突时停止，不删除冲突设备。
2. 用专用、格式正确的临时后端启动当前项目的真实服务程序。
3. 在多个偏移写入不同数据，flush/fsync 后逐字节或以强哈希验证回读。
4. 在格式允许时独立核对后端数据；检查未写区域。
5. 关闭并删除本轮设备，用同一后端重建，再验证持久性。
6. 保存创建/删除记录、后端标识、校验结果和前后资源清单。

### AgentENV 生命周期

使用真实 AgentENV 路径验证创建、Guest 命令、持久标记、pause/capture、restore、
标记一致性和停止。默认先用小规格与串行负载确认功能，再决定并发压测规模。
旧 benchmark 可能硬编码很高的并发数，不要把一次总入口调用当作低风险 smoke test。

分别记录 Host 与 Guest 的运行内核。Host 的 6.6 配置不能证明 Guest 使用 OLK-6.6，
普通 ARM Guest 的功能通过也不能证明 Pvspinlock 协议或 TLBI 范围机制已生效。

## 4. 结果门控与归因

- 退出码 0 不代表有任何用例完成。检查结构化结果、实际功能断言、原始样本和数量。
  `Skipping`、未匹配用例、panic、初始化失败或缺少样本时，不输出成功或性能收益。
- 当前旧版 bounded snapshot 文本日志可用共享脚本检查：

```bash
python3 scripts/check_benchmark_output.py \
  --log /absolute/path/to/current-benchmark.log \
  --benchmark snapshot_resume --expected-samples 10 --exit-code 0
```

示例的数量和退出码必须替换为当前代码约定和实际捕获值；原始样本输出需由真实
benchmark 的配置启用。该脚本只检查旧版 `samples [... ms]` 文本产物是否可用，
不验证环境身份、业务正确性或 A/B 归因，也不会生成收益百分比。

- 镜像初始化、工具链、socket 路径失败归入测试前提/环境适配，不虚构成锁、HZ 或
  TLBI 性能瓶颈。先保留失败证据，单独修正前提，再使用新状态复测。
- 结合阶段解释日志：设备已经正常 stop/delete 后的 ENODEV 与运行中 I/O 失败不同。
  RLIMIT 提升警告也不等于功能失败；记录实际限制，不为消除警告自动扩权。
- 采集器测试、日志解析测试、模型评估、实际 ublk I/O、沙箱生命周期、业务 A/B
  分开报告。只完成存储 I/O 时，不标记沙箱生命周期通过。
- 基线耗时不是优化收益。记录单位、样本数、规格、池/缓存/后台负载和计时边界。
  小样本不外推稳定 P95/P99；带轮询的 ready 观测不等于精确设备启动延迟。

## 5. 清理与验收

正常关闭本轮沙箱和私有 daemon，释放本轮设备及网络资源。对照原有设备集合、
受保护 PID/启动时间和宿主网络摘要。清理不完整时如实标记失败/阻塞，不扩大删除范围。
证据、配置、错误日志和新资产清单保留；删除大文件也只限本轮创建且不再使用的对象。

输出遵循 `validation.md`，增加 `validation_scope` 或 `functional_checks` 明确
本次通过的是存储 I/O、沙箱生命周期还是特定优化机制；没有业务 A/B 时 `gain_pct=null`。
