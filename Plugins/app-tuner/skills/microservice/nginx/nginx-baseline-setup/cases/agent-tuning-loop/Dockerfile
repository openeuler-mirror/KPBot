# Build context: Plugins/app-tuner/（包含 skills/ 的目录）。
# P1 教训：只 COPY SKILL.md 与 references/，评分材料（evals/、cases/）不进镜像。
#
# 安全模型（docs/agent-case-security-notes.md）：
#   - 评分链（entrypoint.sh / verify.sh / prompts / fixtures）root 拥有，agent 只读；
#   - agent 以非特权用户 runner 运行；results/ 为 root 700（判定产物），
#     agent 只写 work/（runner 700，report/base.conf/mock 日志）；
#   - 构建期生成 .pristine.sha256，entrypoint 在 agent 前后各校验一次。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        python3 bash coreutils findutils util-linux shadow \
    && dnf clean all

WORKDIR /workspace

COPY skills/microservice/nginx/nginx-baseline-setup/SKILL.md /staging/skill/SKILL.md
COPY skills/microservice/nginx/nginx-baseline-setup/references /staging/skill/references

COPY skills/microservice/nginx/nginx-baseline-setup/cases/agent-tuning-loop/prompts /workspace/prompts
COPY skills/microservice/nginx/nginx-baseline-setup/cases/agent-tuning-loop/fixtures /workspace/fixtures
COPY skills/microservice/nginx/nginx-baseline-setup/cases/agent-tuning-loop/entrypoint.sh /workspace/entrypoint.sh
COPY skills/microservice/nginx/nginx-baseline-setup/cases/agent-tuning-loop/verify.sh /workspace/verify.sh

# 评分链 root 拥有且不可写；agent 以 runner 运行。results/ 仅 root 可读写，
# agent 的可写面收敛到 work/（report/base.conf/mock 日志）。
RUN useradd -m -s /bin/bash runner \
    && mkdir -p /workspace/results /workspace/work \
    && chmod 755 /workspace \
    && chmod 700 /workspace/results \
    && chown runner:runner /workspace/work \
    && chmod 700 /workspace/work \
    && chmod 500 /workspace/entrypoint.sh /workspace/verify.sh \
    && chmod 555 /workspace/prompts /workspace/fixtures \
    && chmod 444 /workspace/prompts/with-skill.md /workspace/prompts/without-skill.md \
    && chmod 555 /workspace/fixtures/mock_wrk.sh \
    && cd /workspace \
    && sha256sum verify.sh entrypoint.sh prompts/with-skill.md prompts/without-skill.md \
         fixtures/mock_wrk.sh > .pristine.sha256 \
    && chmod 400 .pristine.sha256

USER root

CMD ["/workspace/entrypoint.sh"]
