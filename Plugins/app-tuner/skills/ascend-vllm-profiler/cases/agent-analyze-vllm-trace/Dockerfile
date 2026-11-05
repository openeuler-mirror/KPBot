FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        python3 findutils coreutils \
    && dnf clean all

# skill 装入 staging，运行时由 entrypoint 按 SKILL_MODE 决定是否接入 .opencode/skills/
COPY skills/ascend-vllm-profiler/ /staging/skill/
RUN rm -rf /staging/skill/cases

COPY skills/ascend-vllm-profiler/cases/agent-analyze-vllm-trace/src/ /workspace/src/
COPY skills/ascend-vllm-profiler/cases/agent-analyze-vllm-trace/prompts/ /workspace/prompts/
COPY skills/ascend-vllm-profiler/cases/agent-analyze-vllm-trace/verify.sh /workspace/verify.sh
COPY skills/ascend-vllm-profiler/cases/agent-analyze-vllm-trace/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/opt /workspace/results /workspace/input \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
