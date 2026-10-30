#!/usr/bin/env bash
# Source-checkout test: pack the actual npm artifact, then run its entries
# with a fresh Python environment without pip/httpx. No backend is contacted.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/kpbot package test.XXXXXX")"
case "$TMPROOT" in "${TMPDIR:-/tmp}"/kpbot\ package\ test.*) ;; *) exit 1 ;; esac
trap 'rm -rf "${TMPROOT:?}"' EXIT

npm --prefix "$ROOT" pack --ignore-scripts --json --pack-destination "$TMPROOT" \
    --cache "$TMPROOT/npm-cache" > "$TMPROOT/pack.json"
ARCHIVE="$(python3 - "$TMPROOT/pack.json" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1]))
item = value[0] if isinstance(value, list) else value['@kunpengbot/kpbot']
paths = [entry['path'] for entry in item['files']]
required = ('Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/cli/configure.py',
             'Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/cli/backend.py',
             'Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/backends/tencentdb_http.py',
            'Plugins/memory/skills/kpbot-memory/references/getting-started.md',
            'Plugins/memory/skills/kpbot-memory/references/deployment.md',
            'Plugins/memory/docs/backend-contract.md')
required += ('Plugins/memory/docs/backend-contract.html',
             'Plugins/memory/docs/backend-development.md')
assert all(path in paths for path in required), 'missing memory delivery file'
assert not any(path.startswith('tools/') or path.endswith('/setup-openviking.py') for path in paths), \
    'source-only development helper leaked into package'
assert not any(path.startswith(('.runtime/', 'kpbot-memory-test/')) or
               '__pycache__/' in path or path.endswith('.pyc') for path in paths)
print(item['filename'])
PY
)"
mkdir "$TMPROOT/package copy"
tar -xzf "$TMPROOT/$ARCHIVE" -C "$TMPROOT/package copy"
PACKAGE="$TMPROOT/package copy/package"
python3 -m venv --without-pip "$TMPROOT/fresh python"
PYTHON="$TMPROOT/fresh python/bin/python3"
"$PYTHON" -I -c 'import importlib.util; assert importlib.util.find_spec("httpx") is None'
PATH="$TMPROOT/fresh python/bin:$PATH" "$PACKAGE/bin/kpbot" memory backend --help > "$TMPROOT/backend-help.txt"
PATH="$TMPROOT/fresh python/bin:$PATH" "$PACKAGE/bin/kpbot" memory configure --help > "$TMPROOT/configure-help.txt"
"$PYTHON" - "$PACKAGE" <<'PY'
from pathlib import Path
import re
import sys
package = Path(sys.argv[1])
root = package / 'Plugins/memory/skills/kpbot-memory'
sys.path.insert(0, str(root / 'scripts'))
from kpbot_memory.cli.backend import create_backend
for name, expected in [('openviking', 'OpenVikingBackend'), ('mem0', 'Mem0Backend'),
                       ('tencentdb', 'TencentDBBackend')]:
    adapter = create_backend(name)
    assert type(adapter).__name__ == expected
    assert type(adapter).__module__ == 'kpbot_memory.backends.' + name + '_http'
assert 'httpx' not in sys.modules, 'adapter construction must not import optional dependencies'
for page in [root / 'SKILL.md', *root.glob('references/*.md')]:
    for link in re.findall(r'\]\(([^)]+)\)', page.read_text(encoding='utf-8')):
        link = link.strip('<>')
        if link.startswith(('http://', 'https://', '#')):
            continue
        target = (page.parent / link.split('#', 1)[0]).resolve()
        assert target.is_relative_to(root) and target.exists(), (page.name, link)
plugin = package / 'Plugins/memory'
for page in [plugin / 'README.md', *plugin.glob('docs/*.md'), *plugin.glob('docs/*.html')]:
    text = page.read_text(encoding='utf-8')
    links = re.findall(r'\]\(([^)]+)\)', text) if page.suffix == '.md' else re.findall(r'href="([^"]+)"', text)
    for link in links:
        link = link.strip('<>')
        if link.startswith(('http://', 'https://', '#')):
            continue
        target = (page.parent / link.split('#', 1)[0]).resolve()
        assert target.is_relative_to(package) and target.exists(), (page.name, link)
assert '--progress' in (Path(sys.argv[1]).parents[1] / 'backend-help.txt').read_text()
assert '--backend' in (Path(sys.argv[1]).parents[1] / 'configure-help.txt').read_text()
print('PASS: actual npm package, spaced paths, clean Python help, bundled docs and exclusions')
PY
