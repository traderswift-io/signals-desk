import asyncio
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from redis.exceptions import ConnectionError

from . import desk_delivery_store, desk_record
from .desk_deliveries import disable_store, persist_delivery, post_delivery, queue_delivery
from .desk_executor import process_event, submit
from .desk_journal_schema import EVENT_FIELDS
from .desk_record import read_records
from .desk_scenarios_bracket import envelope, fixture, main as bracket_scenarios
from .desk_scenarios_budget_identity import check_budget_releases
from .desk_scenarios_delivery_recovery import check_recovery
from .desk_scenarios_fakes import record_anchor_fixture
from .desk_scenarios_redis import stream_fields
from .desk_signal_consumer import consume
from .desk_watcher import arm, disarm, websocket_reconnect


async def diagnostic_histories():
    for fault in ('stale', 'ticker_down'):
        async with fixture() as (runtime, fake):
            value = envelope(fault)
            record_anchor_fixture(runtime['config'], value)
            fake[fault] = 1
            await submit(runtime, value)
    async with fixture() as (runtime, fake):
        value = envelope('refusal-source')
        record_anchor_fixture(runtime['config'], value)
        fake['candle_close'] = '98999'
        assert await submit(runtime, value) == {'refused': 'signal_invalidated'}
        fake.pop('candle_close')
        runtime['tickets'].clear()
        client = SimpleNamespace(xread=AsyncMock(return_value=[('signals', [('1-0', {})])]))
        await consume(runtime, client, maximum_entries=1)
        client.xread = AsyncMock(side_effect=ConnectionError('offline'))
        try:
            await consume(runtime, client, maximum_entries=1)
        except ConnectionError:
            pass
        value = envelope('stream-source')
        record_anchor_fixture(runtime['config'], value)
        client.xread = AsyncMock(return_value=[('signals', [('2-0', stream_fields(value))])])
        await consume(runtime, client, maximum_entries=1)
        await submit(runtime, value)
        ticket = runtime['tickets']['stream-source']
        await process_event(runtime, ticket, {'channel': 'watcher_restarted'})
        runtime['config']['resident'] = True
        ticket['orders'] = []
        ticket['position'] = 0
        async def ready(runtime, ticket):
            ticket['alive'] = True
            ticket['ready'].set()
            await asyncio.Event().wait()

        with patch('desk.desk_watcher.websocket_reconnect', ready):
            await arm(runtime, ticket)
            await disarm(ticket)
        ticket['stopping'] = False

        async def messages():
            yield SimpleNamespace(type=None)

        async def close():
            ticket['stopping'] = True

        async def connect(runtime, ticket):
            ticket['websocket'] = AsyncMock()
            ticket['websocket'].__aiter__.side_effect = messages
            ticket['websocket'].close = close

        with patch('desk.desk_watcher.connect_watcher', connect):
            await websocket_reconnect(runtime, ticket)
        await disable_store(runtime, OSError('unavailable'))
        desk_delivery_store.prepare_delivery_store(runtime['config'])
        runtime['deliveries'] = {'enabled': True, 'pending': {}, 'abandoned': set(), 'writes': asyncio.Lock(),
                                 'persistence': set(), 'not_before': 0, 'headers': {}, 'url': 'delivery'}
        await queue_delivery(runtime, 'delivery-source', '3-0')
        response = SimpleNamespace(status=200, headers={}, json=AsyncMock(return_value={'recorded': True}))
        context = AsyncMock()
        context.__aenter__.return_value = response
        with patch.object(runtime['session'], 'post', return_value=context):
            await post_delivery(runtime, {'signal_id': 'delivery-source', 'event_id': '3-0'})
            response.status = 429
            await post_delivery(runtime, {'signal_id': 'delivery-source', 'event_id': '4-0', 'attempts': 19})
        await persist_delivery(runtime, 'delivery_error', {'reason': 'RuntimeError'})


def field_shape(value):
    if type(value) is dict:
        return tuple(sorted((name, field_shape(item)) for name, item in value.items()))
    if type(value) is list:
        return tuple(field_shape(item) for item in value)
    return type(value).__name__


async def collect_writer_histories():
    histories = {}
    original = desk_record.append_record
    delivery_original = desk_delivery_store.write_row

    def remember(path, event):
        try:
            records = read_records(path)
        except (ValueError, TypeError):
            return
        if not records or records[-1]['event'] != event:
            return
        snapshot = Path(path).read_text()
        row = records[-1]
        shape = field_shape(row)
        samples = histories.setdefault(event, {})
        samples.setdefault(shape, snapshot)

    def record(path, event, **values):
        original(path, event, **values)
        remember(path, event)

    def delivery_record(stream, event, values):
        delivery_original(stream, event, values)
        stream.flush()
        position = stream.tell()
        stream.seek(0)
        snapshot = stream.read().decode()
        stream.seek(position)
        rows = [json.loads(line) for line in snapshot.splitlines()]
        shape = field_shape(rows[-1])
        histories.setdefault(event, {}).setdefault(shape, snapshot)

    with ExitStack() as stack:
        for module in list(sys.modules.values()):
            if not getattr(module, '__name__', '').startswith('desk.'):
                continue
            for name, item in list(vars(module).items()):
                if item is original:
                    stack.enter_context(patch.object(module, name, record))
        stack.enter_context(patch.object(desk_delivery_store, 'write_row', delivery_record))
        await bracket_scenarios()
        await check_budget_releases()
        await check_recovery()
        await diagnostic_histories()
    return {event: list(samples.values()) for event, samples in histories.items() if event in EVENT_FIELDS}
