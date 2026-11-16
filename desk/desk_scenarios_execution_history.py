import asyncio
import copy
import io
import json
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from .desk_executor import cancel, submit
from .desk_scenarios_resident import finish_exit
from .desk_record import read_records, signed_today
from .desk_scenarios_anchor_gate import verify_fixture
from .desk_scenarios_bracket import envelope, fixture
from .desk_scenarios_history import nested_fields, replace_field
from .desk_scenarios_journal import observe_recovered
from .desk_scenarios_writer_history import collect_writer_histories


async def ready_watcher(runtime, ticket):
    ticket['alive'] = True


async def observe(runtime, fake, value, contents, action, initial_fake):
    runtime['request_times'] = []
    runtime['backoff_until'] = 0
    Path(runtime['config']['record_path']).write_text(contents)
    try:
        budget = signed_today(runtime['config'], 'BTC')
    except ValueError:
        budget = None
    if runtime['config']['resident'] and action == 'submit':
        runtime['tickets'].clear()
        fake.clear()
        fake.update(copy.deepcopy(initial_fake))
        with patch('desk.desk_executor.arm', ready_watcher):
            result = await submit(runtime, value)
        activity = (fake['prepares'] - initial_fake['prepares'], len(fake['intents']) - len(initial_fake['intents']))
    else:
        result, activity = await observe_recovered(runtime, fake, value, contents, action, initial_fake)
    return budget, result, activity


async def check_entry_reply_evidence():
    async with fixture(resident=True) as (runtime, fake):
        async def arm(runtime, ticket):
            await ready_watcher(runtime, ticket)
            fake['events'].append('watcher_armed')

        with patch('desk.desk_executor.arm', arm):
            assert await submit(runtime, envelope('previous')) == {'state': 'filled'}
        assert await cancel(runtime, 'previous') == {'state': 'filled'}
        await finish_exit(runtime, fake, runtime['tickets']['previous'], '100000')
        runtime['config']['policy']['asset_caps'][0]['max_daily_size'] = '0.015'
        runtime['config']['resident'] = False
        value = envelope('next')
        await verify_fixture(runtime, value)
        path = Path(runtime['config']['record_path'])
        clean = path.read_text()
        rows = [json.loads(line) for line in clean.splitlines()]
        index = next(index for index, row in enumerate(rows) if row['event'] == 'reply')
        changed = replace_field(rows, index, ('signed',), False)
        initial_fake = copy.deepcopy(fake)
        for resident in (False, True):
            runtime['config']['resident'] = resident
            for action in (('size', 'gate', 'submit') if resident else ('size', 'gate', 'run', 'submit')):
                baseline = await observe(runtime, fake, value, clean, action, initial_fake)
                assert baseline[0] == Decimal('0.01') and baseline[2] == (0, 0), baseline
                assert (Decimal(baseline[1]['size']) == Decimal('0.01') if action == 'size'
                        else baseline[1] == {'refused': 'over_max_daily_size'} or
                        baseline[1].get('refused') == 'over_max_daily_size'), baseline
                altered = await observe(runtime, fake, value, changed, action, initial_fake)
                assert altered == (None, {'refused': 'anchor_not_verified'}, (0, 0)), (resident, action, altered)


async def check_entry_completion_evidence():
    async with fixture() as (runtime, fake):
        value = envelope('unresolved')
        await verify_fixture(runtime, value)
        with patch('desk.desk_scenarios_bracket.coordinator_intent', AsyncMock(side_effect=TimeoutError)):
            assert (await submit(runtime, value))['reason'] == 'bracket_outcome_unresolved'
        value = envelope('next')
        await verify_fixture(runtime, value)
        path = Path(runtime['config']['record_path'])
        clean = path.read_text()
        rows = [json.loads(line) for line in clean.splitlines()]
        index = next(index for index, row in enumerate(rows) if row['event'] == 'state' and row['state'] == 'sent')
        assert rows[index]['entry_done'] is False
        changed = replace_field(rows, index, ('entry_done',), True)
        initial_fake = copy.deepcopy(fake)
        for action in ('size', 'gate', 'run', 'submit'):
            baseline = await observe(runtime, fake, value, clean, action, initial_fake)
            assert baseline[0] == Decimal('0.01') and baseline[2] == (0, 0), baseline
            if action in ('run', 'submit'):
                assert baseline[1] == {'refused': 'account_busy'}, baseline
            elif action == 'gate':
                assert baseline[1]['status'] == 'PASS', baseline
            else:
                assert Decimal(baseline[1]['size']) == Decimal('0.01'), baseline
            altered = await observe(runtime, fake, value, changed, action, initial_fake)
            assert altered == (None, {'refused': 'anchor_not_verified'}, (0, 0)), (action, altered)


async def check_boolean_history():
    with redirect_stdout(io.StringIO()):
        histories = await collect_writer_histories()
    comparisons, covered = 0, set()
    async with fixture() as (runtime, fake):
        value = envelope('boolean-next')
        await verify_fixture(runtime, value)
        passed = Path(runtime['config']['record_path']).read_text()
        initial_fake = copy.deepcopy(fake)
        for event, snapshots in histories.items():
            for snapshot in snapshots:
                rows = [json.loads(line) for line in snapshot.splitlines()]
                index = len(rows) - 1
                for keys, original in nested_fields(rows[index]):
                    if type(original) is not bool:
                        continue
                    identity = (event, keys, original, rows[index].get('state'))
                    if identity in covered:
                        continue
                    covered.add(identity)
                    clean = snapshot + passed
                    changed = replace_field(rows, index, keys, not original) + passed
                    for action in ('size', 'gate', 'run', 'submit'):
                        baseline = await observe(runtime, fake, value, clean, action, initial_fake)
                        assert baseline[0] is not None, (identity, baseline)
                        altered = await observe(runtime, fake, value, changed, action, initial_fake)
                        assert altered == baseline or altered == (
                            None, {'refused': 'anchor_not_verified'}, (0, 0)), (identity, action, baseline, altered)
                        comparisons += 1
        for identity in (('reply', ('signed',), True, None), ('reply', ('ambiguous',), True, None),
                         ('state', ('entry_done',), False, 'sent')):
            assert identity in covered, identity
    return comparisons, len(covered)


async def main():
    with patch('time.time_ns', return_value=1794824160000000000):
        await check_entry_reply_evidence()
        print('PASS entry reply evidence: edited resident signed reply refuses routine and resident entry paths', flush=True)
        await check_entry_completion_evidence()
        print('PASS entry completion evidence: edited sent completion refuses; unresolved control keeps account_busy', flush=True)
        comparisons, fields = await check_boolean_history()
        print('PASS boolean history: ' + str(fields) + ' writer boolean fields; ' + str(comparisons)
              + ' differential comparisons preserve outcomes or refuse without coordinator calls', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
