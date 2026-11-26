#!/usr/bin/env bash
# probe_mooncake.sh — 在目标容器内运行：探测现役 mooncake 版本、.so、加载路径、构建工具链
# 用法（宿主机）: docker exec -i <容器名> bash -s < probe_mooncake.sh
#                 （-i 必带：bash -s 从 stdin 读脚本，无 -i 时重定向传不进容器、
#                   bash 读到 EOF 即退出、宿主机拿不到探测结果；
#                   以收到完整的 PROBE_JSON_BEGIN/END 摘要为成功判据）
# 输出: 人读文本 + 末尾 JSON 摘要（供部署决策消费）
set -u

section() { echo; echo "===== $* ====="; }

section "1. Python 包与版本 (pip)"
pip list 2>/dev/null | grep -i mooncake || echo "pip: no mooncake packages"
pip show mooncake-store mooncake-engine mooncake 2>/dev/null | grep -E "^(Name|Version|Location):" || true

section "2. import 实际加载路径"
for mod in mooncake.store mooncake.engine mooncake; do
  python -c "import $mod as m; print('$mod ->', getattr(m, '__file__', '?'))" 2>/dev/null \
    || echo "$mod: import failed"
done

# **一次 find 全根扫描、缓存结果复用**（原 4 趟 find 每趟 10-60s，无缓存则总耗 1-4min，
# 且容器 IO 慢时可能挂起；合并为一次并加 30s 超时兜底）
ALL_SO_CACHE=$(timeout 30 find / \( -name "*mooncake*.so*" -o -name "libstore_c*.so*" \) -not -path "/proc/*" -not -path "/sys/*" -printf '%p -> %l\n' 2>/dev/null | sed 's/ -> $//' || echo "FIND_TIMEOUT_OR_ERROR")

section "3. mooncake 相关 .so 文件（含软链指向）"
echo "$ALL_SO_CACHE" | head -30

section "4. 引擎进程实际 mmap 的 mooncake 库 (最权威)"
# 找 vLLM 引擎进程: EngineCore / vllm / ray
# head -5 截断：多引擎进程场景可能截断（engine_pids_truncated 见 JSON 摘要），
# 真正持有 mmap 的进程被截掉时按第 3 节 find 结果 + JSON 的 so_files 兜底
ENG_PIDS_RAW=$(ps -eo pid,cmd 2>/dev/null | grep -E "EngineCore|vllm|raylet" | grep -v grep | awk '{print $1}')
ENG_COUNT=$(echo "$ENG_PIDS_RAW" | grep -c . || true)
ENG_PIDS=$(echo "$ENG_PIDS_RAW" | head -5)
ENGINE_PIDS_TRUNCATED=false
[ "$ENG_COUNT" -gt 5 ] && ENGINE_PIDS_TRUNCATED=true
if [ -z "$ENG_PIDS" ]; then
  echo "未发现运行中的引擎进程（可能 stopped）；改用第 3 节 find 结果定位"
else
  for pid in $ENG_PIDS; do
    echo "--- pid=$pid ($(cat /proc/$pid/comm 2>/dev/null)) ---"
    grep -i mooncake /proc/$pid/maps 2>/dev/null | awk '{print $NF}' | sort -u
  done
fi

section "5. 主 .so 版本串"
# 对第 3/4 节找到的每个 .so 提版本线索
for so in $(find / -name "*mooncake*.so*" -not -path "/proc/*" -not -path "/sys/*" 2>/dev/null | sort -u | head -10); do
  v=$(strings "$so" 2>/dev/null | grep -iE "^v?[0-9]+\.[0-9]+\.[0-9]+" | head -3 | tr '\n' ' ')
  g=$(strings "$so" 2>/dev/null | grep -iE "git (commit|hash|sha)|g[0-9a-f]{7,}" | head -2 | tr '\n' ' ')
  echo "$so | ver=[$v] git=[$g]"
done

section "6. 构建工具链"
for t in git cmake g++ gcc make; do
  command -v $t >/dev/null 2>&1 && echo "$t: $($t --version 2>/dev/null | head -1)" || echo "$t: MISSING"
done

section "7. RDMA / 传输依赖"
ldconfig -p 2>/dev/null | grep -E "ibverbs|rdmacm|mooncake" | head -10 || echo "ldconfig: 无 RDMA/mooncake 记录"

section "8. 容器内源码/构建残留"
ls -d /workspace/*mooncake* /root/*mooncake* /opt/*mooncake* /data/*mooncake* /usr/local/src/*mooncake* 2>/dev/null || echo "无源码目录"
SRC_CPP_CACHE=$(timeout 30 find / -name "real_client.cpp" -not -path "/proc/*" -not -path "/sys/*" 2>/dev/null || echo "")
echo "$SRC_CPP_CACHE" | head -5

section "9. glog / 日志级别相关环境（白名单变量）"
# 白名单逐变量读取，不做整行 env grep——MOONCAKE_API_TOKEN 之类的凭据变量
# 名称或值都可能命中宽匹配，整行输出会把令牌原值写进探测报告
PROBE_ENV_WHITELIST="GLOG_minloglevel GLOG_v GLOG_logtostderr GLOG_log_dir MOONCAKE_LOG_LEVEL RAY_DEDUP_LOGS"
found=0
for var in $PROBE_ENV_WHITELIST; do
  val=$(printenv "$var" 2>/dev/null || true)
  if [ -n "$val" ]; then
    printf '%s=%s\n' "$var" "$val"
    found=1
  fi
done
[ "$found" -eq 1 ] || echo "白名单变量（$PROBE_ENV_WHITELIST）均未设置"

# ---- JSON 摘要 ----
PY_PKGS=$(pip list 2>/dev/null | grep -i mooncake | tr '\n' ';' | sed 's/;*$//')
SO_LIST=$(echo "$ALL_SO_CACHE" | sort -u | head -20 | tr '\n' ';')
SO_TRUNCATED=false
[ "$(echo "$ALL_SO_CACHE" | grep -c .)" -gt 20 ] && SO_TRUNCATED=true
MAP_SO=""
# 按 pid 循环累积（不能把多 pid 拼进单个 /proc 路径：$ENG_PIDS 展开为
# "/proc/<pid1> <pid2>/maps" 会让 grep 收到非法路径、字段恒为空）
if [ -n "$ENG_PIDS" ]; then
  MAP_SO=$(for pid in $ENG_PIDS; do
             grep -ih mooncake /proc/$pid/maps 2>/dev/null
           done | awk '{print $NF}' | sort -u | tr '\n' ';')
fi
CMAKE_OK=$(command -v cmake >/dev/null 2>&1 && echo true || echo false)
GPP_OK=$(command -v g++ >/dev/null 2>&1 && echo true || echo false)
SRC_RES=$(echo "$SRC_CPP_CACHE" | head -1)

echo
echo "PROBE_JSON_BEGIN"
# JSON 摘要由 python3 json.dumps 生成：手工 heredoc 拼接时 .so 路径含
# 双引号/反斜杠会使整个摘要不可解析——序列化器保证任意路径值合法
if command -v python3 >/dev/null 2>&1 && python3 -c "pass" >/dev/null 2>&1; then
  python3 - "$PY_PKGS" "$SO_LIST" "$MAP_SO" "$ENGINE_PIDS_TRUNCATED" "$SO_TRUNCATED" "$CMAKE_OK" "$GPP_OK" "$SRC_RES" <<'PYEOF2'
import json, sys
obj = {
    "pip_packages": sys.argv[1],
    "so_files": sys.argv[2],
    "engine_mapped_so": sys.argv[3],
    "engine_pids_truncated": sys.argv[4] == "true",
    "so_files_truncated": sys.argv[5] == "true",
    "cmake": sys.argv[6] == "true",
    "gpp": sys.argv[7] == "true",
    "source_residue": sys.argv[8],
}
print(json.dumps(obj, ensure_ascii=False))
PYEOF2
else
  echo '{"error": "容器内无可用 python3，JSON 摘要生成失败——人读文本节（1-9）仍可作探测依据"}'
fi
echo "PROBE_JSON_END"
