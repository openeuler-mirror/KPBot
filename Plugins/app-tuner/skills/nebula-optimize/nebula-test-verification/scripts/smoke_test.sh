#!/usr/bin/env bash
# smoke_test.sh — NebulaGraph 只读功能冒烟
#
# 通过 nebula-console 执行一组只读查询：主机/Space、计数、抽样属性、GO/MATCH/
# LOOKUP/FETCH/FIND SHORTEST PATH/GET SUBGRAPH。所有语句由固定只读模板生成，
# 标识符与字面值经白名单校验后拼入，不透传用户原始语句。不写数据、不修改 schema。
#
# 用法:
#   NEBULA_ADDR=127.0.0.1:9670 SPACE=ldbc_bench EXPECT_PERSON=933 \
#     bash smoke_test.sh
#
# 可选环境变量:
#   NEBULA_ADDR         地址:端口，默认 127.0.0.1:9669
#   NEBULA_USER         用户名，默认 root；建议使用只读账号
#   NEBULA_CONSOLE      console 路径，默认 nebula-console
#   NEBULA_CONSOLE_WRAPPER  可选包装命令，用于安全注入凭据（推荐）
#   SPACE               Space 名，默认 ldbc_bench
#   TAG / EDGE          计数用 Tag/Edge，默认 Person / KNOWS
#   EXPECT_PERSON       抽样 VID，默认 933
#   SAMPLE_PROP         抽样属性名，默认 firstName（NebulaGraph-Bench v1.2.0 Person 字段）
#   EXPECT_TAG_COUNT    期望 Tag 计数（可选，按 tag_cnt 结果行精确比较整数）
#   EXPECT_EDGE_COUNT   期望 Edge 计数（可选，按 edge_cnt 结果行精确比较整数）
#   EXPECT_HOSTS        期望 storaged 实例数（可选，仅核对数量；建议用 EXPECT_HOSTS_LIST）
#   EXPECT_HOSTS_LIST   期望实例集合（可选，逗号分隔 host:port，逐项核对在线集合）
#   FSP_FROM / FSP_TO   最短路径点对（可选，单独执行）
#   FSP_EXPECT          期望 reachable/unreachable，默认 reachable；
#                       unreachable 时校验空集，不匹配计入 FAIL
#   LOOKUP_TAG / LOOKUP_PROP / LOOKUP_VALUE / LOOKUP_LIMIT
#                       LOOKUP 只读模板参数（标识符白名单校验；字面值禁引号/
#                       反斜杠/任意空白（含换行/回车）/分号，含则 exit 2 拒绝执行）
#
# 输出: 每条检查 PASS/FAIL + 原始输出；存在 FAIL 时退出码非零。
set -u

usage() {
  cat <<'EOF'
用法: smoke_test.sh [--help]

只读 nGQL 冒烟。通过环境变量配置地址/Space/期望值；语句全部来自固定只读模板。

示例:
  NEBULA_ADDR=127.0.0.1:9670 SPACE=ldbc_bench EXPECT_PERSON=933 \
    EXPECT_TAG_COUNT=65645 EXPECT_EDGE_COUNT=1938516 bash smoke_test.sh

  FSP_FROM=933 FSP_TO=2199023256352 FSP_EXPECT=reachable SPACE=ldbc_bench bash smoke_test.sh

  LOOKUP_TAG=Person LOOKUP_PROP=firstName LOOKUP_VALUE=Tim LOOKUP_LIMIT=10 bash smoke_test.sh

安全:
  - 脚本不接收明文密码，也不把密码放进 console 命令行（ps 可见）。
  - 建议使用只读账号执行：CREATE USER readonly WITH PASSWORD '***';
    GRANT ROLE GUEST ON ldbc_bench TO readonly;（GRANT 不支持通配 *，须按具体 Space 授权，多个 Space 各授一次）
    root 等可写账号仅用于确认脚本自身不产生写操作。
  - 需要鉴权时用 NEBULA_CONSOLE_WRAPPER 注入凭据；无 wrapper 时 console 按自身方式提示。
  - 标识符（Space/Tag/Edge/属性）须匹配 ^[A-Za-z_][A-Za-z0-9_]*$；字面值经校验转义，
    拒绝内嵌引号/反斜杠/分号。
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) echo "ERROR: 未知参数: $1" >&2; usage >&2; exit 2 ;;
esac

NEBULA_ADDR="${NEBULA_ADDR:-127.0.0.1:9669}"
NEBULA_USER="${NEBULA_USER:-root}"
NEBULA_CONSOLE="${NEBULA_CONSOLE:-nebula-console}"
NEBULA_CONSOLE_WRAPPER="${NEBULA_CONSOLE_WRAPPER:-}"
SPACE="${SPACE:-ldbc_bench}"
TAG="${TAG:-Person}"
EDGE="${EDGE:-KNOWS}"
EXPECT_PERSON="${EXPECT_PERSON:-933}"
SAMPLE_PROP="${SAMPLE_PROP:-firstName}"
EXPECT_TAG_COUNT="${EXPECT_TAG_COUNT:-}"
EXPECT_EDGE_COUNT="${EXPECT_EDGE_COUNT:-}"
EXPECT_HOSTS="${EXPECT_HOSTS:-}"
FSP_FROM="${FSP_FROM:-}"
FSP_TO="${FSP_TO:-}"
FSP_EXPECT="${FSP_EXPECT:-reachable}"
NEBULA_VID_TYPE="${NEBULA_VID_TYPE:-int}"   # ldbc_bench 的 vid_type：int（Bench v1.2.0 默认导入）| string
LOOKUP_TAG="${LOOKUP_TAG:-}"
LOOKUP_PROP="${LOOKUP_PROP:-}"
LOOKUP_VALUE="${LOOKUP_VALUE:-}"
LOOKUP_LIMIT="${LOOKUP_LIMIT:-10}"

HOST="${NEBULA_ADDR%:*}"
PORT="${NEBULA_ADDR##*:}"

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_RST=""
fi
PASS_CNT=0; FAIL_CNT=0
pass() { PASS_CNT=$((PASS_CNT+1)); echo "${C_GRN}[PASS]${C_RST} $*"; }
fail() { FAIL_CNT=$((FAIL_CNT+1)); echo "${C_RED}[FAIL]${C_RST} $*"; }

if ! command -v "$NEBULA_CONSOLE" >/dev/null 2>&1; then
  echo "ERROR: 找不到 nebula-console: ${NEBULA_CONSOLE}" >&2
  echo "提示：设置 NEBULA_CONSOLE=/path/to/nebula-console" >&2
  exit 2
fi

CONSOLE_CMD=("$NEBULA_CONSOLE" -addr "$HOST" -port "$PORT" -u "$NEBULA_USER")
if [ -n "$NEBULA_CONSOLE_WRAPPER" ]; then
  read -r -a WRAP <<< "$NEBULA_CONSOLE_WRAPPER"
  CONSOLE_CMD=("${WRAP[@]}" "${CONSOLE_CMD[@]}")
fi
if [ -n "${NEBULA_PASS:-}" ]; then
  echo "WARN: 忽略 NEBULA_PASS；请用 NEBULA_CONSOLE_WRAPPER 注入凭据，避免密码进入 argv" >&2
fi

# ---- 输入校验：所有拼入语句的成分必须通过白名单，非法即退出（不执行任何查询）----
ident_ok() { printf '%s' "$1" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*$'; }
vid_ok()   { if [ "$NEBULA_VID_TYPE" = int ]; then printf '%s' "$1" | grep -qE '^[0-9]+$'; else printf '%s' "$1" | grep -qE '^[A-Za-z0-9_.:@+-]+$'; fi; }  # int 空间收紧为纯数字；string 空间放宽标识符成分
vid_lit()  { if [ "$NEBULA_VID_TYPE" = string ]; then printf "'%s'" "$1"; else printf '%s' "$1"; fi; }  # int 空间 VID 不加引号（v3.8.0 对 VID 严格类型校验，'933' 在 int64 空间直接 SemanticError）
int_ok()   { printf '%s' "$1" | grep -qE '^[0-9]+$'; }
lit_dq()   { has_cr_or_lf "$1" && return 1; printf '%s' "$1" | grep -qzE '^[^"\\[:space:];]*$'; }  # 整串白名单：-z 不按行切分、^$ 锚定整个值，[:space:] 覆盖换行/回车——禁引号/反斜杠/任意空白（含换行）/分号（grep 按行匹配时纯换行值会漏网，console 按行发语句可注入第二条语句）

BAD_INPUT=0
for v in "$SPACE" "$TAG" "$EDGE"; do
  ident_ok "$v" || { echo "ERROR: 非法标识符（仅限字母/数字/下划线，且不以数字开头）: $v" >&2; BAD_INPUT=1; }
done
ident_ok "$SAMPLE_PROP" || { echo "ERROR: 非法属性名: $SAMPLE_PROP" >&2; BAD_INPUT=1; }
vid_ok "$EXPECT_PERSON" || { echo "ERROR: 非法 VID 字面值: $EXPECT_PERSON" >&2; BAD_INPUT=1; }
[ -n "$FSP_FROM" ] && { vid_ok "$FSP_FROM" || { echo "ERROR: 非法 FSP_FROM: $FSP_FROM" >&2; BAD_INPUT=1; }; }
[ -n "$FSP_TO" ]   && { vid_ok "$FSP_TO"   || { echo "ERROR: 非法 FSP_TO: $FSP_TO" >&2; BAD_INPUT=1; }; }
# 端点须成对：只给一端时上面的校验会放行、FSP 段却被跳过，其余项 PASS 即 exit 0，
# 最短路径校验被静默绕过——一律按参数错误在连接前拒绝
if { [ -n "$FSP_FROM" ] || [ -n "$FSP_TO" ]; } && { [ -z "$FSP_FROM" ] || [ -z "$FSP_TO" ]; }; then
  echo "ERROR: FSP_FROM/FSP_TO 须同时给出或同时省略（只给一端会静默跳过最短路径校验）" >&2
  BAD_INPUT=1
fi
[ -n "$EXPECT_TAG_COUNT" ]  && { int_ok "$EXPECT_TAG_COUNT"  || { echo "ERROR: EXPECT_TAG_COUNT 须为非负整数" >&2; BAD_INPUT=1; }; }
[ -n "$EXPECT_EDGE_COUNT" ] && { int_ok "$EXPECT_EDGE_COUNT" || { echo "ERROR: EXPECT_EDGE_COUNT 须为非负整数" >&2; BAD_INPUT=1; }; }
[ -n "$EXPECT_HOSTS" ]      && { int_ok "$EXPECT_HOSTS"      || { echo "ERROR: EXPECT_HOSTS 须为非负整数" >&2; BAD_INPUT=1; }; }
case "$FSP_EXPECT" in reachable|unreachable) ;; *) echo "ERROR: FSP_EXPECT 仅支持 reachable/unreachable" >&2; BAD_INPUT=1 ;; esac
case "$NEBULA_VID_TYPE" in int|string) ;; *) echo "ERROR: NEBULA_VID_TYPE 仅支持 int|string（其他 VID 类型不在本冒烟示例口径内）" >&2; BAD_INPUT=1 ;; esac
if [ -n "$LOOKUP_TAG" ]; then
  ident_ok "$LOOKUP_TAG"  || { echo "ERROR: 非法 LOOKUP_TAG: $LOOKUP_TAG" >&2; BAD_INPUT=1; }
  ident_ok "$LOOKUP_PROP" || { echo "ERROR: 非法 LOOKUP_PROP: $LOOKUP_PROP" >&2; BAD_INPUT=1; }
  lit_dq "$LOOKUP_VALUE"  || { echo "ERROR: LOOKUP_VALUE 含引号/反斜杠/任意空白（含换行/回车）/分号，拒绝拼入语句" >&2; BAD_INPUT=1; }
  int_ok "$LOOKUP_LIMIT"  || { echo "ERROR: LOOKUP_LIMIT 须为非负整数" >&2; BAD_INPUT=1; }
fi
[ "$BAD_INPUT" -eq 0 ] || exit 2

if [ "$NEBULA_USER" = "root" ]; then
  echo "WARN: 当前使用 root（可写账号）。只读冒烟建议创建只读账号（GRANT ROLE GUEST ON ${SPACE} TO readonly）并设 NEBULA_USER。" >&2
fi

# ---- 查询构造：全部来自固定只读模板 ----
QUERY=$(cat <<EOF
SHOW SPACES;
USE ${SPACE};
DESCRIBE SPACE ${SPACE};
MATCH (n:${TAG}) RETURN count(n) AS tag_cnt;
MATCH ()-[e:${EDGE}]->() RETURN count(e) AS edge_cnt;
FETCH PROP ON ${TAG} $(vid_lit "$EXPECT_PERSON") YIELD ${TAG}.${SAMPLE_PROP};
GO 1 STEPS FROM $(vid_lit "$EXPECT_PERSON") OVER ${EDGE} YIELD ${EDGE}._dst | LIMIT 5;
GO 2 STEPS FROM $(vid_lit "$EXPECT_PERSON") OVER ${EDGE} YIELD ${EDGE}._dst | LIMIT 5;
MATCH (n:${TAG})-[:${EDGE}]->(m) WHERE id(n) == $(vid_lit "$EXPECT_PERSON") RETURN id(m) LIMIT 5;
GET SUBGRAPH 1 STEPS FROM $(vid_lit "$EXPECT_PERSON") YIELD VERTICES AS nodes, EDGES AS relations;
EOF
)
if [ -n "$LOOKUP_TAG" ]; then
  QUERY="${QUERY}
LOOKUP ON ${LOOKUP_TAG} WHERE ${LOOKUP_TAG}.${LOOKUP_PROP} == \"${LOOKUP_VALUE}\" YIELD id(vertex) AS vid, ${LOOKUP_TAG}.${LOOKUP_PROP} AS val LIMIT ${LOOKUP_LIMIT};"
fi

echo "Nebula smoke test"
echo "  addr=${NEBULA_ADDR} space=${SPACE} tag=${TAG} edge=${EDGE} vid=${EXPECT_PERSON}"
echo "------------------------------------------------------------"

OUT=$(mktemp)
ERR=$(mktemp)

# 单条语句单独调用，输出落在独立文件，便于按结果表精确解析
run_query() {
  # shellcheck disable=SC2068
  "${CONSOLE_CMD[@]}" -e "$1" >"$2" 2>"$ERR"
  local rc=$?
  cat "$2"
  if [ -s "$ERR" ]; then
    echo "--- stderr ---"
    cat "$ERR"
  fi
  return $rc
}

# 从 console 结果表按列名提取首个整数值行；缺列/缺行返回空串。
# 边界纪律：命中目标列头后，遇到「任何新的表头行」（|...| 内含字母，即下一张表）
# 立即结束——相邻两张结果表粘连时，避免把后一张表的数据行算给前一张的列
# （Tag 表数据缺失、Edge 表数据补位的反例：提取 tag_cnt 会读到 edge_cnt 的值）。
table_value() {
  local col="$1" file="$2"
  awk -v col="$col" '
    BEGIN { hdr=0 }
    !hdr && $0 ~ ("\\|[[:space:]]*" col "[[:space:]]*\\|") { hdr=1; next }
    hdr && $0 ~ ("\\|[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\\|") { exit }
    hdr && /^[[:space:]]*[|+]+[[:space:]]*$/ { next }
    hdr && /^[[:space:]]*$/ { exit }
    hdr && /^[[:space:]]*\|[[:space:]]*[0-9]+[[:space:]]*\|[[:space:]]*$/ {
      gsub(/[^0-9]/, ""); print; exit
    }
    hdr && /Empty set/ { print ""; exit }
  ' "$file"
}

# ---- 主批次 ----
run_query "$QUERY" "$OUT"
RC=$?
if [ "$RC" -ne 0 ]; then
  fail "nebula-console 退出码 ${RC}"
else
  pass "nebula-console 主批次执行完成"
fi
if grep -qiE "SemanticError|SyntaxError|Storage Error|Error:|Invalid param|Missing yield|not existed|\[ERROR \(" "$OUT"; then
  fail "输出包含查询错误（见上方原始输出；console 执行错误统一带 [ERROR (<code>)] 前缀）"
else
  pass "未发现查询错误关键字"
fi

# ---- Tag/Edge 计数：按列名解析结果行，精确比较整数 ----
if [ -n "$EXPECT_TAG_COUNT" ]; then
  GOT_TAG=$(table_value tag_cnt "$OUT")
  if [ -z "$GOT_TAG" ]; then
    fail "未解析到 tag_cnt 结果行（缺列/缺行）"
  elif [ "$GOT_TAG" = "$EXPECT_TAG_COUNT" ]; then
    pass "Tag 计数精确匹配: tag_cnt=${GOT_TAG}"
  else
    fail "Tag 计数不匹配: tag_cnt=${GOT_TAG} 期望=${EXPECT_TAG_COUNT}"
  fi
else
  echo "[SKIP] 未设置 EXPECT_TAG_COUNT，跳过 Tag 计数校验"
fi

if [ -n "$EXPECT_EDGE_COUNT" ]; then
  GOT_EDGE=$(table_value edge_cnt "$OUT")
  if [ -z "$GOT_EDGE" ]; then
    fail "未解析到 edge_cnt 结果行（缺列/缺行）"
  elif [ "$GOT_EDGE" = "$EXPECT_EDGE_COUNT" ]; then
    pass "Edge 计数精确匹配: edge_cnt=${GOT_EDGE}"
  else
    fail "Edge 计数不匹配: edge_cnt=${GOT_EDGE} 期望=${EXPECT_EDGE_COUNT}"
  fi
else
  echo "[SKIP] 未设置 EXPECT_EDGE_COUNT，跳过 Edge 计数校验"
fi

# ---- SHOW HOSTS：单独执行，按 host:port 解析实例集合，逐项核对 ----
# 纪律：只比数量不够——预期节点 A、B，实际在线 A、C 时数量相等仍会误判通过；
# 从数据行提取「host:端口 状态」，构建实际在线集合与预期集合逐项比较。
OUT_HOSTS=$(mktemp)
run_query "SHOW HOSTS;" "$OUT_HOSTS" >/dev/null
ONLINE_SET=$(awk -F'|' '
  /^\|/ && $0 !~ /Host/ {
    gsub(/[[:space:]]/, "", $2); host=$2; gsub(/[[:space:]]/, "", $3); port=$3
    st=""; for (i=4; i<=NF; i++) { if ($i ~ /ONLINE/) st="ONLINE"; else if ($i ~ /OFFLINE/) st="OFFLINE" }
    if (host != "" && st == "ONLINE") print host ":" port
  }' "$OUT_HOSTS" 2>/dev/null | sort)
OFFLINE_ROWS=$(grep -cE '^\|.*[[:space:]]"?OFFLINE"?' "$OUT_HOSTS" || true)
ONLINE_ROWS=$(printf '%s\n' "$ONLINE_SET" | grep -c . || true)
if [ "$ONLINE_ROWS" -eq 0 ] && [ "$OFFLINE_ROWS" -eq 0 ]; then
  fail "SHOW HOSTS 未解析到任何实例数据行（仅表头？）"
elif [ "$OFFLINE_ROWS" -gt 0 ]; then
  fail "SHOW HOSTS 存在 ${OFFLINE_ROWS} 个 OFFLINE 实例（须全部 ONLINE）"
elif [ -n "${EXPECT_HOSTS_LIST:-}" ]; then
  EXPECT_SET=$(printf '%s\n' "$EXPECT_HOSTS_LIST" | tr ',' '\n' | sed 's/[[:space:]]//g' | sort)
  if [ "$ONLINE_SET" = "$EXPECT_SET" ]; then
    pass "SHOW HOSTS 在线实例集合与预期逐项一致（${ONLINE_ROWS} 个）"
  else
    miss=$(comm -23 <(printf '%s\n' "$EXPECT_SET") <(printf '%s\n' "$ONLINE_SET") | tr '\n' ' ')
    extra=$(comm -13 <(printf '%s\n' "$EXPECT_SET") <(printf '%s\n' "$ONLINE_SET") | tr '\n' ' ')
    fail "SHOW HOSTS 实例集合不匹配：缺失[${miss}] 多余[${extra}]"
  fi
elif [ -n "$EXPECT_HOSTS" ] && [ "$ONLINE_ROWS" -ne "$EXPECT_HOSTS" ]; then
  fail "SHOW HOSTS ONLINE 实例数 ${ONLINE_ROWS} != 期望 ${EXPECT_HOSTS}（仅数量核对，建议用 EXPECT_HOSTS_LIST 逐项核对）"
else
  pass "SHOW HOSTS 全部实例 ONLINE（${ONLINE_ROWS} 个）"
fi

# ---- LOOKUP：单独执行，按错误前缀/空集/结果行独立校验（与 FSP/计数同级）----
# run_query 每次都启动新的 nebula-console 会话，主批次的 USE 不跨会话生效，
# 新会话必须先选择已校验的 SPACE 再执行查询
if [ -n "$LOOKUP_TAG" ]; then
  OUT_LKP=$(mktemp)
  run_query "USE ${SPACE}; LOOKUP ON ${LOOKUP_TAG} WHERE ${LOOKUP_TAG}.${LOOKUP_PROP} == \"${LOOKUP_VALUE}\" YIELD id(vertex) AS vid, ${LOOKUP_TAG}.${LOOKUP_PROP} AS val LIMIT ${LOOKUP_LIMIT};" "$OUT_LKP" >/dev/null
  if grep -q '\[ERROR (' "$OUT_LKP"; then
    fail "LOOKUP 执行错误（无索引时为 [ERROR (-1005)]: There is no index to use at runtime，先建索引并 REBUILD）"
  elif grep -q "Empty set" "$OUT_LKP"; then
    fail "LOOKUP 结果为空（Empty set）：核对属性值/属性名是否真实存在"
  else
    LKP_ROWS=$(grep -cE '^\|[^|]*\|' "$OUT_LKP" || true)
    LKP_ROWS=$((LKP_ROWS - 1))        # 扣掉列头行
    if [ "${LKP_ROWS:-0}" -ge 1 ]; then
      pass "LOOKUP 返回结果行（${LKP_ROWS} 行）"
    else
      fail "LOOKUP 未解析到结果行"
    fi
  fi
  rm -f "$OUT_LKP"
fi

# ---- 最短路径：单独执行，按列内容/空集精确校验 ----
if [ -n "$FSP_FROM" ] && [ -n "$FSP_TO" ]; then
  OUT_FSP=$(mktemp)
  run_query "USE ${SPACE}; FIND SHORTEST PATH FROM $(vid_lit "$FSP_FROM") TO $(vid_lit "$FSP_TO") OVER * YIELD path AS p;" "$OUT_FSP" >/dev/null
  HAS_PATH=$(grep -cE '^\|[^|]*<[^|]*\|' "$OUT_FSP" || true)     # 路径值行必含 < 与 >
  EMPTY_SET=$(grep -c "Empty set" "$OUT_FSP" || true)
  if [ "$FSP_EXPECT" = "unreachable" ]; then
    if [ "$EMPTY_SET" -gt 0 ] && [ "$HAS_PATH" -eq 0 ]; then
      pass "不可达点对校验为空集"
    else
      fail "期望不可达（空集），但返回了 ${HAS_PATH} 行路径"
    fi
  else
    if [ "$HAS_PATH" -gt 0 ]; then
      pass "可达点对返回路径（${HAS_PATH} 行）"
    else
      fail "期望可达路径，实际为空（Empty set 或无值行）"
    fi
  fi
  rm -f "$OUT_FSP"
fi

echo "------------------------------------------------------------"
echo "Summary: PASS=${PASS_CNT} FAIL=${FAIL_CNT}"
rm -f "$OUT" "$OUT_HOSTS" "$ERR"
if [ "$FAIL_CNT" -gt 0 ]; then
  exit 1
fi
exit 0
