---
name: mysql-source-optimization
description: >
  MySQL 8.0 源码优化子技能：从 perf 热点到源码的挖掘方法、补丁实施与验证。覆盖的补丁机制
  示例（补丁原文由操作者提供，本 skill 不附带，不代表已验证收益）包括：fast-tuple（固定字段
  memcmp）、Read View version（原子版本复用视图）、space-skipping - NEON collation（ASCII 批量
  sort key/哈希/空格）、binlog ordered（多组 pending lock/cond 降组提交争用）、rec_get_offsets
  （传 compact 标志）、binlog preallocate（fallocate - 有效 EOF），以及语义边界、aarch64 条件编译、
  MTR/A-B 验证、负结果与回滚。用于评估/实施 MySQL 专属补丁；不负责 OS（→
  mysql-os-optimization）、my.cnf（→ mysql-app-config-optimization）、通用 C/C++ 微架构（→
  kpbot-code-optimizer）。
  触发词：MySQL 源码优化、patch、fast-tuple、Read View、collation、NEON、binlog、
  rec_get_offsets、preallocate、memcmp、组提交、源码热点。
license: MulanPSL-2.0
compatibility: MySQL 8.0.25/8.0.28；补丁多为 `__aarch64__ && __linux__`；鲲鹏 920/920G、openEuler 实测
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: relational_database
  version: "1.0.0"

---

# MySQL 源码优化

## 简介

源码优化只做两件事：**把已归因的热点变成语义等价的改动**，以及**用证据证明收益与安全**。

```
perf/等待证据 → 源码函数 → 机制（比较/分配/锁/拷贝/IO）→ 最小补丁
  → debug 等价校验 → MTR/冒烟 → 单 patch A/B → 保留/回滚
```

原则：**结构性去分配/去哈希/去重复计算 > 微调拷贝 > 指令级微优化**；不改变 SQL 语义、
持久化格式、提交顺序与复制一致性。

## 何时触发

- 已定位到 MySQL 源码函数（如 `row_search_mvcc`、`cmp_dtuple_rec`、`my_strnxfrm_*`、
`Commit_stage_manager`、`rec_get_offsets`），需要评估/实施补丁。
- 要理解参考补丁示例的机制、适用条件、风险与验证方式（真实补丁须由操作者提供）。
- 要挖掘新的 aarch64/结构优化点。
- 补丁出现负收益/回归，需要回滚与归因。

**不适用**：通用 C/C++ 向量化/循环/汇编 → `kpbot-code-optimizer`；OS/配置 →
`mysql-os-optimization`/`mysql-app-config-optimization`；部署/构建流程 →
`mysql-build-deployment`；压测方法 → `mysql-test-verification`。

## 工作流

| 步 | 动作 | 产出 |
| --- | --- | --- |
| 1 | 固定场景与热点（perf/off-CPU/等待） | 热点函数与机制 |
| 2 | 读源码，确认调用路径与语义边界 | 改动点/回退点 |
| 3 | 写最小补丁（条件编译、默认回退） | patch/diff |
| 4 | debug 构建 + `ut_ad` 等价校验 | 等价证据 |
| 5 | MTR 相关 suite/全量 + 冒烟/对账 | 正确性 |
| 6 | 单 patch 编译 + 四场景 A/B | 收益/波动 |
| 7 | 判定保留/回滚，记录负结果 | 台账 |

## 补丁清单（机制示例；详见 references/patch-catalog.md）

下表与参考资料均为机制示例，仓内不附带补丁原文；实际操作须由操作者提供与目标源码版本匹配的
真实 `.patch` 文件（例如 `my_strnxfrm` 相关改动也要按目标 collation 实现提供）。

| Patch | 核心改动 | 适用 | 关键风险 |
| --- | --- | --- | --- |
| fast-tuple | `dtuple_fetch_partial_cmp_rec` + `rec_direct_memcmp`，固定字段直接比较 | Point/range | 比较语义/字段条件 |
| Read View version | `readview_version` 原子计数，`view_open` 无锁复用 | 高并发只读/RC | 可见性/长事务/purge |
| space-skipping + NEON | ASCII 16B 批量计数/空格；8B sort key；16B 哈希 | RO order/distinct | 非 ASCII 回退/collation |
| binlog ordered | 每队列 32 组 pending lock/cond | RW/WO 高并发 | 提交顺序/唤醒/恢复 |
| rec_get_offsets | 传 compact 标志，省重复判断 | 读路径 | compact 判断错误 |
| binlog preallocate | `fallocate` + 有效 EOF + `ftruncate` | 写多/大 binlog | 空间/读取边界/崩溃 |

## 语义边界（必须逐项检查）

- **比较**：字段类型、NULL、变长、collation、降序、min-rec、instant cols、multi-value。
- **可见性**：RC/RR、活跃事务、Read View 复用条件、purge 推进。
- **字符**：ASCII 快路径只能用于确定 ASCII 的段；非 ASCII 必须回退；尾空格/大小写/
重音语义必须与 MTR 期望一致。
- **提交/复制**：提交顺序、fdatasync/fsync、binlog EOF、rotate、崩溃恢复、主从一致。
- **持久化**：binlog preallocate 的有效长度、零区判断、关闭回收。
- **平台**：`__aarch64__ && __linux__` 条件；x86 不受影响；CPU 特性（LSE/CRC/NEON）。

## 实施纪律

1. 补丁必须可**默认回退**（条件不满足走原逻辑）。
2. 先加 debug 等价校验（如 `cmp_dtuple_rec` 对照），再关闭。
3. 一个 patch 一个机制；不要混合多个优化。
4. 基线/候选分别构建并记录 md5、编译参数、commit。
5. 正确性门禁：MTR + 冒烟 + 数据对账 + 回读；读写补丁加复制/崩溃用例。
6. 性能：四场景 ≥3 轮、同口径、波动 <5%；负收益回滚。
7. 高风险补丁（视图/提交/binlog）需备份、窗口、回滚演练。

## 挖掘新优化点

1. perf 热点函数 → 查调用频率/参数 → 判断是否有重复计算、可批量、可跳过。
2. 看数据布局：固定长度/连续内存/ASCII 段是否可批量。
3. 看锁：是否有全局 mutex/条件变量可分组/分片。
4. 看分配/拷贝：临时对象、`std::move(const&)`、重复构造、哈希/字符串。
5. 先证明语义等价，再做性能；小步提交，记录负结果。

## 风险与回滚

| 变更 | 风险 | 回滚 |
| --- | --- | --- |
| 比较/排序补丁 | 结果错误/假性能 | 原补丁反向 `--index`（见下），重编，MTR 复验 |
| 视图/事务补丁 | 可见性/长事务/purge 异常 | 原补丁反向 `--index`（见下），重编，事务用例 |
| 组提交补丁 | 顺序/唤醒/恢复 | 原补丁反向 `--index`（见下），复制/崩溃用例 |
| binlog preallocate | 空间/读取边界 | 原补丁反向 `--index`（见下），`mysqlbinlog`/主从复验 |
| 构建参数 | 崩溃/ABI | 回退 flags，恢复二进制 |

### 源码补丁回滚（`apply_patch.sh --apply` 之后）

`apply_patch.sh --apply` 用 `git apply --index` 同时改动工作树和索引；只做 `git apply -R` 仅撤销
工作树，下一次提交仍可能带上补丁。必须用**应用时的同一个补丁文件**先反向校验，再同时回退两处：

```bash
git apply -R --index --check /abs/path/to/your-change.patch   # 反向校验；失败即停止，勿强推
git apply -R --index /abs/path/to/your-change.patch           # 同时撤销工作树与索引
git status --short                                            # 核对结果
git diff --cached --stat                                      # 确认其他已暂存改动仍在
```

`--check` 失败说明目标已被其他改动修改，按 `before.txt`/`after.txt` 与备份手工回退；不要强行反向。

回滚必须同时恢复二进制与（如涉及）数据/配置；没有数据快照不得声称可回退。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/patch-catalog.md` | 每个 patch 的位置、机制、代码要点、适用、风险、验证 |
| `scripts/apply_patch.sh` | 默认 dry-run/check（要求 Git 工作树、`git apply --index --check`）；`--apply --approved-change-id` 才应用并记录：先写 `before.txt`，应用后再写 `after.txt`，后者失败退出 3 并明确"已应用但回执不完整" |
