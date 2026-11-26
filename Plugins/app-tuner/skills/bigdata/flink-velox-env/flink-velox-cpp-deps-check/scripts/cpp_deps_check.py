#!/usr/bin/env python3
"""cpp_deps_check.py — read-only check of the C++ libraries the GlutenFlink stack needs, on two levels.

Level 1, package-manager libraries: 13 libraries probed by header under
/usr/include (also its <triplet>/ subdirectories) and /usr/local/include, each
with its openEuler -devel package.
Level 2, Velox's source-built libraries: the list is parsed from the
install_velox_deps call sequence of the velox checkout's own setup script
(package-manager and conda entries excluded); each library is probed under
/usr/local (cmake config dir, include dir, header, or a probe binary such as
protoc for protobuf).
The script classifies only: it installs nothing, builds nothing, and writes
nowhere except the optional --out file.

Usage:
  cpp_deps_check.py [--velox-dir DIR] [--json] [--out FILE [--overwrite]]

--out FILE also archives the JSON result. The file must be a regular path inside
the project directory (the working directory) — a symlink or a path outside it
is refused, and an existing file is kept unless --overwrite is given.

The velox checkout: --velox-dir, else $VELOX_HOME, else $GFV_REPOS_DIR/velox,
else a repos/velox walked upward from the working directory (bounded by
ENV_CHECK_WALK_ROOT when that tier-A seam is set). Without it the level-2 probe
is reported as skipped, the verdict is limited to "system headers present", and
the check does not fail.

Exit codes: 0 every probed library present (a skipped level 2 is allowed);
1 at least one missing; 2 check error (JSON in the full contract shape,
error_message "check aborted: ..."), a refused --out, or an invalid command line
(usage on stderr, no JSON).

Test seams, used by the tier-A case only: ENV_CHECK_SYSROOT prefixes every
absolute system path (/etc/os-release, /usr/include, /usr/local, ...);
ENV_CHECK_WALK_ROOT bounds the upward search for repos/velox.
"""
import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys

KEY = 'flink_velox_cpp_deps_check_result'

SYSROOT = os.environ.get('ENV_CHECK_SYSROOT', '')

STATUS_ORDER = ('ok', 'info', 'skipped', 'warn', 'fail')

# multiarch <triplet>/ include directory by machine, used only when no compiler
# can be asked (gcc -dumpmachine is the primary source)
TRIPLET_BY_MACHINE = {'aarch64': 'aarch64-linux-gnu', 'x86_64': 'x86_64-linux-gnu',
                      'armv7l': 'arm-linux-gnueabihf', 'ppc64le': 'powerpc64le-linux-gnu',
                      'riscv64': 'riscv64-linux-gnu', 's390x': 's390x-linux-gnu'}

# <lib>: (<header candidates>, <openEuler package>) — the GFVBot installer's table.
CPP_DEPS = (
    ('libevent', ('event2/event.h',), 'libevent-devel'),
    ('openssl', ('openssl/ssl.h',), 'openssl-devel'),
    ('re2', ('re2/re2.h',), 're2-devel'),
    ('zstd', ('zstd.h',), 'zstd-devel'),
    ('lz4', ('lz4.h',), 'lz4-devel'),
    ('double-conversion', ('double-conversion/double-conversion.h',), 'double-conversion-devel'),
    ('libdwarf', ('libdwarf.h', 'libdwarf-0/libdwarf.h', 'libdwarf/libdwarf.h'), 'libdwarf-devel'),
    ('libelf', ('libelf.h',), 'elfutils-libelf-devel'),
    ('curl', ('curl/curl.h',), 'libcurl-devel'),
    ('icu', ('unicode/uversion.h',), 'libicu-devel'),
    ('sodium', ('sodium.h',), 'libsodium-devel'),
    ('zlib', ('zlib.h',), 'zlib-devel'),
    ('librdkafka', ('librdkafka/rdkafka.h',), 'librdkafka-devel'),
)
# Libraries Velox's setup script builds from source into /usr/local are not
# listed here: the list is parsed from the checkout's own script.
SOURCE_PROBE_OVERRIDE = {'protobuf': 'protoc'}


def sys_path(rel):
    """Absolute system path under the (optional) sysroot prefix."""
    return SYSROOT + rel


def is_exec(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def make_item(name, status, detail, fix=None, **extra):
    item = {'name': name, 'status': status, 'detail': detail}
    if fix:
        item['fix'] = fix
    item.update(extra)
    return item


def worst(statuses):
    statuses = list(statuses)
    return max(statuses, key=STATUS_ORDER.index) if statuses else 'ok'


# --- level 1: package-manager libraries -------------------------------------
def multiarch_triplet():
    """The <triplet>/ include directory of the *target*: what the compiler on PATH
    says (`gcc -dumpmachine`), else the table for this machine. Never every
    directory under /usr/include — another architecture's headers do not make a
    library present for this build (aarch64-linux-gnu/curl/curl.h on an x86_64
    host is not the x86_64 curl development package)."""
    try:
        proc = subprocess.run(['gcc', '-dumpmachine'], capture_output=True, text=True,
                              errors='replace', timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None:
        lines = (proc.stdout or '').strip().splitlines()
        if lines and re.fullmatch(r'[a-z0-9_]+(-[a-z0-9_]+){2,3}', lines[0].strip()):
            return lines[0].strip()
    return TRIPLET_BY_MACHINE.get(platform.machine())


def header_dirs():
    dirs = [sys_path('/usr/include/')]
    triplet = multiarch_triplet()
    if triplet:
        dirs.append(sys_path(f'/usr/include/{triplet}/'))
    dirs.append(sys_path('/usr/local/include/'))
    return dirs


def header_exists(headers):
    """The first existing header path, or None: /usr/include, its <triplet>/
    subdirectory for the target, then /usr/local/include."""
    return next((d + h for h in headers for d in header_dirs() if os.path.isfile(d + h)), None)


def scan_system():
    payload = {'expected': len(CPP_DEPS), 'present': 0, 'missing': []}
    items = []
    for lib, headers, package in CPP_DEPS:
        found = header_exists(headers)
        if found:
            items.append(make_item(lib, 'ok', f'header found: {found}'))
        else:
            payload['missing'].append(lib)
            items.append(make_item(lib, 'fail', f'no header {headers[0]} under /usr/include '
                                                f'or /usr/local/include',
                                   f'system package (openEuler name): {package}', package=package))
    payload['present'] = len(CPP_DEPS) - len(payload['missing'])
    return payload, items


# --- level 2: Velox's source-built libraries --------------------------------
def read_os_release():
    info = {}
    try:
        with open(sys_path('/etc/os-release'), encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                m = re.match(r'^([A-Z][A-Z0-9_]*)=(.*)$', raw.strip())
                if m:
                    info[m.group(1)] = m.group(2).strip().strip('"')
    except OSError:
        pass
    return info


def velox_setup_script(os_id, os_version):
    """The velox setup script whose install functions fit this OS — the same
    choice the installer makes; None where the checkout ships none."""
    os_id = (os_id or '').lower()
    major = re.match(r'\d+', os_version or '')
    if os_id in ('ubuntu', 'debian'):
        return 'setup-ubuntu.sh'
    if os_id in ('centos', 'rhel') and major and major.group(0) == '7':
        return None
    return 'setup-centos9.sh'


def locate_velox(explicit):
    """(checkout path or None, how it was given, error or None)."""
    candidates = ((explicit, '--velox-dir'),
                  (os.environ.get('VELOX_HOME'), 'VELOX_HOME'),
                  (os.path.join(os.environ['GFV_REPOS_DIR'], 'velox')
                   if os.environ.get('GFV_REPOS_DIR') else None, 'GFV_REPOS_DIR/velox'))
    for path, how in candidates:
        if not path:
            continue
        if os.path.isdir(path):
            return os.path.realpath(path), how, None
        if how == '--velox-dir' or how == 'VELOX_HOME':
            return None, how, f'{path} is not a directory'
    # a repos/velox walked upward from the working directory, bounded by
    # ENV_CHECK_WALK_ROOT when set (a tier-A seam: the fake machine must never
    # reach a real checkout above the scratch directory); default: to the root
    here = os.getcwd()
    boundary = os.path.realpath(os.environ.get('ENV_CHECK_WALK_ROOT') or '/')
    while True:
        candidate = os.path.join(here, 'repos', 'velox')
        if os.path.isdir(candidate):
            return os.path.realpath(candidate), 'repos/velox walked up from the working directory', None
        parent = os.path.dirname(here)
        if here == boundary or parent == here:
            return None, None, None
        here = parent


def velox_source_libs(setup_path):
    """The install_* calls inside install_velox_deps, in order, minus the
    package-manager and conda entries."""
    libs, inside = [], False
    try:
        with open(setup_path, encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                if not inside:
                    if re.match(r'^function install_velox_deps \{', raw):
                        inside = True
                    continue
                if raw.startswith('}'):
                    break
                m = re.match(r'^\s*run_and_time\s+install_(\S+)\s*$', raw)
                if m and not m.group(1).endswith(('_from_dnf', '_from_apt')) and m.group(1) != 'conda':
                    libs.append(m.group(1))
    except OSError:
        return []
    return libs


def source_lib_present(name):
    """Any credible /usr/local artifact for the library: a cmake config dir
    (name, Capitalized, UPPER), an include dir, a header (with the lib* prefix)
    or a probe override binary."""
    for c in (name, name[:1].upper() + name[1:], name.upper()):
        if os.path.isdir(sys_path(f'/usr/local/lib/cmake/{c}')) \
                or os.path.isdir(sys_path(f'/usr/local/lib64/cmake/{c}')):
            return True
    if os.path.isdir(sys_path(f'/usr/local/include/{name}')):
        return True
    if os.path.isfile(sys_path(f'/usr/local/include/{name}.h')) \
            or os.path.isfile(sys_path(f'/usr/local/include/lib{name}.h')):
        return True
    override = SOURCE_PROBE_OVERRIDE.get(name)
    return bool(override) and is_exec(sys_path(f'/usr/local/bin/{override}'))


def scan_source(velox_path, velox_error, how, os_info):
    payload = {'list_source': None, 'expected': [], 'present': None, 'missing': []}
    if velox_error:
        return payload, [make_item('source-deps', 'warn', f'not probed: {velox_error}')]
    if not velox_path:
        return payload, [make_item(
            'source-deps', 'skipped',
            'not probed: needs the velox checkout — its setup script is the library list '
            '(give --velox-dir or VELOX_HOME)')]
    script = velox_setup_script(os_info.get('ID'), os_info.get('VERSION_ID'))
    if not script:
        return payload, [make_item(
            'source-deps', 'warn',
            'not probed: the velox checkout ships no setup script for CentOS/RHEL 7 '
            '(only setup-ubuntu.sh and setup-centos9.sh), so this OS is not a supported build host')]
    path = os.path.join(velox_path, 'scripts', script)
    if not os.path.isfile(path):
        return payload, [make_item('source-deps', 'warn', f'not probed: {path} not found')]
    libs = velox_source_libs(path)
    payload.update(list_source=path, expected=libs)
    if not libs:
        return payload, [make_item(
            'source-deps', 'warn', f'no install_* calls found in install_velox_deps inside {path}')]
    missing = [lib for lib in libs if not source_lib_present(lib)]
    payload.update(present=len(libs) - len(missing), missing=missing)
    items = [make_item(
        lib, 'fail', 'not found under /usr/local (no cmake config dir, include dir, header or probe binary)',
        f"built by Velox's own setup script: install_{lib}()") for lib in missing]
    if missing:
        items.append(make_item('source-deps', 'info',
                               f'{payload["present"]}/{len(libs)} present (list: {path}; velox checkout via {how})'))
    else:
        items.append(make_item('source-deps', 'ok',
                               f'{len(libs)}/{len(libs)} present under /usr/local (list: {path}; velox checkout via {how})'))
    return payload, items


def next_step(system, source, summary):
    if system['missing']:
        packages = ' '.join(package for lib, _, package in CPP_DEPS if lib in system['missing'])
        return f'install the missing system packages (dnf install {packages}), then rerun'
    if source['missing']:
        return ('build the missing libraries with Velox\'s own setup script ('
                + ', '.join(f'install_{lib}()' for lib in source['missing']) + '), then rerun')
    if summary['status'] == 'skipped':
        return ('system headers present; source-level libraries not probed — give the velox '
                'checkout (--velox-dir or VELOX_HOME) to check them')
    if summary['status'] == 'warn':
        return f"system headers present; source-level libraries not probed ({summary['detail']})"
    return 'ready: every C++ library the stack needs is present'


def build_result(explicit_velox_dir):
    os_info = read_os_release()
    triplet = multiarch_triplet()
    system, system_items = scan_system()
    velox_path, how, velox_error = locate_velox(explicit_velox_dir)
    source, source_items = scan_source(velox_path, velox_error, how, os_info)
    items = system_items + source_items
    summary = next((i for i in items if i['name'] == 'source-deps'),
                   make_item('source-deps', 'skipped', 'not probed'))
    missing = ([f'{lib}: no header {next(h for l, h, _ in CPP_DEPS if l == lib)[0]} under /usr/include '
                f'or /usr/local/include' for lib in system['missing']]
               + [f'{lib}: not found under /usr/local' for lib in source['missing']])
    success = not missing
    result = {
        'success': success,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': worst(item['status'] for item in items),
        'os': {'id': os_info.get('ID'), 'version': os_info.get('VERSION_ID')},
        'triplet': triplet,
        'system': system,
        'source': source,
        'source_probed': bool(source['expected']),
        'velox_dir': velox_path,
        'items': items,
        'missing': missing,
        'next_step': next_step(system, source, summary),
        'error_message': '' if success else f'{len(missing)} item(s) missing: ' + ', '.join(
            system['missing'] + source['missing']),
    }
    return {KEY: result}, 0 if success else 1


def aborted(exc):
    """A check crash in the full contract shape: every documented key exists, so a
    consumer never trips over a missing field while handling the failure."""
    return {KEY: {
        'success': False,
        'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'status': 'fail',
        'os': None,
        'triplet': None,
        'system': {'expected': len(CPP_DEPS), 'present': None, 'missing': []},
        'source': {'list_source': None, 'expected': [], 'present': None, 'missing': []},
        'source_probed': False,
        'velox_dir': None,
        'items': [],
        'missing': [],
        'next_step': 'fix the check error first (see error_message), then rerun',
        'error_message': f'check aborted: {exc!r}',
    }}


def write_out(path, result, overwrite):
    """The --out archive stays inside the project (the working directory), is never a
    symlink, and is never truncated without --overwrite. Returns the exit code to use
    when the write is refused (0 when written); the verdict itself is kept either way."""
    root = os.path.realpath(os.getcwd()) + os.sep
    target = os.path.abspath(path)
    if os.path.islink(target) or not os.path.realpath(target).startswith(root):
        print('cpp_deps_check.py: --out must be a regular path inside the project directory '
              f'(the working directory), not a symlink or a path outside it: {path}', file=sys.stderr)
        return 2
    if os.path.exists(target) and not overwrite:
        print(f'cpp_deps_check.py: --out already exists, pass --overwrite to replace it: {path}',
              file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w' if overwrite else 'x', encoding='utf-8') as fh:  # 'x' never follows a symlink
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:  # the archive is a side output: say so, keep the verdict
        print(f'cpp_deps_check.py: cannot write --out {path}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    return 0


def render(result):
    r = result[KEY]
    if r['error_message'].startswith('check aborted'):
        return f"CHECK ERROR: {r['error_message']}\nnext: {r['next_step']}"
    system, source = r['system'], r['source']
    summary = next((i for i in r['items'] if i['name'] == 'source-deps'),
                   make_item('source-deps', 'skipped', 'not probed'))
    lines = []
    if r['success']:
        if r['source_probed']:
            second = (f'source-level {source["present"]}/{len(source["expected"])} present under '
                      f'/usr/local (list: {source["list_source"]})')
        elif summary['status'] == 'skipped':
            second = f'source-level skipped: {summary["detail"]}'
        else:
            second = f'source-level {summary["status"]}'
        lines.append(f'cpp-deps    OK       {system["present"]}/{system["expected"]} system headers '
                     f'present; {second}')
        for item in r['items']:  # a warning never blocks, but must be seen
            if item['status'] == 'warn':
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
        lines.append('')
        # the conclusion is only "everything present" when the source level was probed
        lines.append('READY: every C++ library the stack needs is present' if r['source_probed']
                     else f"READY: {system['present']}/{system['expected']} system headers present; "
                          f'source-level libraries not probed')
    else:
        lines.append(f'cpp-deps    FAIL     {len(system["missing"])} system, {len(source["missing"])} '
                     f'source-level missing')
        for item in r['items']:
            if item['status'] in ('fail', 'warn') or (item['status'] == 'info' and item['name'] != 'source-deps'):
                lines.append(f"  {item['status'].upper():<5} {item['name']}: {item['detail']}")
                if item.get('fix'):
                    lines.append(f"        fix: {item['fix']}")
        lines.append('')
        lines.append(f"NOT READY: {len(r['missing'])} item(s) missing")
    lines.append(f"next: {r['next_step']}")
    return '\n'.join(line.rstrip() for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the C++ libraries (package-manager '
                                                 'level and Velox source-built level) for the Flink + '
                                                 'GlutenFlink stack.')
    parser.add_argument('--velox-dir', help='the velox checkout whose setup script lists the '
                                            'source-built libraries (default: $VELOX_HOME, '
                                            '$GFV_REPOS_DIR/velox, or repos/velox upward from here)')
    parser.add_argument('--out', help='also write the JSON result to this file (inside the project '
                                      'directory; an existing file is kept unless --overwrite)')
    parser.add_argument('--overwrite', action='store_true', help='let --out replace an existing file')
    parser.add_argument('--json', action='store_true', help='print the JSON result only')
    args = parser.parse_args(argv)
    try:
        result, code = build_result(args.velox_dir)
    except Exception as exc:  # a check bug must never be reported as "present"
        result, code = aborted(exc), 2
    if args.out:
        code = max(code, write_out(args.out, result, args.overwrite))  # a refused write is 2, the verdict still prints
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
