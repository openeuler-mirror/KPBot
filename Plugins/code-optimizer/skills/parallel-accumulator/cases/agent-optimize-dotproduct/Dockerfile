FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        gcc binutils findutils coreutils glibc-headers shadow \
    && dnf clean all

# 被测 skill 与评分材料分离：只 COPY SKILL.md 与 references/，
# 绝不把 evals/（评分答案）或 cases/ 带进镜像（with 臂不可见评分材料）。
COPY skills/parallel-accumulator/SKILL.md /staging/skill/SKILL.md
COPY skills/parallel-accumulator/references /staging/skill/references

COPY skills/parallel-accumulator/cases/agent-optimize-dotproduct/src/ /workspace/src/
COPY skills/parallel-accumulator/cases/agent-optimize-dotproduct/bench/ /workspace/bench/
COPY skills/parallel-accumulator/cases/agent-optimize-dotproduct/prompts/ /workspace/prompts/
COPY skills/parallel-accumulator/cases/agent-optimize-dotproduct/verify.sh /workspace/verify.sh
COPY skills/parallel-accumulator/cases/agent-optimize-dotproduct/entrypoint.sh /workspace/entrypoint.sh

# 非特权用户运行 + 完整性清单（构建期生成 pristine sha256，镜像层不可变，防篡改）
RUN useradd -m optimizer \
    && mkdir -p /workspace/opt /workspace/results \
    && chown -R optimizer:optimizer /workspace \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh \
    && sha256sum bench/main.c src/kernel.c verify.sh entrypoint.sh > /workspace/.pristine.sha256 \
    && chmod 444 bench/main.c src/kernel.c /workspace/.pristine.sha256 \
    && chmod 555 verify.sh entrypoint.sh

USER optimizer

ENTRYPOINT ["/workspace/entrypoint.sh"]