FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

# 工具链：编译验证用 gcc/binutils/findutils；curl 供后续官方安装 opencode 使用
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        gcc binutils findutils coreutils \
    && dnf clean all

# skill 装入 staging，运行时由 entrypoint 按 SKILL_MODE 决定是否接入 .opencode/skills/
COPY skills/apply-vectorization/ /staging/skill/
COPY opencode/apply-vectorization/SKILL.md /staging/skill/SKILL.md
RUN rm -rf /staging/skill/cases

# 待优化源码 + 基准 + 提示词（with/without 两套）+ 执行脚本
COPY skills/apply-vectorization/cases/agent-optimize-dotproduct/src/ /workspace/src/
COPY skills/apply-vectorization/cases/agent-optimize-dotproduct/bench/ /workspace/bench/
COPY skills/apply-vectorization/cases/agent-optimize-dotproduct/prompts/ /workspace/prompts/
COPY skills/apply-vectorization/cases/agent-optimize-dotproduct/verify.sh /workspace/verify.sh
COPY skills/apply-vectorization/cases/agent-optimize-dotproduct/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/opt /workspace/results \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]