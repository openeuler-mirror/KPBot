FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        gcc binutils findutils coreutils \
    && dnf clean all

# skill 装入 staging，运行时由 entrypoint 按 SKILL_MODE 决定是否接入 .opencode/skills/
COPY skills/memory-access-optimization/ /staging/skill/
RUN rm -rf /staging/skill/cases

COPY skills/memory-access-optimization/cases/agent-optimize-memlayout/src/ /workspace/src/
COPY skills/memory-access-optimization/cases/agent-optimize-memlayout/bench/ /workspace/bench/
COPY skills/memory-access-optimization/cases/agent-optimize-memlayout/prompts/ /workspace/prompts/
COPY skills/memory-access-optimization/cases/agent-optimize-memlayout/verify.sh /workspace/verify.sh
COPY skills/memory-access-optimization/cases/agent-optimize-memlayout/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/opt /workspace/results \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]