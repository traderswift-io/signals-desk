import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from json import dumps
from pathlib import Path
from unittest.mock import AsyncMock, patch

from .desk_executor import process_event, recover, submit
from .desk_exit_driver import drive_exit
from .desk_record import journal_guard, latest_tickets, now_ms, read_records
from .desk_scenarios_bracket import envelope, fixture
from .desk_scenarios_fakes import fill_passive
from .desk_scenarios_exit_fixture import driver_fixture
from .desk_scenarios_history import integer_boundaries
from .desk_scenarios_resident import market_book


@asynccontextmanager
async def custody_fixture(price='98999'):
    timestamp = now_ms()
    with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
        async with fixture(resident=True) as (runtime, fake):
            async def arm(runtime, ticket):
                ticket['alive'] = True

            with patch('desk.desk_executor.arm', arm):
                value = envelope('exit-custody')
                value['payload']['exit_by'] = timestamp + 10000
                assert await submit(runtime, value) == {'state': 'filled'}
                await process_event(runtime, runtime['tickets']['exit-custody'], market_book(price))
                yield runtime, fake, clock, timestamp


def write_rows(path, rows):
    path.write_text(''.join(dumps(row) + '\n' for row in rows))


async def replay(runtime, fake, clock, timestamp, rows, initial_fake):
    path = Path(runtime['config']['record_path'])
    write_rows(path, rows)
    runtime['tickets'].clear()
    runtime['request_times'] = []
    fake.clear()
    fake.update(deepcopy(initial_fake))
    clock.return_value = (timestamp + 60000) * 1000000
    before = len(fake['intents'])
    prepared = fake['prepares']
    with journal_guard(runtime['config']) as refusal:
        read_records(path)
        await recover(runtime)
    if refusal:
        assert refusal == {'refused': 'anchor_not_verified'}
        assert await submit(runtime, envelope('blocked')) == refusal
        assert len(fake['intents']) == before and fake['prepares'] == prepared
        return None
    ticket = runtime['tickets'].get('exit-custody')
    if ticket is None:
        restored = next(iter(latest_tickets(runtime['config']).values()))
        assert restored['state'] in ('exited', 'not_filled', 'refused')
        assert len(fake['intents']) == before and fake['prepares'] == prepared
        return [], [], restored['backstop_px'], restored['closing'], restored['stop_hit'], fake['position']
    await process_event(runtime, ticket, market_book())
    book_actions = [row['kind'] for row in fake['intents'][before:]]
    await process_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
    actions = [{name: row[name] for name in ('kind', 'size', 'limit_px', 'reduce_only', 'tif', 'oid') if name in row}
               for row in fake['intents'][before:]]
    read_records(path)
    return book_actions, actions, ticket['backstop_px'], ticket['closing'], ticket['stop_hit'], fake['position']


async def check_exit_state_regressions():
    async with custody_fixture() as (runtime, fake, clock, timestamp):
        rows = read_records(runtime['config']['record_path'])
        initial_fake = deepcopy(fake)
        latest = max(index for index, row in enumerate(rows) if row['event'] == 'state')
        assert rows[latest]['closing'] == 'stop' and rows[latest]['stop_hit']
        clean = await replay(runtime, fake, clock, timestamp, rows, initial_fake)
        assert clean[0] == ['cancel', 'order'] and clean[3] == 'time_stop'
        for field, replacement in (('closing', None), ('backstop_px', None), ('stop_hit', False),
                                   ('closing', 'target'), ('closing', 'unknown')):
            changed = deepcopy(rows)
            changed[latest][field] = replacement
            assert await replay(runtime, fake, clock, timestamp, changed, initial_fake) is None


async def check_exit_state_field_matrix():
    count = 0
    for stage in ('stop', 'target', 'time_stop', 'giveback'):
        async with custody_fixture('98999' if stage in ('stop', 'time_stop') else '102001') as (runtime, fake, clock, timestamp):
            ticket = runtime['tickets']['exit-custody']
            clock.return_value = (timestamp + 20000) * 1000000
            if stage == 'time_stop':
                await process_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
            elif stage == 'giveback':
                await process_event(runtime, ticket, market_book())
            rows = read_records(runtime['config']['record_path'])
            initial_fake = deepcopy(fake)
            clean = await replay(runtime, fake, clock, timestamp, rows, initial_fake)
            assert clean is not None
            for index, row in enumerate(rows):
                if row['event'] != 'state':
                    continue
                for field in ('backstop_px', 'closing', 'stop_hit'):
                    if row.get(field) in (None, False):
                        continue
                    for replacement in ((None, False) if field == 'stop_hit' else (None,)):
                        changed = deepcopy(rows)
                        changed[index][field] = replacement
                        observed = await replay(runtime, fake, clock, timestamp, changed, initial_fake)
                        assert observed is None or observed == clean, (index, field, replacement, observed, clean)
                        count += 1
    assert count > 50
    print('PASS exit custody mutations: ' + str(count), flush=True)


async def check_target_giveback_recovery():
    async with custody_fixture('102001') as (runtime, fake, clock, timestamp):
        ticket = runtime['tickets']['exit-custody']
        clock.return_value = (timestamp + 20000) * 1000000
        await process_event(runtime, ticket, market_book())
        assert ticket['closing'] is None and not ticket.get('stop_hit')
        path = Path(runtime['config']['record_path'])
        rows = read_records(path)
        assert rows[-1]['reason'] == 'target_giveback'
        runtime['tickets'].clear()
        await recover(runtime)
        ticket = runtime['tickets']['exit-custody']
        assert ticket['closing'] is None and ticket['backstop_px'] == '98000'
        before = len(fake['intents'])
        clock.return_value = (timestamp + 40000) * 1000000
        await process_event(runtime, ticket, market_book('102001'))
        assert ticket['closing'] == 'target' and len(fake['intents']) == before + 1
        read_records(path)
        rows[-1].pop('reason')
        write_rows(path, rows)
        with journal_guard(runtime['config']) as refusal:
            read_records(path)
        assert refusal == {'refused': 'anchor_not_verified'}


async def check_exit_evidence_recovery():
    async with custody_fixture() as (runtime, fake, clock, timestamp):
        ticket = runtime['tickets']['exit-custody']
        clock.return_value = (timestamp + 20000) * 1000000
        await process_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
        path = Path(runtime['config']['record_path'])
        rows = read_records(path)
        index = next(index for index, row in enumerate(rows) if row['event'] == 'time_stop')
        write_rows(path, rows[:index + 1])
        runtime['tickets'].clear()
        await recover(runtime)
        ticket = runtime['tickets']['exit-custody']
        assert ticket['closing'] == 'time_stop' and ticket['stop_hit'] and ticket['backstop_px'] == '98000'
        before = len(fake['intents'])
        await process_event(runtime, ticket, market_book())
        assert [row['kind'] for row in fake['intents'][before:]] == ['cancel', 'order']
        rows = read_records(path)
        assert sum(row['event'] == 'time_stop' for row in rows) == 1
        latest = max(index for index, row in enumerate(rows) if row['event'] == 'state')
        for closing in (None, 'stop', 'target', 'expiry', 'withdrawn'):
            changed = deepcopy(rows)
            changed[latest]['closing'] = closing
            write_rows(path, changed)
            with journal_guard(runtime['config']) as refusal:
                read_records(path)
            assert refusal == {'refused': 'anchor_not_verified'}


async def check_exit_pacing_regressions():
    async with custody_fixture() as (runtime, fake, clock, timestamp):
        rows = read_records(runtime['config']['record_path'])
        initial_fake = deepcopy(fake)
        latest = max(index for index, row in enumerate(rows) if row['event'] == 'state')
        clean = await replay(runtime, fake, clock, timestamp, rows, initial_fake)
        assert clean[0] == ['cancel', 'order'] and clean[3] == 'time_stop'
        for field, replacement in (('exit_action_cost', 1000000), ('last_exit_action_ms', timestamp + 3600000)):
            changed = deepcopy(rows)
            changed[latest][field] = replacement
            assert await replay(runtime, fake, clock, timestamp, changed, initial_fake) is None


async def check_exit_integer_matrix():
    count, fields = 0, set()
    for stage in ('stop', 'target', 'time_stop', 'giveback', 'resize', 'flat', 'requote'):
        async with custody_fixture('102001' if stage in ('target', 'giveback') else '98999') as (runtime, fake, clock, timestamp):
            ticket = runtime['tickets']['exit-custody']
            clock.return_value = (timestamp + 20000) * 1000000
            if stage in ('resize', 'flat'):
                fill_passive(fake)
                if stage == 'flat':
                    fake['position'] = 0
                await process_event(runtime, ticket, {'channel': 'userFills'})
            elif stage in ('giveback', 'requote'):
                await process_event(runtime, ticket, market_book())
            elif stage == 'time_stop':
                await process_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
            rows = read_records(runtime['config']['record_path'])
            initial_fake = deepcopy(fake)
            clean = await replay(runtime, fake, clock, timestamp, rows, initial_fake)
            assert clean is not None
            for index, row in enumerate(rows):
                if row['event'] != 'state':
                    continue
                for keys, replacement in integer_boundaries(rows, index):
                    changed = deepcopy(rows)
                    target = changed[index]
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = replacement
                    observed = await replay(runtime, fake, clock, timestamp, changed, initial_fake)
                    assert observed is None or observed == clean, (stage, index, keys, replacement, observed, clean)
                    fields.add(keys)
                    count += 1
    assert {('timestamp',), ('claimed_at',), ('close_ms',), ('exit_action_cost',), ('last_exit_action_ms',)} <= fields
    assert count > 1000
    print('PASS exit integer boundaries: ' + str(count) + ' substitutions across ' + str(len(fields)) + ' fields', flush=True)


async def check_exit_pacing_recovery():
    async with custody_fixture() as (runtime, fake, clock, timestamp):
        path = Path(runtime['config']['record_path'])
        clock.return_value = (timestamp + 20000) * 1000000
        await process_event(runtime, runtime['tickets']['exit-custody'], market_book())
        rows = read_records(path)
        initial_fake = deepcopy(fake)
        reservation = max(index for index, row in enumerate(rows) if row['event'] == 'exit_reservation')
        assert rows[reservation]['cost'] == 2
        for index in range(reservation, len(rows)):
            write_rows(path, rows[:index + 1])
            restored = next(iter(latest_tickets(runtime['config']).values()))
            dispatched = [row for row in rows[reservation:index + 1] if row['event'] == 'intent']
            expected_cost = min(2, len(dispatched)) if dispatched else 2
            assert restored['exit_action_cost'] == expected_cost
            assert restored['last_exit_action_ms'] == rows[reservation]['timestamp']
            assert await replay(runtime, fake, clock, timestamp, rows[:index + 1], initial_fake) is not None
        clean = await replay(runtime, fake, clock, timestamp, rows, initial_fake)
        legacy = [row for row in rows if row['event'] != 'exit_reservation']
        assert await replay(runtime, fake, clock, timestamp, legacy, initial_fake) == clean


async def check_exit_pacing_clamp():
    for excessive_cost, future_time in ((True, False), (False, True), (True, True)):
        for fast, used in ((False, 0), (False, 1000), (True, 0), (True, 1000)):
            with driver_fixture() as (runtime, ticket, sent, info, clock):
                timestamp = clock.return_value
                window = runtime['config']['exit_min_action_ms']
                ticket.update(last_exit_action_ms=timestamp + (3600000 if future_time else 0),
                              exit_action_cost=1000000 if excessive_cost else 1, exit_fast=fast)
                info.return_value['nRequestsUsed'] = used
                for elapsed in (0, 1):
                    clock.return_value = timestamp + elapsed
                    await drive_exit(runtime, ticket, AsyncMock())
                    assert not sent and not info.await_count
                if fast and used:
                    clock.return_value = timestamp + runtime['config']['exit_fast_action_ms'] * (2 if excessive_cost else 1)
                    await drive_exit(runtime, ticket, AsyncMock())
                    assert not sent and info.await_count == 1
                clock.return_value = timestamp + window
                await drive_exit(runtime, ticket, AsyncMock())
                assert len(sent) == 1 and sent[0]['reduce_only'] and info.await_count == 1 + bool(fast and used)
                assert ticket['last_exit_action_ms'] == timestamp + window
                read_records(runtime['config']['record_path'])
    for cost in (1, 2):
        for fast in (False, True):
            with driver_fixture() as (runtime, ticket, sent, info, clock):
                timestamp = clock.return_value
                interval = runtime['config']['exit_fast_action_ms' if fast else 'exit_min_action_ms']
                wait = min(runtime['config']['exit_min_action_ms'], interval * cost)
                ticket.update(last_exit_action_ms=timestamp - 1, exit_action_cost=cost, exit_fast=fast)
                info.return_value['nRequestsUsed'] = 0 if fast else 1000
                clock.return_value = timestamp + wait - 2
                await drive_exit(runtime, ticket, AsyncMock())
                assert not sent
                clock.return_value += 1
                await drive_exit(runtime, ticket, AsyncMock())
                assert len(sent) == 1
                read_records(runtime['config']['record_path'])


async def main():
    for check in (check_exit_state_regressions, check_exit_state_field_matrix,
                  check_target_giveback_recovery, check_exit_evidence_recovery,
                  check_exit_pacing_regressions, check_exit_integer_matrix, check_exit_pacing_recovery,
                  check_exit_pacing_clamp):
        await check()
        print('PASS exit custody: ' + check.__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
