import asyncio
import copy
import json
from logging import getLogger
from decimal import Decimal
from os import umask
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from .desk_delivery_store import append_delivery_record, prepare_delivery_store, preserve_tail
from .desk_executor import recover, submit
from .desk_keygen import generate_key
from .desk_private_storage import private_directory, private_open
from .desk_record import append_record, journal_guard, read_records, review, writer_lock
from .desk_scenarios_bracket import command_result, envelope, fixture
from .desk_scenarios_budget_identity import spent_journal
from .desk_scenarios_fakes import record_anchor_fixture


def require_permission_refusal(operation):
    try:
        operation()
    except ValueError as error:
        assert str(error).startswith('insecure_permissions: '), str(error)
        assert not Path(str(error).split(': ', 1)[1]).is_absolute(), str(error)
    else:
        raise AssertionError('Loose storage permissions accepted')


def check_storage_permissions():
    for mask in (0o022, 0o000):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            previous = umask(mask)
            try:
                path = root / 'state' / 'nested' / 'journal.jsonl'
                config = {'record_path': str(path), '_config_directory': str(root)}
                append_record(path, 'extra')
                with writer_lock(config):
                    pass
                prepare_delivery_store(config)
                delivery = Path(str(path) + '.deliveries.jsonl')
                append_delivery_record(delivery, 'delivery_reported', event_id='sample')
                preserve_tail(delivery, b'incomplete')
                key_directory = root / 'keys'
                with patch('desk.desk_keygen.SigningKey.generate', side_effect=LookupError('no_key_needed')):
                    try:
                        generate_key({'delegate_key_file': str(key_directory / 'unused')})
                    except LookupError as error:
                        assert str(error) == 'no_key_needed'
                for parent in (path.parent, path.parent.parent, key_directory):
                    assert parent.stat().st_mode & 0o777 == 0o700
                for target in (path, Path(str(path) + '.lock'), delivery, Path(str(delivery) + '.torn')):
                    assert target.stat().st_mode & 0o777 == 0o600
                with private_open(key_directory / 'creation-probe', 'x') as stream:
                    stream.write('placeholder\n')
                assert (key_directory / 'creation-probe').stat().st_mode & 0o777 == 0o600
                config['delegate_key_file'] = str(key_directory / 'unused')
                targets = (path, Path(str(path) + '.lock'), delivery, Path(str(delivery) + '.torn'))
                parents = (path.parent.parent, path.parent, key_directory)
                originals = {target: target.read_bytes() for target in targets}
                for target in targets:
                    target.chmod(0o644)
                for parent in parents:
                    parent.chmod(0o755)
                logger = getLogger('desk.desk_private_storage')
                with patch.object(logger, 'warning') as logged:
                    with writer_lock(config):
                        pass
                    assert logged.call_count == len(targets) + len(parents)
                    messages = [call.args[0] % call.args[1:] for call in logged.call_args_list]
                    for target in targets + parents:
                        old, new = ('0755', '0700') if target in parents else ('0644', '0600')
                        assert ('storage_permissions path=' + str(target.relative_to(root)) +
                                ' old=' + old + ' new=' + new) in messages
                    logged.reset_mock()
                    with writer_lock(config):
                        pass
                    logged.assert_not_called()
                for target in targets:
                    assert target.read_bytes() == originals[target] and target.stat().st_mode & 0o777 == 0o600
                for parent in parents:
                    assert parent.stat().st_mode & 0o777 == 0o700
                for target in targets + parents:
                    mode = 0o755 if target in parents else 0o644
                    target.chmod(mode)
                    with patch('desk.desk_private_storage.fchmod', side_effect=PermissionError):
                        require_permission_refusal(lambda: writer_lock(config))
                    assert target.stat().st_mode & 0o777 == mode
                    target.chmod(0o700 if target in parents else 0o600)
                with patch('desk.desk_private_storage.geteuid', return_value=root.stat().st_uid + 1):
                    require_permission_refusal(lambda: writer_lock(config))
                path.chmod(0o644)
                require_permission_refusal(lambda: read_records(path))
                require_permission_refusal(lambda: append_record(path, 'extra'))
                assert path.read_bytes() == originals[path]
                path.chmod(0o600)
                key_directory.chmod(0o755)
                with (patch('desk.desk_keygen.SigningKey.generate', side_effect=LookupError('no_key_needed')),
                      patch.object(logger, 'warning') as logged):
                    try:
                        generate_key(config)
                    except LookupError:
                        pass
                    assert key_directory.stat().st_mode & 0o777 == 0o700
                    logged.assert_called_once()
                key_directory.chmod(0o755)
                with (patch('desk.desk_private_storage.fchmod', side_effect=PermissionError),
                      patch('desk.desk_keygen.SigningKey.generate') as signing):
                    require_permission_refusal(lambda: generate_key(config))
                    signing.assert_not_called()
                key_directory.chmod(0o700)
            finally:
                umask(previous)


async def observe_recovered(runtime, fake, value, contents, action, initial_fake):
    path = Path(runtime['config']['record_path'])
    path.write_text(contents)
    runtime['tickets'].clear()
    fake.clear()
    fake.update(copy.deepcopy(initial_fake))
    if action == 'submit':
        with journal_guard(runtime['config']) as recovery_refusal:
            await recover(runtime)
        result = await submit(runtime, value)
        if recovery_refusal:
            assert result == recovery_refusal
    else:
        result = await command_result(runtime, action, value, verified=False)
    activity = (fake['prepares'] - initial_fake['prepares'], len(fake['intents']) - len(initial_fake['intents']))
    return result, activity


def writer_mutations(rows):
    bracket = next(index for index, row in enumerate(rows) if row['event'] == 'intent')
    reply = next(index for index, row in enumerate(rows) if row['event'] == 'reply')
    state = next(index for index, row in enumerate(rows) if row['event'] == 'state')
    outcome = next(index for index, row in enumerate(rows) if row['event'] == 'bracket_result')
    variants = [(bracket, ('intent', 'stop'), None), (bracket, ('intent', 'stop', 'size'), []),
                (reply, ('ambiguous',), 'false'), (bracket, ('intent', 'take_profit'), None),
                (bracket, ('intent', 'take_profit'), []), (reply, ('signed',), 'true'),
                (outcome, ('outcome', 'ambiguous'), 'false'), (outcome, ('outcome', 'intent'), []),
                (state, ('entry_done',), 'false'), (state, ('reason',), []),
                (reply, ('venue_answer',), []), (reply, ('reason',), []),
                (reply, ('venue_status',), False), (reply, ('signature',), []),
                (bracket, ('rearm_intent',), []), (bracket, ('intent', 'expires_at'), '1')]
    for leg in ('entry', 'stop', 'take_profit'):
        for field, value in rows[bracket]['intent'][leg].items():
            variants.append((bracket, ('intent', leg, field), 'false' if type(value) is bool else []))
    seen = set()
    for index, row in enumerate(rows):
        pending = [((), row)]
        while pending:
            keys, value = pending.pop()
            identity = (row['event'], keys, type(value))
            if keys and identity not in seen:
                seen.add(identity)
                variants.append((index, keys, {} if type(value) is list else []))
            if type(value) is dict:
                pending.extend((keys + (key,), item) for key, item in value.items())
            elif type(value) is list:
                pending.extend((keys + (key,), item) for key, item in enumerate(value))
    for index, keys, replacement in variants:
        changed = copy.deepcopy(rows)
        target = changed[index]
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = replacement
        yield keys, ''.join(json.dumps(row) + '\n' for row in changed)


def check_review_writer():
    with TemporaryDirectory() as directory:
        config = {'record_path': str(Path(directory) / 'journal.jsonl')}
        for side in ('long', 'short'):
            for price in ('100000', '100001', '99999'):
                ticket = {'key': ['sample', 'sample', side + price],
                          'signal': {'side': side, 'levels': {'entry': '100000'}}, 'fill_price': price}
                review(config, ticket)
        rows = read_records(config['record_path'])
        assert [row['slippage'] for row in rows] == ['0', '1', '-1', '-0', '-1', '1']
        for row in rows:
            assert type(row['slippage']) is str
        for invalid in ('NaN', 'Infinity', '1e16', [], {}, False):
            append_record(config['record_path'], 'review', key=['sample', 'sample', 'invalid'],
                          signal_entry='100000', fill_price='100000', slippage=invalid,
                          stop_hit=False, stop='software', description='stop: software')
            try:
                read_records(config['record_path'])
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid review slippage accepted')
            Path(config['record_path']).write_text(''.join(json.dumps(row) + '\n' for row in rows))


async def check_writer_corruption():
    check_review_writer()
    async with fixture() as (runtime, fake):
        value, original, cancellation, before_pass, passed = await spent_journal(runtime, fake, 'bracket')
        runtime['config']['policy']['asset_caps'][0]['max_daily_size'] = '0.03'
        clean = before_pass + passed
        rows = [json.loads(line) for line in clean.splitlines()]
        initial_fake = copy.deepcopy(fake)
        for action in ('size', 'gate', 'run', 'submit'):
            result, activity = await observe_recovered(runtime, fake, value, clean, action, initial_fake)
            if action == 'size':
                assert Decimal(result['size']) == Decimal('0.01') and activity == (0, 0)
            elif action == 'gate':
                assert result['status'] == 'PASS' and activity == (0, 0)
            else:
                assert result == {'state': 'sent'} and activity == (1, 1), (action, result, activity)
        for keys, changed in writer_mutations(rows):
            for action in ('size', 'gate', 'run', 'submit'):
                result = await observe_recovered(runtime, fake, value, changed, action, initial_fake)
                assert result == ({'refused': 'anchor_not_verified'}, (0, 0)), (keys, action, result)


async def check_journal_tail():
    async with fixture() as (runtime, fake):
        value = envelope('tail')
        record_anchor_fixture(runtime['config'], value)
        path = Path(runtime['config']['record_path'])
        clean = path.read_text()
        initial_fake = copy.deepcopy(fake)
        for changed in (clean[:-1], clean + '{"event":"extra","timestamp":0}',
                        clean + '{"event":', clean + ' \t', clean + ' \t\n', clean + '\n',
                        clean[:-1] + '\r', clean[:-1] + '\r' + clean):
            for action in ('size', 'gate', 'run', 'submit'):
                path.write_text(changed)
                try:
                    read_records(path)
                except ValueError:
                    pass
                else:
                    raise AssertionError('Torn or empty record accepted')
                result = await observe_recovered(runtime, fake, value, changed, action, initial_fake)
                assert result == ({'refused': 'anchor_not_verified'}, (0, 0)), (changed, action, result)
                if not changed.endswith('\n'):
                    assert path.read_bytes() == changed.encode()
                assert await submit(runtime, value) == {'refused': 'anchor_not_verified'}
                assert fake['prepares'] == initial_fake['prepares'] and fake['intents'] == initial_fake['intents']
        path.write_text(clean)
        assert len(read_records(path)) == 1


async def main():
    check_storage_permissions()
    print('PASS private storage: private creation, logged startup tightening and inaccessible storage refusal', flush=True)
    await check_writer_corruption()
    print('PASS writer field corruption: all four paths refuse mutations; clean controls still send', flush=True)
    await check_journal_tail()
    print('PASS journal tail: unterminated, truncated, whitespace and empty records refuse all four paths', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
