from .desk_bracket import wake_close_refusal
from .desk_io import oracle_prices
from .desk_record import append_record, journal_guard, now_ms, set_state, signed_today
from .desk_risk_gate import evaluate_intent
from .signer_delegate_client import run_round as client_round


async def run_round(runtime, ticket, fields):
    config = runtime['config']
    exit_request = fields['kind'] == 'cancel' or fields.get('reduce_only', False)

    def gate(intent):
        with journal_guard(config) as refusal:
            reason = evaluate_intent(intent, config['policy'], runtime['asset_indices'],
                                     signed_today(config, intent.get('asset')), now_ms(), ticket['signal'].get('reply_by'),
                                     ticket['signal'].get('exit_by'))
        return refusal.get('refused') if refusal else reason

    async def before_send(body):
        intent = body['intent']
        if fields['kind'] == 'bracket' and not config.get('resident', True) and wake_close_refusal(ticket.get('close_ms')):
            return 'stale_wake_close'
        refusal = gate(intent)
        if refusal:
            return refusal
        if not exit_request and config.get('resident', True) and not ticket['alive']:
            return 'watcher_not_alive'
        prices = await oracle_prices(runtime, ticket['signal']['asset'], exit_request)
        if fields['kind'] == 'bracket' and not config.get('resident', True) and wake_close_refusal(ticket.get('close_ms')):
            return 'stale_wake_close'
        if not exit_request and any(prices.get(source + '_error') not in (None, 'unmapped') for source in ('hl_oracle', 'binance')):
            append_record(config['record_path'], 'oracle_refusal', key=ticket['key'], **prices)
            return 'oracle_unavailable'
        refusal = gate(intent)
        if refusal:
            return refusal
        if not exit_request and ticket['state'] == 'claimed':
            set_state(config, ticket, 'sent')
        append_record(config['record_path'], 'intent', key=ticket['key'], intent=intent, rearm_intent=body['rearm_intent'], **prices)
        return None

    def on_event(event, **values):
        append_record(config['record_path'], event, key=ticket['key'], **values)
        if config.get('resident', True) and event in ('reply', 'prepare_reply') and not ticket.get('closing'):
            runtime['events'].put_nowait((ticket, {'channel': 'coordinator_reply'}))

    with journal_guard(config) as refusal:
        signed_today(config, ticket['signal']['asset'])
    if refusal:
        return {'signed': False, 'reason': refusal['refused']}
    if not exit_request and ticket['signal'].get('exit_by') is not None and now_ms() >= ticket['signal']['exit_by']:
        return {'signed': False, 'reason': 'exit_by_passed'}
    outcome = await client_round(runtime, fields, before_send=before_send, validate_rearm=gate, on_event=on_event)
    if 'reconciliation' in outcome:
        ticket['reconciliation'] = outcome.pop('reconciliation')
    return outcome
