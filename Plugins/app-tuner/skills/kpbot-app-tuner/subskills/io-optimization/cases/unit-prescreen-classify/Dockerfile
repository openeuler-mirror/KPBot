# tier A 纯逻辑用例环境：python3 标准库 + bash + gawk（detect_bottleneck.sh
# 的 awk 使用 gawk 的 match($0, /re/, array) 语法）。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        python3 \
        gawk \
    && dnf clean all

# 被测对象：主 skill（scripts/detect_bottleneck.sh）+ io-optimization 子 skill
# （scripts/ + references/）。cases 不随镜像分发。
COPY skills/kpbot-app-tuner/scripts/ /staging/skill/scripts/
COPY skills/kpbot-app-tuner/subskills/io-optimization/ /staging/skill/subskills/io-optimization/
RUN rm -rf /staging/skill/subskills/io-optimization/cases

# 用例自身：单测 + mock 工具 + 执行器
COPY skills/kpbot-app-tuner/subskills/io-optimization/cases/unit-prescreen-classify/tests/ /workspace/tests/
COPY skills/kpbot-app-tuner/subskills/io-optimization/cases/unit-prescreen-classify/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/results \
    && chmod +x /workspace/entrypoint.sh /workspace/tests/mockbin/*

ENTRYPOINT ["/workspace/entrypoint.sh"]
