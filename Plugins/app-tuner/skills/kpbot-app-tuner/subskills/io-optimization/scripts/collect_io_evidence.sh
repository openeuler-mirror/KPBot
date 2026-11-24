#!/usr/bin/env bash
# collect_io_evidence.sh — read-only IO evidence collection for the
# io-optimization subskill. Run it after the prescreen verdict lands on
# disk_io / disk_bandwidth (or as an evidence refresh before proposing
# I/O scheduler / readahead / mount-option / writeback candidates).
#
# Usage: collect_io_evidence.sh [duration_seconds] [device]
#   duration_seconds  sampling window per metric, default 10
#   device            optional block device to focus on (e.g. nvme0n1);
#                     when omitted the busiest devices of the window are
#                     reported (idle devices are filtered out)
#
# Output: a single JSON object on stdout. Strictly read-only: no sysfs,
# procfs or runtime state is modified. Missing tools degrade gracefully
# and are recorded in "notes".
#
# Column parsing is dynamic (header-name based): sysstat >= 12 reorders and
# extends iostat -x columns, so fixed field numbers break across versions.
set -uo pipefail

DURATION="${1:-10}"
FOCUS_DEVICE="${2:-}"

notes=()
have_cmd() { command -v "$1" >/dev/null 2>&1; }
jstr() { printf '%s' "$1" | sed 's/"/\\"/g'; }

# json_join array... — prints "a", "b" (no brackets), escaping each element
json_join() {
  local first=1 item
  for item in "$@"; do
    [[ -z "${item}" ]] && continue
    [[ ${first} -eq 1 ]] || printf ', '
    first=0
    printf '"%s"' "$(jstr "${item}")"
  done
}

# ---------------------------------------------------------------- vmstat ---
# procps vmstat's first data row is the since-boot average; skip it and
# average the sampling window only (consistent with the iostat second-section
# window below). An empty window (DURATION=1) degrades to null + note.
iowait_pct="null"
if have_cmd vmstat; then
  iowait_pct="$( { vmstat 1 "${DURATION}" 2>/dev/null || true; } \
    | awk 'NR>3 {sum+=$16; n++} END {if (n==0) print "null"; else printf "%.2f", sum/n}')"
  [[ "${iowait_pct}" == "null" ]] && notes+=("vmstat_since_boot_only")
else
  notes+=("vmstat_missing")
fi

# ----------------------------------------------------------------- iostat ---
# iostat -x D 2: first report is since boot, second report covers exactly
# the DURATION window. Parse only the second section, with dynamic columns.
# Header forms differ across sysstat versions: "Device" (e.g. openEuler 24.03)
# and "Device:" (matched by prefix, cf. use-analysis use_scan.py). When the
# header cannot be parsed at all, degrade with a note instead of silently
# reporting an empty device list.
dev_json=""
iostat_status=""
if have_cmd iostat; then
  iostat_raw="$( { iostat -x "${DURATION}" 2 2>/dev/null || true; } | awk -v focus="${FOCUS_DEVICE}" '
    substr($1, 1, 6) == "Device" {
      section++
      r_i=rkb_i=w_i=wkb_i=raw_i=waw_i=aq_i=util_i=0
      for (i = 2; i <= NF; i++) {
        if      ($i == "r/s")     r_i = i
        else if ($i == "rkB/s")   rkb_i = i
        else if ($i == "w/s")     w_i = i
        else if ($i == "wkB/s")   wkb_i = i
        else if ($i == "r_await") raw_i = i
        else if ($i == "w_await") waw_i = i
        else if ($i == "aqu-sz")  aq_i = i
        else if ($i == "%util")   util_i = i
      }
      next
    }
    section == 2 && util_i && NF >= util_i && $1 ~ /^(nvme|sd|vd|xvd|dm-|md)/ {
      dev = $1
      if (focus != "" && dev != focus) next
      util = $util_i + 0
      if (focus == "" && util <= 0.01) next
      rs = (r_i) ? $r_i + 0 : 0
      ws = (w_i) ? $w_i + 0 : 0
      rkb = (rkb_i) ? $rkb_i + 0 : 0
      wkb = (wkb_i) ? $wkb_i + 0 : 0
      raw = (raw_i) ? $raw_i + 0 : 0
      waw = (waw_i) ? $waw_i + 0 : 0
      aq = (aq_i) ? $aq_i + 0 : 0
      printf "%s %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f\n", dev, util, rs, ws, rkb, wkb, raw, waw, aq
    }
    END { printf "STATUS sections=%d util_i=%d\n", section + 0, util_i + 0 }
  ')"
  iostat_status="$(printf '%s\n' "${iostat_raw}" | grep '^STATUS ' | tail -1 || true)"
  dev_json="$(printf '%s\n' "${iostat_raw}" | grep -v '^STATUS ' || true)"
  # Parsed OK iff the Device header matched in both sections AND the %util
  # column was located. Otherwise degrade with a note so an empty device
  # list is not mistaken for "no active devices" (sysstat versions differ:
  # "Device" vs "Device:" — both accepted above via prefix match).
  if ! printf '%s' "${iostat_status}" | grep -Eq 'sections=[2-9][0-9]* util_i=[1-9][0-9]*'; then
    notes+=("iostat_header_unparsed")
  fi
else
  notes+=("iostat_missing")
fi

# ------------------------------------------------------- per-device sysfs ---
device_section=""
while read -r dev util rs ws rkb wkb raw waw aq; do
  [[ -z "${dev}" ]] && continue
  sched="null"; ra="null"; nrq="null"; rot="null"
  qdir="/sys/block/${dev}/queue"
  if [[ -d "${qdir}" ]]; then
    [[ -r "${qdir}/scheduler" ]] && sched="\"$(jstr "$(cat "${qdir}/scheduler")")\""
    [[ -r "${qdir}/read_ahead_kb" ]] && ra="$(cat "${qdir}/read_ahead_kb")"
    [[ -r "${qdir}/nr_requests" ]] && nrq="$(cat "${qdir}/nr_requests")"
    [[ -r "${qdir}/rotational" ]] && rot="$(cat "${qdir}/rotational")"
  else
    notes+=("sysfs_queue_missing:${dev}")
  fi
  device_section+="    {\"name\": \"${dev}\", \"util_pct\": ${util}, \"r_per_sec\": ${rs}, \"w_per_sec\": ${ws}, \"rkB_per_sec\": ${rkb}, \"wkB_per_sec\": ${wkb}, \"r_await_ms\": ${raw}, \"w_await_ms\": ${waw}, \"avg_queue_size\": ${aq}, \"scheduler\": ${sched}, \"read_ahead_kb\": ${ra}, \"nr_requests\": ${nrq}, \"rotational\": ${rot}},"
  device_section+=$'\n'
done <<<"${dev_json}"
# strip the trailing comma and newline of the last entry
if [[ -n "${device_section}" ]]; then
  device_section="${device_section%$'\n'}"
  device_section="${device_section%,}"
fi

# ------------------------------------------------------------- findmnt ------
# Only block-device-backed mounts matter for IO analysis. Pseudo filesystems
# (nsfs/proc/sysfs/tmpfs/cgroup/...) are filtered out; on container hosts
# findmnt can otherwise return thousands of nsfs rows.
mount_section=""
if have_cmd findmnt; then
  mount_section="$(findmnt -rn -o SOURCE,FSTYPE,OPTIONS,TARGET 2>/dev/null | awk '
    /^\/dev\// {
      src = $1; fs = $2; opts = $3
      $1 = $2 = $3 = ""
      target = substr($0, 4)
      gsub(/"/, "\\\"", src); gsub(/"/, "\\\"", fs)
      gsub(/"/, "\\\"", opts); gsub(/"/, "\\\"", target)
      printf "    {\"source\": \"%s\", \"fstype\": \"%s\", \"options\": \"%s\", \"target\": \"%s\"},\n", src, fs, opts, target
    }
  ')"
  # drop the trailing comma of the last entry
  mount_section="$(printf '%s\n' "${mount_section}" | sed '$ s/,$//')"
else
  notes+=("findmnt_missing")
fi

# ---------------------------------------------------------------- vm sysctl --
read_sysctl() {
  if [[ -r "/proc/sys/vm/$1" ]]; then cat "/proc/sys/vm/$1"; else printf 'null'; fi
}
dirty_ratio="$(read_sysctl dirty_ratio)"
dirty_bg="$(read_sysctl dirty_background_ratio)"
dirty_expire="$(read_sysctl dirty_expire_centisecs)"
dirty_wb="$(read_sysctl dirty_writeback_centisecs)"
swappiness="$(read_sysctl swappiness)"

# ------------------------------------------------------------------ PSI -----
# /proc/pressure/<res> line format:
#   some avg10=0.00 avg60=0.00 avg300=0.00 total=0
# Values start after '='; parse with index() so any field-name length works.
# (A fixed substr offset once dropped the first value digit: avg10=12.34
# was read as 2.34 — pressure systematically underreported.)
# PSI_DIR is overridable for tests / containers with masked /proc.
PSI_DIR="${PSI_DIR:-/proc/pressure}"
psi_some_avg10="null"; psi_some_avg60="null"; psi_full_avg10="null"
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
psi_io_file="${PSI_DIR}/io"
if [[ -r "${psi_io_file}" ]]; then
  psi_some_avg10="$(read_psi_field "${psi_io_file}" some avg10)"
  psi_some_avg60="$(read_psi_field "${psi_io_file}" some avg60)"
  psi_full_avg10="$(read_psi_field "${psi_io_file}" full avg10)"
  if [[ "${psi_some_avg10}" == "null" || "${psi_some_avg60}" == "null" || "${psi_full_avg10}" == "null" ]]; then
    notes+=("psi_io_parse_anomaly")
  fi
else
  notes+=("psi_io_unavailable")
fi

# ------------------------------------------------------------------ output --
printf '{\n'
printf '  "script": "collect_io_evidence.sh",\n'
printf '  "duration_sec": %s,\n' "${DURATION}"
printf '  "read_only": true,\n'
printf '  "iowait_pct": %s,\n' "${iowait_pct}"
printf '  "psi_io": {"some_avg10": %s, "some_avg60": %s, "full_avg10": %s},\n' \
  "${psi_some_avg10}" "${psi_some_avg60}" "${psi_full_avg10}"
if [[ -n "${device_section}" ]]; then
  printf '  "devices": [\n%s\n  ],\n' "${device_section}"
else
  printf '  "devices": [],\n'
fi
if [[ -n "${mount_section}" ]]; then
  printf '  "mounts": [\n%s\n  ],\n' "${mount_section}"
else
  printf '  "mounts": [],\n'
fi
printf '  "vm": {"dirty_ratio": %s, "dirty_background_ratio": %s, "dirty_expire_centisecs": %s, "dirty_writeback_centisecs": %s, "swappiness": %s},\n' \
  "${dirty_ratio}" "${dirty_bg}" "${dirty_expire}" "${dirty_wb}" "${swappiness}"
printf '  "notes": ['
json_join "${notes[@]:-}"
printf ']\n}\n'
