ARG BASE_IMAGE=openeuler/openeuler:24.03-lts
FROM ${BASE_IMAGE}

WORKDIR /workspace

# 诊断类用例只需 python3 + coreutils；agent 写入 opt/report.json，由 verify.sh 检查
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        python3 coreutils findutils shadow \
    && dnf clean all \
    && python3 --version

# 整个技能包装入 staging；entrypoint 按 SKILL_MODE + INNER_SKILL 复现 install.sh 的嵌套发现逻辑
COPY skills/kptools-agentenv-tuning/ /staging/pkg/
RUN rm -rf /staging/pkg/cases /staging/pkg/skills/*/cases \
    && chmod 700 /staging

COPY skills/kptools-agentenv-tuning/skills/agentenv-cluster-tlbi/cases/agent-triage-binding-not-tlbi/evidence/ /workspace/evidence/
COPY skills/kptools-agentenv-tuning/skills/agentenv-cluster-tlbi/cases/agent-triage-binding-not-tlbi/prompts/ /workspace/prompts/
COPY skills/kptools-agentenv-tuning/skills/agentenv-cluster-tlbi/cases/agent-triage-binding-not-tlbi/verify.sh /workspace/verify.sh
COPY skills/kptools-agentenv-tuning/skills/agentenv-cluster-tlbi/cases/agent-triage-binding-not-tlbi/entrypoint.sh /workspace/entrypoint.sh

ENV PKG_NAME=kptools-agentenv-tuning \
    INNER_SKILL=agentenv-cluster-tlbi \
    EXPECTED_SKILL=agentenv-cluster-tlbi

# The supervisor and grader remain root-owned; only opt/ is writable by the agent.
RUN useradd -m analyzer \
    && mkdir -p /workspace/opt /workspace/results \
    && chmod 755 /workspace \
    && chown analyzer:analyzer /workspace/opt \
    && chmod 700 /workspace/results \
    && chmod -R go-w /workspace/evidence /workspace/prompts \
    && chmod 500 verify.sh entrypoint.sh \
    && sha256sum verify.sh entrypoint.sh evidence/* prompts/* /staging/pkg/scripts/agent_case_supervisor.py > /workspace/.pristine.sha256 \
    && chmod 400 /workspace/.pristine.sha256

USER root

ENTRYPOINT ["/workspace/entrypoint.sh"]
