#!/usr/bin/env python3
"""skeleton-drift-contract：与 sandbox-bench-flame 的共享骨架一致性检查（tier A）。

两技能共用"长时化 + 白名单 + 范围清理 + 计量期门禁"骨架（相似度 0.78），
任一侧演进时本检查失败，强制两侧同步评审纪律。
门禁标记按实现形态区分：flame 为内联 grep；topdown 为唯一实现脚本调用
（scripts/check_bench_phase.py，判据 Collecting 进度行在该脚本内）。
"""

import re
import sys
from pathlib import Path

DOMAIN = Path(__file__).resolve().parents[3]
SHARED = [
    ('case "$BENCH" in', '9 项白名单校验'),
    ('printf \'%s\\n\' "$$" > "$DIR/runner.pgid"', '本轮进程组记录'),
    ('--bench "^${BENCH}$"', '精确锚定匹配'),
    ('127.0.0.1:9113', '回环监听'),
    ('AENV_HOME_PATH="${AENV_HOME_PATH', 'AENV_* 可覆盖'),
    ('AENV_CONFIG_PATH="${AENV_CONFIG_PATH', 'AENV_CONFIG_PATH 输入化'),
]
GATE = {
    'sandbox-bench-flame': ('Collecting [0-9]+ samples', '计量期门禁（内联判据）'),
    'sandbox-bench-topdown': ('python3 "$CHECKER" "$LOG" --bench "$BENCH"', '计量期门禁（唯一实现脚本调用）'),
}


def main():
    failed = []
    for skill in ('sandbox-bench-topdown', 'sandbox-bench-flame'):
        text = (DOMAIN / skill / 'SKILL.md').read_text(encoding='utf-8')
        for marker, desc in SHARED:
            if marker not in text:
                failed.append('%s 缺 %s（%s）' % (skill, marker, desc))
        marker, desc = GATE[skill]
        if marker not in text:
            failed.append('%s 缺 %s（%s）' % (skill, marker, desc))
    # topdown 门禁唯一实现的判据在 checker 脚本内：Collecting 进度行 + 行尾锚定
    checker = (DOMAIN / 'sandbox-bench-topdown' / 'scripts' / 'check_bench_phase.py').read_text(encoding='utf-8')
    if not re.search(r'Collecting \(\\d\+\) samples', checker):
        failed.append('check_bench_phase.py 缺 Collecting 判据')
    if 'Benchmarking\\s' not in checker:
        failed.append('check_bench_phase.py 缺行尾锚定')
    if failed:
        for f in failed:
            print('FAIL', f)
        return 1
    print('OK: 共享纪律标记（6 共享 + 2 门禁形态 + 2 判据）两侧一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
