import asyncio
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import aiohttp

from .desk_bracket import signal_bracket
from .desk_trigger_distance import check_trigger_distance
from .desk_wake import validate_symbols
from .desk_record import now_ms
from .desk_position_sizer import asset_metadata, size_position
from .signer_delegate_client import account_state, cancel_fields, market_context, order_fields as build_order, price_text, read_info, signing_key


def load_config(path, require_public_key=False):
    config = json.loads(Path(path).read_text())
    for field in ('coordinator_url', 'info_url', 'websocket_url', 'binance_ticker_url', 'access_client_id',
                  'access_client_secret', 'key_ref', 'vault_address', 'account_address', 'delegate_key_file',
                  'record_path', 'risk_fraction', 'leverage_cap', 'exit_band', 'policy', 'redis', 'control_socket'):
        if field not in config:
            raise ValueError(f'missing_config_{field}')
    if config['account_address'] != config['vault_address']:
        raise ValueError('account_must_match_vault')
    try:
        exit_band = Decimal(str(config['exit_band']))
    except InvalidOperation:
        raise ValueError('invalid_exit_band') from None
    if not exit_band.is_finite() or not 0 < exit_band <= Decimal('0.008'):
        raise ValueError('invalid_exit_band')
    config.setdefault('max_trigger_distance_bps', 1000)
    maximum = config['max_trigger_distance_bps']
    if type(maximum) is not int or not 0 < maximum <= 1000:
        raise ValueError('invalid_max_trigger_distance_bps')
    config.setdefault('resident', True)
    config.setdefault('stop_mode', 'software')
    config.setdefault('bracket_slippage_bps', '50')
    config.setdefault('symbols', {})
    validate_symbols(config['symbols'])
    if not isinstance(config['resident'], bool):
        raise ValueError('invalid_resident')
    if config['stop_mode'] not in ('software', 'trigger'):
        raise ValueError('invalid_stop_mode')
    builders = config.get('builder_dexs', [])
    if not isinstance(builders, list) or any(not isinstance(dex, str) or not dex or ':' in dex for dex in builders):
        raise ValueError('invalid_builder_dexs')
    if require_public_key:
        public_key = config.get('delegate_public_key', '')
        if not isinstance(public_key, str) or len(public_key) != 64:
            raise ValueError('invalid_delegate_public_key')
        try:
            config['delegate_public_key'] = bytes.fromhex(public_key).hex()
        except ValueError:
            raise ValueError('invalid_delegate_public_key') from None
        if len(config['delegate_public_key']) != 64:
            raise ValueError('invalid_delegate_public_key')
    return config


async def oracle_prices(runtime, symbol, exit_request=False):
    seconds = 0.5 if exit_request else 10

    async def observe(source):
        result = {source + '_px': None, source + '_timestamp': None}
        try:
            if source == 'hl_oracle':
                _, context, _ = await market_context(runtime, symbol, timeout_seconds=seconds)
                price = context['oraclePx']
            else:
                mapped = runtime['config'].get('binance_symbols', {}).get(symbol)
                if not mapped:
                    return {**result, source + '_error': 'unmapped'}
                async with runtime['session'].get(runtime['config']['binance_ticker_url'], params={'symbol': mapped},
                                                  timeout=aiohttp.ClientTimeout(total=seconds)) as response:
                    response.raise_for_status()
                    price = (await response.json())['price']
            value = Decimal(str(price))
            if not value.is_finite() or value <= 0:
                raise ValueError('invalid_oracle_price')
            return {source + '_px': str(price), source + '_timestamp': now_ms()}
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, IndexError, InvalidOperation) as error:
            return {**result, source + '_error': type(error).__name__ + ': ' + str(error)}

    oracle, binance = await asyncio.gather(observe('hl_oracle'), observe('binance'))
    return {**oracle, **binance}


def order_fields(signal, size, price, reduce_only=False):
    return build_order(signal, size, price, (signal['side'] == 'long') != reduce_only, reduce_only)


async def entry_preview(runtime, signal):
    config = runtime['config']
    metadata, context, local_index = await market_context(runtime, signal['asset'])
    check_trigger_distance(config, signal, context['markPx'],
                           [('sl', signal['levels']['stop']), ('tp', signal['levels'].get('target'))])
    account = await account_state(runtime, signal['asset'])
    if not config.get('resident', True):
        signal_bracket(config, signal, '1', int(metadata['universe'][local_index]['szDecimals']), context['markPx'])
    sized = size_position(signal, account['marginSummary']['accountValue'], context['markPx'],
                          asset_metadata(metadata, local_index), config['risk_fraction'], config['leverage_cap'])
    entry = price_text(Decimal(signal['levels']['entry']), int(metadata['universe'][local_index]['szDecimals']), signal['side'] == 'long')
    fields = order_fields(signal, sized['size'], entry)
    if not config.get('resident', True):
        fields = signal_bracket(config, signal, sized['size'], int(metadata['universe'][local_index]['szDecimals']), context['markPx'])
    intent = {**fields, 'expires_at': now_ms() // 1000 + 120,
              'venue': 'hyperliquid', 'vault_address': config['vault_address'], 'policy_epoch': config['policy']['epoch']}
    return sized, fields, intent, account
