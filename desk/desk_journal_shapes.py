from decimal import Decimal, InvalidOperation


JOURNAL_MAGNITUDE_LIMIT = Decimal('1e15')
JOURNAL_MAX_DIGITS = 28
JOURNAL_MIN_EXPONENT = -28
JOURNAL_MAX_EXPONENT = 14
JOURNAL_MAX_NUMBER_LENGTH = 64
JOURNAL_MAX_DEPTH = 32


def require_fields(value, fields):
    if not isinstance(value, dict):
        raise ValueError('journal_corrupt')
    for name, kind in fields.items():
        if type(value.get(name)) is not kind or kind is str and not value[name]:
            raise ValueError('journal_corrupt')
        if kind is int:
            require_decimal(value[name], positive=False)
            if value[name] < 0:
                raise ValueError('journal_corrupt')


def require_decimal(value, positive=True, allow_signed_zero=False):
    try:
        if type(value) not in (str, int, Decimal) or len(str(value)) > JOURNAL_MAX_NUMBER_LENGTH:
            raise ValueError('journal_corrupt')
        number = Decimal(value)
        if (not number.is_finite() or positive and number <= 0
                or number.is_zero() and number.is_signed() and not allow_signed_zero
                or number.copy_abs() >= JOURNAL_MAGNITUDE_LIMIT
                or len(number.as_tuple().digits) > JOURNAL_MAX_DIGITS
                or not JOURNAL_MIN_EXPONENT <= number.as_tuple().exponent <= JOURNAL_MAX_EXPONENT
                or number and not JOURNAL_MIN_EXPONENT <= number.adjusted() <= JOURNAL_MAX_EXPONENT):
            raise ValueError('journal_corrupt')
    except InvalidOperation:
        raise ValueError('journal_corrupt') from None


def validate_record_tree(record):
    pending = [(record, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > JOURNAL_MAX_DEPTH:
            raise ValueError('journal_corrupt')
        if type(value) in (int, float, Decimal):
            require_decimal(value, positive=False)
        elif isinstance(value, (dict, list)):
            pending.extend((item, depth + 1) for item in (value.values() if isinstance(value, dict) else value))


def validate_record_key(record):
    key = record.get('key')
    if not isinstance(key, list) or len(key) != 3 or any(not isinstance(part, str) or not part for part in key):
        raise ValueError('journal_corrupt')


def validate_state_record(record):
    validate_record_key(record)
    require_fields(record, {'state': str, 'signal': dict})
    if record['state'] not in ('claimed', 'sent', 'filled', 'refused', 'exited', 'not_filled'):
        raise ValueError('journal_corrupt')
    signal = record['signal']
    require_fields(signal, {'signal_id': str, 'publisher': str, 'asset': str, 'asset_index': int,
                            'side': str, 'reply_by': int, 'levels': dict})
    if signal['side'] not in ('long', 'short') or record['key'][1:] != [signal['publisher'], signal['signal_id']]:
        raise ValueError('journal_corrupt')
    for name in ('entry', 'stop'):
        require_decimal(signal['levels'].get(name))
    for name in ('target', 'invalidation'):
        if name in signal['levels'] and (name != 'target' or signal['levels'][name] is not None):
            require_decimal(signal['levels'][name])
    if 'published_at' in signal:
        require_fields(signal, {'published_at': int})
    if signal.get('exit_by') is not None:
        require_fields(signal, {'exit_by': int, 'published_at': int})
        if signal['exit_by'] <= signal['published_at']:
            raise ValueError('journal_corrupt')
    optional_fields(signal, {'kind': str, 'symbol': str, 'venue_symbol': str, 'venue_hint': str,
                             'invalidation': str, 'thesis': str, 'horizon': str, 'links': list, 'raw': str},
                    empty_strings=('raw', 'thesis', 'horizon'))
    if 'links' in signal and any(type(link) is not str for link in signal['links']):
        raise ValueError('journal_corrupt')
    if 'confidence' in signal:
        require_decimal(signal['confidence'], positive=False)
        if not 0 <= Decimal(signal['confidence']) <= 1:
            raise ValueError('journal_corrupt')
    if 'claimed_at' in record:
        require_fields(record, {'claimed_at': int})
        if record['claimed_at'] < 0:
            raise ValueError('journal_corrupt')
    for name in ('stop_hit', 'entry_done'):
        if name in record:
            require_fields(record, {name: bool})
    for name, kind in (('close_ms', int), ('entry_cancel_reason', str)):
        if record.get(name) is not None:
            require_fields(record, {name: kind})
    if record.get('fill_price') is not None:
        require_decimal(record['fill_price'])
    optional_fields(record, {'reason': str})


def optional_fields(value, fields, nullable=(), empty_strings=()):
    for name, kind in fields.items():
        if name in value and not (name in nullable and value[name] is None):
            if name in empty_strings and value[name] == '':
                continue
            require_fields(value, {name: kind})


def validate_trigger_fields(intent, name):
    leg = intent[name]
    fields = {'asset': str, 'asset_index': int, 'is_buy': bool, 'reduce_only': bool,
              'is_market': bool, 'tpsl': str, 'size': str, 'trigger_px': str, 'limit_px': str}
    require_fields(leg, fields)
    entry = intent['entry']
    if (set(leg) != set(fields) or leg['asset'] != intent['asset']
            or leg['asset_index'] != intent['asset_index'] or leg['is_buy'] == entry['is_buy']
            or not leg['reduce_only'] or not leg['is_market']
            or leg['tpsl'] != ('sl' if name == 'stop' else 'tp')):
        raise ValueError('journal_corrupt')
    for field in ('size', 'trigger_px', 'limit_px'):
        require_decimal(leg[field])
    if Decimal(leg['size']) != Decimal(entry['size']):
        raise ValueError('journal_corrupt')


def validate_order_fields(intent):
    require_fields(intent, {'kind': str, 'asset': str, 'asset_index': int, 'is_buy': bool,
                            'reduce_only': bool, 'tif': str, 'size': str, 'limit_px': str})
    if intent['kind'] != 'order' or intent['tif'] not in ('Ioc', 'Gtc', 'Alo'):
        raise ValueError('journal_corrupt')
    if intent['tif'] == 'Alo' and not intent['reduce_only']:
        raise ValueError('journal_corrupt')
    for name in ('size', 'limit_px'):
        require_decimal(intent.get(name))


def validate_intent_record(record):
    intent = record.get('intent')
    require_fields(intent, {'request_id': str, 'kind': str, 'delegate_key': str, 'key_ref': str})
    if intent['kind'] not in ('order', 'bracket', 'cancel', 'schedule_cancel', 'trigger_order'):
        raise ValueError('journal_corrupt')
    optional_fields(intent, {'policy_epoch': int, 'venue': str, 'vault_address': str,
                             'venue_nonce': int, 'expires_at': int, 'delegate_signature': str})
    if intent['kind'] == 'bracket':
        require_fields(intent, {'entry': dict, 'stop': dict, 'asset': str, 'asset_index': int})
        entry = intent['entry']
        validate_order_fields(entry)
        if (set(entry) != {'kind', 'asset', 'asset_index', 'is_buy', 'reduce_only', 'tif', 'size', 'limit_px'}
                or entry['asset'] != intent['asset'] or entry['asset_index'] != intent['asset_index']
                or entry['reduce_only'] or entry['tif'] != 'Gtc'):
            raise ValueError('journal_corrupt')
        validate_trigger_fields(intent, 'stop')
        if 'take_profit' in intent:
            validate_trigger_fields(intent, 'take_profit')
    elif intent['kind'] == 'order':
        validate_order_fields(intent)
    elif intent['kind'] == 'trigger_order':
        require_fields(intent, {'asset': str, 'asset_index': int, 'is_buy': bool, 'reduce_only': bool,
                                'is_market': bool, 'tpsl': str, 'size': str, 'trigger_px': str, 'limit_px': str})
        if not intent['reduce_only'] or not intent['is_market'] or intent['tpsl'] != 'sl' or 'tif' in intent:
            raise ValueError('journal_corrupt')
        for name in ('size', 'trigger_px', 'limit_px'):
            require_decimal(intent[name])
        if abs(Decimal(intent['limit_px']) - Decimal(intent['trigger_px'])) > Decimal(intent['trigger_px']) / 2:
            raise ValueError('journal_corrupt')
    elif intent['kind'] == 'cancel':
        require_fields(intent, {'asset': str, 'asset_index': int, 'oid': int})
    else:
        require_fields(intent, {'time_ms': int})
    if 'rearm_intent' in record:
        require_fields(record, {'rearm_intent': dict})
        validate_intent_record({'intent': record['rearm_intent']})
        if record['rearm_intent']['kind'] != 'schedule_cancel':
            raise ValueError('journal_corrupt')


def validate_reply_fields(value, legacy=False):
    require_fields(value, {})
    optional_fields(value, {'request_id': str, 'signed': bool, 'ambiguous': bool, 'reason': str,
                            'venue_status': str, 'signature': str, 'prepared': bool,
                            'key_ref': str, 'policy_epoch': int, 'venue_nonce': int, 'expires_at': int},
                    nullable=('signed', 'request_id'))
    if 'intent' in value and not (legacy and value['intent'] is None):
        validate_intent_record({'intent': value['intent']})
    if 'rearm_intent' in value:
        validate_intent_record({'intent': value['rearm_intent']})
    if 'venue_answer' in value:
        answer = value['venue_answer']
        if (legacy and answer == []) or answer is None:
            return
        require_fields(answer, {})
        optional_fields(answer, {'status': str, 'signature': str})
        if 'response' in answer:
            response = answer['response']
            if type(response) is str:
                if not response:
                    raise ValueError('journal_corrupt')
                return
            require_fields(response, {'type': str})
            if 'data' in response:
                require_fields(response, {'data': dict})
                if 'statuses' in response['data']:
                    require_fields(response['data'], {'statuses': list})
                    for status in response['data']['statuses']:
                        if type(status) is str:
                            if not status:
                                raise ValueError('journal_corrupt')
                            continue
                        require_fields(status, {})
                        if len(status) != 1 or not set(status) <= {'error', 'resting', 'filled'}:
                            raise ValueError('journal_corrupt')
                        optional_fields(status, {'error': str})
                        for name in ('resting', 'filled'):
                            if name in status:
                                require_fields(status[name], {'oid': int})
                                for field in ('totalSz', 'avgPx'):
                                    if field in status[name]:
                                        require_fields(status[name], {field: str})
                                        require_decimal(status[name][field])


def validate_diagnostic_fields(record):
    if 'key' in record:
        validate_record_key(record)
    for source in ('hl_oracle', 'binance'):
        optional_fields(record, {source + '_timestamp': int, source + '_error': str},
                        nullable=(source + '_timestamp',))
        if record.get(source + '_px') is not None:
            require_decimal(record[source + '_px'])
    event = record['event']
    if event == 'review':
        require_fields(record, {'stop_hit': bool, 'stop': str, 'description': str})
        require_decimal(record.get('signal_entry'))
        for name in ('fill_price', 'slippage'):
            if record.get(name) is not None:
                require_decimal(record[name], positive=name != 'slippage', allow_signed_zero=name == 'slippage')
    elif event == 'reconcile':
        require_fields(record, {'orders': list, 'fills': list})
        require_decimal(record.get('position'), positive=False)
        for order in record['orders']:
            require_fields(order, {'coin': str, 'oid': int})
            optional_fields(order, {'reduceOnly': bool, 'isTrigger': bool, 'side': str,
                                    'timestamp': int, 'orderType': str, 'tif': str})
            for name in ('sz', 'limitPx', 'origSz', 'triggerPx'):
                if name in order:
                    require_fields(order, {name: str})
                    require_decimal(order[name], positive=False)
        for fill in record['fills']:
            require_fields(fill, {'coin': str, 'time': int, 'side': str})
            optional_fields(fill, {'tid': int, 'oid': int})
            for name in ('sz', 'px'):
                require_fields(fill, {name: str})
                require_decimal(fill.get(name))
    elif event == 'gate':
        require_fields(record, {'sizing': dict, 'intent': dict})
        optional_fields(record, {'reason': str}, nullable=('reason',))
        require_decimal(record.get('entry_price'))
        intent = record['intent']
        validate_intent_record({'intent': {'request_id': 'preview', 'delegate_key': 'preview',
                                         'key_ref': 'preview', **intent}})
        sizing_fields = {'size', 'notional', 'distance', 'equity', 'liquidation_price'}
        require_fields(record['sizing'], {name: str for name in sizing_fields})
        if set(record['sizing']) != sizing_fields:
            raise ValueError('journal_corrupt')
        for value in record['sizing'].values():
            require_decimal(value, positive=False)
    elif event == 'backoff':
        require_fields(record, {'route': str, 'reason': str, 'retry': bool})
        require_decimal(record.get('seconds'), positive=False)
    elif event in ('refusal', 'bracket_refused', 'recovery_required', 'redelivery', 'nonce_retry',
                   'stream_refusal', 'deliveries_disabled', 'delivery_error'):
        require_fields(record, {'reason': str})
    elif event == 'consumed':
        require_fields(record, {'stream_id': str, 'result': dict})
    elif event == 'stream_disconnected':
        require_fields(record, {'stream_id': str})


def validate_record(record):
    validate_record_tree(record)
    require_fields(record, {'event': str, 'timestamp': int})
    if record['timestamp'] < 0:
        raise ValueError('journal_corrupt')
    event = record['event']
    validate_diagnostic_fields(record)
    if event == 'state':
        validate_state_record(record)
    elif event == 'intent':
        validate_intent_record(record)
    elif event == 'reply':
        require_fields(record, {'request_id': str})
        validate_reply_fields(record)
    elif event == 'prepare_reply':
        validate_reply_fields(record)
    elif event in ('bracket_result', 'cancel_result', 'exit_result', 'backstop_result', 'backstop_replace_refused'):
        validate_record_key(record)
        require_fields(record, {'outcome': dict})
        optional_fields(record, {'request_id': str, 'reason': str, 'close_ms': int, 'oid': int},
                        nullable=('close_ms',))
        validate_reply_fields(record['outcome'], legacy=event == 'bracket_result' and 'request_id' not in record)
    elif event == 'review':
        validate_record_key(record)
    elif event == 'anchor_verified':
        require_fields(record, {'signal_id': str, 'payload_hash': str})
        digest = record['payload_hash']
        if len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
            raise ValueError('journal_corrupt')
    elif event == 'cursor':
        require_fields(record, {'stream': str, 'stream_id': str})
    elif event in ('delivery_pending', 'delivery_reported', 'delivery_abandoned'):
        require_fields(record, {'event_id': str})
    if event.startswith('delivery_') and record.get('status') == 429 and 'not_before' in record:
        require_fields(record, {'not_before': int})


