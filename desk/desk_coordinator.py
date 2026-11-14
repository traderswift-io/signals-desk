from .desk_io import oracle_prices
from .desk_record import append_record, now_ms, set_state, signed_today
from .desk_risk_gate import evaluate_intent
from .signer_delegate_client import run_round as client_round


async def run_round(runtime, ticket, fields):
    config = runtime['config']
    exit_request = fields['kind'] == 'cancel' or fields.get('reduce_only', False)

    def gate(intent):
        return evaluate_intent(intent, config['policy'], runtime['asset_indices'],
                               signed_today(config, intent.get('asset')), now_ms(), ticket['signal'].get('reply_by'))

    async def before_send(body):
        intent = body['intent']
        refusal = gate(intent)
        if refusal:
            return refusal
        if not exit_request and not ticket['alive']:
            return 'watcher_not_alive'
        prices = await oracle_prices(runtime, ticket['signal']['asset'], exit_request)
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
        if event in ('reply', 'prepare_reply') and not ticket.get('closing'):
            runtime['events'].put_nowait((ticket, {'channel': 'coordinator_reply'}))

    outcome = await client_round(runtime, fields, before_send=before_send, validate_rearm=gate, on_event=on_event)
    if 'reconciliation' in outcome:
        ticket['reconciliation'] = outcome.pop('reconciliation')
    return outcome
