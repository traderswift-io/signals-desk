import argparse
import asyncio
import json
from pathlib import Path

import aiohttp

from .desk_anchor_verify import anchor_refusal, decode_json
from .desk_bracket import entry_candle_guard, wake_close_refusal
from .desk_io import entry_preview, load_config
from .desk_record import journal_guard, latest_tickets, now_ms, read_records, signed_today
from .desk_risk_gate import evaluate_intent
from .desk_signal import parse_signal
from .signer_delegate_client import refresh_meta


async def ticket_result(arguments):
    config = load_config(arguments.config, require_public_key=arguments.action == 'gate')
    if arguments.action == 'review':
        tickets = [row for key, row in latest_tickets(config).items() if key[2] == arguments.input]
        if not tickets:
            return {'refused': 'unknown_signal_id'}
        reviews = [row for row in read_records(config['record_path'])
                   if row['event'] == 'review' and row['key'] == tickets[-1]['key']]
        return reviews[-1] if reviews else {'refused': 'review_unavailable'}
    envelope = Path(arguments.input).read_text()
    with journal_guard(config) as refusal:
        read_records(config['record_path'])
    if refusal:
        return refusal
    if not config.get('resident', True):
        try:
            envelope = decode_json(envelope)
            reason = anchor_refusal(config, envelope)
        except (ValueError, TypeError, RecursionError):
            reason = 'anchor_not_verified'
        if reason:
            return {'refused': reason}
    async with aiohttp.ClientSession() as session:
        runtime = {'config': config, 'session': session, 'asset_indices': {},
                   'close_ms': getattr(arguments, 'close_ms', None)}
        if arguments.action == 'gate' and not config.get('resident', True):
            refusal = wake_close_refusal(runtime['close_ms'])
            if refusal:
                return {'refused': refusal}
        signal = parse_signal(envelope, await refresh_meta(runtime), now_ms())
        if signal.get('kind') == 'cancel':
            return {'refused': 'not_an_entry_signal'}
        sized, fields, intent, account = await entry_preview(runtime, signal)
        if arguments.action == 'size':
            return sized
        with journal_guard(config) as refusal:
            budget = signed_today(config, signal['asset'])
            reason = evaluate_intent(intent, config['policy'], runtime['asset_indices'], budget,
                                     now_ms(), signal['reply_by'], signal.get('exit_by'))
        if refusal:
            return refusal
        if not reason and not config.get('resident', True):
            reason = await entry_candle_guard(runtime, signal)
        return {**({'refused': reason} if reason else {'status': 'PASS'}), 'sizing': sized, 'intent': intent}


async def main(arguments):
    try:
        result = await ticket_result(arguments)
    except (ValueError, KeyError, TypeError, OSError, aiohttp.ClientError, TimeoutError) as error:
        result = {'refused': str(error)}
    print(json.dumps(result, default=str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='desk/config.json')
    parser.add_argument('--close-ms', type=int)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('size', 'gate', 'review'):
        command = commands.add_parser(action)
        command.add_argument('input')
    asyncio.run(main(parser.parse_args()))
