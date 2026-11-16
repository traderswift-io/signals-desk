from .desk_journal_shapes import require_decimal, require_fields, validate_record as validate_record_details


def text_field(value):
    if type(value) is not str or not value.strip():
        raise ValueError('journal_corrupt')


def integer_field(value):
    require_fields({'value': value}, {'value': int})


def number_field(value):
    require_decimal(value, positive=False)


def decimal_text_field(value):
    text_field(value)
    require_decimal(value, positive=False)


def price_field(value):
    text_field(value)
    require_decimal(value)


def slippage_field(value):
    text_field(value)
    require_decimal(value, positive=False, allow_signed_zero=True)


def key_field(value):
    if type(value) is not list or len(value) != 3:
        raise ValueError('journal_corrupt')
    for item in value:
        text_field(item)


def delivery_status(value):
    if type(value) is int and 100 <= value <= 599:
        return
    if type(value) is str and value in ('queued', 'transport_error', 'timeout'):
        return
    raise ValueError('journal_corrupt')


def consumed_result(value):
    require_fields(value, {})
    if not value or set(value) - {'state', 'refused', 'reason', 'signal_id'}:
        raise ValueError('journal_corrupt')
    for item in value.values():
        text_field(item)


def nullable(check):
    def check_optional(value):
        if value is not None:
            check_field(value, check)
    return check_optional


def check_field(value, check):
    if isinstance(check, type):
        if type(value) is not check:
            raise ValueError('journal_corrupt')
    else:
        check(value)


COMMON_FIELDS = {'event': text_field, 'timestamp': integer_field}
PRICE_FIELDS = {source + suffix: check for source in ('hl_oracle', 'binance')
                for suffix, check in (('_px', nullable(price_field)), ('_timestamp', nullable(integer_field)),
                                      ('_error', text_field))}
REPLY_FIELDS = {'request_id': nullable(text_field), 'signed': nullable(bool), 'ambiguous': bool,
                'reason': text_field, 'venue_status': text_field, 'signature': text_field, 'prepared': bool,
                'key_ref': text_field, 'policy_epoch': integer_field, 'venue_nonce': integer_field,
                'expires_at': integer_field, 'intent': dict, 'rearm_intent': dict, 'venue_answer': nullable(dict)}
DELIVERY_FIELDS = {'event_id': text_field, 'signal_id': text_field, 'status': delivery_status,
                   'attempts': integer_field, 'next_attempt': integer_field, 'not_before': integer_field}
RESULT_FIELDS = {'key': key_field, 'outcome': dict, 'request_id': text_field, 'reason': text_field,
                 'close_ms': nullable(integer_field), 'oid': integer_field}
EVENT_FIELDS = {
    'anchor_verified': {'signal_id': text_field, 'payload_hash': text_field},
    'state': {'key': key_field, 'state': text_field, 'signal': dict, 'claimed_at': integer_field,
              'fill_price': nullable(price_field), 'entry_done': bool, 'close_ms': nullable(integer_field),
              'entry_cancel_reason': nullable(text_field), 'stop_hit': bool, 'reason': text_field},
    'intent': {'key': key_field, 'intent': dict, 'rearm_intent': dict, **PRICE_FIELDS},
    'reply': {'key': key_field, **REPLY_FIELDS},
    'prepare_reply': {'key': key_field, **REPLY_FIELDS},
    'bracket_result': RESULT_FIELDS,
    'cancel_result': RESULT_FIELDS,
    'exit_result': RESULT_FIELDS,
    'refusal': {'signal_id': text_field, 'reason': text_field},
    'review': {'key': key_field, 'signal_entry': price_field, 'fill_price': nullable(price_field),
               'slippage': nullable(slippage_field), 'stop_hit': bool, 'stop': text_field, 'description': text_field},
    'gate': {'key': key_field, 'reason': nullable(text_field), 'sizing': dict,
             'entry_price': price_field, 'intent': dict},
    'reconcile': {'key': key_field, 'orders': list, 'fills': list, 'position': decimal_text_field},
    'oracle_refusal': {'key': key_field, **PRICE_FIELDS},
    'backoff': {'key': key_field, 'route': text_field, 'seconds': number_field, 'reason': text_field, 'retry': bool},
    'nonce_retry': {'key': key_field, 'reason': text_field},
    'bracket_refused': {'key': key_field, 'reason': text_field},
    'recovery_required': {'key': key_field, 'reason': text_field},
    'redelivery': {'key': key_field, 'reason': text_field},
    'watcher_armed': {'key': key_field},
    'watcher_disconnected': {'key': key_field},
    'watcher_restarted': {'key': key_field},
    'cursor': {'stream': text_field, 'stream_id': text_field},
    'consumed': {'stream_id': text_field, 'result': consumed_result},
    'stream_refusal': {'stream_id': text_field, 'reason': text_field},
    'stream_disconnected': {'stream_id': text_field},
    'deliveries_disabled': {'reason': text_field, 'exception': text_field},
    'delivery_error': {'reason': text_field},
    'delivery_pending': DELIVERY_FIELDS,
    'delivery_reported': DELIVERY_FIELDS,
    'delivery_abandoned': DELIVERY_FIELDS,
    'delivery_tail_truncated': {'removed_bytes': integer_field},
    'verification_requested': {'signal_id': text_field, 'payload_hash': text_field},
    'extra': {'value': require_decimal},
}


def validate_record(record):
    require_fields(record, {'event': str, 'timestamp': int})
    fields = EVENT_FIELDS.get(record['event'])
    if fields is None:
        raise ValueError('journal_corrupt')
    fields = {**COMMON_FIELDS, **fields}
    if set(record) - fields.keys():
        raise ValueError('journal_corrupt')
    for name, value in record.items():
        check_field(value, fields[name])
    if 'outcome' in record:
        outcome = record['outcome']
        if not outcome or 'signed' not in outcome or set(outcome) - REPLY_FIELDS.keys():
            raise ValueError('journal_corrupt')
        for name, value in outcome.items():
            legacy = record['event'] == 'bracket_result' and 'request_id' not in record
            if legacy and (name == 'intent' and value is None or name == 'venue_answer' and value == []):
                continue
            check_field(value, REPLY_FIELDS[name])
    validate_record_details(record)
