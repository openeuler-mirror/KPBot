FROM openeuler/openeuler:24.03-lts
WORKDIR /workspace
RUN dnf install -y python3 bash coreutils findutils && dnf clean all
COPY skills/kptools-prompt-capture/ /staging/skill/
COPY skills/kptools-prompt-capture/cases/agent-offline-boundary/ /workspace/
RUN mkdir -p results input && chmod -R a-w /workspace/prompts
ENTRYPOINT ["bash", "/workspace/entrypoint.sh"]
