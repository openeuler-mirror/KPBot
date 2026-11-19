# Build context: KPBot 插件根目录（包含 skills/ 的目录）。
# P1 教训：只 COPY SKILL.md 与 references/，评分材料（evals/、cases/）不进镜像。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y python3 bash coreutils && dnf clean all

WORKDIR /workspace

COPY skills/nginx-optimization/SKILL.md /staging/skill/SKILL.md
COPY skills/nginx-optimization/references /staging/skill/references

COPY skills/nginx-optimization/cases/agent-tuning-loop/prompts /workspace/prompts
COPY skills/nginx-optimization/cases/agent-tuning-loop/fixtures /workspace/fixtures
COPY skills/nginx-optimization/cases/agent-tuning-loop/entrypoint.sh /workspace/entrypoint.sh
COPY skills/nginx-optimization/cases/agent-tuning-loop/verify.sh /workspace/verify.sh

RUN chmod +x /workspace/entrypoint.sh /workspace/verify.sh /workspace/fixtures/mock_wrk.sh

CMD ["/workspace/entrypoint.sh"]
