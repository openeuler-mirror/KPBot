#!/usr/bin/env bash
set -euo pipefail

PID="${1:-}"
DURATION="${2:-60}"
CORE_LIST="${3:-}"  # optional: e.g. "32-39" for mpstat -P

if [[ -z "${PID}" ]]; then
  echo '{"bottleneck_type":"unknown_bottleneck","legacy_bottleneck_type":"unknown","detailed_bottleneck_type":"unknown","confidence":"low","evidence":{},"fallback_notes":["target pid is required"]}'
  exit 1
fi

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

cpu_pct="null"
cpu_pct_mpstat="null"
irq_pct_mpstat="null"
idle_pct_mpstat="null"
iowait_pct="null"
disk_util_pct="null"
network_hint="unknown"
network_total_recvq_bytes=0
network_total_sendq_bytes=0
network_socket_count=0
network_retrans_segments=0
network_rx_bytes_per_sec="null"
network_tx_bytes_per_sec="null"
network_rx_packets_per_sec="null"
network_tx_packets_per_sec="null"
network_drop_delta=0
memory_pressure_hint="unknown"
confidence="low"
fallback_notes=()

# mpstat: core-level CPU utilization (primary tool, includes %irq)
if have_cmd mpstat; then
  mpstat_args=(-P ALL 1 "${DURATION}")
  [[ -n "${CORE_LIST}" ]] && mpstat_args=(-P "${CORE_LIST}" 1 "${DURATION}")
  cpu_pct_mpstat="$( { mpstat "${mpstat_args[@]}" 2>/dev/null || true; } | awk '/Average/ && $2 !~ /CPU/ {usr+=$3; sys+=$5; irq+=$8; soft+=$9; idle+=$10; n++} END {if (n==0) print "null"; else printf "%.2f", (usr+sys+irq+soft)}')"
  irq_pct_mpstat="$( { mpstat "${mpstat_args[@]}" 2>/dev/null || true; } | awk '/Average/ && $2 !~ /CPU/ {sum+=$8; n++} END {if (n==0) print "null"; else printf "%.2f", sum}')"
  idle_pct_mpstat="$( { mpstat "${mpstat_args[@]}" 2>/dev/null || true; } | awk '/Average/ && $2 !~ /CPU/ {sum+=$10; n++} END {if (n==0) print "null"; else printf "%.2f", sum}')"
else
  fallback_notes+=("mpstat_missing")
fi

# pidstat: process-level usr/sys breakdown (supplementary)
if have_cmd pidstat; then
  cpu_pct="$( { pidstat -p "${PID}" 1 "${DURATION}" 2>/dev/null || true; } | awk '/Average/ {sum += $(NF-6)} END {if (sum == "") print "null"; else printf "%.2f", sum}')"
else
  fallback_notes+=("pidstat_missing")
fi

if have_cmd vmstat; then
  iowait_pct="$( { vmstat 1 "${DURATION}" 2>/dev/null || true; } | awk 'NR>2 {sum += $16; count++} END {if (count==0) print "null"; else printf "%.2f", sum/count}')"
else
  fallback_notes+=("vmstat_missing")
fi

if have_cmd iostat; then
  samples=$(( DURATION / 5 ))
  [[ "${samples}" -lt 1 ]] && samples=1
  disk_util_pct="$( { iostat -xmd 5 "${samples}" 2>/dev/null || true; } | awk '/nvme|sd|vd|xvd/ {if ($NF+0 > max) max=$NF} END {if (max == "") print "null"; else printf "%.2f", max}')"
else
  fallback_notes+=("iostat_missing")
fi

sample_net_dev() {
  awk '
    NR > 2 && $1 !~ /^lo:/ {
      gsub(":", "", $1)
      rx_bytes += $2
      rx_packets += $3
      rx_drop += $5
      tx_bytes += $10
      tx_packets += $11
      tx_drop += $13
    }
    END {
      printf "%.0f %.0f %.0f %.0f %.0f %.0f\n",
        rx_bytes, tx_bytes, rx_packets, tx_packets, rx_drop, tx_drop
    }
  ' /proc/net/dev 2>/dev/null
}

if have_cmd ss; then
  ss_output="$(ss -tinp 2>/dev/null | awk -v pid="${PID}" '
    /^State/ { next }
    {
      if ($0 ~ ("pid=" pid ",")) {
        recvq += $2 + 0
        sendq += $3 + 0
        sockets++
        if (match($0, /retrans:[0-9]+\/([0-9]+)/, m)) {
          retrans += m[1] + 0
        }
      }
    }
    END {
      printf "%.0f %.0f %.0f %.0f\n", recvq, sendq, sockets, retrans
    }
  ')"

  if [[ -n "${ss_output}" ]]; then
    read -r network_total_recvq_bytes network_total_sendq_bytes network_socket_count network_retrans_segments <<<"${ss_output}"
    if (( network_socket_count > 0 )); then
      network_hint="socket_backlog_sampled"
    else
      network_hint="socket_view_available_but_pid_not_found"
      fallback_notes+=("ss_pid_socket_not_found")
    fi
  else
    network_hint="socket_view_available"
  fi
else
  fallback_notes+=("ss_missing")
fi

if [[ -r /proc/net/dev ]]; then
  net_before="$(sample_net_dev)"
  sample_seconds="${DURATION}"
  if (( DURATION > 0 )); then
    sleep "${DURATION}"
  fi
  net_after="$(sample_net_dev)"
  if [[ -n "${net_before}" && -n "${net_after}" ]]; then
    read -r rx_bytes_before tx_bytes_before rx_packets_before tx_packets_before rx_drop_before tx_drop_before <<<"${net_before}"
    read -r rx_bytes_after tx_bytes_after rx_packets_after tx_packets_after rx_drop_after tx_drop_after <<<"${net_after}"
    network_rx_bytes_per_sec="$(awk -v a="${rx_bytes_after}" -v b="${rx_bytes_before}" -v d="${sample_seconds}" 'BEGIN { if (d <= 0) print "null"; else printf "%.2f", (a-b)/d }')"
    network_tx_bytes_per_sec="$(awk -v a="${tx_bytes_after}" -v b="${tx_bytes_before}" -v d="${sample_seconds}" 'BEGIN { if (d <= 0) print "null"; else printf "%.2f", (a-b)/d }')"
    network_rx_packets_per_sec="$(awk -v a="${rx_packets_after}" -v b="${rx_packets_before}" -v d="${sample_seconds}" 'BEGIN { if (d <= 0) print "null"; else printf "%.2f", (a-b)/d }')"
    network_tx_packets_per_sec="$(awk -v a="${tx_packets_after}" -v b="${tx_packets_before}" -v d="${sample_seconds}" 'BEGIN { if (d <= 0) print "null"; else printf "%.2f", (a-b)/d }')"
    network_drop_delta=$(( (rx_drop_after - rx_drop_before) + (tx_drop_after - tx_drop_before) ))
  else
    fallback_notes+=("net_counter_second_sample_missing")
  fi
else
  fallback_notes+=("proc_net_dev_missing")
fi

if have_cmd free; then
  memory_pressure_hint="$(free -m 2>/dev/null | awk '/Mem:/ {if ($3/$2 > 0.9) print "high"; else if ($3/$2 > 0.75) print "medium"; else print "low"}')"
else
  fallback_notes+=("free_missing")
fi

detailed_bottleneck_type="unknown"
bottleneck_type="unknown_bottleneck"
legacy_bottleneck_type="unknown"

# Use mpstat as primary CPU saturation indicator (includes %irq)
if [[ "${cpu_pct_mpstat}" != "null" ]]; then
  total_cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
  cpu_threshold="$(awk -v cores="${total_cores}" 'BEGIN { printf "%.2f", cores * 85 }')"
  if awk -v cpu="${cpu_pct_mpstat}" -v threshold="${cpu_threshold}" 'BEGIN { exit !(cpu > threshold) }'; then
    detailed_bottleneck_type="cpu"
    confidence="medium"
  fi
elif [[ "${cpu_pct}" != "null" ]]; then
  # Fallback to pidstat if mpstat unavailable (underestimates, excludes %irq)
  total_cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
  cpu_threshold="$(awk -v cores="${total_cores}" 'BEGIN { printf "%.2f", cores * 80 }')"
  if awk -v cpu="${cpu_pct}" -v threshold="${cpu_threshold}" 'BEGIN { exit !(cpu > threshold) }'; then
    detailed_bottleneck_type="cpu"
    confidence="low"
    fallback_notes+=("cpu_judgment_based_on_pidstat_only")
  fi
fi

if [[ "${detailed_bottleneck_type}" == "unknown" \
   && "$(awk \
        -v sendq="${network_total_sendq_bytes}" \
        -v recvq="${network_total_recvq_bytes}" \
        -v drop_delta="${network_drop_delta}" \
        -v tx_pps="${network_tx_packets_per_sec}" \
        -v irq="${irq_pct_mpstat}" \
        'BEGIN {
          if (sendq > 131072 || recvq > 131072 || drop_delta > 0) {
            print "yes"
          } else if (tx_pps != "null" && irq != "null" && tx_pps > 50000 && irq > 10) {
            print "yes"
          } else {
            print "no"
          }
        }')" == "yes" ]]; then
  detailed_bottleneck_type="network"
  bottleneck_type="network_bottleneck"
  legacy_bottleneck_type="network_bottleneck"
  confidence="medium"
fi

if [[ "${detailed_bottleneck_type}" == "unknown" && "${iowait_pct}" != "null" ]]; then
  if awk -v io="${iowait_pct}" 'BEGIN { exit !(io > 10) }'; then
    detailed_bottleneck_type="disk_io"
    bottleneck_type="disk_bottleneck"
    legacy_bottleneck_type="disk_bottleneck"
    confidence="medium"
  fi
fi

if [[ "${detailed_bottleneck_type}" == "unknown" && "${disk_util_pct}" != "null" ]]; then
  if awk -v util="${disk_util_pct}" 'BEGIN { exit !(util > 80) }'; then
    detailed_bottleneck_type="disk_bandwidth"
    bottleneck_type="disk_bottleneck"
    legacy_bottleneck_type="disk_bottleneck"
    confidence="medium"
  fi
fi

if [[ "${detailed_bottleneck_type}" == "unknown" && "${memory_pressure_hint}" == "high" ]]; then
  detailed_bottleneck_type="memory_capacity"
  bottleneck_type="memory_capacity_bottleneck"
  legacy_bottleneck_type="memory_bandwidth_bottleneck"
  confidence="low"
fi

if [[ "${detailed_bottleneck_type}" == "cpu" ]]; then
  bottleneck_type="cpu_bottleneck"
  legacy_bottleneck_type="not_primary_bottleneck"
fi

if [[ "${detailed_bottleneck_type}" == "unknown" && "${#fallback_notes[@]}" -eq 0 ]]; then
  bottleneck_type="no_active_bottleneck"
  legacy_bottleneck_type="not_primary_bottleneck"
fi

if [[ "${detailed_bottleneck_type}" == "unknown" && "${network_hint}" != "unknown" ]]; then
  confidence="low"
fi

fallback_json="[]"
if [[ "${#fallback_notes[@]}" -gt 0 ]]; then
  fallback_json="$(printf '%s\n' "${fallback_notes[@]}" | awk 'BEGIN{printf "["} {printf "%s\"%s\"", sep, $0; sep=","} END{printf "]"}')"
fi

cat <<EOF
{
  "bottleneck_type": "${bottleneck_type}",
  "legacy_bottleneck_type": "${legacy_bottleneck_type}",
  "detailed_bottleneck_type": "${detailed_bottleneck_type}",
  "confidence": "${confidence}",
  "evidence": {
    "mpstat_cpu_pct": ${cpu_pct_mpstat},
    "mpstat_irq_pct": ${irq_pct_mpstat},
    "mpstat_idle_pct": ${idle_pct_mpstat},
    "process_cpu_pct": ${cpu_pct},
    "iowait_pct": ${iowait_pct},
    "disk_util_pct": ${disk_util_pct},
    "network_hint": "${network_hint}",
    "network_total_recvq_bytes": ${network_total_recvq_bytes},
    "network_total_sendq_bytes": ${network_total_sendq_bytes},
    "network_socket_count": ${network_socket_count},
    "network_retrans_segments": ${network_retrans_segments},
    "network_rx_bytes_per_sec": ${network_rx_bytes_per_sec},
    "network_tx_bytes_per_sec": ${network_tx_bytes_per_sec},
    "network_rx_packets_per_sec": ${network_rx_packets_per_sec},
    "network_tx_packets_per_sec": ${network_tx_packets_per_sec},
    "network_drop_delta": ${network_drop_delta},
    "network_sample_seconds": ${sample_seconds:-0},
    "memory_pressure_hint": "${memory_pressure_hint}"
  },
  "fallback_notes": ${fallback_json}
}
EOF
