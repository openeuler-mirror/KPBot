"""Read-only evidence collection; never starts a server or repairs dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# Run in the requested serving interpreter, not the skill interpreter.
PROBE = r'''
import importlib, importlib.machinery, importlib.metadata, json, os, re, sys, traceback
names = ('vllm', 'vllm_ascend', 'mooncake', 'mooncake.engine', 'mooncake.store')
result = {'python': sys.executable, 'version': sys.version, 'argv': sys.argv,
          'sys_path': sys.path, 'cwd': os.getcwd(), 'resolved_without_import': {},
          'path_candidates': {}, 'imports': [],
          'connector_environment': {k: v for k, v in os.environ.items()
              if k in ('ASCEND_ENABLE_USE_FABRIC_MEM', 'VLLM_PLUGINS',
                       'VLLM_PLATFORM_PLUGINS', 'PYTHONPATH', 'MOONCAKE_CONFIG_PATH',
                       'VLLM_KV_CONNECTOR', 'VLLM_KV_ROLE', 'VLLM_KV_CONNECTOR_MODULE_PATH')},
          'connector_selection': {}}

def candidates(name):
    found = []
    for entry in sys.path:
        base = entry or os.getcwd()
        for suffix in (name + '.py', name):
            path = os.path.abspath(os.path.join(base, suffix))
            if os.path.isfile(path) or os.path.isdir(path):
                found.append({'path': path, 'kind': 'module' if suffix.endswith('.py') else 'package'})
    return found

for name in names:
    result['path_candidates'][name] = candidates(name)
    path = None
    spec = None
    try:
        for i, part in enumerate(name.split('.')):
            prefix = '.'.join(name.split('.')[:i+1])
            spec = importlib.machinery.PathFinder.find_spec(prefix, path)
            if spec is None:
                break
            path = spec.submodule_search_locations
            if path is None and prefix != name:
                spec = None
                break
        result['resolved_without_import'][name] = None if spec is None else {
            'origin': spec.origin, 'loader': repr(spec.loader),
            'search_locations': list(spec.submodule_search_locations or ())}
    except Exception:
        result['resolved_without_import'][name] = {'traceback': traceback.format_exc()}

mooncake = result['resolved_without_import'].get('mooncake')
module_candidates = [
    item['path'] for item in result['path_candidates']['mooncake']
    if item['kind'] == 'module'
]
result['import_safety'] = {
    'resolved_mooncake_is_package': None if mooncake is None or 'traceback' in mooncake
        else bool(mooncake['search_locations']),
    'module_candidates': module_candidates,
    'guidance': [],
}
if module_candidates or (mooncake and not mooncake.get('search_locations')
                         and 'traceback' not in mooncake):
    result['import_safety']['guidance'] = [
        'A resolved non-package mooncake cannot provide mooncake.engine or mooncake.store; '
        'candidate files alone do not prove which module the failed worker loaded.',
        'Keep this origin/sys.path evidence. Extract the skill into a NEW directory, '
        'not over a previous extraction. Nothing is automatically deleted.',
        'Run the clean CLI by absolute path from the original launch directory with '
        'the serving interpreter. If its resolved origin is a leftover local mooncake.py, '
        'preserve/review it and have the operator move or rename only that obsolete file '
        'off the serving search path; never remove the installed Mooncake package.',
        'Repeat diagnose after the operator correction. A valid namespace package need '
        'not have __init__.py; do not install packages or add dependency fallbacks.',
    ]

for key in ('VLLM_KV_CONNECTOR', 'VLLM_KV_ROLE', 'VLLM_KV_CONNECTOR_MODULE_PATH',
            'MOONCAKE_CONFIG_PATH'):
    if os.environ.get(key) is not None:
        result['connector_selection'][key] = os.environ[key]
for name in sys.argv[1:]:
    if name.startswith(('kv_connector', 'kv_role', 'kv_transfer', 'mooncake')):
        result['connector_selection'].setdefault('probe_args', []).append(name)

for name in sys.argv[1:]:
    record = {'name': name}
    try:
        module = importlib.import_module(name)
        record.update(status='imported', origin=getattr(module, '__file__', None),
                      package_path=list(getattr(module, '__path__', ())))
    except BaseException:
        record.update(status='failed', traceback=traceback.format_exc())
    result['imports'].append(record)
result['loaded_modules'] = {name: {'origin': getattr(mod, '__file__', None),
    'package_path': list(getattr(mod, '__path__', ())),
    'loader': repr(getattr(mod, '__loader__', None))}
    for name, mod in tuple(sys.modules.items())
    if name.split('.')[0] in ('mooncake', 'vllm', 'vllm_ascend') and mod is not None}
print(json.dumps(result, ensure_ascii=False, default=str))
'''

SECRET_KEY = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|credential)"
)
SECRET_VALUE = re.compile(
    r'''(?i)((?:password|passwd|secret|token|api[_-]?key|authorization|credential)\s*[=:]\s*)(["']?)[^\s,;\]}"']+'''
)
URL_PASSWORD = re.compile(r"(://[^\s/:@]+:)[^\s/@]+(@)")
BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact(value):
    """Best-effort credential redaction; retain stack frames and error messages."""
    if isinstance(value, dict):
        return {
            key: "<redacted>" if SECRET_KEY.search(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = URL_PASSWORD.sub(r"\1<redacted>\2", value)
        value = BEARER.sub(r"\1<redacted>", value)
        return SECRET_VALUE.sub(r"\1<redacted>", value)
    return value

def _connector_summary(value):
    """Keep only connector-related config keys; never initialize serving code."""
    wanted = ("connector", "kv_role", "kv_rank", "kv_parallel", "kv_ip", "kv_port", "engine_id")
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(token in str(key).lower() for token in wanted):
                out[str(key)] = item if not isinstance(item, (dict, list)) else _connector_summary(item)
            elif isinstance(item, dict):
                nested = _connector_summary(item)
                if nested:
                    out[str(key)] = nested
        return out
    if isinstance(value, list):
        return [_connector_summary(item) for item in value if isinstance(item, (dict, list))]
    return {}


def collect(args: argparse.Namespace) -> int:
    package = Path(__file__).resolve().parent.parent
    payload = package / "payload" / "vllm-ascend-timing.json"
    evidence = {
        "revision": "python-put-import-audit-2026-11-07-r4",
        "payload_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "limitations": [
            "Default resolution does not import serving packages or prove their initialization.",
            "Opt-in imports run in a fresh subprocess, not the failed P worker.",
            "No server is started; existing traceback/configuration are operator-supplied evidence.",
            "Redaction is best effort; review locally before sharing.",
        ],
    }
    for name in ("traceback_file", "config_file"):
        path = getattr(args, name)
        if path:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            if name == "config_file":
                try:
                    text = json.loads(text)
                except json.JSONDecodeError:
                    pass  # A startup-argument text file is also valid evidence.
            evidence[name] = {"path": str(path), "content": text}
    if "config_file" in evidence:
        evidence["config_connector_summary"] = _connector_summary(evidence["config_file"]["content"])
    proc = subprocess.run(
        [args.python, "-B", "-c", PROBE, *args.probe_import],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    evidence["probe_exit_code"] = proc.returncode
    evidence["probe_stderr"] = proc.stderr
    try:
        evidence["probe"] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        evidence["probe_stdout"] = proc.stdout
    print(json.dumps(redact(evidence), ensure_ascii=False, indent=2))
    imports = evidence.get("probe", {}).get("imports", [])
    return int(proc.returncode != 0 or any(item["status"] == "failed" for item in imports))


def register(subparsers) -> None:
    parser = subparsers.add_parser("diagnose", help="collect read-only import/startup evidence")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--traceback-file", type=Path, help="existing complete failed startup log")
    parser.add_argument("--config-file", type=Path, help="existing connector JSON or startup arguments")
    parser.add_argument(
        "--probe-import", action="append", default=[], metavar="MODULE",
        help="optional real import in a fresh subprocess; may run package initialization",
    )
    parser.set_defaults(func=collect)
