import asyncio
import copy
import json
import tempfile
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import aiohttp

from .desk_executor import cancel, control_connection, recover, submit
from .desk_io import load_config
from .desk_keygen import generate_key
from .desk_record import now_ms, read_records, set_state, signed_today, ticket_key
from .desk_scenarios_checks import check_gate, check_intake, check_prompt_front_matter, check_tree_style, check_sizer, check_symbol_mapping, check_ticket_local, check_ticket_preview_local
from .desk_scenarios_fakes import eventually, meta, signal_envelope, start_fake, stop_fake, trade
from .desk_scenarios_redis import check_consumer
from .desk_scenarios_execution import check_execution
from .desk_signal import parse_signal
from .desk_watcher import arm, queue_event
from .signer_delegate_client import refresh_meta


@asynccontextmanager
async def fixture():
    with tempfile.TemporaryDirectory() as directory:
        async with aiohttp.ClientSession() as session:
            runtime, fake, runner = await start_fake(Path(directory), session)
            try:
                yield runtime, fake
            finally:
                await stop_fake(runtime, runner)


async def check_failed_budget():
    async with fixture() as (runtime, fake):
        fake['signed_failure'] = True
        assert (await submit(runtime, signal_envelope('failed')))['state'] == 'not_filled'
        assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
        runtime['config']['policy']['asset_caps'][0]['max_daily_size'] = '0.015'
        assert (await submit(runtime, signal_envelope('over-budget')))['refused'] == 'over_max_daily_size'
        assert len(fake['intents']) == 1


async def check_happy():
    async with fixture() as (runtime, fake):
        assert (await submit(runtime, signal_envelope()))['state'] == 'filled'
        assert fake['events'].index('watcher_armed') < fake['events'].index('intent')
        await trade(fake, '98999')
        await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
        assert fake['position'] == 0 and len(fake['intents']) == 2
        assert [intent['reduce_only'] for intent in fake['intents']] == [False, True]
        assert all(intent['tif'] == 'Ioc' for intent in fake['intents'])
        rows = read_records(runtime['config']['record_path'])
        assert [row['state'] for row in rows if row['event'] == 'state'] == ['claimed', 'sent', 'filled', 'exited']
        reviews = [row for row in rows if row['event'] == 'review']
        assert len(reviews) == 1 and reviews[0]['stop'] == 'software' and reviews[0]['stop_hit']
        assert reviews[0]['fill_price'] == '100000' and Decimal(reviews[0]['slippage']) == 0
        for row in (row for row in rows if row['event'] == 'intent'):
            assert row['hl_oracle_px'] == '100001' and row['binance_px'] == '100002'
            assert row['hl_oracle_timestamp'] > 0 and row['binance_timestamp'] > 0
        assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')


async def check_prepare():
    async with fixture() as (runtime, fake):
        fake['prepare_refused'] = True
        assert (await submit(runtime, signal_envelope()))['refused'] == 'prepare_refused: policy_unavailable'
        assert not fake['intents']
        assert any(row.get('reason') == 'prepare_refused: policy_unavailable' for row in read_records(runtime['config']['record_path']))
    async with fixture() as (runtime, fake):
        fake['stale'] = 1
        assert (await submit(runtime, signal_envelope()))['state'] == 'filled'
        assert fake['prepares'] == 2 and len(fake['intents']) == 2
        assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
        assert fake['intents'][0]['venue_nonce'] != fake['intents'][1]['venue_nonce']
    async with fixture() as (runtime, fake):
        fake['stale'] = 2
        assert 'nonce_not_above_highest_signed_' in (await submit(runtime, signal_envelope()))['refused']
        assert fake['prepares'] == 2 and len(fake['intents']) == 2
        assert signed_today(runtime['config'], 'BTC') == 0


async def check_rearm():
    async with fixture() as (runtime, fake):
        fake['bad_rearm'] = True
        assert (await submit(runtime, signal_envelope()))['refused'] == 'client_refused_rearm_intent: wrong_vault'
        assert not fake['intents']


async def check_duplicate():
    async with fixture() as (runtime, fake):
        envelope = signal_envelope()
        await submit(runtime, envelope)
        assert (await submit(runtime, envelope))['refused'] == 'already_acted'
        envelope['payload']['publisher'] = 'another-publisher'
        assert (await submit(runtime, envelope))['refused'] == 'already_acted'
        assert len(fake['intents']) == 1
        key_path = Path(runtime['config']['delegate_key_file'])
        original = key_path.read_bytes()
        try:
            generate_key(runtime['config'])
        except FileExistsError:
            pass
        else:
            raise AssertionError('Key overwritten')
        assert key_path.read_bytes() == original and key_path.stat().st_mode & 0o777 == 0o600


async def seed_live_entry(runtime, fake):
    signal = parse_signal(signal_envelope('live'), meta(), now_ms())
    runtime['asset_indices'] = {'BTC': 0, 'ETH': 1}
    await refresh_meta(runtime)
    ticket = {'key': ticket_key(runtime['config'], signal), 'signal': signal, 'claimed_at': now_ms(),
              'position': Decimal(0), 'orders': [], 'alive': False}
    runtime['tickets']['live'] = ticket
    set_state(runtime['config'], ticket, 'claimed')
    await arm(runtime, ticket)
    set_state(runtime['config'], ticket, 'sent')
    fake['orders'] = [{'oid': 42, 'coin': 'BTC', 'sz': '0.006'}]
    fake['position'] = Decimal('0.004')
    return ticket


async def check_cancel():
    async with fixture() as (runtime, fake):
        await seed_live_entry(runtime, fake)
        config = runtime['config']
        path = Path(config['record_path']).parent / 'config.json'
        path.write_text(json.dumps(config))
        server = await asyncio.start_unix_server(lambda reader, writer: control_connection(runtime, reader, writer), path=config['control_socket'])
        async with server:
            process = await asyncio.create_subprocess_exec('python3', '-B', '-m', 'desk.desk_executor', '--config', str(path),
                                                           'cancel', 'live', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, errors = await process.communicate()
            assert process.returncode == 0, errors.decode()
            assert json.loads(output)['state'] == 'exited'
        assert not fake['orders'] and fake['position'] == 0
        assert [intent['kind'] for intent in fake['intents']] == ['cancel', 'order']
        assert fake['intents'][-1]['reduce_only']
    async with fixture() as (runtime, fake):
        await seed_live_entry(runtime, fake)
        envelope = {'version': 1, 'payload': {'kind': 'cancel', 'signal_id': 'live', 'publisher': 'publisher-placeholder'}, 'receipt': None}
        assert (await submit(runtime, envelope))['state'] == 'exited'
        assert not fake['orders'] and fake['position'] == 0
        assert fake['intents'][-1]['reduce_only']


async def check_recovery():
    async with fixture() as (runtime, fake):
        fake['dead_on_fill'] = True
        assert (await submit(runtime, signal_envelope()))['state'] == 'exited'
        assert len(fake['intents']) == 2 and fake['intents'][-1]['reduce_only'] and fake['position'] == 0
    async with fixture() as (runtime, fake):
        fake['timeout_entry'] = True
        runtime['config']['request_timeout_seconds'] = 0.03
        assert (await submit(runtime, signal_envelope()))['state'] == 'filled'
        sent_index = fake['events'].index('intent')
        assert 'openOrders' in fake['events'][sent_index:] and 'userFills' in fake['events'][sent_index:]
        assert len(fake['intents']) == 1
        await cancel(runtime, 'sample')
        assert len([intent for intent in fake['intents'] if not intent.get('reduce_only', True)]) == 1
        assert fake['position'] == 0


async def check_rate_limit():
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        fake['rate_exit'] = True
        await trade(fake, '98999')
        await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
        assert len(fake['intents']) == 3 and fake['position'] == 0
        exits = fake['intents'][1:]
        assert exits[0]['request_id'] == exits[1]['request_id']
        records = read_records(runtime['config']['record_path'])
        backoffs = [row for row in records if row['event'] == 'backoff']
        assert len(backoffs) == 1 and backoffs[0]['retry'] and backoffs[0]['seconds'] == '0.01'
        sends = [row for row in records if row['event'] == 'intent' and row['intent']['reduce_only']]
        assert sends[1]['timestamp'] - sends[0]['timestamp'] >= 10
    async with fixture() as (runtime, fake):
        fake['rate_entry'] = True
        assert (await submit(runtime, signal_envelope()))['refused'] == 'http_429'
        assert len(fake['intents']) == 1 and fake['position'] == 0
        assert signed_today(runtime['config'], 'BTC') == 0


async def check_symbols():
    check_symbol_mapping()
    for incoming, canonical, asset_index in [('XYZ:TSLA', 'xyz:TSLA', 120001), ('KPEPE', 'kPEPE', 2)]:
        async with fixture() as (runtime, fake):
            runtime['config']['policy']['asset_caps'].append(
                {'asset': canonical, 'max_order_size': '0.02', 'max_daily_size': '0.03'})
            envelope = signal_envelope(asset=incoming)
            if incoming == 'KPEPE':
                envelope['payload'].pop('venue_symbol')
            assert (await submit(runtime, envelope))['state'] == 'filled'
            resolved = runtime['tickets']['sample']['signal']
            assert resolved['asset'] == resolved['venue_symbol'] == canonical
            assert resolved['asset_index'] == asset_index
            assert any(request['type'] == 'perpDexs' for request in fake['info_requests'])
            assert any(request == {'type': 'meta', 'dex': 'xyz'} for request in fake['info_requests'])
            fake['orders'] = [{'coin': canonical, 'oid': 42, 'sz': '0.001'}]
            await trade(fake, '98999', canonical)
            await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
            assert fake['position'] == 0 and not fake['orders']
            assert [intent['kind'] for intent in fake['intents']] == ['order', 'cancel', 'order']
            assert fake['intents'][-1]['reduce_only']
            assert all(intent['asset'] == canonical and intent['asset_index'] == asset_index for intent in fake['intents'])
            records = read_records(runtime['config']['record_path'])
            assert all(row['signal']['venue_symbol'] == canonical for row in records if row['event'] == 'state')
    for incoming, ambiguous, reason in [('KPEPE', True, 'symbol_ambiguous'), ('UNKNOWN', False, 'symbol_unmapped')]:
        async with fixture() as (runtime, fake):
            fake['ambiguous_symbol'] = ambiguous
            assert (await submit(runtime, signal_envelope(asset=incoming)))['refused'] == reason
            assert not fake['intents'] and fake['prepares'] == 0
            assert read_records(runtime['config']['record_path'])[-1]['reason'] == reason


async def check_events():
    for channel in ('trades', 'userFills'):
        async with fixture() as (runtime, fake):
            assert (await submit(runtime, signal_envelope()))['state'] == 'filled'
            await runtime['events'].join()
            runtime['config']['policy']['expires_at'] = now_ms() // 1000 + 59
            await asyncio.sleep(0.02)
            assert len(fake['intents']) == 1 and fake['position'] != 0
            if channel == 'trades':
                await trade(fake, '100000')
            else:
                for websocket in tuple(fake['websockets']):
                    await websocket.send_json({'channel': 'userFills', 'data': {'fills': []}})
            await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
            assert fake['position'] == 0 and len(fake['intents']) == 2
            assert fake['intents'][-1]['reduce_only'] and fake['intents'][-1]['tif'] == 'Ioc'
            rows = read_records(runtime['config']['record_path'])
            assert [row['reason'] for row in rows if row['event'] == 'state' and row['state'] == 'exited'] == ['expiry']
    async with fixture() as (runtime, fake):
        fake['after_entry'] = lambda: runtime['config']['policy'].update(expires_at=now_ms() // 1000 + 59)
        assert (await submit(runtime, signal_envelope()))['state'] == 'exited'
        assert len(fake['intents']) == 2 and fake['intents'][-1]['reduce_only'] and fake['position'] == 0
    for reason, mark in [('stop', '98999'), ('target', '102001'), ('expiry', '100000'), ('watcher_dead', '100000')]:
        async with fixture() as (runtime, fake):
            assert (await submit(runtime, signal_envelope()))['state'] == 'filled'
            await runtime['events'].join()
            fake['mark'] = mark
            if reason == 'expiry':
                runtime['config']['policy']['expires_at'] = now_ms() // 1000 + 59
            if reason == 'watcher_dead':
                fake['fail_reconnect'] = True
            for websocket in tuple(fake['websockets']):
                await websocket.close()
            await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
            assert len(fake['intents']) == 2 and fake['position'] == 0 and fake['intents'][-1]['reduce_only']
            rows = read_records(runtime['config']['record_path'])
            assert [row['reason'] for row in rows if row['event'] == 'state' and row['state'] == 'exited'] == [reason]
            if reason != 'watcher_dead':
                restarted = next(index for index, row in enumerate(rows) if row['event'] == 'watcher_restarted')
                assert rows[restarted - 1]['event'] == 'reconcile'
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        for websocket in tuple(fake['websockets']):
            await websocket.close()
        await eventually(lambda: any(row['event'] == 'watcher_restarted' for row in read_records(runtime['config']['record_path'])))
        await runtime['events'].join()
        assert runtime['tickets']['sample']['state'] == 'filled' and len(fake['intents']) == 1
        await trade(fake, '98999')
        await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
    async with fixture() as (runtime, fake):
        fake['unfilled_entry'] = True
        envelope = signal_envelope()
        assert (await submit(runtime, envelope)) == {'state': 'not_filled'}
        ticket = runtime['tickets']['sample']
        assert ticket['done'].is_set() and not ticket['alive']
        await eventually(lambda: not fake['websockets'])
        assert (await cancel(runtime, 'sample')) == {'state': 'not_filled'}
        await recover(runtime)
        assert (await submit(runtime, envelope))['refused'] == 'already_acted'
        assert len(fake['intents']) == 1 and fake['position'] == 0
        rows = read_records(runtime['config']['record_path'])
        assert [row['state'] for row in rows if row['event'] == 'state'] == ['claimed', 'sent', 'not_filled']
        assert len([row for row in rows if row['event'] == 'review']) == 1


async def check_extra():
    async with fixture() as (runtime, fake):
        envelope = signal_envelope('entry-only')
        assert (await submit(runtime, envelope))['state'] == 'filled'
        await runtime['events'].join()
        ticket = runtime['tickets']['entry-only']
        with patch('desk.desk_executor.now_ms', return_value=ticket['signal']['reply_by'] + 1):
            queue_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
            await runtime['events'].join()
        assert ticket['state'] == 'filled' and len(fake['intents']) == 1
        await cancel(runtime, 'entry-only')
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        fake['partial_exit'] = True
        await trade(fake, '102001')
        await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
        assert len(fake['intents']) == 3 and fake['position'] == 0
        reviews = [row for row in read_records(runtime['config']['record_path']) if row['event'] == 'review']
        assert len(reviews) == 1 and not reviews[0]['stop_hit']
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        runtime['config']['policy']['expires_at'] = now_ms() // 1000 + 59
        await trade(fake, '100000')
        await eventually(lambda: runtime['tickets']['sample']['state'] == 'exited')
        assert fake['position'] == 0
    async with fixture() as (runtime, fake):
        path = Path(runtime['config']['record_path']).parent / 'config.json'
        path.write_text(json.dumps(runtime['config']))
        assert load_config(path)['risk_fraction'] == '0.01'


async def main():
    check_prompt_front_matter()
    print('PASS prompts: prompt front matter, one skill per seat and Executor-only writer', flush=True)
    check_intake()
    print('PASS intake: Desk Lead matches the demo intake skill and rejects conflicting operations', flush=True)
    check_sizer()
    print('PASS 1: sizer, null levels, wrong stop, leverage numbers, liquidation and mapping', flush=True)
    check_gate()
    await check_ticket_local()
    await check_ticket_preview_local()
    print('PASS ticket local: shared sizing/gate, nonresident refusal, no coordinator call, config and read-only review', flush=True)
    await check_failed_budget()
    print('PASS 2: gate, expiry, reduce-only, caps, failed signed budget and reply_by', flush=True)
    for name, function in [('3: watcher-first IOC, stop exit, record and Reviewer', check_happy),
                           ('4: prepare refusal and exactly one stale-nonce retry', check_prepare),
                           ('5: wrong-vault rearm refused before signing', check_rearm),
                           ('6: durable signal deduplication and key overwrite refusal', check_duplicate),
                           ('7: cancel CLI and cancel envelope remove entry and flatten', check_cancel),
                           ('7b: dead watcher flatten and ambiguous timeout reconciliation', check_recovery),
                           ('7d: exit 429 backoff/resend and entry 429 drop', check_rate_limit),
                           ('7e: canonical symbols, builder index, ambiguous and unmapped refusals', check_symbols),
                           ('7f: event expiry, reconnect reconciliation and terminal unfilled IOC', check_events)]:
        await function()
        print('PASS ' + name, flush=True)
    await check_consumer()
    print('PASS 7c: scratch Redis, both gated, one traded, restart cursor, duplicate no-op', flush=True)
    await check_extra()
    print('PASS extra: target, partial exit, policy expiry and config', flush=True)
    await check_execution(fixture)
    check_tree_style()
    print('PASS 8: file lengths and Python style, including prompts and skills', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
