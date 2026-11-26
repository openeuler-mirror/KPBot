#!/usr/bin/env python3
"""Parse patch files and cross-reference with target directory.

Identifies modified files, groups hunks by intent, detects dependencies,
and auto-ignores build.sh modifications.
"""

import re
import sys
import json
import os
from pathlib import Path
from collections import defaultdict


def parse_unified_diff(patch_path):
    """Parse a unified diff file into structured hunks."""
    with open(patch_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    hunks = []
    current_file = None
    current_hunk = None
    is_new_file = False

    for line in content.splitlines():
        # New file in diff
        m = re.match(r'^--- (?:a/)?(.+)$', line)
        if m:
            if current_hunk and current_file:
                hunks[-1] = current_hunk  # already appended
            current_file = None
            is_new_file = (m.group(1) == '/dev/null')
            continue

        m = re.match(r'^\+\+\+ (?:b/)?(.+)$', line)
        if m:
            current_file = m.group(1)
            continue

        # Hunk header
        m = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$', line)
        if m and current_file:
            current_hunk = {
                'file': current_file,
                'source_start': int(m.group(1)),
                'source_count': int(m.group(2) or 1),
                'target_start': int(m.group(3)),
                'target_count': int(m.group(4) or 1),
                'heading': m.group(5).strip(),
                'added_lines': [],
                'removed_lines': [],
                'context_lines': [],
                'is_new_file': is_new_file,
            }
            hunks.append(current_hunk)
            is_new_file = False
            continue

        if current_hunk is None:
            continue

        if line.startswith('+') and not line.startswith('++'):
            current_hunk['added_lines'].append(line[1:])
        elif line.startswith('-') and not line.startswith('--'):
            current_hunk['removed_lines'].append(line[1:])
        elif line.startswith(' '):
            current_hunk['context_lines'].append(line[1:])

    return hunks


# Files/path suffixes that should always be skipped because they are
# framework/fork-specific build scripts that don't exist in the target.
# This is a sensible default — extend it for your own porting project.
ALWAYS_SKIP_SUFFIXES = ('/build.sh',)


def classify_hunk(hunk, target_dir):
    """Classify a hunk into an assessment category."""
    filepath = hunk['file']

    # Auto-skip framework/fork-specific build scripts that don't exist in the target.
    if filepath.endswith(ALWAYS_SKIP_SUFFIXES):
        return 'SKIP_NO_FILE', f'{filepath} is a framework-specific build script; skipped'

    # Check if file exists in target
    target_path = os.path.join(target_dir, filepath)
    if not os.path.exists(target_path):
        # The file may have been renamed in the target. This script can't guess
        # renames reliably — flag it for manual assessment rather than guessing.
        return 'SKIP_NO_FILE', f'File {filepath} does not exist in target (check for a renamed equivalent)'

    return 'PENDING', ''  # Needs manual assessment


def extract_intent_signals(hunk):
    """Extract signals about the hunk's intent from its content."""
    signals = []

    added = '\n'.join(hunk['added_lines'])
    removed = '\n'.join(hunk['removed_lines'])

    # Field additions
    if re.search(r'^\s*(int|double|bool|std::|uint\d+_t)\s+\w+', added, re.MULTILINE):
        signals.append('field_addition')

    # Include additions
    includes = re.findall(r'#include\s+[<"](.+)[>"]', added)
    if includes:
        signals.append(f'new_includes: {", ".join(includes)}')

    # Function signature changes
    if re.search(r'\(.*\*.*\)', removed) and re.search(r'\(.*shared_ptr.*\)', added):
        signals.append('type_migration: raw_ptr_to_shared_ptr')

    # PREFETCH changes
    if 'PREFETCH' in added or 'PREFETCH' in removed:
        signals.append('prefetch_change')

    # SIMD/Architecture
    if any(kw in added for kw in ['__m256', 'svint', 'svuint', 'arm_sve', 'immintrin']):
        signals.append('simd_optimization')

    # Build detection
    if 'COMMON_FLAGS' in added or 'PLATFORM_CXXFLAGS' in added:
        signals.append('build_detection')

    return signals


def cross_reference(hunks, target_dir):
    """Cross-reference patch hunks with target directory."""
    results = []
    files_touched = defaultdict(list)

    for hunk in hunks:
        category, reason = classify_hunk(hunk, target_dir)
        signals = extract_intent_signals(hunk)

        entry = {
            'file': hunk['file'],
            'heading': hunk['heading'],
            'source_start': hunk['source_start'],
            'category': category,
            'reason': reason,
            'signals': signals,
            'added_count': len(hunk['added_lines']),
            'removed_count': len(hunk['removed_lines']),
            'is_new_file': hunk['is_new_file'],
        }
        results.append(entry)
        files_touched[hunk['file']].append(entry)

    return results, dict(files_touched)


def detect_dependencies(patch_results_list):
    """Detect dependencies between patches based on shared files and types."""
    deps = {}
    for i, (patch_name, results, _) in enumerate(patch_results_list):
        deps[patch_name] = {
            'depends_on': [],
            'files_modified': set(),
            'types_introduced': set(),
            'fields_introduced': set(),
        }
        for r in results:
            deps[patch_name]['files_modified'].add(r['file'])
            for sig in r['signals']:
                if 'field_addition' in sig:
                    # Extract field name from added lines (approximate)
                    deps[patch_name]['fields_introduced'].add(r['file'])
                if 'type_migration' in sig:
                    deps[patch_name]['types_introduced'].add(r['file'])

    # Check for cross-patch dependencies
    patch_names = list(deps.keys())
    for i in range(len(patch_names)):
        for j in range(i + 1, len(patch_names)):
            p1, p2 = patch_names[i], patch_names[j]
            shared_files = deps[p1]['files_modified'] & deps[p2]['files_modified']
            if shared_files:
                deps[p2]['depends_on'].append(p1)

    return deps


def main():
    if len(sys.argv) < 3:
        print("Usage: patch_inventory.py <target_dir> <patch1> [patch2] ...")
        sys.exit(1)

    target_dir = sys.argv[1]
    patch_files = sys.argv[2:]

    all_results = []

    for patch_path in sorted(patch_files):
        patch_name = os.path.basename(patch_path)
        hunks = parse_unified_diff(patch_path)
        results, files_touched = cross_reference(hunks, target_dir)

        all_results.append((patch_name, results, files_touched))

        # Summary for this patch
        categories = defaultdict(int)
        for r in results:
            categories[r['category']] += 1

        print(f"\n{'='*60}")
        print(f"Patch: {patch_name}")
        print(f"Total hunks: {len(results)}")
        for cat, count in sorted(categories.items()):
            print(f"  {cat}: {count}")
        print(f"Files touched: {len(files_touched)}")
        for f, entries in sorted(files_touched.items()):
            signals = set()
            for e in entries:
                signals.update(e['signals'])
            signal_str = f" [{', '.join(signals)}]" if signals else ""
            print(f"  {f}: {len(entries)} hunk(s){signal_str}")

    # Detect dependencies
    deps = detect_dependencies(all_results)
    if any(d['depends_on'] for d in deps.values()):
        print(f"\n{'='*60}")
        print("Patch Dependencies:")
        for name, info in sorted(deps.items()):
            if info['depends_on']:
                print(f"  {name} depends on: {', '.join(info['depends_on'])}")

    # Output JSON report
    report = {
        'patches': [],
        'dependencies': {},
    }
    for patch_name, results, files_touched in all_results:
        report['patches'].append({
            'name': patch_name,
            'hunks': results,
            'files_touched': list(files_touched.keys()),
        })
    for name, info in deps.items():
        report['dependencies'][name] = {
            'depends_on': info['depends_on'],
            'shared_files': list(info['files_modified']),
        }

    json_path = os.path.join(os.path.dirname(target_dir), 'patch_inventory.json')
    with open(json_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nJSON report written to: {json_path}")


if __name__ == '__main__':
    main()
