#!/usr/bin/env bash
# kptools-thread-affinity-collector — 运行期线程绑核采集器（双通道，只读）
#
# 通道 A（配置面）：/proc/PID/task/*/status → Cpus_allowed_list（允许掩码时间线）
# 通道 B（实际面）：/proc/PID/task/*/stat → processor(39)/utime(14)/stime(15)（落点+忙闲）
# 每个 tick 重新枚举线程清单（procfs 为准，新线程即刻入采，无启动期名册）。
#
# 输出（三者配套）：
#   <out>/samples.jsonl      机器格式时间序列（首行 meta，中间每行一个 tick，
#                            run 模式末行 end）——供 kptools-thread-affinity-analyzer 消费
#   <out>/topology_raw.txt   采样机拓扑快照（=== 分节）
#   <out>/trace.log          人类可读事件日志（新线程/掩码变化/线程退出/心跳/
#                            终态名册/统计）——cat 即懂，无需工具
#
# 退出码：0 成功；1 参数错误；2 环境不可用/权限不足；3 目标不存在；130 信号中断
set -euo pipefail
export LC_ALL=C

VERSION="1.1.0"
INTERVAL=0.5
DURATION=0
TOLERATE=10
PROC_ROOT="${AT_PROC_ROOT:-/proc}"
SYS_ROOT="${AT_SYS_ROOT:-/sys}"
OUT=""
PID=""
MODE="run"          # run | init | tick
AUTO=0
AUTO_LIST=0
QUIET=0

usage() {
  cat <<'EOF'
用法: collect.sh -p <PID> [选项]              采集指定进程
      collect.sh --auto [选项]                自动发现训练/推理主进程
      collect.sh --auto-list                   列出 --auto 候选
选项:
  -p <PID>       目标进程 PID（与 --auto 二选一）
  -i <sec>       采样间隔（下限 0.05）                [默认 0.5]
  -T <sec>       时长上限，0=不限直至目标结束/信号     [默认 0]
  --tolerate <N> 目标消失连续 N tick 才收尾            [默认 10]
  --out <dir>    输出目录（run 模式须为空或不存在）    [默认 af_<UTC时间>]
  --quiet        不打印进度
  --proc-root <dir> procfs 根（默认 /proc，测试注入用）
  --init / --tick   内部子命令：只建 meta+topology / 只追加一个 tick
产出: samples.jsonl(机器格式) + trace.log(人类可读) + topology_raw.txt(拓扑)
      分析与 HTML 报告由 kptools-thread-affinity-analyzer 对输出目录生成
EOF
}

die() { echo "ERROR: $1" >&2; exit 1; }
now() { echo "${EPOCHREALTIME:-$(date +%s.%N)}"; }

# epoch 秒 → HH:MM:SS（日志用；date 不支持 @epoch 的环境退化为原值）
fmt_ts() {
  local s=${1%%.*}
  date -u -d "@$s" '+%H:%M:%S' 2>/dev/null || echo "$s"
}

# ---------------------------------------------------------------- 参数解析
while [ $# -gt 0 ]; do
  case "$1" in
    -p) PID="${2:?}"; shift 2 ;;
    -i) INTERVAL="${2:?}"; shift 2 ;;
    -T) DURATION="${2:?}"; shift 2 ;;
    --tolerate) TOLERATE="${2:?}"; shift 2 ;;
    --out) OUT="${2:?}"; shift 2 ;;
    --proc-root) PROC_ROOT="${2:?}"; shift 2 ;;
    --auto) AUTO=1; shift ;;
    --auto-list) AUTO_LIST=1; shift ;;
    --init) MODE="init"; shift ;;
    --tick) MODE="tick"; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数 $1（--help 看用法）" ;;
  esac
done

# 数值校验：先格式（纯数字/至多一个小数点）再范围——awk 直接比较大小时
# 非数字字符串按字符串比较也能通过（"abc" >= "0.05" 成立），必须先挡格式
is_num() {
  case "$1" in
    ''|*[!0-9.]*) return 1 ;;
    *.*.*)         return 1 ;;   # 多个小数点非数字
    *)             return 0 ;;
  esac
}
ok_num() { awk -v v="$1" -v m="$2" 'BEGIN{exit !(v>=m)}'; }

is_num "$INTERVAL"  || die "-i 需为数字（got '$INTERVAL'）"
ok_num  "$INTERVAL" 0.05 || die "采样间隔下限 0.05s"
is_num "$DURATION" || die "-T 需为数字（got '$DURATION'）"
ok_num  "$DURATION" 0 || die "-T 不能为负"
case "$TOLERATE" in ''|*[!0-9]*) die "--tolerate 需为正整数（got '$TOLERATE'）" ;; esac
ok_num "$TOLERATE" 1 || die "--tolerate 至少 1"

# ---------------------------------------------------------------- 自动发现
auto_scan() {  # 输出 "pid starttime comm" 候选，按启动时间降序
  local d pid comm lc st
  for d in "$PROC_ROOT"/[0-9]*; do
    pid=${d##*/}
    [ -f "$d/comm" ] || continue
    IFS= read -r comm < "$d/comm" 2>/dev/null || continue
    lc=${comm,,}
    [ -n "$lc" ] || continue
    # shellcheck disable=SC2254
    # comm 是可执行名前 15 字节：版本化解释器产生 python3.11/python3.9 等
    # 形态，故用前缀匹配 python* 而非精确名
    case "$lc" in
      python*|torchrun|raylet|deepspeed|vllm*) ;;
      *) continue ;;
    esac
    st=$(awk '{idx=index($0,"("); copy=$0; sub(/^.*\)/,"",copy); m=split(copy,f," "); if (m>=20) printf "%d", f[20]}' \
          "$d/stat" 2>/dev/null) || continue
    [ -n "$st" ] || continue
    echo "$st $pid $comm"
  done | sort -rn
}

if [ "$AUTO_LIST" -eq 1 ]; then
  echo "pid start_time comm"
  auto_scan | awk '{print $2, $1, $3}'
  exit 0
fi

if [ "$AUTO" -eq 1 ]; then
  [ -z "$PID" ] || die "--auto 与 -p 互斥"
  PID=$(auto_scan | awk 'NR==1{print $2}')
  [ -n "$PID" ] || die "AUTO_NO_CANDIDATE（未发现 python*/torchrun/vllm/raylet 类进程，用 -p 指定）"
  echo "auto target: pid=$PID comm=$(cat "$PROC_ROOT/$PID/comm" 2>/dev/null || echo '?')" >&2
fi

[ -n "$PID" ] || die "需要 -p <PID> 或 --auto（--help 看用法）"
case "$PID" in ''|*[!0-9]*) die "PID 必须是数字" ;; esac

# ---------------------------------------------------------------- 目标检查
[ -d "$PROC_ROOT/$PID/task" ] || { echo "ERROR: TARGET_MISSING pid=$PID" >&2; exit 3; }
if [ ! -r "$PROC_ROOT/$PID/task/$PID/stat" ]; then
  echo "ERROR: PROCFS_UNREADABLE（跨 uid 需 root：/proc/$PID/task/$PID/stat 不可读）" >&2
  exit 2
fi
# comm 可含引号/反斜杠/控制字符（prctl 可设），入 JSON 前清洗
# （与 do_tick 内线程 comm 的清洗纪律一致，meta 行不可漏）
TARGET_COMM=$(cat "$PROC_ROOT/$PID/comm" 2>/dev/null \
  | tr -d '\000-\037\\"' || echo "?")
[ -n "$TARGET_COMM" ] || TARGET_COMM="?"
# 进程启动时间（stat 第 22 字段，去括号后第 20 字段）：--tick 会话身份
# 校验用（PID 复用防护）；读不到时置空（校验降级为仅 PID 比对）
TARGET_STARTTIME=$(awk '{idx=index($0,"("); copy=$0; sub(/^.*\)/,"",copy);
  m=split(copy,f," "); if (m>=20) printf "%d", f[20]}' \
  "$PROC_ROOT/$PID/task/$PID/stat" 2>/dev/null || true)

# ---------------------------------------------------------------- 输出目录
if [ -z "$OUT" ]; then
  OUT="af_$(date -u +%Y%m%d_%H%M%S)"
fi
mkdir -p "$OUT"
SAMPLES="$OUT/samples.jsonl"
TRACE_LOG="$OUT/trace.log"
TRACE_STATE="$OUT/.trace_state.tsv"   # 事件 diff 的持久状态（结束后删除）
TICK_TSV="$OUT/.tick.tsv"             # 单 tick 的线程清单（复用文件）
case "$MODE" in
  run|init)
    # 目录独占：不只查 samples.jsonl——旧的 trace.log/topology_raw.txt
    # 也会被重定向截断、重复 --init 会追加第二个 meta 并重置日志
    if [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then
      die "输出目录非空（$OUT；run/init 模式须为空或不存在，重开新目录）"
    fi
    ;;
  tick)
    [ -f "$SAMPLES" ] || die "--tick 需要已 init 的输出目录（$SAMPLES 不存在）"
    # ---- 会话身份校验：防止换 PID / PID 复用 / 已结束会话继续追加 ----
    meta_line=$(head -n 1 "$SAMPLES")
    case "$meta_line" in *'"meta"'*) ;; *)
      die "samples.jsonl 首行不是 meta 行（$SAMPLES，目录可能损坏）" ;;
    esac
    grep -q '^{"end"' "$SAMPLES" && \
      die "会话已结束（$SAMPLES 已有 end 行；重开新输出目录）"
    meta_pid=$(printf '%s' "$meta_line" | grep -o '"pid":[0-9]*' | head -n 1 | cut -d: -f2)
    [ "$meta_pid" = "$PID" ] || \
      die "--tick 的 PID($PID) 与会话 meta 的 PID($meta_pid) 不一致"
    meta_st=$(printf '%s' "$meta_line" | grep -o '"starttime":[0-9]*' | head -n 1 | cut -d: -f2)
    if [ -n "$meta_st" ] && [ "$meta_st" != "0" ] && [ -n "$TARGET_STARTTIME" ]; then
      [ "$meta_st" = "$TARGET_STARTTIME" ] || \
        die "进程启动时间不一致（meta starttime=$meta_st vs 当前 $TARGET_STARTTIME——PID 可能被复用，重开新目录）"
    fi
    ;;
esac

CLK_TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)
case "$CLK_TCK" in ''|*[!0-9]*) CLK_TCK=100 ;; esac

# ---------------------------------------------------------------- 拓扑快照
collect_topology() {
  {
    echo "=== host"
    uname -s -r 2>/dev/null || echo "unknown"
    [ -r /etc/os-release ] && . /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-}" || true
    echo "=== clk_tck"
    echo "$CLK_TCK"
    echo "=== node_cpulist"    # node|cpulist，每 NUMA 节点一行
    local f node list
    for f in "$SYS_ROOT/devices/system/node/node"*/cpulist; do
      [ -r "$f" ] || continue
      node=${f%/cpulist}; node=${node##*node}
      list=$(cat "$f" 2>/dev/null) || continue
      echo "${node}|${list}"
    done
    echo "=== l3_domains"      # 每行一个 shared_cpu_list（去重）
    local -A seen=()
    for f in "$SYS_ROOT/devices/system/cpu/cpu"[0-9]*/cache/index3/shared_cpu_list; do
      [ -r "$f" ] || continue
      list=$(cat "$f" 2>/dev/null) || continue
      [ -n "$list" ] || continue
      [ -n "${seen[$list]:-}" ] && continue
      seen[$list]=1
      echo "$list"
    done
    echo "=== npu_pci"         # bdf|numa_node（vendor 0x19e5 + class 0x120000
                              # 双校验=昇腾；单查 vendor 会混入 Huawei 网卡 hns3/Hi1822）
    local vf cf bdf node vendor class
    for vf in "$SYS_ROOT/bus/pci/devices/"*/vendor; do
      [ -r "$vf" ] || continue
      read -r vendor < "$vf" 2>/dev/null || continue
      [ "$vendor" = "0x19e5" ] || continue
      bdf=${vf%/vendor}; bdf=${bdf##*/}
      cf="$SYS_ROOT/bus/pci/devices/$bdf/class"
      class=""
      [ -r "$cf" ] && read -r class < "$cf" 2>/dev/null
      case "$class" in 0x120000*) ;; *) continue ;; esac
      node=""
      [ -r "$SYS_ROOT/bus/pci/devices/$bdf/numa_node" ] && \
        read -r node < "$SYS_ROOT/bus/pci/devices/$bdf/numa_node"
      echo "${bdf}|${node:-unknown}"
    done
    echo "=== npu_smi_info_l"
    command -v npu-smi >/dev/null 2>&1 && npu-smi info -l 2>/dev/null || echo "(absent)"
    echo "=== target_view"
    echo "cpuset=$(cat "$PROC_ROOT/$PID/cpuset" 2>/dev/null || echo unreadable)"
    grep -E '^Cpus_allowed_list:' "$PROC_ROOT/$PID/task/$PID/status" 2>/dev/null \
      || echo "main_status_unreadable"
    # 线程库环境变量白名单采集（脱敏）：
    # - 只取绑核诊断所需的具体变量名——前缀匹配会把 KMP_PROFILE_FILE 的
    #   绝对路径、部署方自定义 HCCL_* 配置原值带出诊断包
    # - 值含路径形态（"/"）时只留 basename；超长截断
    echo "=== target_env"
    local kv k v
    while IFS= read -r kv; do
      [ "$kv" = "${kv%%=*}" ] && continue
      k=${kv%%=*}; v=${kv#*=}
      case "$k" in
        OMP_NUM_THREADS|OMP_PROC_BIND|OMP_PLACES|OMP_DYNAMIC|\
        MKL_NUM_THREADS|MKL_DYNAMIC|\
        KMP_AFFINITY|KMP_AFFINITY_PROCS|KMP_AFFINITY_GRANULARITY|\
        OPENBLAS_NUM_THREADS|OPENBLAS_MAIN_FREE|\
        HCCL_CONNECT_TIMEOUT)
          case "$v" in */*) v="${v##*/}" ;; esac
          [ "${#v}" -gt 96 ] && v="${v:0:96}..."
          echo "$k=$v"
          ;;
      esac
    done < <(tr '\0' '\n' 2>/dev/null < "$PROC_ROOT/$PID/environ" || true)
  } > "$OUT/topology_raw.txt"
}

# 拓扑速览一行（日志头部用）：N CPU / M NUMA 节点(...) / NPU:K 台(node...)
topo_summary() {
  local total
  total=$(awk -F'|' '$0 ~ /^[0-9]+\|/ {
    n = split($2, seg, ",")
    for (i = 1; i <= n; i++) {
      if (seg[i] ~ /-/) { split(seg[i], r, "-"); total += r[2] - r[1] + 1 }
      else total++
    }
  } END { printf "%d", total }' "$OUT/topology_raw.txt" 2>/dev/null)
  local nnodes npu
  nnodes=$(grep -cE '^[0-9]+\|' "$OUT/topology_raw.txt" 2>/dev/null || true)
  [ -n "$nnodes" ] || nnodes=0
  npu=$(awk -F'|' '/^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]\|/ {n++} END{printf "%d", n+0}' \
    "$OUT/topology_raw.txt" 2>/dev/null || true)
  [ -n "$npu" ] || npu=0
  local node_detail npu_detail
  node_detail=$(awk -F'|' '$0 ~ /^[0-9]+\|/ { printf " node%s:%s", $1, $2 }' \
    "$OUT/topology_raw.txt" 2>/dev/null)
  # 多 NPU 时自拼逗号分隔（awk printf 无换行 → 单行进 paste 永不插逗号）
  npu_detail=$(awk -F'|' '/^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]\|/ { if (n++) printf ","; printf "%s(node%s)", $1, $2 }' \
    "$OUT/topology_raw.txt" 2>/dev/null || true)
  echo "${total:-?} CPU / ${nnodes:-?} NUMA 节点(${node_detail# }) / NPU:${npu:-0} 台${npu_detail:+(${npu_detail})}"
}

write_meta() {
  # proc_root 可含引号/反斜杠（合法目录名），入 JSON 前转义
  local pr_esc
  pr_esc=$(printf '%s' "$PROC_ROOT" | sed 's/\\/\\\\/g; s/"/\\"/g')
  {
    printf '{"meta":{"skill":"kptools-thread-affinity-collector","version":"%s","pid":%s,"interval":%s,"clk_tck":%s,' \
      "$VERSION" "$PID" "$INTERVAL" "$CLK_TCK"
    printf '"host":"%s","kernel":"%s","started":%s,"proc_root":"%s","auto":%s,"target_comm":"%s","starttime":%s}}\n' \
      "$(uname -n 2>/dev/null || echo '?')" "$(uname -r 2>/dev/null || echo '?')" \
      "$(now)" "$pr_esc" "$([ "$AUTO" -eq 1 ] && echo true || echo false)" \
      "$TARGET_COMM" "${TARGET_STARTTIME:-0}"
  } >> "$SAMPLES"
}

# ---------------------------------------------------------------- 单个 tick
# 线程名(comm)可含空格/括号：按"最后一个 )"切分 stat（procps 同款口径），
# 落点字段为去括号后第 37 个字段（原 stat 第 39 字段 processor）。
do_tick() {  # $1=ts $2=seq  返回 0=采到数据；1=目标消失（已写 absent 行）
  local ts="$1"
  shopt -s nullglob
  local stat_files=( "$PROC_ROOT/$PID/task/"*/stat )
  local status_files=( "$PROC_ROOT/$PID/task/"*/status )
  shopt -u nullglob

  if [ ${#stat_files[@]} -eq 0 ]; then
    printf '{"ts":%s,"seq":%s,"absent_proc":1}\n' "$ts" "$2" >> "$SAMPLES"
    return 1
  fi

  # 文件列表经环境变量传入（/proc 路径无空格），awk 逐文件 getline：
  # 枚举后短命线程退出导致的打开失败只跳过该线程、已读到的其他线程保留
  # ——gawk 对命令行文件打开失败是 fatal error（整 tick 丢弃），必须避开
  local stat_list status_list
  stat_list=$(printf '%s\n' "${stat_files[@]}")
  status_list=$(printf '%s\n' "${status_files[@]}")
  line=$(STAT_LIST="$stat_list" STATUS_LIST="$status_list" \
    awk -v TS="$ts" -v SEQ="$2" -v TSV="$TICK_TSV" '
    function tid_of(path,  nf, fp) { nf = split(path, fp, "/"); return fp[nf-1] }
    BEGIN {
      # 通道 A：status → Cpus_allowed_list（逐文件逐行，读不到就跳过）
      nq = split(ENVIRON["STATUS_LIST"], qarr, "\n")
      for (qi = 1; qi <= nq; qi++) {
        f = qarr[qi]
        if (f == "") continue
        t = tid_of(f)
        while ((getline raw < f) > 0) {
          if (raw ~ /^Cpus_allowed_list:[ \t]/) {
            split(raw, w, " ")
            allowed[t] = w[2]
            break
          }
        }
        close(f)
      }
      # 通道 B：stat → psr/utime/stime（单行文件；行截断=线程退出中→跳过）
      ns = split(ENVIRON["STAT_LIST"], sarr, "\n")
      for (si = 1; si <= ns; si++) {
        f = sarr[si]
        if (f == "") continue
        if ((getline raw < f) <= 0) { close(f); continue }
        close(f)
        idx = index(raw, "(")
        copy = raw
        sub(/^.*\)/, "", copy)               # greedy：截到最后一个 ) 为止
        m = split(copy, fl, " ")
        if (m < 37) continue                  # 行被截断（线程退出中）
        t = tid_of(f)
        comm_len = length(raw) - length(copy) - idx - 1
        comm = substr(raw, idx + 1, comm_len)
        gsub(/[[:cntrl:]\\"]/, "", comm)      # 控制字符/引号/反斜杠不可入 JSON，删除
        tid_comm[t] = comm
        tid_state[t] = fl[1]
        tid_utime[t] = fl[12] + 0
        tid_stime[t] = fl[13] + 0
        tid_psr[t] = fl[37] + 0
        order[++n] = t
      }
      printf "{\"ts\":%s,\"seq\":%s,\"threads\":[", TS, SEQ
      sep = ""
      for (i = 1; i <= n; i++) {
        t = order[i]
        printf "%s{\"tid\":%d,\"comm\":\"%s\",\"state\":\"%s\",\"psr\":%d,\"utime\":%d,\"stime\":%d,\"allowed\":\"%s\"}", \
          sep, t + 0, tid_comm[t], tid_state[t], tid_psr[t], tid_utime[t], tid_stime[t], (t in allowed ? allowed[t] : "?")
        sep = ","
      }
      printf "]}\n"
      # TAB 分隔的线程清单副输出（事件 diff 用；每 tick 覆写）
      print "" > TSV
      for (i = 1; i <= n; i++) {
        t = order[i]
        print t "\t" tid_comm[t] "\t" (t in allowed ? allowed[t] : "?") > TSV
      }
    }
  ' 2>/dev/null) || line=""

  if [ -z "$line" ] || [[ "$line" == *'"threads":[]'* ]]; then
    printf '{"ts":%s,"seq":%s,"absent_proc":1}\n' "$ts" "$2" >> "$SAMPLES"
    return 1
  fi
  printf '%s\n' "$line" >> "$SAMPLES"
  return 0
}

write_end() {  # $1=reason $2=ticks
  printf '{"end":{"ts":%s,"reason":"%s","ticks":%s}}\n' "$(now)" "$1" "$2" >> "$SAMPLES"
}

# ---------------------------------------------------------------- 人类可读事件日志
# 状态文件 TRACE_STATE 格式（TAB 分隔）：
#   首行: #<new>	<exited>	<mask_changes>	<absent_consec>	<absent_total>	<last_event_seq>
#   后续: <tid>	<comm>	<allowed>	<first_seq>	<last_seq>	<miss_count>
# absent 分连续（consec，恢复时清零，驱动 tolerate 与"恢复"事件）与
# 累计（total，恢复不清零，统计行如实反映发生过几次不可见 tick）
declare -A ST_COMM ST_ALLOW ST_FIRST ST_LAST ST_MISS
ST_TIDS=()   # 在册 tid 索引（set -u 下空关联数组的 key 展开有坑，key 列表用普通数组维护）
CNT_NEW=0; CNT_EXIT=0; CNT_MASK=0; CNT_ABSENT_CONSEC=0; CNT_ABSENT_TOTAL=0
LAST_EVENT_SEQ=0

load_state() {
  [ -f "$TRACE_STATE" ] || return 0
  local head rest tid comm allowed first last miss
  { IFS= read -r head; } < "$TRACE_STATE" || return 0
  case "$head" in \#*) ;; *) return 0 ;; esac
  rest=${head#\#}
  IFS=$'\t' read -r CNT_NEW CNT_EXIT CNT_MASK CNT_ABSENT_CONSEC CNT_ABSENT_TOTAL LAST_EVENT_SEQ \
    <<< "$rest" || true
  while IFS=$'\t' read -r tid comm allowed first last miss; do
    [ -n "${tid:-}" ] || continue
    ST_COMM[$tid]=$comm; ST_ALLOW[$tid]=$allowed
    ST_FIRST[$tid]=$first; ST_LAST[$tid]=$last; ST_MISS[$tid]=$miss; ST_TIDS+=("$tid")
  done < <(tail -n +2 "$TRACE_STATE" 2>/dev/null)
}

# 在册线程计数（set -u 下空关联数组的 ${#arr[@]} 会报 unbound，用安全展开）
st_alive_count() {
  local n=0 t
  for t in ${ST_TIDS[@]+"${ST_TIDS[@]}"}; do n=$((n + 1)); done
  echo "$n"
}

save_state() {
  { printf '#\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$CNT_NEW" "$CNT_EXIT" "$CNT_MASK" "$CNT_ABSENT_CONSEC" "$CNT_ABSENT_TOTAL" "$LAST_EVENT_SEQ"
    local tid
    for tid in ${ST_TIDS[@]+"${ST_TIDS[@]}"}; do
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$tid" "${ST_COMM[$tid]}" \
        "${ST_ALLOW[$tid]}" "${ST_FIRST[$tid]}" "${ST_LAST[$tid]}" "${ST_MISS[$tid]:-0}"
    done
  } > "$TRACE_STATE"
}

# 事件 diff：本 tick 线程清单(.tick.tsv) × 状态 → 事件写入 trace.log
# $1=ts $2=seq $3=gone(1=目标不可见)
update_trace_log() {
  local ts="$1" seq="$2" gone="$3" tid comm allowed t events=0
  local hh; hh=$(fmt_ts "$ts")
  if [ "$gone" = "1" ]; then
    if [ "$CNT_ABSENT_CONSEC" -eq 0 ]; then
      printf '[%s] #%s ! 目标进程不可见（开始计数，连续 %s 次判定结束）\n' \
        "$hh" "$seq" "$TOLERATE" >> "$TRACE_LOG"
    fi
    CNT_ABSENT_CONSEC=$((CNT_ABSENT_CONSEC + 1))
    CNT_ABSENT_TOTAL=$((CNT_ABSENT_TOTAL + 1))
    events=1
    save_state
    return 0
  fi
  # 恢复：只清零连续计数——累计数保留（统计行须如实反映发生过不可见）
  if [ "$CNT_ABSENT_CONSEC" -gt 0 ]; then
    printf '[%s] #%s 目标进程已恢复（此前不可见 %s tick，累计 %s tick）\n' \
      "$hh" "$seq" "$CNT_ABSENT_CONSEC" "$CNT_ABSENT_TOTAL" >> "$TRACE_LOG"
    CNT_ABSENT_CONSEC=0; events=1
  fi
  local -A seen_now=()
  while IFS=$'\t' read -r tid comm allowed; do
    [ -n "$tid" ] || continue
    seen_now[$tid]=1
    if [ -z "${ST_COMM[$tid]:-}" ]; then
      printf '[%s] #%s + 新线程 tid=%s (%s) 允许核=%s\n' \
        "$hh" "$seq" "$tid" "$comm" "$allowed" >> "$TRACE_LOG"
      ST_COMM[$tid]=$comm; ST_ALLOW[$tid]=$allowed
      ST_FIRST[$tid]=$seq; ST_MISS[$tid]=0; ST_TIDS+=("$tid")
      CNT_NEW=$((CNT_NEW + 1)); events=1
    else
      if [ "${ST_ALLOW[$tid]}" != "$allowed" ]; then
        printf '[%s] #%s 掩码变化 tid=%s (%s): %s → %s\n' \
          "$hh" "$seq" "$tid" "${ST_COMM[$tid]}" "${ST_ALLOW[$tid]}" "$allowed" \
          >> "$TRACE_LOG"
        ST_ALLOW[$tid]=$allowed
        CNT_MASK=$((CNT_MASK + 1)); events=1
      fi
      ST_MISS[$tid]=0
    fi
    ST_LAST[$tid]=$seq
  done < "$TICK_TSV"
  # 本 tick 未出现的线程：连续 3 次缺失判定退出（事件日志口径，与
  # analyzer 的实例判定独立）
  for tid in ${ST_TIDS[@]+"${ST_TIDS[@]}"}; do
    [ -n "${seen_now[$tid]:-}" ] && continue
    ST_MISS[$tid]=$(( ${ST_MISS[$tid]:-0} + 1 ))
    if [ "${ST_MISS[$tid]}" -ge 3 ]; then
      printf '[%s] #%s - 线程退出 tid=%s (%s)（在册 #%s-#%s）\n' \
        "$hh" "$seq" "$tid" "${ST_COMM[$tid]}" "${ST_FIRST[$tid]}" "${ST_LAST[$tid]}" \
        >> "$TRACE_LOG"
      unset "ST_COMM[$tid]" "ST_ALLOW[$tid]" "ST_FIRST[$tid]" "ST_LAST[$tid]" "ST_MISS[$tid]"
      local _keep=()
      for _t in ${ST_TIDS[@]+"${ST_TIDS[@]}"}; do
        [ "$_t" != "$tid" ] && _keep+=("$_t")
      done
      ST_TIDS=("${_keep[@]+"${_keep[@]}"}")
      CNT_EXIT=$((CNT_EXIT + 1)); events=1
    fi
  done
  # 心跳：每 50 tick 且本 tick 无事件 → 报平安（证明采集活着）
  if [ "$events" -eq 0 ] && [ $((seq % 50)) -eq 0 ]; then
    printf '[%s] #%s 心跳：%s 线程在册，无变化\n' \
      "$hh" "$seq" "$(st_alive_count)" >> "$TRACE_LOG"
  fi
  [ "$events" -eq 1 ] && LAST_EVENT_SEQ=$seq
  save_state
}

# 结尾：终态名册 + 统计（run 模式结束 / Ctrl-C 时由 finalize_trace 调用）
finalize_trace() {  # $1=reason $2=ticks $3=duration_s
  local hh tid first n_live
  hh=$(fmt_ts "$(now)")
  n_live=$(st_alive_count)
  {
    printf '[%s] 结束：原因 %s，共 %s tick\n' "$hh" "$1" "$2"
    printf '\n==== 线程终态名册（%s 个线程仍在册）====\n' "$n_live"
    printf '%-8s %-18s %-12s %s\n' "tid" "comm" "允许核" "在册区间"
    for tid in $(printf "%s\n" ${ST_TIDS[@]+"${ST_TIDS[@]}"} | sort -n); do
      printf '%-8s %-18s %-12s %s\n' "$tid" "${ST_COMM[$tid]}" \
        "${ST_ALLOW[$tid]}" "#${ST_FIRST[$tid]}-#${ST_LAST[$tid]}"
    done
    printf '\n==== 统计 ====\n'
    printf '新线程 %s / 线程退出 %s / 掩码变化 %s / 目标不可见 %s tick\n' \
      "$CNT_NEW" "$CNT_EXIT" "$CNT_MASK" "$CNT_ABSENT_TOTAL"
    printf '\n数据文件：samples.jsonl（机器格式，含每线程落点直方图数据）\n'
    printf '完整判读与 HTML 报告：python3 .../kptools-thread-affinity-analyzer/scripts/analyze.py %s\n' \
      "$(cd "$OUT" 2>/dev/null && pwd || echo '<本目录>')"
  } >> "$TRACE_LOG"
  rm -f "$TRACE_STATE" "$TICK_TSV"
}

write_trace_header() {
  {
    printf '==== kptools-thread-affinity-collector 线程绑核采集日志 ====\n'
    printf '目标    : pid=%s (%s)\n' "$PID" "$TARGET_COMM"
    printf '主机    : %s 内核 %s\n' "$(uname -n 2>/dev/null || echo '?')" \
      "$(uname -r 2>/dev/null || echo '?')"
    printf '拓扑    : %s\n' "$(topo_summary)"
    printf '口径    : 间隔 %ss；允许掩码=配置面 / 落点=实际面（处理器快照，迁移数为下界）\n' "$INTERVAL"
    printf '数据    : samples.jsonl(机器格式) + topology_raw.txt(拓扑) + 本文件(人类可读事件)\n'
    printf '======================================================================\n'
  } > "$TRACE_LOG"
}

# ---------------------------------------------------------------- 模式分发
case "$MODE" in
  init)
    write_meta
    collect_topology
    write_trace_header
    CNT_NEW=0; CNT_EXIT=0; CNT_MASK=0; CNT_ABSENT_CONSEC=0; CNT_ABSENT_TOTAL=0; LAST_EVENT_SEQ=0
    save_state
    echo "init done: $SAMPLES + topology_raw.txt + trace.log"
    exit 0
    ;;
  tick)
    SEQ=$(grep -c '"seq":' "$SAMPLES" || true); SEQ=$((SEQ + 1))
    load_state
    TS=$(now)
    if do_tick "$TS" "$SEQ"; then
      update_trace_log "$TS" "$SEQ" 0
      [ "$QUIET" -eq 0 ] && echo "tick seq=$SEQ done"
    else
      update_trace_log "$TS" "$SEQ" 1
      echo "ERROR: TARGET_MISSING pid=$PID（tick 时目标已消失，absent 行已记录）" >&2
      exit 3
    fi
    exit 0
    ;;
esac

# ---------------------------------------------------------------- run 守护
write_meta
collect_topology
write_trace_header
CNT_NEW=0; CNT_EXIT=0; CNT_MASK=0; CNT_ABSENT_CONSEC=0; CNT_ABSENT_TOTAL=0; LAST_EVENT_SEQ=0
save_state

SEQ=0
GONE=0
T0=$(now)
END_WRITTEN=0
REASON=""

on_signal() {
  [ "$END_WRITTEN" -eq 0 ] && { write_end signal "$SEQ"; END_WRITTEN=1; }
  finalize_trace "signal(中断)" "$SEQ" 0
  echo "interrupted: ticks=$SEQ, data kept in $SAMPLES" >&2
  exit 130
}
trap on_signal INT TERM

while :; do
  TS=$(now)
  SEQ=$((SEQ + 1))
  if do_tick "$TS" "$SEQ"; then
    GONE=0
    update_trace_log "$TS" "$SEQ" 0
    if [ "$QUIET" -eq 0 ] && [ $((SEQ % 50)) -eq 0 ]; then
      echo "tick $SEQ ts=$TS"
    fi
  else
    GONE=$((GONE + 1))
    echo "target gone ($GONE/$TOLERATE)" >&2
    update_trace_log "$TS" "$SEQ" 1
    if [ "$GONE" -ge "$TOLERATE" ]; then
      REASON="target_gone"
      break
    fi
  fi
  if [ "$DURATION" != "0" ]; then
    if awk -v a="$T0" -v b="$TS" -v d="$DURATION" 'BEGIN{exit !(b-a>=d)}'; then
      REASON="timeout"
      break
    fi
  fi
  sleep "$INTERVAL"
done

write_end "$REASON" "$SEQ"
END_WRITTEN=1
finalize_trace "$REASON" "$SEQ" 0
echo "done: reason=$REASON ticks=$SEQ out=$SAMPLES"
