from decimal import Decimal, InvalidOperation, ROUND_DOWN


DEFAULTS = {
    'backstop_extra_bps': '100', 'exit_max_resting': 2, 'exit_depth_frac': '0.03',
    'exit_depth_levels': 5, 'exit_escalate_bps': '30', 'exit_target_giveback_bps': '20',
    'exit_min_action_ms': 10000, 'exit_fast_action_ms': 2000, 'exit_action_reserve': 200,
}


def exit_config(config):
    values = {**DEFAULTS, **config}
    for name in DEFAULTS:
        try:
            value = Decimal(str(values[name]))
        except InvalidOperation:
            raise ValueError('invalid_' + name) from None
        if not value.is_finite() or value < 0:
            raise ValueError('invalid_' + name)
        if isinstance(DEFAULTS[name], int) and (isinstance(values[name], bool) or value != value.to_integral_value()):
            raise ValueError('invalid_' + name)
    if not 0 < Decimal(str(values['exit_depth_frac'])) <= Decimal('0.03'):
        raise ValueError('invalid_exit_depth_frac')
    for name in ('exit_max_resting', 'exit_depth_levels'):
        if int(values[name]) < 1:
            raise ValueError('invalid_' + name)
    if not 2000 <= int(values['exit_fast_action_ms']) <= int(values['exit_min_action_ms']):
        raise ValueError('invalid_exit_fast_action_ms')
    if int(values['exit_min_action_ms']) < 10000:
        raise ValueError('invalid_exit_min_action_ms')
    if Decimal(str(values['backstop_extra_bps'])) <= 0:
        raise ValueError('invalid_backstop_extra_bps')
    return values


def book_context(book, position, config):
    count = int(config.get('exit_depth_levels', 5))
    levels = book.get('levels', []) if isinstance(book, dict) else []
    if len(levels) != 2 or not all(levels):
        return None
    try:
        bids, asks = [[(Decimal(row['px']), Decimal(row['sz'])) for row in side[:count]] for side in levels]
    except (KeyError, TypeError, InvalidOperation):
        return None
    if any(not price.is_finite() or price <= 0 or not size.is_finite() or size < 0 for price, size in bids + asks):
        return None
    if bids[0][0] >= asks[0][0]:
        return None
    long_side = Decimal(position['size']) > 0
    own, opposite = (asks, bids) if long_side else (bids, asks)
    depth = sum((size for _, size in own), Decimal(0))
    return {'mid': (bids[0][0] + asks[0][0]) / 2, 'touch': own[0][0], 'opposite_touch': opposite[0][0],
            'depth': depth, 'other_depth': sum((size for _, size in opposite), Decimal(0)),
            'low_liquidity': depth < abs(Decimal(position['size'])) * 10}


def target_given_back(mid, position, config):
    target = position.get('target')
    if target is None:
        return True
    target = Decimal(target)
    band = Decimal(str(config.get('exit_target_giveback_bps', '20'))) / 10000
    return mid <= target * (1 - band) if Decimal(position['size']) > 0 else mid >= target * (1 + band)


def backstop_actions(position, orders):
    remaining = abs(Decimal(position['size']))
    stops = [order for order in orders if order.get('isTrigger') and order.get('reduceOnly')
             and order.get('orderType', 'Stop').startswith('Stop')]
    if not remaining:
        return [{'action': 'cancel', 'oid': order['oid']} for order in orders]
    sized = [order for order in stops if Decimal(order['sz']) == remaining]
    if sized:
        return [{'action': 'cancel', 'oid': order['oid']} for order in stops if order['oid'] != sized[-1]['oid']]
    if stops and any(Decimal(order['sz']) > remaining for order in stops):
        return [{'action': 'replace_backstop', 'size': str(remaining), 'oid': stops[0]['oid'],
                 'trigger_px': str(position['backstop_px'])}]
    return []


def exit_actions(book, position, orders, reason, config, maintain_backstop=True):
    remaining = abs(Decimal(position['size']))
    if not remaining:
        return backstop_actions(position, orders)
    protection = backstop_actions(position, orders)
    if protection and maintain_backstop:
        return protection
    context = book_context(book, position, config)
    if context is None:
        return []
    resting = [order for order in orders if not order.get('isTrigger') and order.get('reduceOnly')]
    cancels = [{'action': 'cancel', 'oid': order['oid']} for order in resting]
    long_side = Decimal(position['size']) > 0
    mid, stop, backstop = context['mid'], Decimal(position['stop']), Decimal(position['backstop_px'])
    if mid <= backstop if long_side else mid >= backstop:
        return cancels
    if reason == 'target' and target_given_back(mid, position, config):
        return cancels
    if reason not in ('target', 'stop', 'expiry', 'withdrawn', 'time_stop'):
        return []
    escalation = Decimal(str(config.get('exit_escalate_bps', '30'))) / 10000
    aggressive = reason != 'target' and (mid <= stop * (1 - escalation) if long_side else mid >= stop * (1 + escalation))
    fraction = Decimal(str(config.get('exit_depth_frac', '0.03')))
    footprint = context['depth'] * fraction
    quantum = Decimal(1).scaleb(-int(position['szDecimals']))
    size = min(remaining, footprint)
    if context['low_liquidity']:
        size /= 2
    size = size.quantize(quantum, rounding=ROUND_DOWN)
    price = context['opposite_touch'] if aggressive else context['touch']
    if size * price < Decimal('10'):
        size = remaining.quantize(quantum, rounding=ROUND_DOWN)
    limit = 1 if context['low_liquidity'] or aggressive else int(config.get('exit_max_resting', 2))
    if aggressive:
        if resting:
            return cancels
        band = Decimal(str(config['exit_band']))
        if price < mid * (1 - band) if long_side else price > mid * (1 + band):
            return []
    retained, actions = [], []
    for order in resting:
        if (len(retained) >= limit or Decimal(order['limitPx']) != price or Decimal(order['sz']) > size
                or sum((Decimal(row['sz']) for row in retained), Decimal(0)) + Decimal(order['sz']) > min(remaining, footprint)):
            actions.append({'action': 'cancel', 'oid': order['oid']})
        else:
            retained.append(order)
    cancelled = {action['oid'] for action in actions}
    removed = sum((Decimal(order['sz']) for order in resting if order['oid'] in cancelled
                   and any(Decimal(level['px']) == Decimal(order['limitPx'])
                           for level in book['levels'][1 if long_side else 0][:int(config.get('exit_depth_levels', 5))])), Decimal(0))
    effective_depth = max(Decimal(0), context['depth'] - removed)
    footprint = min(footprint, effective_depth * fraction)
    used = sum((Decimal(order['sz']) for order in retained), Decimal(0))
    added = Decimal(0)
    for _ in range(limit - len(retained)):
        available = min(size, remaining - used, footprint - used).quantize(quantum, rounding=ROUND_DOWN)
        if available <= 0 or available * price < Decimal('10') and available != remaining:
            break
        if not aggressive and effective_depth + added < context['other_depth'] <= effective_depth + added + available:
            break
        actions.append({'action': 'place', 'size': str(available), 'limit_px': str(price),
                        'is_buy': not long_side, 'tif': 'Ioc' if aggressive else 'Alo'})
        used += available
        added += available
        if aggressive:
            break
    return actions
