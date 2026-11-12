# agent 用例环境：python3（跑 skill 脚本）+ docker CLI（sock 挂载探真机容器）
FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        python3 docker curl \
    && dnf clean all

# 被测 subskill 装入 staging，运行时由 entrypoint 按 SKILL_MODE 决定是否接入
COPY skills/kpbot-app-tuner/subskills/vllm-ascend-profiling/ /staging/skill/
RUN rm -rf /staging/skill/cases

# 提示词（with/without 两套，仅差 skill 提及）+ 执行/判定脚本
COPY skills/kpbot-app-tuner/subskills/vllm-ascend-profiling/cases/agent-stage1-diagnosis/prompts/ /workspace/prompts/
COPY skills/kpbot-app-tuner/subskills/vllm-ascend-profiling/cases/agent-stage1-diagnosis/verify.sh /workspace/verify.sh
COPY skills/kpbot-app-tuner/subskills/vllm-ascend-profiling/cases/agent-stage1-diagnosis/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/opt /workspace/results \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
