import asyncio
from unittest.mock import patch

from .desk_executor import entry_preview, process_event, reconcile
from .desk_io import oracle_prices
from .desk_record import now_ms, read_records
from .desk_routine import maintain_routine
from .desk_scenarios_bracket import command_result, envelope, fixture
from .desk_scenarios_fakes import coordinator_intent, coordinator_prepare, fill_fake, submit_verified_fixture
from .desk_scenarios_resident import market_book
from .desk_wake import last_close
from .signer_delegate_client import coordinator_backoff, read_info


async def deadline_crossing(resident, stage, offset=1):
    timestamp = last_close(now_ms()) + 60000
    with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
        async with fixture(resident=resident) as (runtime, fake):
            deadline = timestamp + 1000
            value = envelope('entry-deadline')
            value['payload']['exit_by'] = deadline
            if stage == 'resend':
                fake['stale'] = 1

            def advance():
                clock.return_value = (deadline + offset) * 1000000

            async def preview(*arguments):
                result = await entry_preview(*arguments)
                if stage == 'preview':
                    advance()
                return result

            async def arm(runtime, ticket):
                ticket['alive'] = True
                if stage == 'watcher':
                    advance()

            async def account_read(*arguments, **keywords):
                result = await read_info(*arguments, **keywords)
                if stage == 'account':
                    advance()
                return result

            async def prepare(request):
                result = await coordinator_prepare(request)
                if stage == 'prepare':
                    advance()
                return result

            async def prices(*arguments):
                result = await oracle_prices(*arguments)
                if stage == 'oracle':
                    advance()
                return result

            async def backoff(runtime):
                await coordinator_backoff(runtime)
                if stage == 'send' and fake['prepares']:
                    advance()

            async def intent(request):
                result = await coordinator_intent(request)
                if stage == 'resend':
                    advance()
                return result

            with (patch('desk.desk_executor.entry_preview', preview), patch('desk.desk_executor.arm', arm),
                  patch('desk.desk_executor.read_info', account_read),
                  patch('desk.desk_scenarios_bracket.coordinator_prepare', prepare),
                  patch('desk.desk_scenarios_bracket.coordinator_intent', intent),
                  patch('desk.desk_coordinator.oracle_prices', prices),
                  patch('desk.signer_delegate_client.coordinator_backoff', backoff)):
                result = await submit_verified_fixture(runtime, value)
            assert result.get('refused', result.get('reason')) == 'exit_by_passed', (resident, stage, result)
            assert fake['prepares'] == (0 if stage in ('preview', 'account', 'watcher') else 2 if stage == 'resend' else 1)
            assert len(fake['intents']) == (1 if stage == 'resend' else 0)
            assert not fake['position'] and not fake['orders']
            rows = read_records(runtime['config']['record_path'])
            assert sum(row['event'] == 'intent' for row in rows) == (1 if stage == 'resend' else 0)
            assert any(row.get('reason') == 'exit_by_passed' for row in rows)


async def check_preview_deadline():
    for resident in (False, True):
        for offset in (0, 1):
            await deadline_crossing(resident, 'preview', offset)


async def check_preparation_deadline():
    for resident in (False, True):
        for stage in ('account', 'prepare', 'oracle', 'send'):
            await deadline_crossing(resident, stage)
    await deadline_crossing(True, 'watcher')


async def check_resend_deadline():
    for resident in (False, True):
        await deadline_crossing(resident, 'resend')


async def check_gate_preview_deadline():
    for resident in (False, True):
        timestamp = last_close(now_ms()) + 60000
        with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
            async with fixture(resident=resident) as (runtime, fake):
                value = envelope('gate-deadline')
                value['payload']['exit_by'] = timestamp + 1000

                async def preview(*arguments):
                    result = await entry_preview(*arguments)
                    clock.return_value = (timestamp + 1001) * 1000000
                    return result

                with patch('desk.desk_ticket.entry_preview', preview):
                    result = await command_result(runtime, 'gate', value)
                assert result['refused'] == 'exit_by_passed'
                assert not fake['prepares'] and not fake['intents']


async def check_deadline_maintenance():
    for resident in (False, True):
        timestamp = last_close(now_ms()) + 60000
        with patch('time.time_ns', return_value=timestamp * 1000000) as clock:
            async with fixture(resident=resident) as (runtime, fake):
                value = envelope('maintenance-deadline')
                value['payload']['exit_by'] = timestamp + 1000

                async def arm(runtime, ticket):
                    ticket['alive'] = True

                with patch('desk.desk_executor.arm', arm):
                    await submit_verified_fixture(runtime, value)
                ticket = runtime['tickets']['maintenance-deadline']
                if not resident:
                    fill_fake(fake, fake['intents'][0]['entry'])
                    fake['orders'] = [order for order in fake['orders'] if order['oid'] != 42]
                clock.return_value = (timestamp + 1001) * 1000000
                if resident:
                    await process_event(runtime, ticket, market_book())
                    await process_event(runtime, ticket, {'channel': 'trades', 'data': [{'coin': 'BTC', 'px': '100000'}]})
                else:
                    await maintain_routine(runtime, reconcile)
                actions = fake['intents'][1:]
                assert actions and all(row['kind'] == 'cancel' or row.get('reduce_only') for row in actions)
                assert ticket['closing'] == 'time_stop'
                if not resident:
                    assert ticket['state'] == 'exited' and not fake['orders'] and not fake['position']
                read_records(runtime['config']['record_path'])


async def main():
    for check in (check_preview_deadline, check_preparation_deadline, check_resend_deadline,
                  check_gate_preview_deadline, check_deadline_maintenance):
        await check()
        print('PASS exit deadline: ' + check.__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
