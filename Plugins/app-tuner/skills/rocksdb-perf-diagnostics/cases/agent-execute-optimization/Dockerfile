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
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/prompts /opt/prompts
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/entrypoint.sh /opt/entrypoint.sh
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/verify.sh /opt/verify.sh

# mock 基准是 agent 的输入，保留在 /workspace。
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/fixtures /workspace/fixtures

# 权限配置预置进镜像，避免只读根上运行时写失败导致 deny 静默缺失。
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/opencode.json /workspace/opencode.json
COPY skills/rocksdb-perf-diagnostics/cases/agent-execute-optimization/opencode.json /workspace/.opencode/opencode.json

RUN chmod +x /opt/entrypoint.sh /opt/verify.sh /workspace/fixtures/mock_db_bench.sh

CMD ["/opt/entrypoint.sh"]
