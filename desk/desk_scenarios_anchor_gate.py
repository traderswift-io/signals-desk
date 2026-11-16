import asyncio
import copy
import hashlib
import io
import json
from contextlib import redirect_stdout
from decimal import Decimal, InvalidOperation, Overflow
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .desk_anchor_verify import canonical, main as verify_main
from .desk_executor import cancel, submit
from .desk_record import append_record, read_records, signed_today
from .desk_scenarios_anchor import CONFIG, PATHS, responses, transport
from .desk_scenarios_bracket import check_resident, command_result, envelope, fixture
from .desk_scenarios_fakes import coordinator_intent


async def verify_fixture(runtime, value, reason='pass'):
    directory = Path(runtime['config']['record_path']).parent
    config_path = directory / 'chain.json'
    envelope_path = directory / 'verify-envelope.json'
    config_path.write_text(json.dumps({**runtime['config'], **CONFIG}))
    envelope_path.write_text(json.dumps(value))
    replies = responses()
    digest = hashlib.sha256(canonical(value['payload']).encode('utf-8')).hexdigest()
    replies[2] = (200, json.dumps({'vetoed': False, 'segment_hash': digest}), 'application/json')
    if reason == 'anchor_missing':
        replies[2] = (404, '{}', 'application/json')
    requests = []
    output = io.StringIO()
    signal_id = value['payload']['signal_id']
    paths = [path.replace('/sample/', '/' + signal_id + '/') for path in PATHS]
    with (patch('aiohttp.ClientSession.get', new=transport(replies, requests, paths=paths)), redirect_stdout(output)):
        code = await verify_main(SimpleNamespace(config=config_path, envelope=envelope_path))
    assert requests == paths and code == int(reason != 'pass')
    assert output.getvalue().splitlines() == [
        json.dumps({'signal_id': signal_id, 'reads': [200, 200, 404 if reason == 'anchor_missing' else 200]}),
        'VERIFY | ' + signal_id + ' | ' + reason]
    return digest


async def require_refusal(runtime, fake, value):
    before = read_records(runtime['config']['record_path'])
    for action in ('size', 'gate', 'run'):
        assert await command_result(runtime, action, value, verified=False) == {'refused': 'anchor_not_verified'}
        if action != 'run':
            assert read_records(runtime['config']['record_path']) == before
    assert await submit(runtime, value) == {'refused': 'anchor_not_verified'}
    assert not fake['intents'] and fake['prepares'] == 0 and not runtime['tickets']


async def check_journal_guards():
    async with fixture() as (runtime, fake):
        value = envelope('sample')
        await verify_fixture(runtime, value)
        path = Path(runtime['config']['record_path'])
        recorded = path.read_text()
        for contents in ('{broken}\n', 'null\n', '[]\n'):
            path.write_text(contents)
            for action in ('size', 'gate', 'run'):
                assert await command_result(runtime, action, value, verified=False) == {'refused': 'anchor_not_verified'}
            assert path.read_text() == contents
        path.write_text(recorded)
        with patch('desk.desk_anchor_verify.read_records', side_effect=OSError('journal_unavailable')):
            for action in ('size', 'gate', 'run'):
                assert await command_result(runtime, action, value, verified=False) == {'refused': 'anchor_not_verified'}
        envelope_path = path.parent / 'envelope.json'
        duplicate = json.dumps(value)[:-1] + ',"payload":' + json.dumps(value['payload']) + '}'
        for contents in (duplicate, '{', '{"payload":null}'):
            envelope_path.write_text(contents)
            for action in ('size', 'gate', 'run'):
                assert await command_result(runtime, action) == {'refused': 'anchor_not_verified'}
        assert path.read_text() == recorded and not fake['intents'] and fake['prepares'] == 0


async def check_anchor_gate():
    await check_journal_guards()
    async with fixture() as (runtime, fake):
        value = envelope('sample')
        path = runtime['config']['record_path']
        await require_refusal(runtime, fake, value)
        before = read_records(path)
        await verify_fixture(runtime, value, 'anchor_missing')
        assert read_records(path) == before
        await require_refusal(runtime, fake, value)
        digest = hashlib.sha256(canonical(value['payload']).encode('utf-8')).hexdigest()
        for event, signal_id, recorded_hash in (
                ('anchor_verified', 'other', digest), ('anchor_verified', 'sample', '0' * 64),
                ('verification_requested', 'sample', digest)):
            append_record(path, event, signal_id=signal_id, payload_hash=recorded_hash)
        await require_refusal(runtime, fake, value)
        assert await verify_fixture(runtime, value) == digest
        recorded = read_records(path)[-1]
        assert recorded['event'] == 'anchor_verified' and recorded['signal_id'] == 'sample'
        assert recorded['payload_hash'] == digest and len(digest) == 64 and digest == digest.lower()
        changed = copy.deepcopy(value)
        changed['payload']['levels']['entry'] = '100001'
        changed['segment_hash'] = digest
        changed['receipt'] = {'segment_hash': digest}
        await require_refusal(runtime, fake, changed)
        changed = copy.deepcopy(value)
        changed['payload']['signal_id'] = 'other'
        await require_refusal(runtime, fake, changed)
        before = read_records(path)
        reordered = {**value, 'payload': dict(reversed(list(value['payload'].items())))}
        assert 'size' in await command_result(runtime, 'size', reordered, verified=False)
        assert (await command_result(runtime, 'gate', reordered, verified=False))['status'] == 'PASS'
        assert read_records(path) == before
        assert await command_result(runtime, 'run', value, verified=False) == {'state': 'sent'}
        assert len(fake['intents']) == 1 and fake['intents'][0]['kind'] == 'bracket'
    async with fixture() as (runtime, fake):
        value = envelope('sample')
        with patch('desk.desk_anchor_verify.append_record', side_effect=OSError('journal_unavailable')):
            await verify_fixture(runtime, value, 'invalid_input')
        assert read_records(runtime['config']['record_path']) == []
        await require_refusal(runtime, fake, value)
    async with fixture(resident=True) as (runtime, fake):
        value = envelope('sample')
        assert 'size' in await command_result(runtime, 'size', value, verified=False)
        assert (await command_result(runtime, 'gate', value, verified=False))['status'] == 'PASS'
        assert read_records(runtime['config']['record_path']) == []
        assert not fake['intents'] and fake['prepares'] == 0
    await check_resident()


async def check_journal_corruption_order():
    async with fixture() as (runtime, fake):
        value = envelope('sample')
        await verify_fixture(runtime, value)
        path = Path(runtime['config']['record_path'])
        recorded = path.read_text()
        malformed_rows = ['null', '[]', '"record"', '1', 'true', '{broken}', '{}',
                          '{"event":null,"timestamp":0}', '{"event":"","timestamp":0}',
                          '{"event":"state"}', '{"event":"state","timestamp":"0"}',
                          '{"event":"state","timestamp":true}', '{"event":"state","timestamp":-1}']
        for row in malformed_rows:
            for contents in (recorded + row + '\n', row + '\n' + recorded):
                path.write_text(contents)
                try:
                    read_records(path)
                except ValueError:
                    pass
                else:
                    raise AssertionError('Malformed journal accepted')
                for action in ('size', 'gate', 'run'):
                    result = await command_result(runtime, action, value, verified=False)
                    assert result == {'refused': 'anchor_not_verified'}, (row, action, result)
                    assert path.read_text() == contents
                assert await submit(runtime, value) == {'refused': 'anchor_not_verified'}
                refusal = json.loads(path.read_text()[len(contents):])
                assert refusal['event'] == 'refusal' and refusal['reason'] == 'anchor_not_verified'
                assert not fake['intents'] and fake['prepares'] == 0 and not runtime['tickets']


def malformed_event_records(runtime, value):
    signal = {**value['payload'], 'asset': 'BTC', 'asset_index': 0}
    key = [runtime['config']['account_address'], signal['publisher'], signal['signal_id']]
    state = {'event': 'state', 'timestamp': 0, 'key': key, 'state': 'claimed', 'signal': signal}
    intent = {'request_id': 'sample', 'kind': 'order', 'reduce_only': False, 'asset': 'BTC',
              'delegate_key': runtime['config']['delegate_public_key'], 'key_ref': runtime['config']['key_ref'],
              'size': '0.01', 'asset_index': 0, 'is_buy': True, 'limit_px': '100000', 'tif': 'Ioc'}
    rows = [{'event': 'state', 'timestamp': 0, 'key': None},
            {'event': 'intent', 'timestamp': 0, 'intent': None}]
    for field, values in (('key', (None, 'abc', [], key[:2], [key[0], {}, key[2]])),
                          ('state', (None, [], 'unknown')), ('signal', (None, [], {})),
                          ('claimed_at', (None, '0', True)), ('fill_price', ({}, 'NaN')),
                          ('entry_done', ('false',)), ('stop_hit', ([],)), ('close_ms', ('0',)),
                          ('entry_cancel_reason', ([],))):
        rows.extend({**state, field: replacement} for replacement in values)
    for field in ('signal_id', 'publisher', 'asset', 'asset_index', 'side', 'reply_by', 'levels'):
        rows.append({**state, 'signal': {**signal, field: None}})
        rows.append({**state, 'signal': {name: item for name, item in signal.items() if name != field}})
    for field in ('entry', 'stop', 'target', 'invalidation'):
        rows.append({**state, 'signal': {**signal, 'levels': {**signal['levels'], field: 'NaN'}}})
    for field in intent:
        rows.append({'event': 'intent', 'timestamp': 0, 'intent': {**intent, field: None}})
        rows.append({'event': 'intent', 'timestamp': 0,
                     'intent': {name: item for name, item in intent.items() if name != field}})
    for size in ('NaN', 'Infinity', '-1', [], True):
        rows.append({'event': 'intent', 'timestamp': 0, 'intent': {**intent, 'size': size}})
    for entry in (None, [], {}, {**intent, 'size': 'NaN'}, {**intent, 'kind': 'cancel'}):
        rows.append({'event': 'intent', 'timestamp': 0, 'intent': {**intent, 'kind': 'bracket', 'entry': entry}})
    rows.extend([{'event': 'reply', 'timestamp': 0, 'request_id': []},
                 {'event': 'reply', 'timestamp': 0, 'request_id': 'sample', 'signed': 'false'},
                 {'event': 'bracket_result', 'timestamp': 0, 'key': key, 'outcome': None},
                 {'event': 'bracket_result', 'timestamp': 0, 'key': key, 'outcome': {'signed': 'true'}}])
    return [json.dumps(row) for row in rows]


async def check_journal_event_contents(make_rows=malformed_event_records):
    async with fixture() as (runtime, fake):
        value = envelope('sample')
        await verify_fixture(runtime, value)
        path = Path(runtime['config']['record_path'])
        recorded = path.read_text()
        rows = make_rows(runtime, value)
        for row in rows:
            malformed = row + '\n'
            for contents in (recorded + malformed, malformed + recorded):
                path.write_text(contents)
                for action in ('size', 'gate', 'run'):
                    result = await command_result(runtime, action, value, verified=False)
                    assert result == {'refused': 'anchor_not_verified'}, (row, action, result)
                    assert path.read_text() == contents
                    assert not fake['intents'] and fake['prepares'] == 0 and not runtime['tickets']
                assert await submit(runtime, value) == {'refused': 'anchor_not_verified'}
                assert not fake['intents'] and fake['prepares'] == 0 and not runtime['tickets']


def numeric_event_records(runtime, value):
    intent = {'request_id': 'sample-old', 'kind': 'order', 'reduce_only': False, 'asset': 'BTC',
              'delegate_key': runtime['config']['delegate_public_key'], 'key_ref': runtime['config']['key_ref'],
              'asset_index': 0, 'is_buy': True, 'limit_px': '100000', 'tif': 'Ioc'}
    rows = [{'event': 'intent', 'timestamp': 0, 'intent': {**intent, 'size': number}}
            for number in ('1e1000000', '1e-1000000', 'NaN', 'Infinity', '9' * 10000, '-1', True)]
    for depth in (33, 2000):
        rows.append('{"event":"extra","timestamp":0,"nested":' + '{"nested":' * depth + '0' + '}' * depth + '}')
    return [row if isinstance(row, str) else json.dumps(row) for row in rows]


def substituted_event_records(runtime, value):
    signal = {**value['payload'], 'asset': 'BTC', 'asset_index': 0}
    key = [runtime['config']['account_address'], signal['publisher'], signal['signal_id']]
    state = '"event":"state","key":' + json.dumps(key) + ',"state":"claimed"'
    signal_fields = json.dumps({name: item for name, item in signal.items()
                               if name not in ('levels', 'reply_by', 'published_at', 'asset_index')})[1:-1]
    intent = {**json.loads(numeric_event_records(runtime, value)[0])['intent'], 'size': '0.01'}
    rows = []
    for number in ('"1e15"', '"1e-29"', '"0.' + '1' * 29 + '"', '"1.00000000000000000000000000000"',
                   '"0"', 'false', '{}', '[]'):
        rows.append('{' + state + ',"timestamp":0,"signal":' + json.dumps(signal) + ',"fill_price":' + number + '}')
        for field in ('entry', 'stop', 'target', 'invalidation'):
            levels = json.dumps({name: item for name, item in signal['levels'].items() if name != field})[1:-1]
            rows.append('{' + state + ',"timestamp":0,"signal":{' + signal_fields
                        + ',"reply_by":0,"asset_index":0,"levels":{' + levels + ',"' + field + '":' + number + '}}}')
        bracket = {name: intent[name] for name in ('request_id', 'asset', 'asset_index', 'delegate_key', 'key_ref')}
        entry = json.dumps({**{name: item for name, item in intent.items()
                              if name not in ('request_id', 'delegate_key', 'key_ref', 'size')}, 'tif': 'Gtc'})[:-1]
        rows.append('{"event":"intent","timestamp":0,"intent":' + json.dumps({**bracket, 'kind': 'bracket'})[:-1]
                    + ',"entry":' + entry + ',"size":' + number + '}}}')
    for number in ('1000000000000000', '-1', 'true', '"0"', 'null'):
        rows.append('{' + state + ',"signal":' + json.dumps(signal) + ',"timestamp":' + number + '}')
        for field in ('claimed_at', 'close_ms'):
            rows.append('{' + state + ',"timestamp":0,"signal":' + json.dumps(signal)
                        + ',"' + field + '":' + ('[]' if number == 'null' else number) + '}')
        for field in ('reply_by', 'published_at', 'asset_index'):
            fields = json.dumps({name: item for name, item in signal.items() if name != field})[:-1]
            rows.append('{' + state + ',"timestamp":0,"signal":' + fields + ',"' + field + '":' + number + '}}')
        rows.append('{"event":"delivery_pending","timestamp":0,"event_id":"sample",'
                    + '"status":429,"not_before":' + number + '}')
    assert len(rows) >= 30
    return rows


def raw_json_event_records(runtime, value, group):
    if group == 'exact numeric ranges':
        return ['{"event":"extra","timestamp":0,"value":' + number + '}'
                for number in ('1e-1000000', '1e400', '9' * 400, '-0', '-0.0')]
    if group == 'exact intent precision':
        intent = ('{"event":"intent","timestamp":0,"intent":{"request_id":"sample-old","kind":"order",'
                  '"asset_index":0,"is_buy":true,"limit_px":"100000","tif":"Ioc",'
                  '"reduce_only":false,"asset":"BTC","delegate_key":' + json.dumps(runtime['config']['delegate_public_key'])
                  + ',"key_ref":' + json.dumps(runtime['config']['key_ref']) + ',"size":')
        return [intent + number + '}}' for number in
                ('0.010000000000000000000000000001', '0.010000000000000000000000000000', '1e-1000000')]
    if group == 'exact integer fields':
        return ['{"event":"extra","timestamp":' + number + '}' for number in ('-0', '1E+2', '0.5', '0.0')]
    if group == 'duplicates':
        return ['{"event":"state","timestamp":0,"key":null,"event":"extra"}',
                '{"event":"extra","timestamp":null,"timestamp":0}',
                '{"event":"extra","timestamp":NaN,"timestamp":0}',
                '{"event":"extra","timestamp":1e1000000,"timestamp":0}']
    if group == 'nested duplicates':
        return ['{"event":"extra","timestamp":0,"nested":' + nested + '}' for nested in
                ('{"key":null,"key":0}', '[{"key":0,"key":0}]',
                 '{"outer":[{"key":null,"ke\\u0079":0}]}')]
    return ['{"event":"extra","timestamp":0,"nested":[{"value":' + constant + '}]}'
            for constant in ('NaN', 'Infinity', '-Infinity')]


async def check_journal_exception_boundary():
    errors = (InvalidOperation, Overflow, OverflowError, ArithmeticError, TypeError, KeyError,
              ValueError, RecursionError, RuntimeError, OSError)
    async with fixture() as (runtime, fake):
        value = envelope('sample')
        await verify_fixture(runtime, value)
        path = Path(runtime['config']['record_path'])
        recorded = path.read_text()
        for error in errors:
            for target, actions in (
                    ('desk.desk_anchor_verify.read_records', ('size', 'gate', 'run')),
                    ('desk.desk_anchor_verify.signed_today', ('size', 'gate', 'run')),
                    ('desk.desk_ticket.signed_today', ('gate',)),
                    ('desk.desk_executor.already_acted', ('run',)),
                    ('desk.desk_executor.signed_today', ('run',)),
                    ('desk.desk_coordinator.signed_today', ('run',)),
                    ('desk.desk_routine.latest_tickets', ('run',))):
                for action in actions:
                    path.write_text(recorded)
                    if target == 'desk.desk_anchor_verify.signed_today':
                        row = json.loads(numeric_event_records(runtime, value)[0])
                        row['intent']['size'] = '0.01'
                        path.write_text(recorded + json.dumps(row) + '\n')
                    with patch(target, side_effect=error('unexpected journal failure')) as reader:
                        result = await command_result(runtime, action, value, verified=False)
                        assert result == {'refused': 'anchor_not_verified'}, (target, error, result)
                        assert reader.called
                    assert not fake['intents'] and fake['prepares'] == 0


async def check_recovery_corruption_refusal():
    async with fixture() as (runtime, fake):
        value = envelope('sample')
        await verify_fixture(runtime, value)
        path = Path(runtime['config']['record_path'])
        recorded = path.read_text()
        for error in (TypeError('unexpected corrupt row'), KeyError('signal'), ValueError('journal_corrupt')):
            with (patch('desk.desk_routine.latest_tickets', side_effect=error) as recovery,
                  patch('desk.desk_executor.maintain_routine', new_callable=AsyncMock) as maintenance,
                  patch('desk.desk_executor.submit', new_callable=AsyncMock) as submission):
                result = await command_result(runtime, 'run', value, verified=False)
                assert result == {'refused': 'anchor_not_verified'}
                recovery.assert_called_once()
                maintenance.assert_not_awaited()
                submission.assert_not_awaited()
            assert path.read_text() == recorded
            assert not fake['intents'] and fake['prepares'] == 0 and not runtime['tickets']


async def check_ambiguous_journal_results():
    for resident in (True, False):
        async with fixture(resident=resident) as (runtime, fake):
            value = envelope('sample')
            if not resident:
                await verify_fixture(runtime, value)

            async def ambiguous_intent(request):
                await coordinator_intent(request)
                raise TimeoutError

            async def arm(runtime, ticket):
                ticket['alive'] = True
                fake['events'].append('watcher_armed')

            with (patch('desk.desk_scenarios_bracket.coordinator_intent', ambiguous_intent),
                  patch('desk.desk_executor.arm', arm)):
                result = await submit(runtime, value)
            assert result == ({'state': 'filled'} if resident else
                              {'state': 'sent', 'reason': 'bracket_outcome_unresolved'}), result
            rows = read_records(runtime['config']['record_path'])
            reply = next(row for row in rows if row['event'] == 'reply')
            assert reply['signed'] is None and reply['ambiguous'] is True
            assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
            if resident:
                assert await cancel(runtime, 'sample') == {'state': 'exited'}
                assert fake['position'] == 0 and len(fake['intents']) == 2
            else:
                outcome = next(row['outcome'] for row in rows if row['event'] == 'bracket_result')
                assert outcome['signed'] is None and outcome['ambiguous'] is True
                assert 'size' in await command_result(runtime, 'size', value, verified=False)
                assert (await command_result(runtime, 'gate', value, verified=False))['status'] == 'PASS'
                runtime['tickets'].clear()
                assert await command_result(runtime, 'run', value, verified=False) == {'refused': 'already_acted'}
                assert fake['prepares'] == 1 and len(fake['intents']) == 1
            assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')


async def main():
    await check_anchor_gate()
    print('PASS anchor_gate: missing or changed payload refused, matching pass proceeds, resident path unchanged', flush=True)
    await check_journal_corruption_order()
    print('PASS anchor_gate corruption: size, gate and run refuse malformed rows before or after a matching pass', flush=True)
    await check_journal_event_contents()
    print('PASS anchor_gate event contents: malformed state, intent and outcome rows refuse without coordinator activity', flush=True)
    await check_recovery_corruption_refusal()
    print('PASS anchor_gate recovery: unexpected corrupt-row exceptions refuse before maintenance or submission', flush=True)
    await check_ambiguous_journal_results()
    print('PASS anchor_gate ambiguous results: null signed replies retain budget, cancellation and routine recovery', flush=True)
    await check_journal_event_contents(numeric_event_records)
    print('PASS anchor_gate numeric bounds: extreme numbers and deep objects refuse in either journal order', flush=True)
    await check_journal_event_contents(substituted_event_records)
    print('PASS anchor_gate substitutions: 83 malformed field combinations refuse size, gate and run', flush=True)
    for group in ('duplicates', 'nested duplicates', 'constants',
                  'exact numeric ranges', 'exact intent precision', 'exact integer fields'):
        await check_journal_event_contents(partial(raw_json_event_records, group=group))
        print('PASS anchor_gate ' + group + ': raw rows refuse size, gate, run and direct submit in either journal order', flush=True)
    await check_journal_exception_boundary()
    print('PASS anchor_gate exceptions: journal reads, budget use and recovery fail closed without coordinator activity', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
