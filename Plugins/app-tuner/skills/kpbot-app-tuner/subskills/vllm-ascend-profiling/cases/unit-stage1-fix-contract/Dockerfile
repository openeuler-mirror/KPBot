# tier A 纯逻辑用例环境：仅需 python3 标准库（unittest）
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        python3 \
    && dnf clean all

# 被测 skill 装入 staging（含 scripts/ + references/），cases 不随镜像分发
COPY skills/kpbot-app-tuner/subskills/vllm-ascend-profiling/ /staging/skill/
RUN rm -rf /staging/skill/cases

# 用例自身：单测 + 执行器
COPY skills/kpbot-app-tuner/subskills/vllm-ascend-profiling/cases/unit-stage1-fix-contract/tests/ /workspace/tests/
COPY skills/kpbot-app-tuner/subskills/vllm-ascend-profiling/cases/unit-stage1-fix-contract/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/results && chmod +x /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
