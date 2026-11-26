# Nginx 鲲鹏加速 Playbook（硬件/运行时加速命令模板）

> 本文件是 `nginx-kunpeng-tuning` SKILL.md 的执行细节层：加速项清单与风险纪律见 SKILL.md。
> 经验数据来源：鲲鹏平台 nginx 压测实测（kpbot-dev-journal `a+k/bale/nginx/`）。

L3 属**运行时/库替换类高危变更**：每步必须按 Safety Gates 执行——先快照、再变更、
验证生效、A/B 证实收益后才保留；任一环节失败立即按回退步骤恢复，禁止跳步叠加。
多租户/共享节点上驱动与环境变量类变更影响全机，执行前必须确认占用情况。

## 前置快照（所有加速步骤共用，必做）

```bash
SNAP=$NGINX_DIR/rollback_snapshot; mkdir -p $SNAP
cp -a $NGINX_DIR/sbin/nginx $SNAP/nginx.orig            # nginx 二进制快照
sha256sum $NGINX_DIR/sbin/nginx | tee $SNAP/nginx.sha256
$NGINX_DIR/sbin/nginx -V 2>&1 | tee $SNAP/nginx-V.txt    # 编译参数存档（回退对比与重建依据）
cp -a $CONF $SNAP/nginx.conf.orig                       # 配置快照
env | sort > $SNAP/launch.env                           # 启动环境基线（LD_PRELOAD 等应此刻为空）
systemctl cat nginx 2>/dev/null > $SNAP/nginx.unit      # systemd 单元存档（若有）
```

## KAL OpenSSL（鲲鹏加速版 OpenSSL，https 场景核心收益）

**风险**：替换 OpenSSL 供给——`LD_LIBRARY_PATH` 对 nginx 及其子进程全部生效，与发行版
OpenSSL 混链可能符号冲突/ABI 不一致；环境变量类变更影响本会话所有 openssl 调用方。

> 本 skill 只承载运行期注入；KAL 源码/制品的获取渠道未明确、不在本 skill 内维护
> （现场已具备 KAL 部署：源码、安装路径与环境说明由用户或内部渠道提供时才执行本节）。

```bash
# 运行期（前提：KAL 已部署于 <KAL_OPENSSL_PATH>，路径由现场提供）：
export LD_LIBRARY_PATH=<KAL_OPENSSL_PATH>/lib:$LD_LIBRARY_PATH
export PATH=<KAL_OPENSSL_PATH>/bin:$PATH
taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
```

**生效验证**：
```bash
ldd $NGINX_DIR/sbin/nginx | grep libssl    # 应指向 <KAL_OPENSSL_PATH>/lib 的 kal 版本
$NGINX_DIR/sbin/nginx -t -c $CONF          # 依赖加载门控
```
收益以 https 压测 A/B 为准（验证纪律见 `../../nginx-baseline-setup/`）。

**回退**：
```bash
unset LD_LIBRARY_PATH                      # 或按 $SNAP/launch.env 恢复原环境
nginx -s stop -c $CONF; taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
ldd $NGINX_DIR/sbin/nginx | grep libssl    # 应回到发行版 libssl
```

## KAE / UADK 硬件加速引擎（hisi_hpre 等，需鲲鹏加速器硬件）

**风险**：`modprobe -r hisi_hpre` 卸载驱动影响**全机所有占用该加速器的进程**（多租户节点
高风险）；`uacce_mode=2` 改变设备直通模式；`OPENSSL_CONF`/`OPENSSL_ENGINES` 影响本会话
所有 openssl 调用方。

> 设备判定、引擎自检、依赖与 ASF 使能的完整工作流见
> `accelerator-optimization`（按 skill 名引用；安装后位于 `<config>/skills/kpbot-app-tuner/subskills/accelerator-optimization/`）（采集/使能/验证脚本齐备）。
>
> 若 `accelerator-optimization` 尚未合入（当前安装树没有其采集/使能脚本），用以下最小路径完成采集与验证；模式切换/回退沿用本节下方命令块：
>
> ```bash
> # 采集（只读）
> lspci -d 19e5:a25[89] 2>/dev/null || true
> ls /sys/class/uacce/hisi_hpre-* /dev/hisi_hpre-* 2>/dev/null || true
> cat /sys/module/hisi_hpre/parameters/uacce_mode 2>/dev/null || true
> # 使能：执行本节下方“切换前保存实际模式/确认占用/切换”命令块
> # 验证
> export OPENSSL_ENGINES=/usr/lib64/engines-1.1 OPENSSL_CONF=<openssl_kae.cnf>
> openssl engine -t uadk_engine
> grep -c uadk /proc/<nginx-worker-pid>/maps
> ```

```bash
# 1) 切换前保存实际模式与加载状态；参数不可读则停止
UACCE_MODE_ORIG=$(cat /sys/module/hisi_hpre/parameters/uacce_mode) || {
  echo "ERROR: cannot read hisi_hpre uacce_mode; refuse to switch" >&2; exit 1; }
MODULE_LOADED_ORIG=$(lsmod | awk '$1=="hisi_hpre"{print 1; exit}')
[ -n "$UACCE_MODE_ORIG" ] || { echo "ERROR: empty uacce_mode; refuse to switch" >&2; exit 1; }

# 2) 从 sysfs 解析实际 /dev 节点，逐一检查占用；无法解析节点或权限不足则停止
command -v fuser >/dev/null || { echo "ERROR: fuser not available; cannot verify occupancy" >&2; exit 1; }
NODES=0
for d in /sys/class/uacce/hisi_hpre-*; do
  [ -e "$d" ] || continue
  name=${d##*/}
  node=""
  for cand in "/dev/$name" "/dev/uacce/$name"; do
    [ -e "$cand" ] && node="$cand" && break
  done
  [ -n "$node" ] || { echo "ERROR: cannot resolve /dev node for $name; refuse to switch" >&2; exit 1; }
  [ -r "$node" ] || { echo "ERROR: cannot access $node; refuse to switch" >&2; exit 1; }
  if fuser -v "$node" >/dev/null 2>&1; then
    echo "ERROR: $node is in use; refuse to switch" >&2; exit 1
  fi
  NODES=$((NODES + 1))
done
[ "$NODES" -gt 0 ] || { echo "ERROR: no hisi_hpre uacce device found" >&2; exit 1; }

# 3) 确认无占用后切换；随后读回参数确认生效
modprobe -r hisi_hpre && modprobe hisi_hpre uacce_mode=2
[ "$(cat /sys/module/hisi_hpre/parameters/uacce_mode)" = "2" ] || {
  echo "ERROR: uacce_mode readback is not 2" >&2; exit 1; }
export OPENSSL_ENGINES=/usr/lib64/engines-1.1               # OpenSSL 1.1 引擎目录（按实际安装位置调整）
export OPENSSL_CONF=<openssl_kae.cnf>                       # 引用 kaectl 生成的引擎配置
```
**生效验证**：`openssl engine -t` 显示 kae 引擎可用；nginx `error_log` 无 engine 加载错误；
https 压测 A/B 确认收益。无硬件时整节跳过。

**回退**：
```bash
# 回退前按同样方式复查占用；仍在用则等获准窗口，不得直接卸载
command -v fuser >/dev/null || { echo "ERROR: fuser not available; cannot verify occupancy" >&2; exit 1; }
for d in /sys/class/uacce/hisi_hpre-*; do
  [ -e "$d" ] || continue
  name=${d##*/}
  for cand in "/dev/$name" "/dev/uacce/$name"; do
    [ -e "$cand" ] || continue
    [ -r "$cand" ] || { echo "ERROR: cannot access $cand; refuse to roll back" >&2; exit 1; }
    if fuser -v "$cand" >/dev/null 2>&1; then
      echo "ERROR: $cand is in use; refuse to roll back" >&2; exit 1
    fi
  done
done
modprobe -r hisi_hpre
if [ "$MODULE_LOADED_ORIG" = "1" ]; then
  modprobe hisi_hpre uacce_mode="$UACCE_MODE_ORIG"
  [ "$(cat /sys/module/hisi_hpre/parameters/uacce_mode)" = "$UACCE_MODE_ORIG" ] || {
    echo "ERROR: uacce_mode readback does not match original $UACCE_MODE_ORIG" >&2; exit 1; }
else
  lsmod | awk '$1=="hisi_hpre"{found=1} END{exit found?0:1}' && {
    echo "ERROR: hisi_hpre still loaded; original state was unloaded" >&2; exit 1; }
fi
unset OPENSSL_ENGINES OPENSSL_CONF
```


## tcmalloc（替换 glibc malloc；nginx 自带内存池，收益场景相关，A/B 为准）

**风险**：`LD_PRELOAD` 替换全局分配器，影响 nginx master 与全部 worker 及其子进程；与其他
预加载分配器（jemalloc 等）互斥；库版本与运行时不匹配可致段错误（nginx 崩溃 = 服务中断）。

```bash
# 二选一：发行版包 或 源码固定前缀安装；安装后必须先确认库文件真实存在
# 分支 A：发行版包（openEuler：gperftools-libs 提供带版本号的 .so.4）
#   dnf install -y gperftools-libs
#   TCMALLOC_LIB=/usr/lib64/libtcmalloc.so.4
# 分支 B：源码编译——固定发布 tag，禁止 master HEAD
#   git clone https://github.com/gperftools/gperftools && cd gperftools
#   git checkout gperftools-2.9.1 && git rev-parse HEAD | tee gperftools.commit
#   ./autogen.sh && ./configure --prefix=/opt/gperftools-2.9.1 && make -j && make install
#   TCMALLOC_LIB=/opt/gperftools-2.9.1/lib/libtcmalloc.so

[ -n "${TCMALLOC_LIB:-}" ] && [ -r "$TCMALLOC_LIB" ] || {
  echo "ERROR: TCMALLOC_LIB is unset or unreadable; refuse to restart nginx" >&2; exit 1; }
export LD_PRELOAD="$TCMALLOC_LIB"       # 启动环境沿用所选分支的绝对路径
nginx -s stop -c $CONF; taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
```
**生效验证**：
```bash
grep -c tcmalloc /proc/<nginx-master-pid>/maps   # ≥1 才生效
grep -c tcmalloc /proc/<worker-pid>/maps          # worker 继承 LD_PRELOAD，同样应有映射
```

**回退**：
```bash
unset LD_PRELOAD
nginx -s stop -c $CONF; taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
grep -c tcmalloc /proc/<nginx-master-pid>/maps || echo "回退确认：无 tcmalloc 映射"
```

## xcall 微架构开关（序列不在本 playbook 维护）

风险纪律见 SKILL.md §2（最高风险：非公开接口、影响同核其他进程、无回执；共享节点禁止、
仅独占环境在完整基线存档后使用、重启消除）。开关序列属实测经验值，未在 skill 内
维护——确需使用时按现场原始实测记录（kpbot-dev-journal）执行，生效与收益仅以压测 A/B
前后对比作为证据。
