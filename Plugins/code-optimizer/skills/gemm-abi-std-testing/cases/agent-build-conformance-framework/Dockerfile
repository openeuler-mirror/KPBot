# agent-build-conformance-framework —— 端到端 agent 级用例镜像
#
# 构建上下文 = Plugins/code-optimizer/（与 docs/case-spec.md 一致）
# 构建（本机容器无法解析 openeuler 源时需 --network host）：
#   docker build --network host \
#       -f skills/gemm-abi-std-testing/cases/agent-build-conformance-framework/Dockerfile \
#       -t kpbot/code-optimizer-agent-conformance:1.0-openeuler24.03 .
# A/B 对照：
#   ./run_ab.sh --save results-ab
FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

# 工具链与运行时：编译验证用 gcc/binutils/make；verify/entrypoint 用 python3
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        gcc binutils make findutils coreutils python3 \
    && dnf clean all

# skill 装入 staging；运行时由 entrypoint 按 SKILL_MODE 决定是否接入 .opencode/skills/
COPY skills/gemm-abi-std-testing/ /staging/skill/
RUN rm -rf /staging/skill/cases

# case fixture（DUT + oracle + 标准文本）+ 提示词 + 执行脚本
COPY skills/gemm-abi-std-testing/cases/agent-build-conformance-framework/src/ /workspace/src/
COPY skills/gemm-abi-std-testing/cases/agent-build-conformance-framework/bench/ /workspace/bench/
COPY skills/gemm-abi-std-testing/cases/agent-build-conformance-framework/std/ /workspace/std/
COPY skills/gemm-abi-std-testing/cases/agent-build-conformance-framework/prompts/ /workspace/prompts/
COPY skills/gemm-abi-std-testing/cases/agent-build-conformance-framework/verify.sh /workspace/verify.sh
COPY skills/gemm-abi-std-testing/cases/agent-build-conformance-framework/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/opt/conformance /workspace/results \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
