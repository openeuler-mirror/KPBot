"""Load collection bundles (folders or .tar.gz).

A bundle that cannot be read never aborts the run: it is returned as a
LoadError and the report records why.
"""
import json
import pathlib
import shutil
import tarfile
from collections import Counter

SUPPORTED_SCHEMA = 1


class LoadError(Exception):
    pass


def _checked_child(parent, candidate):
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as e:
        raise LoadError('cannot resolve %s: %s' % (candidate, e))
    if parent not in resolved.parents:
        raise LoadError('path escapes %s: %s' % (parent, candidate))
    return resolved


def _read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def safe_name(name):
    """Folder-name mangling shared with the collector, so paths line up."""
    import re
    return re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(name)).strip('._') or 'container'


MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def extract_tar(path, dest):
    """Extract a collection archive, refusing anything but plain files/dirs."""
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    with tarfile.open(str(path), 'r:*') as tar:
        for member in tar.getmembers():
            name = pathlib.PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise LoadError('archive member escapes the output folder: ' + member.name)
            if not (member.isfile() or member.isdir()):
                raise LoadError('archive contains a link or device node: ' + member.name)
            # 采集包正常是几 MB；损坏或被塞过的包不该把磁盘写满
            if member.size > MAX_MEMBER_BYTES:
                raise LoadError('archive member is too large (%d bytes): %s'
                                % (member.size, member.name))
            written += member.size
            if written > MAX_TOTAL_BYTES:
                raise LoadError('archive expands to more than %d bytes; refusing'
                                % MAX_TOTAL_BYTES)
            target = dest / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, open(str(target), 'wb') as out:
                shutil.copyfileobj(src, out)
    return dest


def find_collection_root(folder):
    """A bundle root is the folder holding collection_meta.json.

    中断留下的目录可能没有 meta。只要里面有 inventory.json，
    就按「不完整的包」处理，而不是整个跳过。
    """
    folder = pathlib.Path(folder)
    if (folder / 'collection_meta.json').is_file():
        return folder
    found = [p.parent for p in sorted(folder.rglob('collection_meta.json'))
             if (p.parent / 'collection_meta.json').is_file()]
    # The archive wraps one collection folder; deeper matches are per-container copies.
    found = [p for p in found if p.parent == folder] or found
    if not found:
        if list(folder.glob('*/inventory.json')):
            return folder          # 不完整的包，load_bundle 会补一个最小 meta
        raise LoadError('no collection_meta.json found under ' + str(folder))
    if len(found) > 1:
        raise LoadError('more than one collection found under %s: %s'
                        % (folder, ', '.join(str(p.name) for p in found)))
    return found[0]


def open_source(source, workdir):
    """Return the collection root for a folder or archive path."""
    path = pathlib.Path(source).expanduser()
    if path.is_dir():
        return find_collection_root(path)
    if not path.is_file():
        raise LoadError('not a folder or file: ' + str(path))
    if not tarfile.is_tarfile(str(path)):
        raise LoadError('not a collection folder or tar archive: ' + str(path))
    return find_collection_root(extract_tar(path, workdir))


def load_bundle(label, source, workdir):
    """Read one bundle into
    {label, source, root, meta, containers:[{...}], incomplete}.

    Raises LoadError with a readable reason; callers record it per bundle.
    """
    root = open_source(source, workdir).resolve()
    incomplete = None
    meta_file = _checked_child(root, root / 'collection_meta.json')
    if meta_file.is_file():
        try:
            meta = _read_json(meta_file)
        except (OSError, ValueError) as e:
            raise LoadError('unreadable collection_meta.json: %s' % e)
        if not isinstance(meta, dict):
            raise LoadError('collection_meta.json must be a JSON object')
        schema = meta.get('schema')
        if schema != SUPPORTED_SCHEMA:
            raise LoadError('unsupported collector schema %r (expected %d)'
                            % (schema, SUPPORTED_SCHEMA))
    else:
        # 采集被硬杀掉（kill -9）时可能连 meta 都没来得及写
        names = sorted(p.parent.name for p in root.glob('*/inventory.json'))
        if not names:
            raise LoadError('既没有 collection_meta.json 也没有任何 inventory.json')
        meta = {'schema': SUPPORTED_SCHEMA, 'role': None, 'node': None,
                'timestamp_utc': None, 'targets': names,
                'reconstructed': True}
        incomplete = ('这个采集包没有 collection_meta.json，已按目录名重建；'
                      '采集可能是被硬中断的')
    containers = []
    names = meta.get('targets') or []
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
        raise LoadError('collection_meta.json has no valid targets')
    folders = meta.get('folders', {})
    if not isinstance(folders, dict):
        raise LoadError('folders must be a JSON object')
    locations = [folders.get(name, safe_name(name)) for name in names]
    counts = Counter(location for location in locations if isinstance(location, str))
    for name, location in zip(names, locations):
        entry = {'container': name, 'folder': None, 'inventory': None,
                 'docker': {}, 'error': None}
        containers.append(entry)
        try:
            if (not isinstance(location, str) or location in ('', '.', '..')
                    or pathlib.Path(location).name != location or counts[location] != 1):
                raise LoadError('invalid or ambiguous object folder: %r' % location)
            folder = root / location
            if folder.is_symlink():
                raise LoadError('object folder is a symbolic link: %s' % location)
            folder = _checked_child(root, folder)
            entry['folder'] = folder
            inv = _checked_child(folder, folder / 'inventory.json')
            err = _checked_child(folder, folder / 'ERROR.txt')
            dsafe = _checked_child(folder, folder / 'docker_inspect_safe.json')
        except LoadError as e:
            entry['error'] = str(e)
            continue
        if inv.is_file():
            try:
                entry['inventory'] = _read_json(inv)
            except (OSError, ValueError) as e:
                entry['error'] = 'unreadable inventory.json: %s' % e
        else:
            reason = ''
            if err.is_file():
                try:
                    reason = err.read_text(encoding='utf-8', errors='replace').strip()
                except OSError:
                    reason = ''
            entry['error'] = reason.splitlines()[0] if reason else 'inventory.json is missing'
        if dsafe.is_file():
            try:
                entry['docker'] = _read_json(dsafe)
            except (OSError, ValueError):
                entry['docker'] = {}
    return {'label': label, 'source': str(source), 'root': root,
            'meta': meta, 'containers': containers,
            'incomplete': incomplete}
