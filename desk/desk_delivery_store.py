import ctypes
import fcntl
import json
from pathlib import Path

from . import desk_record


def sync_directory(path):
    library = ctypes.CDLL(None)
    library.open.argtypes = (ctypes.c_char_p, ctypes.c_int)
    descriptor = library.open(str(path).encode(), 0)
    if descriptor < 0:
        raise OSError('delivery_directory_open_failed')
    try:
        if library.fsync(descriptor) != 0:
            raise OSError('delivery_directory_sync_failed')
    finally:
        library.close(descriptor)


def write_row(stream, event, values):
    row = {'event': event, 'timestamp': desk_record.now_ms(), **values}
    stream.write((json.dumps(row, default=str, sort_keys=True, separators=(',', ':')) + '\n').encode())
    desk_record.sync_file(stream)


def append_delivery_record(path, event, **values):
    with Path(path).open('r+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0, 2)
        write_row(stream, event, values)


def preserve_tail(path, tail):
    target = Path(str(path) + '.torn')
    target.touch(mode=0o600, exist_ok=True)
    target.chmod(0o600)
    with target.open('ab') as copy:
        fcntl.flock(copy, fcntl.LOCK_EX)
        copy.write(tail)
        desk_record.sync_file(copy)
        sync_directory(target.parent)


def prepare_delivery_store(config):
    path = Path(desk_record.delivery_path(config))
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        lines = stream.readlines()
        rows, boundary, truncated = [], 0, False
        for index, line in enumerate(lines):
            final = index == len(lines) - 1
            try:
                if not line.endswith(b'\n'):
                    raise ValueError('delivery_line_incomplete')
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                if not final:
                    raise
                truncated = True
                break
            if not isinstance(row, dict) or not isinstance(row.get('event'), str):
                raise ValueError('delivery_record_invalid')
            rows.append(row)
            boundary += len(line)
        state = desk_record.delivery_state(config, rows)
        if truncated:
            tail = b''.join(lines[index:])
            preserve_tail(path, tail)
            stream.truncate(boundary)
            desk_record.sync_file(stream)
            stream.seek(0, 2)
            write_row(stream, 'delivery_tail_truncated', {'removed_bytes': len(tail)})
        desk_record.sync_file(stream)
        sync_directory(path.parent)
    return state
