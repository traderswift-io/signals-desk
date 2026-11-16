import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from .desk_executor import cancel, control_connection, recover, submit
from .desk_io import load_config
from .desk_keygen import generate_key
from .desk_record import now_ms, read_records, signed_today
from .desk_scenarios_checks import check_gate, check_intake, check_prompt_front_matter, check_tree_style, check_sizer, check_symbol_mapping, check_ticket_local, check_ticket_preview_local
from .desk_scenarios_fakes import signal_envelope
from .desk_scenarios_redis import check_consumer
from .desk_scenarios_execution import check_execution
from .desk_scenarios_resident import finish_exit, fixture, market_book, next_event


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
        ticket = runtime['tickets']['sample']
        await finish_exit(runtime, fake, ticket)
        assert fake['intents'][0]['kind'] == 'bracket'
        exits = [intent for intent in fake['intents'] if intent['kind'] == 'order']
        assert len(exits) == 2 and all(intent['reduce_only'] and intent['tif'] == 'Alo' for intent in exits)
        rows = read_records(runtime['config']['record_path'])
        reviews = [row for row in rows if row['event'] == 'review']
        assert len(reviews) == 1 and reviews[0]['stop_hit'] and reviews[0]['fill_price'] == '100000'
        assert all(row['hl_oracle_px'] == '100001' and row['binance_px'] == '100002' for row in rows if row['event'] == 'intent')
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
        generate_key(runtime['config'])
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
    fake['partial_entry'] = '0.004'
    await submit(runtime, signal_envelope('live'))
    await runtime['events'].join()
    ticket = runtime['tickets']['live']
    assert not ticket.get('entry_done') and ticket['position'] == Decimal('0.004')
    return ticket

async def check_cancel():
    for control in (True, False):
        async with fixture() as (runtime, fake):
            ticket = await seed_live_entry(runtime, fake)
            if control:
                output = []
                reader = SimpleNamespace(readline=AsyncMock(return_value=b'{"signal_id":"live"}'))
                writer = SimpleNamespace(write=output.append, drain=AsyncMock(), close=lambda: None, wait_closed=AsyncMock())
                await control_connection(runtime, reader, writer)
                assert json.loads(output[0])['state'] == 'filled'
            else:
                envelope = {'version': 1, 'payload': {'kind': 'cancel', 'signal_id': 'live', 'publisher': 'publisher-placeholder'}}
                assert (await submit(runtime, envelope))['state'] == 'filled'
            assert ticket['closing'] == 'withdrawn' and len(fake['intents']) == 1
            await next_event(runtime, ticket, market_book())
            assert fake['intents'][1]['kind'] == 'cancel' and fake['intents'][1]['oid'] == 42
            assert any(order.get('isTrigger') for order in fake['orders'])
            await finish_exit(runtime, fake, ticket, '100000')

async def check_recovery():
    async with fixture() as (runtime, fake):
        fake['timeout_entry'] = True
        runtime['config']['request_timeout_seconds'] = 0.03
        assert (await submit(runtime, signal_envelope()))['state'] == 'filled'
        assert len(fake['intents']) == 1 and fake['orders'][0]['isTrigger']
        assert any(row['type'] == 'frontendOpenOrders' for row in fake['info_requests'])

    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        before = len(fake['intents'])
        await recover(runtime)
        ticket = runtime['tickets']['sample']
        assert ticket['entry_done'] and ticket['backstop_px'] == '98000'
        assert fake['position'] == Decimal('0.01') and len(fake['intents']) == before
        assert any(order.get('isTrigger') for order in fake['orders'])
        await cancel(runtime, 'sample')
        await finish_exit(runtime, fake, ticket, '100000')

async def check_rate_limit():
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        fake['rate_exit'] = True
        await finish_exit(runtime, fake, runtime['tickets']['sample'])
        exits = [intent for intent in fake['intents'] if intent['kind'] == 'order']
        assert exits[0]['request_id'] == exits[1]['request_id']
        records = read_records(runtime['config']['record_path'])
        backoffs = [row for row in records if row['event'] == 'backoff']
        assert len(backoffs) == 1 and backoffs[0]['retry'] and backoffs[0]['seconds'] == '0.01'
        sends = [row for row in records if row['event'] == 'intent' and row['intent'].get('reduce_only')]
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
            await finish_exit(runtime, fake, runtime['tickets']['sample'])
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
    for channel in ('trades', 'userFills', 'l2Book'):
        async with fixture() as (runtime, fake):
            await submit(runtime, signal_envelope())
            await runtime['events'].join()
            runtime['config']['policy']['expires_at'] = now_ms() // 1000 + 59
            ticket = runtime['tickets']['sample']
            assert len(fake['intents']) == 1 and fake['position'] != 0
            event = market_book() if channel == 'l2Book' else {'channel': channel, 'data': []}
            await next_event(runtime, ticket, event)
            assert ticket['closing'] == 'expiry'
            await finish_exit(runtime, fake, ticket, '100000')
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        ticket = runtime['tickets']['sample']
        await next_event(runtime, ticket, {'channel': 'watcher_dead'})
        assert len(fake['intents']) == 1 and fake['orders']
        await next_event(runtime, ticket, {'channel': 'watcher_restarted'})
        assert len(fake['intents']) == 1
        assert any(row['event'] == 'watcher_restarted' for row in read_records(runtime['config']['record_path']))
    async with fixture() as (runtime, fake):
        fake['unfilled_entry'] = True
        envelope = signal_envelope()
        assert await submit(runtime, envelope) == {'state': 'not_filled'}
        ticket = runtime['tickets']['sample']
        assert ticket['done'].is_set() and not ticket['alive']
        await recover(runtime)
        assert (await submit(runtime, envelope))['refused'] == 'already_acted'
        assert len(fake['intents']) == 1

async def check_extra():
    async with fixture() as (runtime, fake):
        await submit(runtime, signal_envelope())
        await runtime['events'].join()
        ticket = runtime['tickets']['sample']
        with patch('desk.desk_executor.now_ms', return_value=ticket['signal']['reply_by'] + 1):
            await next_event(runtime, ticket, market_book())
        assert ticket['state'] == 'filled' and len(fake['intents']) == 1
        await finish_exit(runtime, fake, ticket, '102001')
        reviews = [row for row in read_records(runtime['config']['record_path']) if row['event'] == 'review']
        assert len(reviews) == 1 and not reviews[0]['stop_hit']
        path = Path(runtime['config']['record_path']).parent / 'config.json'
        path.write_text(json.dumps(runtime['config']))
        assert load_config(path)['exit_depth_frac'] == '0.03'

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
    for name, function in [('3: watcher-first bracket, sliced stop exit, record and Reviewer', check_happy),
                           ('4: prepare refusal and exactly one stale-nonce retry', check_prepare),
                           ('5: wrong-vault rearm refused before signing', check_rearm),
                           ('6: durable signal deduplication and key overwrite refusal', check_duplicate),
                           ('7: cancel control command and envelope remove entry and unwind', check_cancel),
                           ('7b: recovery retains backstop and never replays entry', check_recovery),
                           ('7d: exit 429 backoff/resend and entry 429 drop', check_rate_limit),
                           ('7e: canonical symbols, builder index, ambiguous and unmapped refusals', check_symbols),
                           ('7f: event expiry, reconnect reconciliation and terminal unfilled bracket', check_events)]:
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
