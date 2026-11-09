# Build context: KPBot 插件根目录（包含 skills/ 的目录）。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y python3 bash coreutils && dnf clean all

WORKDIR /workspace

# 只装入运行时 skill 内容：SKILL.md + references/ + scripts/。
# evals/、cases/ 是测试资产，绝不能被 agent 工作区看到。
COPY skills/rocksdb-perf-diagnostics/SKILL.md   /staging/skill/SKILL.md
COPY skills/rocksdb-perf-diagnostics/references /staging/skill/references
COPY skills/rocksdb-perf-diagnostics/scripts    /staging/skill/scripts

# 测试脚本/评分逻辑放在 /opt，不放 /workspace，避免 agent 直接读取。
COPY skills/rocksdb-perf-diagnostics/cases/agent-java-path/prompts /opt/prompts
COPY skills/rocksdb-perf-diagnostics/cases/agent-java-path/entrypoint.sh /opt/entrypoint.sh
COPY skills/rocksdb-perf-diagnostics/cases/agent-java-path/verify.sh /opt/verify.sh

RUN chmod +x /opt/entrypoint.sh /opt/verify.sh

CMD ["/opt/entrypoint.sh"]
