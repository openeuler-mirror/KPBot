#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
shared="$script_dir/../../sandbox-timeline-profile/scripts/run_split_skill.sh"
if [[ ! -f "$shared" ]]; then
  printf 'BLOCKED: shared sandbox-timeline-profile scripts are unavailable; install that Skill alongside this one\n' >&2
  exit 2
fi
exec bash "$shared" report "$@"
