FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

# Rust 工具链 + python3（spill 分析脚本是纯 python 文本统计，无需 LLVM）
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        rustc cargo python3 gcc binutils findutils coreutils shadow \
    && dnf clean all \
    && rustc --version

# skill 装入 staging（含 scripts/analyze_assembly_spill.py），运行时由 entrypoint 按 SKILL_MODE 决定是否接入
# chmod a+rX：COPY 继承源目录权限（umask 077 下 700/600），非特权 optimizer 用户
# 运行期需读 staging 装配 .opencode/skills，不开放读权限 with 臂 cp 会失败
COPY skills/register-pressure-analysis/ /staging/skill/
RUN rm -rf /staging/skill/cases && chmod -R a+rX /staging/skill

COPY skills/register-pressure-analysis/cases/agent-analyze-rust-spill/src/ /workspace/src/
COPY skills/register-pressure-analysis/cases/agent-analyze-rust-spill/bench/ /workspace/bench/
COPY skills/register-pressure-analysis/cases/agent-analyze-rust-spill/prompts/ /workspace/prompts/
COPY skills/register-pressure-analysis/cases/agent-analyze-rust-spill/verify.sh /workspace/verify.sh
COPY skills/register-pressure-analysis/cases/agent-analyze-rust-spill/entrypoint.sh /workspace/entrypoint.sh

RUN useradd -m optimizer \
    && mkdir -p /workspace/opt /workspace/results \
    && chown -R optimizer:optimizer /workspace \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh \
    && sha256sum bench/main.rs src/kernel.rs verify.sh entrypoint.sh > /workspace/.pristine.sha256 \
    && chmod 444 bench/main.rs src/kernel.rs /workspace/.pristine.sha256 \
    && chmod 555 verify.sh entrypoint.sh

USER optimizer

ENTRYPOINT ["/workspace/entrypoint.sh"]
