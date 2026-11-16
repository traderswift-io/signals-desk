import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from .desk_executor import cancel, process_event, reconcile, recover, submit
from .desk_record import journal_guard, now_ms, read_records
from .desk_scenarios_bracket import envelope, fixture
from .desk_scenarios_exit_custody import write_rows
from .desk_scenarios_fakes import fill_passive, submit_verified_fixture
from .desk_scenarios_legacy_fixture import CAPTURED_HISTORIES
from .desk_scenarios_resident import market_book


async def arm_fixture(runtime, ticket):
    ticket['alive'] = True
    runtime['fixture_events'].append('watcher_armed')


async def check_legacy_resident():
    snapshot = CAPTURED_HISTORIES['resident']
    for partial in (False, True):
        async with fixture(resident=True) as (runtime, fake):
            path = Path(runtime['config']['record_path'])
            write_rows(path, snapshot['rows'])
            path.chmod(0o600)
            fake.update(deepcopy(snapshot['fake']))
            fake.update(position=Decimal(fake['position']), nonce=1000, partial_exit=partial)
            runtime['fixture_events'] = fake['events']
            timestamp = now_ms()
            with patch('time.time_ns', return_value=timestamp * 1000000) as clock, patch('desk.desk_executor.arm', arm_fixture):
                await recover(runtime)
                ticket = runtime['tickets']['parent-resident']
                assert len(fake['intents']) == 1
                action = fake['intents'][0]
                assert action['kind'] == 'order' and action['reduce_only'] and action['tif'] == 'Ioc'
                assert Decimal(action['size']) == Decimal(snapshot['fake']['position'])
                if partial:
                    assert fake['position'] == Decimal(snapshot['fake']['position']) / 2
                    runtime['tickets'].clear()
                    await recover(runtime)
                    ticket = runtime['tickets']['parent-resident']
                    assert len(fake['intents']) == 1
                    clock.return_value = (timestamp + 10000) * 1000000
                    await process_event(runtime, ticket, market_book('98999'))
                    assert len(fake['intents']) == 2
                assert not fake['position'] and ticket['state'] == 'exited'
                assert all(action['reduce_only'] for action in fake['intents'])
                read_records(path)


async def check_legacy_routine():
    snapshot = CAPTURED_HISTORIES['routine']
    async with fixture() as (runtime, fake):
        path = Path(runtime['config']['record_path'])
        write_rows(path, snapshot['rows'])
        path.chmod(0o600)
        fake.update(deepcopy(snapshot['fake']))
        fake.update(position=Decimal(fake['position']), nonce=1000)
        read_records(path)
        await recover(runtime)
        assert not fake['intents']
        assert await cancel(runtime, 'parent-routine') == {'state': 'exited'}
        assert [action['kind'] for action in fake['intents']] == ['cancel', 'cancel', 'order']
        assert [action['oid'] for action in fake['intents'][:2]] == [43, 44]
        assert fake['intents'][-1]['reduce_only'] and fake['intents'][-1]['tif'] == 'Ioc'
        assert not fake['position'] and not fake['orders']
        read_records(path)


async def check_legacy_protection_retained():
    async with fixture(resident=True) as (runtime, fake):
        snapshot = CAPTURED_HISTORIES['resident']
        path = Path(runtime['config']['record_path'])
        write_rows(path, snapshot['rows'])
        path.chmod(0o600)
        fake.update(deepcopy(snapshot['fake']))
        stop = {'coin': 'BTC', 'oid': 43, 'sz': '0.01', 'reduceOnly': True, 'isTrigger': True}
        fake.update(position=Decimal(fake['position']), nonce=1000, orders=[stop], partial_exit=True)
        runtime['fixture_events'] = fake['events']
        timestamp = now_ms()
        with patch('time.time_ns', return_value=timestamp * 1000000) as clock, patch('desk.desk_executor.arm', arm_fixture):
            await recover(runtime)
            ticket = runtime['tickets']['parent-resident']
            assert fake['position'] and fake['orders'] == [stop]
            assert len(fake['intents']) == 1 and fake['intents'][0]['reduce_only']
            clock.return_value = (timestamp + 10000) * 1000000
            await process_event(runtime, ticket, market_book('98999'))
            assert not fake['position'] and fake['orders'] == [stop]
            clock.return_value = (timestamp + 20000) * 1000000
            await process_event(runtime, ticket, {'channel': 'userFills'})
            assert ticket['state'] == 'exited' and not fake['orders']
            assert [row['kind'] for row in fake['intents']] == ['order', 'order', 'cancel']
            read_records(path)


async def check_routine_writer_versions():
    for historical in (True, False):
        async with fixture() as (runtime, fake):
            path = Path(runtime['config']['record_path'])
            if historical:
                rows = deepcopy(CAPTURED_HISTORIES['routine']['rows'])
            else:
                await submit_verified_fixture(runtime, envelope())
                fake['position'] = Decimal('0.01')
                fake['orders'] = [order for order in fake['orders'] if order['oid'] != 42]
                await reconcile(runtime, runtime['tickets']['bracket'])
                rows = read_records(path)
            write_rows(path, rows)
            path.chmod(0o600)
            read_records(path)
            latest = max(index for index, row in enumerate(rows) if row['event'] == 'state')
            for value in (None, '98001'):
                changed = deepcopy(rows)
                changed[latest]['backstop_px'] = value
                write_rows(path, changed)
                before, prepares = len(fake['intents']), fake['prepares']
                with journal_guard(runtime['config']) as refusal:
                    await recover(runtime)
                assert refusal == {'refused': 'anchor_not_verified'}
                assert len(fake['intents']) == before and fake['prepares'] == prepares
            if not historical:
                changed = deepcopy(rows)
                del changed[latest]['backstop_px']
                write_rows(path, changed)
                with journal_guard(runtime['config']) as refusal:
                    read_records(path)
                assert refusal == {'refused': 'anchor_not_verified'}
                write_rows(path, rows)
                await recover(runtime)
                before = len(fake['intents'])
                assert await cancel(runtime, 'bracket') == {'state': 'exited'}
                assert [row['kind'] for row in fake['intents'][before:]] == ['cancel', 'cancel', 'order']
                assert not fake['position'] and not fake['orders']
                read_records(path)


async def check_refused_resize_progress():
    for low_budget in (False, True):
        for restart in (False, True):
            timestamp = now_ms()
            with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
                async with fixture(resident=True) as (runtime, fake):
                    runtime['fixture_events'] = fake['events']
                    with patch('desk.desk_executor.arm', arm_fixture):
                        await submit(runtime, envelope('resize-progress'))
                        ticket = runtime['tickets']['resize-progress']
                        await process_event(runtime, ticket, market_book('102001'))
                        fill_passive(fake)
                        assert fake['position'] == Decimal('0.00400')
                        old_stop = deepcopy(fake['orders'][0])
                        fake['trigger_refused'] = 'trigger_not_reduce_only' if restart else True
                        fake['requests_used'] = 10000 if low_budget else 0
                        before = len(fake['intents'])
                        clock.return_value = (timestamp + 10000) * 1000000
                        await process_event(runtime, ticket, {'channel': 'userFills'})
                        assert old_stop in fake['orders']
                        if restart:
                            runtime['tickets'].clear()
                            await recover(runtime)
                            ticket = runtime['tickets']['resize-progress']
                        for elapsed in (20000, 30000, 40000):
                            clock.return_value = (timestamp + elapsed) * 1000000
                            await process_event(runtime, ticket, market_book('98550'))
                        actions = fake['intents'][before:]
                        assert any(row['kind'] == 'order' and row['tif'] == 'Ioc' for row in actions)
                        assert all(row['reduce_only'] for row in actions if row['kind'] != 'cancel')
                        assert not fake['position']
                        rows = read_records(runtime['config']['record_path'])
                        assert sum(row['event'] == 'backstop_replace_refused' for row in rows) == 1


async def check_four_resize_refusals():
    async with fixture(resident=True) as (runtime, fake):
        snapshot = CAPTURED_HISTORIES['resident']
        path = Path(runtime['config']['record_path'])
        write_rows(path, snapshot['rows'])
        path.chmod(0o600)
        fake.update(deepcopy(snapshot['fake']))
        fake.update(position=Decimal('0.004'), nonce=1000, orders=[{
            'coin': 'BTC', 'oid': 43, 'sz': '0.01', 'reduceOnly': True,
            'isTrigger': True, 'orderType': 'Stop Market', 'triggerPx': '98000'}])
        runtime['fixture_events'] = fake['events']
        with patch('desk.desk_executor.arm', arm_fixture):
            await recover(runtime)
        baseline_remaining = fake['position']
        assert baseline_remaining == 0
        assert len(fake['intents']) == 1 and fake['intents'][0]['reduce_only']
        assert fake['intents'][0]['tif'] == 'Ioc' and Decimal(fake['intents'][0]['size']) == Decimal('0.004')
        read_records(path)
    timestamp = now_ms()
    with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
        async with fixture(resident=True) as (runtime, fake):
            runtime['fixture_events'] = fake['events']
            with patch('desk.desk_executor.arm', arm_fixture):
                await submit(runtime, envelope('resize-refusals'))
            ticket = runtime['tickets']['resize-refusals']
            await process_event(runtime, ticket, market_book('102001'))
            fill_passive(fake)
            old_stop = deepcopy(fake['orders'][0])
            ticket.pop('book')
            fake['trigger_refused'] = True
            before = len(fake['intents'])
            for elapsed in (10000, 20000, 30000, 40000):
                clock.return_value = (timestamp + elapsed) * 1000000
                await process_event(runtime, ticket, {'channel': 'userFills'})
                assert old_stop in fake['orders']
            assert [row['kind'] for row in fake['intents'][before:]] == ['trigger_order'] * 4
            clock.return_value = (timestamp + 50000) * 1000000
            await process_event(runtime, ticket, market_book('98550'))
            assert fake['intents'][-1]['kind'] == 'order' and fake['intents'][-1]['reduce_only']
            assert fake['intents'][-1]['tif'] == 'Ioc' and fake['position'] == baseline_remaining
            assert old_stop in fake['orders']
            rows = read_records(runtime['config']['record_path'])
            assert sum(row['event'] == 'backstop_replace_refused' for row in rows) == 1


async def main():
    for check in (check_legacy_resident, check_legacy_routine, check_legacy_protection_retained, check_routine_writer_versions,
                  check_refused_resize_progress, check_four_resize_refusals):
        await check()
        print('PASS exit upgrade: ' + check.__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
