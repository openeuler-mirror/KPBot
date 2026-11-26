FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y --disablerepo='everything' --disablerepo='source'         --disablerepo='debuginfo' --disablerepo='update-source'         --setopt=install_weak_deps=0 \
        bash coreutils python3 findutils \
    && dnf clean all

COPY skills/kptools-bios-collector/ /staging/skill/
RUN rm -rf /staging/skill/cases

COPY skills/kptools-bios-collector/cases/agent-bios-trigger/prompts/ /workspace/prompts/
COPY skills/kptools-bios-collector/cases/agent-bios-trigger/entrypoint.sh /workspace/entrypoint.sh
RUN mkdir -p /workspace/results && chmod +x /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
