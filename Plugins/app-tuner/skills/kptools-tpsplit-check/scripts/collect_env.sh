#!/usr/bin/env bash
# collect_env.sh — 只读采集 vLLM-Ascend 部署的显存事实，输出 KEY=VALUE（供 analyze_feasibility.py）
#
# 用法:
#   bash collect_env.sh --log <vllm启动日志> [--model <模型目录>] \
#                       [--script <vllm启动脚本>] [--docker <容器名>] [--npu <卡号，默认0>]
#
# 采集位置约定（踩坑结论，别改回去）:
#   - npu-smi / ps 一律在宿主机执行：容器内 npu-smi 常返回空，单卡显存会全缺
#   - 宿主机 ps 能看到所有容器的进程，参数可能拿错实例（TP16/别人的 max_model_len）
#     → 有启动脚本就给 --script（支持脚本内 $var 展开），ps 仅兜底并标记 PARAM_SOURCE=ps
#   - --log/--model/--script 先按宿主机本地路径解析；文件不存在且给了 --docker 时，
#     自动改用 docker exec 在容器里读（采集本身仍在宿主机做）
#
# 日志格式双兼容: vLLM 标准（Model weights take / Available KV cache / GPU KV cache size）
# 与 vLLM-Ascend（Actual usage: X GiB for weights / for kv cache / Desired usage / Total usage）。
#
# 只做读取: npu-smi / ps / docker exec cat|grep|ls / config.json 检查。
# 不改配置、不重启、不写目标目录。首次使用先跑: bash -n collect_env.sh 查语法。
# 注意: npu-smi 某些字段可能需要 root；老卡 -t usages 可能只有 HBM Usage Rate(%)，
# 此时输出 NPU<i>_HBM_RATE_PCT（无 MB 值），分析脚本会走日志口径并标注。

set -u
set -f  # 禁用 glob 展开：解析 vllm 命令行按词拆分时，参数里的 * 不能被 shell 展开成文件名

LOG=""; MODEL=""; SCRIPT=""; DOCKER=""; NPU_ID=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --log)    LOG="$2"; shift 2 ;;
    --model)  MODEL="$2"; shift 2 ;;
    --script) SCRIPT="$2"; shift 2 ;;
    --docker) DOCKER="$2"; shift 2 ;;
    --npu)    NPU_ID="$2"; shift 2 ;;
    *) echo "collect_env.sh: unknown arg: $1 (expect --log/--model/--script/--docker/--npu)" >&2; shift ;;
  esac
done

echo "HOST_NAME=$(hostname 2>/dev/null || echo unknown)"
echo "COLLECT_TIME=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown)"

# ---------- 通用读文件工具（本地优先，--docker 时容器兜底） ----------
read_file() { # read_file <path> → 内容到 stdout
  if [[ -n "$1" && -f "$1" ]]; then cat "$1" 2>/dev/null
  elif [[ -n "$1" && -n "$DOCKER" ]]; then docker exec "$DOCKER" cat "$1" 2>/dev/null
  fi
}
file_exists() { # file_exists <path> → 返回0表示存在
  if [[ -n "$1" && -f "$1" ]]; then return 0; fi
  if [[ -n "$1" && -n "$DOCKER" ]]; then docker exec "$DOCKER" test -f "$1" 2>/dev/null; return $?; fi
  return 1
}
loggrep() { # loggrep <ERE> → 首个匹配行（本地文件或容器内 grep）
  if [[ -n "$LOG" && -f "$LOG" ]]; then grep -m1 -E "$1" "$LOG" 2>/dev/null
  elif [[ -n "$LOG" && -n "$DOCKER" ]]; then docker exec "$DOCKER" grep -m1 -E "$1" "$LOG" 2>/dev/null
  fi
}
loggrepo() { # loggrepo <ERE> → 首个匹配段（-o 只回显匹配本身；本地或容器）
  if [[ -n "$LOG" && -f "$LOG" ]]; then grep -m1 -oE "$1" "$LOG" 2>/dev/null
  elif [[ -n "$LOG" && -n "$DOCKER" ]]; then docker exec "$DOCKER" grep -m1 -oE "$1" "$LOG" 2>/dev/null
  fi
}
first_num() { printf '%s\n' "$1" | grep -oE '[0-9]+\.?[0-9]*' | head -n 1; }

# ---------- 1) npu-smi 单卡显存（宿主机执行；主表优先 + usages + rate 兜底） ----------
if command -v npu-smi >/dev/null 2>&1; then
  HBM_USED=""; HBM_TOTAL=""; HBM_RATE=""

  # 1a) 主表 npu-smi info：目标卡所在行，取行内 "used / total" 数字对
  #     兼容两种表头：NPU id 在首列（$1）或被竖线分隔（$2，常见 "| 0 ..." 行）；
  #     （总量 >= 1024MB 才可信，防止把别的字段当成显存）
  TBL="$(npu-smi info 2>/dev/null || true)"
  if [[ -n "$TBL" && -z "$HBM_TOTAL" ]]; then
    ROW="$(printf '%s\n' "$TBL" | awk -v nid="$NPU_ID" '$1 == nid || $2 == nid' | head -n 1)"
    PAIR="$(printf '%s\n' "$ROW" | grep -oE '[0-9]+[[:space:]]*/[[:space:]]*[0-9]+' | head -n 1)"
    if [[ "$PAIR" == *"/"* ]]; then
      a="$(printf '%s' "$PAIR" | cut -d/ -f1 | tr -d '[:space:]')"
      b="$(printf '%s' "$PAIR" | cut -d/ -f2 | tr -d '[:space:]')"
      if [[ "$b" =~ ^[0-9]+$ ]] && (( b >= 1024 )); then
        HBM_USED="$a"; HBM_TOTAL="$b"
      fi
    fi
  fi

  # 1b) -t usages 的 key-value 块：HBM-Usage(MB) : used / total
  #     Rate(%) 行不算 MB（防止把 78% 误当 78MB）；单个数字不认，总量过小不认
  USG="$(npu-smi info -t usages 2>/dev/null || true)"
  if [[ -n "$USG" ]]; then
    PARSED="$(printf '%s\n' "$USG" | awk -v nid="$NPU_ID" '
      /^NPU[ _]ID/ { cur = $NF }
      cur == nid && /HBM/ {
        israte = ($0 ~ /Rate/)
        line = $0; gsub(/[^0-9 ]/, " ", line)
        n = split(line, a, / +/); k = 0
        for (i = 1; i <= n; i++) if (a[i] ~ /^[0-9]+$/) vals[++k] = a[i]
        if (!israte && k >= 2 && vals[k] + 0 >= 1024) { print "USED=" vals[1]; print "TOTAL=" vals[k]; exit }
        if (israte && k >= 1) { print "RATE=" vals[k] }
      }
    ')"
    if [[ -z "$HBM_TOTAL" ]]; then
      HBM_USED="$(printf '%s\n' "$PARSED" | sed -n 's/^USED=//p' | head -n 1)"
      HBM_TOTAL="$(printf '%s\n' "$PARSED" | sed -n 's/^TOTAL=//p' | head -n 1)"
    fi
    if [[ -z "$HBM_RATE" ]]; then
      HBM_RATE="$(printf '%s\n' "$PARSED" | sed -n 's/^RATE=//p' | head -n 1)"
    fi
    echo "NPU_LOCAL_COUNT=$(printf '%s\n' "$USG" | grep -cE '^NPU[ _]ID' || true)"
  fi

  [[ -n "$HBM_USED" ]]  && echo "NPU${NPU_ID}_USED_MB=$HBM_USED"
  [[ -n "$HBM_TOTAL" ]] && echo "NPU${NPU_ID}_TOTAL_MB=$HBM_TOTAL"
  # 老卡兜底：只有速率没有 MB 时输出百分比（分析脚本用它做现状参考，可用口径走日志）
  [[ -n "$HBM_RATE" && -z "$HBM_TOTAL" ]] && echo "NPU${NPU_ID}_HBM_RATE_PCT=$HBM_RATE"
  # 与卡号无关的别名键：分析端按 NPU_<名> 读取，用 --npu 采别的卡时不会丢单卡显存事实
  [[ -n "$HBM_USED" ]]  && echo "NPU_USED_MB=$HBM_USED"
  [[ -n "$HBM_TOTAL" ]] && echo "NPU_TOTAL_MB=$HBM_TOTAL"
  [[ -n "$HBM_RATE" && -z "$HBM_TOTAL" ]] && echo "NPU_HBM_RATE_PCT=$HBM_RATE"
else
  echo "# collect_env.sh: npu-smi not found" >&2
fi

# ---------- 2) vLLM 参数：--script 优先（宿主机 ps 会看到所有容器进程），ps 兜底 ----------
CMDLINE=""; PARAM_SOURCE="none"; SCRIPT_RAW=""

resolve_var() { # resolve_var <name> → 从脚本赋值行取值（export/成对单双引号/行尾注释都处理）
  local name="$1"
  printf '%s\n' "$SCRIPT_RAW" | tr -d '\r' | awk -v n="$name" -v q="'" -v dq="\"" '
    index($0, n "=") > 0 {
      line = $0
      head = line
      sub(/^[[:space:]]*export[[:space:]]+/, "", head)
      if (head !~ ("^[[:space:]]*" n "[[:space:]]*=")) next
      pos = index(line, "=")
      val = substr(line, pos + 1)
      sub(/^[[:space:]]+/, "", val)
      ci = index(val, "#")
      if (ci > 0) val = substr(val, 1, ci - 1)                   # 去行尾注释
      sub(/[[:space:]]+$/, "", val)
      # 剥成对引号：单引号与双引号都要剥（shellcheck 推荐写法 TP="32" / TP='"'"'32'"'"'，
      # 只剥单引号会让 TP_SIZE 带引号落盘、分析端 _int 返回 None 被当缺失）
      L = length(val)
      if (L >= 2 && ((substr(val, 1, 1) == q && substr(val, L, 1) == q) ||
                     (substr(val, 1, 1) == dq && substr(val, L, 1) == dq)))
        val = substr(val, 2, L - 2)
      sub(/[[:space:]]+$/, "", val)
      print val; exit
    }'
}
resolve_vars() { # resolve_vars <字符串> → 展开其中所有 $VAR / ${VAR} / ${VAR:-默认}（最多 20 层）
  local s="$1" sub vn vv n=0
  while printf '%s' "$s" | grep -q '[$]' && (( n < 20 )); do
    # 先试 ${VAR:-default} 形式（脚本常见）：有赋值用赋值，没有用默认值
    sub="$(printf '%s' "$s" | grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*:-[^}]*\}' | head -n 1)"
    if [[ -n "$sub" ]]; then
      vn="$(printf '%s' "$sub" | sed -e 's/^\${//' -e 's/:-.*$//')"
      vv="$(resolve_var "$vn")"
      if [[ -z "$vv" ]]; then
        vv="${sub#*:-}"
        vv="${vv%\}}"
      fi
      s="${s//"$sub"/"$vv"}"
      n=$((n + 1))
      continue
    fi
    sub="$(printf '%s' "$s" | grep -oE '\$\{?[A-Za-z_][A-Za-z0-9_]*\}?' | head -n 1)"
    [[ -z "$sub" ]] && break
    vn="$(printf '%s' "$sub" | sed -e 's/^\$//' -e 's/^{//' -e 's/}$//')"
    vv="$(resolve_var "$vn")"
    if [[ -z "$vv" ]]; then
      echo "# collect_env.sh: unresolved \$${vn} in serve line (kept literal)" >&2
      break
    fi
    s="${s//"$sub"/"$vv"}"
    n=$((n + 1))
  done
  printf '%s' "$s"
}

if [[ -n "$SCRIPT" ]]; then
  SCRIPT_RAW="$(read_file "$SCRIPT")"
  if [[ -n "$SCRIPT_RAW" ]]; then
    # 去注释、拼反斜杠续行、压成一行，再截出 vllm serve 命令段
    JOINED="$(printf '%s\n' "$SCRIPT_RAW" | tr -d '\r' \
      | sed -e 's/^[[:space:]]*#.*$//' -e 's/[[:space:]]#.*$//' -e 's/\\[[:space:]]*$//' \
      | tr '\n' ' ' | tr -s ' ')"
    SERVE_LINE="$(printf '%s\n' "$JOINED" \
      | grep -oE '(vllm[[:space:]]+serve|vllm\.entrypoints\.[A-Za-z.]+)[^;&|<>]*' | tail -n 1)"
    if [[ -n "$SERVE_LINE" ]]; then
      CMDLINE="$(resolve_vars "$SERVE_LINE")"
      PARAM_SOURCE="script"
    else
      echo "# collect_env.sh: no vllm serve line found in --script" >&2
    fi
  else
    echo "# collect_env.sh: --script unreadable (local & in container): $SCRIPT" >&2
  fi
fi
if [[ -z "$CMDLINE" ]]; then
  CMDLINE="$(ps -eo args 2>/dev/null | grep -E 'vllm[[:space:]]+serve|vllm\.entrypoints' | grep -v grep | head -n 1 || true)"
  [[ -n "$CMDLINE" ]] && PARAM_SOURCE="ps"
fi
echo "PARAM_SOURCE=$PARAM_SOURCE"

sanitize_args() { # 只保留测算所需参数（模型目录/TP/DP/PP/util/max-len/kv-dtype/EP），剔除 --api-key 等敏感与无关项
  printf '%s\n' $CMDLINE | awk '
    BEGIN { keep_next = 0; skip_val = 0; got_model = 0 }
    {
      w = $0
      if (w == "vllm" || w == "serve") { printf "%s ", w; next }
      if (skip_val && w !~ /^-/) { skip_val = 0; next }   # 吃掉被丢参数的值（值不以 - 开头才算）
      if (keep_next) { printf "%s ", w; keep_next = 0; next }
      if (w ~ /^--/) {
        skip_val = 0
        key = w; sub(/=.*/, "", key)
        if (key == "--enable-expert-parallel") { printf "%s ", w; next }
        if (key == "--tensor-parallel-size" || key == "--data-parallel-size" || \
            key == "--pipeline-parallel-size" || key == "--gpu-memory-utilization" || \
            key == "--max-model-len" || key == "--kv-cache-dtype") {
          printf "%s ", w
          if (w !~ /=/) keep_next = 1     # 值在下一词
        } else if (w !~ /=/) {
          skip_val = 1                     # 非白名单参数：其值（下一词）一并丢弃
        }
        next
      }
      if (!got_model && w != "") {
      printf "%s ", w
      if (w ~ /^"/ && w !~ /"$/) {          # 带引号多词路径：拼到闭合，VLLM_ARGS 展示完整
        while ((getline w2) > 0) { printf "%s ", w2; if (w2 ~ /"$/) break }
      }
      got_model = 1
      next
    }
    }' | sed -e 's/[[:space:]]*$//'
}

if [[ -n "$CMDLINE" ]]; then
  echo "VLLM_ARGS=$(sanitize_args)"
  # 从命令行词表按引号拼接取值：模型路径含空格（--model "/data/my model"、
  # serve "/data/my model"）时不被 IFS 拆词丢值
  join_val() { # join_val <起始下标>（用数组 _w）→ 拼接带引号多词值并剥成对引号
    local v="${_w[$1]:-}" j
    if [[ "$v" == \"* && "$v" != *\" ]] || [[ "$v" == \'* && "$v" != *\' ]]; then
      for ((j = $1 + 1; j < ${#_w[@]}; j++)); do
        v+=" ${_w[j]}"
        case "${_w[j]}" in *\"|*\') break ;; esac
      done
    fi
    case "$v" in
      \"*\") v="${v#\"}"; v="${v%\"}" ;;
      \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
    printf '%s' "$v"
  }
  val_of() { # val_of <长参数名(不含--)> → 值；--name=v 与 --name v；支持带引号多词值
    local name="$1" i v=""
    read -r -a _w <<< "$CMDLINE"
    for ((i = 0; i < ${#_w[@]}; i++)); do
      if [[ "${_w[i]}" == "--${name}="* ]]; then
        v="${_w[i]#*=}"
        break
      elif [[ "${_w[i]}" == "--${name}" ]]; then
        v="$(join_val $((i + 1)))"
        break
      fi
    done
    printf '%s' "$v"
  }
  echo "TP_SIZE=$(val_of tensor-parallel-size)"
  echo "DP_SIZE=$(val_of data-parallel-size)"
  echo "PP_SIZE=$(val_of pipeline-parallel-size)"
  echo "GPU_MEMORY_UTILIZATION=$(val_of gpu-memory-utilization)"
  echo "MAX_MODEL_LEN=$(val_of max-model-len)"
  echo "KV_CACHE_DTYPE=$(val_of kv-cache-dtype)"
  # 位置参数模型目录: "vllm serve <model_dir> ..."（--model 没给时用）。
  # 提取须按词表+引号拼接（模型路径含空格/带引号都正确）；serve 形式是位置参数，
  # vllm.entrypoints 形式的模型在 --model <路径>
  read -r -a _w <<< "$CMDLINE"
  POS_MODEL=""
  for ((i = 0; i < ${#_w[@]}; i++)); do
    if [[ "${_w[i]}" == "serve" ]]; then
      POS_MODEL="$(join_val $((i + 1)))"
      break
    fi
  done
  if [[ -z "$POS_MODEL" ]]; then
    POS_MODEL="$(val_of model)"
  fi
  if [[ -z "$MODEL" ]]; then MODEL="$POS_MODEL"; fi
  if printf '%s\n' $CMDLINE | grep -qx -- '--enable-expert-parallel'; then
    echo "EP_ENABLED=1"
    # EP 世界大小 = 实例 TP×DP（vLLM --enable-expert-parallel 语义）；拿不到数字时输出空，
    # 分析端走「缺 DP_SIZE 无法推断」告警而不是拍默认值
    EP_TP="$(val_of tensor-parallel-size)"; EP_DP="$(val_of data-parallel-size)"
    if [[ "$EP_TP" =~ ^[0-9]+$ && "$EP_DP" =~ ^[0-9]+$ ]]; then
      echo "EP_GROUP_SIZE=$((EP_TP * EP_DP))"
    else
      echo "EP_GROUP_SIZE="
    fi
  else
    echo "EP_ENABLED=0"
  fi
else
  echo "# collect_env.sh: no vllm cmdline from --script or ps" >&2
fi

# ---------- 3) 模型目录检查（本地或容器内，只读） ----------
if [[ -n "$MODEL" ]]; then
  echo "MODEL_DIR=$MODEL"
  if file_exists "$MODEL/config.json"; then
    echo "CONFIG_EXISTS=1"
    CFG="$(read_file "$MODEL/config.json")"
    for key in num_hidden_layers kv_lora_rank n_routed_experts num_experts hidden_size; do
      v="$(printf '%s\n' "$CFG" | grep -oE "\"${key}\"[[:space:]]*:[[:space:]]*[0-9]+" | head -n 1 | grep -oE '[0-9]+' | tail -n 1)"
      [[ -n "$v" ]] && echo "CONFIG_$(echo "$key" | tr 'a-z' 'A-Z')=$v"
    done
    if [[ -d "$MODEL" ]]; then
      echo "SAFETENSORS_COUNT=$(find "$MODEL" -maxdepth 1 -name '*.safetensors' 2>/dev/null | wc -l | tr -d ' ')"
    elif [[ -n "$DOCKER" ]]; then
      # 直接 docker exec find、路径作为独立参数传递：模型目录含引号/特殊字符时不会拼出可被执行的命令
      echo "SAFETENSORS_COUNT=$(docker exec "$DOCKER" find "$MODEL" -maxdepth 1 -name '*.safetensors' 2>/dev/null | wc -l | tr -d ' ')"
    fi
  else
    echo "CONFIG_EXISTS=0"
  fi
else
  echo "MODEL_DIR="
  echo "CONFIG_EXISTS=0"
fi

# ---------- 4) vLLM 启动日志显存 profiling（双格式，本地或容器内 grep） ----------
if [[ -n "$LOG" ]]; then
  LOG_OK=0
  [[ -f "$LOG" ]] && LOG_OK=1
  if [[ $LOG_OK -eq 0 && -n "$DOCKER" ]]; then
    docker exec "$DOCKER" test -f "$LOG" 2>/dev/null && LOG_OK=2
  fi
  if [[ $LOG_OK -ne 0 ]]; then
    gnum() { # gnum <含数字的ERE> → 匹配段内第一个数字（时间戳在短语前，被 -o 匹配段排除）
      local m
      m="$(loggrepo "$1")"
      [[ -n "$m" ]] && first_num "$m"
    }
    # 每卡权重: vLLM "Model weights take X GiB" 或 vLLM-Ascend "Actual usage: X GiB for weights"
    v="$(gnum 'Model weights take [0-9]+\.?[0-9]*|Actual usage: [0-9]+\.?[0-9]* GiB for weights')"
    [[ -n "$v" ]] && echo "LOG_MODEL_WEIGHTS_GIB=$v"
    # KV 池: vLLM "Available KV cache(memory): X GiB" 或 Ascend "Actual usage: X GiB for kv cache"
    v="$(gnum 'Available KV cache( memory)?: [0-9]+\.?[0-9]*|Actual usage: [0-9]+\.?[0-9]* GiB for [Kk][Vv][ -]?[Cc]ache')"
    [[ -n "$v" ]] && echo "LOG_AVAILABLE_KV_GIB=$v"
    # 可用总预算（最可信口径）: Ascend "Desired usage: X GiB for total"
    v="$(gnum 'Desired usage: [0-9]+\.?[0-9]* GiB for [Tt]otal')"
    [[ -n "$v" ]] && echo "LOG_DESIRED_TOTAL_GIB=$v"
    # 实际总占用: Ascend "Total usage: X GiB"（用于反推非 KV 开销）
    v="$(gnum 'Total usage: [0-9]+\.?[0-9]*')"
    [[ -n "$v" ]] && echo "LOG_ACTUAL_TOTAL_GIB=$v"
    # KV token 数: "GPU/NPU KV cache size: N tokens"（按字段提取，防止行首时间戳被当成数字）
    m="$(loggrepo '(GPU|NPU) KV cache size: [0-9,]+')"
    v="$(printf '%s\n' "$m" | tr -d ',' | grep -oE '[0-9]+' | tail -n 1)"
    [[ -n "$v" ]] && echo "LOG_KV_TOKENS=$v"
    v="$(gnum 'Non-?[Tt]orch (memory|mem)[^0-9]*[0-9]+\.?[0-9]*')"
    [[ -n "$v" ]] && echo "LOG_NON_TORCH_GIB=$v"
    v="$(gnum 'torch peak memory[^0-9]*[0-9]+\.?[0-9]*')"
    [[ -n "$v" ]] && echo "LOG_TORCH_PEAK_GIB=$v"
    # Maximum concurrency for X tokens per request: N.x → 提取冒号后的完整小数（如 2.50，不是 50）
    m="$(loggrepo 'Maximum concurrency for [0-9,]+ tokens per request: [0-9]+\.?[0-9]*x?')"
    v="$(printf '%s\n' "$m" | sed -n 's/.*:[[:space:]]*\([0-9][0-9.]*\)x\?$/\1/p')"
    [[ -n "$v" ]] && echo "LOG_MAX_CONCURRENCY=$v"
  else
    echo "# collect_env.sh: --log not found locally nor in container: $LOG" >&2
  fi
else
  echo "# collect_env.sh: --log not given" >&2
fi

echo "# collect_env.sh done (read-only)"
