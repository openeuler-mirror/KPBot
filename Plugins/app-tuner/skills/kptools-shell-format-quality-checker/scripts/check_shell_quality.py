#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


SHEBANG = re.compile(
    rb'#![ \t]*(?P<prefix>/bin/|/usr/bin/|/usr/bin/env[ \t]+)(?P<shell>bash|sh)[ \t]*'
)
CONTINUATION = re.compile(r'(?<!\\)(?:\\\\)*\\[ \t]+$')
SYNTAX_LINE = re.compile(rb'(?m)^.*?: (?:line )?(\d+):')


def collect_paths(inputs, errors):
    def walk_error(exc):
        errors.append({'file': str(exc.filename), 'check': 'input', 'message': str(exc)})

    files = {}
    for path in inputs:
        if not path.is_symlink() and path.is_dir():
            found = False
            error_count = len(errors)
            for root, dirs, names in os.walk(path, onerror=walk_error):
                dirs.sort()
                for name in sorted(names):
                    candidate = Path(root, name)
                    if candidate.suffix == '.sh' and candidate.is_file() and not candidate.is_symlink():
                        files.setdefault(candidate.resolve(), candidate)
                        found = True
            if not found and len(errors) == error_count:
                errors.append({'file': str(path), 'check': 'input', 'message': 'No .sh files found'})
        elif not path.is_symlink() and path.is_file():
            files.setdefault(path.resolve(), path)
        else:
            errors.append({
                'file': str(path), 'check': 'input', 'message': 'Not a regular file or directory',
            })
    return files.values()


def check_bytes(data, add):
    blocked = False
    if data.startswith(b'\xef\xbb\xbf'):
        add(1, 'error', 'bom', 'UTF-8 BOM at the start of the file')
        blocked = True
    if b'\0' in data:
        add(data.count(b'\n', 0, data.index(b'\0')) + 1, 'error', 'nul', 'NUL byte in the script')
        blocked = True
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        add(data.count(b'\n', 0, exc.start) + 1, 'error', 'encoding', f'Invalid UTF-8 at byte {exc.start + 1}')
        text = None
        blocked = True
    if b'\r' in data:
        lines = data.split(b'\n')
        for number, raw in enumerate(lines, 1):
            if number < len(lines) and raw.endswith(b'\r'):
                add(number, 'error', 'crlf', 'Line ends in CRLF (0d 0a)')
                raw = raw[:-1]
                blocked = True
            if b'\r' in raw:
                add(number, 'error', 'bare-cr', 'CR byte not part of CRLF')
                blocked = True
    return text, blocked


def check_style(path, text, add):
    if path.suffix != '.sh' or any(char.isspace() for char in path.name):
        add(None, 'warning', 'filename', 'Expected a .sh filename without whitespace')
    if not text:
        return
    if not text.endswith('\n'):
        add(None, 'warning', 'missing-final-newline', 'File does not end in LF')
    lines = text.replace('\r\n', '\n').split('\n')
    indents = {}
    for number, line in enumerate(lines, 1):
        if number < len(lines) and CONTINUATION.search(line):
            add(number, 'warning', 'continuation-space',
                'Unpaired backslash before trailing blanks; check whether it was meant to continue a command')
        elif line.endswith((' ', '\t')):
            add(number, 'warning', 'trailing-whitespace', 'Line ends in spaces or tabs')
        if line.startswith((' ', '\t')) and line.strip(' \t'):
            indents.setdefault(line[0], number)
        if len(line) > 80:
            add(number, 'info', 'long-line', f'{len(line)} characters (limit: 80)')
    if len(indents) == 2:
        space, tab = indents[' '], indents['\t']
        add(None, 'info', 'mixed-indentation', f'Space indentation at line {space}; tab indentation at line {tab}')


def tool_env():
    blocked = {'BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS', 'SHELLCHECK_OPTS', 'POSIXLY_CORRECT'}
    env = {key: value for key, value in os.environ.items()
           if key not in blocked and not key.startswith('BASH_FUNC_')}
    env['LC_ALL'] = 'C'
    return env


def check_file(path, mode, analyzer, analyzer_state, report, env):
    file = str(path)
    status = {'file': file, 'shell': None, 'syntax': 'not-run', 'shellcheck': analyzer_state}
    report['files'].append(status)

    def add(line, severity, check, message):
        report['findings'].append(dict(file=file, line=line, severity=severity, check=check, message=message))

    def failed(check, message):
        report['errors'].append(dict(file=file, check=check, message=message))

    try:
        data = path.read_bytes()
    except OSError as exc:
        failed('input', str(exc))
        return
    text, blocked = check_bytes(data, add)
    if text is not None and '\0' not in text:
        check_style(path, text, add)
    if blocked:
        status['syntax'] = 'blocked-by-format'
        if analyzer:
            status['shellcheck'] = 'blocked-by-format'
        return
    interpreter = None
    if mode == 'auto':
        match = SHEBANG.fullmatch(data.partition(b'\n')[0])
        if not match:
            failed('interpreter', 'No supported sh/bash shebang; specify --shell only when the caller is known')
            return
        mode = match['shell'].decode('ascii')
        prefix = match['prefix'].decode('ascii')
        if prefix.endswith('/') and os.name != 'nt':
            interpreter = prefix + mode
    status['shell'] = mode
    interpreter = interpreter or shutil.which(mode)
    if interpreter is None:
        failed('syntax', f'{mode} is not available on PATH')
    else:
        argv = [interpreter, '--noprofile', '--norc', '-n'] if mode == 'bash' else [interpreter, '-n']
        try:
            result = subprocess.run(argv, input=data, capture_output=True, env=env)
        except OSError as exc:
            failed('syntax', str(exc))
        else:
            if result.returncode == 2:
                status['syntax'] = 'failed'
                location = SYNTAX_LINE.search(result.stderr)
                add(int(location[1]) if location else None, 'error', 'syntax', f'{mode} -n rejected the script')
            elif result.returncode == 0:
                status['syntax'] = 'passed'
            else:
                failed('syntax', f'{mode} -n exited with status {result.returncode}')

    if analyzer:
        argv = [analyzer, '--norc', '--format=json', '--severity=warning', f'--shell={mode}', '-']
        try:
            result = subprocess.run(argv, input=data, capture_output=True, env=env)
            if result.returncode not in (0, 1):
                raise ValueError(f'exited with status {result.returncode}')
            findings = json.loads(result.stdout)
            if not isinstance(findings, list):
                raise ValueError('expected a JSON array of findings')
            records = [(row['line'], row['level'], f"SC{row['code']}: {row['message']}") for row in findings]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            status['shellcheck'] = 'error'
            failed('shellcheck', f'ShellCheck could not complete: {exc}')
        else:
            status['shellcheck'] = 'findings' if records else 'passed'
            for line, level, message in records:
                add(line, level, 'shellcheck', message)


def main():
    parser = argparse.ArgumentParser(description='Read-only checks for Linux deployment shell scripts.')
    parser.add_argument('--shell', default='auto', choices=('auto', 'bash', 'sh'))
    parser.add_argument('--skip-shellcheck', action='store_true')
    parser.add_argument('paths', nargs='+', type=Path, help='files or directories (recursively checks .sh files)')
    args = parser.parse_args()
    analyzer = None if args.skip_shellcheck else shutil.which('shellcheck')
    analyzer_state = 'not-run' if analyzer else 'skipped' if args.skip_shellcheck else 'unavailable'
    report = {'files': [], 'findings': [], 'errors': []}
    env = tool_env()
    for path in collect_paths(args.paths, report['errors']):
        check_file(path, args.shell, analyzer, analyzer_state, report, env)
    print(json.dumps(report, indent=2))
    if report['errors'] or any(item['check'] == 'continuation-space' for item in report['findings']):
        return 2
    return int(any(item['severity'] == 'error' for item in report['findings']))


if __name__ == '__main__':
    raise SystemExit(main())
