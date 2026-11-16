import asyncio
import io
import json
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .desk_executor import cancel, close_ticket, main as executor_main, process_event, submit
from .desk_io import load_config, oracle_prices
from .desk_record import now_ms, read_records
from .desk_scenarios_fakes import record_anchor_fixture, signal_envelope, submit_verified_fixture
from .desk_wake import last_close
from .desk_scenarios_resident import finish_exit, market_book, next_event
from .desk_ticket import main as ticket_main
from .desk_watcher import queue_event


def trades(price):
    return {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': price}]}


async def check_trade_burst(fixture):
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        ticket = runtime['tickets']['sample']
        before = len(fake['info_requests'])
        for _ in range(500):
            await process_event(runtime, ticket, trades('100000'))
        assert len(fake['info_requests']) == before and len(fake['intents']) == 1
        async with runtime['operation_lock']:
            for _ in range(500):
                queue_event(runtime, ticket, trades('100000'))
            assert runtime['events'].qsize() == 1
        await runtime['events'].join()
        assert len(fake['info_requests']) == before
        await finish_exit(runtime, fake, ticket)


async def check_entry_precision(fixture):
    for side, stop, expected in [('long', '96000', '97124'), ('short', '98200', '97123')]:
        async with fixture() as (runtime, fake):
            envelope = signal_envelope()
            envelope['payload']['side'] = side
            envelope['payload']['levels'] = {'entry': '97123.456', 'stop': stop, 'target': None}
            assert (await submit(runtime, envelope))['state'] == 'filled'
            assert fake['intents'][0]['entry']['limit_px'] == expected
            rows = read_records(runtime['config']['record_path'])
            assert next(row for row in rows if row['event'] == 'gate')['entry_price'] == expected
            assert next(row for row in rows if row['event'] == 'intent')['intent']['entry']['limit_px'] == expected
            assert runtime['tickets']['sample']['signal']['levels']['entry'] == '97123.456'


async def check_stop_gap_reprices(fixture):
    for side, stop, mark in [('long', '99000', '98000'), ('short', '101000', '102000')]:
        async with fixture() as (runtime, fake):
            envelope = signal_envelope()
            envelope['payload']['side'] = side
            envelope['payload']['levels'] = {'entry': '100000', 'stop': stop, 'target': None}
            await submit(runtime, envelope)
            ticket = runtime['tickets']['sample']
            await next_event(runtime, ticket, market_book(mark))
            assert ticket['stop_hit'] and len(fake['intents']) == 1
            assert abs(fake['position']) == Decimal('0.01') and fake['orders'][0]['isTrigger']


async def check_queued_stop_wick(fixture):
    for waiting_for_lock in (False, True):
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            ticket = runtime['tickets']['sample']
            async with runtime['operation_lock']:
                queue_event(runtime, ticket, trades('98999'))
                if waiting_for_lock:
                    await asyncio.sleep(0)
                queue_event(runtime, ticket, trades('100000'))
                assert [trade['px'] for trade in ticket['pending_trades']['data']] == ['98999', '100000']
            await runtime['events'].join()
            assert ticket['stop_hit'] and ticket['closing'] == 'stop'
            await finish_exit(runtime, fake, ticket, '100000')


async def check_first_exit_cached_position(fixture):
    for reason, price in [('stop', '98999'), ('target', '102001')]:
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            ticket = runtime['tickets']['sample']
            start = len(fake['events'])
            await next_event(runtime, ticket, market_book(price))
            calls = fake['events'][start:]
            sent = calls.index('intent')
            assert calls[0] == 'userRateLimit' and 'frontendOpenOrders' not in calls[:sent]
            assert ticket['closing'] == reason
            assert fake['intents'][-1]['tif'] == 'Alo' and Decimal(fake['intents'][-1]['size']) <= Decimal('0.006')


async def check_other_exit_reconciles(fixture):
    for reason in ('cancel', 'expiry', 'watcher_dead', 'restart_recovery'):
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            ticket = runtime['tickets']['sample']
            ticket['position'] = Decimal(0)
            start = len(fake['events'])
            await close_ticket(runtime, ticket, reason)
            assert fake['events'][start:start + 3] == ['frontendOpenOrders', 'userFills', 'clearinghouseState']
            assert fake['orders'][0]['isTrigger'] and fake['position'] != 0
            await finish_exit(runtime, fake, ticket, '100000')


async def check_exit_oracle_failure(fixture):
    for fault in ('ticker_down', 'oracle_down', 'ticker_slow'):
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            fake[fault] = True
            ticket = runtime['tickets']['sample']
            await next_event(runtime, ticket, market_book('98999'))
            assert fake['intents'][-1]['reduce_only']
            rows = read_records(runtime['config']['record_path'])
            sent = [row for row in rows if row['event'] == 'intent'][-1]
            source = 'hl_oracle' if fault == 'oracle_down' else 'binance'
            assert sent[source + '_px'] is None and sent[source + '_error']
    async with fixture() as (runtime, fake):
        fake['ticker_down'] = True
        assert (await submit(runtime, signal_envelope()))['refused'] == 'oracle_unavailable'
        assert not fake['intents']


async def check_binance_mapping(fixture):
    async with fixture() as (runtime, fake):
        for asset, expected in [('BTC', 'BTCUSDT'), ('ETH', 'ETHUSDT'), ('SOL', 'SOLUSDT')]:
            prices = await oracle_prices(runtime, asset, True)
            assert prices['binance_px'] == '100002' and fake['ticker_symbols'][-1] == expected
        before = len(fake['ticker_symbols'])
        prices = await oracle_prices(runtime, 'xyz:TSLA', True)
        assert prices['hl_oracle_px'] == '100001' and prices['binance_error'] == 'unmapped'
        assert len(fake['ticker_symbols']) == before


async def check_exit_cached_meta(fixture):
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        before = len(fake['info_requests'])
        await finish_exit(runtime, fake, runtime['tickets']['sample'])
        requests = fake['info_requests'][before:]
        assert not any(row['type'] == 'meta' for row in requests)
        assert any(intent['kind'] == 'trigger_order' for intent in fake['intents'])
        assert fake['position'] == 0


async def command_result(runtime, module, action, value):
    directory = Path(runtime['config']['record_path']).parent
    envelope_path = directory / 'envelope.json'
    if isinstance(value, dict):
        record_anchor_fixture(runtime['config'], value)
        envelope_path = directory / 'envelope.json'
        envelope_path.write_text(json.dumps(value))
        value = str(envelope_path)
    output = io.StringIO()
    with (patch('desk.desk_ticket.load_config', return_value=runtime['config']),
          patch('desk.desk_executor.load_config', return_value=runtime['config']),
          patch('desk.desk_executor.create_runtime', return_value=runtime),
          patch('desk.desk_ticket.aiohttp.ClientSession') as session, redirect_stdout(output)):
        session.return_value.__aenter__.return_value = runtime['session']
        arguments = SimpleNamespace(config='<fake>', action=action, input=value, envelope=value,
                                    signal_id=value, close_ms=runtime.get('close_ms'))
        await (ticket_main(arguments) if module == 'desk_ticket' else executor_main(arguments))
    return json.loads(output.getvalue())


async def check_ticket_comparison(fixture):
    for case in ('long', 'short', 'null_entry', 'null_stop', 'wrong_stop', 'leverage', 'liquidation',
                 'minimum', 'asset', 'order_cap', 'daily_cap', 'expiry', 'reply_by', 'chain_stale'):
        async with fixture() as (runtime, fake):
            config = runtime['config']
            envelope = signal_envelope(case)
            payload = envelope['payload']
            if case in ('long', 'short'):
                payload['side'] = case
                payload['levels'] = {'entry': '97123.456', 'stop': '96000' if case == 'long' else '98200', 'target': None}
            if case in ('null_entry', 'null_stop'):
                payload['levels'][case.removeprefix('null_')] = None
            if case == 'wrong_stop':
                payload['levels']['stop'] = '101000'
            if case == 'leverage':
                config['risk_fraction'] = '0.1'
            if case == 'liquidation':
                config.update(risk_fraction='0.6', leverage_cap='40')
                payload['levels']['stop'] = '98500'
            if case == 'minimum':
                config['risk_fraction'] = '0.00001'
            if case == 'asset':
                payload['symbol'] = payload['venue_symbol'] = 'ETH'
            if case == 'order_cap':
                config['policy']['asset_caps'][0]['max_order_size'] = '0.005'
            if case == 'daily_cap':
                fake['signed_failure'] = True
                await submit(runtime, signal_envelope('spent'))
                config['policy']['asset_caps'][0]['max_daily_size'] = '0.015'
            if case == 'expiry':
                config['policy']['expires_at'] = now_ms() // 1000 + 60
            if case == 'reply_by':
                payload['reply_by'] = now_ms() - 1
            if case == 'chain_stale':
                config['policy']['chain_stale'] = True
            record_path = Path(config['record_path'])
            before = record_path.read_bytes() if record_path.exists() else b''
            calls = (fake['prepares'], len(fake['intents']))
            sized = await command_result(runtime, 'desk_ticket', 'size', envelope)
            gated = await command_result(runtime, 'desk_ticket', 'gate', envelope)
            assert (fake['prepares'], len(fake['intents'])) == calls
            assert (record_path.read_bytes() if record_path.exists() else b'') == before
            fake['unfilled_entry'] = True
            result = await submit(runtime, envelope)
            assert gated.get('refused') == result.get('refused'), case
            rows = read_records(config['record_path'])
            gates = [row for row in rows if row['event'] == 'gate' and row['key'][2] == case]
            if gates:
                recorded = gates[-1]
                assert sized == gated['sizing'] == recorded['sizing'], case
                assert gated.get('refused') == recorded['reason'], case
                preview = gated['intent']
                assert preview['expires_at'] <= recorded['intent']['expires_at']
                assert {**preview, 'expires_at': recorded['intent']['expires_at']} == recorded['intent']
                assert preview['entry']['limit_px'] == recorded['entry_price']
                if not recorded['reason']:
                    assert gated['status'] == 'PASS'
                    assert all(fake['intents'][-1]['entry'][field] == preview['entry'][field] for field in
                               ('asset', 'asset_index', 'size', 'limit_px', 'is_buy', 'reduce_only', 'tif'))
            else:
                assert sized == gated == result, case
            recorded_review = next(row for row in rows if row['event'] == 'review' and row['key'][2] == case) if gates else None
            if recorded_review:
                assert await command_result(runtime, 'desk_ticket', 'review', case) == recorded_review
                assert read_records(config['record_path']) == rows



async def check_nonresident(fixture):
    async with fixture() as (runtime, fake):
        config = runtime['config']
        config.update(resident=False, stop_mode='software', symbols={})
        runtime.pop('close_ms', None)
        assert await command_result(runtime, 'desk_executor', 'run', signal_envelope('missing-close')) == {
            'refused': 'stale_wake_close'}
        assert await submit_verified_fixture(runtime, signal_envelope('missing-close')) == {'refused': 'stale_wake_close'}
        runtime['close_ms'] = last_close(now_ms())
        result = await command_result(runtime, 'desk_executor', 'run', signal_envelope('routine'))
        assert result == {'refused': 'published_not_traded'}
        assert fake['prepares'] == 0 and not fake['intents'] and not fake['websockets']
        assert await submit_verified_fixture(runtime, signal_envelope()) == result
        assert fake['prepares'] == 0 and not fake['intents']
        path = Path(config['record_path']).parent / 'config.json'
        config['stop_mode'] = 'trigger'
        assert await command_result(runtime, 'desk_executor', 'run', signal_envelope('trigger-routine')) == result
        assert await submit_verified_fixture(runtime, signal_envelope('trigger')) == result
        assert fake['prepares'] == 0 and not fake['intents'] and not fake['websockets']
        for resident, stop_mode in ((True, 'software'), (True, 'trigger')):
            config.update(resident=resident, stop_mode=stop_mode)
            fake['unfilled_entry'] = True
            assert (await submit_verified_fixture(runtime, signal_envelope('resident-' + stop_mode)))['state'] == 'not_filled'
        config.pop('resident')
        config.pop('stop_mode')
        path.write_text(json.dumps(config))
        loaded = load_config(path)
        assert loaded['resident'] is True and loaded['stop_mode'] == 'software'
        for changes, reason in (({'resident': 'false'}, 'invalid_resident'), ({'stop_mode': 'none'}, 'invalid_stop_mode')):
            path.write_text(json.dumps({**config, **changes}))
            try:
                load_config(path)
            except ValueError as error:
                assert str(error) == reason
            else:
                raise AssertionError(reason)
    for stop_mode, envelope_cancel in (('software', False), ('software', True), ('trigger', False), ('trigger', True)):
        async with fixture() as (runtime, fake):
            await submit_verified_fixture(runtime, signal_envelope())
            await runtime['events'].join()
            runtime['config'].update(resident=False, stop_mode=stop_mode)
            if envelope_cancel:
                envelope = {'version': 1, 'payload': {'kind': 'cancel', 'signal_id': 'sample', 'publisher': 'publisher-placeholder'}}
                result = await submit_verified_fixture(runtime, envelope)
            else:
                result = await command_result(runtime, 'desk_executor', 'cancel', 'sample')
            assert result == {'state': 'exited'} and fake['position'] == 0 and not fake['orders']
            assert [intent['kind'] for intent in fake['intents']] == ['bracket', 'cancel', 'order']
            assert fake['intents'][-1]['reduce_only']


async def check_execution(fixture):
    for check in (check_trade_burst, check_entry_precision, check_stop_gap_reprices, check_queued_stop_wick,
                  check_first_exit_cached_position, check_other_exit_reconciles, check_exit_oracle_failure,
                  check_binance_mapping, check_exit_cached_meta, check_ticket_comparison, check_nonresident):
        await check(fixture)
        print('PASS ' + check.__name__, flush=True)
