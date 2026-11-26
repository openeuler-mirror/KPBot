FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

# Rust 工具链：dnf 主路径（openEuler 24.03 官方仓库 rustc/cargo，1.75+ stable）。
# 备选 pin 版本（需网络可达 static.rust-lang.org，取消注释替换 dnf 行）：
#   RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
#       | sh -s -- -y --default-toolchain 1.85.0 --profile minimal \
#       && echo 'source $HOME/.cargo/env' >> /root/.bashrc
#   ENV PATH="/root/.cargo/bin:${PATH}"
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        rustc cargo gcc binutils findutils coreutils glibc-headers shadow \
    && dnf clean all \
    && rustc --version && cargo --version

# skill 装入 staging，运行时由 entrypoint 按 SKILL_MODE 决定是否接入 .opencode/skills/
COPY skills/operation-fusion/ /staging/skill/
COPY opencode/operation-fusion/SKILL.md /staging/skill/SKILL.md
RUN rm -rf /staging/skill/cases

# 待优化源码 + 基准 + 提示词 + 执行脚本
COPY skills/operation-fusion/cases/agent-optimize-fused-positivity/src/ /workspace/src/
COPY skills/operation-fusion/cases/agent-optimize-fused-positivity/bench/ /workspace/bench/
COPY skills/operation-fusion/cases/agent-optimize-fused-positivity/prompts/ /workspace/prompts/
COPY skills/operation-fusion/cases/agent-optimize-fused-positivity/verify.sh /workspace/verify.sh
COPY skills/operation-fusion/cases/agent-optimize-fused-positivity/entrypoint.sh /workspace/entrypoint.sh

# 非特权用户运行（useradd + 目录归属 + 可执行位一步完成）
# 构建期生成 pristine 源 sha256 清单（镜像层不可变）+ 防篡改 chmod（444/555 对 owner 也禁写）
RUN useradd -m optimizer \
    && mkdir -p /workspace/opt /workspace/results \
    && chown -R optimizer:optimizer /workspace \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh \
    && sha256sum bench/main.rs src/kernel.rs verify.sh entrypoint.sh > /workspace/.pristine.sha256 \
    && chmod 444 bench/main.rs src/kernel.rs /workspace/.pristine.sha256 \
    && chmod 555 verify.sh entrypoint.sh

USER optimizer

ENTRYPOINT ["/workspace/entrypoint.sh"]
