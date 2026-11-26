# 数据导入、注册与建图

## 1. 数据集选择

| 数据集 | 用途 | 压缩/解压 | 来源 |
|--------|------|-----------|------|
| LDBC SNB Interactive SF1 (CsvBasic, LongDateFormatter) | 12 条基准查询、社交网络异构图 | 约 230MB `.tar.zst` | `https://datasets.ldbcouncil.org/snb-interactive-v1/social_network-sf1-CsvBasic-LongDateFormatter.tar.zst` |
| Panama Papers（ICIJ Offshore Leaks） | 单节点/单边大图、实体搜索 | 约 73MB zip / 约 626MB 解压 | `https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip` |

> 注（数据路径分叉）：本表为**导入/基准用 v1 数据**（CsvBasic，入 `postgres`
> 库）。test-verification 的 LDBC driver 验证用**另一套 v2 数据**
> （`graphs/csv/bi/composite-merged-fk/` 形态，SF0.003 示例随上游仓库或自生成
> SF1+，**另建 `ldbcsnb` 库**）——两套 schema/布局互不兼容，来源与用法见
> `../pggraph-test-verification/references/ldbc-driver-verification.md` §3/§5。

下载后记录 URL、文件大小、SHA256，不要直接执行来源不明的解压脚本：

```bash
sha256sum "${LDBC_ARCHIVE}" | tee "${RESULTS_DIR}/dataset.sha256"
```

## 2. LDBC 归一化

原始 CSV 用 `|` 分隔、ID 无类型前缀。归一化目标：

1. 用 `zstd` 解压 `.tar.zst`。
2. 每种实体/关系的多分片流式合并，列名规范化（如 `firstName→first_name`、`type→kind`）。
3. 给源 ID 加类型前缀（`person:`、`forum:` 等），避免跨表 ID 冲突，并使边端点显式化。
4. 边写入时校验 src/dst 都在对应节点 ID 集合中，不在则丢弃，保证引用完整性。
5. 输出标准 CSV + `metadata.json`（节点/边计数、seed、schema 版本）。

### 2.1 节点表（8）

| 表 | 主键 | 可搜索列 |
|----|------|----------|
| `ldbc.persons` | `person_id` | first_name, last_name, gender |
| `ldbc.forums` | `forum_id` | title |
| `ldbc.posts` | `post_id` | language, content |
| `ldbc.comments` | `comment_id` | content |
| `ldbc.places` | `place_id` | name, kind |
| `ldbc.organisations` | `organisation_id` | name, kind |
| `ldbc.tags` | `tag_id` | name |
| `ldbc.tagclasses` | `tagclass_id` | name |

### 2.2 边表（23）

`person_knows_person`(knows)、`person_has_interest_tag`(has_interest)、
`person_is_located_in_place`、`person_likes_comment`、`person_likes_post`、
`person_study_at_organisation`(study_at)、`person_work_at_organisation`(work_at)、
`forum_container_of_post`(container_of)、`forum_has_member_person`(has_member)、
`forum_has_moderator_person`(has_moderator)、`forum_has_tag_tag`、`post_has_creator_person`、
`post_has_tag_tag`、`post_is_located_in_place`、`comment_has_creator_person`、
`comment_has_tag_tag`、`comment_is_located_in_place`、`comment_reply_of_comment`、
`comment_reply_of_post`、`organisation_is_located_in_place`、`place_is_part_of_place`、
`tag_has_type_tagclass`、`tagclass_is_subclass_of_tagclass`。

另有 `person_speaks_language`、`person_email` 作为多值属性表，用 `|` 分隔展开。

所有边在基准中注册为 `bidirectional := true`，因此图内边数是原始边数的 2 倍；
报告里的 `edges_loaded` 要注明这个口径。

### 2.3 建表与 COPY

```sql
DROP SCHEMA IF EXISTS ldbc CASCADE;
CREATE SCHEMA ldbc;

CREATE TABLE ldbc.persons (
  person_id text PRIMARY KEY,
  first_name text, last_name text, gender text,
  birthday text, creation_date text, location_ip text, browser_used text
);

CREATE TABLE ldbc.person_knows_person (
  src_person_id text NOT NULL REFERENCES ldbc.persons(person_id),
  dst_person_id text NOT NULL REFERENCES ldbc.persons(person_id),
  creation_date text
);

COPY ldbc.persons FROM '/path/to/persons.csv' WITH (FORMAT csv, HEADER true);
COPY ldbc.person_knows_person FROM '/path/to/person_knows_person.csv' WITH (FORMAT csv, HEADER true);

CREATE INDEX ldbc_persons_name_idx ON ldbc.persons(first_name, last_name);
```

建议：

- 大批量 COPY 前可临时关闭不必要的索引/约束，导入后重建；但边/主键约束用于发现数据问题，
  不要长期关闭。
- 用 `ON_ERROR_STOP=1`，避免半成品数据被当作成功。
- 记录 `COPY` 行数与 `metadata.json` 对账。

## 3. 注册

```sql
SELECT graph.add_table('ldbc.persons'::regclass, 'person_id',
                       ARRAY['first_name','last_name','gender']);
SELECT graph.add_table('ldbc.forums'::regclass, 'forum_id', ARRAY['title']);
-- ... 其余节点表 ...

SELECT graph.add_edge('ldbc.person_knows_person'::regclass, 'src_person_id',
                      'ldbc.persons'::regclass, 'dst_person_id', 'knows', true);
-- ... 其余 22 条边 ...

SELECT * FROM graph.registered_tables();
SELECT * FROM graph.registered_edges();
```

注册要求：

- `id_column` 是主键或唯一 NOT NULL 索引。
- 边端点列的类型/值与目标主键可比；跨表 fallback 解析按 table_oid 排序取第一个，
  能精确指定 `to_table` 就不要依赖 fallback。
- 过滤列单独注册，且必须注册**表上实际存在**的列（LDBC persons 原始属性只有
  firstName/lastName/gender/birthday 等，没有 age——年龄只能由 birthday 派生）：
  归一化阶段把 `birthday` 转成 date 后注册
  `graph.add_filter_column('ldbc.persons'::regclass,'birthday','date')`；
  需要 age 过滤时，先在归一化时生成派生列（如
  `date_part('year', age(birthday::date))`）再注册派生列；与搜索列是两套。
- 注册只写 catalog，不碰数据；重复注册前先确认，避免重复边。

## 4. 建图

```sql
SET graph.memory_limit_mb = 4096;       -- 按数据集留余量
SET graph.persist_on_build = on;
SET graph.auto_load = on;

SELECT row_to_json(b) FROM graph.build('csr_readonly') b;
```

或可变覆盖层：

```sql
SET graph.mutable_enabled = on;
SELECT row_to_json(b) FROM graph.build('mutable_overlay') b;
```

返回字段：`nodes_loaded`、`edges_loaded`、`build_time_ms`、`memory_used_mb`、
`sync_mode`、`projection_mode`。

注意事项：

- LDBC SF1 构建约数百秒；期间有 `NOTICE`（跳过不存在触发器）属正常。
- 构建中断可能留下 build 锁：查 `pg_stat_activity`，必要时 `pg_terminate_backend`。
- OOM 预检按 `~140B/节点 + ~5B/边` 估算；`memory_limit_mb` 太小会报错或降级只读。
- 构建后立即做一次 warm traverse，确认工件已加载。

## 5. 验收

```sql
-- 1) 规模与状态
SELECT * FROM graph.status();

-- 2) 功能：1-hop 返回 ≥1
SELECT count(*) FROM graph.traverse(
  'ldbc.persons'::regclass,
  (SELECT src_person_id FROM ldbc.person_knows_person LIMIT 1),
  1, hydrate := false, max_rows := 1);

-- 3) 注册数量
SELECT count(*) FROM graph._registered_tables;  -- 期望 8
SELECT count(*) FROM graph._registered_edges;   -- 期望 23

-- 4) 边数口径：graph.status() 统计全图（所有已注册关系），期望值必须按**全部已注册
--    关系表**逐张统计并按各自 bidirectional 设置加总（仅注册 knows 时才等于
--    count(person_knows_person) × 2；混入单向边时按 ×1 计）：
SELECT (SELECT sum(edge_count) FROM graph.status()) AS graph_edges,
       (SELECT count(*) FROM ldbc.person_knows_person) * 2 AS expected_knows_only;
-- 多关系注册时改为对每张已注册边表分别求和（bidirectional=true ×2，false ×1）后加总
```

## 6. Panama 数据集

- 结构：1 节点表 `panama.nodes`、1 边表 `panama.edges`；边注册为双向 `related_to`，
  用 `rel_type` 列做动态标签。
- 准备流程与 LDBC 类似：下载/解压/转换 → COPY → 注册 → build。
- 查询集：status、entity_search、traverse_depth_2、shortest_path、gql_one_hop_scalar、
  sql_join_one_hop_equivalent、component_stats、largest_component。
- 如果下载 URL 不可达，手工放置 zip 到 `sandbox/benchmark/datasets/panama/`；
  不要跳过 SHA256 校验。

## 7. 基准入口

### 7.1 官方 Docker

```bash
cd "${PGGraph_SRC}"
sandbox/run_benchmarks.sh ldbc --yes
sandbox/run_benchmarks.sh panama --yes
sandbox/run_benchmarks.sh ldbc --yes --prepare-only
sandbox/run_benchmarks.sh ldbc --yes --hot-iterations 20 --build-mode csr_readonly
```

环境变量：`PGGRAPH_PG_PORT`（默认 55432）、`PGGRAPH_CONTAINER_NAME`、
`PGGRAPH_IMAGE_NAME`、`PGGRAPH_REBUILD_IMAGE`、`PGGRAPH_RECREATE_CONTAINER`。

### 7.2 本地 PostgreSQL（无 Docker）

官方脚本依赖 Docker；本机部署需要本地包装器，把 `docker exec/cp/restart` 替换为
`psql`/文件复制/`pg_ctl restart`，并保持同样的 prepare → warm → hot 流程。
包装器必须参数化，不要把固定路径/IP/密码写死。

典型命令：

```bash
bash "${PGGraph_HOME}/pggraph_verify.sh" build
bash "${PGGraph_HOME}/pggraph_verify.sh" prepare ldbc
bash "${PGGraph_HOME}/pggraph_verify.sh" bench ldbc --iterations 10
bash "${PGGraph_HOME}/pggraph_verify.sh" throughput ldbc --iterations 20 --concurrency 64 --mp
```

### 7.3 脚本命令语义

| 命令 | 行为 | 破坏性 |
|------|------|--------|
| `copy` | 从只读备份覆盖工作树 | 是（覆盖工作树） |
| `build` | 编译安装 + 重启 PG | 低（会重启） |
| `prepare` | 建表/导入/注册/build；已就绪则跳过 | 是（DROP schema/重建） |
| `bench` | hot 基准，单连接，含 EXPLAIN/checksum | 否 |
| `throughput` | 多连接吞吐，原始 wall time，无 checksum | 否 |
| `status` | 只读诊断 | 否 |
| `restart` | 重启 PG | 中（中断连接） |
| `clean-graph` | `graph.reset()` + 清 catalog | 是（删工件/注册） |

执行破坏性命令前：确认可重建、已备份、用户明确同意；不要在生产实例上直接跑
`prepare`/`clean-graph`。

## 8. 备份与恢复

| 对象 | 方式 | 说明 |
|------|------|------|
| 源表 | `pg_dump`/物理备份 | 权威数据，必须备份 |
| 图工件 | 拷贝 `PGDATA/<data_dir>/<graph_id>` | 可选；格式/版本一致才可复用 |
| catalog | 随 `pg_dump` | 注册信息；也可用注册脚本重建 |
| 结果 | 保留 `report.json` + `sql_manifest` | 复现与 A/B 证据 |

恢复后：

1. 校验 PG 版本、扩展版本、工件格式。
2. `CREATE EXTENSION IF NOT EXISTS graph`。
3. 若工件不可复用：`graph.reset()` → 清 catalog → 重新注册 → `graph.build()`。
4. 用 1-hop traverse + row_count/checksum 验收。
