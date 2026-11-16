from decimal import Decimal, InvalidOperation

from .desk_wake import BAR_MS, active_asset
from .signer_delegate_client import bracket_fields, now_ms, price_text, read_info, resolve_symbol


def resident_bracket(config, signal, size, size_decimals):
    buy = signal['side'] == 'long'
    levels = signal['levels']
    extra = Decimal(str(config.get('backstop_extra_bps', '100'))) / 10000
    stop = Decimal(levels['stop'])
    backstop = stop - Decimal(levels['entry']) * extra if buy else stop + Decimal(levels['entry']) * extra
    return signal_bracket(config, {**signal, 'levels': {**levels, 'stop': str(backstop), 'target': None}}, size, size_decimals)


def signal_bracket(config, signal, size, size_decimals):
    try:
        band = Decimal(str(config.get('bracket_slippage_bps', '50'))) / 10000
        if not band.is_finite() or not 0 <= band <= Decimal('0.5'):
            raise ValueError('trigger_price_outside_band')
        buy = signal['side'] == 'long'
        levels = signal['levels']
        entry = price_text(Decimal(levels['entry']), size_decimals, buy)
        triggers = [levels['stop'], levels.get('target')]
        prices = []
        for trigger in triggers:
            if trigger is None:
                prices.extend((None, None))
            else:
                trigger = price_text(Decimal(trigger), size_decimals, not buy)
                limit = price_text(Decimal(trigger) * (1 - band if buy else 1 + band), size_decimals, not buy)
                prices.extend((trigger, limit))
        return bracket_fields(signal, size, entry, buy, *prices)
    except InvalidOperation:
        raise ValueError('trigger_price_outside_band') from None


async def closed_price(runtime, signal, close):
    bars = await read_info(runtime, 'candleSnapshot', req={
        'coin': signal['asset'], 'interval': '15m', 'startTime': close - BAR_MS, 'endTime': close})
    matching = [bar for bar in bars if bar.get('t') == close - BAR_MS and close - 1 <= bar.get('T', 0) <= close]
    if len(matching) != 1:
        raise ValueError('closed_candle_unavailable')
    try:
        price = Decimal(str(matching[0]['c']))
    except InvalidOperation:
        raise ValueError('closed_candle_unavailable') from None
    if not price.is_finite() or price <= 0:
        raise ValueError('closed_candle_unavailable')
    return price


def invalidated(signal, price):
    level = signal['levels'].get('invalidation', signal['levels']['stop'])
    try:
        level = Decimal(str(level))
    except InvalidOperation:
        raise ValueError('invalid_invalidation_level') from None
    if not level.is_finite() or level <= 0:
        raise ValueError('invalid_invalidation_level')
    return price <= level if signal['side'] == 'long' else price >= level


def wake_close_refusal(close):
    if type(close) is not int or close % BAR_MS or not close <= now_ms() < close + BAR_MS:
        return 'stale_wake_close'
    return None


async def entry_candle_guard(runtime, signal):
    close = runtime.get('close_ms')
    refusal = wake_close_refusal(close)
    if refusal:
        return refusal
    if signal['asset'] not in runtime['config'].get('symbols', {}):
        return 'published_not_traded'
    if not active_asset(runtime['config'], signal['asset'], close):
        return 'outside_active_hours'
    if signal.get('published_at', 0) > close:
        return 'awaiting_candle_close'
    metadata = resolve_symbol(signal['asset'], runtime['meta'])
    signal_bracket(runtime['config'], signal, '1', int(metadata['szDecimals']))
    if invalidated(signal, await closed_price(runtime, signal, close)):
        return 'signal_invalidated'
    return wake_close_refusal(close)
