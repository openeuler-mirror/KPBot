FROM openeuler/openeuler:24.03-lts

# Tier-B toolchain: C compiler for the fixture, binutils for the codegen check.
RUN dnf install -y \
        --disablerepo='everything' \
        --disablerepo='source' \
        --disablerepo='debuginfo' \
        --disablerepo='update-source' \
        --setopt=install_weak_deps=0 \
        gcc binutils make findutils coreutils python3 \
    && dnf clean all \
    && gcc --version && objdump --version | head -1

WORKDIR /workspace

# Build context is the plugin root (docs/case-spec.md §2).
COPY skills/hot-path-overhead-elimination/cases/append-notify/ /workspace/

RUN chmod +x /workspace/run.sh

ENTRYPOINT ["/bin/bash", "/workspace/run.sh"]
