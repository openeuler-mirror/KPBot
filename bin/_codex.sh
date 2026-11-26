#!/usr/bin/env bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adapter_path="$PACKAGE_ROOT/adapters/codex/install.py"
if command -v cygpath >/dev/null 2>&1; then
    adapter_path="$(cygpath -am "$adapter_path")"
fi
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
        exec "$candidate" "$adapter_path" "$@"
    fi
done
echo "KPBot Codex requires Python 3.8 or newer (tried python3 and python)" >&2
exit 1
