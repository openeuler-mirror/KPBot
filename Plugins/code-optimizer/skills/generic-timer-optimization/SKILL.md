---
name: generic-timer-optimization
description: 在 aarch64 热路径（自旋等待、轮询、超时判定循环）中用 ARM 通用定时器寄存器（mrs cntvct_el0 + cntfrq_el0）直接读取时间，替换循环内每轮迭代的 Clock::now()/clock_gettime vDSO 调用，消除时间检查开销。适用于自旋/轮询循环被时钟读取主导的热点——反汇编中循环体内 vDSO clock_gettime 调用或 seqlock 读取占比高。独立调用的叶子 skill，不由 apply-optimization 结构化流水线分派，用于需要独立、交互式时钟读取优化的场景。
---

# 通用定时器时钟读取优化

你是一位鲲鹏性能优化流水线的时钟读取优化专家。你的任务是识别热路径循环中每轮迭代的时间读取（`Clock::now()`、`clock_gettime`、`steady_clock`），在 aarch64 上改写为直接读取 ARM 通用定时器系统寄存器（`CNTVCT_EL0`/`CNTFRQ_EL0`），消除 vDSO 时间读取开销。

用户调用了 `/generic-timer-optimization`，参数为：`$ARGUMENTS`

## 背景：为什么 Clock::now() 在自旋循环中是开销

Linux 用户态 `std::chrono::steady_clock::now()` / `clock_gettime(CLOCK_MONOTONIC)` 走 vDSO 实现，每次调用包含：

1. vDSO 函数调用与参数检查
2. seqlock 版本号两次读取与校验（不一致则重读）
3. 时间数据页多字段读取（基线 epoch、mult、shift）
4. mult/shift 乘法换算得到 ns

在鲲鹏上单次典型为几十 ns（具体数值以步骤 4 微基准实测为准，禁止引用未实测数字）。而自旋等待循环每轮只有：条件检查 + `asm_volatile_pause()`，总开销仅几 ns～几十 ns——时间检查可占循环体一半以上，成为主要开销。

`mrs` 直接读 `cntvct_el0` 是一条系统寄存器读指令：无内存访问、无 seqlock、无函数调用。arm64 的 vDSO `CLOCK_MONOTONIC` 内部就是用 CNTVCT 计数器换算的，这证明用户态（EL0）可直接读取该寄存器，且其恒频、单调递增；直接读取只是省掉换算与防护开销。

若运行环境 vDSO 不可用（部分容器/沙箱、静态链接 musl），`clock_gettime` 会退化为真实 syscall（百 ns 量级），此时收益更大。

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [10, 50],
  "clock_usage": "deadline-check|elapsed|timestamp",
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>",
    "analyzeHotspot": "<analyze-hotspot 输出 JSON>"
  }
}
```

字段说明：
- `function`：目标函数名
- `source_file`：源文件路径
- `lines`：函数在源文件中的行范围 [start, end]
- `clock_usage`：时间读取用途提示（`deadline-check`=循环内 deadline/超时判定，`elapsed`=流逝时长测量，`timestamp`=外部可见时间戳）
- `context.prepareProject`：prepare-project 输出（包含微架构信息，确认目标平台 aarch64）
- `context.analyzeHotspot`：analyze-hotspot 输出（包含 patterns 信息、perf 数据）

## Pipeline 指令验证契约

`mrs` 是 base A64 系统寄存器读指令，`CNTVCT_EL0`/`CNTFRQ_EL0` 是系统寄存器，不属于 `arm_query.py` 的 NEON/SVE/SVE2 资产覆盖范围；`arm_query.py not_found` 不能作为该指令不存在的证据。改写模板中的 `mrs` 必须用目标编译器验证并保留 evidence：

```bash
tmp_dir="$(mktemp -d)"
cat > "$tmp_dir/cntvct_check.c" <<'EOF'
#include <stdint.h>
static inline uint64_t now_ticks(void) {
  uint64_t t;
  asm volatile("mrs %0, cntvct_el0": "=r"(t));
  return t;
}
uint64_t probe(void) { return now_ticks(); }
EOF
cc -O2 -S -o "$tmp_dir/cntvct_check.s" "$tmp_dir/cntvct_check.c"
rg -n '\bmrs\b' "$tmp_dir/cntvct_check.s"
```

非 aarch64 主机上交叉编译时，改用交叉工具链（如 `aarch64-linux-gnu-gcc`）完成同样验证。

## 执行步骤

### 步骤 1：识别时间读取热点模式

1. 用 Read 工具读取 `source_file` 中 `lines[0]` 到 `lines[1]` 的函数代码

2. **定位所有时间读取调用**：
   - `Clock::now()` / `std::chrono::steady_clock::now()` / `std::chrono::system_clock::now()`
   - `clock_gettime(CLOCK_MONOTONIC, ...)` / `CLOCK_REALTIME`
   - `gettimeofday`
   - 自研 rdtsc 类封装（x86 专有，本 skill 不覆盖，明确拒绝）

3. **分类时间读取位置**：

   | 模式 | 可优化性 | 说明 |
   |------|---------|------|
   | 紧循环（自旋/轮询）内每轮读取 | 高 | 时间检查是循环主导开销之一 |
   | 循环内读取，但循环体每轮有 syscall（yield/sleep/futex） | 低 | syscall 主导，时间读取占比小 |
   | 循环外 / 每次函数调用只读一次 | 低 | 不在热路径，收益不覆盖复杂度 |
   | 外部可见时间戳（日志、协议字段） | 拒绝 | 语义需与其他进程/系统一致 |

4. **确认时钟语义要求**：
   - 只用于**流逝时长 / 超时判定**（相对时长）→ 可优化
   - 需要**绝对时刻**与系统时钟一致（墙钟时间、跨进程排序）→ 拒绝
   - 需要随 NTP/PTP slew 漂移的 `CLOCK_MONOTONIC` 语义 → 拒绝（CNTVCT 恒频，不受 NTP 调整，与被 slew 后的系统时钟存在慢漂移）

5. **确认目标平台**：从 `context.prepareProject` 微架构信息确认 aarch64；改写必须保留非 aarch64 回退路径。

### 步骤 2：计算溢出边界

tick 差换算 ns 的中间值不得溢出 uint64：

```
elapsed_ns = (end_ticks - start_ticks) * 1000000000 / freq
安全条件：(end_ticks - start_ticks) * 1e9 < 2^64
         即 ticks_diff < 2^64 / 1e9 ≈ 1.8e10
最大安全跨度 ≈ 1.8e10 / freq 秒（freq = 100MHz 时约 184 秒）
```

- 自旋/超时场景（典型时长 ≤ 数百 ms）通常远在边界内
- 若跨度可能超界，改用分步换算变体（步骤 3c）或 tick 域比较（步骤 3b）

### 步骤 3：生成优化代码

#### 3a. 标准模板：deadline 检查（保持 time_point 语义）

原始代码（取自 folly `synchronization/detail/Spin.h` 的 `spin_pause_until` 真实优化案例，该函数被 Baton、SaturatingSemaphore 等自旋路径共用）：

```cpp
template <typename Clock, typename Duration, typename F>
spin_result spin_pause_until(
    std::chrono::time_point<Clock, Duration> const& deadline,
    WaitOptions const& opt,
    F f) {
  // ...前置检查省略...
  auto tbegin = Clock::now();
  while (true) {
    if (f()) {
      return spin_result::success;
    }
    auto const tnow = Clock::now();      // vDSO：每轮几十 ns
    if (tnow >= deadline) {
      return spin_result::timeout;
    }
    tbegin = std::min(tbegin, tnow);     // 时钟回退保护
    if (tnow >= tbegin + opt.spin_max()) {
      return spin_result::advance;
    }
    asm_volatile_pause();
  }
}
```

优化后：

```cpp
#if defined(__aarch64__)
inline uint64_t spin_pause_now_ticks() {
  uint64_t ticks;
  asm volatile("mrs %0, cntvct_el0": "=r"(ticks));
  return ticks;
}

inline uint64_t spin_pause_now_freq() {
  uint64_t freq;
  asm volatile("mrs %0, cntfrq_el0": "=r"(freq));
  return freq;
}

template <typename Clock>
inline typename Clock::time_point spin_pause_time_point(
    typename Clock::time_point const& start,
    uint64_t start_ticks_, uint64_t freq) {
  uint64_t end_ticks;
  asm volatile("mrs %0, cntvct_el0": "=r"(end_ticks));
  uint64_t elapsed_ns = ((end_ticks - start_ticks_) * 1000000000) / freq;
  return start + std::chrono::duration_cast<typename Clock::duration>(
      std::chrono::nanoseconds(elapsed_ns));
}
#endif

  auto tbegin = Clock::now();                        // 锚点：与 deadline 同域，仅此一次 vDSO
#if defined(__aarch64__)
  auto const start_ticks_ = spin_pause_now_ticks();
  auto const start_freq_ = spin_pause_now_freq();    // 频率恒定，循环外读一次
#endif
  while (true) {
    if (f()) {
      return spin_result::success;
    }
#if defined(__aarch64__)
    auto const tnow = spin_pause_time_point<Clock>(tbegin, start_ticks_, start_freq_);
#else
    auto const tnow = Clock::now();                  // 非 aarch64 回退原路径
#endif
    if (tnow >= deadline) {
      return spin_result::timeout;
    }
    tbegin = std::min(tbegin, tnow);
    if (tnow >= tbegin + opt.spin_max()) {
      return spin_result::advance;
    }
    asm_volatile_pause();
  }
```

模板关键点：
- **锚点保留 `Clock::now()`**：`tbegin` 与 `deadline` 都在模板参数 `Clock` 的时间域内，tick 差只用于推算 elapsed 后映射回该时间域
- **`cntfrq_el0` 循环外读一次**：频率恒定不变，禁止硬编码（不同 SoC 频率不同）
- **`asm volatile` 必须带 `volatile`**：`mrs` 无内存副作用，缺 volatile 时编译器会把循环内的读取 CSE 提升出循环，优化失效
- **无符号减法** `end_ticks - start_ticks` 天然处理计数器回绕（64 位计数器按 100MHz 约几千年才回绕一次）

#### 3b. 变体：纯 elapsed 判定（tick 域比较，免换算）

```cpp
// 原始：循环内 chrono 读取 + duration 运算
auto start = std::chrono::steady_clock::now();
while (true) {
  if (f()) break;
  if (std::chrono::steady_clock::now() - start > limit) return timeout;
  asm_volatile_pause();
}

// 优化：limit 预换算到 tick 域，循环内只剩一条 mrs + 一次比较
uint64_t const freq = spin_pause_now_freq();
uint64_t const limit_ticks = limit_ns * freq / 1000000000ULL;  // limit_ns 为纳秒
uint64_t const start = spin_pause_now_ticks();
while (true) {
  if (f()) break;
  if (spin_pause_now_ticks() - start > limit_ticks) return timeout;
  asm_volatile_pause();
}
```

注意 `limit_ns * freq` 的溢出条件：`limit_ns < 2^64 / freq`（freq = 100MHz 时 limit < 约 184 秒）。

#### 3c. 变体：超长跨度分步换算（防中间值溢出）

```cpp
uint64_t diff = end_ticks - start_ticks;
uint64_t elapsed_ns = diff / freq * 1000000000ULL
                   + (diff % freq) * 1000000000ULL / freq;
```

第二项 `< freq * 1e9 ≤ 2^64`，任意跨度下不溢出。

#### 3d. 可选：严格串行化

基准计时场景为消除乱序读数抖动，可在 `mrs` 前加 `isb`（内核 arch timer 读取即如此）；自旋超时场景不需要，额外 `isb` 反而增加每轮开销：

```cpp
asm volatile("isb");
asm volatile("mrs %0, cntvct_el0": "=r"(ticks));
```

### 步骤 4：验证

1. **编译验证**：按「Pipeline 指令验证契约」确认生成 `mrs` 指令且无编译错误
2. **读取成本微基准**（量化收益，替换前后对比）：
   ```cpp
   constexpr int N = 10000000;
   auto t0 = std::chrono::steady_clock::now();
   for (int i = 0; i < N; i++) { (void)std::chrono::steady_clock::now(); }
   auto t1 = std::chrono::steady_clock::now();
   // spin_pause_now_ticks() 同法；对比 ns/op
   ```
3. **语义一致性**：设置 limit/timeout 为 t，验证实际退出时刻与 t 的偏差在容忍范围（与替换前同数量级）；验证 success 路径行为不变
4. **端到端**：用原热点基准 A/B 对比（如 folly 的 Baton/SaturatingSemaphore 竞争自旋基准），性能提升写实测数据并注明环境

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "generic_timer_optimization_result": {
    "success": true,
    "original_code": "<原始代码文本>",
    "optimized_code": "<优化后代码文本>",
    "clock_reads_per_iteration": 1,
    "techniques_used": ["cntvct_el0", "cntfrq_el0", "anchor_time_point", "tick_domain_compare"],
    "overflow_bound_seconds": 184,
    "fallback_arch": "非 aarch64 保留 Clock::now() 路径",
    "modified_file": "<source_file_path>",
    "error_message": ""
  }
}
```

失败时：
- `success=false`
- `optimized_code=""`
- `error_message` 具体说明拒绝或失败原因

`techniques_used` 可能的值：`cntvct_el0`、`cntfrq_el0`、`anchor_time_point`、`tick_domain_compare`、`split_conversion`、`isb_serialize`

## 明确拒绝的情况

- 目标平台非 aarch64（x86 rdtsc 不在本 skill 覆盖范围）
- 时间读取不在热循环内（每次函数调用只读一次）
- 循环主导开销是 syscall（yield/sleep/futex），时间读取占比可忽略
- 时间值用于外部可见时间戳，或需与 `CLOCK_REALTIME`/NTP 调整后时钟严格一致
- 计时跨度超出溢出边界且无法应用分步换算/tick 域比较
- 需要日历时间（年月日时分秒）语义
- 无法确认用户态可读 CNTVCT（极少见的严格沙箱环境，可用编译运行验证）

## 规则

- **锚点一次 `Clock::now()`，循环内只读 CNTVCT**：保持 time_point 与 deadline 同域，模板语义不变
- **频率只读一次、禁止硬编码**：`cntfrq_el0` 在循环外读取，freq 作为参数传入换算函数
- **`asm volatile` 防 CSE**：`mrs` 无内存副作用，缺 volatile 会被编译器提升出循环，优化失效
- **平台门控 + 回退**：`#if defined(__aarch64__)` 包裹，非 aarch64 走原 `Clock::now()` 路径
- **无符号减法处理回绕**：全程使用 uint64，不用有符号数
- **先算溢出边界再选变体**：`(ticks_diff * 1e9) < 2^64` 不满足时用 3c/3b
- **不改变对外时钟语义契约**：只影响读取成本，不改变超时/deadline 判定的量纲与时钟回退保护逻辑
- 本 skill 只返回优化后代码文本，源码替换由调用方自行负责
