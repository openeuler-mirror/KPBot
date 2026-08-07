#!/usr/bin/env bash
#
# run_tpcds_benchmark.sh - 执行 TPC-DS 所有 SQL 并统计性能
#
# 用法（在 server2-spark 容器内执行）:
#   ./run_tpcds_benchmark.sh [--database DATABASE] [--output-dir DIR]
#
# 示例:
#   ./run_tpcds_benchmark.sh --database tpcds --output-dir /tmp/tpcds-results
#
# 输出:
#   ${OUTPUT_DIR}/logs/q01.log          - 每个 SQL 的执行日志
#   ${OUTPUT_DIR}/tpcds_results.csv     - 所有 SQL 的执行结果
#   ${OUTPUT_DIR}/tpcds_summary.txt     - 汇总报告
#

set -euo pipefail

# 默认值
DATABASE="${DATABASE:-tpcds}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/tpcds-results-$(date +%Y%m%d_%H%M%S)}"
SPARK_SQL="spark-sql"
SQL_DIR="${SQL_DIR:-/home/spark_cluster/gluten/tools/gluten-it/common/src/main/resources/tpcds-queries}"

# 解析参数
while [[ $# -gt 0 ]]; do
  case $1 in
    --database)
      DATABASE="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --sql-dir)
      SQL_DIR="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

mkdir -p "${OUTPUT_DIR}/logs"
RESULTS_FILE="${OUTPUT_DIR}/tpcds_results.csv"
SUMMARY_FILE="${OUTPUT_DIR}/tpcds_summary.txt"

echo "=========================================="
echo "TPC-DS Benchmark 执行脚本"
echo "=========================================="
echo "数据库: ${DATABASE}"
echo "SQL 目录: ${SQL_DIR}"
echo "输出目录: ${OUTPUT_DIR}"
echo "日志目录: ${OUTPUT_DIR}/logs"
echo "结果文件: ${RESULTS_FILE}"
echo "汇总报告: ${SUMMARY_FILE}"
echo "=========================================="
echo ""

# 初始化结果文件
echo "query,status,execution_time_ms,rows,error_message" > "${RESULTS_FILE}"

# 获取所有 SQL 文件（排除 variant 后缀如 _a, _b）
get_sql_files() {
  local sql_dir="$1"
  # 提取基础 query 号排序
  ls "${sql_dir}"/*.sql 2>/dev/null | \
    sed 's/.*\/q\([0-9]*\).*/\1/' | sort -n | uniq | \
    while read qnum; do
      # 找对应的 SQL 文件（可能有 q14a.sql, q14b.sql 等变体）
      ls "${sql_dir}"/q${qnum}*.sql 2>/dev/null
    done
}

# 执行单个 SQL 并计时
execute_query() {
  local sql_file="$1"
  local query_name
  query_name=$(basename "${sql_file}" .sql)

  local log_file="${OUTPUT_DIR}/logs/${query_name}.log"
  local start_time end_time duration
  local status="SUCCESS"
  local error_msg=""
  local row_count=0

  echo -n "Executing ${query_name}... "

  # 记录开始时间
  start_time=$(date +%s%3N)

  # 清空日志文件
  > "${log_file}"

  {
    echo "=========================================="
    echo "Query: ${query_name}"
    echo "SQL File: ${sql_file}"
    echo "Start Time: $(date)"
    echo "=========================================="
    echo ""
  } >> "${log_file}"

  # 执行 SQL，捕获输出和错误
  local output
  output=$(${SPARK_SQL} \
    --database "${DATABASE}" \
    -f "${sql_file}" 2>&1) || {
    status="FAILED"
    error_msg=$(echo "$output" | grep -E "Error|Exception|FAILED" | head -3 | tr '\n' ' ' | cut -c1-300)
  }

  # 计算执行时间
  end_time=$(date +%s%3N)
  duration=$((end_time - start_time))

  # 统计行数（SUCCESS 时）
  if [[ "$status" == "SUCCESS" ]]; then
    row_count=$(echo "$output" | grep -c "^[0-9]" || echo 0)
  fi

  # 写入日志文件
  {
    echo ""
    echo "=========================================="
    echo "End Time: $(date)"
    echo "Duration: ${duration} ms"
    echo "Status: ${status}"
    if [[ -n "$error_msg" ]]; then
      echo "Error: ${error_msg}"
    fi
    echo "=========================================="
  } >> "${log_file}"

  # 追加输出到日志
  echo "" >> "${log_file}"
  echo "--- SQL Output ---" >> "${log_file}"
  echo "$output" >> "${log_file}"

  # 写入结果 CSV
  echo "${query_name},${status},${duration},${row_count},\"${error_msg}\"" >> "${RESULTS_FILE}"

  if [[ "$status" == "SUCCESS" ]]; then
    echo "OK (${duration} ms, ${row_count} rows) -> ${log_file}"
  else
    echo "FAILED (${duration} ms) -> ${log_file}"
    echo "   Error: ${error_msg}"
  fi
}

# 主执行循环
echo "开始执行 TPC-DS 查询..."
echo ""

total_files=$(get_sql_files "${SQL_DIR}" | wc -l)
echo "共发现 ${total_files} 个查询文件"
echo ""

completed=0
failed=0
success_time=0
start_total=$(date +%s)

for sql_file in $(get_sql_files "${SQL_DIR}"); do
  execute_query "${sql_file}"
  last_status=$(tail -1 "${RESULTS_FILE}" | cut -d',' -f2)
  if [[ "$last_status" == "SUCCESS" ]]; then
    ((completed++)) || true
  else
    ((failed++)) || true
  fi
done

end_total=$(date +%s)
total_time=$((end_total - start_total))

# 生成汇总报告
{
  echo "=========================================="
  echo "TPC-DS Benchmark 汇总报告"
  echo "=========================================="
  echo "执行时间: $(date)"
  echo "数据库: ${DATABASE}"
  echo "SQL 目录: ${SQL_DIR}"
  echo "日志目录: ${OUTPUT_DIR}/logs"
  echo ""
  echo "----------------------------------------"
  echo "执行统计:"
  echo "  总查询数: $((completed + failed))"
  echo "  成功数: ${completed}"
  echo "  失败数: ${failed}"
  echo "  总耗时: ${total_time} 秒 ($(($total_time / 60)) 分 $(($total_time % 60)) 秒)"
  echo ""
  echo "----------------------------------------"
  echo "性能统计 (按执行时间排序 TOP 20):"
  echo ""

  # 按执行时间排序输出 TOP 20
  if command -v awk >/dev/null 2>&1; then
    awk -F',' 'NR>1 && $2=="SUCCESS" {printf "  %-10s %10s ms %6s rows\n", $1, $3, $4}' "${RESULTS_FILE}" | \
      sort -k2 -n | head -20

    echo ""
    echo "----------------------------------------"
    echo "汇总统计:"

    # 计算统计信息
    awk -F',' 'NR>1 && $2=="SUCCESS" {
      sum+=$3; count++;
      if($3<min||min==0)min=$3;
      if($3>max)max=$3;
      total_rows+=$4
    } END {
      if (count > 0) {
        avg=sum/count
        printf "  成功查询数: %d\n", count
        printf "  总行数: %d\n", total_rows
        printf "  最快查询: %d ms\n", min
        printf "  最慢查询: %d ms\n", max
        printf "  平均耗时: %.2f ms\n", avg
        printf "  总耗时: %d ms (%.2f min)\n", sum, sum/60000
      }
    }' "${RESULTS_FILE}"
  fi

  echo ""
  echo "----------------------------------------"
  echo "失败查询:"
  echo ""
  awk -F',' 'NR>1 && $2=="FAILED" {printf "  %-10s %s\n", $1, $5}' "${RESULTS_FILE}"

  echo ""
  echo "----------------------------------------"
  echo "输出文件:"
  echo "  日志目录: ${OUTPUT_DIR}/logs/"
  echo "  结果CSV: ${RESULTS_FILE}"
  echo "  汇总报告: ${SUMMARY_FILE}"
  echo ""
  echo "=========================================="
} > "${SUMMARY_FILE}"

echo ""
echo "=========================================="
echo "执行完成!"
echo "汇总报告: ${SUMMARY_FILE}"
echo "详细结果: ${RESULTS_FILE}"
echo "SQL日志: ${OUTPUT_DIR}/logs/"
echo "=========================================="

# 显示汇总
cat "${SUMMARY_FILE}"
