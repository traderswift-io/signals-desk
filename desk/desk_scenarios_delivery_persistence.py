import asyncio
import threading
from pathlib import Path
from unittest.mock import patch

import aiohttp

from .desk_deliveries import delivery_route, finish_deliveries, prepare_deliveries, queue_delivery, schedule_delivery
from .desk_record import append_record, delivery_path, now_ms, pending_deliveries, read_records, signed_today, sync_file
from .desk_scenarios_deliveries import fixture, posted, rows, settle


async def check_isolation():
    async with fixture() as (runtime, state, _):
        config = runtime['config']
        append_record(config['record_path'], 'delivery_pending', signal_id='ignored', event_id='0-0')
        runtime.pop('deliveries')
        await prepare_deliveries(runtime)
        assert not runtime['deliveries']['pending']
        before = read_records(config['record_path'])
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        beats = []

        def slow_sync(stream):
            if stream.name == delivery_path(config):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise RuntimeError('scenario_delivery_lock_blocked_trading')
            sync_file(stream)

        async def heartbeat():
            await asyncio.sleep(0.02)
            beats.append(loop.time())

        async def budget():
            assert signed_today(config, 'BTC') == 0
            assert read_records(config['record_path']) == before

        with patch('desk.desk_record.sync_file', side_effect=slow_sync):
            queued = asyncio.create_task(queue_delivery(runtime, 'isolated', '1-0'))
            try:
                await asyncio.wait_for(started.wait(), 1)
                begin = loop.time()
                await asyncio.wait_for(asyncio.gather(budget(), heartbeat()), 0.5)
                assert beats and beats[0] - begin < 0.5 and not queued.done()
                assert not release.is_set()
            finally:
                release.set()
                await queued
        assert read_records(config['record_path']) == before
        assert [row['event_id'] for row in pending_deliveries(config)] == ['1-0']
        runtime.pop('deliveries')
        await prepare_deliveries(runtime)
        assert list(runtime['deliveries']['pending']) == ['1-0']
        schedule_delivery(runtime)
        await settle(runtime)
        assert posted(state) == ['1-0'] and read_records(config['record_path']) == before


async def check_shutdown_budget():
    async with fixture() as (runtime, state, _):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def held_record(path, event, **values):
            if event == 'delivery_reported':
                loop.call_soon_threadsafe(started.set)
                release.wait()
            append_record(path, event, **values)

        with patch('desk.desk_deliveries.append_record', side_effect=held_record):
            try:
                await queue_delivery(runtime, 'held', '1-0')
                schedule_delivery(runtime)
                await asyncio.wait_for(started.wait(), 1)
                begin = loop.time()
                await asyncio.wait_for(finish_deliveries(runtime), 5.5)
                assert 4.9 <= loop.time() - begin < 5.5
                assert not release.is_set() and not runtime['deliveries']['tasks']
                assert runtime['deliveries']['persistence']
                assert pending_deliveries(runtime['config'])[0]['status'] == 'queued'
                assert not rows(runtime, 'delivery_reported') and posted(state) == ['1-0']
                schedule_delivery(runtime, '2-0')
                assert not runtime['deliveries']['tasks']
            finally:
                release.set()
                await asyncio.gather(*tuple(runtime['deliveries']['persistence']), return_exceptions=True)


async def check_decoding(raw, content_type, exception):
    async with fixture() as (runtime, state, _):
        state['responses'] = {'1-0': (raw, content_type)}
        failures = []
        decode = aiohttp.ClientResponse.json

        async def real_decode(response, **kwargs):
            try:
                return await decode(response, **kwargs)
            except Exception as error:
                failures.append(type(error))
                raise

        await queue_delivery(runtime, 'broken', '1-0')
        await queue_delivery(runtime, 'later', '2-0')
        timestamp = now_ms()
        with patch.object(aiohttp.ClientResponse, 'json', new=real_decode):
            for attempt in range(1, 21):
                with patch('desk.desk_deliveries.now_ms', return_value=timestamp):
                    schedule_delivery(runtime)
                    await settle(runtime)
                assert failures == [exception] * attempt, (attempt, failures)
                event = 'delivery_abandoned' if attempt == 20 else 'delivery_pending'
                row = rows(runtime, event)[-1]
                delay = min(5000 * 2 ** (attempt - 1), 900000)
                assert row['event_id'] == '1-0' and row['attempts'] == attempt
                assert row['next_attempt'] == timestamp + delay
                assert posted(state).count('1-0') == attempt and posted(state).count('2-0') == 1
                assert [row['event_id'] for row in rows(runtime, 'delivery_reported')] == ['2-0']
                assert not rows(runtime, 'delivery_error')
                runtime.pop('deliveries')
                await prepare_deliveries(runtime)
                with patch('desk.desk_deliveries.now_ms', return_value=timestamp + delay - 1):
                    schedule_delivery(runtime)
                    await settle(runtime)
                assert posted(state).count('1-0') == attempt
                timestamp += delay
        assert not pending_deliveries(runtime['config'])
        assert runtime['deliveries']['abandoned'] == {'1-0'}
        await queue_delivery(runtime, 'broken', '1-0')
        await queue_delivery(runtime, 'after', '3-0')
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp):
            schedule_delivery(runtime)
            await settle(runtime)
        assert posted(state)[-1] == '3-0' and posted(state).count('1-0') == 20
        text = Path(delivery_path(runtime['config'])).read_text()
        assert 'base64_codec' not in text and not rows(runtime, 'delivery_error')


async def check_deep_json():
    await check_decoding(b'[' * 100000 + b'0' + b']' * 100000, 'application/json', RecursionError)


async def check_bad_charset():
    await check_decoding(b'{}', 'application/json; charset=base64_codec', LookupError)


async def check_timeout_ceiling():
    async with fixture() as (runtime, state, _):
        with patch.object(runtime['session'], 'post', wraps=runtime['session'].post) as post:
            for index, value in enumerate((30, 30.001, 3600, 1e300)):
                runtime['config']['request_timeout_seconds'] = value
                await queue_delivery(runtime, 'timeout', f'{index}-0')
                schedule_delivery(runtime)
                await settle(runtime)
                assert post.call_args.kwargs['timeout'].total == (30 if value == 30 else 10)
        assert len(state['posts']) == 4


async def check_empty_delimiters():
    async with fixture() as (runtime, state, _):
        original = runtime['config']['signals_url']
        for suffix in ('?', '#'):
            runtime.pop('deliveries')
            runtime['config']['signals_url'] = 'https://example.invalid' + suffix
            await prepare_deliveries(runtime)
            assert not runtime['deliveries']['enabled']
            await queue_delivery(runtime, 'invalid', '1-0')
            schedule_delivery(runtime)
            assert not runtime['deliveries']['tasks'] and not state['posts']
        assert len(rows(runtime, 'deliveries_disabled')) == 2
        runtime.pop('deliveries')
        runtime['config']['signals_url'] = original + '/'
        await prepare_deliveries(runtime)
        await queue_delivery(runtime, 'valid', '1-0')
        schedule_delivery(runtime)
        await settle(runtime)
        assert state['posts'][0]['path'] == '/v1/deliveries'
        assert delivery_route({'signals_url': 'https://example.invalid/base/'}) == 'https://example.invalid/base/v1/deliveries'


async def check_persistence():
    for name, check in [('19: delivery lock never holds trading journal reads or heartbeat', check_isolation),
                        ('20: shutdown stops waiting after five seconds of stalled persistence', check_shutdown_budget),
                        ('21: real deep JSON decoding backs off, progresses and abandons', check_deep_json),
                        ('22: real bad charset decoding backs off, progresses and abandons', check_bad_charset),
                        ('23: oversized finite timeouts fall back to ten seconds', check_timeout_ceiling),
                        ('24: empty URL delimiters refused and delivery path built structurally', check_empty_delimiters)]:
        await check()
        print('PASS deliveries ' + name, flush=True)
