# Build context: KPBot 插件根目录（包含 skills/ 的目录）。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y python3 bash coreutils && dnf clean all

WORKDIR /workspace

# skill 装入 /staging/skill，entrypoint 在 with 模式下复制到 .opencode/skills/
COPY skills/rocksdb-perf-diagnostics /staging/skill

COPY skills/rocksdb-perf-diagnostics/cases/agent-java-path/prompts /workspace/prompts
COPY skills/rocksdb-perf-diagnostics/cases/agent-java-path/entrypoint.sh /workspace/entrypoint.sh
COPY skills/rocksdb-perf-diagnostics/cases/agent-java-path/verify.sh /workspace/verify.sh

RUN chmod +x /workspace/entrypoint.sh /workspace/verify.sh

CMD ["/workspace/entrypoint.sh"]
