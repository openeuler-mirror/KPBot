# 功能正确性验证

## 1. 构建验证

优化/升级后先确认“编出来的东西”可信：

```bash
# 产物存在、大小合理
ls -lh "${BUILD_DIR}/bin/nebula-"{metad,graphd,storaged}

# 版本与库身份
strings "${BUILD_DIR}/bin/nebula-graphd" | grep -m1 -E 'jemalloc|[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]+'
# 注意：这是版本串核对，不是下载物完整性校验；sha256/GPG/commit 校验应在编译前完成
md5sum "${BUILD_DIR}/bin/nebula-"{metad,graphd,storaged}
git -C "${NEBULA_HOME}" rev-parse --short HEAD

# 关键符号（按改动选择）
nm -C "${BUILD_DIR}/bin/nebula-storaged" | grep -E "findFirstNull|TscHelper" || true
```

| 检查 | 通过标准 |
|------|----------|
| 产物 | 三个二进制存在，大小与基线同量级 |
| jemalloc | native 5.2.x / 优化版 5.3.x（与构建记录一致） |
| 符号 | 新函数/补丁符号存在（如 `findFirstNull`） |
| 身份 | md5 与 commit 已记录，能和基线区分 |

> 只验证“符号存在”不代表逻辑正确；仍需功能冒烟和路径比对。

## 2. C++ 单元测试

前提：CMake 配置 `-DENABLE_TESTING=ON`，并完成测试目标编译。

```bash
cd "${BUILD_DIR}"
ctest -j"$(nproc)" --output-on-failure
# 只跑某模块
ctest -L storage --output-on-failure
```

要点：

- 单测使用 MockCluster/进程内数据，不依赖运行中的集群。
- 优化涉及编解码、表达式、迭代器、RocksDB 封装时，优先跑对应模块。
- 失败用例必须定位到“代码回归”还是“环境/数据问题”，不能直接跳过。

## 3. TCK（端到端语义）

Nebula 的 TCK 用 pytest-bdd 执行 `.feature`，覆盖 MATCH/GO/LOOKUP/bugfix/optimizer/
expression 等。总量约 241 个 feature（以目标版本为准）。

```bash
# 具体入口以源码 tests/ 为准；一般需要 Python 依赖与测试数据
cd "${NEBULA_HOME}/tests"
python3 -m pytest -q tck/            # 路径按版本调整
```

注意：

- pytest-bdd 4.0.2 与 Python 3.11 存在兼容问题（正则/Scenario Outline 解析），
  遇到导入/解析错误先核对 Python 与依赖版本，不要误判为 Nebula 回归。
- TCK 依赖 NBA/Student/ngdata 等数据集；数据集缺失时先导入或跳过相关用例。
- TCK 通过率要与基线对比；优化前后差异必须逐条解释。

## 4. nGQL 冒烟

> 本节口径：按 NebulaGraph-Bench v1.2.0 默认导入（INT64 VID、Person 字段 firstName/
> lastName/birthday/creationDate 等），birthday 为 **string、格式 yyyy-MM-dd**（bench 导入
> 模板不做日期类型转换；若用 build-deployment 的 importer YAML 导入，birthday 为 datetime，
> 本节 birthday 相关示例需相应调整口径）。

### 4.1 主机与 Space

```ngql
SHOW HOSTS;
SHOW SPACES;
USE ldbc_bench;
DESCRIBE SPACE ldbc_bench;
SHOW TAGS;
SHOW EDGES;
```

期望：所有 host `ONLINE`，分区数符合建 Space 时的 `partition_num`，Tag/Edge 与 schema 一致。

### 4.2 计数与抽样

```ngql
MATCH (n:Person) RETURN count(n);
MATCH ()-[e:KNOWS]->() RETURN count(e);

FETCH PROP ON Person '933' YIELD Person.firstName, Person.lastName, Person.birthday;
```

期望：计数与导入对账一致；属性非空且类型正确（birthday 为 string、格式 yyyy-MM-dd，字符串长度合理）。

### 4.3 遍历与路径

```ngql
// 前提：示例 Space 为 FIXED_STRING VID（'933' 带引号）。int64 VID 空间（Bench v1.2.0 默认）
// 写 GO 1 STEP FROM 933 ...（不带引号），带引号的字符串在 int64 空间报 SemanticError: Vid should be a INT64。
GO 1 STEP FROM '933' OVER KNOWS YIELD KNOWS.creationDate;
GO 2 STEP FROM '933' OVER KNOWS YIELD KNOWS.creationDate;
MATCH (n:Person)-[:KNOWS]->(m:Person) WHERE id(n) == '933' RETURN m LIMIT 5;
MATCH p = (n:Person)-[:KNOWS*1..2]->(m:Person) WHERE id(n) == '933' RETURN p LIMIT 3;
GET SUBGRAPH 2 STEPS FROM '933' YIELD VERTICES AS nodes, EDGES AS relations;
```

### 4.4 索引

前提：3.8.0 的 LOOKUP **只走索引扫描**——无可用索引时直接报错，不存在"无索引→慢速全表
兜底"（官方文档前提条件：请确保 LOOKUP 语句有至少一个索引可用）。NebulaGraph-Bench v1.2.0
的导入模板只建 Space/Tag/Edge，不含任何 CREATE INDEX，需先建索引并 REBUILD 生效；注意官方
告警"索引会大幅降低写性能"，压测 Space 不要顺手建索引。

```ngql
CREATE TAG INDEX person_birthday_idx ON Person(birthday(20));
REBUILD TAG INDEX person_birthday_idx;

LOOKUP ON Person WHERE Person.birthday < '1990-01-01' YIELD id(vertex), Person.firstName, Person.lastName LIMIT 10;
LOOKUP ON Person WHERE Person.birthday == '1981-04-30' YIELD id(vertex), Person.firstName;
```

期望：结果与已知答案一致；无可用索引时报错（先建索引再执行），而不是变慢。

### 4.5 最短路径

```ngql
FIND SHORTEST PATH FROM '933' TO '2199023256352' OVER * YIELD path AS p;
FIND SHORTEST PATH FROM '<不可达起点>' TO '<不可达终点>' OVER * YIELD path AS p;
```

- 已知点对：与基线逐条比对路径（顶点序列、边类型、方向、rank）。
- 不可达点对：期望空结果，不能报错或返回错误路径。
- 双向/多路径：注意返回顺序可能不稳定，比对“集合/路径本身”而不是行序。

### 4.6 写入回读（隔离 Space）

每次运行创建**独立 Space**，命名带时间戳/进程号（如 `smoke_rw_$(date +%s)_$$`）。**不要用
`IF NOT EXISTS`**：复用同名 Space 会更新并删除他人数据，清理时还会影响其他使用者；
Space 已存在时 `CREATE SPACE` 会报错——按报错停止，不要降级复用。

拆开执行并逐段核验成功——不要把 CREATE、USE、写入、DROP 拼进同一个可整段粘贴的
语句块：客户端遇错继续时，重跑会让 CREATE 报错后继续 USE/写入/DROP **上一次**的同名
空间。每次唯一由 shell 变量生成、创建失败立即停止：

```bash
SN="smoke_rw_$(date +%s)_$$"    # 本次唯一的 Space 名（shell 变量注入，勿手抄固定名）
```

```ngql
CREATE SPACE ${SN} (partition_num=1, replica_factor=1, vid_type=FIXED_STRING(32));
```

上一步失败（如 Space 已存在）即停——不 USE、不写入。成功后再继续：

```ngql
USE ${SN};
CREATE TAG T(v int);
INSERT VERTEX T(v) VALUES 'a':(1), 'b':(2);
FETCH PROP ON T 'a' YIELD T.v;          // 期望 1
UPDATE VERTEX ON T 'a' SET T.v = 3;
FETCH PROP ON T 'a' YIELD T.v;          // 期望 3
DELETE VERTEX 'a';
FETCH PROP ON T 'a' YIELD T.v;          // 期望空
DROP SPACE ${SN};                       // 只清理本次创建的 Space（变量保证非他人空间）
```

写入测试不要在生产 Space/业务库上执行；只清理本次创建的对象——Space/Tag/数据都在
本次 Space 内，`DROP SPACE` 一次即全部回收，不得删除其他使用者创建的任何对象。

## 5. 数据对账

### 5.1 计数对账

| 对象 | 期望来源 | 记录 |
|------|---------|------|
| Person | 数据集/导入日志 | 期望 vs 实际 |
| Comment/Post/Forum | 同上 | 期望 vs 实际 |
| KNOWS/LIKES/HAS_TAG 等 | 同上 | 期望 vs 实际 |
| Space vertices | 各 Tag 之和（可能因 VID 重叠略有差异） | 说明差异原因 |

LDBC 注意：不同实体 VID 数值区间可能重叠，导致少量顶点合并/同 key 边覆盖；
**Tag 计数应精确**，Space 级 vertices 可能小于 Tag 之和，属于数据固有现象，不是导入失败。

### 5.2 属性对账

- 抽样 10–100 个点/边，与 CSV 原始行逐字段比对。
- 重点检查：日期格式、字符串截断/补 `\0`、空值、rank、方向。
- 有索引的属性，与无索引查询结果比对。

### 5.3 索引对账

```ngql
// 索引查询（字段用 schema 实有属性；LDBC Person 无 age）
LOOKUP ON Person WHERE Person.birthday == '1981-04-30' YIELD id(vertex);
// 全表/已知集合对照（小数据集可用 MATCH + WHERE）
MATCH (n:Person) WHERE n.Person.birthday == '1981-04-30' RETURN id(n);
```

大批量数据不要用全表 MATCH 做对照，改用抽样 + 已知答案 + 计数。

## 6. 优化后的语义保持

源码优化（容器替换、拷贝消除、缓存、向量化）最容易引入“结果看起来差不多”的错误。
每次优化后至少跑：

1. 4.1–4.5 冒烟全通过。
2. 已知点对的最短路径逐条比对（路径集合一致）。
3. 不可达点对返回空集。
4. INSERT → FETCH 回读 → UPDATE → DELETE → 再 FETCH。
5. 索引查询与基线结果一致。
6. 计数与基线一致（写测试在隔离 Space）。

建议把基线结果保存为文本/JSON，优化后做 diff，而不是人工“看着对”。

## 7. 常见错误签名

| 现象 | 可能原因 | 先查 |
|------|---------|------|
| `SyntaxError near 'Tag'` | `Tag` 是保留字 | 用反引号 `` `Tag` `` |
| `space vid_type must be specified` | CREATE SPACE 缺 vid_type | 加 `vid_type=int` 或 `FIXED_STRING(n)` |
| `Missing yield clause` | LOOKUP 缺 YIELD | 补 `YIELD id(vertex), ...` |
| `Invalid param!` | 字符串索引缺长度 | `name(64)` 而非 `name` |
| 查询返回空但应有数据 | 数据未导入/恢复、Space 选错、分区 OFFLINE | `SHOW HOSTS`、计数、日志 |
| 结果与基线不同但无报错 | 语义回归、路径顺序、索引选择变化 | 逐条 diff、固定排序、对比执行计划 |
| 写入超时/失败率升高 | 磁盘满、WAL/compaction、Raft 选举 | `df -h`、storaged 日志、磁盘 await |
| 启动后部分 Space 缺失 | meta/storage 未成对恢复 | 恢复流程与 `cluster.id` |

## 8. 功能验证报告模板

| 字段 | 内容 |
|------|------|
| 版本/身份 | Nebula commit、二进制 md5、third-party/jemalloc/RocksDB |
| 环境 | 硬件/NUMA/OS/编译器、部署形态、绑核 |
| 构建 | 产物、符号、库版本、单测结果 |
| TCK | 通过/失败数、失败条目与原因 |
| 冒烟 | 每条查询、期望、实际、结论 |
| 对账 | Tag/Edge 期望 vs 实际、差异说明 |
| 写测试 | Space、操作序列、回读结果 |
| 结论 | 通过/不通过；不通过的具体回归点 |
