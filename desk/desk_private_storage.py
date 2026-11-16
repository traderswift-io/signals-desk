from builtins import open as open_stream
from functools import partial
from logging import getLogger
from os import O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK, O_RDONLY, close, fchmod, fstat, geteuid, open as open_descriptor
from pathlib import Path
from stat import S_ISDIR, S_ISREG


def relative_storage_path(path, base=None):
    target = Path(path).absolute().parts
    origin = Path(base if base is not None else Path.cwd()).absolute().parts
    shared = 0
    for left, right in zip(target, origin):
        if left != right:
            break
        shared += 1
    return str(Path(*(['..'] * (len(origin) - shared) + list(target[shared:]))))


def permission_refusal(path, base=None):
    return ValueError('insecure_permissions: ' + relative_storage_path(path, base))


def require_private(metadata, path, directory=False, base=None):
    expected = S_ISDIR if directory else S_ISREG
    if not expected(metadata.st_mode) or metadata.st_uid != geteuid() or metadata.st_mode & 0o077:
        raise permission_refusal(path, base)


def private_directory(path, base=None):
    path = Path(path)
    try:
        if not path.exists() and not path.is_symlink():
            if not path.parent.exists():
                private_directory(path.parent, base)
            path.mkdir(mode=0o700, exist_ok=True)
        require_private(path.lstat(), path, directory=True, base=base)
    except PermissionError:
        raise permission_refusal(path, base) from None


def private_descriptor(path, flags, base=None):
    try:
        descriptor = open_descriptor(path, flags | O_NOFOLLOW | O_NONBLOCK, 0o600)
    except PermissionError:
        raise permission_refusal(path, base) from None
    try:
        require_private(fstat(descriptor), path, base=base)
    except Exception:
        close(descriptor)
        raise
    return descriptor


def private_open(path, mode='a', encoding='utf-8', base=None):
    if mode not in ('r', 'a', 'x', 'ab', 'a+b', 'r+b'):
        raise ValueError('unsupported_storage_mode')
    path = Path(path)
    private_directory(path.parent, base)
    return open_stream(path, mode, encoding=None if 'b' in mode else encoding,
                       newline=None if 'b' in mode else '', opener=partial(private_descriptor, base=base))


def tighten_owned_path(path, directory=False, base=None):
    path = Path(path)
    descriptor = None
    try:
        metadata = path.lstat()
        expected = S_ISDIR if directory else S_ISREG
        if metadata.st_uid != geteuid() or not expected(metadata.st_mode):
            raise permission_refusal(path, base)
        flags = O_RDONLY | O_NOFOLLOW | O_NONBLOCK | (O_DIRECTORY if directory else 0)
        descriptor = open_descriptor(path, flags)
        metadata = fstat(descriptor)
        if metadata.st_uid != geteuid() or not expected(metadata.st_mode):
            raise permission_refusal(path, base)
        old_mode, new_mode = metadata.st_mode & 0o7777, 0o700 if directory else 0o600
        if old_mode != new_mode:
            fchmod(descriptor, new_mode)
            getLogger(__name__).warning('storage_permissions path=%s old=%04o new=%04o',
                                        relative_storage_path(path, base), old_mode, new_mode)
        metadata = fstat(descriptor)
        require_private(metadata, path, directory, base)
        if metadata.st_mode & 0o7777 != new_mode:
            raise permission_refusal(path, base)
    except OSError:
        raise permission_refusal(path, base) from None
    finally:
        if descriptor is not None:
            close(descriptor)


def prepare_private_directories(directories, base):
    directories = {Path(path).absolute() for path in directories}
    for directory in tuple(directories):
        parent = directory.parent
        while parent != base and parent.is_relative_to(base):
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda path: len(path.parts)):
        if directory.exists() or directory.is_symlink():
            tighten_owned_path(directory, directory=True, base=base)
        else:
            private_directory(directory, base)


def prepare_private_storage(config):
    base = Path(config.get('_config_directory', Path(config['record_path']).absolute().parent))
    journal = Path(config['record_path'])
    files = [journal, Path(str(journal) + '.lock'), Path(str(journal) + '.deliveries.jsonl'),
             Path(str(journal) + '.deliveries.jsonl.torn')]
    directories = {path.absolute().parent for path in files}
    if 'delegate_key_file' in config:
        directories.add(Path(config['delegate_key_file']).absolute().parent)
    prepare_private_directories(directories, base)
    for path in files:
        if path.exists() or path.is_symlink():
            tighten_owned_path(path, base=base)
