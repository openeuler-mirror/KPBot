FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

# Rust 工具链 + LLVM（mca 分析脚本依赖 llvm-mca/clang）
# 备选：若 dnf 源内 llvm 包名漂移，改用 llvm-toolset（含 llvm-mca）
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        rustc cargo llvm llvm-devel python3 gcc binutils findutils coreutils shadow \
    && dnf clean all \
    && rustc --version && llvm-mca --version | head -1

# skill 装入 staging（含 scripts/mca_analyze.py），运行时由 entrypoint 按 SKILL_MODE 决定是否接入
COPY skills/llvm-mca-analysis/ /staging/skill/
RUN rm -rf /staging/skill/cases

COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/src/ /workspace/src/
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/bench/ /workspace/bench/
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/prompts/ /workspace/prompts/
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/verify.sh /workspace/verify.sh
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/entrypoint.sh /workspace/entrypoint.sh

RUN useradd -m optimizer \
    && mkdir -p /workspace/opt /workspace/results \
    && chown -R optimizer:optimizer /workspace \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh \
    && sha256sum bench/main.rs src/kernel.rs verify.sh entrypoint.sh > /workspace/.pristine.sha256 \
    && chmod 444 bench/main.rs src/kernel.rs /workspace/.pristine.sha256 \
    && chmod 555 verify.sh entrypoint.sh

USER optimizer

ENTRYPOINT ["/workspace/entrypoint.sh"]
