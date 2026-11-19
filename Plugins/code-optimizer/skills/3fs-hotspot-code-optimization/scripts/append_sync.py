#!/usr/bin/env python3
"""裸盘 4K 追加写 + fdatasync 延迟分位数统计。

用途: 模拟 3FS chunk_engine 写 RocksDB WAL 的 IO 模式（4K 追加写，每写一块
立即 fdatasync），绕开 3FS 与 fio 版本差异，直接定界盘固件的 FLUSH 长尾。
背景案例与判读方法见 SKILL.md 阶段 2c（fdatasync 长尾案例）。

用法:
    python3 append_sync.py [path] [duration_seconds]

    path              测试文件路径，必须落在与 RocksDB/chunk_engine meta 相同的
                      文件系统上（默认 /storage/data0/append-sync-test）。
                      **必须是不存在的新文件名**：脚本以 O_EXCL 独占创建，
                      目标已存在则报错退出（防止误清空/误删既有 WAL 或数据
                      文件）；结束时只删除本次自己创建的文件
    duration_seconds  测试时长秒数（默认 30）

输出: count / avg / p50 / p90 / p95 / p99 / p99.9 / max（单位 µs）。

判读纪律:
    - 每节点至少跑 3 次（同固件个体状态波动大）；A/B 两边用同一脚本。
    - 看 p99/p99.9 与对照的比值，不要只看 avg —— 均值会掩盖长尾
      （案例中慢集群 p50 与快集群相当，p99 差 10~15 倍）。
"""

import os
import sys
import time

DEFAULT_PATH = "/storage/data0/append-sync-test"
DEFAULT_DURATION_SECS = 30
BLOCK_SIZE = 4096


def percentile(sorted_times, fraction):
    """取已排序样本的分位数（与案例记录口径一致: int(n * p)）。"""
    return sorted_times[int(len(sorted_times) * fraction)]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DURATION_SECS

    # O_EXCL: 只创建不存在的文件。目标已存在（如运维误指向既有 WAL/数据文件）
    # 时立即报错退出，绝不截断或删除既有数据；结束时的 unlink 只作用于本次
    # 独占创建的文件。
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("error: 目标文件已存在: %s\n"
              "本脚本会独占创建测试文件并在结束时删除它；为防止误删既有数据\n"
              "（WAL/数据文件等），请指定一个不存在的新文件名，例如:\n"
              "  python3 append_sync.py %s.append-test-$$" % (path, path),
              file=sys.stderr)
        return 1
    buf = b"x" * BLOCK_SIZE
    times = []
    start = time.time()
    try:
        while time.time() - start < duration:
            sample_start = time.time()
            os.write(fd, buf)
            os.fdatasync(fd)
            times.append((time.time() - sample_start) * 1e6)
    finally:
        os.close(fd)
        os.unlink(path)

    count = len(times)
    if count == 0:
        print("error: no samples collected (duration too short?)",
              file=sys.stderr)
        return 1

    times.sort()
    print("count=%d  avg=%.1fus  p50=%.1fus  p90=%.1fus  p95=%.1fus  "
          "p99=%.1fus  p99.9=%.1fus  max=%.1fus"
          % (count,
             sum(times) / count,
             percentile(times, 0.5),
             percentile(times, 0.9),
             percentile(times, 0.95),
             percentile(times, 0.99),
             percentile(times, 0.999),
             times[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
