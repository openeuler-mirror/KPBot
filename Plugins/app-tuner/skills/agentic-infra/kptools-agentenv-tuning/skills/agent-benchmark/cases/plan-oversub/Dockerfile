FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 python3-yaml \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY skills/agentic-infra/kptools-agentenv-tuning/skills/agent-benchmark/ ./
USER 65534:65534
CMD ["python3", "cases/plan-oversub/run.py"]
