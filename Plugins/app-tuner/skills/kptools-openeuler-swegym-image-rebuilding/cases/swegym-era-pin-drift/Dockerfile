# swegym-era-pin-drift —— 端到端 agent 级用例镜像
#
# 构建上下文 = Plugins/app-tuner/（与参考用例一致）
# 构建（容器内解析 openeuler 源 / pypi 镜像需稳定出网时加 --network host）：
#   docker build --network host \
#       -f skills/kptools-openeuler-swegym-image-rebuilding/cases/swegym-era-pin-drift/Dockerfile \
#       -t kpbot/app-tuner-agent-swegym-era-pin-drift:1.0-openeuler24.03 .
# A/B 对照：
#   ./run_ab.sh --save results-ab
#
# fixture 概要（全部在镜像内生成，无外部 src/）：
#   - testbed git 仓库两笔提交：base_commit 时代 requirements 含 pyarrow==13.0.0；
#     HEAD 丢掉该 pin（依赖漂移）。instance.json 记录真实 base_commit。
#   - 官方验收测试 tests_official/ 断言 pyarrow 主版本 = 13（时代兼容判据）。
#   - 开箱预建"漂移态" venv 并自检 gold=0 —— agent 面对真实失败现场。
#   - 受保护文件（官方测试 / run_gold.sh / build_env.sh / instance.json）固化
#     sha256，verify.sh 复核防作弊（改判据=篡改）。
FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        git python3 python3-pip findutils coreutils tar gzip xz diffutils \
    && dnf clean all

# pypi 镜像源（CN 网络下保证 pandas/pyarrow wheel 可拉）
RUN mkdir -p /etc/pip.conf.d \
    && printf '[global]\nindex-url = https://pypi.tuna.tsinghua.edu.cn/simple\n' \
       > /etc/pip.conf

# ---- fixture：testbed 仓库（base 时代 → HEAD 漂移）----
RUN mkdir -p /workspace/testbed && cd /workspace/testbed \
    && git init -q . \
    && git config user.email ci@fixture.local \
    && git config user.name fixture \
    && printf 'pandas==1.5.3\npyarrow==13.0.0\n' > requirements.txt \
    && git add requirements.txt \
    && git commit -qm 'base: era environment tested against pyarrow 13' \
    && git rev-parse HEAD > /workspace/base_commit.txt \
    && printf 'pandas==1.5.3\n' > requirements.txt \
    && git add requirements.txt \
    && git commit -qm 'chore: drop unused pins (introduces drift)'

# ---- fixture：实例元数据（SWE-Gym 风格）----
RUN cd /workspace && python3 - <<'PY'
import json
base = open('/workspace/base_commit.txt').read().strip()
json.dump({
    "instance_id": "swegym-mini-001",
    "base_commit": base,
    "FAIL_TO_PASS": ["tests_official/test_pyarrow_compat.py::test_pyarrow_era_compat"],
}, open('/workspace/instance.json', 'w'), indent=1)
PY

RUN mkdir -p /workspace/env /workspace/tests_official /workspace/opt /workspace/results \
    && cp /workspace/testbed/requirements.txt /workspace/env/requirements.txt

# ---- fixture：官方验收测试（gold 判据，受保护）----
RUN printf '%s\n' \
    'def test_pyarrow_era_compat():' \
    '    import pyarrow' \
    '    major = int(pyarrow.__version__.split(".")[0])' \
    '    assert major == 13, f"pyarrow {pyarrow.__version__} is out of the tested era (13.x)"' \
    '    import pandas as pd' \
    '    tbl = pyarrow.Table.from_pydict({"a": [1, 2, 3]})' \
    '    df = tbl.to_pandas()' \
    '    assert list(df["a"]) == [1, 2, 3]' \
    > /workspace/tests_official/test_pyarrow_compat.py

# ---- fixture：构建/验收脚本（受保护；修复必须经 env/requirements.txt 流经 build_env.sh）----
RUN printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'cd /workspace' \
    '[ -x /workspace/venv/bin/python ] || python3 -m venv /workspace/venv' \
    '/workspace/venv/bin/pip install -q -r env/requirements.txt' \
    > /workspace/build_env.sh \
    && printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'cd /workspace' \
    'mkdir -p results' \
    'if /workspace/venv/bin/python -m pytest tests_official/test_pyarrow_compat.py -q > results/gold_run.log 2>&1; then' \
    '  echo 1 > results/gold.json' \
    'else' \
    '  echo 0 > results/gold.json' \
    'fi' \
    'cat results/gold.json' \
    > /workspace/run_gold.sh \
    && chmod +x /workspace/build_env.sh /workspace/run_gold.sh

# ---- 预建漂移态环境 + 固化保护清单 + 开箱自检 gold=0 ----
RUN bash /workspace/build_env.sh \
    && /workspace/venv/bin/pip install -q pytest \
    && bash /workspace/run_gold.sh \
    && test "$(cat /workspace/results/gold.json)" = 0 \
    && cd /workspace \
    && sha256sum tests_official/test_pyarrow_compat.py run_gold.sh build_env.sh instance.json \
       > /workspace/.fixture.sha256

# skill 装入 staging；运行时由 entrypoint 按 SKILL_MODE 决定是否接入 .opencode/skills/
COPY skills/kptools-openeuler-swegym-image-rebuilding/ /staging/skill/
RUN rm -rf /staging/skill/cases

# 提示词 + 执行脚本
COPY skills/kptools-openeuler-swegym-image-rebuilding/cases/swegym-era-pin-drift/prompts/ /workspace/prompts/
COPY skills/kptools-openeuler-swegym-image-rebuilding/cases/swegym-era-pin-drift/verify.sh /workspace/verify.sh
COPY skills/kptools-openeuler-swegym-image-rebuilding/cases/swegym-era-pin-drift/entrypoint.sh /workspace/entrypoint.sh

RUN chmod +x /workspace/verify.sh /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
