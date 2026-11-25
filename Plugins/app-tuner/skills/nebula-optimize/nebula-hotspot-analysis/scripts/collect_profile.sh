#!/usr/bin/env bash
# collect_profile.sh — NebulaGraph 只读 profiling 采集
#
# 默认只打印采集计划（dry-run）；加 --execute 才真正采样。
# 不修改 Nebula 配置/数据，不重启服务。采样本身有少量开销。
#
# 用法:
#   bash collect_profile.sh --pid 12345
#   bash collect_profile.sh --pid 12345 --duration 30 \
#     --stats-url http://127.0.0.1:19669/stats \
#     --stats-url http://127.0.0.1:19779/stats --execute
#
# 选项:
#   --pid PID            必填，目标进程
#   --duration SEC       采样秒数，1-600，默认 20
#   --out DIR            输出目录；已存在即拒绝（每轮原子创建独立目录）
#   --flamegraph-dir DIR FlameGraph 脚本目录（含 stackcollapse-perf.pl/flamegraph.pl）
#   --stats-url URL      可重复，采集 HTTP /stats（仅允许 http/https 且路径以 /stats
#                        开头；认证请用 NEBULA_STATS_AUTH（curl -u 格式），不要写进 URL；
#                        凭据仅发送给 https 端点并走证书校验，http 端点不附凭据）
#   --stats-interval SEC /stats 窗口内重复采样间隔，默认 5
#   --no-perf            跳过 perf（只采系统指标）
#   --execute            真正执行；缺省仅打印计划
#   -h, --help
#
# 输出:
#   perf.data / perf.unfold / perf.folded / flamegraph.svg（可用时）
#   pidstat.txt / mpstat.txt / ss.txt / stats_<序号>[_r<N>].txt / env.txt / threads.txt
#   window.txt（窗口起止）/ summary.txt（各项采集状态）
# 退出码:
#   0 全部成功；1 有被请求的采集项执行失败；2 用法/参数错误；3 输出目录冲突
set -u

usage() {
  cat <<'EOF'
用法: collect_profile.sh --pid PID [选项]

  --pid PID            目标进程（必填）
  --duration SEC       采样秒数，1-600，默认 20
  --out DIR            输出目录（已存在即拒绝，每轮独立）
  --flamegraph-dir DIR FlameGraph 目录
  --stats-url URL      HTTP /stats 端点，可重复（http/https，路径以 /stats 开头）
  --stats-interval SEC /stats 窗口内采样间隔，默认 5
  --no-perf            跳过 perf
  --execute            真正执行（默认 dry-run）
  -h, --help

示例:
  bash collect_profile.sh --pid 12345
  bash collect_profile.sh --pid 12345 --duration 30 \
    --stats-url http://127.0.0.1:19669/stats --execute
EOF
}

PID=""
DURATION=20
OUT=""
FG_DIR=""
STATS_URLS=()
STATS_INTERVAL=5
USE_PERF=1
EXECUTE=0

# 带值选项先校验剩余参数数目，缺值输出用法并以 2 退出（避免 shift 2 失败后死循环）
while [ $# -gt 0 ]; do
  case "$1" in
    --pid|--duration|--out|--flamegraph-dir|--stats-interval)
      [ $# -ge 2 ] || { echo "ERROR: $1 缺少参数值" >&2; usage >&2; exit 2; }
      case "$1" in
        --pid) PID="$2" ;;
        --duration) DURATION="$2" ;;
        --out) OUT="$2" ;;
        --flamegraph-dir) FG_DIR="$2" ;;
        --stats-interval) STATS_INTERVAL="$2" ;;
      esac
      shift 2 ;;
    --stats-url)
      [ $# -ge 2 ] || { echo "ERROR: $1 缺少参数值" >&2; usage >&2; exit 2; }
      STATS_URLS+=("$2"); shift 2 ;;
    --no-perf) USE_PERF=0; shift ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: 未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

int_ok() { printf '%s' "$1" | grep -qE '^[0-9]+$'; }

# ---- 参数校验：dry-run 与执行共用 ----
if [ -z "$PID" ]; then
  echo "ERROR: 必须指定 --pid" >&2
  usage >&2
  exit 2
fi
if ! int_ok "$PID"; then
  echo "ERROR: --pid 须为正整数: ${PID}" >&2
  exit 2
fi
if ! int_ok "$DURATION" || [ "$DURATION" -lt 1 ] || [ "$DURATION" -gt 600 ]; then
  echo "ERROR: --duration 须为 1-600 的整数（sleep 会接受 infinity 等非法值，禁止透传）: ${DURATION}" >&2
  exit 2
fi
if [ "${#STATS_URLS[@]}" -gt 0 ]; then
  # 仅在实际采集 /stats 时校验 interval（未提供 --stats-url 时该参数不参与采集，不校验）
  if ! int_ok "$STATS_INTERVAL" || [ "$STATS_INTERVAL" -lt 1 ] || [ "$STATS_INTERVAL" -gt "$DURATION" ]; then
    echo "ERROR: --stats-interval 须为 1-${DURATION} 的整数" >&2
    exit 2
  fi
fi
if ! kill -0 "$PID" 2>/dev/null; then
  echo "ERROR: 进程不存在或无权访问: ${PID}" >&2
  exit 2
fi

# stats URL：校验协议/目标/路径；拒绝 userinfo（认证用 NEBULA_STATS_AUTH）
mask_url() {  # 显示用：去掉 userinfo 与 query，避免凭据/敏感参数进日志
  printf '%s' "$1" | sed -E 's#^[a-zA-Z]+://[^/]*@#<auth>@#; s#\?.*$#?***#'
}
auth_free_url() {  # 文件名/请求用：拒绝 userinfo
  case "$1" in *"@"*) return 1 ;; esac
  return 0
}
BAD_URL=0
i=0
for url in "${STATS_URLS[@]}"; do
  i=$((i+1))
  case "$url" in
    http://*|https://*) ;;
    *) echo "ERROR: --stats-url ${i} 仅允许 http/https: ${url}" >&2; BAD_URL=1 ;;
  esac
  if ! auth_free_url "$url"; then
    echo "ERROR: --stats-url ${i} 不允许带 userinfo（认证请用 NEBULA_STATS_AUTH，形如 user:pass）" >&2
    BAD_URL=1
  fi
  # 先剥离查询串再对路径做字面前缀比较（?format=json 是 v3.8.0 真实支持的参数）
  path=$(printf '%s' "$url" | sed -E 's#^[a-zA-Z]+://[^/]*##; s#\?.*$##')
  case "$path" in
    /stats|/stats/*) ;;
    *) echo "ERROR: --stats-url ${i} 路径须以 /stats 开头（查询串允许，如 ?format=json）: ${url}" >&2; BAD_URL=1 ;;
  esac
  # 拒绝路径回溯成分：curl 默认压平 ..，会把请求导向 /admin 等写端点
  case "$url" in
    *".."*|*"%2e"*|*"%2E"*)
      echo "ERROR: --stats-url ${i} 含回溯成分（../ 或 %2e），拒绝执行" >&2; BAD_URL=1 ;;
  esac
done
[ "$BAD_URL" -eq 0 ] || exit 2
# 认证绑定提示：凭据仅发送给 https 端点（curl 默认证书校验）；混采 http 端点不附凭据
if [ -n "${NEBULA_STATS_AUTH:-}" ]; then
  for url in "${STATS_URLS[@]}"; do
    case "$url" in
      http://*)
        echo "WARN: NEBULA_STATS_AUTH 仅对 https 端点生效，端点 ${i}（$(mask_url "$url")）为明文 http、不附凭据（采集可能 401 失败）" >&2 ;;
    esac
  done
fi

if [ -z "$OUT" ]; then
  OUT="./profile-$(date +%Y%m%d_%H%M%S)_$$"
fi
if [ -e "$OUT" ]; then
  echo "ERROR: 输出目录已存在: ${OUT}——每轮使用独立目录；复用会截断上轮 env.txt 等产物，已拒绝。请换目录或先归档删除。" >&2
  exit 3
fi

echo "collect_profile"
echo "  pid=${PID} duration=${DURATION}s out=${OUT} perf=${USE_PERF} execute=${EXECUTE}"
echo "  stats_interval=${STATS_INTERVAL}s stats_endpoints=${#STATS_URLS[@]}"
echo "------------------------------------------------------------"

if [ "$EXECUTE" != "1" ]; then
  echo "[dry-run] 将执行："
  echo "  mkdir ${OUT} && chmod 700 ${OUT}          # 每轮原子创建独立目录，并收紧读权限"
  echo "  ps -Lo pid,tid,comm,psr,pcpu -p ${PID} > ${OUT}/threads.txt   # 仅枚举目标进程线程"
  echo "  白名单字段 env.txt（PID/线程数/进程名/CPU/内存，不保存完整启动参数）"
  echo "  同一窗口并行启动: perf record / pidstat / mpstat（各 ${DURATION}s），"
  echo "    /stats 每 ${STATS_INTERVAL}s 采一轮（文件按序号命名，不落 URL）"
  echo "  窗口结束后: perf script → stackcollapse → flamegraph"
  i=0
  for url in "${STATS_URLS[@]}"; do
    i=$((i+1))
    echo "  curl -s --proto '=http,https' --max-time 5 -- \"$(mask_url "$url")\" > ${OUT}/stats_${i}_r<k>.txt"
  done
  echo ""
  echo "确认影响后加 --execute 运行。"
  exit 0
fi

# ---- 每轮原子创建独立输出目录；父目录须存在；chmod 700 限制其他用户读取 ----
PARENT=$(dirname "$OUT")
mkdir -p "$PARENT" || { echo "ERROR: 无法创建父目录 ${PARENT}" >&2; exit 1; }
mkdir "$OUT" 2>/dev/null || { echo "ERROR: 输出目录已存在或无法创建: ${OUT}" >&2; exit 3; }
chmod 700 "$OUT"

# ---- env.txt：只记录诊断白名单字段，不保存完整启动参数（可能含认证值） ----
{
  date
  uname -a
  command -v lscpu >/dev/null && lscpu
  command -v numactl >/dev/null && numactl -H
  echo "pid=${PID}"
  ps -o pid,nlwp,comm,pcpu,pmem,rss -p "$PID" 2>/dev/null || true
} > "${OUT}/env.txt" 2>&1

# ---- threads.txt：按指定 PID 枚举全部线程（保留 TID 与线程名），不整机枚举 ----
ps -Lo pid,tid,comm,psr,pcpu -p "$PID" 2>/dev/null > "${OUT}/threads.txt" || true

# ---- 同一稳态窗口并行采集；起止时间入 window.txt；perf 解析放窗口结束后 ----
date '+start=%F %T' > "${OUT}/window.txt"
COLLECT_PIDS=()

if [ "$USE_PERF" = "1" ]; then
  if command -v perf >/dev/null 2>&1; then
    ( perf record -F 999 --call-graph dwarf -p "$PID" -o "${OUT}/perf.data" -- sleep "$DURATION" \
        > "${OUT}/perf_record.log" 2>&1 ) &
    COLLECT_PIDS+=($!)
    PERF_STATUS=running
  else
    echo "[SKIP] 未找到 perf，跳过 CPU 采样" >&2
    PERF_STATUS=skip
  fi
else
  PERF_STATUS=skip
fi

if command -v pidstat >/dev/null 2>&1; then
  ( pidstat -p "$PID" 1 "$DURATION" > "${OUT}/pidstat.txt" 2>&1; echo $? > "${OUT}/.pidstat_rc" ) &
  COLLECT_PIDS+=($!)
  PIDSTAT_STATUS=running
else
  echo "[SKIP] 未找到 pidstat" >&2
  PIDSTAT_STATUS=skip
fi

if command -v mpstat >/dev/null 2>&1; then
  ( mpstat -P ALL 1 "$DURATION" > "${OUT}/mpstat.txt" 2>&1; echo $? > "${OUT}/.mpstat_rc" ) &
  COLLECT_PIDS+=($!)
  MPSTAT_STATUS=running
else
  echo "[SKIP] 未找到 mpstat" >&2
  MPSTAT_STATUS=skip
fi

if command -v ss >/dev/null 2>&1; then
  ss -s > "${OUT}/ss.txt" 2>&1 || true
  SS_STATUS=ok
else
  SS_STATUS=skip
fi

# /stats 在同一窗口内按间隔重复采样；产物按端点序号+轮次命名，不把 URL 写进文件名/日志
STATS_STATUS=ok
if [ "${#STATS_URLS[@]}" -gt 0 ]; then
  if command -v curl >/dev/null 2>&1; then
    # stats 采集整体放后台子 shell（登记到 COLLECT_PIDS，与 perf/pidstat/mpstat 同窗并行）；
    # 外层按轮次、轮内**并发**访问所有端点（& + wait）——串行访问时首端点耗满 5s 会使
    # 后续端点的本轮采样出窗；elapsed 用真实时间差（date +%s）累计，不随 curl 耗时漂移
    (
      start_ts=$(date +%s)
      round=0
      while :; do
        [ $(( $(date +%s) - start_ts )) -ge "$DURATION" ] && break
        round=$((round+1))
        i=0
        round_pids=""
        for url in "${STATS_URLS[@]}"; do
          i=$((i+1))
          (
            f="${OUT}/stats_${i}_r${round}.txt"
            # 认证只发给 https 端点（curl 默认做证书校验，不使用 -k）：同一凭据发给远端
            # http 端点等于把密码明文送出去；http 端点一律不附凭据
            case "$url" in
              https://*) extra=(); [ -n "${NEBULA_STATS_AUTH:-}" ] && extra=(-u "$NEBULA_STATS_AUTH") ;;
              http://*)  extra=() ;;
            esac
            code=$(curl -s --proto '=http,https' --max-time 5 -o "$f" -w '%{http_code}' \
                      ${extra+"${extra[@]}"} -- "$url" 2>/dev/null)
            # 逐轮核对 HTTP 状态与响应格式：401/404/503 等带正文的错误页会被 -o 写进文件、
            # 非空检查当成功——必须按状态码 + JSON 首字符判定
            if [ "$code" != "200" ] || ! head -c 64 "$f" 2>/dev/null | grep -q '^[[:space:]]*[{[]'; then
              { printf 'round=%s endpoint=%s url=%s http=%s' "$round" "$i" "$(mask_url "$url")" "${code:-none}"
                [ -s "$f" ] || printf ' empty'
                printf '\n'; } >> "${OUT}/stats_fail_detail.txt"
              touch "${OUT}/.stats_fail"
            fi
          ) &
          round_pids="${round_pids} $!"
        done
        # 等**本轮**全部端点完成后才进入间隔 sleep（下一轮窗口从轮首统一计时）
        for p in $round_pids; do wait "$p" 2>/dev/null; done
        sleep "$STATS_INTERVAL"
      done
    ) &
    COLLECT_PIDS+=($!)
  else
    echo "[SKIP] 未找到 curl，跳过 stats 采样" >&2
    STATS_STATUS=skip
  fi
fi

# 等待窗口内启动的采集全部结束
for cpid in "${COLLECT_PIDS[@]}"; do
  wait "$cpid" 2>/dev/null
done
date '+end=%F %T' >> "${OUT}/window.txt"
# stats 子 shell 的失败标志（子 shell 改不了父变量，经标志文件传递）
if [ -e "${OUT}/.stats_fail" ]; then
  rm -f "${OUT}/.stats_fail"
  STATS_STATUS=fail
  echo "[FAIL] 存在失败的 stats 采样轮次（非 200 或响应非 JSON，逐轮明细见 ${OUT}/stats_fail_detail.txt）" >&2
fi

# ---- 窗口结束后解析 perf；逐项记录状态 ----
RC=0
{
  echo "perf=${PERF_STATUS}"
  echo "pidstat=${PIDSTAT_STATUS}"
  echo "mpstat=${MPSTAT_STATUS}"
  echo "ss=${SS_STATUS}"
  echo "stats=${STATS_STATUS}"
} > "${OUT}/summary.txt"

if [ "$PERF_STATUS" = "running" ]; then
  if [ -s "${OUT}/perf.data" ]; then
    if perf script -i "${OUT}/perf.data" > "${OUT}/perf.unfold" 2>"${OUT}/perf_script.log"; then
      PERF_STATUS=ok
      if [ -n "$FG_DIR" ] && [ -x "${FG_DIR}/stackcollapse-perf.pl" ]; then
        "${FG_DIR}/stackcollapse-perf.pl" "${OUT}/perf.unfold" > "${OUT}/perf.folded" 2>/dev/null || true
        if [ -x "${FG_DIR}/flamegraph.pl" ] && [ -s "${OUT}/perf.folded" ]; then
          "${FG_DIR}/flamegraph.pl" "${OUT}/perf.folded" > "${OUT}/flamegraph.svg" 2>/dev/null || true
        else
          echo "[FAIL] flamegraph.pl 不可用或 folded 为空" >&2
        fi
      else
        echo "[SKIP] 未提供 --flamegraph-dir 或脚本不可执行，跳过 SVG（perf.unfold 已保留）" >&2
      fi
    else
      echo "[FAIL] perf script 失败，见 ${OUT}/perf_script.log" >&2
      PERF_STATUS=fail
    fi
  else
    echo "[FAIL] perf record 未产生样本（权限/paranoid），见 ${OUT}/perf_record.log" >&2
    PERF_STATUS=fail
  fi
fi
if [ "$PIDSTAT_STATUS" = "running" ]; then
  if [ "$(cat "${OUT}/.pidstat_rc" 2>/dev/null || echo 1)" != "0" ]; then
    PIDSTAT_STATUS=fail; echo "[FAIL] pidstat 退出非零（错误文本混入日志也会非空，按退出码判）" >&2
  elif [ -s "${OUT}/pidstat.txt" ]; then
    PIDSTAT_STATUS=ok
  else
    PIDSTAT_STATUS=fail; echo "[FAIL] pidstat 无样本" >&2
  fi
  rm -f "${OUT}/.pidstat_rc"
fi
if [ "$MPSTAT_STATUS" = "running" ]; then
  if [ "$(cat "${OUT}/.mpstat_rc" 2>/dev/null || echo 1)" != "0" ]; then
    MPSTAT_STATUS=fail; echo "[FAIL] mpstat 退出非零（错误文本混入日志也会非空，按退出码判）" >&2
  elif [ -s "${OUT}/mpstat.txt" ]; then
    MPSTAT_STATUS=ok
  else
    MPSTAT_STATUS=fail; echo "[FAIL] mpstat 无样本" >&2
  fi
  rm -f "${OUT}/.mpstat_rc"
fi
{
  echo "perf=${PERF_STATUS}"
  echo "pidstat=${PIDSTAT_STATUS}"
  echo "mpstat=${MPSTAT_STATUS}"
  echo "ss=${SS_STATUS}"
  echo "stats=${STATS_STATUS}"
} > "${OUT}/summary.txt"

# 被请求的采集项失败 → 非零退出码（与 2=用法、3=目录冲突区分）
for st in "$PERF_STATUS" "$PIDSTAT_STATUS" "$MPSTAT_STATUS" "$STATS_STATUS"; do
  [ "$st" = "fail" ] && RC=1
done

echo "------------------------------------------------------------"
if [ "$RC" -eq 0 ]; then
  echo "完成：${OUT}"
else
  echo "完成（存在失败项，见 ${OUT}/summary.txt）：${OUT}"
fi
ls -lh "$OUT" 2>/dev/null | sed 's/^/  /'
exit "$RC"
