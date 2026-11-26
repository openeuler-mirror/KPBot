#!/usr/bin/env python3
"""Generate a structured porting report from a process log.

Reads a JSON log file produced during the porting process and generates
a markdown report summarizing what was ported, adapted, or skipped.
"""

import json
import sys
import os
from datetime import datetime
from collections import defaultdict


def load_log(log_path):
    """Load the porting process log."""
    with open(log_path, 'r') as f:
        return json.load(f)


def generate_report(log_data, output_path):
    """Generate a markdown porting report."""
    lines = []
    lines.append("# Patch Porting Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Source version: {log_data.get('source_version', 'N/A')}")
    lines.append(f"Target version: {log_data.get('target_version', 'N/A')}")
    lines.append("")

    # Summary
    patches = log_data.get('patches', [])
    total_hunks = 0
    category_counts = defaultdict(int)
    compilation_status = "N/A"

    for patch in patches:
        for hunk in patch.get('hunks', []):
            total_hunks += 1
            category_counts[hunk.get('category', 'UNKNOWN')] += 1
        if patch.get('compilation') == 'PASS':
            compilation_status = "PASS"
        elif patch.get('compilation') == 'FAIL':
            compilation_status = "FAIL"

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Patches processed: {len(patches)}")
    lines.append(f"- Total hunks: {total_hunks}")
    for cat in ['DIRECT_PORT', 'ADAPTED_PORT', 'MISSING_INFRA', 'SKIP_NO_FILE', 'SKIP_FORK_CONFLICT']:
        count = category_counts.get(cat, 0)
        lines.append(f"- {cat}: {count}")
    lines.append(f"- Compilation: {compilation_status}")
    lines.append("")

    # Per-patch details
    lines.append("## Per-Patch Details")
    lines.append("")

    for patch in patches:
        name = patch.get('name', 'unknown')
        intent = patch.get('intent', 'N/A')
        status = patch.get('status', 'N/A')

        lines.append(f"### {name}")
        lines.append(f"**Intent**: {intent}")
        lines.append(f"**Status**: {status}")
        lines.append(f"**Compilation**: {patch.get('compilation', 'N/A')}")
        lines.append("")

        # Table of files
        hunks = patch.get('hunks', [])
        if hunks:
            lines.append("| File | Category | Notes |")
            lines.append("|------|----------|-------|")
            # Group by file
            by_file = defaultdict(list)
            for h in hunks:
                by_file[h.get('file', 'unknown')].append(h)

            for filepath, file_hunks in sorted(by_file.items()):
                categories = set(h.get('category', 'UNKNOWN') for h in file_hunks)
                notes = []
                for h in file_hunks:
                    if h.get('reason'):
                        notes.append(h['reason'])
                    if h.get('adaptation_notes'):
                        notes.append(h['adaptation_notes'])
                cat_str = ', '.join(sorted(categories))
                note_str = '; '.join(notes[:2])  # Limit notes
                lines.append(f"| {filepath} | {cat_str} | {note_str} |")

        lines.append("")

    # Warnings and issues
    warnings = log_data.get('warnings', [])
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # Output artifacts
    artifacts = log_data.get('artifacts', [])
    if artifacts:
        lines.append("## Output Artifacts")
        lines.append("")
        for a in artifacts:
            lines.append(f"- `{a}`")
        lines.append("")

    # Write report
    report_text = '\n'.join(lines)
    with open(output_path, 'w') as f:
        f.write(report_text)

    print(report_text)
    return report_text


def main():
    if len(sys.argv) < 3:
        print("Usage: port_report.py <log.json> <output.md>")
        print("")
        print("The log.json should have this structure:")
        print(json.dumps({
            "source_version": "rocksdb v6.26.1",
            "target_version": "frocksdb 6.20.3",
            "patches": [{
                "name": "0001_autumn.patch",
                "intent": "Add autumn_c option",
                "status": "PORTED",
                "compilation": "PASS",
                "hunks": [{
                    "file": "include/rocksdb/advanced_options.h",
                    "category": "DIRECT_PORT",
                    "reason": "",
                    "adaptation_notes": ""
                }]
            }],
            "warnings": [],
            "artifacts": ["0001_autumn_frocksdb-6.20.3.patch"]
        }, indent=2))
        sys.exit(1)

    log_path = sys.argv[1]
    output_path = sys.argv[2]

    log_data = load_log(log_path)
    generate_report(log_data, output_path)
    print(f"\nReport written to: {output_path}")


if __name__ == '__main__':
    main()
