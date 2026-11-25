#!/usr/bin/env bash
# collect_memory_evidence.sh — read-only memory evidence collection for the
# io-optimization subskill. Run it after the prescreen verdict lands on
# memory_capacity / memory_bandwidth (or as an evidence refresh before
# proposing capacity, bandwidth or NUMA-locality candidates).
#
# Usage: collect_memory_evidence.sh [duration_seconds]
#   duration_seconds  sampling window per metric, default 10
#
# Output: a single JSON object on stdout. Strictly read-only. Missing tools
# degrade gracefully and are recorded in "notes".
set -uo pipefail

DURATION="${1:-10}"

notes=()
have_cmd() { command -v "$1" >/dev/null 2>&1; }
jstr() { printf '%s' "$1" | sed 's/"/\\"/g'; }

json_join() {
  local first=1 item
  for item in "$@"; do
    [[ -z "${item}" ]] && continue
    [[ ${first} -eq 1 ]] || printf ', '
    first=0
    printf '"%s"' "$(jstr "${item}")"
  done
}

# ------------------------------------------------------------------ free ----
mem_total="null"; mem_used="null"; mem_avail="null"; swap_total="null"; swap_used="null"
if have_cmd free; then
  read -r mem_total mem_used mem_avail swap_total swap_used < <(
    free -m 2>/dev/null | awk '
      /^Mem:/  {t=$2; u=$3; a=$7}
      /^Swap:/ {st=$2; su=$3}
      END {print t+0, u+0, a+0, st+0, su+0}
    '
  )
else
  notes+=("free_missing")
fi

# ---------------------------------------------------------------- vmstat ----
# procps vmstat's first data row is the since-boot average; skip it so
# historical paging is not reported as current-window swap activity. An
# empty window (DURATION=1) degrades to null + note.
si_kb="null"; so_kb="null"
if have_cmd vmstat; then
  read -r si_kb so_kb < <(
    { vmstat 1 "${DURATION}" 2>/dev/null || true; } | awk 'NR>3 {si+=$7; so+=$8; n++} END {if (n==0) print "null null"; else printf "%.2f %.2f", si/n, so/n}'
  )
  [[ "${si_kb}" == "null" ]] && notes+=("vmstat_since_boot_only")
else
  notes+=("vmstat_missing")
fi

# --------------------------------------------------------------- numastat ---
# System-wide per-node remote miss ratio: numa_miss / (numa_hit + numa_miss).
# The per-node row of numastat output gives the node id in the header line.
numa_nodes=""
numa_summary="null"
if have_cmd numastat; then
  numa_nodes="$(numastat 2>/dev/null | awk '
    $1 == "numa_hit"  {for (i = 2; i <= NF; i++) hit[i-2] = $i + 0}
    $1 == "numa_miss" {for (i = 2; i <= NF; i++) miss[i-2] = $i + 0}
    END {
      for (n in hit) {
        pct = (hit[n] + miss[n] > 0) ? 100 * miss[n] / (hit[n] + miss[n]) : 0
        printf "%s %.2f\n", n, pct
      }
    }
  ' | sort -n | awk '{printf "    {\"node\": %s, \"remote_miss_pct\": %s},\n", $1, $2}')"
  numa_nodes="${numa_nodes%,}"
  numa_summary="$(numastat 2>/dev/null | awk '
    $1 == "numa_hit"  {for (i = 2; i <= NF; i++) th += $i + 0}
    $1 == "numa_miss" {for (i = 2; i <= NF; i++) tm += $i + 0}
    END {if (th + tm > 0) printf "%.2f", 100 * tm / (th + tm); else print "null"}')"
else
  notes+=("numastat_missing")
fi

# ------------------------------------------------------- node memory usage --
node_mem=""
for node_dir in /sys/devices/system/node/node[0-9]*; do
  [[ -r "${node_dir}/meminfo" ]] || continue
  node_id="${node_dir##*node}"
  # meminfo lines look like "Node 0 MemTotal:     19530784 kB"
  node_total_kb="$(awk -v n="${node_id}" '$1=="Node" && $2==n && $3=="MemTotal:" {print $4; found=1} END {if (!found) print "null"}' "${node_dir}/meminfo")"
  node_free_kb="$(awk -v n="${node_id}" '$1=="Node" && $2==n && $3=="MemFree:" {print $4; found=1} END {if (!found) print "null"}' "${node_dir}/meminfo")"
  if [[ "${node_total_kb}" != "null" && "${node_free_kb}" != "null" ]]; then
    node_used_mb="$(awk -v t="${node_total_kb}" -v f="${node_free_kb}" 'BEGIN {printf "%.0f", (t-f)/1024}')"
    node_total_mb="$(awk -v t="${node_total_kb}" 'BEGIN {printf "%.0f", t/1024}')"
    node_mem+="    {\"node\": ${node_id}, \"mem_total_mb\": ${node_total_mb}, \"mem_used_mb\": ${node_used_mb}},"
    node_mem+=$'\n'
  fi
done
# strip the trailing comma and newline of the last entry
if [[ -n "${node_mem}" ]]; then
  node_mem="${node_mem%$'\n'}"
  node_mem="${node_mem%,}"
fi

# ------------------------------------------------------------------- perf ---
cache_miss_rate="null"
if have_cmd perf; then
  perf_out="$( { perf stat -e cache-misses,cache-references -a -- sleep "${DURATION}" 2>&1 || true; } )"
  misses="$(printf '%s\n' "${perf_out}" | awk '$2 == "cache-misses" {gsub(",", "", $1); print $1; found=1} END {if (!found) print ""}')"
  refs="$(printf '%s\n' "${perf_out}" | awk '$2 == "cache-references" {gsub(",", "", $1); print $1; found=1} END {if (!found) print ""}')"
  if [[ -n "${misses}" && -n "${refs}" && "${refs}" != "0" && "${refs}" != "<not" && "${misses}" != "<not" ]]; then
    cache_miss_rate="$(awk -v m="${misses}" -v r="${refs}" 'BEGIN {printf "%.2f", 100 * m / r}')"
  else
    notes+=("perf_cache_counters_not_counted")
  fi
else
  notes+=("perf_missing")
fi

# --------------------------------------------------------------- THP / PSI --
thp_mode="null"
if [[ -r /sys/kernel/mm/transparent_hugepage/enabled ]]; then
  thp_mode="$(awk '{match($0, /\[[a-z]+\]/); if (RSTART) print substr($0, RSTART+1, RLENGTH-2)}' /sys/kernel/mm/transparent_hugepage/enabled)"
fi
psi_some_avg10="null"; psi_some_avg60="null"; psi_full_avg10="null"
# /proc/pressure/<res> values start after '='; index()-based parsing avoids
# the fixed-offset bug that dropped the first value digit (12.34 -> 2.34).
# PSI_DIR is overridable for tests / containers with masked /proc.
PSI_DIR="${PSI_DIR:-/proc/pressure}"
read_psi_field() {
  # $1 = psi file, $2 = line prefix (some|full), $3 = field (avg10|avg60)
  awk -v prefix="$2" -v field="$3" '
    $1 == prefix {
      for (i = 2; i <= NF; i++) {
        if (index($i, field "=") == 1) {
          val = substr($i, length(field "=") + 1)
          if (val ~ /^[0-9]+(\.[0-9]+)?$/) {
            printf "%.2f\n", val + 0
            found = 1
          }
          exit
        }
      }
    }
    END { if (!found) print "null" }
  ' "$1" 2>/dev/null
}
psi_mem_file="${PSI_DIR}/memory"
if [[ -r "${psi_mem_file}" ]]; then
  psi_some_avg10="$(read_psi_field "${psi_mem_file}" some avg10)"
  psi_some_avg60="$(read_psi_field "${psi_mem_file}" some avg60)"
  psi_full_avg10="$(read_psi_field "${psi_mem_file}" full avg10)"
  if [[ "${psi_some_avg10}" == "null" || "${psi_some_avg60}" == "null" || "${psi_full_avg10}" == "null" ]]; then
    notes+=("psi_memory_parse_anomaly")
  fi
else
  notes+=("psi_memory_unavailable")
fi
swappiness="null"
[[ -r /proc/sys/vm/swappiness ]] && swappiness="$(cat /proc/sys/vm/swappiness)"

# ------------------------------------------------------------------ output --
printf '{\n'
printf '  "script": "collect_memory_evidence.sh",\n'
printf '  "duration_sec": %s,\n' "${DURATION}"
printf '  "read_only": true,\n'
printf '  "mem_mb": {"total": %s, "used": %s, "available": %s, "swap_total": %s, "swap_used": %s},\n' \
  "${mem_total}" "${mem_used}" "${mem_avail}" "${swap_total}" "${swap_used}"
printf '  "swap_activity_kb_per_sec": {"si": %s, "so": %s},\n' "${si_kb}" "${so_kb}"
printf '  "psi_memory": {"some_avg10": %s, "some_avg60": %s, "full_avg10": %s},\n' \
  "${psi_some_avg10}" "${psi_some_avg60}" "${psi_full_avg10}"
printf '  "numa": {"system_remote_miss_pct": %s, "nodes": [\n%s\n  ]},\n' "${numa_summary}" "${numa_nodes:-}"
printf '  "node_memory_mb": [\n%s\n  ],\n' "${node_mem:-}"
printf '  "cache_miss_rate_pct": %s,\n' "${cache_miss_rate}"
printf '  "thp_mode": "%s",\n' "${thp_mode}"
printf '  "swappiness": %s,\n' "${swappiness}"
printf '  "notes": ['
json_join "${notes[@]:-}"
printf ']\n}\n'
