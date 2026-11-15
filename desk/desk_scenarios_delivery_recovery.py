import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from . import desk_signal_consumer as consumer
from .desk_deliveries import prepare_deliveries, queue_delivery
from .desk_delivery_store import sync_directory
from .desk_record import append_record, delivery_path, pending_deliveries, read_records, reader_cursor, sync_file
from .desk_scenarios_fakes import configuration, signal_envelope
from .desk_scenarios_redis import stream_fields


def restart_config(directory):
    config = configuration(directory, 'https://example.invalid')
    token = directory / 'subscriber.token'
    token.write_text('fake-subscriber-token\n')
    config.update(signals_url='https://example.invalid', subscriber_token_file=str(token),
                  signals_access_client_id='fake-signals-id', signals_access_client_secret='fake-signals-secret')
    append_record(config['record_path'], 'cursor', stream=config['redis']['stream'], stream_id='1-0')
    return config


async def consumer_restart(config, inspect):
    runtime = {'config': config, 'tickets': {}}
    client = SimpleNamespace(aclose=AsyncMock(), xread=AsyncMock(return_value=[
        ('signals', [('2-0', stream_fields(signal_envelope('after-restart')))])]))
    server = AsyncMock()
    server.close = Mock()
    recovered = AsyncMock()
    original_consume = consumer.consume

    async def start_server(*args, **kwargs):
        Path(kwargs['path']).touch()
        return server

    async def intake(current, connection):
        recovered.assert_awaited_once_with(current)
        await inspect(current)
        await original_consume(current, connection, 1)

    with (patch.object(consumer, 'load_config', return_value=config),
          patch.object(consumer.redis, 'Redis', return_value=client),
          patch.object(consumer, 'create_runtime', return_value=runtime),
          patch.object(consumer, 'event_worker', AsyncMock()),
          patch.object(consumer, 'recover', recovered),
          patch.object(consumer, 'shutdown', AsyncMock()),
          patch.object(consumer, 'submit', AsyncMock(return_value={'state': 'filled'})) as submitted,
          patch.object(consumer, 'schedule_delivery'),
          patch.object(consumer.asyncio, 'start_unix_server', side_effect=start_server),
          patch.object(consumer, 'consume', side_effect=intake)):
        await consumer.main('unused-config')
    submitted.assert_awaited_once()
    client.aclose.assert_awaited_once()
    assert reader_cursor(config) == '2-0'
    return runtime


async def check_torn_tail():
    tails = (b'{"event":"delivery_reported","event_id":', b'broken-final-record\n',
             b'{"event":"delivery_reported","event_id":"1-0"}', b'\xff\n')
    for tail in tails:
        with tempfile.TemporaryDirectory() as directory:
            config = restart_config(Path(directory))
            path = Path(delivery_path(config))
            append_record(path, 'delivery_pending', signal_id='queued', event_id='1-0',
                          status='queued', attempts=0, next_attempt=0)
            prefix, inode = path.read_bytes(), path.stat().st_ino
            with path.open('ab') as stream:
                stream.write(tail)
            copy, events = Path(str(path) + '.torn'), []
            previous = b'earlier-torn-tail' if tail == tails[1] else b''
            if previous:
                copy.write_bytes(previous)
                copy.chmod(0o644)

            def synced_file(stream):
                sync_file(stream)
                events.append(('file', str(stream.name)))

            def synced_directory(parent):
                if len(events) == 1:
                    assert path.read_bytes() == prefix + tail
                    assert copy.read_bytes() == previous + tail
                    assert copy.stat().st_mode & 0o777 == 0o600
                sync_directory(parent)
                events.append(('directory', str(parent)))

            async def inspect(runtime):
                assert runtime['deliveries']['enabled']
                assert list(runtime['deliveries']['pending']) == ['1-0']
                assert runtime['deliveries']['pending']['1-0']['signal_id'] == 'queued'
                assert reader_cursor(config) == '1-0'
                assert path.stat().st_ino == inode
                assert path.read_bytes().startswith(prefix) and path.read_bytes().endswith(b'\n')
                assert [row['event'] for row in read_records(path)] == ['delivery_pending', 'delivery_tail_truncated']
                assert read_records(path)[-1]['removed_bytes'] == len(tail)
                assert copy.read_bytes() == previous + tail
                assert copy.stat().st_mode & 0o777 == 0o600
                assert events == [('file', str(copy)), ('directory', str(path.parent)),
                                  ('file', str(path)), ('file', str(path)), ('file', str(path)),
                                  ('directory', str(path.parent))]

            with (patch('desk.desk_record.sync_file', side_effect=synced_file),
                  patch('desk.desk_delivery_store.sync_directory', side_effect=synced_directory)):
                await consumer_restart(config, inspect)
            assert [row['event_id'] for row in pending_deliveries(config)] == ['1-0', '2-0']
            await prepare_deliveries({'config': config})
            assert len([row for row in read_records(path) if row['event'] == 'delivery_tail_truncated']) == 1
            assert copy.read_bytes() == previous + tail


async def check_corrupt_store():
    for corrupt in (b'broken-interior\n', b'\xff\n', b'[]\n'):
        with tempfile.TemporaryDirectory() as directory:
            config = restart_config(Path(directory))
            path = Path(delivery_path(config))
            append_record(path, 'delivery_pending', signal_id='queued', event_id='1-0', status='queued')
            with path.open('ab') as stream:
                stream.write(corrupt)
            append_record(path, 'delivery_reported', event_id='1-0')
            original = path.read_bytes()

            async def inspect(runtime):
                assert not runtime['deliveries']['enabled'] and not runtime['deliveries']['tasks']
                assert reader_cursor(config) == '1-0' and path.read_bytes() == original
                diagnostic = read_records(config['record_path'])[-1]
                assert diagnostic['event'] == 'deliveries_disabled'
                assert diagnostic['reason'] == 'delivery_store_corrupt'
                assert diagnostic['exception'] in ('JSONDecodeError', 'UnicodeDecodeError', 'ValueError')
                assert set(diagnostic) == {'event', 'timestamp', 'reason', 'exception'}

            await consumer_restart(config, inspect)
            assert path.read_bytes() == original


async def check_unreadable_store():
    with tempfile.TemporaryDirectory() as directory:
        config = restart_config(Path(directory))
        path = Path(delivery_path(config))
        append_record(path, 'delivery_pending', signal_id='queued', event_id='1-0', status='queued')
        original, opened = path.read_bytes(), Path.open

        def unreadable(target, *args, **kwargs):
            if target == path:
                raise PermissionError('fake-subscriber-token')
            return opened(target, *args, **kwargs)

        async def inspect(runtime):
            assert not runtime['deliveries']['enabled'] and reader_cursor(config) == '1-0'
            diagnostic = read_records(config['record_path'])[-1]
            assert diagnostic['reason'] == 'delivery_store_unavailable'
            assert diagnostic['exception'] == 'PermissionError'
            assert 'fake-subscriber-token' not in Path(config['record_path']).read_text()

        with patch.object(Path, 'open', new=unreadable):
            await consumer_restart(config, inspect)
        assert path.read_bytes() == original


async def check_durable_creation():
    with tempfile.TemporaryDirectory() as directory:
        config = restart_config(Path(directory))
        path, events = Path(delivery_path(config)), []
        assert not path.exists()

        def synced_file(stream):
            sync_file(stream)
            events.append(('file', str(stream.name)))

        def synced_directory(parent):
            assert path.exists() and events[-1] == ('file', str(path))
            sync_directory(parent)
            events.append(('directory', str(parent)))

        async def inspect(runtime):
            assert runtime['deliveries']['enabled'] and reader_cursor(config) == '1-0'
            assert events == [('file', str(path)), ('directory', str(path.parent))]

        with (patch('desk.desk_record.sync_file', side_effect=synced_file),
              patch('desk.desk_delivery_store.sync_directory', side_effect=synced_directory)):
            await consumer_restart(config, inspect)
        assert events == [('file', str(path)), ('directory', str(path.parent)),
                          ('file', config['record_path']), ('file', str(path)), ('file', config['record_path'])]
        assert pending_deliveries(config)[0]['event_id'] == '2-0'
        runtime = {'config': config}
        await prepare_deliveries(runtime)
        path.unlink()
        client = SimpleNamespace(xread=AsyncMock(return_value=[
            ('signals', [('3-0', stream_fields(signal_envelope('missing-store')))])]))
        with (patch.object(consumer, 'submit', AsyncMock(return_value={'state': 'filled'})),
              patch.object(asyncio.get_running_loop(), 'call_exception_handler') as diagnostic):
            try:
                await consumer.consume(runtime, client, 1)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError('Missing active store allowed cursor advancement')
        assert reader_cursor(config) == '2-0' and not path.exists()
        diagnostic.assert_called_once_with({'message': 'delivery_persistence_failed'})


async def check_creation_failure():
    for failure in ('create', 'file_sync', 'directory_sync'):
        with tempfile.TemporaryDirectory() as directory:
            config = restart_config(Path(directory))
            path, opened = Path(delivery_path(config)), Path.open

            def open_store(target, *args, **kwargs):
                if target == path:
                    raise PermissionError('fake-signals-secret')
                return opened(target, *args, **kwargs)

            def file_sync(stream):
                if str(stream.name) == str(path):
                    raise OSError('fake-signals-secret')
                sync_file(stream)

            async def inspect(runtime):
                assert not runtime['deliveries']['enabled'] and reader_cursor(config) == '1-0'
                await queue_delivery(runtime, 'unpersisted', '9-0')
                assert reader_cursor(config) == '1-0' and not runtime['deliveries']['pending']
                diagnostic = read_records(config['record_path'])[-1]
                assert diagnostic['reason'] == 'delivery_store_unavailable'
                assert diagnostic['exception'] in ('PermissionError', 'OSError')
                assert 'fake-signals-secret' not in Path(config['record_path']).read_text()

            injection = (patch.object(Path, 'open', new=open_store) if failure == 'create' else
                         patch('desk.desk_record.sync_file', side_effect=file_sync) if failure == 'file_sync' else
                         patch('desk.desk_delivery_store.sync_directory', side_effect=OSError('fake-signals-secret')))
            with injection:
                await consumer_restart(config, inspect)
            assert not path.exists() if failure == 'create' else path.read_bytes() == b''
            restarted = {'config': config}
            with patch('desk.desk_delivery_store.sync_directory', wraps=sync_directory) as directory_sync:
                await prepare_deliveries(restarted)
            assert restarted['deliveries']['enabled'] and directory_sync.call_count == 1
            await queue_delivery(restarted, 'after-repair', '3-0')
            assert pending_deliveries(config)[0]['event_id'] == '3-0'


async def check_tail_copy_failure():
    for failure in ('open', 'file_sync', 'directory_sync'):
        with tempfile.TemporaryDirectory() as directory:
            config = restart_config(Path(directory))
            path = Path(delivery_path(config))
            append_record(path, 'delivery_pending', signal_id='queued', event_id='1-0', status='queued')
            tail = b'{"event":"delivery_reported"'
            with path.open('ab') as stream:
                stream.write(tail)
            original, copy, opened = path.read_bytes(), Path(str(path) + '.torn'), Path.open

            def open_copy(target, *args, **kwargs):
                if target == copy:
                    raise PermissionError('fake-signals-secret')
                return opened(target, *args, **kwargs)

            def file_sync(stream):
                if str(stream.name) == str(copy):
                    raise OSError('fake-signals-secret')
                sync_file(stream)

            async def inspect(runtime):
                assert not runtime['deliveries']['enabled']
                assert reader_cursor(config) == '1-0' and path.read_bytes() == original
                diagnostic = read_records(config['record_path'])[-1]
                assert diagnostic['reason'] == 'delivery_store_unavailable'
                assert diagnostic['exception'] in ('PermissionError', 'OSError')
                assert 'fake-signals-secret' not in Path(config['record_path']).read_text()

            injection = (patch.object(Path, 'open', new=open_copy) if failure == 'open' else
                         patch('desk.desk_record.sync_file', side_effect=file_sync) if failure == 'file_sync' else
                         patch('desk.desk_delivery_store.sync_directory', side_effect=OSError('fake-signals-secret')))
            with injection:
                await consumer_restart(config, inspect)
            assert path.read_bytes() == original
            previous = copy.read_bytes() if copy.exists() else b''
            runtime = {'config': config}
            await prepare_deliveries(runtime)
            assert runtime['deliveries']['enabled'] and list(runtime['deliveries']['pending']) == ['1-0']
            assert copy.read_bytes() == previous + tail
            assert read_records(path)[-1]['removed_bytes'] == len(tail)


async def check_preparation_exception():
    for target in ('desk.desk_deliveries.delivery_state', 'desk.desk_record.delivery_state',
                   'desk.desk_deliveries.delivery_headers', 'desk.desk_deliveries.delivery_route',
                   'desk.desk_deliveries.asyncio.Lock'):
        with tempfile.TemporaryDirectory() as directory:
            config = restart_config(Path(directory))
            path = Path(delivery_path(config))
            append_record(path, 'delivery_pending', signal_id='queued', event_id='1-0', status='queued')
            original = path.read_bytes()

            async def inspect(runtime):
                assert not runtime['deliveries']['enabled']
                assert reader_cursor(config) == '1-0' and path.read_bytes() == original
                diagnostic = read_records(config['record_path'])[-1]
                assert diagnostic['event'] == 'deliveries_disabled'
                assert diagnostic['reason'] == diagnostic['exception'] == 'RuntimeError'
                assert set(diagnostic) == {'event', 'timestamp', 'reason', 'exception'}
                assert 'fake-signals-secret' not in Path(config['record_path']).read_text()

            with patch(target, side_effect=RuntimeError('fake-signals-secret')):
                await consumer_restart(config, inspect)
            assert path.read_bytes() == original


async def check_recovery():
    for name, check in [('25: torn final records repaired before consumer recovery with queued identity intact', check_torn_tail),
                        ('26: corrupt interior records disable reporting without preventing recovery or intake', check_corrupt_store),
                        ('27: unreadable store disables reporting with sanitized recovery diagnostics', check_unreadable_store),
                        ('28: first creation syncs file and directory before queue and cursor', check_durable_creation),
                        ('29: creation and sync failures disable reporting before cursor advancement', check_creation_failure),
                        ('30: torn-copy failures preserve original bytes and allow recovery before repair', check_tail_copy_failure),
                        ('31: arbitrary delivery_state and preparation exceptions still reach recovery', check_preparation_exception)]:
        await check()
        print('PASS deliveries ' + name, flush=True)
