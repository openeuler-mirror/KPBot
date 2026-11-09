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
# chmod a+rX：COPY 继承源目录权限（umask 077 下 700/600），非特权 optimizer 用户
# 运行期需读 staging 装配 .opencode/skills，不开放读权限 with 臂 cp 会失败
COPY skills/llvm-mca-analysis/ /staging/skill/
RUN rm -rf /staging/skill/cases && chmod -R a+rX /staging/skill

COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/src/ /workspace/src/
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/bench/ /workspace/bench/
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/prompts/ /workspace/prompts/
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/verify.sh /workspace/verify.sh
COPY skills/llvm-mca-analysis/cases/agent-analyze-rust-asm-mca/entrypoint.sh /workspace/entrypoint.sh

# P1-d 所有权收敛：仅 /workspace/opt（agent 产物目录）交 optimizer；
# /workspace 根、results、src/bench/prompts、verify.sh、entrypoint.sh 保持
# root 拥有——agent 不再能 chmod/覆写 555 判定脚本与预置内容（原 chown -R
# 连 owner+555 的 chmod 击穿面一并交出）。entrypoint 以 root 起跑：agent 段
# 内部 setpriv 降权 optimizer，verify 段回 root 落盘判定文件（见 entrypoint.sh）。
RUN useradd -m optimizer \
    && mkdir -p /workspace/opt /workspace/results \
    && chown -R optimizer:optimizer /workspace/opt \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh \
    && sha256sum bench/main.rs src/kernel.rs verify.sh entrypoint.sh > /workspace/.pristine.sha256 \
    && chmod 444 bench/main.rs src/kernel.rs /workspace/.pristine.sha256 \
    && chmod 555 verify.sh entrypoint.sh \
    && chmod 755 /workspace /workspace/results

# P1-d：以 root 运行 entrypoint（降权在容器内部按段执行，不再依赖镜像 USER）
USER root

ENTRYPOINT ["/workspace/entrypoint.sh"]
