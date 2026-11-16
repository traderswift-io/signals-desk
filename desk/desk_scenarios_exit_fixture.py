import asyncio
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from .desk_exit_algo import exit_config
from .desk_record import append_record, now_ms, set_state


def book(mid='110', own='100', other='120'):
    mid = Decimal(mid)
    return {'coin': 'BTC', 'levels': [[{'px': str(mid - Decimal('0.1')), 'sz': other}],
                                     [{'px': str(mid + Decimal('0.1')), 'sz': own}]]}


def position(size='10', short=False):
    return {'size': str(-Decimal(size) if short else Decimal(size)), 'entry': '100',
            'stop': '105' if short else '95', 'target': '90' if short else '110',
            'backstop_px': '106' if short else '94', 'szDecimals': 3}


def resting(action, oid=1):
    return {'coin': 'BTC', 'oid': oid, 'sz': action['size'], 'limitPx': action['limit_px'],
            'reduceOnly': True, 'isTrigger': False}


def stop_order(size='10', oid=90):
    return {'coin': 'BTC', 'oid': oid, 'sz': size, 'reduceOnly': True, 'isTrigger': True, 'orderType': 'Stop Market'}


@contextmanager
def driver_fixture(entered=True):
    with TemporaryDirectory() as directory:
        config = exit_config({'record_path': str(Path(directory) / 'records.jsonl'), 'vault_address': '<fake>',
                              'account_address': '<fake>', 'exit_band': '0.005', 'policy': {'expires_at': 9999999999}})
        ticket = {'key': ['<fake>', 'publisher', 'sample'], 'signal': {'asset': 'BTC', 'asset_index': 0,
                  'signal_id': 'sample', 'publisher': 'publisher', 'side': 'long', 'levels': {'entry': '100', 'stop': '95', 'target': '110'}, 'reply_by': 999999999999},
                  'claimed_at': 0, 'state': 'claimed', 'position': Decimal('10') if entered else Decimal(0),
                  'orders': [stop_order()], 'backstop_px': '94', 'book': book(), 'closing': 'target',
                  'entry_done': False, 'alive': True, 'done': asyncio.Event()}
        runtime = {'config': config, 'operation_lock': asyncio.Lock(),
                   'meta': {'universe': [{'name': 'BTC', 'szDecimals': 3}]}, 'tickets': {'sample': ticket}}
        path, key = config['record_path'], ticket['key']
        entry = {'kind': 'order', 'asset': 'BTC', 'asset_index': 0, 'is_buy': True, 'size': '10',
                 'limit_px': '100', 'reduce_only': False, 'tif': 'Gtc', 'delegate_key': 'fixture',
                 'key_ref': 'fixture', 'request_id': 'entry'}
        set_state(config, ticket, 'claimed')
        set_state(config, ticket, 'sent')
        append_record(path, 'intent', key=key, intent=entry)
        append_record(path, 'reply', key=key, request_id='entry', signed=True,
                      venue_answer={'response': {'type': 'order', 'data': {'statuses': [{'resting': {'oid': 42}}]}}})
        if entered:
            append_record(path, 'reconcile', key=key, orders=ticket['orders'], fills=[], position=ticket['position'])
            ticket['entry_done'] = True
            set_state(config, ticket, 'filled')
        sent = []

        async def send(runtime, ticket, fields):
            sent.append(fields)
            intent = {**fields, 'delegate_key': 'fixture', 'key_ref': 'fixture', 'request_id': str(len(sent))}
            append_record(path, 'intent', key=key, intent=intent)
            refused = fields['kind'] == 'trigger_order' and ticket.get('refuse_trigger')
            if refused:
                outcome = {'signed': False, 'reason': 'unsupported_kind', 'intent': intent}
            else:
                if fields['kind'] == 'cancel':
                    ticket['orders'] = [order for order in ticket['orders'] if order['oid'] != fields['oid']]
                elif fields['kind'] == 'trigger_order':
                    ticket['orders'].append(stop_order(fields['size'], 100 + len(sent)))
                elif fields['tif'] == 'Alo':
                    ticket['orders'].append(resting(fields, 100 + len(sent)))
                else:
                    ticket['position'] -= Decimal(fields['size'])
                outcome = {'signed': True, 'intent': intent,
                           'venue_answer': {'response': {'type': 'order', 'data': {'statuses': ['success']}}}}
            append_record(path, 'reply', key=key, request_id=intent['request_id'], **outcome)
            append_record(path, 'reconcile', key=key, orders=ticket['orders'], fills=[], position=ticket['position'])
            return outcome

        with (patch('desk.desk_exit_driver.run_round', send), patch('desk.desk_routine.run_round', send),
              patch('desk.desk_exit_driver.read_info', AsyncMock(return_value={'nRequestsCap': 1000, 'nRequestsUsed': 0})) as info,
              patch('desk.desk_exit_driver.now_ms', return_value=now_ms()) as clock,
              patch('time.time_ns', side_effect=lambda: clock.return_value * 1000000)):
            yield runtime, ticket, sent, info, clock
