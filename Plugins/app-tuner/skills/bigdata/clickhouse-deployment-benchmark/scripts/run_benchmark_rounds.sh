#!/usr/bin/env bash
# Run clickhouse-benchmark warmup and measured rounds into a fresh directory.

set -eu

usage() {
    local status=${1:-2}
    cat >&2 <<'EOF'
usage: run_benchmark_rounds.sh --output DIR [--rounds N] [--client PATH]
       [--subcommand NAME] -- BENCHMARK_ARGS...

Creates DIR exclusively, writes warmup.log, then round-1.log ... round-N.log.
Exit codes: 0 all rounds passed, 1 a benchmark run failed, 2 usage error,
3 output directory already exists or cannot be created.
EOF
    exit "$status"
}

output=""
rounds=3
client="clickhouse-benchmark"
subcommand=""
while (($#)); do
    case "$1" in
        --output)
            (($# >= 2)) || usage
            output=$2
            shift 2
            ;;
        --rounds)
            (($# >= 2)) || usage
            rounds=$2
            shift 2
            ;;
        --client)
            (($# >= 2)) || usage
            client=$2
            shift 2
            ;;
        --subcommand)
            (($# >= 2)) || usage
            subcommand=$2
            shift 2
            ;;
        --)
            shift
            break
            ;;
        --help | -h)
            usage 0
            ;;
        *)
            usage
            ;;
    esac
done

[[ -n $output ]] || usage
[[ $rounds =~ ^[1-9][0-9]*$ ]] || {
    echo "--rounds must be a positive integer" >&2
    exit 2
}
(($# > 0)) || {
    echo "benchmark arguments are required after --" >&2
    exit 2
}

parent=$(dirname "$output")
if ! mkdir -p "$parent" 2>/dev/null; then
    echo "output parent directory cannot be created: $parent" >&2
    exit 3
fi
if ! mkdir "$output" 2>/dev/null; then
    echo "output directory already exists or cannot be created: $output" >&2
    exit 3
fi

if [[ -n $subcommand ]]; then
    set -- "$subcommand" "$@"
fi

run_round() {
    local name=$1
    shift
    if ! "$client" "$@" >"$output/$name.log" 2>&1; then
        echo "benchmark failed for $name; see $output/$name.log" >&2
        return 1
    fi
}

run_round warmup "$@" || exit 1
for ((round = 1; round <= rounds; round++)); do
    run_round "round-$round" "$@" || exit 1
done

echo "wrote $output/warmup.log and $rounds measured round logs"
