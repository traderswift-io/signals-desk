import asyncio
import json
from decimal import Decimal
from pathlib import Path

from .desk_executor import cancel, close_ticket, process_event, submit
from .desk_io import load_config, oracle_prices
from .desk_record import now_ms, read_records
from .desk_scenarios_fakes import signal_envelope
from .desk_watcher import queue_event
from .signer_delegate_client import order_statuses


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
            queue_event(runtime, ticket, trades('100000'))
            for _ in range(500):
                queue_event(runtime, ticket, trades('100000'))
            assert runtime['events'].qsize() == 1
        await runtime['events'].join()
        assert len(fake['info_requests']) == before and len(fake['intents']) == 1
        start = len(fake['events'])
        async with runtime['operation_lock']:
            queue_event(runtime, ticket, trades('100000'))
            await asyncio.sleep(0)
            for _ in range(500):
                queue_event(runtime, ticket, trades('100000'))
            queue_event(runtime, ticket, trades('98999'))
            assert runtime['events'].qsize() == 0
        await runtime['events'].join()
        assert ticket['state'] == 'exited' and fake['position'] == 0
        assert len(fake['intents']) == 2 and fake['intents'][-1]['reduce_only']
        calls = fake['events'][start:]
        before_intent = calls[:calls.index('intent')]
        assert before_intent[0] == 'prepare'
        assert sorted(before_intent[1:]) == ['metaAndAssetCtxs', 'ticker']
        assert Decimal(fake['intents'][-1]['limit_px']) == Decimal('98505')


async def check_entry_precision(fixture):
    for side, stop, expected in [('long', '96000', '97124'), ('short', '98200', '97123')]:
        async with fixture() as (runtime, fake):
            envelope = signal_envelope()
            envelope['payload']['side'] = side
            envelope['payload']['levels'] = {'entry': '97123.456', 'stop': stop, 'target': None}
            assert (await submit(runtime, envelope))['state'] == 'filled'
            assert fake['intents'][0]['limit_px'] == expected
            rows = read_records(runtime['config']['record_path'])
            assert next(row for row in rows if row['event'] == 'gate')['entry_price'] == expected
            assert next(row for row in rows if row['event'] == 'intent')['intent']['limit_px'] == expected
            assert runtime['tickets']['sample']['signal']['levels']['entry'] == '97123.456'


async def check_stop_gap_reprices(fixture):
    for side, stop, mark, prices in [('long', '99000', '96030', ['98505', '95549']),
                                     ('short', '101000', '104030', ['101505', '104551'])]:
        async with fixture() as (runtime, fake):
            envelope = signal_envelope()
            envelope['payload']['side'] = side
            envelope['payload']['levels'] = {'entry': '100000', 'stop': stop, 'target': None}
            await submit(runtime, envelope)
            await runtime['events'].join()
            ticket = runtime['tickets']['sample']
            fake['mark'], fake['match_exit_mark'] = mark, True
            async with asyncio.timeout(5):
                await process_event(runtime, ticket, trades(mark))
            assert ticket['state'] == 'exited' and ticket['stop_hit'] and fake['position'] == 0
            exits = fake['intents'][1:]
            assert [Decimal(intent['limit_px']) for intent in exits] == list(map(Decimal, prices))
            assert all(intent['reduce_only'] and intent['tif'] == 'Ioc' for intent in exits)
            assert all(intent['is_buy'] == (side == 'short') for intent in exits)
            assert len(fake['fills']) == 2
            rows = read_records(runtime['config']['record_path'])
            outcomes = [row['outcome'] for row in rows if row['event'] == 'exit_result']
            assert order_statuses(outcomes[0]) == [{'error': 'Ioc order would have no immediate match'}]
            assert Decimal(order_statuses(outcomes[1])[0]['filled']['totalSz']) == Decimal('0.01')


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
                assert runtime['events'].qsize() == (0 if waiting_for_lock else 1)
            await runtime['events'].join()
            assert ticket['state'] == 'exited' and ticket['stop_hit'] and fake['position'] == 0
            assert len(fake['intents']) == 2 and fake['intents'][-1]['reduce_only']
            assert Decimal(fake['intents'][-1]['limit_px']) == Decimal('98505')


async def check_first_exit_cached_position(fixture):
    for reason, price, limit in [('stop', '98999', '98505'), ('target', '102001', '101490')]:
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            ticket = runtime['tickets']['sample']
            start = len(fake['events'])
            await process_event(runtime, ticket, trades(price))
            calls = fake['events'][start:]
            sent = calls.index('intent')
            assert calls[:sent][0] == 'prepare'
            assert sorted(calls[1:sent]) == ['metaAndAssetCtxs', 'ticker']
            assert calls[sent + 1:] == ['openOrders', 'userFills', 'clearinghouseState']
            assert ticket['state'] == 'exited' and ticket['closing'] == reason and fake['position'] == 0
            assert Decimal(fake['intents'][-1]['size']) == Decimal('0.01') and fake['intents'][-1]['reduce_only']
            assert Decimal(fake['intents'][-1]['limit_px']) == Decimal(limit)


async def check_other_exit_reconciles(fixture):
    for reason in ('cancel', 'expiry', 'watcher_dead', 'restart_recovery'):
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            ticket = runtime['tickets']['sample']
            ticket['position'] = Decimal(0)
            start = len(fake['events'])
            await close_ticket(runtime, ticket, reason)
            assert fake['events'][start:start + 3] == ['openOrders', 'userFills', 'clearinghouseState']
            assert ticket['state'] == 'exited' and fake['position'] == 0
            assert len(fake['intents']) == 2 and fake['intents'][-1]['reduce_only']


async def check_exit_oracle_failure(fixture):
    for fault in ('ticker_down', 'oracle_down', 'ticker_slow'):
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            fake[fault] = True
            before = len(fake.get('ticker_symbols', []))
            await process_event(runtime, runtime['tickets']['sample'], trades('98999'))
            assert fake['position'] == 0 and len(fake['intents']) == 2
            assert fake['intents'][-1]['reduce_only']
            assert len(fake['ticker_symbols']) == before + 1
            rows = read_records(runtime['config']['record_path'])
            sent = [row for row in rows if row['event'] == 'intent'][-1]
            source = 'hl_oracle' if fault == 'oracle_down' else 'binance'
            assert sent[source + '_px'] is None and sent[source + '_timestamp'] is None
            assert sent[source + '_error']
            other = 'binance' if source == 'hl_oracle' else 'hl_oracle'
            assert sent[other + '_px'] is not None
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        fake['orders'] = [{'coin': 'BTC', 'oid': 42, 'sz': '0.001'}]
        await process_event(runtime, runtime['tickets']['sample'], {'channel': 'userFills'})
        fake['ticker_down'] = fake['oracle_down'] = True
        await process_event(runtime, runtime['tickets']['sample'], trades('98999'))
        assert fake['position'] == 0 and not fake['orders']
        assert [intent['kind'] for intent in fake['intents']] == ['order', 'cancel', 'order']
        rows = read_records(runtime['config']['record_path'])
        for sent in [row for row in rows if row['event'] == 'intent'][1:]:
            assert sent['hl_oracle_px'] is None and sent['binance_px'] is None
            assert sent['hl_oracle_error'] and sent['binance_error']
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
        assert prices['hl_oracle_px'] == '100001'
        assert prices['binance_px'] is None and prices['binance_error'] == 'unmapped'
        assert len(fake['ticker_symbols']) == before


async def check_exit_cached_meta(fixture):
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        fake['partial_exit'] = True
        before = len(fake['info_requests'])
        await process_event(runtime, runtime['tickets']['sample'], trades('98999'))
        requests = fake['info_requests'][before:]
        assert sum(row['type'] == 'metaAndAssetCtxs' for row in requests) == 3
        assert not any(row['type'] == 'meta' for row in requests)
        assert [Decimal(intent['limit_px']) for intent in fake['intents'][1:]] == [Decimal('98505'), Decimal('99500')]
        assert fake['position'] == 0
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        fake['mark'] = '98000'
        before = len(fake['info_requests'])
        await cancel(runtime, 'sample')
        requests = fake['info_requests'][before:]
        assert sum(row['type'] == 'metaAndAssetCtxs' for row in requests) == 2
        assert Decimal(fake['intents'][-1]['limit_px']) == Decimal('97510') and fake['position'] == 0


async def command_result(runtime, module, action, value):
    directory = Path(runtime['config']['record_path']).parent
    config_path = directory / 'config.json'
    config = dict(runtime['config'])
    if module == 'desk_ticket':
        config.update(delegate_key_file=str(directory / 'unavailable.key'),
                      access_client_id='<unused>', access_client_secret='<unused>')
        if action == 'size':
            config.pop('delegate_public_key', None)
    config_path.write_text(json.dumps(config))
    if isinstance(value, dict):
        envelope_path = directory / 'envelope.json'
        envelope_path.write_text(json.dumps(value))
        value = str(envelope_path)
    process = await asyncio.create_subprocess_exec('python3', '-B', '-m', 'desk.' + module,
                                                   '--config', str(config_path), action, value,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, errors = await process.communicate()
    assert process.returncode == 0, errors.decode()
    return json.loads(output)


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
                assert preview['limit_px'] == recorded['entry_price']
                if not recorded['reason']:
                    assert gated['status'] == 'PASS'
                    assert all(fake['intents'][-1][field] == preview[field] for field in
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
        config.update(resident=False, stop_mode='software')
        result = await command_result(runtime, 'desk_executor', 'run', signal_envelope('routine'))
        assert result == {'refused': 'no_resting_stop_until_rotation'}
        assert fake['prepares'] == 0 and not fake['intents'] and not fake['websockets']
        assert await submit(runtime, signal_envelope()) == result
        assert fake['prepares'] == 0 and not fake['intents']
        path = Path(config['record_path']).parent / 'config.json'
        config['stop_mode'] = 'trigger'
        assert await command_result(runtime, 'desk_executor', 'run', signal_envelope('trigger-routine')) == result
        assert await submit(runtime, signal_envelope('trigger')) == result
        assert fake['prepares'] == 0 and not fake['intents'] and not fake['websockets']
        for resident, stop_mode in ((True, 'software'), (True, 'trigger')):
            config.update(resident=resident, stop_mode=stop_mode)
            fake['unfilled_entry'] = True
            assert (await submit(runtime, signal_envelope('resident-' + stop_mode)))['state'] == 'not_filled'
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
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            fake['orders'] = [{'oid': 42, 'coin': 'BTC', 'sz': '0.006'}]
            runtime['config'].update(resident=False, stop_mode=stop_mode)
            if envelope_cancel:
                envelope = {'version': 1, 'payload': {'kind': 'cancel', 'signal_id': 'sample', 'publisher': 'publisher-placeholder'}}
                result = await submit(runtime, envelope)
            else:
                result = await command_result(runtime, 'desk_executor', 'cancel', 'sample')
            assert result == {'state': 'exited'} and fake['position'] == 0 and not fake['orders']
            assert [intent['kind'] for intent in fake['intents']] == ['order', 'cancel', 'order']
            assert fake['intents'][-1]['reduce_only']


async def check_execution(fixture):
    for check in (check_trade_burst, check_entry_precision, check_stop_gap_reprices, check_queued_stop_wick,
                  check_first_exit_cached_position, check_other_exit_reconciles, check_exit_oracle_failure,
                  check_binance_mapping, check_exit_cached_meta, check_ticket_comparison, check_nonresident):
        await check(fixture)
        print('PASS ' + check.__name__, flush=True)
