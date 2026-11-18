# recall-topk — 验证基准容器
# 本机 x86 上构建以验证 case 逻辑可编译可运行；
# 真实效果验证请在鲲鹏 aarch64 环境执行（build context 为 skill 根目录，即本 case 上一级）：
#   docker buildx build --platform linux/arm64 -f cases/recall-topk/Dockerfile -t kpbot-recall-topk-arm64 .
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# 编译工具必装；perf 依宿主内核版本不定，尽力安装
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ make curl ca-certificates \
 && apt-get install -y --no-install-recommends linux-generic-perf linux-perf \
    linux-tools-common || true \
 && rm -rf /var/lib/apt/lists/*

# opencode CLI
RUN curl -fsSL https://opencode.ai/install | bash || true
RUN cp /root/.opencode/bin/opencode /usr/local/bin/opencode 2>/dev/null || true

# 非 root 用户
RUN useradd -ms /bin/bash eval
USER eval
WORKDIR /home/eval
RUN mkdir -p .config/opencode/plugins .cannbot .local/share/opencode

# case 内容
USER root
WORKDIR /case
COPY cases/recall-topk/src/ ./src/
COPY cases/recall-topk/verify.sh ./verify.sh
COPY cases/recall-topk/prompts/ ./prompts/
COPY cases/recall-topk/run_eval.sh ./run_eval.sh
COPY cases/recall-topk/.opencode/ ./.opencode/
# skill 本体：sra-eval agent 加载 sra-dag-tuning skill 所需
COPY SKILL.md /case/.opencode/skills/sra-dag-tuning/SKILL.md
RUN chmod +x verify.sh run_eval.sh && mkdir -p /case/results && chown eval:eval /case/results && chown -R eval:eval /case

USER eval
ENV HOME=/home/eval
CMD ["./verify.sh"]
