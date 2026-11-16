#!/bin/bash
# =============================================================================
# kptools-arthas 测试套件
# 覆盖语法验证、discover、collect 参数校验、错误处理、SKILL.md/evals 格式；
# java + arthas-boot.jar 齐备时追加端到端冒烟（AR 组，见组 5）
# =============================================================================
set +e

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TEST_DIR/../.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
TEST_PASSED=0
TEST_FAILED=0
TEST_SKIPPED=0

mkdir -p "$TEST_RESULTS_DIR"

# 运行产物不入库：退出时清理测试产物，保持工作区干净（不依赖 .gitignore）
cleanup() {
    [ -d "${TEST_RESULTS_DIR:-}" ] && rm -rf "${TEST_RESULTS_DIR:?}"/*
}
trap cleanup EXIT

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }

assert_equal() {
    local expected="$1" actual="$2" message="${3:-AssertEqual}"
    if [ "$expected" = "$actual" ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         期望: $expected"; echo "         实际: $actual"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}
assert_success() {
    local exit_code="$1" message="${2:-Command should succeed}"
    if [ "$exit_code" -eq 0 ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (exit code: $exit_code)"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}
assert_failure() {
    local exit_code="$1" message="${2:-Command should fail}"
    if [ "$exit_code" -ne 0 ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}
assert_contains() {
    local haystack="$1" needle="$2" message="${3:-AssertContains}"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         未找到: $needle"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}
skip() { echo "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED + 1)); }
test_group_start() { echo -e "\n\033[34m===== 测试组: $1 =====\033[0m"; }

# =============================================================================
# 1. 脚本语法验证
# =============================================================================
test_group_start "1. 语法验证"

for script in discover.sh deploy.sh collect.sh; do
    [ -f "$SCRIPTS/$script" ]; assert_success $? "$script 存在"
    if [ -f "$SCRIPTS/$script" ]; then
        bash -n "$SCRIPTS/$script" 2>"$TEST_RESULTS_DIR/syntax_$script.err"
        assert_success $? "$script 语法检查"
    fi
done

# =============================================================================
# 2. discover（AT-001）
# =============================================================================
test_group_start "2. discover"

echo "  AT-001: discover.sh"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_success $rc "AT-001: discover 退出码 0"
assert_contains "$output" "java:" "AT-001: 输出含 java 状态"
assert_contains "$output" "arthas-boot.jar" "AT-001: 输出含 arthas 状态"

echo "  AT-001b: discover.sh --pid（本机自检）"
SELF_PID=$$
output=$(bash "$SCRIPTS/discover.sh" --pid "$SELF_PID" 2>&1); rc=$?
assert_success $rc "AT-001b: --pid 报告式退出 0"
assert_contains "$output" "非 Java 进程" "AT-001b: shell 进程路由提示正确"

# =============================================================================
# 3. deploy --help（AT-002）
# =============================================================================
test_group_start "3. deploy"

echo "  AT-002: deploy.sh --help"
output=$(bash "$SCRIPTS/deploy.sh" --help 2>&1); rc=$?
assert_success $rc "AT-002: --help 退出码 0"
assert_contains "$output" "--prewarm" "AT-002: 帮助含 --prewarm（离线预热说明）"

if command -v java &>/dev/null && [ -f "$HOME/.arthas/arthas-boot.jar" ]; then
    output=$(bash "$SCRIPTS/deploy.sh" 2>&1); rc=$?
    assert_success $rc "AT-002: 已安装时 deploy 幂等退出 0"
    assert_contains "$output" "already installed" "AT-002: 报告已安装"
else
    skip "AT-002: 本机无 java/arthas，安装路径需网络，跳过幂等检查"
fi

echo "  AT-002b/c/d: 下载完整性校验（sha256 fail-closed）"
FAKEHOME="$TEST_RESULTS_DIR/fakehome"; mkdir -p "$FAKEHOME"
if [ -f /opt/arthas/arthas-boot.jar ]; then
    skip "AT-002b/c/d: 环境存在 /opt/arthas 系统级安装，跳过下载路径用例"
elif ! command -v curl >/dev/null 2>&1; then
    skip "AT-002b/c/d: 无 curl，跳过下载校验用例"
else
    output=$(HOME="$FAKEHOME" ARTHAS_BOOT_JAR="" ARTHAS_BOOT_URL="file:///nonexistent.jar" bash "$SCRIPTS/deploy.sh" 2>&1); rc=$?
    assert_equal 3 $rc "AT-002b: 覆盖 ARTHAS_BOOT_URL 未配对 SHA256 → 3"
    assert_contains "$output" "必须同时提供 ARTHAS_BOOT_SHA256" "AT-002b: fail-closed 提示配对摘要"

    printf 'fake-jar-bytes' > "$TEST_RESULTS_DIR/fake-boot.jar"
    WRONG_SHA="$(printf 'other-content' | sha256sum | awk '{print $1}')"
    output=$(HOME="$FAKEHOME" ARTHAS_BOOT_JAR="" ARTHAS_BOOT_URL="file://$TEST_RESULTS_DIR/fake-boot.jar" ARTHAS_BOOT_SHA256="$WRONG_SHA" bash "$SCRIPTS/deploy.sh" 2>&1); rc=$?
    assert_equal 1 $rc "AT-002c: 摘要不匹配 → 1 且产物删除"
    [ ! -e "$FAKEHOME/.arthas/arthas-boot.jar" ]
    assert_success $? "AT-002c: 校验失败产物已清理"

    RIGHT_SHA="$(sha256sum "$TEST_RESULTS_DIR/fake-boot.jar" | awk '{print $1}')"
    output=$(HOME="$FAKEHOME" ARTHAS_BOOT_JAR="" ARTHAS_BOOT_URL="file://$TEST_RESULTS_DIR/fake-boot.jar" ARTHAS_BOOT_SHA256="$RIGHT_SHA" bash "$SCRIPTS/deploy.sh" 2>&1); rc=$?
    assert_equal 0 $rc "AT-002d: 配对摘要匹配 → 0"
    assert_contains "$output" "sha256 verified" "AT-002d: 报告已校验安装"
fi

# =============================================================================
# 4. collect 参数校验（AT-003 系列）
# =============================================================================
test_group_start "4. collect 参数校验"

echo "  AT-003: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "AT-003: --help 退出码 0"
for token in "-p" "-c" "--preset" "-o" "--jar" "--timeout" "--shutdown" "cpu" "blocked" "overview"; do
    assert_contains "$output" "$token" "AT-003: 帮助含 $token"
done

echo "  AT-004: 参数错误矩阵"
output=$(bash "$SCRIPTS/collect.sh" 2>&1); rc=$?
assert_failure $rc "AT-004: 缺 -p 退出码非 0"
assert_contains "$output" "-p" "AT-004: 缺 -p 提示参数"

bash "$SCRIPTS/collect.sh" -p abc --preset cpu >/dev/null 2>&1; rc=$?
assert_equal 3 $rc "AT-004: 非数字 PID → 3"

bash "$SCRIPTS/collect.sh" -p 1  >/dev/null 2>&1; rc=$?
assert_equal 3 $rc "AT-004: 缺命令串与预设 → 3"

bash "$SCRIPTS/collect.sh" -p 1 -c "version" --preset cpu >/dev/null 2>&1; rc=$?
assert_equal 3 $rc "AT-004: -c 与 --preset 同用 → 3"

bash "$SCRIPTS/collect.sh" -p 1 --preset bogus >/dev/null 2>&1; rc=$?
assert_equal 3 $rc "AT-004: 未知预设 → 3"

bash "$SCRIPTS/collect.sh" -p 1 --bogus >/dev/null 2>&1; rc=$?
assert_equal 3 $rc "AT-004: 未知选项 → 3"

# 伪装 java 进程（cmdline 含 java，通过路由检查）后验证 --jar 显式路径校验
bash -c 'exec -a java sleep 30' & FAKE_JAVA_PID=$!
sleep 0.5
bash "$SCRIPTS/collect.sh" -p "$FAKE_JAVA_PID" --jar "/nonexistent/arthas-boot.jar" -c "version" >/dev/null 2>&1; rc=$?
assert_equal 2 $rc "AT-004: --jar 不存在（显式指定不回退） → 2"
kill "$FAKE_JAVA_PID" 2>/dev/null; wait "$FAKE_JAVA_PID" 2>/dev/null

echo "  AT-005: 非 Java 进程语言路由"
sleep 30 & SLEEP_PID=$!
output=$(bash "$SCRIPTS/collect.sh" -p "$SLEEP_PID" -c "version" 2>&1); rc=$?
assert_equal 1 $rc "AT-005: 非 Java 进程 → 1（路由检查先于依赖检查）"
assert_contains "$output" "不是 Java 进程" "AT-005: 提示语言路由"
kill "$SLEEP_PID" 2>/dev/null; wait "$SLEEP_PID" 2>/dev/null

echo "  AT-006: 不存在的 PID"
bash "$SCRIPTS/collect.sh" -p 999999999 -c "version" >/dev/null 2>&1; rc=$?
assert_equal 1 $rc "AT-006: 目标进程不存在 → 1"

echo "  AT-008: 3658 被非目标进程占用时端口回退不静默终止（errexit 回归）"
if ! command -v java >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    skip "AT-008: 需 java + python3（端口回退回归用例）"
else
    # 临时监听占住 3658（模拟其他进程会话驻留；占用者既非目标进程也非本脚本）
    (timeout 10 python3 -c 'import socket,time; s=socket.socket(); s.bind(("127.0.0.1",3658)); s.listen(1); time.sleep(10)' >/dev/null 2>&1 &)
    sleep 1
    bash -c 'exec -a java sleep 20' & FAKE_JAVA_3658=$!
    sleep 0.5
    printf 'fake' > "$TEST_RESULTS_DIR/fake-boot2.jar"
    bash "$SCRIPTS/collect.sh" -p "$FAKE_JAVA_3658" --jar "$TEST_RESULTS_DIR/fake-boot2.jar" \
        -c "version" -o "$TEST_RESULTS_DIR/at008_evidence" \
        >"$TEST_RESULTS_DIR/at008.out" 2>"$TEST_RESULTS_DIR/at008.stderr"; rc=$?
    assert_equal 1 $rc "AT-008: 假 jar 全命令失败 → exit 1（而非管道赋值静默终止）"
    grep -q "端口 3658 被" "$TEST_RESULTS_DIR/at008.stderr"
    assert_success $? "AT-008: stderr 含端口占用回退 NOTICE"
    [ -n "$(ls "$TEST_RESULTS_DIR/at008_evidence" 2>/dev/null)" ]
    assert_success $? "AT-008: 证据目录已创建（回退分支确实执行）"
    kill "$FAKE_JAVA_3658" 2>/dev/null; wait "$FAKE_JAVA_3658" 2>/dev/null
fi

echo "  AT-009: 命令含未加引号通配符时不被路径名展开（glob 回归）"
if ! command -v java >/dev/null 2>&1; then
    skip "AT-009: 需 java（glob 回归用例）"
else
    GLOB_DIR="$TEST_RESULTS_DIR/globdir"; mkdir -p "$GLOB_DIR"
    touch "$GLOB_DIR/FooController.java" "$GLOB_DIR/XxxControllerTest.java"
    printf 'fake' > "$GLOB_DIR/fake.jar"
    bash -c 'exec -a java sleep 20' & FAKE_JAVA_GLOB=$!
    sleep 0.5
    ( cd "$GLOB_DIR" && bash "$SCRIPTS/collect.sh" -p "$FAKE_JAVA_GLOB" --jar "$GLOB_DIR/fake.jar" \
        -c "dashboard -n 1;*Controller*;sc -d *Controller*" -o "$GLOB_DIR/ev" \
        >"$GLOB_DIR/out" 2>"$GLOB_DIR/stderr" ); rc=$?
    EVID_GLOB="$(ls "$GLOB_DIR"/ev/*.txt 2>/dev/null | head -1)"
    assert_equal 1 $rc "AT-009: 假 jar 全命令失败 → exit 1"
    grep -Fq "=== CMD: *Controller* ===" "$EVID_GLOB"
    assert_success $? "AT-009: 无空格通配 token 保持字面（不被展开）"
    grep -Fq "=== CMD: sc -d *Controller* ===" "$EVID_GLOB"
    assert_success $? "AT-009: 含空格命令中的通配符保持字面"
    if grep -Fq "FooController.java" "$EVID_GLOB"; then
        assert_failure 1 "AT-009: 通配符未被当前目录文件名展开"
    else
        assert_success 0 "AT-009: 通配符未被当前目录文件名展开"
    fi
    kill "$FAKE_JAVA_GLOB" 2>/dev/null; wait "$FAKE_JAVA_GLOB" 2>/dev/null
fi

# =============================================================================
# 5. 端到端冒烟（AR 组：需 java + javac + arthas-boot.jar + 可联网首次 attach）
# =============================================================================
test_group_start "5. 端到端冒烟"

CAN_SMOKE=1
command -v java >/dev/null 2>&1      || CAN_SMOKE=0
command -v javac >/dev/null 2>&1     || CAN_SMOKE=0
[ -f "$HOME/.arthas/arthas-boot.jar" ] || [ -n "${ARTHAS_BOOT_JAR:-}" ] || CAN_SMOKE=0
if [ "$CAN_SMOKE" = "1" ]; then
    echo "  AR-001: 批处理采集 + 预设 cpu"
    printf 'public class ArthasSleep { public static void main(String[] a) throws Exception { Thread.sleep(120000L); } }\n' \
        > "$TEST_RESULTS_DIR/ArthasSleep.java"
    javac -d "$TEST_RESULTS_DIR" "$TEST_RESULTS_DIR/ArthasSleep.java"
    # 后台 JVM 输出重定向 /dev/null，避免持有调用方管道导致挂起
    (java -cp "$TEST_RESULTS_DIR" ArthasSleep >/dev/null 2>&1 &)
    sleep 2
    JPID=$(jps -l 2>/dev/null | awk '/ArthasSleep/{print $1; exit}')
    if [ -n "$JPID" ]; then
        OUT_EVID="$TEST_RESULTS_DIR/ar001_evidence"
        output=$(bash "$SCRIPTS/collect.sh" -p "$JPID" --preset cpu -o "$OUT_EVID" --shutdown 2>"$TEST_RESULTS_DIR/ar001.stderr"); rc=$?
        assert_success $rc "AR-001: 批处理采集退出码 0"
        if [ -n "$output" ] && [ -s "$output" ]; then
            assert_success 0 "AR-001: 证据文件生成且非空"
            grep -q "=== CMD: dashboard -n 1 ===" "$output" 2>/dev/null
            assert_success $? "AR-001: 证据含 dashboard 分节标记"
            grep -q "=== CMD: thread -n 5 ===" "$output" 2>/dev/null
            assert_success $? "AR-001: 证据含 thread 分节标记"
            grep -q "=== CMD: shutdown" "$output" 2>/dev/null
            assert_success $? "AR-001: --shutdown 追加释放标记"
        else
            assert_failure 1 "AR-001: 证据文件生成且非空"
        fi
        kill "$JPID" 2>/dev/null
        wait "$JPID" 2>/dev/null
    else
        pkill -f 'java.*ArthasSleep' 2>/dev/null
        skip "AR-001: 冒烟 JVM 启动失败"
    fi
else
    skip "AR-001: 需 java + javac + arthas-boot.jar（首次 attach 需联网下载完整包）"
fi

# =============================================================================
# 6. SKILL.md / evals 格式（AT-007）
# =============================================================================
test_group_start "6. SKILL.md / evals"

assert_contains "$(head -3 "$SKILL_DIR/SKILL.md" 2>/dev/null)" "name: kptools-arthas" "AT-007: frontmatter name 正确"
if [ -f "$SKILL_DIR/SKILL.md" ]; then
    assert_success 0 "AT-007: SKILL.md 存在"
else
    assert_failure 1 "AT-007: SKILL.md 存在"
fi

for f in eval_queries.json evals.json; do
    if [ -f "$SKILL_DIR/evals/$f" ]; then
        python3 -c "import json; json.load(open('$SKILL_DIR/evals/$f'))" 2>/dev/null
        assert_success $? "AT-007: evals/$f JSON 合法"
        grep -q '"skill_name": "kptools-arthas"' "$SKILL_DIR/evals/$f"
        assert_success $? "AT-007: evals/$f skill_name 正确"
    else
        assert_failure 1 "AT-007: evals/$f 存在"
    fi
done
grep -q 'routes to kptools-devkit' "$SKILL_DIR/evals/eval_queries.json" 2>/dev/null
assert_success $? "AT-007: 含跨 SKILL 负例路由（java-perf）"

# =============================================================================
# 汇总
# =============================================================================
echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
if [ $TEST_FAILED -eq 0 ]; then
    green "  PASS: $TEST_PASSED"
else
    red "  PASS: $TEST_PASSED"
fi
red "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"
exit $TEST_FAILED
