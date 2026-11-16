import asyncio
import copy
import io
import json
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from .desk_journal_schema import EVENT_FIELDS
from .desk_exit_pacing import MAX_STATE_TIMESTAMP, STATE_INTEGER_LIMITS
from .desk_record import read_records, signed_today
from .desk_scenarios_bracket import envelope, fixture
from .desk_scenarios_budget_identity import spent_journal
from .desk_scenarios_fakes import record_anchor_fixture
from .desk_scenarios_journal import observe_recovered
from .desk_scenarios_journal_schema import check_static_coverage
from .desk_scenarios_writer_history import collect_writer_histories


def serialize(rows):
    return ''.join(json.dumps(row) + '\n' for row in rows)


def nested_fields(value, path=()):
    if type(value) is dict:
        for name, item in value.items():
            yield path + (name,), item
            yield from nested_fields(item, path + (name,))
    elif type(value) is list:
        for index, item in enumerate(value):
            yield path + (index,), item
            yield from nested_fields(item, path + (index,))


def replace_field(rows, index, keys, replacement):
    changed = copy.deepcopy(rows)
    target = changed[index]
    for name in keys[:-1]:
        target = target[name]
    target[keys[-1]] = replacement
    return serialize(changed)


def integer_boundaries(rows, index):
    row = rows[index]
    neighbours = [item for item in rows if item['event'] == 'state' and item['key'] == row['key']]
    position = sum(item['event'] == 'state' and item['key'] == row['key'] for item in rows[:index])
    previous = dict(nested_fields(neighbours[position - 1])) if position else {}
    following = dict(nested_fields(neighbours[position + 1])) if position + 1 < len(neighbours) else {}
    for keys, original in nested_fields(row):
        if type(original) is not int:
            continue
        maximum = STATE_INTEGER_LIMITS.get(keys[0], MAX_STATE_TIMESTAMP) if len(keys) == 1 else MAX_STATE_TIMESTAMP
        replacements = {0, maximum + 1, 10 ** 30, row['timestamp'] + 1}
        for values, offset in ((previous, -1), (following, 1)):
            value = values.get(keys, original)
            if type(value) is int:
                replacements.add(value + offset)
        for replacement in sorted(replacements - {original}):
            yield keys, replacement


async def require_corruption(runtime, fake, value, contents, initial_fake, label):
    Path(runtime['config']['record_path']).write_text(contents)
    try:
        read_records(runtime['config']['record_path'])
    except ValueError as error:
        assert str(error) == 'journal_corrupt', (label, error)
    else:
        raise AssertionError(('Corrupt history accepted', label))
    for action in ('size', 'gate', 'run', 'submit'):
        result = await observe_recovered(runtime, fake, value, contents, action, initial_fake)
        assert result == ({'refused': 'anchor_not_verified'}, (0, 0)), (label, action, result)


async def check_history_consistency():
    count = 0
    async with fixture() as (runtime, fake):
        value, original, cancellation, before_pass, passed = await spent_journal(runtime, fake, 'bracket')
        clean = before_pass + passed
        rows = [json.loads(line) for line in clean.splitlines()]
        initial_fake = copy.deepcopy(fake)
        assert any(row['event'] == 'state' and row['state'] == 'exited' for row in rows)
        assert signed_today(runtime['config'], 'BTC') == Decimal('0.01')
        for action in ('size', 'gate', 'run', 'submit'):
            result, activity = await observe_recovered(runtime, fake, value, clean, action, initial_fake)
            assert activity == (0, 0)
            assert (Decimal(result['size']) == Decimal('0.01') if action == 'size'
                    else result.get('refused') == 'over_max_daily_size'), (action, result)
        reply = next(index for index, row in enumerate(rows) if row['event'] == 'reply')
        outcome = next(index for index, row in enumerate(rows) if row['event'] == 'bracket_result')
        cases = [(reply, ('signed',), False), (reply, ('ambiguous',), True),
                 (outcome, ('request_id',), 'different-request'),
                 (outcome, ('outcome', 'signed'), False), (outcome, ('outcome', 'ambiguous'), True),
                 (outcome, ('outcome', 'intent', 'entry', 'limit_px'), '99999')]
        for keys, item in nested_fields(rows[outcome]['outcome']['intent']):
            replacement = (not item if type(item) is bool else item + 1 if type(item) is int
                           else item + '-changed' if type(item) is str else [])
            cases.append((outcome, ('outcome', 'intent') + keys, replacement))
        for index, keys, replacement in cases:
            await require_corruption(runtime, fake, value, replace_field(rows, index, keys, replacement),
                                     initial_fake, (rows[index]['event'], keys))
            count += 1
    return count


def legal_replacement(row, keys, replacement):
    nullable = {'fill_price', 'slippage', 'close_ms', 'entry_cancel_reason', 'hl_oracle_px',
                'hl_oracle_timestamp', 'binance_px', 'binance_timestamp', 'signed', 'venue_answer',
                'backstop_px', 'closing', 'last_exit_action_ms', 'exit_action_cost', 'action_budget_low', 'exit_fast', 'exit_by'}
    if replacement is None:
        return (keys[-1] in nullable or keys[-1] == 'target'
                or keys == ('reason',) and row['event'] == 'gate'
                or keys == ('request_id',) and row['event'] == 'prepare_reply')
    original = row
    for key in keys:
        original = original[key]
    if keys in (('action_budget_low',), ('exit_fast',)) and type(replacement) is bool:
        return True
    if keys in (('last_exit_action_ms',), ('exit_action_cost',)) and type(replacement) is int:
        return False
    if (keys[-1] in ('close_ms', 'hl_oracle_timestamp', 'binance_timestamp')
            and type(replacement) is int):
        return True
    if keys == ('seconds',) and type(replacement) is int:
        return True
    if keys[-2:-1] == ('levels',) and type(replacement) is int:
        return True
    if keys[-1] == 'signed' and type(replacement) is bool:
        return True
    if type(original) is bool and type(replacement) is bool:
        return True
    if type(original) in (int, Decimal) and type(replacement) is int:
        return True
    if keys[-1] in ('raw', 'thesis', 'horizon') and replacement == '':
        return True
    if type(original) is list and replacement == []:
        return keys[-1] in ('orders', 'fills', 'links', 'statuses')
    if type(original) is dict and replacement == {}:
        return keys[-1] in ('venue_answer', 'data')
    return False


async def check_all_writer_fields():
    with redirect_stdout(io.StringIO()):
        histories = await collect_writer_histories()
    expected = set(EVENT_FIELDS) - {'extra', 'verification_requested'}
    assert set(histories) == expected, ('Writer events missing', expected - set(histories))
    count = 0
    covered = set()
    async with fixture() as (runtime, fake):
        value = envelope('schema-next')
        record_anchor_fixture(runtime['config'], value)
        passed = Path(runtime['config']['record_path']).read_text()
        initial_fake = copy.deepcopy(fake)
        for event, snapshots in histories.items():
            for snapshot in snapshots:
                rows = [json.loads(line) for line in snapshot.splitlines()]
                index = len(rows) - 1
                row = rows[index]
                if event == 'refusal' and row.get('reason') == 'signal_invalidated':
                    result = await observe_recovered(runtime, fake, value, snapshot + passed, 'submit', initial_fake)
                    assert result == ({'state': 'sent'}, (1, 1)), result
                for keys, original in nested_fields(row):
                    identity = (event, keys, type(original))
                    if identity in covered:
                        continue
                    covered.add(identity)
                    for replacement in ([], {}, None, 7, True, ''):
                        if legal_replacement(row, keys, replacement):
                            continue
                        changed = replace_field(rows, index, keys, replacement) + passed
                        await require_corruption(runtime, fake, value, changed, initial_fake, (event, keys, replacement))
                        count += 1
        assert ('refusal', ('signal_id',), str) in covered
    return count, len(covered)


async def main():
    sites = check_static_coverage()
    print('PASS journal writer coverage: ' + str(sites) + ' AST call sites; unlisted fields and events fail', flush=True)
    count = await check_history_consistency()
    print('PASS journal history: ' + str(count) + ' terminal reply and nested intent contradictions refuse four paths', flush=True)
    count, fields = await check_all_writer_fields()
    print('PASS journal field matrix: refusal signal_id and ' + str(fields) + ' writer fields; '
          + str(count) + ' malformed mutations refuse four paths', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
