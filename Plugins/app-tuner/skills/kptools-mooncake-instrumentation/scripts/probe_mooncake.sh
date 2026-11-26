#!/usr/bin/env bash
# probe_mooncake.sh — 在目标容器内运行：探测现役 mooncake 版本、.so、加载路径、构建工具链
# 用法（宿主机）: docker exec <容器名> bash -s < probe_mooncake.sh
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

section "3. mooncake 相关 .so 文件（含软链指向）"
# -printf '%p -> %l'：软链输出 "路径 -> 目标"，普通文件经 sed 去掉尾部 " -> "；
# 现役 .so 常见多级软链形态（libmooncake-store.so -> libmooncake-store.so.1.2.3），
# 缺指向信息会削弱版本锚定的 md5/路径核对（需要真实文件而非链接本身）
find / \( -name "*mooncake*.so*" -o -name "libstore_c*.so*" \) -not -path "/proc/*" -not -path "/sys/*" -printf '%p -> %l\n' 2>/dev/null | sed 's/ -> $//'

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
find / -name "real_client.cpp" -not -path "/proc/*" -not -path "/sys/*" 2>/dev/null | head -5

section "9. glog / 日志级别相关环境"
env 2>/dev/null | grep -iE "GLOG|MOONCAKE|RAY_DEDUP" || echo "无相关环境变量"

# ---- JSON 摘要 ----
PY_PKGS=$(pip list 2>/dev/null | grep -i mooncake | tr '\n' ';' | sed 's/;*$//')
SO_LIST=$(find / \( -name "*mooncake*.so*" -o -name "libstore_c*.so*" \) -not -path "/proc/*" -not -path "/sys/*" -printf '%p -> %l\n' 2>/dev/null | sed 's/ -> $//' | sort -u | head -20 | tr '\n' ';')
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
SRC_RES=$(find / -name "real_client.cpp" -not -path "/proc/*" -not -path "/sys/*" 2>/dev/null | head -1)

echo
echo "PROBE_JSON_BEGIN"
cat <<EOF
{
  "pip_packages": "$PY_PKGS",
  "so_files": "$SO_LIST",
  "engine_mapped_so": "$MAP_SO",
  "engine_pids_truncated": $ENGINE_PIDS_TRUNCATED,
  "cmake": $CMAKE_OK,
  "gpp": $GPP_OK,
  "source_residue": "$SRC_RES"
}
EOF
echo "PROBE_JSON_END"
