import asyncio
import json
from decimal import Decimal
from pathlib import Path

from aiohttp import web
from nacl.signing import VerifyKey

from .desk_anchor_verify import payload_hash
from .desk_executor import create_runtime, event_worker, shutdown, submit
from .desk_keygen import generate_key
from .desk_record import append_record, now_ms


def signal_envelope(identifier='sample', asset='BTC'):
    return {'version': 1, 'receipt': None, 'payload': {'signal_id': identifier, 'publisher': 'publisher-placeholder',
            'kind': 'signal', 'symbol': asset, 'venue_symbol': asset, 'venue_hint': 'hyperliquid', 'side': 'long',
            'published_at': now_ms(), 'reply_by': now_ms() + 3600000, 'thesis': 'Scenario',
            'invalidation': 'Stop trades', 'horizon': 'intraday', 'confidence': '0.7',
            'levels': {'entry': '100000', 'stop': '99000', 'target': '102000'}, 'links': [], 'raw': ''}}


def meta(dex=''):
    return {'universe': [{'name': symbol, 'szDecimals': 5, 'maxLeverage': 40} for symbol in (('xyz:OTHER', 'xyz:TSLA') if dex == 'xyz' else ('BTC', 'ETH', 'kPEPE'))],
            'marginTables': [[40, {'marginTiers': [{'lowerBound': '0', 'maxLeverage': 40},
                                                  {'lowerBound': '1000000', 'maxLeverage': 20}]}]]}


def configuration(directory, base):
    return {'coordinator_url': base, 'info_url': base + '/info', 'websocket_url': base + '/ws',
            'builder_dexs': ['xyz'], 'binance_ticker_url': base + '/ticker', 'access_client_id': 'fake-id', 'access_client_secret': 'fake-secret',
            'binance_symbols': {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT'},
            'key_ref': 'fake-key-reference', 'account_address': '0x1111111111111111111111111111111111111111',
            'vault_address': '0x1111111111111111111111111111111111111111',
            'delegate_key_file': str(directory / 'delegate.key'), 'record_path': str(directory / 'record.jsonl'),
            'control_socket': str(directory / 'control.sock'), 'risk_fraction': '0.01', 'leverage_cap': '5', 'exit_band': '0.005',
            'policy': {'epoch': 1, 'venues': ['hyperliquid'], 'vault_address': '0x1111111111111111111111111111111111111111',
                       'expires_at': now_ms() // 1000 + 172800, 'revoked': False, 'revoked_at': 0,
                       'asset_caps': [{'asset': 'BTC', 'max_order_size': '0.02', 'max_daily_size': '0.03'}]},
            'redis': {'host': '127.0.0.1', 'port': 0, 'username': 'reader', 'password': 'fake-password', 'stream': 'signals'}}


def verify_signature(intent):
    unsigned = {key: value for key, value in intent.items() if key != 'delegate_signature'}
    message = json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()
    VerifyKey(bytes.fromhex(intent['delegate_key'])).verify(message, bytes.fromhex(intent['delegate_signature']))


async def coordinator_prepare(request):
    state = request.app['state']
    assert request.headers['CF-Access-Client-Id'] == 'fake-id'
    assert request.headers['CF-Access-Client-Secret'] == 'fake-secret'
    state['prepares'] += 1
    state['events'].append('prepare')
    if state.get('prepare_refused'):
        return web.json_response({'prepared': False, 'reason': 'policy_unavailable'})
    body = await request.json()
    state['nonce'] += 2
    timestamp = now_ms()
    rearm = {'request_id': 'rearm-' + str(state['nonce']), 'key_ref': 'fake-key-reference', 'delegate_key': body['delegate_key'],
             'policy_epoch': 1, 'venue': 'hyperliquid', 'vault_address': body['vault_address'], 'kind': 'schedule_cancel',
             'time_ms': timestamp + 60000, 'venue_nonce': state['nonce'] - 1, 'expires_at': timestamp // 1000 + 120}
    if state.get('bad_rearm'):
        rearm['vault_address'] = '0x2222222222222222222222222222222222222222'
    return web.json_response({'prepared': True, 'key_ref': 'fake-key-reference', 'policy_epoch': 1,
                              'venue_nonce': state['nonce'], 'expires_at': timestamp // 1000 + 120, 'rearm_intent': rearm})


async def coordinator_intent(request):
    state = request.app['state']
    assert request.headers['CF-Access-Client-Secret'] == 'fake-secret'
    body = await request.json()
    intent = body['intent']
    verify_signature(intent)
    verify_signature(body['rearm_intent'])
    state['intents'].append(intent)
    state['events'].append('intent')
    if state.get('stale', 0):
        state['stale'] -= 1
        return web.json_response({'signed': False, 'reason': 'nonce_not_above_highest_signed_99'})
    if state.get('rate_entry') and (intent['kind'] == 'bracket' or intent['kind'] == 'order' and not intent['reduce_only']):
        state['rate_entry'] = False
        return web.Response(status=429, headers={'Retry-After': '0.01'})
    if state.get('rate_exit') and intent.get('reduce_only'):
        state['rate_exit'] = False
        return web.Response(status=429, headers={'Retry-After': '0.01'})
    if intent['kind'] == 'bracket':
        if state.get('bracket_refusal'):
            return web.json_response({'signed': False, 'reason': state['bracket_refusal']})
        entry = intent['entry']
        if state.get('signed_failure') or state.get('unfilled_entry'):
            return web.json_response({'signed': True, 'venue_answer': {'response': {'type': 'order', 'data': {'statuses': [{'error': 'venue_failed'}]}}}})
        assert entry['tif'] == 'Gtc' and not entry['reduce_only']
        state['orders'].append({'coin': entry['asset'], 'oid': 42, 'sz': entry['size'],
                                'reduceOnly': False, 'isTrigger': False})
        statuses = [{'resting': {'oid': 42}}]
        for index, name in enumerate(('stop', 'take_profit'), 43):
            if name in intent:
                leg = intent[name]
                assert leg['reduce_only'] and leg['is_market']
                state['orders'].append({'coin': leg['asset'], 'oid': index, 'sz': leg['size'],
                                        'reduceOnly': True, 'isTrigger': True, 'orderType': 'Stop Market' if name == 'stop' else 'Take Profit Market',
                                        'triggerPx': leg['trigger_px'], 'limitPx': leg['limit_px']})
                statuses.append({'resting': {'oid': index}})
        if state.get('resident'):
            filled = state.get('partial_entry', entry['size'])
            if Decimal(filled) == Decimal(entry['size']):
                state['orders'] = [order for order in state['orders'] if order['oid'] != 42]
            else:
                state['orders'][0]['sz'] = str(Decimal(entry['size']) - Decimal(filled))
            fill_fake(state, {**entry, 'size': filled})
            if state.get('dead_on_fill'):
                for websocket in tuple(state['websockets']):
                    await websocket.close()
            if state.get('timeout_entry'):
                await asyncio.sleep(0.12)
            if state.get('after_entry'):
                state['after_entry']()
    elif intent['kind'] == 'trigger_order':
        if state.get('trigger_refused'):
            reason = state['trigger_refused'] if isinstance(state['trigger_refused'], str) else 'unsupported_kind'
            return web.json_response({'signed': False, 'reason': reason})
        oid = 1000 + len(state['intents'])
        state['orders'].append({'coin': intent['asset'], 'oid': oid, 'sz': intent['size'], 'reduceOnly': True,
                                'isTrigger': True, 'orderType': 'Stop Market', 'triggerPx': intent['trigger_px']})
        statuses = [{'resting': {'oid': oid}}]
    elif intent['kind'] == 'cancel':
        state['orders'] = [order for order in state['orders'] if order['oid'] != intent['oid']]
        statuses = ['success']
    elif intent.get('tif') == 'Alo':
        oid = 1000 + len(state['intents'])
        state['orders'].append({'coin': intent['asset'], 'oid': oid, 'sz': intent['size'], 'limitPx': intent['limit_px'],
                                'reduceOnly': True, 'isTrigger': False, 'side': 'B' if intent['is_buy'] else 'A'})
        statuses = [{'resting': {'oid': oid}}]
    elif state.get('signed_failure'):
        statuses = [{'error': 'venue_failed'}]
    elif state.get('unfilled_entry') and not intent['reduce_only']:
        statuses = [{'error': 'Ioc order would have no immediate match'}]
    elif state.get('match_exit_mark') and intent['reduce_only'] and (
            Decimal(intent['limit_px']) < Decimal(state['mark']) if intent['is_buy'] else Decimal(intent['limit_px']) > Decimal(state['mark'])):
        statuses = [{'error': 'Ioc order would have no immediate match'}]
    else:
        assert intent['tif'] == 'Ioc'
        assert not state.get('resident', True) or 'watcher_armed' in state['events']
        size = Decimal(intent['size'])
        if intent['reduce_only']:
            size = min(size, abs(state['position']))
            if state.get('partial_exit'):
                size /= 2
                state['partial_exit'] = False
        state['position_asset'] = intent['asset']
        state['position'] += size if intent['is_buy'] else -size
        fill = {'coin': intent['asset'], 'sz': str(size), 'px': intent['limit_px'], 'side': 'B' if intent['is_buy'] else 'A',
                'time': now_ms(), 'tid': len(state['fills']) + 1, 'oid': len(state['fills']) + 100}
        state['fills'].append(fill)
        statuses = [{'filled': {'totalSz': str(size), 'avgPx': intent['limit_px'], 'oid': fill['oid']}}]
        if state.get('dead_on_fill') and not intent['reduce_only']:
            for websocket in tuple(state['websockets']):
                await websocket.close()
            await asyncio.sleep(0.01)
        if state.get('timeout_entry') and not intent['reduce_only']:
            await asyncio.sleep(0.12)
        if state.get('after_entry') and not intent['reduce_only']:
            state['after_entry']()
    return web.json_response({'signed': True, 'request_id': intent['request_id'], 'venue_status': 'ok',
                              'venue_answer': {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': statuses}}}})


async def info(request):
    state = request.app['state']
    body = await request.json()
    kind = body['type']
    state['events'].append(kind)
    state.setdefault('info_requests', []).append(body)
    dex = body.get('dex', '')
    current_meta = meta(dex)
    if state.get('ambiguous_symbol') and dex == 'xyz':
        current_meta['universe'].append({'name': 'Kpepe', 'szDecimals': 5, 'maxLeverage': 40})
    if kind == 'perpDexs':
        result = [None, {'name': 'other'}, {'name': 'xyz'}]
    elif kind == 'meta':
        result = current_meta
    elif kind == 'metaAndAssetCtxs':
        if state.get('oracle_down'):
            return web.Response(status=503)
        result = [current_meta, [{'markPx': state.get('mark', '100000'), 'oraclePx': '100001'} for asset in current_meta['universe']]]
    elif kind in ('openOrders', 'frontendOpenOrders'):
        result = [order for order in state['orders'] if (order['coin'].split(':')[0] if ':' in order['coin'] else '') == dex]
    elif kind == 'userRateLimit':
        result = {'nRequestsCap': 10000, 'nRequestsUsed': state.get('requests_used', 0)}
    elif kind == 'candleSnapshot':
        start = body['req']['startTime']
        result = [{'t': start, 'T': start + 899999, 'c': state.get('candle_close', '100000')}]
    elif kind == 'userFills':
        result = state['fills']
    elif kind == 'userAbstraction':
        if state.get('account_mode_unavailable'):
            return web.Response(status=503)
        result = state.get('account_mode', 'disabled')
    elif kind == 'spotClearinghouseState':
        result = {'balances': state.get('spot_balances', [])}
    elif kind == 'clearinghouseState':
        result = {'marginSummary': {'accountValue': state.get('perp_equity', '1000')}, 'assetPositions': []}
        symbol = state.get('position_asset', 'BTC')
        if state['position'] and (symbol.split(':')[0] if ':' in symbol else '') == dex:
            result['assetPositions'] = [{'position': {'coin': symbol, 'szi': str(state['position']), 'entryPx': '100000'}}]
    else:
        raise AssertionError(kind)
    return web.json_response(result)


async def ticker(request):
    state = request.app['state']
    state['events'].append('ticker')
    state.setdefault('ticker_symbols', []).append(request.query['symbol'])
    assert request.query['symbol'] in ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
    if state.get('ticker_slow'):
        await asyncio.sleep(1)
    if state.get('ticker_down'):
        return web.Response(status=503)
    return web.json_response({'price': '100002'})


async def websocket_handler(request):
    state = request.app['state']
    if state.get('fail_reconnect') or state.get('dead_on_fill') and 'intent' in state['events']:
        return web.Response(status=503)
    websocket = web.WebSocketResponse()
    await websocket.prepare(request)
    state['websockets'].append(websocket)
    subscriptions = []
    try:
        async for message in websocket:
            command = json.loads(message.data)
            subscriptions.append(command['subscription']['type'])
            if set(subscriptions) == {'trades', 'l2Book', 'userFills'}:
                state['events'].append('watcher_armed')
            await websocket.send_json({'channel': 'subscriptionResponse', 'data': command})
    finally:
        state['websockets'].remove(websocket)
    return websocket


async def start_fake(directory, session):
    state = {'resident': True, 'prepares': 0, 'nonce': 100, 'intents': [], 'events': [], 'position': Decimal(0), 'orders': [], 'fills': [], 'websockets': []}
    application = web.Application()
    application['state'] = state
    application.add_routes([web.post('/prepare', coordinator_prepare), web.post('/intent', coordinator_intent),
                            web.post('/info', info), web.get('/ticker', ticker), web.get('/ws', websocket_handler)])
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    config = configuration(directory, f'http://127.0.0.1:{port}')
    generate_key(config)
    runtime = create_runtime(config, session)
    runtime['worker'] = asyncio.create_task(event_worker(runtime))
    return runtime, state, runner


async def stop_fake(runtime, runner):
    await shutdown(runtime)
    await runner.cleanup()


async def trade(state, price, symbol='BTC'):
    for websocket in tuple(state['websockets']):
        await websocket.send_json({'channel': 'trades', 'data': [{'coin': symbol, 'px': str(price), 'time': now_ms()}]})


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


def record_anchor_fixture(config, envelope):
    if not config.get('resident', True):
        append_record(config['record_path'], 'anchor_verified',
                      signal_id=envelope['payload']['signal_id'], payload_hash=payload_hash(envelope))


async def submit_verified_fixture(runtime, envelope):
    record_anchor_fixture(runtime['config'], envelope)
    return await submit(runtime, envelope)


def fill_fake(state, fields):
    size = Decimal(fields['size'])
    if fields.get('reduce_only'):
        size = min(size, abs(state['position']))
    state['position_asset'] = fields['asset']
    state['position'] += size if fields['is_buy'] else -size
    state['fills'].append({'coin': fields['asset'], 'sz': str(size), 'px': fields['limit_px'],
                           'side': 'B' if fields['is_buy'] else 'A', 'time': now_ms(),
                           'tid': len(state['fills']) + 1, 'oid': len(state['fills']) + 100})


def fill_passive(state):
    for order in list(state['orders']):
        if order.get('reduceOnly') and not order.get('isTrigger'):
            fill_fake(state, {'size': order['sz'], 'asset': order['coin'], 'limit_px': order['limitPx'],
                              'reduce_only': True, 'is_buy': order['side'] == 'B'})
            state['orders'].remove(order)
