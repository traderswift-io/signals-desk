from decimal import Decimal, InvalidOperation


def check_trigger_distance(config, signal, mark_price, triggers):
    try:
        mark = Decimal(str(mark_price))
    except InvalidOperation:
        raise ValueError('invalid_mark_price') from None
    if not mark.is_finite() or mark <= 0:
        raise ValueError('invalid_mark_price')
    maximum = config.get('max_trigger_distance_bps', 1000)
    for leg, trigger in triggers:
        if trigger is None:
            continue
        try:
            price = Decimal(str(trigger))
        except InvalidOperation:
            raise ValueError('invalid_trigger_price') from None
        if not price.is_finite() or price <= 0:
            raise ValueError('invalid_trigger_price')
        distance = abs(price - mark) / mark * 10000
        if distance > maximum:
            error = ValueError('trigger_distance_exceeds_max')
            error.details = {'asset': signal['asset'], 'leg': leg, 'distance_bps': str(distance)}
            raise error
