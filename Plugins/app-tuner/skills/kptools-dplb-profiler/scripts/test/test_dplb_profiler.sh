#!/bin/bash
# =============================================================================
# kptools-dplb-profiler 测试套件（采集端）
# 覆盖：脚本语法、采集端 parse_row 行为（TPOT 指标名命中/counter 口径/负 queue
#       修正/PD 双池数组分离）、回采端纯函数（幽灵 tag/模糊建议/queue_fixed/fnum/
#       未导出列剔除）。
#       报告端回归见 kptools-dplb-report/scripts/test/test_dplb_report.sh。
# 用法: bash scripts/test/test_dplb_profiler.sh
# =============================================================================
set +e

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TEST_DIR/../.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
TEST_PASSED=0
TEST_FAILED=0
BASH_BIN=$(command -v bash)
export PYTHONDONTWRITEBYTECODE=1

# 解析可用的 Python：逐个验证能否真正执行（规避 Windows 商店的 python 占位 stub）
find_py() {
    local c
    for c in python3 python py; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}
PY_BIN="$(find_py)"
if [ -z "$PY_BIN" ]; then
    echo "[FATAL] 未找到可用的 python3/python/py，无法运行测试套件" >&2
    exit 2
fi

TMP="$(mktemp -d /tmp/lbprof_test.XXXXXX 2>/dev/null || mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass() { echo "  [PASS] $1"; TEST_PASSED=$((TEST_PASSED + 1)); }
fail() { echo "  [FAIL] $1"; TEST_FAILED=$((TEST_FAILED + 1)); }
check() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expect=[$3] got=[$2])"; fi; }

run_py_suite() {
    # 运行 Python 子套件一次：透传输出并按末行 "N/M" 计入汇总
    local name="$1" file="$2" out rc frac
    out=$("$PY_BIN" "$file" 2>&1)
    rc=$?
    echo "$out"
    frac=$(echo "$out" | grep -oE '[0-9]+/[0-9]+' | tail -1)
    [ -n "$frac" ] || frac="rc=$rc"
    if [ "$rc" -eq 0 ]; then
        pass "$name ($frac)"
    else
        fail "$name ($frac)"
    fi
}

echo "== 0. 语法检查 =="
for s in extract_metric_v2.sh extract_metric_pd.sh extract_metric_single.sh lbprof_common.sh; do
    if "$BASH_BIN" -n "$SCRIPTS/$s" 2>/dev/null; then pass "$s syntax"; else fail "$s syntax"; fi
done

# 首字节断言：所有可执行脚本必须以 "#!" 开头（无 UTF-8 BOM / 无前导空白）。
# 部署侧按 SKILL.md 以 ./xxx.sh / nohup ./xxx.sh 方式执行（走 shebang）；首字节若是
# BOM（EF BB BF）则 execve 报 ENOEXEC，nohup 直接 Exec format error，PD 采集整链断掉。
for s in extract_metric_v2.sh extract_metric_pd.sh extract_metric_single.sh lbprof_common.sh extract_metric_prom.py; do
    check "$s 首两字节为 #!（无 BOM）" "$(head -c 2 "$SCRIPTS/$s")" "#!"
done
if awk -f "$SCRIPTS/lbprof_common.awk" </dev/null 2>/dev/null; then pass "lbprof_common.awk syntax"; else fail "lbprof_common.awk syntax"; fi
for p in extract_metric_prom.py; do
    if "$PY_BIN" -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$SCRIPTS/$p" 2>/dev/null; then
        pass "$p syntax"
    else
        fail "$p syntax"
    fi
done

echo "== 1. 采集端 parse_row（v2：TPOT/命中率/负queue/列数） =="
sed -n '/^lbprof_parse_row_v2()/,/^}$/p' "$SCRIPTS/lbprof_common.sh" > "$TMP/parse_row.sh"
# shellcheck disable=SC1090
source "$TMP/parse_row.sh"
# 行发射依赖公共 awk 库（唯一副本）经变量拼接加载
LBPROF_AWK_LIB="$(cat "$SCRIPTS/lbprof_common.awk")"

BODY='# HELP vllm:num_requests_running
vllm:num_requests_running{model="x",engine="0"} 5
vllm:request_success_total{engine="0",finished_reason="stop"} 100
vllm:request_success_total{engine="0",finished_reason="length"} 50
vllm:prompt_tokens_total{engine="0"} 12000
vllm:generation_tokens_total{engine="0"} 6000
vllm:gpu_cache_usage_perc{engine="0"} 0.42
vllm:prefix_cache_hits_total{engine="0"} 80
vllm:prefix_cache_queries_total{engine="0"} 100
vllm:external_prefix_cache_hits_total{engine="0"} 0
vllm:external_prefix_cache_queries_total{engine="0"} 0
process_start_time_seconds{engine="0"} 1793797588
vllm:time_to_first_token_seconds_sum{engine="0"} 50.5
vllm:time_to_first_token_seconds_count{engine="0"} 150
vllm:request_queue_time_seconds_sum{engine="0"} -4242.17
vllm:request_queue_time_seconds_count{engine="0"} 150
vllm:prefill_time_seconds_sum{engine="0"} 4300.0
vllm:prefill_time_seconds_count{engine="0"} 150
vllm:request_time_per_output_token_seconds_sum{engine="0"} 29.6
vllm:request_time_per_output_token_seconds_count{engine="0"} 1200
vllm:e2e_request_latency_seconds_sum{engine="0"} 4350.5
vllm:e2e_request_latency_seconds_count{engine="0"} 150'

ROW=$(printf '%s' "$BODY" | lbprof_parse_row_v2 "2026-11-17 11:33:34" "10.0.0.1:9100")
NCOLS=$(echo "$ROW" | awk -F, '{print NF}')
check "v2: 输出 33 列" "$NCOLS" "33"

# 列索引: 0 ts 1 tag | 2 running 3 waiting 4 swapped 5 success 6 failure 7 preempt 8 prompt 9 gen
#         10 validation 11 kv 12 cpu 13 prefix_hit 14 pending 15 ext_hit 16 ps | 17 ttft_avg
#         18 queue_avg 19 prefill_avg 20 tpot_avg 21 e2e_avg | 22 ttft_sum 23 ttft_count
#         24 queue_sum 25 queue_count 26 prefill_sum 27 prefill_count 28 tpot_sum 29 tpot_count
#         30 e2e_sum 31 e2e_count | 32 queue_fixed
get() { echo "$ROW" | awk -F, -v i="$1" '{print $i}'; }
check "v2: success=150 (拆 finished_reason 求和)" "$(get 6)" "150"
check "v2: 本地命中率 0.8 (hits/queries counter)" "$(get 14)" "0.8"
check "v2: 外部命中率 0 (0/0 记 0 而非缺失)" "$(get 16)" "0"
check "v2: tpot_avg 来自 request_time_per_output_token" "$(get 21)" "0.0246667"
check "v2: queue_sum 负值修正为 0" "$(get 25)" "0"
check "v2: prefill_sum 吸收负 queue (4300-4242.17=57.83)" "$(get 27)" "57.83"
check "v2: queue_fixed=1" "$(get 33)" "1"

# 无负 queue 的正常行
ROW2=$(printf '%s' "$BODY" | sed 's/-4242.17/2.5/' | lbprof_parse_row_v2 "t" "10.0.0.1:9101")
get2() { echo "$ROW2" | awk -F, -v i="$1" '{print $i}'; }
check "v2: 正常 queue_sum 保留 2.5" "$(get2 25)" "2.5"
check "v2: 正常行 queue_fixed=0" "$(get2 33)" "0"

# 旧版 gauge 命中率回退（无 counter 时）
ROW3=$(printf '%s\nvllm:prefix_cache_hit_rate{engine="0"} 0.65\nvllm:external_prefix_cache_hit_rate{engine="0"} 0.75' "$BODY" \
    | sed '/prefix_cache_hits_total/d;/prefix_cache_queries_total/d;/external_prefix_cache_hits_total/d;/external_prefix_cache_queries_total/d' \
    | lbprof_parse_row_v2 "t" "10.0.0.1:9102")
get3() { echo "$ROW3" | awk -F, -v i="$1" '{print $i}'; }
check "v2: gauge 回退本地命中率 0.65" "$(get3 14)" "0.65"
check "v2: gauge 回退外部命中率 0.75" "$(get3 16)" "0.75"

# sum 缺失但 count 存在：派生 avg 必须写 -1 而非 0（评审修复回归：sum/count 双检，
# 与 Prometheus 回采缺失口径统一，防止下游把同一缺口解释为零延迟）
ROW5=$(printf '%s' "$BODY" | sed '/time_to_first_token_seconds_sum/d' | lbprof_parse_row_v2 "t" "10.0.0.1:9104")
get5() { echo "$ROW5" | awk -F, -v i="$1" '{print $i}'; }
check "v2: ttft_sum 缺失时 ttft_avg 记 -1（非 0）" "$(get5 18)" "-1"
check "v2: ttft_sum 落盘为 -1" "$(get5 23)" "-1"
check "v2: ttft_count 正常保留" "$(get5 24)" "150"
ROW6=$(printf '%s' "$BODY" | sed '/request_queue_time_seconds_sum/d' | lbprof_parse_row_v2 "t" "10.0.0.1:9105")
get6() { echo "$ROW6" | awk -F, -v i="$1" '{print $i}'; }
check "v2: queue_sum 缺失时 queue_avg 记 -1（非 0）" "$(get6 19)" "-1"
check "v2: queue_sum 缺失不触发负 queue 修正" "$(get6 33)" "0"

# 畸形/边界取值：NaN/Inf/缺值/带时间戳的行不再被 awk 隐式转 0 或误取时间戳
EDGE='vllm:num_requests_running{engine="0"} NaN
vllm:num_requests_waiting{engine="0"} 3 1700000000000
vllm:num_requests_swapped{engine="0"}'
ROW4=$(printf '%s' "$EDGE" | lbprof_parse_row_v2 "t" "10.0.0.1:9103")
get4() { echo "$ROW4" | awk -F, -v i="$1" '{print $i}'; }
check "v2: NaN 值记为缺失 -1（不再隐式转 0）" "$(get4 3)" "-1"
check "v2: 带时间戳行取 value 而非时间戳" "$(get4 4)" "3"
check "v2: 无值畸形行记为缺失 -1" "$(get4 5)" "-1"

echo "== 1b. v2/single：urls 行白名单（file:// 等非 http(s) 行拒绝，防本地文件外泄） =="
printf 'http://127.0.0.1:59991/metrics\nfile:///etc/passwd\n' > "$TMP/bad_urls.txt"
"$BASH_BIN" "$SCRIPTS/extract_metric_v2.sh" 1 "$TMP/bad_urls.txt" > "$TMP/v2_bad.log" 2>&1
check "v2: file:// 行非零退出" "$?" "1"
check "v2: 提示 invalid URL" "$(grep -c 'invalid URL line' "$TMP/v2_bad.log")" "1"
"$BASH_BIN" "$SCRIPTS/extract_metric_single.sh" 1 "$TMP/bad_urls.txt" > "$TMP/single_bad.log" 2>&1
check "single: file:// 行非零退出" "$?" "1"

echo "== 2. pd.sh：P/D 数组分离 + 空池校验 =="
cd "$TMP" || exit 1
printf 'http://127.0.0.1:59991/metrics\nhttp://127.0.0.1:59992/metrics\n' > p_urls.txt
printf 'http://127.0.0.1:59993/metrics\nhttp://127.0.0.1:59994/metrics\nhttp://127.0.0.1:59995/metrics\n' > d_urls.txt
# 冒烟直接执行（chmod +x 后经 shebang 而非 bash <script>）：shebang 失效（如 BOM）时
# 本用例即刻暴露，不再被显式指定解释器的调用方式绕过
chmod +x "$SCRIPTS/extract_metric_pd.sh"
"$SCRIPTS/extract_metric_pd.sh" 2 p_urls.txt d_urls.txt > pd_run.log 2>&1
P_CSV=$(ls dp_load_metrics_P_*.csv 2>/dev/null | head -1)
D_CSV=$(ls dp_load_metrics_D_*.csv 2>/dev/null | head -1)
if [ -n "$P_CSV" ] && [ -n "$D_CSV" ]; then
    check "pd: P 池只含 P 标签" "$(awk -F, 'NR>1{print $2}' "$P_CSV" | sort -u | tr '\n' ' ')" "127.0.0.1:59991 127.0.0.1:59992 "
    check "pd: D 池只含 D 标签" "$(awk -F, 'NR>1{print $2}' "$D_CSV" | sort -u | tr '\n' ' ')" "127.0.0.1:59993 127.0.0.1:59994 127.0.0.1:59995 "
    check "pd: P 文件 33 列" "$(head -1 "$P_CSV" | awk -F, '{print NF}')" "33"
    check "pd: D 回退行 33 列" "$(sed -n 2p "$D_CSV" | awk -F, '{print NF}')" "33"
    check "pd: 表头含 queue_fixed" "$(head -1 "$P_CSV" | awk -F, '{print $33}')" "queue_fixed"
else
    fail "pd: 未产出 P/D CSV（采集脚本执行失败，见 pd_run.log）"
fi

# D 池空文件应非零退出
printf '' > d_empty.txt
"$BASH_BIN" "$SCRIPTS/extract_metric_pd.sh" 1 p_urls.txt d_empty.txt > pd_err.log 2>&1
check "pd: D 池为空时非零退出" "$?" "1"

echo "== 2b. pd.sh：urls 注入防护（eval 已移除 + URL 白名单） =="
# 注入 payload：双引号闭合 + 命令替换。旧 eval 实现会在采集机上执行 touch；
# 新实现应拒绝该行并退出，且不留下任何执行痕迹（pwned 文件不存在 = 未执行）。
printf 'http://127.0.0.1:59991/metrics\n' > p_ok.txt
printf 'http://127.0.0.1:59993/metrics\n' > d_inject.txt
printf 'http://evil";touch %s/pwned;"\n' "$TMP" >> d_inject.txt
"$BASH_BIN" "$SCRIPTS/extract_metric_pd.sh" 1 p_ok.txt d_inject.txt > inject.log 2>&1
check "pd-inject: 非法行非零退出" "$?" "1"
check "pd-inject: 提示 invalid URL" "$(grep -c 'invalid URL line' inject.log)" "1"
check "pd-inject: 注入 payload 未执行（无 pwned 文件）" "$(ls "$TMP/pwned" 2>/dev/null | wc -l)" "0"
# $(id) 形态同样拒绝
printf 'http://x";$(id);"\n' > d_inject2.txt
"$BASH_BIN" "$SCRIPTS/extract_metric_pd.sh" 1 p_ok.txt d_inject2.txt > inject2.log 2>&1
check "pd-inject: 命令替换形态同样拒绝" "$?" "1"

echo "== 2c. v2/single：节点不可达时回退行与表头列数一致（33/22，评审修复回归） =="
# 历史缺陷：公共库把「总列数」当「回退行 -1 个数」，回退行比表头多 2 列（35/24）。
# 以不可达端口端到端跑一轮，断言 CSV 每行（表头 + 回退行）NF 与表头列数一致。
printf 'http://127.0.0.1:59991/metrics\n' > "$TMP/dead_urls.txt"
mkdir -p "$TMP/v2_dead" "$TMP/single_dead"
(cd "$TMP/v2_dead" && "$BASH_BIN" "$SCRIPTS/extract_metric_v2.sh" 1 "$TMP/dead_urls.txt" > v2_dead.log 2>&1)
V2_DEAD_CSV=$(cd "$TMP/v2_dead" && ls dp_load_metrics_*.csv 2>/dev/null | head -1)
if [ -n "$V2_DEAD_CSV" ]; then
    check "v2: 不可达回退行与表头同为 33 列" "$(awk -F, '{print NF}' "$TMP/v2_dead/$V2_DEAD_CSV" | sort -u | tr '\n' ' ')" "33 "
else
    fail "v2: 不可达场景未产出 CSV（见 $TMP/v2_dead/v2_dead.log）"
fi
(cd "$TMP/single_dead" && "$BASH_BIN" "$SCRIPTS/extract_metric_single.sh" 1 "$TMP/dead_urls.txt" > single_dead.log 2>&1)
SINGLE_DEAD_CSV=$(cd "$TMP/single_dead" && ls dp_load_metrics_*.csv 2>/dev/null | head -1)
if [ -n "$SINGLE_DEAD_CSV" ]; then
    check "single: 不可达回退行与表头同为 22 列" "$(awk -F, '{print NF}' "$TMP/single_dead/$SINGLE_DEAD_CSV" | sort -u | tr '\n' ' ')" "22 "
else
    fail "single: 不可达场景未产出 CSV（见 $TMP/single_dead/single_dead.log）"
fi

echo "== 3. single.sh：TPOT 新指标名 + 负 queue 修正（avg 口径，公共库 lbprof_parse_row_legacy） =="
sed -n '/^lbprof_parse_row_legacy()/,/^}$/p' "$SCRIPTS/lbprof_common.sh" > "$TMP/parse_row_single.sh"
# shellcheck disable=SC1090
source "$TMP/parse_row_single.sh"
LBPROF_AWK_LIB="$(cat "$SCRIPTS/lbprof_common.awk")"
SROW=$(printf '%s' "$BODY" | lbprof_parse_row_legacy "t" "10.0.0.1:9100")
SNCOLS=$(echo "$SROW" | awk -F, '{print NF}')
check "single: 输出 22 列" "$SNCOLS" "22"
sget() { echo "$SROW" | awk -F, -v i="$1" '{print $i}'; }
check "single: tpot_avg 新指标名" "$(sget 21)" "0.0246667"
check "single: queue_avg 负值修正为 0" "$(sget 19)" "0"
check "single: prefill_avg 吸收负 queue" "$(sget 20)" "0.385533"

# 畸形/边界取值（与 v2 同口径：parse_row 值解析逻辑三脚本一致）
SROW2=$(printf '%s' "$EDGE" | lbprof_parse_row_legacy "t" "10.0.0.1:9103")
sget2() { echo "$SROW2" | awk -F, -v i="$1" '{print $i}'; }
check "single: NaN 值记为缺失 -1（不再隐式转 0）" "$(sget2 3)" "-1"
check "single: 带时间戳行取 value 而非时间戳" "$(sget2 4)" "3"
check "single: 无值畸形行记为缺失 -1" "$(sget2 5)" "-1"

# sum 缺失但 count 存在：single 与 v2 同口径（评审修复回归）
SROW3=$(printf '%s' "$BODY" | sed '/time_to_first_token_seconds_sum/d' | lbprof_parse_row_legacy "t" "10.0.0.1:9104")
sget3() { echo "$SROW3" | awk -F, -v i="$1" '{print $i}'; }
check "single: ttft_sum 缺失时 ttft_avg 记 -1（非 0）" "$(sget3 18)" "-1"

# 负 queue 修正顺序：先修 sum 再求平均（评审修复回归：queue -6/3 + prefill 20/2
# 契约口径 (20-6)/2=7，旧 avg 级相加实现 20/2+(-6/3)=8；字段缺失与有效负值按存在性区分）
QBODY='vllm:request_queue_time_seconds_sum{engine="0"} -6
vllm:request_queue_time_seconds_count{engine="0"} 3
vllm:prefill_time_seconds_sum{engine="0"} 20
vllm:prefill_time_seconds_count{engine="0"} 2'
QROW=$(printf '%s' "$QBODY" | lbprof_parse_row_legacy "t" "10.0.0.1:9106")
qget() { echo "$QROW" | awk -F, -v i="$1" '{print $i}'; }
check "single: 负 queue 按 sum 级修正后求平均 (20-6)/2=7（非 8）" "$(qget 20)" "7"
check "single: 修正后 queue_avg=0" "$(qget 19)" "0"

echo "== 4. 回采端单测（extract_metric_prom.py 纯函数） =="
run_py_suite "回采单测套件" "$TEST_DIR/test_extract_metric_prom.py"

echo ""
echo "== dplb-profiler 套件结果: PASS=$TEST_PASSED FAIL=$TEST_FAILED =="
[ "$TEST_FAILED" -eq 0 ]
