import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import aiohttp

from .signer_delegate_client import dex_fields, now_ms, read_info


BAR_MS = 900000
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


def last_close(timestamp):
    return timestamp // BAR_MS * BAR_MS


def minute_of_day(value):
    hour, minute = value.split(':')
    if len(hour) != 2 or len(minute) != 2:
        raise ValueError('invalid_symbols')
    hours, minutes = int(hour), int(minute)
    if not 0 <= hours <= 24 or not 0 <= minutes < 60 or hours == 24 and minutes:
        raise ValueError('invalid_symbols')
    return hours * 60 + minutes


def active_days(value):
    bounds = value.split('-')
    start, end = DAYS.index(bounds[0]), DAYS.index(bounds[-1])
    if len(bounds) > 2:
        raise ValueError('invalid_symbols')
    return {(start + offset) % 7 for offset in range((end - start) % 7 + 1)}


def validate_symbols(symbols):
    try:
        if not isinstance(symbols, dict):
            raise ValueError('invalid_symbols')
        for asset, schedule in symbols.items():
            if not isinstance(asset, str) or not asset or not schedule['active_utc']:
                raise ValueError('invalid_symbols')
            active_days(schedule['days'])
            for start, end in schedule['active_utc']:
                if not minute_of_day(start) < minute_of_day(end):
                    raise ValueError('invalid_symbols')
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError('invalid_symbols') from None


def active_asset(config, asset, close):
    schedule = config.get('symbols', {}).get(asset)
    if schedule is None:
        return False
    date = datetime.fromtimestamp(close / 1000, timezone.utc)
    minute = date.hour * 60 + date.minute
    return date.weekday() in active_days(schedule['days']) and any(
        minute_of_day(start) <= minute < minute_of_day(end) for start, end in schedule['active_utc'])


def wake_decision(config, orders, positions, timestamp):
    close = last_close(timestamp)
    active = {asset for asset in config.get('symbols', {}) if active_asset(config, asset, close)}
    resting = any(order['coin'] in active and not order.get('reduceOnly', False)
                  and not order.get('isTrigger', False) for order in orders)
    opened = any(row['position']['coin'] in active and Decimal(row['position']['szi']) for row in positions)
    reason = ('outside_active_hours' if not active else 'resting_entry' if resting else
              'open_position' if opened else 'hourly_signal_check' if close % 3600000 == 0 else 'no_work')
    date = datetime.fromtimestamp(close / 1000, timezone.utc)
    return {'wake': reason in ('resting_entry', 'open_position', 'hourly_signal_check'), 'reason': reason,
            'close_ms': close, 'last_close_utc': date.isoformat(),
            'next_close_utc': (date + timedelta(minutes=15)).isoformat()}


async def wake_result(runtime, timestamp=None):
    config = runtime['config']
    validate_symbols(config.get('symbols', {}))
    orders, positions = [], []
    markets = {dex_fields(asset).get('dex', '') for asset in config.get('symbols', {})}
    for market in sorted(markets):
        fields = {'dex': market} if market else {}
        orders.extend(await read_info(runtime, 'frontendOpenOrders', user=config['vault_address'], **fields))
        account = await read_info(runtime, 'clearinghouseState', user=config['vault_address'], **fields)
        positions.extend(account['assetPositions'])
    return wake_decision(config, orders, positions, now_ms() if timestamp is None else timestamp)


async def main(path):
    timestamp = now_ms()
    try:
        config = json.loads(Path(path).read_text())
        async with aiohttp.ClientSession() as session:
            result = await wake_result({'config': config, 'session': session}, timestamp)
    except (ValueError, KeyError, TypeError, OSError, InvalidOperation, aiohttp.ClientError, TimeoutError):
        result = {**wake_decision({}, [], [], timestamp), 'reason': 'wake_unavailable'}
    print(json.dumps(result))
    print('WAKE | ' + ('yes' if result['wake'] else 'no') + ' | ' + result['reason']
          + ' | next_close_utc=' + result['next_close_utc'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    asyncio.run(main(parser.parse_args().config))
