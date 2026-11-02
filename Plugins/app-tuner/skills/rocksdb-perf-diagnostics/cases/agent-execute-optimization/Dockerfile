# Build context: KPBot 插件根目录（包含 skills/ 的目录）。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y python3 bash coreutils && dnf clean all

WORKDIR /workspace

COPY skills/rocksdb-perf-diagnostics /staging/skill

COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/prompts /workspace/prompts
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/fixtures /workspace/fixtures
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/entrypoint.sh /workspace/entrypoint.sh
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/verify.sh /workspace/verify.sh

RUN chmod +x /workspace/entrypoint.sh /workspace/verify.sh /workspace/fixtures/mock_db_bench.sh

CMD ["/workspace/entrypoint.sh"]
