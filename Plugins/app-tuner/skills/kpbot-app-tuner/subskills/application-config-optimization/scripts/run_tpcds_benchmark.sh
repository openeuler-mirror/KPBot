#!/usr/bin/env bash
set -euo pipefail

# run_tpcds_benchmark.sh — 输出 TPC-DS benchmark 执行命令 JSON
#
# 推荐器模式：只输出推荐命令，不直接执行。
# 主框架读取 JSON 后通过安全门控执行。
#
# 用法:
#   run_tpcds_benchmark.sh [--database DATABASE] [--output-dir DIR] [--sql-dir DIR]
#
# 输出: JSON 格式的候选动作列表（stdout）

DATABASE="${DATABASE:-tpcds}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/tpcds-results-$(date +%Y%m%d_%H%M%S)}"
SQL_DIR="${SQL_DIR:-/home/spark_cluster/gluten/tools/gluten-it/common/src/main/resources/tpcds-queries}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --database)   DATABASE="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --sql-dir)    SQL_DIR="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--database <name>] [--output-dir <dir>] [--sql-dir <dir>]"
      echo "推荐器模式：输出 benchmark 执行命令 JSON，不执行修改。"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

cat << EOF
{
  "skill": "run_tpcds_benchmark",
  "description": "执行 TPC-DS 所有 SQL 并统计性能",
  "benchmark_config": {
    "database": "${DATABASE}",
    "sql_dir": "${SQL_DIR}",
    "output_dir": "${OUTPUT_DIR}",
    "results_file": "${OUTPUT_DIR}/tpcds_results.csv",
    "summary_file": "${OUTPUT_DIR}/tpcds_summary.txt"
  },
  "actions": [
    {
      "action_id": "tpcds-benchmark-run",
      "action_type": "benchmark_execute",
      "description": "在 Spark 容器内执行 TPC-DS 所有 SQL 查询并统计性能",
      "commands_execute": [
        "mkdir -p ${OUTPUT_DIR}/logs",
        "echo 'query,status,execution_time_ms,rows,error_message' > ${OUTPUT_DIR}/tpcds_results.csv",
        "for sql_file in ${SQL_DIR}/q*.sql; do qname=\$(basename \$sql_file .sql); start=\$(date +%s%3N); spark-sql -e \"USE ${DATABASE}; \$(cat \$sql_file)\" > ${OUTPUT_DIR}/logs/\${qname}.log 2>&1; status=\$?; end=\$(date +%s%3N); elapsed=\$((end - start)); echo \"\$qname,\$status,\$elapsed,0,\" >> ${OUTPUT_DIR}/tpcds_results.csv; done",
        "echo 'TPC-DS benchmark completed' > ${OUTPUT_DIR}/tpcds_summary.txt"
      ],
      "rollback": [],
      "risk": "low",
      "validation": "test -f ${OUTPUT_DIR}/tpcds_results.csv && wc -l ${OUTPUT_DIR}/tpcds_results.csv"
    }
  ]
}
EOF
