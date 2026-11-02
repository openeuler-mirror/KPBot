#!/bin/bash
# =============================================================================
# kptools-ascend-pd-env 测试套件
# 覆盖语法、清单、帮助、discover、假数据端到端、退出码与对象判定、输出顺序
# 可选: PD_ENV_E2E=1 时额外跑一次真容器采集
# =============================================================================

set +e

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TEST_DIR/../.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
WORK="$TEST_RESULTS_DIR/work"
TEST_PASSED=0
TEST_FAILED=0
TEST_SKIPPED=0

rm -rf "$WORK"
mkdir -p "$WORK"

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }

pass() { echo "  [PASS] $1"; TEST_PASSED=$((TEST_PASSED + 1)); }
fail() { echo "  [FAIL] $1"; TEST_FAILED=$((TEST_FAILED + 1)); }
skip() { echo "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED + 1)); }

assert_success() {
    if [ "$1" -eq 0 ]; then pass "$2"; else fail "$2 (exit code: $1)"; fi
}

assert_exit() { # $1=actual $2=expected $3=message
    if [ "$1" -eq "$2" ]; then pass "$3 (exit $2)"; else fail "$3 (期望 $2，实际 $1)"; fi
}

assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 — 未找到: $2"; fi
}

assert_file() {
    if [ -f "$1" ]; then pass "$2"; else fail "$2 — 文件不存在: $1"; fi
}

# 从 对比结果.json 里取第一组对比的一个字段
result_field() { # $1=对比目录 $2=字段名
    python3 -c "import json,sys; print(json.load(open(sys.argv[1],encoding='utf-8'))['comparisons'][0][sys.argv[2]])" \
        "$1/对比/对比结果.json" "$2" 2>/dev/null
}

# 第一组对比里状态为「仅X有」的差异条数
result_only_one_side() { # $1=对比目录
    python3 -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(sum(1 for r in d['comparisons'][0]['differences'] if r['status'].startswith('仅')))" \
        "$1/对比/对比结果.json" 2>/dev/null
}

assert_equal() { # $1=实际 $2=期望 $3=说明
    if [ "$1" = "$2" ]; then pass "$3（$2）"; else fail "$3 — 期望 $2，实际 $1"; fi
}

# 某个分类里状态不等于指定值的项数，用来断言整类的状态
result_category_other_than() { # $1=对比目录 $2=分类 $3=期望状态
    python3 -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(sum(1 for i in d['items'] if i['category']==sys.argv[2] and i['status']!=sys.argv[3]))" \
        "$1/对比/对比结果.json" "$2" "$3" 2>/dev/null
}

# 文件或目录的八进制权限，macOS 和 Linux 的 stat 参数不同，统一用 python 读
mode_of() { python3 -c "import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])" "$1" 2>/dev/null; }

group() { echo -e "\n\033[34m===== 测试组: $1 =====\033[0m"; }

# =============================================================================
group "1. 语法与清单"
# =============================================================================

for script in discover.sh collect.sh; do
    bash -n "$SCRIPTS/$script" 2>/dev/null
    assert_success $? "SYNTAX: $script 语法正确"
done

for script in pd_env.py compare.py pdenv/probe.py pdenv/collect.py pdenv/bundle.py \
              pdenv/fields.py pdenv/diff.py pdenv/report.py; do
    python3 -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" \
        "$SCRIPTS/$script" 2>/dev/null
    assert_success $? "SYNTAX: $script 语法正确"
done

python3 -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" \
    "$TEST_DIR/make_fixture.py" 2>/dev/null
assert_success $? "SYNTAX: make_fixture.py 语法正确"

for f in evals/evals.json evals/eval_queries.json; do
    python3 -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" \
        "$SKILL_DIR/$f" 2>/dev/null
    assert_success $? "SYNTAX: $f 合法 JSON"
done

assert_file "$SKILL_DIR/SKILL.md" "清单: SKILL.md 存在"
assert_file "$SKILL_DIR/references/pd-env-guide.md" "清单: references 指南存在"

python3 - "$SKILL_DIR/SKILL.md" <<'PY' 2>/dev/null
import sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')
assert text.startswith('---\n'), 'no frontmatter'
fm = text.split('---\n')[1]
for key in ('name:', 'description:', 'license:', 'compatibility:', 'metadata:'):
    assert key in fm, key
assert 'name: kptools-ascend-pd-env' in fm
PY
assert_success $? "清单: SKILL.md frontmatter 字段齐全且 name 与目录名一致"

# =============================================================================
group "2. 帮助与参数校验"
# =============================================================================

OUT=$(bash "$SCRIPTS/collect.sh" --help 2>&1); CODE=$?
assert_exit $CODE 0 "PE-001: collect.sh --help"
assert_contains "$OUT" "可以混着写" "PE-001: 帮助说明了容器名和采集包可以混写"
assert_contains "$OUT" "退出码" "PE-001: 帮助列出了退出码"

OUT=$(bash "$SCRIPTS/collect.sh" 2>&1); CODE=$?
assert_exit $CODE 1 "PE-002: 不给对象时报错"
assert_contains "$OUT" "至少要给一个对象" "PE-002: 提示要给对象"

OUT=$(bash "$SCRIPTS/collect.sh" /definitely/not/here.tar.gz --out "$WORK/x1" 2>&1); CODE=$?
assert_exit $CODE 1 "PE-003: 采集包路径不存在时报错而不是当容器名去采"
assert_contains "$OUT" "不能当采集包用" "PE-003: 说明了路径不能当采集包"

OUT=$(bash "$SCRIPTS/discover.sh" --help 2>&1); CODE=$?
assert_exit $CODE 0 "PE-004: discover.sh --help"
assert_contains "$OUT" "READY" "PE-004: 帮助列出了状态码"

OUT=$(bash "$SCRIPTS/discover.sh" --nonsense 2>&1); CODE=$?
assert_exit $CODE 3 "PE-005: discover.sh 未知参数退出 3"

# =============================================================================
group "3. discover"
# =============================================================================

OUT=$(bash "$SCRIPTS/discover.sh" 2>/dev/null); CODE=$?
STATUS=$(echo "$OUT" | head -1)
case "$STATUS" in
    READY)              assert_exit $CODE 0 "PD-001: discover 报 READY" ;;
    PYTHON_MISSING|DOCKER_MISSING|DOCKER_UNAVAILABLE)
                        assert_exit $CODE 2 "PD-001: discover 报 $STATUS 并退出 2" ;;
    *)                  fail "PD-001: discover 输出了未知状态 '$STATUS'" ;;
esac

# =============================================================================
group "4. 假数据端到端"
# =============================================================================

FX="$WORK/fixtures"
python3 "$TEST_DIR/make_fixture.py" "$FX/single" c-only >/dev/null 2>&1
assert_success $? "PF-000: 造出单容器采集包"
python3 "$TEST_DIR/make_fixture.py" "$FX/pair" vllm-p,vllm-d >/dev/null 2>&1
assert_success $? "PF-000: 造出双容器采集包"
python3 "$TEST_DIR/make_fixture.py" "$FX/other" vllm-d2-alt >/dev/null 2>&1
assert_success $? "PF-000: 造出带差异的采集包"
python3 "$TEST_DIR/make_fixture.py" "$FX/nopkg" ok-side,blind-side-nopkg >/dev/null 2>&1
assert_success $? "PF-000: 造出一侧包列表未采到的采集包"
python3 "$TEST_DIR/make_fixture.py" "$FX/failed" live-side,dead-side-failed >/dev/null 2>&1
assert_success $? "PF-000: 造出一侧采集失败的采集包"
python3 "$TEST_DIR/make_fixture.py" "$FX/noproc" full-side,noproc-side-noproc >/dev/null 2>&1
assert_success $? "PF-000: 造出一侧进程采集失败的采集包"
python3 "$TEST_DIR/make_fixture.py" "$FX/skipproc" full-side2,skip-side-skipproc >/dev/null 2>&1
assert_success $? "PF-000: 造出一侧进程采集被跳过的采集包"
python3 "$TEST_DIR/make_fixture.py" "$FX/noprofile" base-side,prof-side-noprofile >/dev/null 2>&1
assert_success $? "PF-000: 造出一侧 Python 画像采集失败的采集包"
mkdir -p "$FX/broken" && echo "这不是采集包" > "$FX/broken/readme.txt"
( cd "$FX" && tar czf single.tar.gz single ) 2>/dev/null

echo "  PF-001: 两个采集包 → 自动对比"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/single" "$FX/other" --out "$WORK/o1" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-001: 两个包对比成功"
assert_contains "$OUT" "环境 2 个" "PF-001: 报告里是 2 个环境"
for f in 环境对比报告.html 环境简表.csv 完整总表.csv 对比摘要.md 对比结果.json; do
    assert_file "$WORK/o1/对比/$f" "PF-001: 产物 $f"
done
if [ ! -e "$WORK/o1/采集" ] && [ ! -e "$WORK/o1/采集.tar.gz" ]; then
    pass "PF-001: 只给采集包时不生成 采集/ 和 采集.tar.gz"
else
    fail "PF-001: 没采本机容器却生成了 采集/ 或 采集.tar.gz"
fi
assert_equal "$(result_field "$WORK/o1" diff_count)" 5 "PF-001: 认出预置的差异项数"
assert_equal "$(result_field "$WORK/o1" perf_diff_count)" 4 "PF-001: 认出性能相关差异项数"
assert_equal "$(result_only_one_side "$WORK/o1")" 1 "PF-001: 单边存在的项判成「仅X有」"

echo "  PF-002: 单个单容器包 → 不够两个环境"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/single" --out "$WORK/o2" 2>&1); CODE=$?
assert_exit $CODE 3 "PF-002: 只有 1 个环境时退出 3"
assert_contains "$OUT" "没法对比" "PF-002: 说明了为什么没对比"

echo "  PF-003: 单个双容器包 → 包内两个容器互比"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/pair" --out "$WORK/o3" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-003: 一个包里两个容器也能比"
assert_contains "$OUT" "环境 2 个" "PF-003: 识别出 2 个环境"
assert_equal "$(result_field "$WORK/o3" diff_count)" 5 "PF-003: 认出预置的差异项数"
assert_equal "$(result_field "$WORK/o3" perf_diff_count)" 4 "PF-003: 认出性能相关差异项数"
assert_equal "$(result_only_one_side "$WORK/o3")" 1 "PF-003: 单边存在的项判成「仅X有」"

echo "  PF-004: .tar.gz 形式的采集包"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/single.tar.gz" "$FX/other" --out "$WORK/o4" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-004: .tar.gz 能直接当采集包用"

echo "  PF-005: 一个包读不进来 → 退出 1，报告仍生成"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/broken" "$FX/pair" --out "$WORK/o5" 2>&1); CODE=$?
assert_exit $CODE 1 "PF-005: 有包没读进来时退出非零"
assert_contains "$OUT" "没有读进来" "PF-005: 明确说了哪个包没读进来"
assert_file "$WORK/o5/对比/环境对比报告.html" "PF-005: 其余环境的报告照常生成"

echo "  PF-006: 全部包都读不进来"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/broken" --out "$WORK/o6" 2>&1); CODE=$?
assert_exit $CODE 3 "PF-006: 一个都读不出来时退出 3"

echo "  PF-007: 输出目录非空"
mkdir -p "$WORK/o7" && touch "$WORK/o7/占位"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/single" "$FX/other" --out "$WORK/o7" 2>&1); CODE=$?
assert_exit $CODE 1 "PF-007: 输出目录非空时拒绝覆盖"

echo "  PF-008: 失败提示排在结论前面（2>&1 合并后）"
bash "$SCRIPTS/collect.sh" "$FX/broken" "$FX/pair" --out "$WORK/o8" > "$WORK/o8.log" 2>&1
FIRST_FAIL=$(grep -n "没有读进来" "$WORK/o8.log" | head -1 | cut -d: -f1)
FIRST_OK=$(grep -n "^环境 " "$WORK/o8.log" | head -1 | cut -d: -f1)
if [ -n "$FIRST_FAIL" ] && [ -n "$FIRST_OK" ] && [ "$FIRST_FAIL" -lt "$FIRST_OK" ]; then
    pass "PF-008: 失败提示在成功汇总之前"
else
    fail "PF-008: 顺序不对（失败行 $FIRST_FAIL，汇总行 $FIRST_OK）"
fi

echo "  PF-009: 对比结果.json 结构可被 agent 消费"
python3 - "$WORK/o1/对比/对比结果.json" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
for key in ('schema', 'generated_at', 'environments', 'items', 'comparisons'):
    assert key in d, key
c = d['comparisons'][0]
for key in ('left', 'right', 'diff_count', 'perf_diff_count', 'differences'):
    assert key in c, key
PY
assert_success $? "PF-009: 对比结果.json 字段齐全"

echo "  PF-010: 1 个好包（单容器）+ 1 个坏包 → 退出 1"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/broken" "$FX/single" --out "$WORK/o10" 2>&1); CODE=$?
assert_exit $CODE 1 "PF-010: 剩下一个环境时也按「有包没读进来」返回 1"
assert_contains "$OUT" "没有读进来" "PF-010: 说了哪个包没读进来"

echo "  PF-011: 一侧的包列表没采到时，结论里说明包差异不可信"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/nopkg" --out "$WORK/o11" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-011: 一侧包列表缺失仍能出报告"
assert_contains "$OUT" "包差异不可信" "PF-011: 结论行提醒包差异不可信"
assert_contains "$(result_field "$WORK/o11" note)" "包差异不可信" \
    "PF-011: 对比结果.json 的 note 里也有这句"
assert_equal "$(result_category_other_than "$WORK/o11" 核心包 无法比较)" 0 \
    "PF-011: 核心包整类判成「无法比较」而不是「仅X有」"
assert_equal "$(result_only_one_side "$WORK/o11")" 1 \
    "PF-011: 只有真正单边存在的那一项算「仅X有」"
assert_equal "$(result_category_other_than "$WORK/o11" 其他包 无法比较)" 0 \
    "PF-011: 非核心包同样判成无法比较"
assert_equal "$(result_field "$WORK/o11" diff_count)" 4 "PF-011: 包相关的项不计入差异"

echo "  PF-012: 只给采集包时输出目录和报告文件的权限"
bash "$SCRIPTS/collect.sh" "$FX/single" "$FX/other" --out "$WORK/o12" >/dev/null 2>&1
assert_equal "$(mode_of "$WORK/o12")" 700 "PF-012: 输出目录权限"
assert_equal "$(mode_of "$WORK/o12/对比")" 700 "PF-012: 对比目录权限"
for f in 环境对比报告.html 对比结果.json; do
    assert_equal "$(mode_of "$WORK/o12/对比/$f")" 600 "PF-012: $f 权限"
done

echo "  PF-013: 一侧采集失败时整组判成「无法比较」"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/failed" --out "$WORK/o13" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-013: 一侧成功一侧失败时仍生成报告"
assert_contains "$OUT" "无法比较" "PF-013: 结论里标明无法比较"
assert_contains "$OUT" "该组合无法比较" "PF-013: 点明无法比较的是哪一组"
assert_equal "$(result_field "$WORK/o13" diff_count)" 0 "PF-013: 差异数为 0"
assert_equal "$(result_field "$WORK/o13" perf_diff_count)" 0 "PF-013: 性能相关差异数也是 0"
assert_equal "$(result_only_one_side "$WORK/o13")" 0 "PF-013: 没有任何「仅X有」条目"
assert_equal "$(result_field "$WORK/o13" not_comparable_count)" \
    "$(result_field "$WORK/o13" total_items)" "PF-013: 全部项目都记成无法比较"
assert_contains "$(cat "$WORK/o13/对比/对比摘要.md")" "该组合无法比较" \
    "PF-013: 对比摘要.md 里也写明该组合无法比较"

echo "  PF-014: processes 采集失败时，启动参数与进程环境变量整类无法比较"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/noproc" --out "$WORK/o14" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-014: 单项采集失败时仍生成报告"
assert_equal "$(result_category_other_than "$WORK/o14" 启动参数 无法比较)" 0 \
    "PF-014: 启动参数整类判成无法比较"
assert_equal "$(result_category_other_than "$WORK/o14" 进程环境变量 无法比较)" 0 \
    "PF-014: 进程环境变量整类判成无法比较"
assert_equal "$(result_only_one_side "$WORK/o14")" 0 "PF-014: 这两类里没有「仅X有」"
assert_equal "$(result_field "$WORK/o14" diff_count)" 6 "PF-014: 差异项数只含可比较的项"
assert_equal "$(result_field "$WORK/o14" perf_diff_count)" 3 "PF-014: 性能相关差异数"
assert_equal "$(result_field "$WORK/o14" not_comparable_count)" 6 "PF-014: 无法比较项数"

echo "  PF-015: 被跳过的采集项不算失败，对应项仍按单边存在处理"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/skipproc" --out "$WORK/o15" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-015: 有跳过项时仍生成报告"
assert_equal "$(result_field "$WORK/o15" not_comparable_count)" 0 \
    "PF-015: 跳过不会变成无法比较"
assert_equal "$(result_only_one_side "$WORK/o15")" 5 "PF-015: 跳过的那一侧按「仅X有」计入差异"

echo "  PF-016: Python 画像采集失败时，进程环境变量整类无法比较"
OUT=$(bash "$SCRIPTS/collect.sh" "$FX/noprofile" --out "$WORK/o16" 2>&1); CODE=$?
assert_exit $CODE 0 "PF-016: 画像采集失败时仍生成报告"
assert_equal "$(result_category_other_than "$WORK/o16" 进程环境变量 无法比较)" 0 \
    "PF-016: 进程环境变量整类判成无法比较"
assert_equal "$(result_field "$WORK/o16" diff_count)" 14 "PF-016: 差异项数只含可比较的项"
assert_equal "$(result_field "$WORK/o16" perf_diff_count)" 2 "PF-016: 性能相关差异数"
assert_equal "$(result_field "$WORK/o16" not_comparable_count)" 28 "PF-016: 无法比较项数"

# =============================================================================
group "5. 退出码与对象判定"
# =============================================================================

BAD_DOCKER="unix:///nonexistent/kptools-pd-env-test.sock"

OUT=$(bash "$SCRIPTS/collect.sh" somectr --bogus 2>&1); CODE=$?
assert_exit $CODE 1 "PA-001: 未知参数按参数错误返回 1，不是 2"

OUT=$(bash "$SCRIPTS/collect.sh" somectr --role X 2>&1); CODE=$?
assert_exit $CODE 1 "PA-002: --role 取值非法返回 1"

OUT=$(DOCKER_HOST="$BAD_DOCKER" bash "$SCRIPTS/collect.sh" somectr --out "$WORK/nd" 2>&1); CODE=$?
assert_exit $CODE 2 "PA-003: 要采容器却连不上 docker 返回 2"
assert_contains "$OUT" "连不上 docker" "PA-003: 说清了是 docker 的问题"
if [ ! -e "$WORK/nd" ]; then
    pass "PA-003: 连不上 docker 时不写出空报告"
else
    fail "PA-003: 连不上 docker 却建了输出目录 $WORK/nd"
fi

OUT=$(DOCKER_HOST="$BAD_DOCKER" bash "$SCRIPTS/collect.sh" "$FX/single" "$FX/other" \
      --out "$WORK/nd2" 2>&1); CODE=$?
assert_exit $CODE 0 "PA-004: 只给采集包时不检查 docker"

NAMEDIR="$WORK/names"
mkdir -p "$NAMEDIR"
( cd "$NAMEDIR" && mkdir -p sameas_dir && echo hi > sameas_file )
OUT=$(cd "$NAMEDIR" && bash "$SCRIPTS/collect.sh" sameas_dir "$FX/single" --out ./od 2>&1); CODE=$?
assert_contains "$OUT" "已按采集包处理" "PA-005: 同名目录提示按采集包处理"
OUT=$(cd "$NAMEDIR" && DOCKER_HOST="$BAD_DOCKER" bash "$SCRIPTS/collect.sh" sameas_file \
      --out ./of 2>&1); CODE=$?
assert_contains "$OUT" "已按容器名处理" "PA-006: 同名普通文件按容器名处理"
assert_exit $CODE 2 "PA-006: 按容器名处理后因 docker 不可用返回 2"

OUT=$(bash "$SCRIPTS/collect.sh" "$NAMEDIR/sameas_file" --out "$WORK/ob" 2>&1); CODE=$?
assert_exit $CODE 1 "PA-007: 带路径的普通文件不能当采集包，返回 1"
assert_contains "$OUT" "既不是目录也不是" "PA-007: 说清了为什么不能当采集包"

if grep -q "不记值\|--item\|名字像凭据" "$SCRIPTS/pdenv/collect.py"; then
    fail "PA-008: SUMMARY.txt 仍含「不记值 / --item」字样，与不做遮盖的说明矛盾"
else
    pass "PA-008: SUMMARY.txt 不含「不记值 / --item」字样"
fi
if grep -q "一律原样记录，不做遮盖" "$SCRIPTS/pdenv/collect.py"; then
    pass "PA-008: SUMMARY.txt 写明取值一律原样记录"
else
    fail "PA-008: SUMMARY.txt 没有写明取值一律原样记录"
fi

# =============================================================================
group "6. 真容器端到端（可选）"
# =============================================================================

if [ "${PD_ENV_E2E:-0}" != "1" ]; then
    skip "PE2E: 未设置 PD_ENV_E2E=1，跳过真容器采集"
elif ! command -v docker >/dev/null 2>&1 || ! docker version >/dev/null 2>&1; then
    skip "PE2E: docker 不可用"
else
    IMAGE="${PD_ENV_E2E_IMAGE:-python:3.12-slim}"
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        skip "PE2E: 本地没有镜像 $IMAGE（不自动拉取）"
    else
        CNAME="kptools_pdenv_test_$$_$RANDOM"
        docker run -d --name "$CNAME" --label kptools_pdenv_test=1 "$IMAGE" \
            python3 -c "import time; time.sleep(600)" >/dev/null 2>&1
        if [ $? -ne 0 ]; then
            fail "PE2E: 测试容器起不来"
        else
            OUT=$(bash "$SCRIPTS/collect.sh" "$CNAME" --out "$WORK/e2e" 2>&1); CODE=$?
            assert_exit $CODE 0 "PE2E: 单容器采集成功（只有 1 个环境，不做对比）"
            assert_file "$WORK/e2e/采集/环境报告.html" "PE2E: 单环境报告生成"
            assert_file "$WORK/e2e/采集.tar.gz" "PE2E: 采集包打包"
            assert_file "$WORK/e2e/采集/$CNAME/inventory.json" "PE2E: 结构化结果写出"
            OUT=$(bash "$SCRIPTS/collect.sh" "$CNAME" "$WORK/e2e/采集.tar.gz" \
                  --out "$WORK/e2e2" 2>&1); CODE=$?
            assert_exit $CODE 0 "PE2E: 现采 + 历史包混合对比"
            assert_contains "$OUT" "环境 2 个" "PE2E: 混合模式识别出 2 个环境"
        fi
        docker rm -f $(docker ps -aq --filter "label=kptools_pdenv_test=1") >/dev/null 2>&1
    fi
fi

# =============================================================================
echo ""
echo "========================================"
echo "通过 $TEST_PASSED  失败 $TEST_FAILED  跳过 $TEST_SKIPPED"
echo "========================================"
if [ "$TEST_FAILED" -gt 0 ]; then
    red "有用例失败，中间产物留在 $WORK 供排查"
    exit 1
fi
# 全过就把中间产物删掉：test_results/ 会被 install.sh 一起拷走，不能留垃圾
rm -rf "$WORK"
green "全部通过"
exit 0
