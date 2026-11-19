FROM openeuler/openeuler:24.03-lts

WORKDIR /workspace

# agent 段以非 root 用户运行（docs/agent-case-security-notes.md 规则 3：
# entrypoint 以 root 起跑、内部按段降权；评分链文件保持 root 拥有）
RUN dnf install -y --disablerepo='everything' --disablerepo='source'         --disablerepo='debuginfo' --disablerepo='update-source'         --setopt=install_weak_deps=0 \
        bash coreutils findutils procps-ng iproute curl util-linux shadow-utils \
        java-17-openjdk-devel \
    && dnf clean all \
    && useradd -m arthas-agent

# arthas-boot.jar（官方分发包）。URL 与摘要为脚本内常量、不做 ARG：
# --build-arg 可改写期望摘要会使校验退化为自证（评审 P2），下载的可执行产物
# 的信任锚不可被构建参数覆盖；升级 arthas 版本时同步更新摘要常量。
RUN mkdir -p /opt/arthas \
    && curl -fsSL -o /opt/arthas/arthas-boot.jar https://arthas.aliyun.com/arthas-boot.jar \
    && echo "94e63f4b38dc1c54316ffc1ecb0d8f5b868ade4f285094c351113aeb0113ee19  /opt/arthas/arthas-boot.jar" | sha256sum -c -

# 预热：首次 attach 会从 maven 仓库拉取完整 arthas 包缓存到 ~/.arthas，
# 构建期完成预热后容器运行期可离线 attach（离线要点见 references/arthas-guide.md §2.3）。
# 以 arthas-agent 用户执行：缓存落在 /home/arthas-agent/.arthas，运行期 agent 同 uid
# 可直接复用（attach 要求同 uid）。
# 注意：JVM PID 用启动时记录的 $!（pid 文件）精确定位 —— pgrep -f 'java Sleeper' 会
# 匹配到构建 shell 自身（其命令行含该字面量且 PID 更小），导致 attach 失败；
# 预热链路整体 fail-fast（任一步失败即构建失败），并用完整包缓存（arthas-core.jar）
# 断言预热确实生效，防止“预热静默失败 + 运行期离线 attach 挂死”。
RUN printf 'public class Sleeper { public static void main(String[] a) throws Exception { Thread.sleep(600000L); } }\n' > /tmp/Sleeper.java \
    && javac -d /tmp /tmp/Sleeper.java
RUN setpriv --reuid=arthas-agent --regid=arthas-agent --init-groups \
    env HOME=/home/arthas-agent bash -c 'cd /tmp && (java Sleeper & echo $! > /tmp/sleeper.pid) && sleep 4 \
    && timeout 240 java -jar /opt/arthas/arthas-boot.jar "$(cat /tmp/sleeper.pid)" -b -c "version" \
    && test -n "$(find "$HOME/.arthas/lib" -name arthas-core.jar -print -quit 2>/dev/null)"'

ENV ARTHAS_BOOT_JAR=/opt/arthas/arthas-boot.jar

COPY skills/kptools-arthas/ /staging/skill/
RUN rm -rf /staging/skill/cases

COPY skills/kptools-arthas/cases/agent-arthas-trigger/prompts/ /workspace/prompts/
COPY skills/kptools-arthas/cases/agent-arthas-trigger/entrypoint.sh /workspace/entrypoint.sh
RUN mkdir -p /workspace/results && chmod +x /workspace/entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
