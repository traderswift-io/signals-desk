import asyncio
import threading
from email.utils import formatdate
from pathlib import Path
from unittest.mock import AsyncMock, patch

from .desk_deliveries import finish_deliveries, prepare_deliveries, queue_delivery, schedule_delivery
from .desk_executor import create_runtime, event_worker, recover, shutdown
from .desk_record import append_record, now_ms, pending_deliveries, read_records
from .desk_scenarios_deliveries import entry, fixture, posted, rows, settle
from .desk_scenarios_fakes import signal_envelope
from .desk_scenarios_redis import stream_fields


async def check_endpoints():
    async with fixture() as (runtime, state, _):
        original = runtime['config']['signals_url']
        for url in ('http://example.invalid', 'http://localhost', 'https:///missing',
                    'https://user:fake@example.invalid', 'https://example.invalid:bad',
                    'https://example.invalid?query=1', 'https://example.invalid#fragment', ''):
            runtime.pop('deliveries')
            runtime['config']['signals_url'] = url
            await prepare_deliveries(runtime)
            assert not runtime['deliveries']['enabled']
        runtime.pop('deliveries')
        runtime.pop('allow_http_deliveries')
        runtime['config']['signals_url'] = original
        await prepare_deliveries(runtime)
        assert not runtime['deliveries']['enabled']
        runtime.pop('deliveries')
        runtime['config']['signals_url'] = 'https://example.invalid'
        await prepare_deliveries(runtime)
        assert runtime['deliveries']['enabled'] and not state['posts']
        assert len(rows(runtime, 'deliveries_disabled')) == 9


async def check_disabled_recovery():
    for contents in (None, '', 'fake-subscriber-token\nsecond', 'fake-subscriber-token\n\n', 'directory'):
        async with fixture() as (runtime, state, trading):
            await entry(runtime, 'open', asset='BTC')
            await settle(runtime)
            await shutdown(runtime)
            token = Path(runtime['config']['subscriber_token_file'])
            token.unlink()
            if contents == 'directory':
                token.mkdir()
            elif contents is not None:
                token.write_text(contents)
            restarted = create_runtime(runtime['config'], runtime['session'])
            restarted['allow_http_deliveries'] = True
            await prepare_deliveries(restarted)
            assert not restarted['deliveries']['enabled']
            restarted['worker'] = asyncio.create_task(event_worker(restarted))
            try:
                await recover(restarted)
                assert trading['position'] == 0
                await entry(restarted, 'next', asset='BTC', entry_id='2-0')
                assert rows(restarted, 'consumed')[-1]['result'] == {'state': 'filled'}
                assert posted(state) == ['1-0']
                disabled = rows(restarted, 'deliveries_disabled')
                assert len(disabled) == 1 and disabled[0]['reason'] in ('FileNotFoundError', 'ValueError', 'IsADirectoryError')
                assert 'fake-subscriber-token' not in Path(restarted['config']['record_path']).read_text()
            finally:
                await finish_deliveries(restarted)
                await shutdown(restarted)
    async with fixture() as (runtime, state, _):
        runtime.pop('deliveries')
        runtime['config'].pop('signals_access_client_secret')
        await entry(runtime, 'missing-credential', asset='BTC')
        assert rows(runtime, 'consumed')[-1]['result'] == {'state': 'filled'}
        assert rows(runtime, 'deliveries_disabled')[-1]['reason'] == 'KeyError' and not state['posts']


async def check_shutdown():
    async with fixture() as (runtime, state, _):
        state['hold'] = True
        runtime['config']['request_timeout_seconds'] = 3600
        await entry(runtime, 'first')
        await asyncio.wait_for(state['arrived'].wait(), 5)
        for index in range(2, 102):
            await queue_delivery(runtime, 'queued', f'{index}-0')
            schedule_delivery(runtime, f'{index}-0')
        assert len(runtime['deliveries']['tasks']) == 1
        await asyncio.wait_for(finish_deliveries(runtime), 1)
        assert not state['release'].is_set() and not runtime['deliveries']['tasks']
        assert len(pending_deliveries(runtime['config'])) == 101
        restarted = {'config': runtime['config'], 'session': runtime['session'], 'allow_http_deliveries': True}
        await prepare_deliveries(restarted)
        assert list(restarted['deliveries']['pending']) == [f'{index}-0' for index in range(1, 102)]


async def check_timeouts():
    async with fixture() as (runtime, state, _):
        with patch.object(runtime['session'], 'post', wraps=runtime['session'].post) as post:
            for index, value in enumerate((0, -1, float('nan'), float('inf'), -float('inf'), 'bad', None, 0.25)):
                runtime['config']['request_timeout_seconds'] = value
                await entry(runtime, f'timeout-{index}', entry_id=f'{index}-0')
                await settle(runtime)
                calls = [call for call in post.call_args_list if call.args[0].endswith('/v1/deliveries')]
                assert calls[-1].kwargs['timeout'].total == (0.25 if value == 0.25 else 10)
                assert calls[-1].kwargs['allow_redirects'] is False
        assert len(state['posts']) == 8


async def check_cursor_order():
    async with fixture() as (runtime, state, _):
        def cursor_record(path, event, **values):
            if event == 'cursor':
                pending = pending_deliveries(runtime['config'])
                assert pending and pending[-1]['event_id'] == values['stream_id']
                assert pending[-1]['status'] == 'queued' and not state['posts']
            append_record(path, event, **values)

        with patch('desk.desk_signal_consumer.append_record', side_effect=cursor_record):
            await entry(runtime, 'first')
        await settle(runtime)
        events = [row['event'] for row in read_records(runtime['config']['record_path'])]
        assert events.index('delivery_pending') < events.index('cursor') < events.index('delivery_reported')
    async with fixture() as (runtime, state, _):
        with patch('desk.desk_signal_consumer.queue_delivery', AsyncMock(side_effect=OSError('fake write failure'))):
            try:
                await entry(runtime, 'unsaved')
            except OSError:
                pass
            else:
                raise AssertionError('Pending write failure did not stop cursor advancement')
        assert not rows(runtime, 'cursor') and not state['posts']


async def check_batch_limit():
    for retry_after in ('30', 'invalid', None, 'date'):
        async with fixture() as (runtime, state, _):
            timestamp = now_ms() // 1000 * 1000
            header = formatdate((timestamp + 30000) / 1000, usegmt=True) if retry_after == 'date' else retry_after
            state.update(status=429, headers={} if header is None else {'Retry-After': header})
            for index in range(12):
                await queue_delivery(runtime, 'older', f'0-{index}')
            with patch('desk.desk_deliveries.now_ms', return_value=timestamp):
                await entry(runtime, 'fresh', timestamp=timestamp + 999999)
                await settle(runtime)
            assert posted(state) == ['0-0']
            pending = pending_deliveries(runtime['config'])
            deadline = timestamp + (30000 if retry_after in ('30', 'date') else 5000)
            assert pending[0]['not_before'] == deadline
            assert pending[-1]['event_id'] == '1-0' and pending[-1]['status'] == 'queued'
            runtime.pop('deliveries')
            await prepare_deliveries(runtime)
            with patch('desk.desk_deliveries.now_ms', return_value=deadline - 1):
                schedule_delivery(runtime)
                await settle(runtime)
            assert posted(state) == ['0-0']


async def check_backoff():
    async with fixture() as (runtime, state, _):
        state['status'] = 503
        timestamp = now_ms()
        for attempt in range(1, 21):
            with patch('desk.desk_deliveries.now_ms', return_value=timestamp):
                if attempt == 1:
                    await entry(runtime, 'first')
                else:
                    schedule_delivery(runtime)
                await settle(runtime)
            event = 'delivery_abandoned' if attempt == 20 else 'delivery_pending'
            row = rows(runtime, event)[-1]
            delay = min(5000 * 2 ** (attempt - 1), 900000)
            assert row['attempts'] == attempt and row['next_attempt'] == timestamp + delay
            with patch('desk.desk_deliveries.now_ms', return_value=timestamp + delay - 1):
                schedule_delivery(runtime)
                await settle(runtime)
            assert len(state['posts']) == attempt
            timestamp += delay
        assert not pending_deliveries(runtime['config'])
        assert len(rows(runtime, 'delivery_pending')) == 20
        assert all(row['status'] != 'sending' for row in rows(runtime, 'delivery_pending'))
        runtime.pop('deliveries')
        await prepare_deliveries(runtime)
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp + 900000):
            await entry(runtime, 'first')
            await settle(runtime)
        assert len(state['posts']) == 20 and len(rows(runtime, 'delivery_abandoned')) == 1
        state['status'] = 200
        await entry(runtime, 'later', entry_id='2-0')
        await settle(runtime)
        assert posted(state)[-1] == '2-0'


async def check_task_errors():
    async with fixture() as (runtime, state, _):
        diagnostics = []
        loop = asyncio.get_running_loop()
        original = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: diagnostics.append(context))
        try:
            with patch('desk.desk_deliveries.report_delivery', AsyncMock(side_effect=RuntimeError('fake-subscriber-token'))):
                await entry(runtime, 'first')
                await settle(runtime)
            assert [row['reason'] for row in rows(runtime, 'delivery_error')] == ['RuntimeError']
            assert not diagnostics and not runtime['deliveries']['tasks'] and not state['posts']
            assert pending_deliveries(runtime['config'])[0]['status'] == 'queued'
            assert 'fake-subscriber-token' not in Path(runtime['config']['record_path']).read_text()
            schedule_delivery(runtime)
            await settle(runtime)
            assert posted(state) == ['1-0']
        finally:
            loop.set_exception_handler(original)


async def check_odd_timestamps():
    for enabled in (True, False):
        async with fixture() as (runtime, state, _):
            if not enabled:
                runtime.pop('deliveries')
                runtime['config'].pop('signals_url')
            for index, timestamp in enumerate(('', 'invalid', '-1')):
                signal_id = f'odd-{index}'
                await entry(runtime, signal_id, timestamp=timestamp, asset='BTC', entry_id=f'{index}-0')
                await settle(runtime)
                assert rows(runtime, 'consumed')[-1]['result'] == {'state': 'filled'}
                envelope = signal_envelope(signal_id)
                envelope['payload'] = {'kind': 'cancel', 'signal_id': signal_id,
                                       'publisher': envelope['payload']['publisher'], 'published_at': now_ms()}
                fields = stream_fields(envelope)
                fields['ts'] = timestamp
                await entry(runtime, signal_id, fields=fields, entry_id=f'{index}-1')
                await settle(runtime)
                assert rows(runtime, 'consumed')[-1]['result'] == {'state': 'exited'}
            assert not rows(runtime, 'stream_refusal')
            assert len(state['posts']) == (6 if enabled else 0)


async def check_journal_io():
    async with fixture() as (runtime, state, _):
        loop_thread = threading.get_ident()
        writers = []

        def record(path, event, **values):
            writers.append(threading.get_ident())
            append_record(path, event, **values)

        with (patch('desk.desk_deliveries.delivery_state', side_effect=AssertionError('Unexpected journal reread')),
              patch('desk.desk_deliveries.append_record', side_effect=record)):
            await entry(runtime, 'first')
            await settle(runtime)
            await entry(runtime, 'second', entry_id='2-0')
            await settle(runtime)
        assert len(writers) == 4 and all(writer != loop_thread for writer in writers)
        assert posted(state) == ['1-0', '2-0']


async def check_write_cancellation():
    async with fixture() as (runtime, state, _):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def record(path, event, **values):
            if event == 'delivery_reported':
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise RuntimeError('scenario_write_not_released')
            append_record(path, event, **values)

        with patch('desk.desk_deliveries.append_record', side_effect=record):
            finisher = None
            try:
                await entry(runtime, 'first')
                await asyncio.wait_for(started.wait(), 5)
                finisher = asyncio.create_task(finish_deliveries(runtime))
                await asyncio.sleep(0)
                assert not finisher.done()
                assert pending_deliveries(runtime['config'])[0]['event_id'] == '1-0'
            finally:
                release.set()
                if finisher is not None:
                    await asyncio.wait_for(finisher, 5)
        assert posted(state) == ['1-0'] and not runtime['deliveries']['tasks']
        assert not pending_deliveries(runtime['config']) and not runtime['deliveries']['pending']
        assert rows(runtime, 'delivery_reported')[-1]['event_id'] == '1-0'


async def check_safety():
    for name, check in [('8: HTTPS and explicit local-only test transport', check_endpoints),
                        ('9: invalid token and missing credentials allow recovery and trading', check_disabled_recovery),
                        ('10: bounded worker and cancellation preserve pending rows', check_shutdown),
                        ('11: invalid delivery timeouts fall back to ten seconds', check_timeouts),
                        ('12: durable pending precedes cursor and failed writes preserve cursor', check_cursor_order),
                        ('13: 429 stops batch and cooldown survives restart', check_batch_limit),
                        ('14: exponential backoff and terminal abandonment after twenty failures', check_backoff),
                        ('15: task exceptions are retrieved and sanitized', check_task_errors),
                        ('16: odd timestamps still trade and cancel with reporting on or off', check_odd_timestamps),
                        ('17: delivery writes leave the loop and pending is not reread', check_journal_io),
                        ('18: shutdown settles an in-flight journal write', check_write_cancellation)]:
        await check()
        print('PASS deliveries ' + name, flush=True)
