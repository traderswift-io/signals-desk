import asyncio
from decimal import Decimal

from .desk_bracket import closed_price, invalidated, wake_close_refusal
from .desk_coordinator import run_round
from .desk_journal_evidence import entry_completion_agrees
from .desk_record import append_record, bracket_result_agrees, latest_tickets, now_ms, read_records, review, set_state
from .desk_wake import active_asset
from .signer_delegate_client import cancel_fields, market_context, order_fields, order_statuses, price_text, resolve_symbol
from .desk_watcher import disarm


async def bracket_result(runtime, ticket, outcome, reconcile):
    config = runtime['config']
    if outcome.get('signed') is False:
        reason = outcome.get('reason', 'bracket_refused')
        append_record(config['record_path'], 'bracket_refused', key=ticket['key'], reason=reason)
        set_state(config, ticket, 'refused', reason=reason)
        review(config, ticket)
        return {'state': 'bracket_refused', 'reason': reason}
    ticket['uncertain'] = bool(outcome.get('ambiguous'))
    statuses = order_statuses(outcome)
    append_record(config['record_path'], 'bracket_result', key=ticket['key'],
                  request_id=outcome['intent']['request_id'], outcome=outcome)
    await reconcile(runtime, ticket)
    if ticket['uncertain'] or any(isinstance(status, dict) and 'error' in status for status in statuses):
        ticket['uncertain'] = True
        append_record(config['record_path'], 'recovery_required', key=ticket['key'], reason='bracket_outcome_unresolved')
        return {'state': ticket['state'], 'reason': 'bracket_outcome_unresolved'}
    if not ticket['position'] and not ticket['orders']:
        set_state(config, ticket, 'not_filled', reason='bracket_not_resting')
        review(config, ticket)
    return {'state': ticket['state']}


async def recover_routine(runtime, reconcile):
    config = runtime['config']
    for key, row in latest_tickets(config).items():
        if row['state'] in ('refused', 'exited', 'not_filled'):
            continue
        ticket = {'key': list(key), 'signal': row['signal'], 'claimed_at': row.get('claimed_at', row['timestamp']),
                  'state': row['state'], 'fill_price': row.get('fill_price'), 'stop_hit': row.get('stop_hit', False),
                  'entry_done': row.get('entry_done', False), 'close_ms': row.get('close_ms'),
                  'entry_cancel_reason': row.get('entry_cancel_reason'), 'closing': row.get('closing'),
                  'backstop_px': row.get('backstop_px'),
                  'position': Decimal(0), 'orders': [], 'alive': False, 'done': asyncio.Event()}
        runtime['tickets'][key[2]] = ticket
        await reconcile(runtime, ticket)
        if not ticket['position'] and not ticket['orders']:
            records = read_records(config['record_path'])
            outcomes = [record for record in records if record['event'] == 'bracket_result' and record['key'] == list(key)]
            confirmed = (outcomes and bracket_result_agrees(records, outcomes[-1])
                         and outcomes[-1]['outcome'].get('signed') is True
                         and not outcomes[-1]['outcome'].get('ambiguous'))
            if entry_completion_agrees(records, ticket['key']) or confirmed:
                set_state(config, ticket, 'exited', reason='venue_flat')
                review(config, ticket)
            else:
                ticket['uncertain'] = True


async def cancel_resting_order(runtime, ticket, order, reason, reconcile):
    outcome = await run_round(runtime, ticket, cancel_fields(ticket['signal'], order['oid']))
    append_record(runtime['config']['record_path'], 'cancel_result', key=ticket['key'], reason=reason,
                  close_ms=runtime.get('close_ms'), oid=order['oid'], outcome=outcome)
    await reconcile(runtime, ticket)
    if any(current['oid'] == order['oid'] for current in ticket['orders']):
        return outcome.get('reason', 'cancel_unconfirmed')
    return None


async def cleanup_protection(runtime, ticket, reason, reconcile, limit=None):
    refusal = None
    for order in list(ticket['orders'])[:limit]:
        outcome = await cancel_resting_order(runtime, ticket, order, reason, reconcile)
        refusal = refusal or outcome
    if not ticket['position'] and not ticket['orders'] and not ticket.get('uncertain'):
        set_state(runtime['config'], ticket, 'exited', reason=reason)
        review(runtime['config'], ticket)
    result = {'signal_id': ticket['signal']['signal_id'], 'state': ticket['state'], 'reason': reason}
    return {**result, **({'refused': refusal} if refusal else {})}


async def maintain_routine(runtime, reconcile):
    config = runtime['config']
    close = runtime.get('close_ms')
    refusal = wake_close_refusal(close)
    if refusal:
        return {'refused': refusal}
    results = []
    async with runtime['operation_lock']:
        for ticket in runtime['tickets'].values():
            signal = ticket['signal']
            if ticket['state'] in ('refused', 'exited', 'not_filled'):
                continue
            await reconcile(runtime, ticket)
            entries = [order for order in ticket['orders'] if not order.get('reduceOnly', False)
                       and not order.get('isTrigger', False)]
            if not entries and ticket['position'] and not ticket.get('entry_done'):
                ticket['entry_done'] = True
                set_state(config, ticket, ticket['state'], reason='entry_filled')
            if signal.get('exit_by') is not None and signal['exit_by'] <= now_ms():
                results.append(await time_stop_routine(runtime, ticket, reconcile))
                continue
            if ticket.get('entry_done') and not ticket['position']:
                results.append(await cleanup_protection(runtime, ticket, 'entry_done_flat', reconcile))
                continue
            if not ticket.get('entry_cancel_reason') and not active_asset(config, signal['asset'], close):
                continue
            if ticket.get('entry_cancel_reason'):
                reason = ticket['entry_cancel_reason']
            elif signal['reply_by'] <= now_ms():
                reason = 'reply_by_expired'
            else:
                price = await closed_price(runtime, signal, close)
                refusal = wake_close_refusal(close)
                if refusal:
                    return {'refused': refusal}
                if not invalidated(signal, price):
                    continue
                reason = 'signal_invalidated'
            if not ticket.get('entry_cancel_reason'):
                ticket['entry_cancel_reason'] = reason
                set_state(config, ticket, ticket['state'], reason=reason)
            for order in entries:
                refusal = await cancel_resting_order(runtime, ticket, order, reason, reconcile)
                if refusal:
                    results.append({'signal_id': signal['signal_id'], 'refused': refusal})
                    break
            else:
                if not ticket.get('entry_done') and entry_completion_agrees(read_records(config['record_path']), ticket['key']):
                    ticket['entry_done'] = True
                    set_state(config, ticket, ticket['state'], reason=reason)
                if ticket.get('entry_done') and not ticket['position']:
                    results.append(await cleanup_protection(runtime, ticket, reason, reconcile))
                    continue
                if not ticket['position'] and not ticket['orders'] and not ticket.get('uncertain'):
                    set_state(config, ticket, 'exited', reason=reason)
                    review(config, ticket)
                results.append({'signal_id': signal['signal_id'], 'state': ticket['state'], 'reason': reason})
    return {'results': results}


async def close_routine(runtime, ticket, reason, reconcile, trigger=None):
    config, signal = runtime['config'], ticket['signal']
    reason = 'time_stop' if ticket.get('closing') == 'time_stop' else reason
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
        fields = {**order_fields(signal, abs(ticket['position']), price, buy, True), 'is_buy': buy}
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


async def time_stop_routine(runtime, ticket, reconcile):
    config, signal = runtime['config'], ticket['signal']
    if ticket.get('closing') != 'time_stop':
        append_record(config['record_path'], 'time_stop', key=ticket['key'])
        ticket['closing'] = 'time_stop'
        set_state(config, ticket, ticket['state'])
    for order in list(ticket['orders']):
        if not order.get('reduceOnly') and not order.get('isTrigger'):
            refusal = await cancel_resting_order(runtime, ticket, order, 'time_stop', reconcile)
            if refusal:
                return {'refused': refusal}
    if ticket['position']:
        _, context, _ = await market_context(runtime, signal['asset'])
        refusal = wake_close_refusal(runtime.get('close_ms'))
        if refusal:
            return {'refused': refusal}
        buy = ticket['position'] < 0
        band = Decimal(str(config.get('bracket_slippage_bps', '50'))) / 10000
        if not band.is_finite() or not 0 <= band <= Decimal('0.5'):
            return {'refused': 'trigger_price_outside_band'}
        decimals = int(resolve_symbol(signal['asset'], runtime['meta'])['szDecimals'])
        price = price_text(Decimal(context['markPx']) * (1 + band if buy else 1 - band), decimals, buy)
        outcome = await run_round(runtime, ticket, order_fields(signal, abs(ticket['position']), price, buy, True))
        append_record(config['record_path'], 'exit_result', key=ticket['key'], reason='time_stop', outcome=outcome)
        await reconcile(runtime, ticket)
        if ticket['position']:
            return {'state': ticket['state'], 'reason': 'time_stop'}
    if entry_completion_agrees(read_records(config['record_path']), ticket['key']):
        ticket['entry_done'] = True
        set_state(config, ticket, ticket['state'])
    return await cleanup_protection(runtime, ticket, 'time_stop', reconcile)
