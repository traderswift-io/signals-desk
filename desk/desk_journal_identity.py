from decimal import Decimal

from .desk_journal_evidence import validate_execution_history


def intent_records(records):
    intents = {}
    for row in records:
        if row['event'] != 'intent':
            continue
        intent = row['intent']
        previous = intents.setdefault(intent['request_id'], row)
        if previous['intent'] != intent:
            raise ValueError('journal_corrupt')
        if row['timestamp'] < previous['timestamp']:
            intents[intent['request_id']] = row
    return intents


def reply_records(records):
    intents, replies, attempts = {}, {}, {}
    for row in records:
        if row['event'] == 'intent':
            request_id = row['intent']['request_id']
            intents.setdefault(request_id, row)
            attempts[request_id] = attempts.get(request_id, 0) + 1
        if row['event'] != 'reply':
            continue
        request_id = row['request_id']
        intent = intents.get(request_id)
        if intent is None or row['timestamp'] < intent['timestamp']:
            raise ValueError('journal_corrupt')
        previous = replies.get(request_id)
        if previous is not None:
            content = {name: value for name, value in row.items() if name != 'timestamp'}
            prior_content = {name: value for name, value in previous.items() if name != 'timestamp'}
            fields = intent['intent']
            exit_retry = (fields['kind'] == 'cancel' or fields.get('reduce_only') is True)
            exit_retry = (exit_retry and previous.get('signed') is False
                          and previous.get('reason') == 'http_429' and attempts[request_id] > 0)
            if not exit_retry and any(name not in content or content[name] != value
                                      for name, value in prior_content.items()):
                raise ValueError('journal_corrupt')
            row = {**row, 'timestamp': max(previous['timestamp'], row['timestamp'])}
        replies[request_id] = row
        attempts[request_id] = 0
    return replies


def require_same(first, second):
    if type(first) is not type(second) or first != second:
        raise ValueError('journal_corrupt')
    if isinstance(first, dict):
        for name in first:
            require_same(first[name], second[name])
    elif isinstance(first, list):
        for left, right in zip(first, second):
            require_same(left, right)


def validate_history(records):
    intents = intent_records(records)
    reply_records(records)
    validate_execution_history(records)
    signals, request_keys, replies, results = {}, {}, {}, {}
    states = {}
    for row in records:
        event = row['event']
        key = row.get('key')
        if event == 'state':
            identity = tuple(key)
            require_same(signals.setdefault(identity, row['signal']), row['signal'])
            states[identity] = row
        if event == 'review':
            state = states.get(tuple(key))
            if state is not None:
                require_same(state.get('fill_price'), row.get('fill_price'))
                require_same(state.get('stop_hit', False), row['stop_hit'])
            if row['stop'] not in ('software', 'bracket') or row['description'] != 'stop: ' + row['stop']:
                raise ValueError('journal_corrupt')
            fill = row.get('fill_price')
            if fill is None:
                require_same(row.get('slippage'), None)
            elif state is not None:
                direction = Decimal(1) if state['signal']['side'] == 'long' else Decimal(-1)
                expected = (Decimal(fill) - Decimal(row['signal_entry'])) * direction
                if Decimal(row['slippage']) != expected:
                    raise ValueError('journal_corrupt')
        request_id = row['intent']['request_id'] if event == 'intent' else row.get('request_id')
        if request_id is not None and key is not None:
            require_same(request_keys.setdefault(request_id, key), key)
        if event == 'reply':
            replies[request_id] = row
        if event not in ('bracket_result', 'cancel_result', 'exit_result'):
            continue
        outcome = row['outcome']
        copied_intent = outcome.get('intent')
        if event == 'bracket_result' and request_id is None and copied_intent is None:
            continue
        if copied_intent is None:
            if event == 'bracket_result' or outcome.get('signed') is not False:
                raise ValueError('journal_corrupt')
            continue
        identity = copied_intent['request_id']
        if request_id is not None:
            require_same(request_id, identity)
        original = intents.get(identity)
        reply = replies.get(identity)
        if original is None or reply is None:
            if event != 'bracket_result' and outcome.get('signed') is False and original is None:
                continue
            raise ValueError('journal_corrupt')
        require_same(original['intent'], copied_intent)
        require_same(original.get('key'), key)
        expected_kind = {'bracket_result': 'bracket', 'cancel_result': 'cancel', 'exit_result': 'order'}[event]
        require_same(copied_intent['kind'], expected_kind)
        for name in ('signed', 'ambiguous'):
            default = False if name == 'ambiguous' else None
            require_same(outcome.get(name, default), reply.get(name, default))
        for name in set(outcome) & set(reply) - {'intent', 'event', 'timestamp', 'key'}:
            require_same(outcome[name], reply[name])
        if event == 'bracket_result':
            require_same(results.setdefault(identity, outcome), outcome)
    for row in records:
        signal = signals.get(tuple(row['key'])) if 'key' in row else None
        if signal is None:
            continue
        if row['event'] == 'review':
            if Decimal(row['signal_entry']) != Decimal(signal['levels']['entry']):
                raise ValueError('journal_corrupt')
        if row['event'] == 'intent' and row['intent']['kind'] in ('order', 'bracket', 'cancel'):
            for name in ('asset', 'asset_index'):
                require_same(row['intent'][name], signal[name])
            intent = row['intent']
            if intent['kind'] in ('order', 'bracket'):
                entry = intent.get('entry', intent)
                require_same(entry['is_buy'], (signal['side'] == 'long') != entry['reduce_only'])
