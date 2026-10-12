FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

# Rust 工具链（同 case 1 策略：dnf 主路径，rustup pin 备选）
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        rustc cargo gcc binutils findutils coreutils glibc-headers shadow \
    && dnf clean all \
    && rustc --version && cargo --version

COPY skills/precision-transform/ /staging/skill/
COPY opencode/precision-transform/SKILL.md /staging/skill/SKILL.md
RUN rm -rf /staging/skill/cases

COPY skills/precision-transform/cases/agent-optimize-quant-dot/src/ /workspace/src/
COPY skills/precision-transform/cases/agent-optimize-quant-dot/bench/ /workspace/bench/
COPY skills/precision-transform/cases/agent-optimize-quant-dot/prompts/ /workspace/prompts/
COPY skills/precision-transform/cases/agent-optimize-quant-dot/verify.sh /workspace/verify.sh
COPY skills/precision-transform/cases/agent-optimize-quant-dot/entrypoint.sh /workspace/entrypoint.sh

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
