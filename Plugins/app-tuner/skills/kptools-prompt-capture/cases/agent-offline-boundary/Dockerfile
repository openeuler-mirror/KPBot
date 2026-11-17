FROM openeuler/openeuler:24.03-lts
WORKDIR /workspace
RUN dnf install -y python3 bash coreutils findutils shadow && dnf clean all
COPY skills/kptools-prompt-capture/ /staging/skill/
COPY skills/kptools-prompt-capture/cases/agent-offline-boundary/ /harness/
RUN useradd -m analyzer \
    && mkdir -p /workspace/opt /workspace/results /run/secrets \
    && chown analyzer:analyzer /workspace/opt \
    && chmod 700 /workspace/opt /workspace/results /run/secrets /staging /harness \
    && chmod 755 /workspace \
    && rm -rf /staging/skill/cases /staging/skill/evals \
    && chmod 500 /harness/*.py /harness/*.sh \
    && find /harness /staging/skill -type f ! -name pristine.sha256 -print0 \
       | sort -z | xargs -0 sha256sum > /harness/pristine.sha256 \
    && chmod 400 /harness/pristine.sha256
# Only the supervisor/grader run as root; the agent runs as analyzer.
USER root
ENTRYPOINT ["bash", "/harness/entrypoint.sh"]
