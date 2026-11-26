FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY skills/agentic-infra/kptools-agentenv-tuning/skills/kptools-kernel-switch/ ./
RUN install -d -o 65534 -g 65534 -m 0700 results
USER 65534:65534
CMD ["python3", "cases/workflow-contract/run.py", "--results-dir", "results"]
