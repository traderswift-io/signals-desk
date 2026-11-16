EXIT_REASONS = frozenset(('stop', 'target', 'expiry', 'withdrawn', 'time_stop', 'cancel',
                          'restart_recovery', 'watcher_dead'))


def advance_closing(custody, closing, giveback=False):
    if closing is not None and closing not in EXIT_REASONS:
        raise ValueError('journal_corrupt')
    previous = custody['closing']
    if previous == 'time_stop' and closing != previous:
        raise ValueError('journal_corrupt')
    if previous is not None and closing is None and not (previous == 'target' and giveback):
        raise ValueError('journal_corrupt')
    if previous not in (None, 'target') and closing == 'target':
        raise ValueError('journal_corrupt')
    if custody['stop_hit'] and closing in (None, 'target'):
        raise ValueError('journal_corrupt')
    custody['closing'] = closing


def establish_backstop(custody, price):
    if custody['backstop_px'] is not None and price != custody['backstop_px']:
        raise ValueError('journal_corrupt')
    custody['backstop_px'] = price


def exit_custody(records):
    tickets, state_fields = {}, {}
    for row in records:
        if 'key' not in row:
            continue
        custody = tickets.setdefault(tuple(row['key']), {'backstop_px': None, 'closing': None, 'stop_hit': False})
        event = row['event']
        if event == 'intent' and row['intent']['kind'] == 'bracket':
            establish_backstop(custody, row['intent']['stop']['trigger_px'])
        elif event == 'time_stop':
            advance_closing(custody, 'time_stop')
        elif event == 'exit_result' and row.get('reason') in EXIT_REASONS:
            advance_closing(custody, row['reason'])
        elif event == 'state':
            established = state_fields.setdefault(tuple(row['key']), set())
            if any(name not in row for name in established):
                raise ValueError('journal_corrupt')
            established.update(name for name in ('backstop_px', 'closing', 'stop_hit') if name in row)
            if 'backstop_px' in row:
                establish_backstop(custody, row['backstop_px'])
            if 'stop_hit' in row:
                if custody['stop_hit'] and row['stop_hit'] is not True:
                    raise ValueError('journal_corrupt')
                custody['stop_hit'] = row['stop_hit']
            if 'closing' in row:
                advance_closing(custody, row['closing'], row.get('reason') == 'target_giveback')
    return tickets
