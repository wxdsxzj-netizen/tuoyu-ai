"""Stdlib-only recovery-set validation and journalled attachment cutover.

Runs on the operator host for preflight, inside the backend container for staging/cutover.
Never extracts links, special files or recovery metadata from an archive.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

RESERVED = {'.restore-staging', '.restore-recovery'}


def members(archive):
    seen = set()
    for item in archive:
        path = PurePosixPath(item.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'uploads'
                or any(p in RESERVED for p in path.parts) or not (item.isfile() or item.isdir())
                or path.as_posix() in seen or (len(path.parts) == 1 and not item.isdir())):
            raise ValueError('Unsafe or duplicate attachment archive member')
        seen.add(path.as_posix())
        yield item, path
    if not seen:
        raise ValueError('Attachment archive is empty')


def preflight(db_file, uploads_file):
    db_file, uploads_file = Path(db_file), Path(uploads_file)
    match = re.fullmatch(r'db-(\d{8}T\d{6}Z)\.dump', db_file.name)
    if not match or uploads_file.name != f'uploads-{match[1]}.tar.gz' or db_file.parent.resolve() != uploads_file.parent.resolve():
        raise ValueError('Recovery set names or directories do not match')
    manifest = db_file.parent / f'SHA256SUMS-{match[1]}'
    for path in (db_file, uploads_file, manifest):
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError('Recovery set must contain three nonempty regular files, not symlinks')
    expected = {}
    for line in manifest.read_text(encoding='ascii').splitlines():
        entry = re.fullmatch(r'([a-f0-9]{64})  ([^/\\]+)', line)
        if not entry or entry[2] in expected:
            raise ValueError('Invalid checksum manifest')
        expected[entry[2]] = entry[1]
    if set(expected) != {db_file.name, uploads_file.name}:
        raise ValueError('Manifest must describe exactly the database and attachment files')
    for path in (db_file, uploads_file):
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected[path.name]:
            raise ValueError('Recovery checksum mismatch')
    with tarfile.open(uploads_file, 'r:gz') as archive:
        list(members(archive))


def safe_root(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Uploads root must be an existing directory, not a symlink')
    for name in RESERVED:
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError('Recovery metadata directory is unsafe')
        path.mkdir(mode=0o700, exist_ok=True)
    return root


def token_path(root, token, kind):
    if not re.fullmatch(r'[a-f0-9]{24}', token):
        raise ValueError('Invalid recovery token')
    return safe_root(root) / kind / token


def stage(root, token, stream):
    destination = token_path(root, token, '.restore-staging')
    destination.mkdir(mode=0o700)  # No reuse of a previous attempt.
    with tempfile.TemporaryFile() as tmp:
        shutil.copyfileobj(stream, tmp)
        tmp.seek(0)
        with tarfile.open(fileobj=tmp, mode='r:gz') as archive:
            entries = list(members(archive))  # Validate ALL entries before writing any.
            for item, path in entries:
                target = destination.joinpath(*path.parts[1:])
                if item.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with target.open('xb') as output, archive.extractfile(item) as source:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o600)


def journal(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as output:
        json.dump(data, output)
        output.flush()
        os.fsync(output.fileno())
    tmp.replace(path)


def cutover(root, token):
    root = safe_root(root)
    staged = token_path(root, token, '.restore-staging')
    if not staged.is_dir() or staged.is_symlink():
        raise ValueError('Staged attachments not found')
    recovery = token_path(root, token, '.restore-recovery')
    recovery.mkdir(mode=0o700)
    old = recovery / 'previous'
    old.mkdir(mode=0o700)
    state = {'phase': 'moving_previous', 'previous': [p.name for p in root.iterdir() if p.name not in RESERVED],
             'incoming': [p.name for p in staged.iterdir()], 'moved_previous': [], 'moved_incoming': []}
    journal_file = recovery / 'journal.json'
    journal(journal_file, state)
    for name in state['previous']:
        (root / name).rename(old / name)
        state['moved_previous'].append(name)
        journal(journal_file, state)
    state['phase'] = 'installing_restored'
    journal(journal_file, state)
    for name in state['incoming']:
        (staged / name).rename(root / name)
        state['moved_incoming'].append(name)
        journal(journal_file, state)
    state['phase'] = 'complete'
    journal(journal_file, state)
    staged.rmdir()


def rollback(root, token):
    root = safe_root(root)
    recovery = token_path(root, token, '.restore-recovery')
    state_file = recovery / 'journal.json'
    state = json.loads(state_file.read_text())
    if state.get('phase') != 'complete':
        raise ValueError('Incomplete or already rolled-back cutover requires journal-guided manual recovery')
    previous = recovery / 'previous'
    if previous.is_symlink() or not previous.is_dir():
        raise ValueError('Previous attachment directory is unsafe')
    replaced = recovery / 'replaced-restored'
    replaced.mkdir(mode=0o700)
    state['phase'] = 'rolling_back'
    journal(state_file, state)
    for path in list(root.iterdir()):
        if path.name not in RESERVED:
            path.rename(replaced / path.name)
    for path in list(previous.iterdir()):
        path.rename(root / path.name)
    state['phase'] = 'rolled_back'
    journal(state_file, state)


if __name__ == '__main__':
    operation = sys.argv[1]
    if operation == 'preflight':
        preflight(*sys.argv[2:4])
    elif operation == 'stage':
        stage('/data/uploads', sys.argv[2], sys.stdin.buffer)
    elif operation == 'cutover':
        cutover('/data/uploads', sys.argv[2])
    elif operation == 'rollback':
        rollback('/data/uploads', sys.argv[2])
    else:
        raise SystemExit('Unknown recovery operation')
