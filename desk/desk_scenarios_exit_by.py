import asyncio
from decimal import Decimal
from unittest.mock import patch

from .desk_executor import reconcile, recover, submit
from .desk_record import now_ms, read_records
from .desk_routine import maintain_routine
from .desk_scenarios_bracket import envelope, fixture
from .desk_scenarios_fakes import fill_fake, submit_verified_fixture
from .desk_scenarios_resident import fixture as resident_fixture, market_book, next_event
from .desk_signal import parse_signal


async def check_no_exit_by():
    for explicit_null in (False, True):
        async with resident_fixture() as (runtime, fake):
            value = envelope('without-time-stop')
            if explicit_null:
                value['payload']['exit_by'] = None
            assert await submit(runtime, value) == {'state': 'filled'}
            ticket = runtime['tickets']['without-time-stop']
            await next_event(runtime, ticket, market_book())
            await next_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
            assert len(fake['intents']) == 1 and not ticket.get('closing')
            assert not any(row['event'] == 'time_stop' for row in read_records(runtime['config']['record_path']))


async def check_resident_time_stop():
    for channel in ('trades', 'watcher_restarted'):
        async with resident_fixture() as (runtime, fake):
            value = envelope('time-stop')
            deadline = now_ms() + 10000
            value['payload']['exit_by'] = deadline
            assert await submit(runtime, value) == {'state': 'filled'}
            ticket = runtime['tickets']['time-stop']
            await next_event(runtime, ticket, market_book())
            event = {'channel': channel, 'data': [{'coin': 'BTC', 'px': '100000'}]}
            with patch('desk.desk_executor.now_ms', return_value=deadline - 1):
                await next_event(runtime, ticket, event)
            assert len(fake['intents']) == 1 and not ticket.get('closing')
            with patch('desk.desk_executor.now_ms', return_value=deadline):
                await next_event(runtime, ticket, {'channel': 'l2Book', 'data': market_book()['data']})
                assert not ticket.get('closing')
                await next_event(runtime, ticket, event)
                assert fake['intents'][-1]['kind'] == 'order' and fake['intents'][-1]['reduce_only']
                assert fake['intents'][-1]['tif'] == 'Alo' and ticket['closing'] == 'time_stop'
                await next_event(runtime, ticket, event)
                await recover(runtime)
                ticket = runtime['tickets']['time-stop']
                await next_event(runtime, ticket, event)
            records = read_records(runtime['config']['record_path'])
            assert sum(row['event'] == 'time_stop' for row in records) == 1
            assert any(row['event'] == 'exit_result' and row['reason'] == 'time_stop' for row in records)
            assert ticket['signal']['exit_by'] == deadline


async def check_exit_by_passed():
    for resident in (True, False):
        async with fixture(resident=resident) as (runtime, fake):
            value = envelope('past-time-stop')
            value['payload']['exit_by'] = now_ms()
            result = await submit_verified_fixture(runtime, value)
            assert result == {'refused': 'exit_by_passed'}
            assert fake['prepares'] == 0 and not fake['intents']


async def routine_time_stop(filled):
    async with fixture() as (runtime, fake):
        value = envelope('routine-time-stop')
        deadline = now_ms() + 10000
        value['payload']['exit_by'] = deadline
        assert await submit_verified_fixture(runtime, value) == {'state': 'sent'}
        ticket = runtime['tickets']['routine-time-stop']
        if filled:
            fill_fake(fake, fake['intents'][0]['entry'])
            fake['orders'] = [order for order in fake['orders'] if order['oid'] != 42]
        with patch('desk.desk_routine.now_ms', return_value=deadline - 1):
            await maintain_routine(runtime, reconcile)
        assert len(fake['intents']) == 1
        with patch('desk.desk_routine.now_ms', return_value=deadline):
            result = await maintain_routine(runtime, reconcile)
            assert result['results'][0]['reason'] == 'time_stop'
            await maintain_routine(runtime, reconcile)
        assert ticket['state'] == 'exited' and fake['position'] == 0 and not fake['orders']
        actions = fake['intents'][1:]
        if filled:
            assert actions[0]['kind'] == 'order' and actions[0]['reduce_only']
            assert Decimal(actions[0]['limit_px']) == Decimal('99500')
            assert all(action['kind'] == 'cancel' for action in actions[1:])
        else:
            assert all(action['kind'] == 'cancel' for action in actions)
            assert actions[0]['oid'] == 42
        records = read_records(runtime['config']['record_path'])
        assert sum(row['event'] == 'time_stop' for row in records) == 1


async def check_routine_position_time_stop():
    await routine_time_stop(True)


async def check_routine_entry_time_stop():
    await routine_time_stop(False)


async def check_invalid_exit_by():
    async with fixture(resident=True) as (runtime, fake):
        value = envelope('invalid-time-stop')
        published = value['payload']['published_at']
        for invalid in ('123', True, False, published, published - 1, Decimal(published + 1), [], {}):
            value['payload']['exit_by'] = invalid
            assert await submit(runtime, value) == {'refused': 'invalid_exit_by'}
        assert fake['prepares'] == 0 and not fake['intents']
        value['payload']['exit_by'] = now_ms() + 10000
        for published in (None, True, '1'):
            value['payload']['published_at'] = published
            try:
                parse_signal(value, runtime['meta'], now_ms())
            except ValueError as error:
                assert str(error) == 'invalid_exit_by'
            else:
                raise AssertionError('Invalid publication time accepted')


async def check_resident_resting_entry_expiry():
    for expiry in ('time_stop', 'expiry'):
        async with resident_fixture() as (runtime, fake):
            value = envelope('resting-expiry')
            deadline = (now_ms() // 1000 + 10) * 1000
            if expiry == 'time_stop':
                value['payload']['exit_by'] = deadline
            fake['resident'] = False
            assert await submit(runtime, value) == {'state': 'sent', 'reason': 'reconciliation_hold'}
            ticket = runtime['tickets']['resting-expiry']
            assert not ticket['position'] and fake['intents'][0]['entry']['tif'] == 'Gtc'
            assert {order['oid'] for order in fake['orders']} == {42, 43}
            if expiry == 'expiry':
                runtime['config']['policy']['expires_at'] = deadline // 1000 + 60
            runtime['config']['fixture_clock']['now_ms'] = deadline - 1
            await next_event(runtime, ticket, market_book())
            assert len(fake['intents']) == 1 and not ticket.get('closing')
            runtime['config']['fixture_clock']['now_ms'] = deadline
            if expiry == 'time_stop':
                await next_event(runtime, ticket, market_book())
                assert len(fake['intents']) == 1 and not ticket.get('closing')
            await next_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
            assert ticket['closing'] == expiry and ticket['entry_done']
            assert fake['intents'][-1]['kind'] == 'cancel' and fake['intents'][-1]['oid'] == 42
            assert len(fake['orders']) == 1 and fake['orders'][0]['isTrigger']
            await recover(runtime)
            ticket = runtime['tickets']['resting-expiry']
            await next_event(runtime, ticket, {'channel': 'userFills'})
            assert ticket['state'] == 'exited' and not fake['orders'] and fake['position'] == 0
            assert [row['kind'] for row in fake['intents'][1:]] == ['cancel', 'cancel']
            records = read_records(runtime['config']['record_path'])
            assert sum(row['event'] == 'time_stop' for row in records) == (expiry == 'time_stop')


async def main():
    cases = [check_no_exit_by, check_resident_time_stop, check_exit_by_passed,
             check_routine_position_time_stop, check_routine_entry_time_stop, check_invalid_exit_by,
             check_resident_resting_entry_expiry]
    for check in cases:
        await check()
        print('PASS exit_by: ' + check.__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
