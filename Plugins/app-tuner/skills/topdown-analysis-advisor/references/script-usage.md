# 脚本用法

运行目录为本 skill 根目录，脚本只使用 Node.js 标准库。
最低版本 Node.js 18，不需要 npm install。
Windows 支持离线 analyze 和测试；collect 仅允许 Linux aarch64。

## 在线采集

先定位实际的 kptools-devkit/scripts/run_devkit.sh。
下面路径只是示例，请替换为环境中的绝对路径。

```bash
node scripts/topdown.mjs collect \
  --wrapper /opt/skills/kptools-devkit/scripts/run_devkit.sh \
  --out ./collection-01 \
  --duration 30 \
  --pid 1234 \
  --level 1 \
  --scope all \
  --context "steady workload, run 01"
```

父目录必须存在，输出目录必须不存在。
命令不自动创建多级目录，不复用已有证据目录。
--pid 与 --cpus 二选一，不支持 ALL 作为默认采集目标。
CPU 范围示例为 0-3,8；PID 列表示例为 1234,1235。
--duration 必须显式提供，范围 1..3600 秒。
--interval 默认 1 秒，必须不大于 duration。
--level 默认 1，允许 0..6。
--scope 默认 all，允许 user、kernel、all。
采集超时为请求时长加 30 秒启动/收尾余量。
超过时限终止采集器进程组，保存 timeout 状态。
不自动重试，也不启动业务进程。

## 离线解析

```bash
node scripts/topdown.mjs analyze \
  --input ./collection-01/stdout.txt \
  --manifest ./collection-01/manifest.json \
  --output ./topdown-summary.json
```

已有外部文件没有 manifest 时可省略该参数。
此时来源信息需人工补全，不能推定采集成功或范围已知。
输入上限为 32 MiB，超过时保留原文件并提取完整报告块进行分析。
提取后记录来源偏移，解析器行号针对提取后的文件。
输出文件必须不存在，避免覆盖先前分析。
manifest 与输入哈希不一致时写警告，不静默关联。
工具失败但留下部分数据时保留分析结果并标记 needs_review。

## 返回码

| 返回码 | 含义 |
| --- | --- |
| 0 | collect 进程成功，或 analyze 无格式警告 |
| 1 | 参数、文件或脚本执行错误 |
| 2 | 采集失败/超时，或解析结果需要人工复核 |

返回 2 时通常仍有产物，应读取状态和警告。
返回 0 不代表已完成根因分析。
stderr 可能包含零退出情况下的工具警告，必须复读。

## 测试

```bash
node --check scripts/topdown.mjs
node --test scripts/test/topdown.test.mjs
```

测试覆盖解析、多窗口、缺测、大计数、参数拦截、证据不覆盖及进程失败/超时。
模拟采集用当前 Node 进程，不安装 DevKit，不访问生产服务。
临时测试文件写入系统临时目录并清理。
真实平台验收还需检查工具版本、PID 范围、PMU、原始输出和采集开销。
本包的模拟测试不能替代上述真机验收。
