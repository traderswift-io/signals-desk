import ctypes
import fcntl
import json
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from .desk_journal_identity import intent_records as first_intent_records, reply_records, validate_history
from .desk_journal_evidence import reply_evidence, validate_execution_history
from .desk_journal_schema import validate_record
from .desk_journal_shapes import require_decimal
from .desk_private_storage import prepare_private_storage, private_open


JSON_MAX_INTEGER_DIGITS = 4300


@contextmanager
def journal_guard(config):
    refusal = {}
    try:
        yield refusal
    except Exception:
        refusal['refused'] = 'anchor_not_verified'


def sync_file(stream):
    stream.flush()
    if ctypes.CDLL(None).fsync(stream.fileno()) != 0:
        raise OSError('record_sync_failed')


def now_ms():
    return time.time_ns() // 1000000


def append_record(path, event, **values):
    record = journal_number_strings({'event': event, 'timestamp': now_ms(), **values})
    line = json.dumps(record, default=str, allow_nan=False, sort_keys=True, separators=(',', ':'))
    with private_open(path, 'a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0, 2)
        if stream.tell():
            stream.seek(-1, 2)
            if stream.read(1) != b'\n':
                raise ValueError('journal_torn_tail')
        stream.write((line + '\n').encode('utf-8'))
        sync_file(stream)


def journal_number_strings(value):
    if type(value) is float:
        number = Decimal(str(value))
        require_decimal(number, positive=False)
        return str(number)
    if isinstance(value, dict):
        return {key: journal_number_strings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [journal_number_strings(item) for item in value]
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('non_json_constant')


def exact_integer(text):
    if len(text.lstrip('-')) > JSON_MAX_INTEGER_DIGITS:
        raise ValueError('integer_too_long')
    return Decimal(text) if text == '-0' else int(text)


def decode_json(text):
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant,
                      parse_float=Decimal, parse_int=exact_integer)


def read_records(path):
    target = Path(path)
    if not target.exists():
        return []
    with private_open(target, 'r') as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        contents = stream.read()
        if contents and not contents.endswith('\n'):
            raise ValueError('journal_torn_tail')
        records = [decode_json(line) for line in contents.split('\n')[:-1]]
    for record in records:
        validate_record(record)
    validate_history(records)
    return records


def ticket_key(config, signal):
    return [config['account_address'], signal['publisher'], signal['signal_id']]


def already_acted(path, key):
    return any(row.get('event') == 'state' and row['key'][0] == key[0] and row['key'][2] == key[2] for row in read_records(path))


def set_state(config, ticket, state, **values):
    details = {'claimed_at': ticket['claimed_at'], 'fill_price': ticket.get('fill_price'),
               'entry_done': ticket.get('entry_done', False), 'close_ms': ticket.get('close_ms'),
               'entry_cancel_reason': ticket.get('entry_cancel_reason'),
               'stop_hit': ticket.get('stop_hit', False), **values}
    append_record(config['record_path'], 'state', key=ticket['key'], state=state, signal=ticket['signal'], **details)
    ticket['state'] = state
    if state in ('refused', 'exited', 'not_filled') and ticket.get('done'):
        ticket['done'].set()


def latest_tickets(config):
    latest = {}
    for row in read_records(config['record_path']):
        if row['event'] == 'state' and row['key'][0] == config['account_address']:
            latest[tuple(row['key'])] = row
    return latest


def bracket_result_agrees(records, result):
    request_id = result.get('request_id')
    if not isinstance(request_id, str) or not request_id:
        return False
    intent = first_intent_records(records).get(request_id)
    reply = reply_records(records).get(request_id)
    if (intent is None or intent.get('key') != result['key']
            or intent['intent']['kind'] != 'bracket' or reply is None):
        return False
    outcome = result['outcome']
    return (outcome.get('signed') is reply.get('signed')
            and outcome.get('ambiguous', False) is reply.get('ambiguous', False))


def signed_today(config, asset, timestamp=None, records=None):
    timestamp = now_ms() if timestamp is None else timestamp
    rows = read_records(config['record_path']) if records is None else records
    validate_execution_history(rows)
    outcomes = reply_records(rows)
    total = Decimal(0)
    intents = first_intent_records(rows)
    for row in intents.values():
        intent = row['intent']
        if intent['kind'] == 'bracket':
            intent = {**intent, **intent['entry']}
        if intent['kind'] != 'order' or intent['reduce_only'] or intent['asset'] != asset:
            continue
        if intent['delegate_key'] != config['delegate_public_key'] or intent['key_ref'] != config['key_ref']:
            continue
        outcome = outcomes.get(intent['request_id'], {})
        signed = reply_evidence(outcome)[0] if outcome else None
        if signed is False:
            continue
        first_day = row['timestamp'] // 86400000
        last_day = (outcome['timestamp'] if signed is True else timestamp) // 86400000
        if not first_day <= timestamp // 86400000 <= last_day:
            continue
        total += Decimal(intent['size'])
    return total


def review(config, ticket):
    path = config['record_path']
    if any(row['event'] == 'review' and row['key'] == ticket['key'] for row in read_records(path)):
        return
    entry = Decimal(ticket['signal']['levels']['entry'])
    fill = ticket.get('fill_price')
    direction = Decimal(1) if ticket['signal']['side'] == 'long' else Decimal(-1)
    stop_mode = 'software' if config.get('resident', True) else 'bracket'
    append_record(path, 'review', key=ticket['key'], signal_entry=entry, fill_price=fill,
                  slippage=(Decimal(fill) - entry) * direction if fill is not None else None,
                  stop_hit=ticket.get('stop_hit', False), stop=stop_mode, description='stop: ' + stop_mode)


def reader_cursor(config):
    rows = [row for row in read_records(config['record_path']) if row['event'] == 'cursor' and row['stream'] == config['redis']['stream']]
    return rows[-1]['stream_id'] if rows else '0-0'


def delivery_path(config):
    return config['record_path'] + '.deliveries.jsonl'


def delivery_state(config, records=None):
    pending, abandoned, not_before = {}, set(), 0
    for row in read_records(delivery_path(config)) if records is None else records:
        if row['event'] == 'delivery_pending':
            if row['event_id'] not in abandoned:
                pending[row['event_id']] = row
        elif row['event'] in ('delivery_reported', 'delivery_abandoned'):
            pending.pop(row['event_id'], None)
            if row['event'] == 'delivery_abandoned':
                abandoned.add(row['event_id'])
        if row['event'].startswith('delivery_') and row.get('status') == 429:
            not_before = max(not_before, row.get('not_before', 0))
    return pending, abandoned, not_before


def pending_deliveries(config):
    return list(delivery_state(config)[0].values())


def writer_lock(config):
    prepare_private_storage(config)
    stream = private_open(config['record_path'] + '.lock', 'a', base=config.get('_config_directory'))
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise ValueError('executor_already_running') from None
    return stream
