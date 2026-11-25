#!/usr/bin/env python3
"""skeleton-drift-contract：与 sandbox-bench-flame 的共享骨架一致性检查（tier A）。

两技能共用"长时化 + 白名单 + 范围清理 + 计量期门禁"骨架（相似度 0.78），
任一侧演进时本检查失败，强制两侧同步评审纪律。
"""

import sys
from pathlib import Path

DOMAIN = Path(__file__).resolve().parents[3]
MARKERS = [
    ('case "$BENCH" in', '9 项白名单校验'),
    ('printf \'%s\\n\' "$$" > "$DIR/runner.pgid"', '本轮进程组记录'),
    ('Collecting [0-9]+ samples', '计量期门禁判据'),
    ('--bench "^${BENCH}$"', '精确锚定匹配'),
    ('127.0.0.1:9113', '回环监听'),
    ('AENV_HOME_PATH="${AENV_HOME_PATH', 'AENV_* 可覆盖'),
    ('AENV_CONFIG_PATH="${AENV_CONFIG_PATH', 'AENV_CONFIG_PATH 输入化'),
]
SKILLS = ['sandbox-bench-topdown', 'sandbox-bench-flame']


def main():
    failed = []
    for skill in SKILLS:
        text = (DOMAIN / skill / 'SKILL.md').read_text(encoding='utf-8')
        for marker, desc in MARKERS:
            if marker not in text:
                failed.append('%s 缺 %s（%s）' % (skill, marker, desc))
    if failed:
        for f in failed:
            print('FAIL', f)
        return 1
    print('OK: %d 个共享纪律标记在 %s 两侧一致' % (len(MARKERS), '/'.join(SKILLS)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
