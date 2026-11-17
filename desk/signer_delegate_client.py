import asyncio
import json
import time
import uuid
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
from nacl.signing import SigningKey

REPLY_JOURNAL_FIELDS = ('request_id', 'signed', 'ambiguous', 'reason', 'venue_status', 'signature',
                        'prepared', 'key_ref', 'policy_epoch', 'venue_nonce', 'expires_at',
                        'intent', 'rearm_intent', 'venue_answer')

REARM_FIELDS = frozenset({'request_id', 'key_ref', 'delegate_key', 'policy_epoch', 'venue', 'vault_address',
                          'kind', 'time_ms', 'venue_nonce', 'expires_at'})


def sign_intent(intent, key):
    unsigned = {name: value for name, value in intent.items() if name != 'delegate_signature'}
    message = json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()
    return {**unsigned, 'delegate_signature': key.sign(message).signature.hex()}


def rearm_refusal(rearm, config, prepared, timestamp):
    if not isinstance(rearm, dict) or rearm.get('kind') != 'schedule_cancel':
        return 'not_schedule_cancel'
    if set(rearm) != REARM_FIELDS:
        return 'unexpected_fields'
    for field, reason in (('key_ref', 'wrong_key_ref'), ('delegate_key', 'wrong_delegate_key'),
                          ('vault_address', 'wrong_vault')):
        expected = config['delegate_public_key'] if field == 'delegate_key' else config[field]
        if rearm[field] != expected:
            return reason
    if rearm['venue'] != 'hyperliquid':
        return 'wrong_venue'
    if rearm['policy_epoch'] != prepared['policy_epoch']:
        return 'wrong_policy_epoch'
    if not isinstance(rearm['time_ms'], int) or isinstance(rearm['time_ms'], bool):
        return 'time_missing'
    if rearm['time_ms'] < timestamp + 15000:
        return 'time_too_soon'
    if rearm['time_ms'] > timestamp + 600000:
        return 'time_too_far'
    if not isinstance(rearm['expires_at'], int) or rearm['expires_at'] <= timestamp // 1000:
        return 'expiry_passed'
    if rearm['expires_at'] > timestamp // 1000 + 120:
        return 'expiry_too_far'
    if not isinstance(rearm['venue_nonce'], int) or isinstance(rearm['venue_nonce'], bool):
        return 'bad_nonce'
    return None


def retry_delay(header, fallback):
    try:
        return max(0, float(header))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(header).timestamp() - now_ms() / 1000)
        except (TypeError, ValueError, AttributeError):
            return fallback


async def coordinator_backoff(runtime):
    timestamp = now_ms()
    runtime['request_times'] = [value for value in runtime['request_times'] if value > timestamp - 60000]
    remaining = max(0, runtime.get('backoff_until', 0) - timestamp)
    if len(runtime['request_times']) >= 60:
        remaining = max(remaining, runtime['request_times'][0] + 60000 - timestamp)
    if remaining:
        await asyncio.sleep(remaining / 1000)


async def door_request(runtime, route, body, exit_request, before_send, on_event):
    config = runtime['config']
    delay = 2
    while True:
        await coordinator_backoff(runtime)
        runtime['request_times'].append(now_ms())
        if route == 'intent':
            intent = body['intent']
            if exit_request and (intent['expires_at'] <= now_ms() // 1000 or rearm_refusal(
                    {key: value for key, value in body['rearm_intent'].items() if key != 'delegate_signature'},
                    config, intent, now_ms()) in ('time_too_soon', 'expiry_passed')):
                return {'signed': False, 'reason': 'refresh_before_send'}
            if intent['expires_at'] <= now_ms() // 1000:
                return {'signed': False, 'reason': 'intent_expired'}
            if before_send:
                refusal = await before_send(body)
                if refusal:
                    return {'signed': False, 'reason': refusal}
        try:
            headers = {'CF-Access-Client-Id': config['access_client_id'], 'CF-Access-Client-Secret': config['access_client_secret']}
            async with runtime['session'].post(config['coordinator_url'].rstrip('/') + '/' + route,
                                               json=body, headers=headers,
                                               timeout=aiohttp.ClientTimeout(total=config.get('request_timeout_seconds', 120))) as response:
                if response.status == 429:
                    seconds = retry_delay(response.headers.get('Retry-After'), delay)
                    runtime['backoff_until'] = now_ms() + int(seconds * 1000)
                    emit(on_event, 'backoff', route=route, seconds=seconds,
                                  reason='http_429', retry=exit_request)
                    emit(on_event, 'reply' if route == 'intent' else 'prepare_reply',
                         request_id=body.get('intent', {}).get('request_id'), signed=False, reason='http_429')
                    if not exit_request:
                        return {'signed': False, 'reason': 'http_429'}
                    delay = min(delay * 2, 30)
                    continue
                if response.status >= 500:
                    result = {'signed': None, 'reason': 'ambiguous_http_status', 'ambiguous': True}
                else:
                    result = await response.json()
                    if not isinstance(result, dict):
                        raise ValueError('invalid_coordinator_reply')
                    if response.status >= 400:
                        result = {'signed': False, 'reason': result.get('reason', 'http_refused')}
        except (TimeoutError, aiohttp.ClientError, ValueError):
            result = {'signed': None, 'reason': 'ambiguous_transport', 'ambiguous': True}
        if route == 'intent' and result.get('signed') is not False:
            detail = str(result.get('reason', '')) + str(result.get('venue_status', ''))
            if result.get('signed') is not True or not result.get('venue_answer') or any(
                    word in detail.lower() for word in ('timeout', 'unknown', 'ambiguous')):
                result['ambiguous'] = True
        emit(on_event, 'reply' if route == 'intent' else 'prepare_reply',
             **{**{name: value for name, value in result.items() if name in REPLY_JOURNAL_FIELDS},
                'request_id': body.get('intent', {}).get('request_id')})
        return result


async def run_round(runtime, fields, before_send=None, validate_rearm=None, on_event=None):
    config = runtime['config']
    exit_request = fields['kind'] == 'cancel' or fields.get('reduce_only', False)
    async with runtime['round_lock']:
        nonce_retries = 0
        while True:
            prepared = await door_request(runtime, 'prepare', {'delegate_key': config['delegate_public_key'],
                                          'vault_address': config['vault_address']}, exit_request, before_send, on_event)
            if not prepared.get('prepared'):
                return {'signed': False, 'reason': 'prepare_refused: ' + str(prepared.get('reason'))}
            if prepared.get('key_ref') != config['key_ref']:
                return {'signed': False, 'reason': 'client_refused_prepare: wrong_key_ref'}
            if any(not isinstance(prepared.get(field), int) or isinstance(prepared[field], bool)
                   for field in ('policy_epoch', 'venue_nonce', 'expires_at')):
                return {'signed': False, 'reason': 'client_refused_prepare: invalid_fields'}
            rearm = prepared.get('rearm_intent')
            if rearm is None:
                return {'signed': False, 'reason': 'client_refused_rearm_intent: missing_rearm'}
            refusal = rearm_refusal(rearm, config, prepared, now_ms())
            if not refusal and validate_rearm:
                refusal = validate_rearm(rearm)
            if refusal:
                return {'signed': False, 'reason': 'client_refused_rearm_intent: ' + refusal}
            expiry = min(prepared['expires_at'], now_ms() // 1000 + 120)
            if not exit_request:
                expiry = min(expiry, rearm['time_ms'] // 1000)
            intent = {'request_id': uuid.uuid4().hex, 'key_ref': config['key_ref'],
                      'delegate_key': config['delegate_public_key'], 'policy_epoch': prepared['policy_epoch'],
                      'venue': 'hyperliquid', 'vault_address': config['vault_address'],
                      'venue_nonce': prepared['venue_nonce'], 'expires_at': expiry, **fields}
            body = {'intent': sign_intent(intent, runtime['signing_key']), 'rearm_intent': sign_intent(rearm, runtime['signing_key'])}
            outcome = await door_request(runtime, 'intent', body, exit_request, before_send, on_event)
            if exit_request and outcome.get('reason') == 'refresh_before_send':
                continue
            if outcome.get('ambiguous'):
                outcome['reconciliation'] = await reconcile_asset(runtime, fields['asset'])
                return {**outcome, 'intent': body['intent']}
            if outcome.get('signed') or 'nonce_not_above_highest_signed_' not in str(outcome.get('reason')) or nonce_retries:
                return {**outcome, 'intent': body['intent']}
            nonce_retries += 1
            emit(on_event, 'nonce_retry', reason=outcome['reason'])


def now_ms():
    return time.time_ns() // 1000000


def emit(callback, event, **fields):
    if callback:
        callback(event, **fields)


def create_client(config, session, key=None):
    key = key if key is not None else signing_key(config)
    return {'config': {**config, 'delegate_public_key': key.verify_key.encode().hex()},
            'session': session, 'signing_key': key, 'round_lock': asyncio.Lock(), 'request_times': [], 'asset_indices': {}}


def combine_meta(main_meta, builder_metas, perp_dexs):
    universe = [{**asset, 'asset_index': index, 'dex': '', 'universe_index': index}
                for index, asset in enumerate(main_meta['universe'])]
    for dex, meta in builder_metas.items():
        matches = [index for index, row in enumerate(perp_dexs) if row and row.get('name') == dex]
        if len(matches) != 1:
            raise ValueError('builder_dex_unmapped' if not matches else 'builder_dex_ambiguous')
        for index, asset in enumerate(meta['universe']):
            # Venue quirk: builder assets use 100000 + perpDexs slot * 10000 + local universe index.
            asset_index = 100000 + matches[0] * 10000 + index
            universe.append({**asset, 'asset_index': asset_index, 'dex': dex, 'universe_index': index})
    return {'universe': universe}


def resolve_symbol(symbol, meta):
    matches = [(index, asset) for index, asset in enumerate(meta['universe']) if asset['name'].casefold() == symbol.casefold()]
    if len(matches) != 1:
        raise ValueError('symbol_unmapped' if not matches else 'symbol_ambiguous')
    index, asset = matches[0]
    if asset.get('isDelisted'):
        raise ValueError('delisted_symbol')
    return {**asset, 'asset': asset['name'], 'asset_index': asset.get('asset_index', index)}


async def refresh_meta(runtime):
    main = await read_info(runtime, 'meta')
    dex_names = list(dict.fromkeys(runtime['config'].get('builder_dexs', [])))
    perp_dexs = await read_info(runtime, 'perpDexs') if dex_names else []
    builders = {dex: await read_info(runtime, 'meta', dex=dex) for dex in dex_names}
    meta = combine_meta(main, builders, perp_dexs)
    runtime['meta'] = meta
    runtime['asset_indices'] = {asset['name']: asset['asset_index'] for asset in meta['universe']}
    return meta


def dex_fields(symbol):
    return {'dex': symbol.split(':', 1)[0]} if ':' in symbol else {}


async def market_context(runtime, symbol, timeout_seconds=10):
    meta, contexts = await read_info(runtime, 'metaAndAssetCtxs', timeout_seconds=timeout_seconds, **dex_fields(symbol))
    matches = [index for index, asset in enumerate(meta['universe']) if asset['name'] == symbol]
    if len(matches) != 1:
        raise ValueError('symbol_unmapped' if not matches else 'symbol_ambiguous')
    index = matches[0]
    return meta, contexts[index], index


async def reconcile_asset(runtime, symbol):
    account = runtime['config']['account_address']
    orders = await read_info(runtime, 'frontendOpenOrders',
                             user=account, **dex_fields(symbol))
    fills = await read_info(runtime, 'userFills', user=account)
    state = await account_state(runtime, symbol)
    return {'orders': [order for order in orders if order['coin'] == symbol],
            'fills': [fill for fill in fills if fill['coin'] == symbol], 'account': state}


def signing_key(config):
    return SigningKey(bytes.fromhex(Path(config['delegate_key_file']).read_text().strip()))


async def read_info(runtime, kind, timeout_seconds=10, **fields):
    async with runtime['session'].post(runtime['config']['info_url'], json={'type': kind, **fields},
                                       timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as response:
        response.raise_for_status()
        return await response.json()


async def account_state(runtime, symbol=''):
    return await read_info(runtime, 'clearinghouseState', user=runtime['config']['account_address'], **dex_fields(symbol))


async def account_equity(runtime, symbol):
    user = runtime['config']['account_address']
    try:
        mode = await read_info(runtime, 'userAbstraction', user=user)
    except (aiohttp.ClientError, OSError, ValueError, TypeError):
        raise ValueError('account_mode_unknown') from None
    if mode not in ('unifiedAccount', 'portfolioMargin', 'disabled', 'default'):
        raise ValueError('account_mode_unknown')
    try:
        account = await account_state(runtime, symbol)
        perp_equity = Decimal(str(account['marginSummary']['accountValue']))
        spot_usdc_equity = Decimal(0)
        if mode in ('unifiedAccount', 'portfolioMargin'):
            spot = await read_info(runtime, 'spotClearinghouseState', user=user)
            if not isinstance(spot['balances'], list):
                raise ValueError('invalid_account_equity')
            balances = [balance for balance in spot['balances'] if balance['coin'] == 'USDC']
            if len(balances) > 1:
                raise ValueError('invalid_account_equity')
            if balances:
                spot_usdc_equity = Decimal(str(balances[0]['total']))
        if any(not value.is_finite() or value < 0 for value in (perp_equity, spot_usdc_equity)):
            raise ValueError('invalid_account_equity')
        equity = perp_equity + spot_usdc_equity
        if not equity.is_finite():
            raise ValueError('invalid_account_equity')
    except (aiohttp.ClientError, OSError, ValueError, TypeError, KeyError, DecimalException):
        raise ValueError('invalid_account_equity') from None
    return {'equity': equity, 'account_mode': mode, 'perp_equity': perp_equity,
            'spot_usdc_equity': spot_usdc_equity, 'account': account}


def price_text(price, size_decimals, is_buy):
    if price == price.to_integral_value():
        return format(price, 'f')
    decimal_tick = Decimal(1).scaleb(-(6 - size_decimals))
    significant_tick = Decimal(1).scaleb(price.adjusted() - 4)
    tick = min(max(decimal_tick, significant_tick), Decimal(1))
    rounded = price.quantize(tick, rounding=ROUND_CEILING if is_buy else ROUND_FLOOR)
    return format(rounded.normalize(), 'f')


def order_fields(asset, size, price, is_buy, reduce_only=False):
    return {'kind': 'order', 'asset_index': asset['asset_index'], 'asset': asset['asset'],
            'is_buy': is_buy, 'limit_px': str(price),
            'size': str(size), 'reduce_only': reduce_only, 'tif': 'Ioc'}


def decimal_text(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('trigger_price_outside_band') from None
    if not number.is_finite() or number <= 0:
        raise ValueError('trigger_price_outside_band')
    return format(number.normalize(), 'f')


def bracket_fields(asset, size, entry_price, is_buy, stop_trigger, stop_limit,
                   take_profit_trigger=None, take_profit_limit=None):
    entry = order_fields(asset, decimal_text(size), decimal_text(entry_price), is_buy)
    entry['tif'] = 'Gtc'
    fields = {'kind': 'bracket', 'asset': asset['asset'], 'asset_index': asset['asset_index'], 'entry': entry}
    for name, trigger, limit, tpsl in [('stop', stop_trigger, stop_limit, 'sl'),
                                      ('take_profit', take_profit_trigger, take_profit_limit, 'tp')]:
        if name == 'take_profit' and trigger is None and limit is None:
            continue
        trigger, limit = decimal_text(trigger), decimal_text(limit)
        reference = Decimal(entry['limit_px'])
        wrong_side = Decimal(trigger) >= reference if is_buy == (tpsl == 'sl') else Decimal(trigger) <= reference
        if wrong_side:
            raise ValueError('stop_on_wrong_side_of_entry' if tpsl == 'sl' else 'take_profit_on_wrong_side_of_entry')
        if abs(Decimal(limit) - Decimal(trigger)) > Decimal(trigger) / 2 or any(
                abs(Decimal(value) - reference) > reference / 2 for value in (trigger, limit)):
            raise ValueError('trigger_price_outside_band')
        fields[name] = {'asset': asset['asset'], 'asset_index': asset['asset_index'], 'is_buy': not is_buy,
                        'size': entry['size'], 'reduce_only': True, 'is_market': True,
                        'trigger_px': trigger, 'limit_px': limit, 'tpsl': tpsl}
    return fields


def cancel_fields(asset, order_id):
    return {'kind': 'cancel', 'asset_index': asset['asset_index'], 'asset': asset['asset'], 'oid': order_id}


def order_statuses(outcome):
    answer = outcome.get('venue_answer') or {}
    response = answer.get('response') if isinstance(answer, dict) else None
    data = response.get('data') if isinstance(response, dict) else None
    return data.get('statuses', []) if isinstance(data, dict) else []
