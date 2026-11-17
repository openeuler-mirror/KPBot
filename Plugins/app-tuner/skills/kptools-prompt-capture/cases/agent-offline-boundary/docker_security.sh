#!/usr/bin/env bash
# Shared by the A/B runner and the disposable-container regression test.
CASE_DOCKER_SECURITY_ARGS=(
    --cap-drop=ALL
    --cap-add=CHOWN
    --cap-add=SETUID
    --cap-add=SETGID
    --cap-add=KILL
    --cap-add=DAC_OVERRIDE
    --security-opt=no-new-privileges
    --pids-limit=256
    --cpus=2
    --memory=1g
)
