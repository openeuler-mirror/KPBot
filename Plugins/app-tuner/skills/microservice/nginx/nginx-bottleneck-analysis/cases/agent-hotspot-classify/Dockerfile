# Build context: KPBot 插件根目录（包含 skills/ 的目录）。
# P1 教训：只 COPY SKILL.md 与 references/，评分材料（evals/、cases/）不进镜像。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y python3 bash coreutils && dnf clean all

WORKDIR /workspace

COPY skills/microservice/nginx/nginx-bottleneck-analysis/SKILL.md /staging/skill/SKILL.md
COPY skills/microservice/nginx/nginx-bottleneck-analysis/references /staging/skill/references

COPY skills/microservice/nginx/nginx-bottleneck-analysis/cases/agent-hotspot-classify/prompts /workspace/prompts
COPY skills/microservice/nginx/nginx-bottleneck-analysis/cases/agent-hotspot-classify/fixtures /workspace/fixtures
COPY skills/microservice/nginx/nginx-bottleneck-analysis/cases/agent-hotspot-classify/entrypoint.sh /workspace/entrypoint.sh
COPY skills/microservice/nginx/nginx-bottleneck-analysis/cases/agent-hotspot-classify/verify.sh /workspace/verify.sh

RUN chmod +x /workspace/entrypoint.sh /workspace/verify.sh

CMD ["/workspace/entrypoint.sh"]
