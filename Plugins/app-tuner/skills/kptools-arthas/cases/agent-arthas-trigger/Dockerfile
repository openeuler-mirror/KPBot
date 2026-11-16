FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

RUN dnf install -y --disablerepo='everything' --disablerepo='source'         --disablerepo='debuginfo' --disablerepo='update-source'         --setopt=install_weak_deps=0 \
        bash coreutils findutils procps-ng curl \
        java-17-openjdk-devel \
    && dnf clean all

# arthas-boot.jar（官方分发包；来源可用构建参数覆盖）
ARG ARTHAS_BOOT_URL=https://arthas.aliyun.com/arthas-boot.jar
RUN mkdir -p /opt/arthas \
    && curl -fsSL -o /opt/arthas/arthas-boot.jar "${ARTHAS_BOOT_URL}"

# 预热：首次 attach 会从 maven 仓库拉取完整 arthas 包缓存到 ~/.arthas，
# 构建期完成预热后容器运行期可离线 attach（离线要点见 references/arthas-guide.md §2.3）
RUN printf 'public class Sleeper { public static void main(String[] a) throws Exception { Thread.sleep(600000L); } }\n' > /tmp/Sleeper.java \
    && javac -d /tmp /tmp/Sleeper.java \
    && cd /tmp && (java Sleeper &) \
    && sleep 4 \
    && java -jar /opt/arthas/arthas-boot.jar $(pgrep -f 'java Sleeper' | head -1) -b -c 'version' \
    ; pkill -f 'java Sleeper' || true

ENV ARTHAS_BOOT_JAR=/opt/arthas/arthas-boot.jar

COPY skills/kptools-arthas/ /staging/skill/
RUN rm -rf /staging/skill/cases

COPY skills/kptools-arthas/cases/agent-arthas-trigger/prompts/ /workspace/prompts/
COPY skills/kptools-arthas/cases/agent-arthas-trigger/entrypoint.sh /workspace/entrypoint.sh
RUN mkdir -p /workspace/results && chmod +x /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
