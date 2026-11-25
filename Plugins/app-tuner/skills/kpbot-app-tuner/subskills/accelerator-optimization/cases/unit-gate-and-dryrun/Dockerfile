# tier A 纯逻辑用例环境：python3 标准库 + bash。
# 被测对象：证据门控（合成 snapshot_metadata.json 夹具）与 enable dry-run
# 只读性，不需要 HPRE 硬件、mock 工具或网络。
FROM openeuler/openeuler:24.03-lts

RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        python3 \
    && dnf clean all

# 被测 skill 装入 staging（scripts/ + SKILL.md），cases 不随镜像分发
COPY skills/kpbot-app-tuner/subskills/accelerator-optimization/ /staging/skill/
RUN rm -rf /staging/skill/cases

# 用例自身：单测 + 执行器
COPY skills/kpbot-app-tuner/subskills/accelerator-optimization/cases/unit-gate-and-dryrun/tests/ /workspace/tests/
COPY skills/kpbot-app-tuner/subskills/accelerator-optimization/cases/unit-gate-and-dryrun/entrypoint.sh /workspace/entrypoint.sh

RUN mkdir -p /workspace/results && chmod +x /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
