from .desk_journal_shapes import JOURNAL_MAGNITUDE_LIMIT


MAX_EXIT_ACTIONS = 2
MIN_EXIT_ACTIONS = 1
MAX_STATE_TIMESTAMP = int(JOURNAL_MAGNITUDE_LIMIT) - 1
STATE_INTEGER_LIMITS = {'timestamp': MAX_STATE_TIMESTAMP, 'claimed_at': MAX_STATE_TIMESTAMP,
                        'close_ms': MAX_STATE_TIMESTAMP, 'last_exit_action_ms': MAX_STATE_TIMESTAMP,
                        'exit_action_cost': MAX_EXIT_ACTIONS}


def bounded_integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('journal_corrupt')


def pacing_cost(value):
    bounded_integer(value, MIN_EXIT_ACTIONS, MAX_EXIT_ACTIONS)


def validate_state_integers(row, previous=None):
    for name, maximum in STATE_INTEGER_LIMITS.items():
        value = row.get(name)
        if value is not None:
            bounded_integer(value, MIN_EXIT_ACTIONS if name == 'exit_action_cost' else 0, maximum)
            if name != 'exit_action_cost' and value > row['timestamp']:
                raise ValueError('journal_corrupt')
    if (row.get('last_exit_action_ms') is None) != (row.get('exit_action_cost') is None):
        raise ValueError('journal_corrupt')
    if row.get('last_exit_action_ms') is not None and row['last_exit_action_ms'] < row.get('claimed_at', 0):
        raise ValueError('journal_corrupt')
    if previous is not None:
        for name in ('claimed_at', 'close_ms'):
            if row.get(name) != previous.get(name):
                raise ValueError('journal_corrupt')
        for name in ('timestamp', 'last_exit_action_ms'):
            prior = previous.get(name)
            if prior is not None and (row.get(name) is None or row[name] < prior):
                raise ValueError('journal_corrupt')


def exit_pacing(records):
    states, reservations, actions, recovered = {}, {}, {}, {}
    for row in records:
        if 'key' not in row:
            continue
        key, event = tuple(row['key']), row['event']
        reservation = reservations.get(key)
        if event == 'exit_reservation':
            pacing_cost(row['cost'])
            bounded_integer(row['timestamp'], 0, MAX_STATE_TIMESTAMP)
            if reservation is not None and row['timestamp'] < reservation['timestamp']:
                raise ValueError('journal_corrupt')
            reservations[key] = row
            actions[key] = {}
        elif event == 'state':
            previous = states.get(key)
            validate_state_integers(row, previous)
            timestamp, cost = row.get('last_exit_action_ms'), row.get('exit_action_cost')
            if timestamp is not None:
                if reservation is None:
                    reservation = {'timestamp': timestamp, 'cost': cost, 'legacy': True}
                    reservations[key], actions[key] = reservation, {}
                elif reservation.get('legacy') and timestamp != reservation['timestamp']:
                    if timestamp < reservation['timestamp'] or timestamp > row['timestamp']:
                        raise ValueError('journal_corrupt')
                    reservation = {'timestamp': timestamp, 'cost': cost, 'legacy': True}
                    reservations[key], actions[key] = reservation, {}
                supported_cost = min(reservation['cost'], max(MIN_EXIT_ACTIONS, len(actions[key])))
                if timestamp != reservation['timestamp'] or cost not in (reservation['cost'], supported_cost):
                    raise ValueError('journal_corrupt')
            elif reservation is not None:
                raise ValueError('journal_corrupt')
            states[key] = row
        elif event == 'intent' and reservation is not None:
            intent = row['intent']
            if intent['kind'] == 'cancel' or intent.get('reduce_only'):
                if row['timestamp'] < reservation['timestamp']:
                    raise ValueError('journal_corrupt')
                actions[key].setdefault(intent['request_id'], row['timestamp'])
        if key in reservations:
            reservation = reservations[key]
            timestamp = reservation['timestamp']
            cost = reservation['cost']
            evidence = actions.get(key, {})
            if evidence:
                cost = min(cost, len(evidence))
                timestamp = min(timestamp, min(evidence.values()))
            recovered[key] = {'last_exit_action_ms': timestamp, 'exit_action_cost': cost}
    return recovered
