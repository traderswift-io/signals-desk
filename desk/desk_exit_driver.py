from decimal import Decimal

from .desk_exit_algo import backstop_actions, book_context, exit_actions, target_given_back
from .desk_exit_pacing import MAX_EXIT_ACTIONS, MIN_EXIT_ACTIONS, pacing_cost
from .desk_coordinator import run_round
from .desk_journal_evidence import entry_completion_agrees
from .desk_record import append_record, now_ms, read_records, set_state
from .desk_routine import cleanup_protection
from .desk_watcher import disarm
from .signer_delegate_client import cancel_fields, market_context, order_fields, order_statuses, price_text, read_info, resolve_symbol


def position_context(runtime, ticket):
    signal = ticket['signal']
    return {**signal['levels'], 'size': str(ticket['position']), 'backstop_px': ticket['backstop_px'],
            'szDecimals': resolve_symbol(signal['asset'], runtime['meta'])['szDecimals']}


async def action_budget(runtime, ticket, cost):
    pacing_cost(cost)
    config = runtime['config']
    timestamp = now_ms()
    slow = int(config.get('exit_min_action_ms', 10000))
    fast = int(config.get('exit_fast_action_ms', 2000))
    last = ticket.get('last_exit_action_ms')
    recorded_cost = ticket.get('exit_action_cost')
    recorded_cost = MIN_EXIT_ACTIONS if recorded_cost is None else recorded_cost
    pacing_supported = MIN_EXIT_ACTIONS <= recorded_cost <= MAX_EXIT_ACTIONS and (last is None or last <= timestamp)
    if last is not None and last > timestamp:
        last = ticket.setdefault('exit_wait_started_ms', timestamp)
    interval = fast if ticket.get('exit_fast') else slow
    elapsed = timestamp - last if last is not None else None
    multiplier = min(MAX_EXIT_ACTIONS, max(MIN_EXIT_ACTIONS, recorded_cost))
    if elapsed is not None and elapsed < min(slow, interval * multiplier):
        return False
    try:
        budget = await read_info(runtime, 'userRateLimit', user=config['vault_address'])
        remaining = int(budget['nRequestsCap']) - int(budget['nRequestsUsed'])
    except (KeyError, ValueError, TypeError):
        append_record(config['record_path'], 'action_budget_unavailable', key=ticket['key'])
        return False
    low = remaining <= int(config.get('exit_action_reserve', 200))
    if low and not ticket.get('action_budget_low'):
        append_record(config['record_path'], 'action_budget_low', key=ticket['key'])
    ticket['action_budget_low'], ticket['exit_fast'] = low, not low
    if pacing_supported:
        set_state(config, ticket, ticket['state'])
    if elapsed is not None and elapsed < min(slow, (slow if low else fast) * multiplier):
        return False
    allowed = min(cost, max(MIN_EXIT_ACTIONS, remaining))
    append_record(config['record_path'], 'exit_reservation', key=ticket['key'], cost=allowed, timestamp=timestamp)
    ticket['last_exit_action_ms'], ticket['exit_action_cost'] = timestamp, allowed
    ticket.pop('exit_wait_started_ms', None)
    set_state(config, ticket, ticket['state'])
    return allowed


async def send_action(runtime, ticket, fields, reconcile):
    outcome = await run_round(runtime, ticket, fields)
    reason = ticket.get('closing') or 'backstop_maintenance'
    if fields['kind'] == 'cancel':
        append_record(runtime['config']['record_path'], 'cancel_result', key=ticket['key'], reason=reason, outcome=outcome)
    elif fields['kind'] == 'trigger_order':
        append_record(runtime['config']['record_path'], 'backstop_result', key=ticket['key'], reason=reason, outcome=outcome)
    else:
        append_record(runtime['config']['record_path'], 'exit_result', key=ticket['key'], reason=reason, outcome=outcome)
    await reconcile(runtime, ticket)
    statuses = order_statuses(outcome)
    return outcome, bool(outcome.get('signed') and not outcome.get('ambiguous')
                         and not any(isinstance(row, dict) and 'error' in row for row in statuses))


def replacement_refused(runtime, ticket):
    return any(row.get('key') == ticket['key'] and row['event'] == 'backstop_replace_refused'
               for row in read_records(runtime['config']['record_path']))


async def replace_backstop(runtime, ticket, action, reconcile, cancel_old=True):
    signal = ticket['signal']
    buy = ticket['position'] < 0
    trigger = Decimal(action['trigger_px'])
    decimals = int(resolve_symbol(signal['asset'], runtime['meta'])['szDecimals'])
    band = Decimal(str(runtime['config'].get('bracket_slippage_bps', '50'))) / 10000
    limit = price_text(trigger * (1 + band if buy else 1 - band), decimals, buy)
    if abs(Decimal(limit) - trigger) > trigger / 2:
        raise ValueError('trigger_price_outside_band')
    fields = {**order_fields(signal, action['size'], limit, buy, True), 'kind': 'trigger_order',
              'trigger_px': str(trigger), 'is_market': True, 'tpsl': 'sl'}
    fields.pop('tif')
    before = {order['oid'] for order in ticket['orders']}
    previously_refused = replacement_refused(runtime, ticket)
    outcome, accepted = await send_action(runtime, ticket, fields, reconcile)
    new_stops = [order for order in ticket['orders'] if order['oid'] not in before and order.get('isTrigger')
                 and order.get('reduceOnly') and Decimal(order['sz']) == abs(ticket['position'])]
    if not accepted or not new_stops:
        if not previously_refused:
            append_record(runtime['config']['record_path'], 'backstop_replace_refused', key=ticket['key'], outcome=outcome)
        return False
    if cancel_old:
        await send_action(runtime, ticket, cancel_fields(signal, action['oid']), reconcile)
    return True


async def close_legacy_position(runtime, ticket, reconcile):
    if not ticket.get('closing') or not await action_budget(runtime, ticket, MIN_EXIT_ACTIONS):
        return {'state': ticket['state']}
    _, context, _ = await market_context(runtime, ticket['signal']['asset'])
    buy = ticket['position'] < 0
    decimals = int(resolve_symbol(ticket['signal']['asset'], runtime['meta'])['szDecimals'])
    band = Decimal(runtime['config']['exit_band'])
    price = price_text(Decimal(context['markPx']) * (1 + band if buy else 1 - band), decimals, buy)
    await send_action(runtime, ticket, order_fields(ticket['signal'], abs(ticket['position']), price, buy, True), reconcile)
    if not ticket['position'] and not ticket['orders']:
        await cleanup_protection(runtime, ticket, ticket['closing'], reconcile)
        if ticket['state'] == 'exited':
            await disarm(ticket)
    return {'state': ticket['state']}


async def drive_exit(runtime, ticket, reconcile):
    config, signal = runtime['config'], ticket['signal']
    entries = [order for order in ticket['orders'] if not order.get('reduceOnly') and not order.get('isTrigger')]
    if not entries and not ticket.get('entry_done') and entry_completion_agrees(read_records(config['record_path']), ticket['key']):
        ticket['entry_done'] = True
        set_state(config, ticket, ticket['state'])
    if not ticket['position'] and ticket.get('entry_done'):
        if not ticket['orders'] or await action_budget(runtime, ticket, 1):
            result = await cleanup_protection(runtime, ticket, 'entry_done_flat', reconcile, limit=1)
            if ticket['state'] == 'exited':
                await disarm(ticket)
            return result
        return {'state': ticket['state']}
    if ticket.get('closing') and entries:
        if await action_budget(runtime, ticket, 1):
            await send_action(runtime, ticket, cancel_fields(signal, entries[0]['oid']), reconcile)
            if (not any(not order.get('reduceOnly') and not order.get('isTrigger') for order in ticket['orders'])
                    and entry_completion_agrees(read_records(config['record_path']), ticket['key'])):
                ticket['entry_done'] = True
                set_state(config, ticket, ticket['state'])
        return {'state': ticket['state']}
    if entries or not ticket['position']:
        return {'state': ticket['state']}
    if not ticket.get('backstop_px'):
        return await close_legacy_position(runtime, ticket, reconcile)
    position = position_context(runtime, ticket)
    protection = backstop_actions(position, ticket['orders'])
    actions = exit_actions(ticket.get('book'), position, ticket['orders'], ticket['closing'], config,
                           maintain_backstop=False) if ticket.get('closing') else []
    if actions and replacement_refused(runtime, ticket):
        protection = []
    remaining_budget = None
    if protection:
        action = protection[0]
        allowed = await action_budget(runtime, ticket, MAX_EXIT_ACTIONS if action['action'] == 'replace_backstop' else MIN_EXIT_ACTIONS)
        if allowed:
            if action['action'] == 'replace_backstop':
                if not await replace_backstop(runtime, ticket, action, reconcile, cancel_old=allowed > 1):
                    remaining_budget = allowed - 1
            else:
                await send_action(runtime, ticket, cancel_fields(signal, action['oid']), reconcile)
        if not remaining_budget:
            return {'state': ticket['state']}
    context = book_context(ticket.get('book'), position, config)
    if context is None or not ticket.get('closing'):
        return {'state': ticket['state']}
    if context['low_liquidity'] and not ticket.get('low_liquidity'):
        append_record(config['record_path'], 'low_liquidity', key=ticket['key'])
    ticket['low_liquidity'] = context['low_liquidity']
    actions = exit_actions(ticket['book'], position, ticket['orders'], ticket['closing'], config, maintain_backstop=False)
    giving_back = ticket['closing'] == 'target' and target_given_back(context['mid'], position, config)
    if actions:
        actions = actions[:MAX_EXIT_ACTIONS]
        allowed = remaining_budget if remaining_budget is not None else await action_budget(runtime, ticket, len(actions))
        if allowed:
            for action in actions[:allowed]:
                if action['action'] == 'cancel':
                    outcome, accepted = await send_action(runtime, ticket, cancel_fields(signal, action['oid']), reconcile)
                    if not accepted or any(order['oid'] == action['oid'] for order in ticket['orders']):
                        break
                else:
                    if not ticket['position']:
                        break
                    fields = {**order_fields(signal, min(Decimal(action['size']), abs(ticket['position'])),
                                             action['limit_px'], action['is_buy'], True), 'tif': action['tif']}
                    outcome, accepted = await send_action(runtime, ticket, fields, reconcile)
                    if not accepted:
                        break
    if giving_back and not any(order.get('reduceOnly') and not order.get('isTrigger') for order in ticket['orders']):
        ticket['closing'] = None
        set_state(config, ticket, ticket['state'], reason='target_giveback')
    return {'state': ticket['state']}
