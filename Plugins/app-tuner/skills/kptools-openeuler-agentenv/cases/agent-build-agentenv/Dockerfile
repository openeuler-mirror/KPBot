# agent-build-agentenv —— 端到端 agent 级用例镜像
#
# 构建上下文 = Plugins/app-tuner/（与 docs/case-spec.md 一致）
# 构建（容器内解析 openeuler 源 / github 需稳定出网时加 --network host）：
#   docker build --network host \
#       -f skills/kptools-openeuler-agentenv/cases/agent-build-agentenv/Dockerfile \
#       -t kpbot/app-tuner-agent-agent-build-agentenv:1.0-openeuler24.03 .
# A/B 对照：
#   ./run_ab.sh --save results-ab
#
# 注意：镜像内只装基础工具（git/curl/python3/tar 等）；
# Rust 工具链、构建依赖（clang-devel / protobuf-* 等）与 AgentENV 源码均由
# agent 在运行时按任务自行获取 / 安装——这正是被测能力。
FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        git curl findutils coreutils tar gzip xz python3 \
    && dnf clean all

# skill 装入 staging；运行时由 entrypoint 按 SKILL_MODE 决定是否接入 .opencode/skills/
COPY skills/kptools-openeuler-agentenv/ /staging/skill/
RUN rm -rf /staging/skill/cases

# 提示词 + 执行脚本（无 src/bench：待编译源 = 上游 AgentENV 仓库，agent 自行获取）
COPY skills/kptools-openeuler-agentenv/cases/agent-build-agentenv/prompts/ /workspace/prompts/
COPY skills/kptools-openeuler-agentenv/cases/agent-build-agentenv/verify.sh /workspace/verify.sh
COPY skills/kptools-openeuler-agentenv/cases/agent-build-agentenv/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/opt /workspace/results \
    && chmod +x /workspace/verify.sh /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
