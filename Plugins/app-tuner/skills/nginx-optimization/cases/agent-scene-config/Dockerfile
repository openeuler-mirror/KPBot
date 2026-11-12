# Build context: KPBot 插件根目录（包含 skills/ 的目录）。
# P1 教训（PR#100 检视）：被测 skill 与评分材料必须分离——本文件只 COPY
# SKILL.md 与 references/，绝不把 evals/（评分答案）或 cases/ 带进镜像。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y python3 bash coreutils && dnf clean all

WORKDIR /workspace

COPY skills/nginx-optimization/SKILL.md /staging/skill/SKILL.md
COPY skills/nginx-optimization/references /staging/skill/references

COPY skills/nginx-optimization/cases/agent-scene-config/prompts /workspace/prompts
COPY skills/nginx-optimization/cases/agent-scene-config/entrypoint.sh /workspace/entrypoint.sh
COPY skills/nginx-optimization/cases/agent-scene-config/verify.sh /workspace/verify.sh

RUN chmod +x /workspace/entrypoint.sh /workspace/verify.sh

CMD ["/workspace/entrypoint.sh"]
