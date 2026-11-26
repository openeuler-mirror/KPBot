#!/usr/bin/env bash
set -euo pipefail

PID="${1:-}"

if [[ -z "${PID}" ]]; then
  echo '{"balance_status":"unknown","hot_cpu_list":[],"thread_distribution":{},"skew_notes":["target pid is required"],"fallback_notes":["missing_pid"]}'
  exit 1
fi

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

thread_distribution="{}"
hot_cpu_list="[]"
balance_status="unknown"
skew_notes="[]"
fallback_notes=()

if have_cmd ps; then
  raw_lines="$(ps -T -p "${PID}" -o psr= 2>/dev/null || true)"
  if [[ -n "${raw_lines}" ]]; then
    thread_distribution="$(
      printf '%s\n' "${raw_lines}" | awk '
        NF {count[$1]++}
        END {
          printf "{"
          sep=""
          for (cpu in count) {
            printf "%s\"%s\":%d", sep, cpu, count[cpu]
            sep=","
          }
          printf "}"
        }'
    )"

    hot_cpu_list="$(
      printf '%s\n' "${raw_lines}" | awk '
        NF {count[$1]++}
        END {
          max=0
          for (cpu in count) if (count[cpu] > max) max=count[cpu]
          printf "["
          sep=""
          for (cpu in count) {
            if (count[cpu] == max) {
              printf "%s\"%s\"", sep, cpu
              sep=","
            }
          }
          printf "]"
        }'
    )"

    stats="$(printf '%s\n' "${raw_lines}" | awk '
      NF {count[$1]++}
      END {
        cpu_count=0
        max=0
        min=-1
        total=0
        for (cpu in count) {
          cpu_count++
          total += count[cpu]
          if (count[cpu] > max) max=count[cpu]
          if (min == -1 || count[cpu] < min) min=count[cpu]
        }
        if (cpu_count == 0) {
          print "0 0 0 0"
        } else {
          printf "%d %d %d %d\n", cpu_count, total, max, min
        }
      }'
    )"

    cpu_count="$(echo "${stats}" | awk '{print $1}')"
    total_threads="$(echo "${stats}" | awk '{print $2}')"
    max_threads="$(echo "${stats}" | awk '{print $3}')"
    min_threads="$(echo "${stats}" | awk '{print $4}')"

    if [[ "${cpu_count}" -gt 1 ]]; then
      if awk -v max="${max_threads}" -v min="${min_threads}" 'BEGIN { exit !((max - min) >= 2) }'; then
        balance_status="skewed"
        skew_notes='["thread_distribution_is_skewed","hot_threads_concentrated_on_subset_of_cpus"]'
      else
        balance_status="balanced"
        skew_notes='["no_obvious_thread_cpu_skew_detected"]'
      fi
    elif [[ "${total_threads}" -gt 0 ]]; then
      balance_status="single_cpu_or_single_thread"
      skew_notes='["insufficient_distribution_width_for_balance_judgement"]'
    else
      fallback_notes+=("no_thread_distribution_data")
    fi
  else
    fallback_notes+=("ps_thread_view_empty")
  fi
else
  fallback_notes+=("ps_missing")
fi

fallback_json="[]"
if [[ "${#fallback_notes[@]}" -gt 0 ]]; then
  fallback_json="$(printf '%s\n' "${fallback_notes[@]}" | awk 'BEGIN{printf "["} {printf "%s\"%s\"", sep, $0; sep=","} END{printf "]"}')"
fi

cat <<EOF
{
  "balance_status": "${balance_status}",
  "hot_cpu_list": ${hot_cpu_list},
  "thread_distribution": ${thread_distribution},
  "skew_notes": ${skew_notes},
  "fallback_notes": ${fallback_json}
}
EOF
