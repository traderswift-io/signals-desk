import asyncio
import json
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from .desk_deliveries import finish_deliveries, prepare_deliveries, schedule_delivery, write_delivery
from .desk_executor import cancel, create_runtime, event_worker, recover, shutdown
from .desk_record import delivery_path, now_ms, pending_deliveries, read_records
from .desk_scenarios_fakes import signal_envelope, start_fake, stop_fake
from .desk_scenarios_redis import stream_fields
from .desk_signal_consumer import consume


async def deliveries(request):
    state = request.app['state']
    body = await request.json()
    state['posts'].append({'body': body, 'headers': dict(request.headers), 'path': request.path})
    state['arrived'].set()
    event_id = body.get('event_id')
    if not isinstance(event_id, str) or re.fullmatch(r'\d+-\d+', event_id) is None:
        return web.json_response({'recorded': False}, status=404)
    if state['hold']:
        await state['release'].wait()
    if event_id in state.get('responses', {}):
        body, content_type = state['responses'][event_id]
        return web.Response(body=body, headers={'Content-Type': content_type})
    if state.get('raw') is not None:
        return web.Response(text=state['raw'], content_type='application/json')
    return web.json_response(state['body'], status=state['status'], headers=state['headers'])


@asynccontextmanager
async def fixture():
    with tempfile.TemporaryDirectory() as directory:
        async with aiohttp.ClientSession() as session:
            directory = Path(directory)
            runtime, trading, trading_runner = await start_fake(directory, session)
            state = {'posts': [], 'status': 200, 'body': {'recorded': True}, 'headers': {},
                     'hold': False, 'arrived': asyncio.Event(), 'release': asyncio.Event()}
            application = web.Application()
            application['state'] = state
            application.add_routes([web.post('/v1/deliveries', deliveries)])
            runner = web.AppRunner(application)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            token = directory / 'subscriber.token'
            token.write_text('fake-subscriber-token\n')
            runtime['config'].update(signals_url=f'http://127.0.0.1:{port}', subscriber_token_file=str(token),
                                     signals_access_client_id='fake-signals-id',
                                     signals_access_client_secret='fake-signals-secret')
            runtime['allow_http_deliveries'] = True
            await prepare_deliveries(runtime)
            try:
                yield runtime, state, trading
            finally:
                await finish_deliveries(runtime)
                state['release'].set()
                await runner.cleanup()
                await stop_fake(runtime, trading_runner)


def rows(runtime, event):
    path = delivery_path(runtime['config']) if event.startswith(('delivery_', 'deliveries_')) else runtime['config']['record_path']
    return [row for row in read_records(path) if row['event'] == event]


def posted(state):
    return [post['body']['event_id'] for post in state['posts']]


async def settle(runtime):
    async def drain():
        while runtime.get('deliveries', {}).get('tasks'):
            await asyncio.gather(*tuple(runtime['deliveries']['tasks']), return_exceptions=True)
    await asyncio.wait_for(drain(), 5)


async def entry(runtime, signal_id, event_id=None, timestamp=None, asset='ETH', fields=None, entry_id='1-0'):
    if fields is None:
        fields = stream_fields(signal_envelope(signal_id, asset))
        fields['event_id'] = 'event-' + signal_id if event_id is None else event_id
        fields['ts'] = str(now_ms() if timestamp is None else timestamp)
    client = SimpleNamespace(xread=AsyncMock(return_value=[('signals', [(entry_id, fields)])]))
    await consume(runtime, client, 1)


async def check_success():
    async with fixture() as (runtime, state, trading):
        await entry(runtime, 'first', asset='BTC')
        await settle(runtime)
        assert len(state['posts']) == 1
        post = state['posts'][0]
        assert post['body'] == {'signal_id': 'first', 'event_id': '1-0'}
        assert post['headers']['Authorization'] == 'Bearer fake-subscriber-token'
        assert post['headers']['CF-Access-Client-Id'] == 'fake-signals-id'
        assert post['headers']['CF-Access-Client-Secret'] == 'fake-signals-secret'
        assert [row['event_id'] for row in rows(runtime, 'delivery_reported')] == ['1-0']
        assert not pending_deliveries(runtime['config'])
        Path(runtime['config']['subscriber_token_file']).unlink()
        await entry(runtime, 'first', event_id='', asset='BTC')
        await settle(runtime)
        assert posted(state) == ['1-0', '1-0']
        assert rows(runtime, 'consumed')[-1]['result'] == {'refused': 'already_acted'}
        assert len(trading['intents']) == 1
        assert state['posts'][-1]['headers']['Authorization'] == 'Bearer fake-subscriber-token'


async def check_unavailable():
    async with fixture() as (runtime, state, _):
        timestamp = now_ms()
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp):
            state['status'] = 503
            await entry(runtime, 'first')
            await settle(runtime)
        assert pending_deliveries(runtime['config'])[0]['status'] == 503
        state['status'] = 200
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp + 5000):
            await entry(runtime, 'second', entry_id='2-0')
            await settle(runtime)
        assert posted(state) == ['1-0', '1-0', '2-0']
        assert [row['event_id'] for row in rows(runtime, 'delivery_reported')] == ['1-0', '2-0']
        assert not pending_deliveries(runtime['config'])
        for index in range(12):
            await write_delivery(runtime, 'delivery_pending', signal_id='older', event_id=f'0-{index}', status=503)
        await write_delivery(runtime, 'delivery_pending', signal_id='older', event_id='0-0', status=503)
        await entry(runtime, 'third', entry_id='3-0')
        await settle(runtime)
        assert posted(state)[3:] == [f'0-{index}' for index in range(10)] + ['3-0']
        assert [row['event_id'] for row in pending_deliveries(runtime['config'])] == ['0-10', '0-11']


async def check_rate_limit():
    async with fixture() as (runtime, state, _):
        timestamp = now_ms()
        state.update(status=429, headers={'Retry-After': '30'})
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp):
            await entry(runtime, 'first', timestamp=1)
            await settle(runtime)
        pending = pending_deliveries(runtime['config'])
        assert pending[0]['status'] == 429 and pending[0]['not_before'] == timestamp + 30000
        state.update(status=200, headers={})
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp + 10000):
            await entry(runtime, 'inside', timestamp=timestamp + 999999, entry_id='2-0')
            await settle(runtime)
        assert posted(state) == ['1-0']
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp + 30001):
            await entry(runtime, 'after', timestamp=-1, entry_id='3-0')
            await settle(runtime)
        assert posted(state) == ['1-0', '1-0', '2-0', '3-0']
        assert not pending_deliveries(runtime['config'])


async def check_timeout():
    async with fixture() as (runtime, state, trading):
        runtime['config']['request_timeout_seconds'] = 1
        state['hold'] = True
        await entry(runtime, 'first', asset='BTC')
        assert rows(runtime, 'consumed')[-1]['result'] == {'state': 'filled'}
        await asyncio.wait_for(state['arrived'].wait(), 5)
        assert runtime['deliveries']['tasks'] and not rows(runtime, 'delivery_reported')
        assert await cancel(runtime, 'first') == {'state': 'exited'}
        assert trading['position'] == 0 and not state['release'].is_set()
        assert not any(row.get('status') == 'timeout' for row in rows(runtime, 'delivery_pending'))
        await settle(runtime)
        assert pending_deliveries(runtime['config'])[0]['status'] == 'timeout'


async def check_restart():
    async with fixture() as (runtime, state, trading):
        timestamp = now_ms()
        state['status'] = 503
        with patch('desk.desk_deliveries.now_ms', return_value=timestamp):
            await entry(runtime, 'first', asset='BTC')
            await settle(runtime)
        await shutdown(runtime)
        restarted = create_runtime(runtime['config'], runtime['session'])
        restarted['allow_http_deliveries'] = True
        await prepare_deliveries(restarted)
        restarted['worker'] = asyncio.create_task(event_worker(restarted))
        try:
            assert pending_deliveries(restarted['config'])[0]['event_id'] == '1-0'
            state['status'] = 200
            await recover(restarted)
            assert trading['position'] == 0 and posted(state) == ['1-0']
            with patch('desk.desk_deliveries.now_ms', return_value=timestamp + 5000):
                schedule_delivery(restarted)
                await settle(restarted)
            assert posted(state) == ['1-0', '1-0']
            assert not pending_deliveries(restarted['config'])
            assert len([intent for intent in trading['intents'] if not intent.get('reduce_only', True)]) == 1
        finally:
            await finish_deliveries(restarted)
            await shutdown(restarted)


async def check_malformed():
    async with fixture() as (runtime, state, _):
        valid = stream_fields(signal_envelope('broken'))
        broken = [{}, *[{**valid, 'envelope': value} for value in ('{', 'null', '[]', '1', json.dumps('text'))],
                  {**valid, 'correlation_id': 'different'}, {**valid, 'kind': 'unknown'}]
        for fields in broken:
            await entry(runtime, 'broken', fields=fields)
        await settle(runtime)
        assert not state['posts'] and not rows(runtime, 'consumed')
        assert len(rows(runtime, 'stream_refusal')) == len(broken)


async def check_secrets():
    async with fixture() as (runtime, state, _):
        state['body'] = {'recorded': False, 'error': 'fake-subscriber-token fake-signals-id fake-signals-secret'}
        for status in (400, 401, 403, 500, 200):
            state['status'] = status
            await entry(runtime, f'refused-{status}', entry_id=f'{status}-0')
            await settle(runtime)
            assert pending_deliveries(runtime['config'])[-1]['status'] == status
        state.update(raw='fake-subscriber-token fake-signals-secret')
        await entry(runtime, 'invalid-json', entry_id='600-0')
        await settle(runtime)
        assert not rows(runtime, 'delivery_reported')
        text = Path(runtime['config']['record_path']).read_text() + Path(delivery_path(runtime['config'])).read_text()
        assert all(secret not in text for secret in ('fake-subscriber-token', 'fake-signals-id', 'fake-signals-secret',
                                                     'Authorization', 'CF-Access-Client-Id', 'CF-Access-Client-Secret'))


async def main():
    from .desk_scenarios_delivery_safety import check_safety
    from .desk_scenarios_delivery_persistence import check_persistence
    from .desk_scenarios_delivery_recovery import check_recovery

    for name, check in [('1: body, headers, recorded and redelivery', check_success),
                        ('2: 503 pending, ordered retries and ten-report limit', check_unavailable),
                        ('3: Retry-After uses wall time and blocks fresh reports', check_rate_limit),
                        ('4: held response never delays trading', check_timeout),
                        ('5: durable pending retried after recovery', check_restart),
                        ('6: malformed stream entries never reported', check_malformed),
                        ('7: credentials absent from records on failures', check_secrets)]:
        await check()
        print('PASS deliveries ' + name, flush=True)
    await check_safety()
    await check_persistence()
    await check_recovery()


if __name__ == '__main__':
    asyncio.run(main())
