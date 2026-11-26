#!/usr/bin/env bash
set -euo pipefail

MYSQL_CMD="mysql"
OUTPUT_DIR=""
DATABASE=""

usage() {
  cat <<'EOF'
Usage:
  collect_mysql_status.sh --output-dir <dir> [--mysql-cmd <mysql>] [--database <db>] [-- mysql options...]

Examples:
  collect_mysql_status.sh --output-dir output/mysql -- -uroot -p
  collect_mysql_status.sh --output-dir output/mysql --mysql-cmd /usr/bin/mysql -- --defaults-extra-file=/root/.my.cnf
EOF
}

MYSQL_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --mysql-cmd) MYSQL_CMD="$2"; shift 2 ;;
    --database) DATABASE="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --) shift; MYSQL_ARGS=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "${OUTPUT_DIR}" ]]; then
  echo "missing required --output-dir" >&2
  exit 2
fi

mkdir -p "${OUTPUT_DIR}"
STATUS_FILE="${OUTPUT_DIR}/collection_status.json"
ERROR_FILE="${OUTPUT_DIR}/collection_error.log"
: > "${ERROR_FILE}"

if ! command -v "${MYSQL_CMD}" >/dev/null 2>&1 && [[ ! -x "${MYSQL_CMD}" ]]; then
  cat > "${STATUS_FILE}" <<EOF
{"status":"failed","fallback_notes":["mysql_client_missing"],"output_dir":"${OUTPUT_DIR}"}
EOF
  echo "mysql client not found: ${MYSQL_CMD}" >&2
  exit 1
fi

run_query() {
  local name="$1"
  local query="$2"
  local output="${OUTPUT_DIR}/${name}.txt"
  if [[ -n "${DATABASE}" ]]; then
    "${MYSQL_CMD}" "${MYSQL_ARGS[@]}" "${DATABASE}" -e "${query}" > "${output}" 2>> "${ERROR_FILE}"
  else
    "${MYSQL_CMD}" "${MYSQL_ARGS[@]}" -e "${query}" > "${output}" 2>> "${ERROR_FILE}"
  fi
}

failed=0
run_query "variables" "SHOW VARIABLES;" || failed=1
run_query "global_status" "SHOW GLOBAL STATUS;" || failed=1
run_query "innodb_status" "SHOW ENGINE INNODB STATUS\\G" || failed=1

if [[ "${failed}" -ne 0 ]]; then
  cat > "${STATUS_FILE}" <<EOF
{"status":"failed","fallback_notes":["mysql_collection_failed"],"output_dir":"${OUTPUT_DIR}","error_file":"${ERROR_FILE}"}
EOF
  echo "MySQL status collection failed; see ${ERROR_FILE}" >&2
  exit 1
fi

cat > "${STATUS_FILE}" <<EOF
{"status":"ok","outputs":["variables.txt","global_status.txt","innodb_status.txt"],"output_dir":"${OUTPUT_DIR}"}
EOF
echo "MySQL status collected in ${OUTPUT_DIR}"
