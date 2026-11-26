#!/bin/bash
# usage: run_one.sh <name> <server_flags> <bench_flags> <server_cpus> <client_cpus> <gomaxprocs> <gogc> [port]
set -u
NAME=$1; SFLAGS=$2; BFLAGS=$3; SCPUS=$4; CCPUS=$5; GMP=$6; GOGCV=$7; PORT=${8:-8888}
ROOT=$(cd "$(dirname "$0")" && pwd)
RESULT_DIR="$ROOT/results"

case "$NAME" in
  ''|*/*|*\\*|*..*)
    echo "invalid name: '$NAME' (use a single path component, e.g. base_c128)" >&2
    exit 2
    ;;
esac

mkdir -p "$RESULT_DIR"
CLOG="$RESULT_DIR/${NAME}.server.log"
BLOG="$RESULT_DIR/${NAME}.bench.log"
BJSON="$RESULT_DIR/${NAME}.bench.json"
rm -f "$BJSON" "$CLOG" "$BLOG"

if (echo > "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "port $PORT is already in use; choose another port" >&2
  exit 3
fi

GOMAXPROCS=$GMP GOGC=$GOGCV GODEBUG=gctrace=1 taskset -c "$SCPUS" \
  "$ROOT/bin/server" -addr ":$PORT" $SFLAGS > "$CLOG" 2>&1 &
SPID=$!

cleanup() {
  kill -TERM "$SPID" 2>/dev/null
  sleep 0.2
  kill -KILL "$SPID" 2>/dev/null
  wait "$SPID" 2>/dev/null
}
trap cleanup EXIT INT TERM

for i in $(seq 1 100); do
  if (echo > "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then break; fi
  sleep 0.1
done
sleep 0.8
if ! kill -0 "$SPID" 2>/dev/null; then
  echo "SERVER_DIED"
  tail -20 "$CLOG"
  exit 1
fi

T0=$(awk '{print $14+$15}' /proc/$SPID/stat)
R0=$(grep VmRSS /proc/$SPID/status | awk '{print $2}')

taskset -c "$CCPUS" "$ROOT/bin/bench" -target "127.0.0.1:$PORT" $BFLAGS -o "$BJSON" > "$BLOG" 2>&1
RC=$?

T1=$(awk '{print $14+$15}' /proc/$SPID/stat)
R1=$(grep VmRSS /proc/$SPID/status | awk '{print $2}')
CPU=$(awk -v a=$T0 -v b=$T1 'BEGIN{printf "%.2f",(b-a)/100}')
GCS=$(grep -c '^gc ' "$CLOG" || true)

echo "{\"name\":\"$NAME\",\"server_cpu_s\":$CPU,\"server_rss_kb\":$R1,\"gc_count\":$GCS,\"bench_rc\":$RC}"

if [ -s "$BJSON" ]; then
  cat "$BJSON"
else
  echo "no result json for this round (bench_rc=$RC)"
  tail -5 "$BLOG"
fi

exit "$RC"
