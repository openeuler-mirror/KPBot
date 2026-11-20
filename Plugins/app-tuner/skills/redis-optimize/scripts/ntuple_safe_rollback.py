#!/usr/bin/env python3
"""Snapshot/restore ethtool ntuple filters for the redis-optimize skill.

Rollback contract:
  * delete only filters added by this tuning run;
  * keep or rebuild filters that existed before the run;
  * never blindly delete every filter on the NIC.

Usage:
  python3 scripts/ntuple_safe_rollback.py snapshot --iface eth2 \
      --out "$SNAP/ntuple.json"
  python3 scripts/ntuple_safe_rollback.py restore --iface eth2 \
      --snapshot "$SNAP/ntuple.json" \
      [--report "$SNAP/ntuple-restore.json"] [--dry-run]

Exit codes:
  0  success (or ntuple unsupported at snapshot time)
  1  one or more filters could not be rebuilt
  2  snapshot/ethtool invocation failed for a reason other than "not supported"
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

FLOW_TYPE_MAP = {
    "Ethernet": "ether",
    "Raw IPv4": "ip4",
    "TCP over IPv4": "tcp4",
    "UDP over IPv4": "udp4",
    "SCTP over IPv4": "sctp4",
    "IPSEC AH over IPv4": "ah4",
    "IPSEC ESP over IPv4": "esp4",
    "Raw IPv6": "ip6",
    "TCP over IPv6": "tcp6",
    "UDP over IPv6": "udp6",
    "SCTP over IPv6": "sctp6",
    "IPSEC AH over IPv6": "ah6",
    "IPSEC ESP over IPv6": "esp6",
}
FLOW_TYPES = set(FLOW_TYPE_MAP.values())

FIELD_MAP = {
    "srcipaddr": "src-ip",
    "destipaddr": "dst-ip",
    "dstipaddr": "dst-ip",
    "srcport": "src-port",
    "destport": "dst-port",
    "dstport": "dst-port",
    "tos": "tos",
    "trafficclass": "tclass",
    "tclass": "tclass",
    "l4proto": "l4proto",
    "spi": "spi",
    "l4data": "l4data",
    "vlanethertype": "vlan-etype",
    "vlan": "vlan",
    "userdefined": "user-def",
    "srcmacaddr": "src",
    "srcmac": "src",
    "destmacaddr": "dst",
    "destmac": "dst",
    "dstmacaddr": "dst",
    "dstmac": "dst",
    "ethertype": "proto",
    "proto": "proto",
    "context": "context",
    "queue": "queue",
    "vf": "vf",
}

# Order follows ethtool(8) so rebuilt commands stay readable.
FIELD_ORDER = [
    "src", "dst", "proto",
    "src-ip", "dst-ip", "tos", "tclass", "l4proto",
    "src-port", "dst-port", "spi", "l4data",
    "vlan-etype", "vlan", "src-mac", "dst-mac", "user-def",
    "context",
]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def ethtool_n(iface):
    proc = run(["ethtool", "-n", iface])
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip()
    return proc.stdout, None


def norm_key(key):
    return re.sub(r"[^a-z0-9]", "", key.lower())


def split_value_mask(value):
    match = re.match(r"^(.*?)\s+mask:\s*(\S+)\s*$", value)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return value.strip(), None


def is_zero_text(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in ("", "0", "0x0", "0.0.0.0", "::",
                "00:00:00:00:00:00", "00:00:00:00:00:00:00:00"):
        return True
    if re.fullmatch(r"0x0+", text):
        return True
    if re.fullmatch(r"0+(\.0+){3}", text):
        return True
    if re.fullmatch(r"(00:)+0+", text):
        return True
    return False


def is_zero_mask(mask):
    return is_zero_text(mask)


def parse_filters(raw):
    rules = []
    if not raw:
        return rules
    for block in re.split(r"(?m)^Filter:\s*", raw)[1:]:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        try:
            location = int(lines[0].split()[0])
        except ValueError:
            continue
        rule = {
            "location": location,
            "flow_type": None,
            "action": None,
            "fields": {},
            "unknown": [],
        }
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if key == "Flow Type":
                rule["flow_type"] = FLOW_TYPE_MAP.get(value, value)
                continue
            if key == "Action":
                try:
                    rule["action"] = int(value, 0)
                except ValueError:
                    rule["unknown"].append(line)
                continue
            field = FIELD_MAP.get(norm_key(key))
            if not field:
                rule["unknown"].append(line)
                continue
            # ethtool uses src/dst for ether rules and src-mac/dst-mac for IP rules.
            if field in ("src", "dst") and rule["flow_type"] != "ether":
                field += "-mac"
            field_value, mask = split_value_mask(value)
            rule["fields"][field] = {
                "value": field_value,
                "mask": mask,
                "source": key,
            }
        rules.append(rule)
    return rules


def signature(rule):
    fields = {
        name: {"value": spec["value"], "mask": spec["mask"]}
        for name, spec in rule.get("fields", {}).items()
        if not is_zero_mask(spec.get("mask"))
    }
    payload = {
        "flow_type": rule.get("flow_type"),
        "action": rule.get("action"),
        "fields": fields,
        "unknown": sorted(rule.get("unknown", [])),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def build_command(iface, rule):
    flow = rule.get("flow_type")
    if flow not in FLOW_TYPES:
        raise ValueError("unsupported/unknown flow type: {!r}".format(flow))
    if rule.get("unknown"):
        raise ValueError("unparsed fields: " + "; ".join(rule["unknown"]))

    cmd = ["ethtool", "--config-ntuple", iface, "flow-type", flow]
    for name in FIELD_ORDER:
        spec = rule.get("fields", {}).get(name)
        if not spec:
            continue
        if is_zero_mask(spec.get("mask")):
            continue
        # Unspecified match fields are commonly printed as zero value + full
        # mask. Omit those defaults so the rebuilt rule keeps wildcard semantics.
        if name not in ("context", "queue", "vf") and is_zero_text(spec.get("value")):
            continue
        cmd.extend([name, str(spec["value"])])
        if spec.get("mask"):
            cmd.extend(["m", str(spec["mask"])])

    fields = rule.get("fields", {})
    if "queue" in fields and not is_zero_mask(fields["queue"].get("mask")):
        cmd.extend(["queue", str(fields["queue"]["value"])])
    elif "vf" in fields and not is_zero_mask(fields["vf"].get("mask")):
        cmd.extend(["vf", str(fields["vf"]["value"])])
    else:
        if rule.get("action") is None:
            raise ValueError("rule has no action/queue/vf target")
        cmd.extend(["action", str(rule["action"])])
    return cmd


def snapshot(iface, out_path):
    raw, error = ethtool_n(iface)
    if raw is None:
        lowered = (error or "").lower()
        unsupported = "not supported" in lowered or "operation not supported" in lowered
        data = {
            "iface": iface,
            "supported": False,
            "error": error,
            "raw": "",
            "rules": [],
        }
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if unsupported:
            print("ntuple not supported on {}; snapshot recorded as unsupported".format(iface),
                  file=sys.stderr)
            return 0
        print("ERROR: ethtool -n {} failed: {}".format(iface, error), file=sys.stderr)
        return 2

    data = {
        "iface": iface,
        "supported": True,
        "raw": raw,
        "rules": parse_filters(raw),
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("snapshot: {} filter(s) -> {}".format(len(data["rules"]), out_path))
    return 0


def restore(iface, snapshot_path, report_path=None, dry_run=False):
    try:
        snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("ERROR: cannot read snapshot {}: {}".format(snapshot_path, exc), file=sys.stderr)
        return 2

    if not snap.get("supported"):
        error = (snap.get("error") or "").lower()
        if "not supported" in error or "operation not supported" in error:
            print("snapshot says ntuple was unsupported; no filter restore needed",
                  file=sys.stderr)
            return 0
        print("ERROR: ntuple snapshot was not captured successfully: {}".format(
            snap.get("error")), file=sys.stderr)
        return 2

    current_raw, error = ethtool_n(iface)
    if current_raw is None:
        print("ERROR: ethtool -n {} failed: {}".format(iface, error), file=sys.stderr)
        return 2

    snapshot_rules = snap.get("rules") or []
    snapshot_sigs = {signature(rule) for rule in snapshot_rules}
    current_rules = parse_filters(current_raw)

    deleted = []
    failed = []
    planned = []

    for rule in current_rules:
        if signature(rule) in snapshot_sigs:
            continue
        cmd = ["ethtool", "--config-ntuple", iface, "delete", str(rule["location"])]
        if dry_run:
            planned.append(" ".join(cmd))
            continue
        proc = run(cmd)
        if proc.returncode == 0:
            deleted.append(rule["location"])
        else:
            failed.append(
                "delete loc {}: {}".format(
                    rule["location"], (proc.stderr or proc.stdout or "").strip()
                )
            )

    if dry_run:
        current_after = current_rules
    else:
        current_after_raw, error = ethtool_n(iface)
        if current_after_raw is None:
            print("ERROR: re-read of filters failed: {}".format(error), file=sys.stderr)
            return 2
        current_after = parse_filters(current_after_raw)

    present = {signature(rule) for rule in current_after}
    rebuilt = []
    for rule in snapshot_rules:
        if signature(rule) in present:
            continue
        try:
            cmd = build_command(iface, rule)
        except ValueError as exc:
            failed.append(
                "cannot rebuild snapshot filter loc {}: {}; raw snapshot is in {}".format(
                    rule.get("location"), exc, snapshot_path
                )
            )
            continue
        if dry_run:
            planned.append(" ".join(cmd))
            continue
        proc = run(cmd)
        if proc.returncode == 0:
            rebuilt.append(rule.get("location"))
            present.add(signature(rule))
        else:
            failed.append(
                "rebuild loc {} failed: {} :: {}".format(
                    rule.get("location"),
                    " ".join(cmd),
                    (proc.stderr or proc.stdout or "").strip(),
                )
            )

    report = {
        "iface": iface,
        "snapshot": str(snapshot_path),
        "dry_run": bool(dry_run),
        "snapshot_filters": len(snapshot_rules),
        "deleted_new_location": deleted,
        "rebuilt_original_location": rebuilt,
        "planned_commands": planned,
        "failed": failed,
        "success": not failed,
    }
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if failed:
        print(
            "MANUAL RESTORE REQUIRED: {} filter(s) could not be restored; "
            "compare with the raw snapshot (ethtool-n.txt / ntuple.json)".format(len(failed)),
            file=sys.stderr,
        )
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="save current ntuple filters")
    snap.add_argument("--iface", required=True)
    snap.add_argument("--out", required=True)

    rest = sub.add_parser("restore", help="delete new filters and rebuild original ones")
    rest.add_argument("--iface", required=True)
    rest.add_argument("--snapshot", required=True)
    rest.add_argument("--report")
    rest.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if args.command == "snapshot":
        return snapshot(args.iface, args.out)
    return restore(args.iface, args.snapshot, args.report, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
