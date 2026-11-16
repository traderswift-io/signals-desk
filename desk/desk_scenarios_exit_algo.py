import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from .desk_exit_algo import backstop_actions, book_context, exit_actions, exit_config
from .desk_exit_driver import drive_exit
from .desk_executor import process_event, submit
from .desk_record import latest_tickets, read_records
from .desk_scenarios_bracket import envelope, fixture
from .desk_scenarios_checks import check_timer_audit
from .desk_scenarios_exit_fixture import book, driver_fixture, position, resting, stop_order


async def check_target_slices():
    config, current, orders = exit_config({'exit_band': '0.005'}), position(), []
    while Decimal(current['size']):
        actions = exit_actions(book(), current, orders, 'target', config)
        assert 0 < len(actions) <= 2
        for action in actions:
            assert action['action'] == 'place' and action['tif'] == 'Alo' and action['limit_px'] == '110.1'
            assert Decimal(action['size']) <= Decimal('3')
            current['size'] = str(Decimal(current['size']) - Decimal(action['size']))
    short = exit_actions(book('90', '120', '100'), position(short=True), [], 'target', config)
    assert short[0]['is_buy'] and short[0]['limit_px'] == '89.9'


async def check_touch_stable():
    config = exit_config({'exit_band': '0.005'})
    actions = exit_actions(book(), position(), [], 'target', config)
    assert exit_actions(book(), position(), [resting(actions[0])], 'target', config) == []


async def check_requote():
    config = exit_config({'exit_band': '0.005'})
    action = exit_actions(book(), position(), [], 'target', config)[0]
    actions = exit_actions(book('111'), position(), [resting(action)], 'target', config)
    assert [item['action'] for item in actions] == ['cancel', 'place']
    assert actions[1]['limit_px'] == '111.1'


async def check_escalation():
    config = exit_config({'exit_band': '0.005'})
    first = exit_actions(book('95'), position(), [], 'stop', config)
    assert first[0]['tif'] == 'Alo'
    aggressive = exit_actions(book('94.7'), position(), [], 'stop', config)
    assert aggressive[0]['tif'] == 'Ioc' and aggressive[0]['limit_px'] == '94.6'
    assert exit_actions(book('94.7'), position(), [resting(first[0])], 'stop', config) == [{'action': 'cancel', 'oid': 1}]
    for reason in ('expiry', 'withdrawn', 'time_stop'):
        assert exit_actions(book('94.7'), position(), [], reason, config)[0]['tif'] == 'Ioc'
    assert exit_actions(book('105.4'), position(short=True), [], 'stop', config)[0]['is_buy']
    assert not exit_actions(book('94.7'), position(), [], 'stop', {**config, 'exit_band': '0.00001'})


async def check_backstop_band():
    config = exit_config({'exit_band': '0.005'})
    assert exit_actions(book('94'), position(), [], 'stop', config) == []
    assert exit_actions(book('106'), position(short=True), [], 'stop', config) == []


async def check_thin_book():
    config = exit_config({'exit_band': '0.005'})
    assert book_context(book(own='50'), position(), config)['low_liquidity']
    actions = exit_actions(book(own='50'), position(), [], 'target', config)
    assert len(actions) == 1 and Decimal(actions[0]['size']) == Decimal('0.750')
    assert not exit_actions(book(own='1'), position(), [], 'target', config)
    with driver_fixture() as (runtime, ticket, sent, info, clock):
        ticket['book'] = book(own='50')
        await drive_exit(runtime, ticket, AsyncMock())
        assert any(row['event'] == 'low_liquidity' for row in read_records(runtime['config']['record_path']))


async def check_ratio_guard():
    config = exit_config({'exit_band': '0.005'})
    assert not exit_actions(book(other='102'), position(), [], 'target', config)
    assert not exit_actions(book('90', '102', '100'), position(short=True), [], 'target', config)


async def check_resize():
    with driver_fixture() as (runtime, ticket, sent, info, clock):
        ticket.update(position=Decimal('4'), closing=None, entry_done=False)
        ticket['orders'].append({'coin': 'BTC', 'oid': 42, 'sz': '6', 'reduceOnly': False, 'isTrigger': False})
        await drive_exit(runtime, ticket, AsyncMock())
        assert not sent and ticket['orders'][0]['sz'] == '10'

    for refused in (False, True):
        with driver_fixture() as (runtime, ticket, sent, info, clock):
            ticket.update(position=Decimal('4'), refuse_trigger=refused)
            await drive_exit(runtime, ticket, AsyncMock())
            assert sent[0]['kind'] == 'trigger_order' and sent[0]['size'] == '4'
            assert sent[0]['reduce_only'] and sent[0]['tpsl'] == 'sl'
            assert abs(Decimal(sent[0]['limit_px']) - Decimal(sent[0]['trigger_px'])) <= Decimal(sent[0]['trigger_px']) / 2
            if refused:
                assert len(sent) == 2 and ticket['orders'][0] == stop_order()
                assert sent[1]['kind'] == 'order' and sent[1]['reduce_only'] and sent[1]['tif'] == 'Alo'
                assert len(ticket['orders']) == 2 and ticket['orders'][1]['sz'] == sent[1]['size']
                assert any(row['event'] == 'backstop_replace_refused' for row in read_records(runtime['config']['record_path']))
            else:
                assert sent[1]['kind'] == 'cancel' and sent[1]['oid'] == 90
                assert len(ticket['orders']) == 1 and ticket['orders'][0]['sz'] == '4'
            assert list(latest_tickets(runtime['config']).values())[0]['entry_done']


async def check_flat():
    with driver_fixture(entered=False) as (runtime, ticket, sent, info, clock):
        ticket['closing'] = 'withdrawn'
        ticket['orders'].insert(0, {'coin': 'BTC', 'oid': 42, 'sz': '10', 'reduceOnly': False, 'isTrigger': False})
        await drive_exit(runtime, ticket, AsyncMock())
        assert ticket['entry_done'] and sent[0]['oid'] == 42
        assert len(ticket['orders']) == 1 and ticket['orders'][0]['isTrigger']
        clock.return_value += 10000
        await drive_exit(runtime, ticket, AsyncMock())
        assert ticket['state'] == 'exited'

    with driver_fixture() as (runtime, ticket, sent, info, clock):
        ticket['position'] = Decimal(0)
        await drive_exit(runtime, ticket, AsyncMock())
        assert sent == [{'kind': 'cancel', 'asset_index': 0, 'asset': 'BTC', 'oid': 90}]
        assert ticket['state'] == 'exited' and ticket['done'].is_set()


async def check_budget():
    with driver_fixture() as (runtime, ticket, sent, info, clock):
        info.return_value = {}
        await drive_exit(runtime, ticket, AsyncMock())
        assert not sent
        assert any(row['event'] == 'action_budget_unavailable' for row in read_records(runtime['config']['record_path']))

    with driver_fixture() as (runtime, ticket, sent, info, clock):
        info.return_value = {'nRequestsCap': 1000, 'nRequestsUsed': 1000}
        ticket['position'] = Decimal('4')
        await drive_exit(runtime, ticket, AsyncMock())
        assert len(sent) == 1 and sent[0]['kind'] == 'trigger_order' and len(ticket['orders']) == 2
        clock.return_value += 3000
        await drive_exit(runtime, ticket, AsyncMock())
        assert len(sent) == 1
        clock.return_value += 8000
        await drive_exit(runtime, ticket, AsyncMock())
        assert len(sent) == 2 and sent[-1]['kind'] == 'cancel' and sent[-1]['oid'] == 90

    with driver_fixture() as (runtime, ticket, sent, info, clock):
        info.return_value = {'nRequestsCap': 1000, 'nRequestsUsed': 800}
        await drive_exit(runtime, ticket, AsyncMock())
        ticket['book'] = book('111')
        clock.return_value += 3000
        await drive_exit(runtime, ticket, AsyncMock())
        assert len(sent) == 1 and info.await_count == 1
        clock.return_value += 8000
        await drive_exit(runtime, ticket, AsyncMock())
        assert [action['kind'] for action in sent] == ['order', 'cancel', 'order']
        assert info.await_count == 2 and info.call_args.kwargs['user'] == '<fake>'
        assert sum(row['event'] == 'action_budget_low' for row in read_records(runtime['config']['record_path'])) == 1
        assert ticket['orders'][0] == stop_order()
        clock.return_value += 20000
        info.return_value['nRequestsUsed'] = 0
        ticket['book'] = book('112')
        await drive_exit(runtime, ticket, AsyncMock())
        assert ticket['exit_fast']
        clock.return_value += 4000
        info.return_value['nRequestsUsed'] = 800
        ticket['book'] = book('113')
        before = len(sent)
        await drive_exit(runtime, ticket, AsyncMock())
        assert len(sent) == before and not ticket['exit_fast']
        assert sum(row['event'] == 'action_budget_low' for row in read_records(runtime['config']['record_path'])) == 2


async def check_bracket_refusal():
    async with fixture(resident=True) as (runtime, fake):
        fake['bracket_refusal'] = 'unsupported_kind'
        async def arm(runtime, ticket):
            ticket['alive'] = True
        with patch('desk.desk_executor.arm', arm):
            result = await submit(runtime, envelope())
        assert result['refused'] == 'unsupported_kind'
        assert len(fake['intents']) == 1 and fake['intents'][0]['kind'] == 'bracket'
        assert 'take_profit' not in fake['intents'][0]
        assert fake['intents'][0]['stop']['trigger_px'] == '98000'
        assert any(row['event'] == 'bracket_refused' for row in read_records(runtime['config']['record_path']))


async def check_giveback():
    config = exit_config({'exit_band': '0.005'})
    action = exit_actions(book(), position(), [], 'target', config)[0]
    assert exit_actions(book('109.78'), position(), [resting(action)], 'target', config) == [{'action': 'cancel', 'oid': 1}]
    assert not exit_actions(book(), {**position(), 'target': None}, [], 'target', config)
    with driver_fixture() as (runtime, ticket, sent, info, clock):
        ticket['orders'].append(resting(action))
        ticket['book'] = book('109.78')
        await drive_exit(runtime, ticket, AsyncMock())
        assert ticket['closing'] is None and ticket['position'] == 10 and len(sent) == 1


async def check_event_only():
    check_timer_audit()
    with driver_fixture() as (runtime, ticket, sent, info, clock):
        ticket['closing'] = None
        with patch('desk.desk_executor.reconcile', AsyncMock()):
            await process_event(runtime, ticket, {'channel': 'coordinator_reply'})
            assert not sent
            await process_event(runtime, ticket, {'channel': 'l2Book', 'data': book()})
        assert len(sent) == 1 and sent[0]['tif'] == 'Alo' and sent[0]['reduce_only']
        assert backstop_actions(position('4'), [stop_order()])[0]['action'] == 'replace_backstop'


async def main():
    cases = [check_target_slices, check_touch_stable, check_requote, check_escalation, check_backstop_band,
             check_thin_book, check_ratio_guard, check_resize, check_flat, check_budget, check_bracket_refusal,
             check_giveback, check_event_only]
    for index, check in enumerate(cases, 1):
        await check()
        print('PASS exit algo ' + str(index) + ': ' + check.__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
