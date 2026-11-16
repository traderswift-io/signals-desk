from decimal import Decimal


STATE_TRANSITIONS = {
    'claimed': {'claimed', 'sent', 'filled', 'refused', 'exited'},
    'sent': {'sent', 'filled', 'refused', 'not_filled', 'exited'},
    'filled': {'filled', 'exited'},
    'refused': {'refused'},
    'not_filled': {'not_filled'},
    'exited': {'exited'},
}


def venue_statuses(reply):
    answer = reply.get('venue_answer') or {}
    response = answer.get('response', {})
    return response.get('data', {}).get('statuses', []) if isinstance(response, dict) else []


def reply_evidence(reply, execution=False):
    signed, ambiguous = reply.get('signed'), reply.get('ambiguous', False)
    statuses = venue_statuses(reply)
    detail = str(reply.get('reason', '')) + str(reply.get('venue_status', ''))
    uncertain = any(word in detail.lower() for word in ('timeout', 'unknown', 'ambiguous'))
    if statuses and signed is not True or execution and signed is False:
        raise ValueError('journal_corrupt')
    if reply.get('reason') in ('ambiguous_transport', 'ambiguous_http_status') and (signed is not None or not ambiguous):
        raise ValueError('journal_corrupt')
    if signed is not False and (signed is not True or not reply.get('venue_answer') or uncertain):
        if not ambiguous:
            raise ValueError('journal_corrupt')
    return signed, ambiguous


def entry_intent(intent):
    return intent['kind'] == 'bracket' or intent['kind'] == 'order' and not intent['reduce_only']


def validate_execution_history(records):
    intents, replies, entries, states = {}, {}, {}, {}
    completed, executed = set(), set()
    entry_orders, cancellations = {}, {}
    for row in records:
        event = row['event']
        key = tuple(row['key']) if 'key' in row else None
        if event == 'intent':
            intent = row['intent']
            request_id = intent['request_id']
            intents.setdefault(request_id, row)
            if key is not None and entry_intent(intent):
                entries[key] = request_id
        if event == 'reply':
            request_id = row['request_id']
            signed, ambiguous = reply_evidence(row)
            replies[request_id] = row
            original = intents[request_id]
            intent = original['intent']
            identity = tuple(original['key']) if 'key' in original else None
            statuses = venue_statuses(row)
            if identity is not None and entry_intent(intent) and statuses:
                status = statuses[0]
                if isinstance(status, dict):
                    for name in ('filled', 'resting'):
                        if name in status:
                            entry_orders.setdefault(identity, set()).add(status[name]['oid'])
                    if 'filled' in status:
                        completed.add(identity)
                        executed.add(request_id)
            if intent['kind'] == 'cancel' and signed is True and not ambiguous and 'success' in statuses:
                cancellations.setdefault(identity, set()).add(intent['oid'])
        request_id = entries.get(key)
        if event == 'reconcile' and request_id is not None:
            original = intents[request_id]
            intent = original['intent']
            entry = intent.get('entry', intent)
            orders = [order for order in row['orders'] if not order.get('reduceOnly', False)
                      and not order.get('isTrigger', False)]
            entry_orders.setdefault(key, set()).update(order['oid'] for order in orders)
            if orders:
                executed.add(request_id)
            fills = [fill for fill in row['fills'] if fill['coin'] == intent['asset']
                     and fill['side'] == ('B' if entry['is_buy'] else 'A')
                     and fill['time'] >= original['timestamp'] and Decimal(fill['sz']) > 0]
            if fills or Decimal(row['position']):
                executed.add(request_id)
                if fills or not orders:
                    completed.add(key)
            known = entry_orders.get(key, set())
            if known and known <= cancellations.get(key, set()) and not orders:
                completed.add(key)
        if request_id is not None and (
                event in ('state', 'review') and row.get('fill_price') is not None
                or event == 'state' and row['state'] == 'filled'
                or event == 'exit_result' and row['outcome'].get('signed') is True):
            executed.add(request_id)
        if event == 'state':
            previous = states.get(key)
            if previous is not None:
                if row['state'] not in STATE_TRANSITIONS[previous['state']]:
                    raise ValueError('journal_corrupt')
                if previous.get('entry_done', False) and not row.get('entry_done', False):
                    raise ValueError('journal_corrupt')
            if row.get('entry_done', False) and key not in completed:
                raise ValueError('journal_corrupt')
            states[key] = row
    for request_id in executed:
        if request_id in replies:
            reply_evidence(replies[request_id], execution=True)
    return completed


def entry_completion_agrees(records, key):
    return tuple(key) in validate_execution_history(records)
