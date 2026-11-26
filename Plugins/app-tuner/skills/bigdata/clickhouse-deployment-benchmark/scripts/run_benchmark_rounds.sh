#!/usr/bin/env bash
# Run clickhouse-benchmark warmup and measured rounds into a fresh directory.

set -eu

usage() {
    local status=${1:-2}
    cat >&2 <<'EOF'
usage: run_benchmark_rounds.sh --output DIR [--rounds N] [--query-file FILE]
       [--client PATH] [--subcommand NAME] -- BENCHMARK_ARGS...

Creates DIR exclusively, writes warmup.log, then round-1.log ... round-N.log.
Use either --query-file or the client's --query; standard input is not supported.
BENCHMARK_ARGS must include a positive --iterations or --timelimit.
Exit codes: 0 all rounds passed, 1 a benchmark run failed, 2 usage error,
3 output directory already exists or cannot be created.
EOF
    exit "$status"
}

usage_error() {
    echo "$1" >&2
    exit 2
}

output=""
rounds=3
query_file=""
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
        --query-file)
            (($# >= 2)) || usage
            [[ -z $query_file ]] || usage_error "--query-file may be specified only once"
            [[ -n $2 ]] || usage_error "--query-file requires a non-empty path"
            query_file=$2
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

if [[ -n $query_file ]] && [[ ! -f $query_file || ! -r $query_file ]]; then
    echo "query file is not a readable regular file: $query_file" >&2
    exit 2
fi

benchmark_args=("$@")
has_query=0
iterations_positive=0
timelimit_positive=0

validate_iterations() {
    [[ $1 =~ ^[0-9]+$ ]] || return 1
    iterations_positive=0
    if [[ $1 =~ [1-9] ]]; then
        iterations_positive=1
    fi
    return 0
}

validate_timelimit() {
    local positive
    [[ $1 =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$ ]] || return 1
    positive=$(LC_ALL=C awk -v value="$1" 'BEGIN {
        number = value + 0
        if (number < 0 || number > 1.7976931348623157e308)
            exit 1
        print (number > 0 ? 1 : 0)
    }') || return 1
    timelimit_positive=0
    if [[ $positive == 1 ]]; then
        timelimit_positive=1
    fi
    return 0
}

validate_limit() {
    if [[ $1 == --iterations || $1 == -i ]]; then
        validate_iterations "$2" || usage_error "--iterations must be a non-negative integer"
    else
        validate_timelimit "$2" || usage_error "--timelimit must be a non-negative finite number"
    fi
}

while (($#)); do
    case "$1" in
        --)
            break
            ;;
        --query | -q)
            has_query=1
            (($# >= 2)) || usage_error "$1 requires a query"
            [[ -n $2 ]] || usage_error "$1 requires a non-empty query"
            shift 2
            continue
            ;;
        --query=* | -q?*)
            has_query=1
            [[ -n ${1#*=} ]] || usage_error "--query requires a non-empty query"
            shift
            continue
            ;;
        --iterations | -i | --timelimit | -t)
            option=$1
            (($# >= 2)) || usage_error "$option requires a value"
            value=$2
            shift
            ;;
        --iterations=* | --timelimit=*)
            option=${1%%=*}
            value=${1#*=}
            ;;
        -i?* | -t?*)
            option=${1:0:2}
            value=${1:2}
            ;;
        *)
            shift
            continue
            ;;
    esac
    validate_limit "$option" "$value"
    shift
done
set -- "${benchmark_args[@]}"

if [[ -n $query_file && $has_query -eq 1 ]]; then
    usage_error "--query-file cannot be combined with the client's --query"
fi
if [[ -z $query_file && $has_query -eq 0 ]]; then
    usage_error "provide --query-file or the client's --query; standard input is not supported"
fi
((iterations_positive || timelimit_positive)) || usage_error "benchmark requires a positive --iterations or --timelimit"

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
    local status=0
    shift
    if [[ -n $query_file ]]; then
        "$client" "$@" <"$query_file" >"$output/$name.log" 2>&1 || status=$?
    else
        "$client" "$@" >"$output/$name.log" 2>&1 || status=$?
    fi
    if ((status != 0)); then
        echo "benchmark failed for $name; see $output/$name.log" >&2
        return 1
    fi
}

run_round warmup "$@" || exit 1
for ((round = 1; round <= rounds; round++)); do
    run_round "round-$round" "$@" || exit 1
done

echo "wrote $output/warmup.log and $rounds measured round logs"
