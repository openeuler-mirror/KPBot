FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y --disablerepo='everything' --disablerepo='source'         --disablerepo='debuginfo' --disablerepo='update-source'         --setopt=install_weak_deps=0 \
        bash coreutils findutils procps-ng iproute curl \
        java-17-openjdk-devel \
    && dnf clean all

# arthas-boot.jar（官方分发包；来源与摘要可用构建参数覆盖，覆盖时必须同时提供
# ARTHAS_BOOT_SHA256，摘要不匹配直接构建失败 —— 下载的可执行产物先校验再执行）
ARG ARTHAS_BOOT_URL=https://arthas.aliyun.com/arthas-boot.jar
ARG ARTHAS_BOOT_SHA256=94e63f4b38dc1c54316ffc1ecb0d8f5b868ade4f285094c351113aeb0113ee19
RUN mkdir -p /opt/arthas \
    && curl -fsSL -o /opt/arthas/arthas-boot.jar "${ARTHAS_BOOT_URL}" \
    && echo "${ARTHAS_BOOT_SHA256}  /opt/arthas/arthas-boot.jar" | sha256sum -c -

# 预热：首次 attach 会从 maven 仓库拉取完整 arthas 包缓存到 ~/.arthas，
# 构建期完成预热后容器运行期可离线 attach（离线要点见 references/arthas-guide.md §2.3）。
# 注意：JVM PID 用启动时记录的 $!（pid 文件）精确定位 —— pgrep -f 'java Sleeper' 会
# 匹配到本 RUN 的构建 shell 自身（其命令行含该字面量且 PID 更小），导致 attach 失败；
# 预热链路整体 fail-fast（任一步失败即构建失败），并用完整包缓存（arthas-core.jar）
# 断言预热确实生效，防止“预热静默失败 + 运行期离线 attach 挂死”。
RUN printf 'public class Sleeper { public static void main(String[] a) throws Exception { Thread.sleep(600000L); } }\n' > /tmp/Sleeper.java \
    && javac -d /tmp /tmp/Sleeper.java \
    && cd /tmp \
    && (java Sleeper & echo $! > /tmp/sleeper.pid) \
    && sleep 4 \
    && timeout 240 java -jar /opt/arthas/arthas-boot.jar "$(cat /tmp/sleeper.pid)" -b -c 'version' \
    && test -n "$(find /root/.arthas/lib -name arthas-core.jar -print -quit 2>/dev/null)"

ENV ARTHAS_BOOT_JAR=/opt/arthas/arthas-boot.jar

COPY skills/kptools-arthas/ /staging/skill/
# 容器内 SKILL.md 用 opencode/ 变体（叠加 agent 安全约束节，与主 SKILL.md 同源维护）
COPY opencode/kptools-arthas/SKILL.md /staging/skill/SKILL.md
RUN rm -rf /staging/skill/cases

COPY skills/kptools-arthas/cases/agent-arthas-trigger/prompts/ /workspace/prompts/
COPY skills/kptools-arthas/cases/agent-arthas-trigger/entrypoint.sh /workspace/entrypoint.sh
RUN mkdir -p /workspace/results && chmod +x /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
