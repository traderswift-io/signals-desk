import argparse
import asyncio
import json
from pathlib import Path

import aiohttp
import redis.asyncio as redis
from redis.exceptions import ConnectionError, TimeoutError

from .desk_deliveries import finish_deliveries, prepare_deliveries, schedule_delivery
from .desk_executor import cancel, control_connection, create_runtime, event_worker, recover, shutdown, submit
from .desk_io import load_config
from .desk_record import append_record, reader_cursor, writer_lock


async def consume(runtime, client, maximum_entries=None):
    prepare_deliveries(runtime)
    config = runtime['config']
    stream, cursor = config['redis']['stream'], reader_cursor(config)
    count = 0
    while maximum_entries is None or count < maximum_entries:
        try:
            results = await client.xread({stream: cursor}, count=1, block=0)
        except (ConnectionError, TimeoutError):
            append_record(config['record_path'], 'stream_disconnected', stream_id=cursor)
            for signal_id in list(runtime['tickets']):
                await cancel(runtime, signal_id)
            raise
        for _, entries in results:
            for entry_id, fields in entries:
                try:
                    required = ('event_id', 'ts', 'source', 'correlation_id', 'signal_id', 'kind', 'envelope', 'segment_hash', 'receipt')
                    if any(name not in fields for name in required):
                        raise ValueError('incomplete_stream_entry')
                    timestamp = int(fields['ts'])
                    if timestamp < 0:
                        raise ValueError('invalid_stream_event')
                    envelope = json.loads(fields['envelope'])
                    if not isinstance(envelope, dict):
                        raise ValueError('invalid_stream_envelope')
                    if 'payload' not in envelope:
                        envelope = {'version': 1, 'payload': envelope, 'receipt': json.loads(fields['receipt']) if fields['receipt'] else None}
                    payload = envelope['payload']
                    if fields['correlation_id'] != fields['signal_id'] or fields['signal_id'] != payload['signal_id']:
                        raise ValueError('stream_signal_id_mismatch')
                    if fields['kind'] != payload.get('kind', 'signal') or fields['kind'] not in ('signal', 'cancel'):
                        raise ValueError('stream_kind_mismatch')
                    result = await submit(runtime, envelope)
                    append_record(config['record_path'], 'consumed', stream_id=entry_id, result=result)
                    schedule_delivery(runtime, fields['signal_id'], entry_id, timestamp)
                except (ValueError, KeyError, TypeError) as error:
                    append_record(config['record_path'], 'stream_refusal', stream_id=entry_id, reason=str(error))
                    schedule_delivery(runtime)
                append_record(config['record_path'], 'cursor', stream=stream, stream_id=entry_id)
                cursor = entry_id
                count += 1
    return cursor


async def main(config_path):
    config = load_config(config_path)
    lock = writer_lock(config)
    connection = config['redis']
    client = redis.Redis(host=connection['host'], port=connection['port'], username=connection['username'],
                         password=connection['password'], decode_responses=True)
    socket_path = Path(config['control_socket'])
    server = None
    try:
        socket_path.unlink(missing_ok=True)
        async with aiohttp.ClientSession() as session:
            runtime = create_runtime(config, session)
            prepare_deliveries(runtime)
            runtime['worker'] = asyncio.create_task(event_worker(runtime))
            try:
                await recover(runtime)
                schedule_delivery(runtime)
                server = await asyncio.start_unix_server(lambda reader, writer: control_connection(runtime, reader, writer),
                                                         path=str(socket_path))
                socket_path.chmod(0o600)
                async with server:
                    await consume(runtime, client)
            finally:
                await shutdown(runtime)
                await finish_deliveries(runtime)
    finally:
        await client.aclose()
        if server:
            server.close()
            await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    asyncio.run(main(parser.parse_args().config))
