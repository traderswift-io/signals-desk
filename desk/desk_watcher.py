import asyncio
import json

import aiohttp

from .desk_record import append_record


def queue_event(runtime, ticket, event):
    if event.get('channel') == 'trades':
        if 'pending_trades' in ticket:
            ticket['pending_trades']['data'].extend(event.get('data', []))
            return
        ticket['pending_trades'] = {**event, 'data': list(event.get('data', []))}
    runtime['events'].put_nowait((ticket, event))


async def connect_watcher(runtime, ticket):
    ticket['websocket'] = await runtime['session'].ws_connect(runtime['config']['websocket_url'], heartbeat=10, receive_timeout=30)
    for subscription in ({'type': 'trades', 'coin': ticket['signal']['asset']},
                         {'type': 'userFills', 'user': runtime['config']['vault_address']}):
        await ticket['websocket'].send_json({'method': 'subscribe', 'subscription': subscription})
    subscriptions = set()
    async for message in ticket['websocket']:
        if message.type != aiohttp.WSMsgType.TEXT:
            break
        payload = json.loads(message.data)
        if payload.get('channel') == 'subscriptionResponse':
            subscription = payload.get('data', {}).get('subscription', {})
            if subscription.get('type') == 'trades' and subscription.get('coin') == ticket['signal']['asset']:
                subscriptions.add('trades')
            if subscription.get('type') == 'userFills' and subscription.get('user') == runtime['config']['vault_address']:
                subscriptions.add('userFills')
            if subscriptions == {'trades', 'userFills'}:
                return
        elif payload.get('channel') in ('trades', 'userFills'):
            queue_event(runtime, ticket, payload)
    raise ValueError('watcher_not_alive')


async def websocket_reconnect(runtime, ticket):
    restarted = False
    try:
        while not ticket['stopping']:
            try:
                await asyncio.wait_for(connect_watcher(runtime, ticket), timeout=10)
            except (aiohttp.ClientError, ValueError, TimeoutError):
                await runtime['events'].put((ticket, {'channel': 'watcher_dead'}))
                return
            ticket['alive'] = True
            ticket['ready'].set()
            if restarted:
                await runtime['events'].put((ticket, {'channel': 'watcher_restarted'}))
            try:
                async for message in ticket['websocket']:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        break
                    payload = json.loads(message.data)
                    if payload.get('channel') in ('trades', 'userFills'):
                        queue_event(runtime, ticket, payload)
            except (aiohttp.ClientError, ValueError, TimeoutError):
                pass
            ticket['alive'] = False
            await ticket['websocket'].close()
            append_record(runtime['config']['record_path'], 'watcher_disconnected', key=ticket['key'])
            restarted = True
    finally:
        ticket['alive'] = False
        ticket['ready'].set()


async def arm(runtime, ticket):
    ticket['ready'] = asyncio.Event()
    ticket['alive'] = False
    ticket['stopping'] = False
    ticket['receiver'] = asyncio.create_task(websocket_reconnect(runtime, ticket))
    await ticket['ready'].wait()
    if not ticket['alive']:
        raise ValueError('watcher_not_alive')
    append_record(runtime['config']['record_path'], 'watcher_armed', key=ticket['key'])


async def disarm(ticket):
    ticket['stopping'] = True
    ticket['alive'] = False
    if ticket.get('receiver'):
        ticket['receiver'].cancel()
        await asyncio.gather(ticket['receiver'], return_exceptions=True)
    if ticket.get('websocket'):
        await ticket['websocket'].close()
