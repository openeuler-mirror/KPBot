#!/bin/bash
# lbprof_common.sh —— dplb-profiler 采集脚本公共库（唯一权威副本，避免多脚本逐字复制）。
# 被 extract_metric_v2.sh / extract_metric_single.sh / extract_metric_pd.sh source；
# 部署时须与采集脚本放同一目录一起上传（库目录可用环境变量 LBPROF_LIB_DIR 覆盖，
# 各脚本内默认取自身所在目录）。URL 白名单、33 列行发射、单池采样循环的唯一实现均在本库。

# Security: url files are typically generated from ticket/chat/wiki text (not fully
#   trusted) -- whitelist each line so curl only ever fetches http(s) URLs (a crafted
#   line like file:///etc/passwd would otherwise be fetched and parsed into the CSV).
#   白名单校验与 host(:port) tag 派生在此唯一实现；任一行不合法即整脚本非零退出。
lbprof_check_url() {
  # $1 = urls 文件的一行, $2 = 来源文件名（报错定位用）
  if ! printf '%s' "$1" | grep -qE '^https?://[A-Za-z0-9._:/-]+$'; then
    echo "Error: invalid URL line in $2 (expect http(s)://host[:port]/path; allowed chars: A-Za-z0-9 . _ : / -): $1" >&2
    exit 1
  fi
}

lbprof_url_tag() {
  # $1 = url -> 输出 host(:port)，作为 CSV 的 ip_port 列取值
  local t="${1#http://}"
  t="${t#https://}"
  printf '%s' "${t%%/*}"
}

# 单池脚本共用：把 urls 文件加载为 URLS/TAGS 数组（逐行过白名单，空列表非零退出）
lbprof_load_urls() {
  # $1 = urls 文件
  URLS=()
  TAGS=()
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    lbprof_check_url "$line" "$1"
    URLS+=("$line")
    TAGS+=("$(lbprof_url_tag "$line")")
  done < "$1"
  if [ "${#URLS[@]}" -eq 0 ]; then echo "Error: no valid URLs in urls file" >&2; exit 1; fi
}

# 公共 awk 函数与值解析规则（唯一副本，见 lbprof_common.awk）。gawk 在 -f 与内联
# 程序并存时会忽略后者，故各脚本统一用 shell 相邻字符串拼接为单一程序参数加载。
_LBPROF_DIR="${LBPROF_LIB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
LBPROF_AWK_LIB="$(cat "$_LBPROF_DIR/lbprof_common.awk")"

# v2 口径（33 列）单行解析：v2 / PD 两个脚本共用的唯一实现。
# 列口径见 references/csv_format.md（report_spec.md §1 的采集侧镜像）。
lbprof_parse_row_v2() {
  awk -v ts="$1" -v tag="$2" "$LBPROF_AWK_LIB"'
    { lbprof_main() }
    END {
      printf "%s,%s,", ts, tag
      lbprof_emit_base()
      lbprof_emit_v2_hist()
    }
  '
}

# legacy 口径（22 列）单行解析：extract_metric_single.sh 专用的唯一实现。
# 与 v2 共用主解析块与基础 15 列，仅 hist 尾段不同（legacy 无 sum/count 原始列）。
lbprof_parse_row_legacy() {
  awk -v ts="$1" -v tag="$2" "$LBPROF_AWK_LIB"'
    { lbprof_main() }
    END {
      printf "%s,%s,", ts, tag
      lbprof_emit_base()
      lbprof_emit_legacy_hist()
    }
  '
}

# 单池脚本（v2 / single）共用的参数校验：时长必须为正整数、urls 文件必须存在
lbprof_check_args_single() {
  # $1 = 采样时长(秒), $2 = urls 文件, $3 = usage 文案
  if [ -z "$1" ] || [ -z "$2" ] || [ ! -f "$2" ]; then
    echo "$3" >&2
    exit 1
  fi
  case "$1" in (*[!0-9]*|'') echo "Error: sampling duration must be a positive integer (seconds)" >&2; exit 1;; esac
  if [ "$1" -le 0 ]; then echo "Error: sampling duration must be a positive integer (seconds)" >&2; exit 1; fi
}

# 单池采样循环（v2 / single 共用的唯一实现）：
#   $1=采样时长秒  $2=采样间隔秒  $3=输出文件  $4=回退行数据列数（v2=33 / single=22）
#   $5=单行解析函数名  $6=完成消息后缀（可空）
# 依赖全局：URLS/TAGS（lbprof_load_urls 加载）、CURL_TIMEOUT。
# 时序语义：每轮以 ROUND_TS（轮询发起时刻）为 timestamp——见 SKILL.md「采集产物契约」。
lbprof_poll_loop() {
  local duration="$1" interval="$2" out="$3" ncols="$4" parser="$5" done_msg="$6"
  local start now elapsed left rounds=0 round_ts body last i fallback
  fallback=""
  for ((i = 0; i < ncols; i++)); do fallback="${fallback},-1"; done
  start=$(date +%s)
  while :; do
    now=$(date +%s)
    elapsed=$((now - start))
    [ "$elapsed" -ge "$duration" ] && break
    round_ts="$(date '+%Y-%m-%d %H:%M:%S')"
    rounds=$((rounds + 1))
    for i in "${!URLS[@]}"; do
      body=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "${URLS[$i]}" 2>/dev/null) || body=""
      if [ -n "$body" ]; then
        printf '%s' "$body" | "$parser" "$round_ts" "${TAGS[$i]}" >> "$out"
      else
        echo "${round_ts},${TAGS[$i]}${fallback}" >> "$out"
      fi
    done
    echo "[round $rounds] elapsed ${elapsed}s / ${duration}s, rows $(wc -l < "$out")"
    now=$(date +%s)
    left=$((duration - (now - start)))
    [ "$left" -le 0 ] && break
    sleep "$((left < interval ? left : interval))"
  done

  echo "Sampling duration has been reached ($(( $(date +%s) - start )) seconds), performing final counter snapshot..."
  for i in "${!URLS[@]}"; do
    body=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "${URLS[$i]}" 2>/dev/null) || body=""
    if [ -n "$body" ]; then
      last=$("$parser" "final" "${TAGS[$i]}")
      echo "  [${TAGS[$i]}] request_success_total=$(echo "$last" | cut -d, -f6) prompt_tokens_total=$(echo "$last" | cut -d, -f9) generation_tokens_total=$(echo "$last" | cut -d, -f10)"
    else
      echo "  [${TAGS[$i]}] final snapshot failed (unreachable)"
    fi
  done
  echo "Sampling complete: $rounds rounds x ${#URLS[@]} nodes, output $out${done_msg}"
}
