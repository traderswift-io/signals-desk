import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path

import aiohttp

from .desk_anchor_verify import anchor_refusal, decode_json
from .desk_bracket import entry_candle_guard
from .desk_routine import bracket_result, maintain_routine, recover_routine
from .desk_coordinator import run_round
from .desk_io import entry_preview, load_config, order_fields
from .desk_private_storage import prepare_private_storage
from .signer_delegate_client import account_state, cancel_fields, create_client, dex_fields, market_context, price_text, read_info, reconcile_asset, refresh_meta, resolve_symbol
from .desk_record import already_acted, append_record, journal_guard, latest_tickets, now_ms, review, set_state, signed_today, ticket_key, writer_lock
from .desk_risk_gate import evaluate_intent
from .desk_signal import parse_signal
from .desk_watcher import arm, disarm


def create_runtime(config, session):
    return {**create_client(config, session), 'operation_lock': asyncio.Lock(), 'events': asyncio.Queue(), 'tickets': {}}


async def reconcile(runtime, ticket):
    config, signal = runtime['config'], ticket['signal']
    snapshot = ticket.pop('reconciliation', None)
    if snapshot is None:
        snapshot = await reconcile_asset(runtime, signal['asset'])
    fills, account = snapshot['fills'], snapshot['account']
    ticket['orders'] = snapshot['orders']
    position = next((row['position'] for row in account['assetPositions'] if row['position']['coin'] == signal['asset']), {})
    ticket['position'] = Decimal(position.get('szi', '0'))
    matching = [fill for fill in fills if fill['coin'] == signal['asset'] and fill['time'] >= ticket['claimed_at']
                and fill.get('side') == ('B' if signal['side'] == 'long' else 'A')]
    if matching:
        total = sum((Decimal(fill['sz']) for fill in matching), Decimal(0))
        if total:
            ticket['fill_price'] = str(sum((Decimal(fill['px']) * Decimal(fill['sz']) for fill in matching), Decimal(0)) / total)
    append_record(config['record_path'], 'reconcile', key=ticket['key'], orders=ticket['orders'],
                  fills=matching, position=ticket['position'])
    if ticket['position'] and ticket['state'] != 'filled':
        set_state(config, ticket, 'filled', fill_price=ticket.get('fill_price') or position.get('entryPx'))
        ticket['fill_price'] = ticket.get('fill_price') or position.get('entryPx')
    return ticket['position']


async def refuse(runtime, ticket, reason):
    set_state(runtime['config'], ticket, 'refused', reason=reason)
    review(runtime['config'], ticket)
    await disarm(ticket)
    return {'refused': reason}


async def close_ticket(runtime, ticket, reason, trigger=None):
    config, signal = runtime['config'], ticket['signal']
    ticket['closing'] = reason
    if reason not in ('stop', 'target'):
        await reconcile(runtime, ticket)
    for order in ticket['orders']:
        outcome = await run_round(runtime, ticket, cancel_fields(signal, order['oid']))
        append_record(config['record_path'], 'cancel_result', key=ticket['key'], outcome=outcome)
        await reconcile(runtime, ticket)
        if any(current['oid'] == order['oid'] for current in ticket['orders']):
            return {'refused': outcome.get('reason', 'cancel_unconfirmed')}
    while ticket['position']:
        buy = ticket['position'] < 0
        if trigger is None:
            _, context, _ = await market_context(runtime, signal['asset'])
            reference = Decimal(context['markPx'])
        else:
            reference = Decimal(trigger)
        asset = resolve_symbol(signal['asset'], runtime['meta'])
        band = Decimal(config['exit_band'])
        price = price_text(reference * (1 + band if buy else 1 - band), int(asset['szDecimals']), buy)
        fields = {**order_fields(signal, abs(ticket['position']), price, True), 'is_buy': buy}
        outcome = await run_round(runtime, ticket, fields)
        append_record(config['record_path'], 'exit_result', key=ticket['key'], reason=reason, outcome=outcome)
        await reconcile(runtime, ticket)
        trigger = None
        if outcome.get('ambiguous') and ticket['position']:
            ticket['uncertain'] = True
            return {'refused': 'ambiguous_exit_held'}
        if not outcome.get('signed'):
            return {'refused': outcome.get('reason', 'exit_refused')}
    if ticket.get('uncertain'):
        return {'refused': 'ambiguous_entry_held'}
    set_state(config, ticket, 'exited', reason=reason)
    review(config, ticket)
    await disarm(ticket)
    return {'state': 'exited'}


async def submit(runtime, envelope):
    async with runtime['operation_lock']:
        config = runtime['config']
        if config.get('resident', True):
            with journal_guard(config) as refusal:
                latest_tickets(config)
            if refusal:
                return refusal
        if not config.get('resident', True):
            try:
                envelope = decode_json(envelope) if isinstance(envelope, str) else envelope
                reason = anchor_refusal(config, envelope)
            except (ValueError, TypeError, RecursionError):
                reason = 'anchor_not_verified'
            if reason:
                with journal_guard(config):
                    append_record(config['record_path'], 'refusal', reason=reason)
                return {'refused': reason}
        raw = json.loads(envelope) if isinstance(envelope, str) else envelope
        payload = raw.get('payload', {})
        try:
            signal = parse_signal(raw, await refresh_meta(runtime), now_ms())
        except (ValueError, KeyError, TypeError) as error:
            identity = payload.get('signal_id')
            details = {'signal_id': identity} if type(identity) is str and identity.strip() else {}
            append_record(config['record_path'], 'refusal', reason=str(error), **details)
            return {'refused': str(error)}
        if signal.get('kind') == 'cancel':
            return await cancel_locked(runtime, signal['signal_id'], signal['publisher'])
        if not config.get('resident', True):
            try:
                reason = await entry_candle_guard(runtime, signal)
            except (ValueError, KeyError, TypeError, aiohttp.ClientError, TimeoutError) as error:
                reason = str(error)
            if reason:
                append_record(config['record_path'], 'refusal', signal_id=signal['signal_id'], reason=reason)
                return {'refused': reason}
        key = ticket_key(config, signal)
        with journal_guard(config) as refusal:
            acted = already_acted(config['record_path'], key)
        if refusal:
            return refusal
        if acted:
            append_record(config['record_path'], 'redelivery', key=key, reason='already_acted')
            return {'refused': 'already_acted'}
        ticket = {'key': key, 'signal': signal, 'claimed_at': now_ms(), 'position': Decimal(0), 'orders': [], 'alive': False, 'done': asyncio.Event()}
        ticket['close_ms'] = runtime.get('close_ms')
        set_state(config, ticket, 'claimed')
        runtime['tickets'][signal['signal_id']] = ticket
        try:
            sized, fields, gate_intent, account = await entry_preview(runtime, signal)
            with journal_guard(config) as refusal:
                budget = signed_today(config, signal['asset'])
                reason = evaluate_intent(gate_intent, config['policy'], runtime['asset_indices'], budget,
                                         now_ms(), signal['reply_by'])
            if refusal:
                return refusal
            append_record(config['record_path'], 'gate', key=key, reason=reason, sizing=sized,
                          entry_price=fields.get('entry', fields)['limit_px'], intent=gate_intent)
            if reason:
                return await refuse(runtime, ticket, reason)
            if any(other is not ticket and other['state'] in ('claimed', 'sent', 'filled') for other in runtime['tickets'].values()):
                return await refuse(runtime, ticket, 'account_busy')
            orders = await read_info(runtime, 'openOrders', user=config['account_address'], **dex_fields(signal['asset']))
            if orders or any(Decimal(row['position']['szi']) for row in account['assetPositions']):
                return await refuse(runtime, ticket, 'account_not_flat')
            for dex in ['', *config.get('builder_dexs', [])]:
                if dex == dex_fields(signal['asset']).get('dex', ''):
                    continue
                other_symbol = dex + ':' if dex else ''
                other_account = await account_state(runtime, other_symbol)
                other_orders = await read_info(runtime, 'openOrders', user=config['account_address'], **dex_fields(other_symbol))
                if other_orders or any(Decimal(row['position']['szi']) for row in other_account['assetPositions']):
                    return await refuse(runtime, ticket, 'account_not_flat')
            if config.get('resident', True):
                await arm(runtime, ticket)
            outcome = await run_round(runtime, ticket, fields)
            if not config.get('resident', True):
                if outcome.get('reason') == 'anchor_not_verified':
                    return {'refused': 'anchor_not_verified'}
                return await bracket_result(runtime, ticket, outcome, reconcile)
            if outcome.get('ambiguous'):
                ticket['uncertain'] = True
            await reconcile(runtime, ticket)
            if ticket['position']:
                ticket['uncertain'] = False
                if not ticket['alive']:
                    return await close_ticket(runtime, ticket, 'watcher_dead')
                if now_ms() >= (config['policy']['expires_at'] - 60) * 1000:
                    return await close_ticket(runtime, ticket, 'expiry')
                return {'state': ticket['state']}
            if ticket['orders'] or ticket.get('uncertain'):
                return {'state': ticket['state'], 'reason': 'reconciliation_hold'}
            if outcome.get('signed'):
                set_state(config, ticket, 'not_filled', reason='entry_unfilled')
                review(config, ticket)
                await disarm(ticket)
                return {'state': 'not_filled'}
            return await refuse(runtime, ticket, outcome.get('reason', 'entry_refused'))
        except (ValueError, KeyError, aiohttp.ClientError, TimeoutError) as error:
            if ticket['state'] in ('sent', 'filled'):
                append_record(config['record_path'], 'recovery_required', key=key, reason=str(error))
                ticket['uncertain'] = True
                return {'refused': 'recovery_required'}
            return await refuse(runtime, ticket, str(error))


async def cancel_locked(runtime, signal_id, publisher=None):
    ticket = runtime['tickets'].get(signal_id)
    if ticket is None or publisher is not None and publisher != ticket['signal']['publisher']:
        return {'refused': 'unknown_signal_id'}
    if ticket['state'] in ('exited', 'refused', 'not_filled'):
        return {'state': ticket['state']}
    return await close_ticket(runtime, ticket, 'cancel')


async def cancel(runtime, signal_id):
    async with runtime['operation_lock']:
        return await cancel_locked(runtime, signal_id)


async def process_event(runtime, ticket, event):
    async with runtime['operation_lock']:
        if event.get('channel') == 'trades':
            event = ticket.pop('pending_trades', event)
        if ticket['state'] in ('exited', 'refused', 'not_filled'):
            return
        if event.get('channel') in ('userFills', 'coordinator_reply', 'watcher_restarted'):
            await reconcile(runtime, ticket)
        signal = ticket['signal']
        if ticket['position']:
            ticket['uncertain'] = False
        reason = ticket.get('closing')
        trigger = None
        if not ticket['alive']:
            reason = 'watcher_dead'
        elif ticket['position'] and now_ms() >= (runtime['config']['policy']['expires_at'] - 60) * 1000:
            reason = 'expiry'
        trades = event.get('data', []) if event.get('channel') == 'trades' else []
        if event.get('channel') == 'watcher_restarted':
            append_record(runtime['config']['record_path'], 'watcher_restarted', key=ticket['key'])
            _, context, _ = await market_context(runtime, signal['asset'])
            trades = [{'coin': signal['asset'], 'px': context['markPx']}]
        if ticket['position']:
            for trade in trades:
                if trade.get('coin') != signal['asset']:
                    continue
                price, stop = Decimal(trade['px']), Decimal(signal['levels']['stop'])
                if trigger is None:
                    trigger = price
                target = signal['levels'].get('target')
                long_side = signal['side'] == 'long'
                if price <= stop if long_side else price >= stop:
                    ticket['stop_hit'], reason, trigger = True, 'stop', stop
                elif target is not None and (price >= Decimal(target) if long_side else price <= Decimal(target)):
                    reason, trigger = 'target', target
        if reason:
            await close_ticket(runtime, ticket, reason, trigger)
        elif ticket['state'] == 'filled' and not ticket['position'] and not ticket['orders']:
            set_state(runtime['config'], ticket, 'exited', reason='venue_flat')
            review(runtime['config'], ticket)
            await disarm(ticket)


async def event_worker(runtime):
    while True:
        ticket, event = await runtime['events'].get()
        try:
            await process_event(runtime, ticket, event)
        except (ValueError, KeyError, aiohttp.ClientError, TimeoutError) as error:
            append_record(runtime['config']['record_path'], 'recovery_required', key=ticket['key'], reason=str(error))
        finally:
            runtime['events'].task_done()


async def recover(runtime):
    await refresh_meta(runtime)
    async with runtime['operation_lock']:
        if not runtime['config'].get('resident', True):
            return await recover_routine(runtime, reconcile)
        for key, row in latest_tickets(runtime['config']).items():
            if row['state'] in ('refused', 'exited', 'not_filled'):
                continue
            ticket = {'key': list(key), 'signal': row['signal'], 'claimed_at': row.get('claimed_at', row['timestamp']), 'state': row['state'],
                      'fill_price': row.get('fill_price'), 'stop_hit': row.get('stop_hit', False),
                      'position': Decimal(0), 'orders': [], 'alive': False, 'done': asyncio.Event()}
            runtime['tickets'][key[2]] = ticket
            try:
                await arm(runtime, ticket)
            except (ValueError, aiohttp.ClientError, TimeoutError):
                ticket['alive'] = False
            await reconcile(runtime, ticket)
            if row['state'] == 'sent' and not ticket['position'] and not ticket['orders']:
                ticket['uncertain'] = True
            await close_ticket(runtime, ticket, 'restart_recovery')


async def shutdown(runtime):
    for ticket in runtime['tickets'].values():
        await disarm(ticket)
    if runtime.get('worker'):
        runtime['worker'].cancel()
        await asyncio.gather(runtime['worker'], return_exceptions=True)


async def control_connection(runtime, reader, writer):
    try:
        command = json.loads(await reader.readline())
        result = await cancel(runtime, command['signal_id'])
        writer.write((json.dumps(result) + '\n').encode())
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main(arguments):
    config = load_config(arguments.config)
    prepare_private_storage(config)
    if arguments.action == 'run' and not config.get('resident', True):
        try:
            envelope = decode_json(Path(arguments.envelope).read_text())
            reason = anchor_refusal(config, envelope)
        except (ValueError, TypeError, OSError, RecursionError):
            reason = 'anchor_not_verified'
        if reason:
            print(json.dumps({'refused': reason}))
            return
    if arguments.action == 'cancel' and Path(config['control_socket']).exists():
        try:
            reader, writer = await asyncio.open_unix_connection(config['control_socket'])
        except ConnectionRefusedError:
            pass
        else:
            writer.write((json.dumps({'signal_id': arguments.signal_id}) + '\n').encode())
            await writer.drain()
            print((await reader.readline()).decode().strip())
            writer.close()
            await writer.wait_closed()
            return
    lock = writer_lock(config)
    Path(config['control_socket']).unlink(missing_ok=True)
    try:
        async with aiohttp.ClientSession() as session:
            runtime = create_runtime(config, session)
            runtime['close_ms'] = getattr(arguments, 'close_ms', None)
            if config.get('resident', True):
                runtime['worker'] = asyncio.create_task(event_worker(runtime))
            try:
                if not config.get('resident', True):
                    with journal_guard(config if arguments.action == 'run' else {'resident': True}) as refusal:
                        await recover(runtime)
                    if refusal:
                        print(json.dumps(refusal))
                        return
                    if arguments.action == 'cancel':
                        result = await cancel(runtime, arguments.signal_id)
                    else:
                        result = await maintain_routine(runtime, reconcile)
                        if arguments.action == 'run' and 'refused' not in result:
                            result = await submit(runtime, envelope)
                    print(json.dumps(result))
                    return
                if arguments.action == 'wake':
                    print(json.dumps({'refused': 'resident_executor'}))
                    return
                if arguments.action == 'cancel':
                    await recover(runtime)
                    print(json.dumps(await cancel(runtime, arguments.signal_id)))
                else:
                    await recover(runtime)
                    print(json.dumps(await submit(runtime, Path(arguments.envelope).read_text())))
                    active = [ticket['done'].wait() for ticket in runtime['tickets'].values()
                              if ticket['state'] in ('sent', 'filled')]
                    if active:
                        server = await asyncio.start_unix_server(
                            lambda reader, writer: control_connection(runtime, reader, writer), path=config['control_socket'])
                        Path(config['control_socket']).chmod(0o600)
                        try:
                            async with server:
                                await asyncio.gather(*active)
                        finally:
                            Path(config['control_socket']).unlink(missing_ok=True)
            finally:
                await shutdown(runtime)
    finally:
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='desk/config.json')
    parser.add_argument('--close-ms', type=int)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('wake')
    command = commands.add_parser('cancel')
    command.add_argument('signal_id')
    command = commands.add_parser('run')
    command.add_argument('envelope')
    asyncio.run(main(parser.parse_args()))
