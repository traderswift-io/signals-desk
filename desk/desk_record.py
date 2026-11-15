import ctypes
import fcntl
import json
import time
from decimal import Decimal
from pathlib import Path


def sync_file(stream):
    stream.flush()
    if ctypes.CDLL(None).fsync(stream.fileno()) != 0:
        raise OSError('record_sync_failed')


def now_ms():
    return time.time_ns() // 1000000


def append_record(path, event, **values):
    line = json.dumps({'event': event, 'timestamp': now_ms(), **values}, default=str, sort_keys=True, separators=(',', ':'))
    with Path(path).open('a', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(line + '\n')
        sync_file(stream)


def read_records(path):
    target = Path(path)
    if not target.exists():
        return []
    with target.open(encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return [json.loads(line) for line in stream.read().splitlines() if line]


def ticket_key(config, signal):
    return [config['account_address'], signal['publisher'], signal['signal_id']]


def already_acted(path, key):
    return any(row.get('event') == 'state' and row['key'][0] == key[0] and row['key'][2] == key[2] for row in read_records(path))


def set_state(config, ticket, state, **values):
    details = {'claimed_at': ticket['claimed_at'], 'fill_price': ticket.get('fill_price'),
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


def signed_today(config, asset, timestamp=None):
    timestamp = now_ms() if timestamp is None else timestamp
    rows = read_records(config['record_path'])
    outcomes = {row['request_id']: row for row in rows if row['event'] == 'reply'}
    total = Decimal(0)
    intents = {row['intent']['request_id']: row for row in rows if row['event'] == 'intent'}
    for row in intents.values():
        intent = row['intent']
        if intent['kind'] != 'order' or intent['reduce_only'] or intent['asset'] != asset:
            continue
        if intent['delegate_key'] != config['delegate_public_key'] or intent['key_ref'] != config['key_ref']:
            continue
        outcome = outcomes.get(intent['request_id'], {})
        if outcome.get('signed') is False:
            continue
        first_day = row['timestamp'] // 86400000
        last_day = outcome.get('timestamp', timestamp) // 86400000
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
    append_record(path, 'review', key=ticket['key'], signal_entry=entry, fill_price=fill,
                  slippage=(Decimal(fill) - entry) * direction if fill is not None else None,
                  stop_hit=ticket.get('stop_hit', False), stop='software', description='stop: software')


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
    stream = Path(config['record_path'] + '.lock').open('a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise ValueError('executor_already_running') from None
    return stream
