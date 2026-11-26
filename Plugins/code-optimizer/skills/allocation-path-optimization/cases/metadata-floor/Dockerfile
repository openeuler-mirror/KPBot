FROM openeuler/openeuler:24.03-lts

# Tier-B toolchain: C compiler for the fixture, binutils for symbol sanity.
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        gcc binutils make findutils coreutils util-linux gawk python3 \
    && dnf clean all \
    && gcc --version

WORKDIR /workspace

# Build context is the skill root; the case and the probe are copied from there.
COPY skills/allocation-path-optimization/cases/metadata-floor/ /workspace/
COPY skills/allocation-path-optimization/scripts/ /workspace/scripts/

RUN chmod +x /workspace/run.sh

ENTRYPOINT ["/bin/bash", "/workspace/run.sh"]
