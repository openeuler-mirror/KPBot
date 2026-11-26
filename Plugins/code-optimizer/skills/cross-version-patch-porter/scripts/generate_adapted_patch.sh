#!/usr/bin/env bash
# generate_adapted_patch.sh — Generate adapted patch and README from a single git commit
# Usage: generate_adapted_patch.sh <target_dir> <original_patch_name> <target_version_id> [commit_ref] [output_dir]
#
# Example: generate_adapted_patch.sh /path/to/frocksdb 0001_autumn frocksdb-6.20.3 HEAD /path/to/output
#
# The commit_ref defaults to HEAD. The patch is generated as the diff of that
# commit only (git show), so each ported patch maps 1:1 to its commit.
#
# Produces:
#   0001_autumn_frocksdb-6.20.3.patch
#   0001_autumn_frocksdb-6.20.3.README

set -euo pipefail

TARGET_DIR="$1"
ORIGINAL_PATCH_NAME="$2"
TARGET_VERSION_ID="$3"
COMMIT_REF="${4:-HEAD}"
OUTPUT_DIR="${5:-$(dirname "$TARGET_DIR")}"

ADAPTED_NAME="${ORIGINAL_PATCH_NAME}_${TARGET_VERSION_ID}"
PATCH_FILE="${OUTPUT_DIR}/${ADAPTED_NAME}.patch"
README_FILE="${OUTPUT_DIR}/${ADAPTED_NAME}.README"

cd "$TARGET_DIR"

echo "Generating adapted patch: $PATCH_FILE"

# Check if git repo
if git rev-parse --git-dir > /dev/null 2>&1; then
    # Generate the diff for just this commit, not the whole working tree.
    # A naive `git add -A && git diff --cached` would capture unrelated or
    # uncommitted changes (and be empty if Step 6 already committed the patch).
    # `--format=` strips the commit-message header so the output is a clean diff.
    git show "$COMMIT_REF" --format= > "$PATCH_FILE"
else
    echo "Warning: Not a git repo. Cannot generate diff automatically."
    echo "You will need to generate the patch manually."
    echo "" > "$PATCH_FILE"
fi

# Check if patch file has content
if [ ! -s "$PATCH_FILE" ]; then
    echo "Warning: Patch file is empty (commit has no changes or not a git repo)"
fi

echo "Patch written to: $PATCH_FILE"
echo ""

# Generate README template
cat > "$README_FILE" << HEREDOC
# ${ADAPTED_NAME}.patch Usage Instructions

## Source
Based on ${ORIGINAL_PATCH_NAME} from the source version, adapted for ${TARGET_VERSION_ID}

## Target Version
${TARGET_VERSION_ID}

## How to Apply
\`\`\`bash
cd ${TARGET_VERSION_ID}/
git apply --check ${ADAPTED_NAME}.patch   # Dry-run check first
git apply ${ADAPTED_NAME}.patch           # Apply the patch
\`\`\`

## Dependencies
- Prerequisite patches: (fill in during porting)
- Dependent patches: (fill in during porting)

## Modification Summary
(fill in during porting — list key changes made)

## Differences from Original Patch
(fill in during porting — what was adapted and why)

HEREDOC

echo "README written to: $README_FILE"
echo ""
echo "Done. Please update the README with porting details."
