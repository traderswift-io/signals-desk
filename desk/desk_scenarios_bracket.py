import asyncio
import io
from json import dumps, loads
import tempfile
from contextlib import asynccontextmanager, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nacl.signing import SigningKey

from .desk_bracket import closed_price
from .desk_executor import create_runtime, main as executor_main, process_event, reconcile, recover, submit
from .desk_coordinator import run_round
from .desk_io import oracle_prices
from .desk_record import latest_tickets, now_ms, read_records, signed_today
from .desk_routine import maintain_routine
from .desk_scenarios_bracket_cleanup import check_leftover_leg_after_stop
from .desk_scenarios_fakes import configuration, coordinator_intent, coordinator_prepare, info, signal_envelope, ticker
from .desk_ticket import main as ticket_main
from .desk_wake import last_close, main as wake_main, wake_result
from .signer_delegate_client import bracket_fields


@asynccontextmanager
async def fixture(resident=False):
    with tempfile.TemporaryDirectory() as directory:
        config = configuration(Path(directory), '<fake>')
        config.update(resident=resident, symbols={'BTC': {'active_utc': [['00:00', '24:00']], 'days': 'mon-sun'}})
        fake = {'prepares': 0, 'nonce': 100, 'intents': [], 'events': [], 'position': Decimal(0),
                'orders': [], 'fills': [], 'websockets': []}

        @asynccontextmanager
        async def request(url, json=None, headers=None, timeout=None, params=None):
            incoming = SimpleNamespace(app={'state': fake}, json=AsyncMock(return_value=json), headers=headers, query=params)
            route = url.rsplit('/', 1)[-1]
            handler = {'info': info, 'prepare': coordinator_prepare, 'intent': coordinator_intent, 'ticker': ticker}[route]
            response = await handler(incoming)
            yield SimpleNamespace(status=response.status, headers=response.headers,
                                  json=AsyncMock(return_value=loads(response.body)), raise_for_status=lambda: None)

        session = SimpleNamespace(post=request, get=request)
        with patch('desk.signer_delegate_client.signing_key', return_value=SigningKey.generate()):
            runtime = create_runtime(config, session)
        runtime['close_ms'] = last_close(now_ms())
        yield runtime, fake


async def command_result(runtime, action, value=None):
    path = Path(runtime['config']['record_path']).parent / 'envelope.json'
    if value:
        path.write_text(dumps(value))

    @asynccontextmanager
    async def session_context():
        yield runtime['session']

    output = io.StringIO()
    with (patch('desk.desk_executor.load_config', return_value=runtime['config']),
          patch('desk.desk_ticket.load_config', return_value=runtime['config']),
          patch('desk.desk_executor.create_runtime', return_value=runtime),
          patch('desk.desk_executor.aiohttp.ClientSession', session_context), redirect_stdout(output)):
        arguments = SimpleNamespace(config='<fake>', action=action, envelope=str(path), input=str(path),
                                    close_ms=runtime.get('close_ms'))
        await (ticket_main(arguments) if action == 'gate' else executor_main(arguments))
    assert 'worker' not in runtime
    return loads(output.getvalue())


def envelope(identifier='bracket', short=False, target=True):
    value = signal_envelope(identifier)
    value['payload']['published_at'] = last_close(now_ms()) - 1
    if short:
        value['payload']['side'] = 'short'
        value['payload']['levels'].update(stop='101000', target='98000')
    if not target:
        value['payload']['levels']['target'] = None
    return value


def wire_action(fields):
    orders = []
    for name in ('entry', 'stop', 'take_profit'):
        if name not in fields:
            continue
        leg = fields[name]
        order_type = {'limit': {'tif': leg['tif']}} if name == 'entry' else {'trigger': {
            'isMarket': leg['is_market'], 'triggerPx': leg['trigger_px'], 'tpsl': leg['tpsl']}}
        orders.append({'a': leg['asset_index'], 'b': leg['is_buy'], 'p': leg['limit_px'],
                       's': leg['size'], 'r': leg['reduce_only'], 't': order_type})
    return {'type': 'order', 'orders': orders, 'grouping': 'normalTpsl'}


async def check_bracket(short=False, target=True):
    async with fixture() as (runtime, fake):
        value = envelope(short=short, target=target)
        result = await submit(runtime, value) if target else await command_result(runtime, 'run', value)
        assert result == {'state': 'sent'}
        assert fake['prepares'] == 1 and len(fake['intents']) == 1 and 'watcher_armed' not in fake['events']
        intent = fake['intents'][0]
        action = wire_action(intent)
        assert intent['kind'] == 'bracket' and action['type'] == 'order' and action['grouping'] == 'normalTpsl'
        assert len(action['orders']) == (3 if target else 2)
        entry = action['orders'][0]
        assert entry == {'a': 0, 'b': not short, 'p': '100000', 's': '0.01', 'r': False, 't': {'limit': {'tif': 'Gtc'}}}
        for index, leg in enumerate(action['orders'][1:]):
            assert list(leg) == ['a', 'b', 'p', 's', 'r', 't']
            trigger = leg['t']['trigger']
            assert list(trigger) == ['isMarket', 'triggerPx', 'tpsl']
            assert leg['r'] and leg['b'] == short and leg['s'] == entry['s'] and leg['a'] == entry['a']
            assert trigger['isMarket'] and trigger['tpsl'] == ('sl' if index == 0 else 'tp')
            assert (Decimal(trigger['triggerPx']) > Decimal(entry['p'])) == (short if index == 0 else not short)
            assert Decimal(leg['p']) == Decimal(trigger['triggerPx']) * (Decimal('1.005') if short else Decimal('0.995'))
        assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
        assert (await submit(runtime, envelope()))['refused'] == 'already_acted'
        await command_result(runtime, 'wake')
        assert len(fake['intents']) == 1 and not fake['websockets']


async def check_local_refusal(band=False):
    async with fixture() as (runtime, fake):
        value = envelope()
        if band:
            runtime['config']['bracket_slippage_bps'] = '5001'
        else:
            value['payload']['levels']['stop'] = '101000'
            fake['candle_close'] = '102000'
        result = await submit(runtime, value)
        assert result == {'refused': 'trigger_price_outside_band' if band else 'stop_on_wrong_side_of_entry'}, result
        assert not fake['intents'] and fake['prepares'] == 0
        assert read_records(runtime['config']['record_path'])[-1]['reason'] == result['refused']
    asset = {'asset': 'BTC', 'asset_index': 0}
    try:
        bracket_fields(asset, '1', '100', True, '49', '48')
    except ValueError as error:
        assert str(error) == 'trigger_price_outside_band'
    else:
        raise AssertionError('Out-of-band trigger accepted')


async def check_signer_refusal():
    reasons = ('unsupported_kind', 'trigger_not_reduce_only', 'bracket_leg_not_reduce_only',
               'bracket_leg_wrong_side', 'bracket_leg_larger_than_entry', 'bracket_leg_wrong_asset',
               'stop_on_wrong_side_of_entry', 'take_profit_on_wrong_side_of_entry', 'trigger_price_outside_band', 'bad_tpsl')
    for reason in reasons:
        async with fixture() as (runtime, fake):
            fake['bracket_refusal'] = reason
            assert await submit(runtime, envelope()) == {'state': 'bracket_refused', 'reason': reason}
            assert [intent['kind'] for intent in fake['intents']] == ['bracket']
            assert signed_today(runtime['config'], 'BTC') == 0
            rows = read_records(runtime['config']['record_path'])
            assert any(row['event'] == 'bracket_refused' and row['reason'] == reason for row in rows)
            assert rows[-1]['stop'] == 'bracket'


async def check_wake(case):
    async with fixture() as (runtime, fake):
        timestamp = int(datetime(2026, 11, 16, 12, 16, tzinfo=timezone.utc).timestamp() * 1000)
        if case == 'resting':
            fake['orders'] = [{'coin': 'BTC', 'oid': 42, 'reduceOnly': False, 'isTrigger': False}]
        if case == 'closed':
            fake['position'] = Decimal('0.01')
            runtime['config']['symbols']['BTC']['active_utc'] = [['09:00', '12:00']]
        result = await wake_result(runtime, timestamp)
        assert result['wake'] == (case == 'resting')
        assert result['reason'] == {'empty': 'no_work', 'resting': 'resting_entry', 'closed': 'outside_active_hours'}[case]
        assert result['next_close_utc'] == '2026-11-16T12:30:00+00:00'
        assert len(fake['info_requests']) == 2 and not fake['intents']
        if case == 'empty':
            assert (await wake_result(runtime, timestamp - 900000))['reason'] == 'hourly_signal_check'
            fake['orders'] = [{'coin': 'BTC', 'reduceOnly': True, 'isTrigger': True}]
            assert not (await wake_result(runtime, timestamp))['wake']
            fake['position'] = Decimal('0.01')
            assert (await wake_result(runtime, timestamp))['reason'] == 'open_position'
            runtime['config']['symbols']['BTC']['days'] = 'tue-fri'
            assert not (await wake_result(runtime, timestamp))['wake']


async def check_expired():
    for expiry, partial in ((True, False), (False, False), (True, True)):
        async with fixture() as (runtime, fake):
            await submit(runtime, envelope())
            ticket = runtime['tickets']['bracket']
            if expiry:
                ticket['signal']['reply_by'] = now_ms() - 1
            else:
                fake['candle_close'] = '98999'
            if partial:
                fake['position'] = Decimal('0.004')
            await maintain_routine(runtime, reconcile)
            kinds = [intent['kind'] for intent in fake['intents']]
            assert kinds[0] == 'bracket' and kinds[1:] == ['cancel'] * (1 if partial else 3)
            assert fake['intents'][1]['oid'] == 42
            assert fake['position'] == (Decimal('0.004') if partial else 0)
            assert len(fake['orders']) == (2 if partial else 0)
            if partial:
                await recover(runtime)
                assert fake['position'] == Decimal('0.004') and len(fake['intents']) == 2


async def check_resident():
    async with fixture(resident=True) as (runtime, fake):
        async def arm(runtime, ticket):
            ticket['alive'] = True
            fake['events'].append('watcher_armed')

        with patch('desk.desk_executor.arm', arm):
            assert await submit(runtime, envelope()) == {'state': 'filled'}
        assert fake['intents'][0]['kind'] == 'order' and fake['intents'][0]['tif'] == 'Ioc'
        assert fake['events'].index('watcher_armed') < fake['events'].index('intent')
        await process_event(runtime, runtime['tickets']['bracket'], {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '98999'}]})
        assert fake['position'] == 0 and len(fake['intents']) == 2 and fake['intents'][1]['reduce_only']
        assert read_records(runtime['config']['record_path'])[-1]['stop'] == 'software'


async def check_candle_guards():
    async with fixture() as (runtime, fake):
        value = envelope()
        value['payload']['published_at'] = last_close(now_ms()) + 1
        assert await submit(runtime, value) == {'refused': 'awaiting_candle_close'}
        value['payload']['published_at'] = last_close(now_ms()) - 1
        with patch('desk.desk_bracket.read_info', AsyncMock(return_value=[])):
            assert await submit(runtime, value) == {'refused': 'closed_candle_unavailable'}
        fake['candle_close'] = '98999'
        assert await submit(runtime, value) == {'refused': 'signal_invalidated'}
        fake['candle_close'] = '100000'
        assert await submit(runtime, value) == {'state': 'sent'}
        assert len(fake['intents']) == 1
    async with fixture() as (runtime, fake):
        runtime['config']['policy']['asset_caps'][0]['max_daily_size'] = '0.009'
        assert await submit(runtime, envelope()) == {'refused': 'over_max_daily_size'}
        assert not fake['intents'] and fake['prepares'] == 0
    output = io.StringIO()
    with patch('desk.desk_wake.Path.read_text', side_effect=OSError), redirect_stdout(output):
        await wake_main('<fake>')
    lines = output.getvalue().splitlines()
    assert len(lines) == 2 and loads(lines[0])['reason'] == 'wake_unavailable'
    assert lines[1].startswith('WAKE | no | wake_unavailable | next_close_utc=')


async def check_later_flat_cleanup():
    for refused_order in (None, 42, 43):
        async with fixture() as (runtime, fake):
            await submit(runtime, envelope())
            fake.update(position=Decimal('0.004'), candle_close='98999')

            async def cancel_once(runtime, ticket, fields):
                if fields.get('oid') == refused_order and not fake.get('cancel_refused'):
                    fake['cancel_refused'] = True
                    return {'signed': False, 'reason': 'cancel_refused'}
                return await run_round(runtime, ticket, fields)

            with patch('desk.desk_routine.run_round', cancel_once):
                result = await maintain_routine(runtime, reconcile)
                if refused_order == 42:
                    assert result['results'][0]['refused'] == 'cancel_refused'
                    assert not runtime['tickets']['bracket'].get('entry_done')
                    fake['candle_close'] = '100000'
                    runtime['tickets'].clear()
                    await recover(runtime)
                    await maintain_routine(runtime, reconcile)
                assert [order['oid'] for order in fake['orders']] == [43, 44]
                assert list(latest_tickets(runtime['config']).values())[0]['entry_done']
                runtime['tickets'].clear()
                await recover(runtime)
                assert runtime['tickets']['bracket']['entry_done']
                fake.update(position=Decimal(0), candle_close='100000')
                result = await maintain_routine(runtime, reconcile)
                if refused_order == 43:
                    assert result['results'][0]['refused'] == 'cancel_refused'
                    assert [order['oid'] for order in fake['orders']] == [43]
                    assert runtime['tickets']['bracket']['state'] == 'filled'
                    assert await submit(runtime, envelope('blocked')) == {'refused': 'account_busy'}
                    runtime['tickets'].clear()
                    await recover(runtime)
                    await maintain_routine(runtime, reconcile)
                assert not fake['orders'] and runtime['tickets']['bracket']['state'] == 'exited'
                assert [intent['oid'] for intent in fake['intents'] if intent['kind'] == 'cancel'] == (
                    [42, 44, 43] if refused_order == 43 else [42, 43, 44])
                cancellations = [row for row in read_records(runtime['config']['record_path']) if row['event'] == 'cancel_result']
                assert any(row['outcome'].get('reason') == 'cancel_refused' for row in cancellations) == bool(refused_order)
                await maintain_routine(runtime, reconcile)
                assert await submit(runtime, envelope('next')) == {'state': 'sent'}


async def check_wake_close_handoff():
    timestamp = int(datetime(2026, 11, 16, 12, 16, tzinfo=timezone.utc).timestamp() * 1000)
    with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
        async with fixture() as (runtime, fake):
            fake['orders'] = [{'coin': 'BTC', 'oid': 42}]

            @asynccontextmanager
            async def wake_session():
                yield runtime['session']

            output = io.StringIO()
            with (patch('desk.desk_wake.Path.read_text', return_value=dumps(runtime['config'])),
                  patch('desk.desk_wake.aiohttp.ClientSession', wake_session), redirect_stdout(output)):
                await wake_main('<fake>')
            printed = loads(output.getvalue().splitlines()[0])
            assert printed['last_close_utc'] == '2026-11-16T12:15:00+00:00'
            runtime['close_ms'] = printed['close_ms']
            fake['orders'] = []
            value = envelope()
            value['payload']['reply_by'] = timestamp + 3600000
            assert (await command_result(runtime, 'gate', value))['status'] == 'PASS'
            assert await command_result(runtime, 'run', value) == {'state': 'sent'}
            await command_result(runtime, 'wake')
            candles = [row['req'] for row in fake['info_requests'] if row['type'] == 'candleSnapshot']
            assert candles and all(row['endTime'] == printed['close_ms'] for row in candles)
            clock.return_value = (timestamp + 900000) * 1000000
            reads = len(fake['info_requests'])
            assert (await command_result(runtime, 'gate', value))['refused'] == 'stale_wake_close'
            assert await command_result(runtime, 'run', value) == {'refused': 'stale_wake_close'}
            assert await submit(runtime, value) == {'refused': 'stale_wake_close'}
            assert await command_result(runtime, 'wake') == {'refused': 'stale_wake_close'}
            assert not any(row['type'] == 'candleSnapshot' for row in fake['info_requests'][reads:])
            assert len(fake['intents']) == 1
            for close in (None, printed['close_ms'] + 1, printed['close_ms'] + 1800000):
                runtime['close_ms'] = close
                assert await submit(runtime, value) == {'refused': 'stale_wake_close'}
        clock.return_value = timestamp * 1000000
        async with fixture() as (runtime, fake):
            async def delayed_prices(*arguments):
                result = await oracle_prices(*arguments)
                clock.return_value = (timestamp + 900000) * 1000000
                return result

            value = envelope()
            value['payload']['reply_by'] = timestamp + 3600000
            with patch('desk.desk_coordinator.oracle_prices', delayed_prices):
                result = await submit(runtime, value)
                assert result == {'state': 'bracket_refused', 'reason': 'stale_wake_close'}, result
            assert fake['prepares'] == 1 and not fake['intents']


async def check_maintenance_close_boundary():
    timestamp = int(datetime(2026, 11, 16, 12, 29, 59, tzinfo=timezone.utc).timestamp() * 1000)
    for price in ('98999', '100000'):
        with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
            async with fixture() as (runtime, fake):
                value = envelope()
                value['payload']['reply_by'] = timestamp + 3600000
                assert await submit(runtime, value) == {'state': 'sent'}
                close = runtime['close_ms']
                assert datetime.fromtimestamp(close / 1000, timezone.utc).isoformat() == '2026-11-16T12:15:00+00:00'
                fake['candle_close'] = price
                records = read_records(runtime['config']['record_path'])
                prepares = fake['prepares']

                async def delayed_candle(runtime, signal, close):
                    result = await closed_price(runtime, signal, close)
                    clock.return_value = (timestamp + 2000) * 1000000
                    return result

                with patch('desk.desk_routine.closed_price', delayed_candle):
                    assert await command_result(runtime, 'wake') == {'refused': 'stale_wake_close'}
                assert runtime['close_ms'] == close
                assert fake['prepares'] == prepares and len(fake['intents']) == 1
                assert [order['oid'] for order in fake['orders']] == [42, 43, 44]
                ticket = runtime['tickets']['bracket']
                assert ticket['state'] == 'sent' and not ticket.get('entry_cancel_reason') and not ticket.get('entry_done')
                assert all(row['event'] == 'reconcile' for row in read_records(runtime['config']['record_path'])[len(records):])
                candles = [row['req'] for row in fake['info_requests'] if row['type'] == 'candleSnapshot']
                assert all(row['endTime'] == close for row in candles)


async def main():
    await check_candle_guards()
    cases = [('long bracket: three ordered legs', lambda: check_bracket()),
             ('short bracket: mirrored sides and bounds', lambda: check_bracket(short=True)),
             ('no target: two ordered legs', lambda: check_bracket(target=False)),
             ('wrong stop: local refusal, nothing signed', lambda: check_local_refusal()),
             ('band over 50%: local refusal, nothing signed', lambda: check_local_refusal(band=True)),
             ('signer refusal: bracket_refused, no plain order', check_signer_refusal),
             ('flat with no work: no wake', lambda: check_wake('empty')),
             ('resting entry in active hours: wake', lambda: check_wake('resting')),
             ('position outside active hours: no wake', lambda: check_wake('closed')),
             ('expired or invalidated entry: cancel, retain partial-fill protection', check_expired),
             ('resident IOC and software exit unchanged', check_resident),
             ('later flat cleanup: persisted entry cancel and refused-leg retry', check_later_flat_cleanup),
             ('wake close handoff: stale submission refused', check_wake_close_handoff),
             ('maintenance close boundary: stale candle refuses without cancellation', check_maintenance_close_boundary),
             ('leftover_leg_after_stop', lambda: check_leftover_leg_after_stop(fixture, envelope))]
    for index, (label, check) in enumerate(cases, 1):
        await check()
        print('PASS bracket ' + str(index) + ': ' + label, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
