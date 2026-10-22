FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y --disablerepo='everything' --disablerepo='source'         --disablerepo='debuginfo' --disablerepo='update-source'         --setopt=install_weak_deps=0 \
        bash coreutils python3 python3-pip findutils \
    && pip3 install --no-cache-dir py-spy \
    && dnf clean all

COPY skills/kptools-py-spy/ /staging/skill/
RUN rm -rf /staging/skill/cases

COPY skills/kptools-py-spy/cases/agent-pyspy-trigger/prompts/ /workspace/prompts/
COPY skills/kptools-py-spy/cases/agent-pyspy-trigger/entrypoint.sh /workspace/entrypoint.sh
RUN mkdir -p /workspace/results && chmod +x /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
