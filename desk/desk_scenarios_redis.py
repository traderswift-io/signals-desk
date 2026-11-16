import asyncio
import json
import socket
import tempfile
from pathlib import Path

import aiohttp
import redis.asyncio as redis
from redis.exceptions import ConnectionError, NoPermissionError

from .desk_executor import create_runtime, event_worker, recover, shutdown
from .desk_record import read_records, reader_cursor
from .desk_scenarios_fakes import signal_envelope, start_fake, stop_fake
from .desk_signal_consumer import consume


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def stream_fields(envelope):
    payload = envelope['payload']
    return {'event_id': payload['signal_id'], 'ts': str(payload['published_at']), 'source': 'scenario',
            'correlation_id': payload['signal_id'], 'signal_id': payload['signal_id'], 'kind': payload['kind'],
            'envelope': json.dumps(envelope), 'segment_hash': '', 'receipt': ''}


async def wait_for_redis(client):
    async with asyncio.timeout(5):
        while True:
            try:
                await client.ping()
                return
            except ConnectionError:
                await asyncio.sleep(0.01)


async def check_consumer():
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        port = free_port()
        process = await asyncio.create_subprocess_exec('redis-server', '--bind', '127.0.0.1', '--port', str(port),
            '--dir', str(directory), '--save', '', '--appendonly', 'no', '--protected-mode', 'yes',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        admin = redis.Redis(host='127.0.0.1', port=port, decode_responses=True)
        reader = None
        try:
            await wait_for_redis(admin)
            await admin.execute_command('ACL', 'SETUSER', 'reader', 'reset', 'on', '>fake-password', '~signals',
                                        '+xread', '+xrange', '+xinfo', '+ping')
            reader = redis.Redis(host='127.0.0.1', port=port, username='reader', password='fake-password', decode_responses=True)
            await reader.ping()
            try:
                await reader.xadd('signals', {'forbidden': 'write'})
            except NoPermissionError:
                pass
            else:
                raise AssertionError('Reader can write')
            first = signal_envelope('redis-first')
            second = signal_envelope('redis-second', 'ETH')
            await admin.xadd('signals', stream_fields(first))
            last_id = await admin.xadd('signals', stream_fields(second))
            async with aiohttp.ClientSession() as session:
                runtime, fake, runner = await start_fake(directory, session)
                runtime['config']['redis']['port'] = port
                restarted = None
                try:
                    await asyncio.wait_for(consume(runtime, reader, 2), 5)
                    rows = read_records(runtime['config']['record_path'])
                    gates = [row for row in rows if row['event'] == 'gate']
                    assert len(gates) == 2 and [row['reason'] for row in gates] == [None, 'asset_not_allowed']
                    assert len(fake['intents']) == 1
                    assert reader_cursor(runtime['config']) == last_id
                    await shutdown(runtime)
                    restarted = create_runtime(runtime['config'], session)
                    restarted['worker'] = asyncio.create_task(event_worker(restarted))
                    await recover(restarted)
                    assert fake['position'] != 0 and any(order.get('isTrigger') for order in fake['orders'])
                    entries_before = len([intent for intent in fake['intents'] if intent['kind'] == 'bracket'])
                    waiting = asyncio.create_task(consume(restarted, reader, 1))
                    await asyncio.sleep(0.03)
                    assert not waiting.done()
                    duplicate_id = await admin.xadd('signals', stream_fields(first))
                    await asyncio.wait_for(waiting, 5)
                    assert reader_cursor(restarted['config']) == duplicate_id
                    assert len([intent for intent in fake['intents'] if intent['kind'] == 'bracket']) == entries_before == 1
                    consumed = [row for row in read_records(runtime['config']['record_path']) if row['event'] == 'consumed']
                    assert len(consumed) == 3 and consumed[-1]['result']['refused'] == 'already_acted'
                finally:
                    if restarted:
                        await shutdown(restarted)
                    await stop_fake(runtime, runner)
        finally:
            if reader:
                await reader.aclose()
            await admin.aclose()
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.communicate(), 5)
