from decimal import Decimal, ROUND_DOWN

from .desk_signal import decimal_number


def brackets_of(tiers, asset_max_leverage):
    rows = sorted((Decimal(str(tier['lowerBound'])), min(int(tier['maxLeverage']), asset_max_leverage)) for tier in tiers)
    result, cumulative, previous_rate = [], Decimal(0), Decimal(0)
    for index, (floor, leverage) in enumerate(rows):
        if leverage <= 0 or floor < 0:
            raise ValueError('invalid_margin_tiers')
        rate = Decimal(1) / (2 * Decimal(leverage))
        cumulative += floor * (rate - previous_rate)
        cap = rows[index + 1][0] if index + 1 < len(rows) else Decimal('Infinity')
        result.append({'floor': floor, 'cap': cap, 'rate': rate, 'deduction': cumulative, 'leverage': leverage})
        previous_rate = rate
    if not result or result[0]['floor'] != 0:
        raise ValueError('missing_margin_tiers')
    return result


def asset_metadata(meta, index):
    asset = dict(meta['universe'][index])
    table_id = int(asset.get('marginTableId') or asset['maxLeverage'])
    tables = {int(key): value['marginTiers'] for key, value in meta.get('marginTables', [])}
    asset['marginTiers'] = tables.get(table_id) or ([{'lowerBound': '0', 'maxLeverage': table_id}] if table_id < 50 else [])
    return asset


def size_position(signal, equity, mark_price, asset_meta, risk_fraction, leverage_cap):
    equity, mark = decimal_number(equity, 'equity'), decimal_number(mark_price, 'mark')
    entry = decimal_number(signal['levels'].get('entry'), 'entry')
    stop = decimal_number(signal['levels'].get('stop'), 'stop')
    fraction, cap = decimal_number(risk_fraction, 'risk_fraction'), decimal_number(leverage_cap, 'leverage_cap')
    distance = abs(entry - stop)
    quantity = fraction * equity / distance if distance else Decimal(0)
    size = quantity.quantize(Decimal(1).scaleb(-int(asset_meta['szDecimals'])), rounding=ROUND_DOWN)
    notional = size * entry
    numbers = f'equity={equity} entry={entry} stop={stop} distance={distance} notional={notional} size={size}'

    def refuse(limit):
        raise ValueError(f'{numbers} limit={limit}')

    if min(equity, entry, stop, mark, cap) <= 0 or not 0 < fraction <= 1:
        refuse('positive_inputs_and_risk_fraction<=1')
    long_side = signal['side'] == 'long'
    if not distance or (long_side and stop >= entry) or (not long_side and stop <= entry):
        refuse('stop_on_losing_side')
    if notional / equity > cap:
        refuse(f'leverage_cap={cap}')
    if notional < 10:
        refuse('minimum_notional=10')
    if asset_meta.get('onlyIsolated') or asset_meta.get('marginMode') in ('strictIsolated', 'noCross'):
        refuse('cross_margin_required')
    brackets = brackets_of(asset_meta['marginTiers'], int(asset_meta['maxLeverage']))
    bracket = next(tier for tier in brackets if tier['floor'] <= size * mark < tier['cap'])
    if notional / equity > bracket['leverage']:
        refuse('margin_tier_max_leverage=' + str(bracket['leverage']))
    direction = Decimal(1) if long_side else Decimal(-1)
    liquidation = Decimal(0)
    for tier in brackets:
        candidate = (direction * size * entry - equity - tier['deduction']) / (size * (direction - tier['rate']))
        if candidate <= 0 and long_side:
            liquidation = Decimal(0)
            break
        if tier['floor'] <= size * candidate < tier['cap']:
            liquidation = candidate
            break
    else:
        refuse('liquidation_tier_unresolved')
    room = (stop - liquidation) if long_side else (liquidation - stop)
    if room / entry < Decimal('0.0025'):
        refuse(f'liquidation_room>=0.0025 liquidation={liquidation}')
    return {'size': size, 'notional': notional, 'distance': distance, 'equity': equity, 'liquidation_price': liquidation}
