import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .desk_executor import cancel, recover, submit
from .desk_scenarios_resident import finish_exit
from .desk_record import append_record, now_ms, read_records, signed_today
from .desk_scenarios_anchor_gate import verify_fixture
from .desk_scenarios_bracket import command_result, envelope, fixture
from .desk_scenarios_fakes import coordinator_intent


async def spent_journal(runtime, fake, kind):
    previous = envelope('previous')
    await verify_fixture(runtime, previous)
    assert await submit(runtime, previous) == {'state': 'sent'}
    await cancel(runtime, 'previous')
    assert not fake['orders'] and fake['position'] == 0
    assert runtime['tickets']['previous']['state'] == 'exited'
    path = Path(runtime['config']['record_path'])
    rows = read_records(path)
    original = next(row for row in rows if row['event'] == 'intent')
    cancellation = next(row['intent'] for row in rows
                        if row['event'] == 'intent' and row['intent']['kind'] == 'cancel')
    if kind == 'order':
        intent = original['intent']
        original['intent'] = {**{name: value for name, value in intent.items()
                                if name not in ('entry', 'stop', 'take_profit')}, **intent['entry'], 'tif': 'Ioc'}
        rows = [row for row in rows if row['event'] != 'bracket_result']
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
    runtime['config']['policy']['asset_caps'][0]['max_daily_size'] = '0.015'
    value = envelope('sample')
    before_pass = path.read_text()
    await verify_fixture(runtime, value)
    clean = path.read_text()
    return value, original, cancellation, before_pass, clean[len(before_pass):]


def conflicting_intents(original, cancellation):
    intent = original['intent']
    variants = [{'request_id': intent['request_id'], 'kind': 'cancel'},
                {**cancellation, 'request_id': intent['request_id']}]
    for field, value in (('size', '0.005'), ('size', '0.02'), ('asset', 'ETH'),
                         ('is_buy', False), ('reduce_only', True), ('limit_px', '99999')):
        changed = copy.deepcopy(intent)
        changed.get('entry', changed)[field] = value
        if field == 'asset':
            changed['asset'] = value
        variants.append(changed)
    variants.extend([{**intent, 'kind': 'schedule_cancel', 'time_ms': now_ms() + 60000},
                     {**intent, 'kind': ''}, {**intent, 'key_ref': 'other-reference'},
                     {**intent, 'kind': 'unknown'}])
    assert len(variants) >= 8
    return variants


async def observe_action(runtime, fake, value, contents, action, initial_fake):
    path = Path(runtime['config']['record_path'])
    path.write_text(contents)
    runtime['tickets'].clear()
    fake.clear()
    fake.update(copy.deepcopy(initial_fake))
    try:
        budget = signed_today(runtime['config'], 'BTC')
    except ValueError:
        budget = None
    result = await submit(runtime, value) if action == 'submit' else await command_result(
        runtime, action, value, verified=False)
    activity = (fake['prepares'] - initial_fake['prepares'], len(fake['intents']) - len(initial_fake['intents']))
    return budget, result, activity


async def check_budget_conflicts(kind):
    async with fixture() as (runtime, fake):
        value, original, cancellation, before_pass, passed = await spent_journal(runtime, fake, kind)
        clean = before_pass + passed
        initial_fake = copy.deepcopy(fake)
        for intent in conflicting_intents(original, cancellation):
            extra = json.dumps({'event': 'intent', 'timestamp': now_ms(), 'intent': intent}) + '\n'
            for before in (True, False):
                changed = before_pass + extra + passed if before else clean + extra
                assert (changed[:len(before_pass)] + changed[len(before_pass) + len(extra):]
                        if before else changed[:-len(extra)]) == clean
                for action in ('size', 'gate', 'run', 'submit'):
                    baseline = await observe_action(runtime, fake, value, clean, action, initial_fake)
                    assert baseline[0] == Decimal('0.01') and baseline[2] == (0, 0)
                    if action == 'size':
                        assert Decimal(baseline[1]['size']) == Decimal('0.01')
                    else:
                        assert baseline[1]['refused'] == 'over_max_daily_size', baseline
                    altered = await observe_action(runtime, fake, value, changed, action, initial_fake)
                    assert altered == baseline or altered == (None, {'refused': 'anchor_not_verified'}, (0, 0)), (
                        kind, intent, before, action, baseline, altered)


async def check_budget_resends():
    for kind in ('bracket', 'order'):
        async with fixture() as (runtime, fake):
            value, original, cancellation, before_pass, passed = await spent_journal(runtime, fake, kind)
            initial_fake = copy.deepcopy(fake)
            clean = before_pass + passed
            for intent in (original['intent'], cancellation):
                for timestamp in (original['timestamp'], original['timestamp'] + 86400000):
                    extra = json.dumps({'event': 'intent', 'timestamp': timestamp, 'intent': intent}) + '\n'
                    for changed in (before_pass + extra + passed, clean + extra):
                        for action in ('size', 'gate', 'run', 'submit'):
                            baseline = await observe_action(runtime, fake, value, clean, action, initial_fake)
                            altered = await observe_action(runtime, fake, value, changed, action, initial_fake)
                            assert baseline[0] == Decimal('0.01') and baseline[2] == (0, 0)
                            assert altered == baseline, (kind, action, baseline, altered)


async def check_intent_shapes():
    async with fixture() as (runtime, fake):
        value, original, cancellation, before_pass, passed = await spent_journal(runtime, fake, 'bracket')
        initial_fake = copy.deepcopy(fake)
        order = {**{name: item for name, item in original['intent'].items()
                    if name not in ('entry', 'stop', 'take_profit')}, **original['intent']['entry']}
        for intent, fields in ((order, ('request_id', 'kind', 'asset', 'asset_index', 'is_buy', 'size',
                                        'reduce_only', 'limit_px', 'tif', 'delegate_key', 'key_ref')),
                               (cancellation, ('request_id', 'asset', 'asset_index', 'oid'))):
            for field in fields:
                for replacement in (None, [], True):
                    if field in ('is_buy', 'reduce_only') and replacement is True:
                        continue
                    changed_intent = {**intent, 'request_id': 'new-identity', field: replacement}
                    row = json.dumps({'event': 'intent', 'timestamp': now_ms(), 'intent': changed_intent}) + '\n'
                    for contents in (before_pass + row + passed, before_pass + passed + row):
                        for action in ('size', 'gate', 'run', 'submit'):
                            assert await observe_action(runtime, fake, value, contents, action, initial_fake) == (
                                None, {'refused': 'anchor_not_verified'}, (0, 0)), (field, replacement, action)


async def compare_journals(runtime, fake, value, clean, changed, initial_fake, expected=None):
    for action in ('size', 'gate', 'run', 'submit'):
        baseline = await observe_action(runtime, fake, value, clean, action, initial_fake)
        altered = await observe_action(runtime, fake, value, changed, action, initial_fake)
        assert baseline[0] == Decimal('0.01') and baseline[2] == (0, 0), baseline
        if action != 'size':
            assert baseline[1]['refused'] == 'over_max_daily_size', baseline
        refused = (None, {'refused': 'anchor_not_verified'}, (0, 0))
        assert altered == baseline or altered == refused, (action, baseline, altered, changed)
        if expected is not None:
            assert altered == (baseline if expected == 'same' else refused), (action, altered)


async def check_reply_identity(kind):
    async with fixture() as (runtime, fake):
        value, original, cancellation, before_pass, passed = await spent_journal(runtime, fake, kind)
        initial_fake = copy.deepcopy(fake)
        rows = [json.loads(line) for line in before_pass.splitlines()]
        reply = next(row for row in rows if row['event'] == 'reply')
        variants = [({**reply, 'signed': False}, 'refuse'),
                    ({**reply, 'signed': None}, 'refuse'),
                    ({**reply, 'venue_answer': {'signature': 'changed'}}, 'refuse'),
                    ({'event': 'reply', 'timestamp': 0, 'request_id': reply['request_id'], 'signed': True}, 'refuse'),
                    ({**reply, 'timestamp': original['timestamp'] - 1}, 'refuse'),
                    ({**reply, 'request_id': 'unknown-request'}, 'refuse'),
                    (reply, 'same'), ({**reply, 'timestamp': now_ms() + 86400000}, 'same')]
        clean = before_pass + passed
        for row, expected in variants:
            extra = json.dumps(row) + '\n'
            for changed in (before_pass + extra + passed, clean + extra):
                await compare_journals(runtime, fake, value, clean, changed, initial_fake, expected)
        await compare_journals(runtime, fake, value, clean, json.dumps(reply) + '\n' + clean,
                               initial_fake, 'refuse')
        confirmed = {**reply, 'signature': 'first-signature'}
        clean += json.dumps(confirmed) + '\n'
        await compare_journals(runtime, fake, value, clean,
                               clean + json.dumps({**confirmed, 'signature': 'different-signature'}) + '\n',
                               initial_fake, 'refuse')
        original['timestamp'] -= 2 * 86400000
        first_intent = next(index for index, row in enumerate(rows) if row['event'] == 'intent')
        rows[first_intent] = original
        clean = ''.join(json.dumps(row) + '\n' for row in rows) + passed
        earlier = {**reply, 'timestamp': reply['timestamp'] - 86400000}
        changed = rows[:first_intent + 1] + [earlier] + rows[first_intent + 1:]
        await compare_journals(runtime, fake, value, clean,
                               ''.join(json.dumps(row) + '\n' for row in changed) + passed,
                               initial_fake, 'same')


async def check_randomized_budget():
    randomizer = Random(8192)
    comparisons = 0
    for kind in ('bracket', 'order'):
        async with fixture() as (runtime, fake):
            value, original, cancellation, before_pass, passed = await spent_journal(runtime, fake, kind)
            initial_fake = copy.deepcopy(fake)
            rows = [json.loads(line) for line in (before_pass + passed).splitlines()]
            templates = {row['event']: row for row in rows}
            templates['outcome'] = {'event': 'outcome', 'timestamp': now_ms(), 'signed': False}
            identities = [row['intent']['request_id'] for row in rows if row['event'] == 'intent']
            combinations = [(event, request_id, variant) for event in templates
                            for request_id in identities for variant in range(2)]
            randomizer.shuffle(combinations)
            for event, request_id, variant in combinations:
                row = copy.deepcopy(templates[event])
                row['request_id'] = request_id
                if 'intent' in row:
                    row['intent']['request_id'] = request_id
                if variant:
                    row['timestamp'] = randomizer.choice([0, now_ms() - 86400000, now_ms() + 86400000])
                    if event == 'reply':
                        row['signed'] = False
                    if 'outcome' in row:
                        row['outcome']['signed'] = False
                    if event == 'state':
                        row['state'] = randomizer.choice(['refused', 'exited', 'not_filled'])
                lines = (before_pass + passed).splitlines(keepends=True)
                lines.insert(randomizer.randrange(len(lines) + 1), json.dumps(row) + '\n')
                await compare_journals(runtime, fake, value, before_pass + passed,
                                       ''.join(lines), initial_fake)
                comparisons += 4
    assert comparisons >= 200, comparisons
    return comparisons


async def check_budget_releases():
    async with fixture() as (runtime, fake):
        fake['bracket_refusal'] = 'unsupported_kind'
        value = envelope('unsigned')
        await verify_fixture(runtime, value)
        assert await submit(runtime, value) == {'state': 'bracket_refused', 'reason': 'unsupported_kind'}
        path = Path(runtime['config']['record_path'])
        rows = read_records(path)
        assert signed_today(runtime['config'], 'BTC') == 0
        reservation = [row for row in rows if row['event'] not in ('reply', 'state', 'review', 'bracket_refused')]
        assert signed_today(runtime['config'], 'BTC', records=reservation) == Decimal('0.01')
        fake.pop('bracket_refusal')
        value = envelope('next')
        await verify_fixture(runtime, value)
        assert await submit(runtime, value) == {'state': 'sent'}
        fake['rate_cancel'] = True

        async def rate_cancel_once(request):
            body = await request.json()
            if body['intent']['kind'] == 'cancel' and fake.get('rate_cancel'):
                fake['rate_cancel'] = False
                fake['intents'].append(body['intent'])
                return SimpleNamespace(status=429, headers={'Retry-After': '0'}, body='{}')
            return await coordinator_intent(request)

        with (patch('desk.desk_scenarios_bracket.coordinator_intent', rate_cancel_once),
              patch('desk.signer_delegate_client.coordinator_backoff', AsyncMock())):
            assert await cancel(runtime, 'next') == {'state': 'exited'}
        rows = read_records(path)
        refusal = next(row for row in rows if row['event'] == 'reply' and row.get('reason') == 'http_429')
        assert any(row['event'] == 'reply' and row['request_id'] == refusal['request_id']
                   and row['signed'] is True for row in rows)
        assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
        value = envelope('after-close')
        await verify_fixture(runtime, value)
        assert await submit(runtime, value) == {'state': 'sent'}
    async with fixture(resident=True) as (runtime, fake):
        async def arm(runtime, ticket):
            ticket['alive'] = True
            fake['events'].append('watcher_armed')

        with patch('desk.desk_executor.arm', arm):
            assert await submit(runtime, envelope('rate-exit')) == {'state': 'filled'}
        fake['rate_exit'] = True
        with patch('desk.signer_delegate_client.coordinator_backoff', AsyncMock()):
            assert await cancel(runtime, 'rate-exit') == {'state': 'filled'}
            await finish_exit(runtime, fake, runtime['tickets']['rate-exit'], '100000')
        rows = read_records(runtime['config']['record_path'])
        replies = [row for row in rows if row['event'] == 'reply']
        assert [row['signed'] for row in replies[:3]] == [True, False, True]
        assert all(row['signed'] is True for row in replies[3:])
        assert replies[1]['request_id'] == replies[2]['request_id']
        assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
    async with fixture() as (runtime, fake):
        value = envelope('ambiguous')
        await verify_fixture(runtime, value)

        async def ambiguous_intent(request):
            await coordinator_intent(request)
            raise TimeoutError

        with patch('desk.desk_scenarios_bracket.coordinator_intent', ambiguous_intent):
            assert (await submit(runtime, value))['reason'] == 'bracket_outcome_unresolved'
        assert signed_today(runtime['config'], 'BTC', timestamp=now_ms() + 86400000) == Decimal('0.01')


async def check_bracket_result_agreement():
    async with fixture() as (runtime, fake):
        value = envelope('unresolved')
        await verify_fixture(runtime, value)
        with patch('desk.desk_scenarios_bracket.coordinator_intent', AsyncMock(side_effect=TimeoutError)):
            assert (await submit(runtime, value))['reason'] == 'bracket_outcome_unresolved'
        path = Path(runtime['config']['record_path'])
        rows = read_records(path)
        reply = next(row for row in rows if row['event'] == 'reply')
        outcome = next(row for row in rows if row['event'] == 'bracket_result')
        assert outcome['request_id'] == reply['request_id']
        assert reply['signed'] is None and reply['ambiguous'] is True
        value = envelope('next')
        await verify_fixture(runtime, value)
        clean = path.read_text()
        initial_fake = copy.deepcopy(fake)
        results = []
        for action in ('run', 'submit'):
            for added in (False, True):
                path.write_text(clean)
                runtime['tickets'].clear()
                fake.clear()
                fake.update(copy.deepcopy(initial_fake))
                if added:
                    append_record(path, 'bracket_result', key=outcome['key'],
                                  outcome={'signed': True, 'ambiguous': False, 'intent': None, 'venue_answer': []})
                if action == 'submit':
                    await recover(runtime)
                    result = await submit(runtime, value)
                else:
                    result = await command_result(runtime, action, value, verified=False)
                results.append(result)
                assert result == {'refused': 'account_busy'}, result
                assert fake['prepares'] == initial_fake['prepares'] and fake['intents'] == initial_fake['intents']
                assert runtime['tickets']['unresolved']['uncertain']
        assert results == [{'refused': 'account_busy'}] * 4


async def main():
    identifiers = iter(range(10000))
    with (patch('time.time_ns', return_value=1794824160000000000),
          patch('uuid.uuid4', side_effect=lambda: SimpleNamespace(hex=format(next(identifiers), '032x')))):
        for kind in ('bracket', 'order'):
            await check_budget_conflicts(kind)
            print('PASS budget identity ' + kind + ': 96 differential comparisons preserve budget or refuse without sends', flush=True)
        await check_budget_resends()
        print('PASS budget identity resends: identical order and cancel repeats retain first reservation and date', flush=True)
        await check_intent_shapes()
        print('PASS budget identity shapes: malformed order and cancel fields refuse all four paths', flush=True)
        for kind in ('bracket', 'order'):
            await check_reply_identity(kind)
            print('PASS reply identity ' + kind + ': contradictory replies refuse; identical confirmations preserve spending', flush=True)
        comparisons = await check_randomized_budget()
        print('PASS randomized budget identity: ' + str(comparisons) + ' comparisons across every recorded event and request ID', flush=True)
        await check_budget_releases()
        print('PASS budget releases: unsigned refusal, completed ticket, exit retry and ambiguous reservation use writer paths', flush=True)
        await check_bracket_result_agreement()
        print('PASS bracket result agreement: appended unmatched outcome preserves account_busy with zero coordinator calls', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
