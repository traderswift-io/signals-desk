import asyncio
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import aiohttp

from .desk_journal_schema import validate_record
from .desk_record import read_records
from .desk_scenarios_bracket import command_result, envelope, fixture
from .desk_scenarios_checks import check_tree_style
from .desk_scenarios_fakes import submit_verified_fixture
from .desk_scenarios_journal_schema import check_static_coverage
from .signer_delegate_client import account_equity, account_state, reconcile_asset


async def check_account_equity_modes():
    for resident in (False, True):
        for mode, perp, spot, expected in (
                ('unifiedAccount', '0', '100', '100'),
                ('unifiedAccount', '25.5', '74.5', '100'),
                ('portfolioMargin', '0', '100', '100'),
                ('disabled', '100', '50', '100'),
                ('default', '100', '50', '100')):
            async with fixture(resident=resident) as (runtime, fake):
                fake.update(account_mode=mode, perp_equity=perp, spot_balances=[
                    {'coin': 'OTHER', 'total': 'not-a-number'}, {'coin': 'USDC', 'total': spot}])
                if resident:
                    runtime['config']['policy']['asset_caps'][0]['max_order_size'] = '0.00001'
                value = envelope('equity-mode')
                for action in ('size', 'gate', 'run'):
                    before = len(fake.get('info_requests', []))
                    result = (await submit_verified_fixture(runtime, value) if resident and action == 'run'
                              else await command_result(runtime, action, value))
                    requests = fake['info_requests'][before:]
                    assert sum(row['type'] == 'userAbstraction' for row in requests) == 1
                    shared = mode in ('unifiedAccount', 'portfolioMargin')
                    assert sum(row['type'] == 'spotClearinghouseState' for row in requests) == int(shared)
                    if action in ('size', 'gate'):
                        sizing = result if action == 'size' else result['sizing']
                        assert Decimal(sizing['equity']) == Decimal(expected), result
                        assert Decimal(sizing['size']) == Decimal('0.001'), result
                    else:
                        assert result == ({'refused': 'over_max_order_size'} if resident else {'state': 'sent'}), result
                rows = read_records(runtime['config']['record_path'])
                gate = next(row for row in rows if row['event'] == 'gate')
                assert gate['account_mode'] == mode
                assert Decimal(gate['perp_equity']) == Decimal(perp)
                assert Decimal(gate['spot_usdc_equity']) == (Decimal(spot) if shared else Decimal(0))
                for field in ('account_mode', 'perp_equity', 'spot_usdc_equity'):
                    assert type(gate[field]) is str
                    for invalid in (None, True, 100, Decimal('100'), {}, []):
                        altered = deepcopy(gate)
                        altered[field] = invalid
                        try:
                            validate_record(altered)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError((field, invalid))
                legacy = {name: item for name, item in gate.items()
                          if name not in ('account_mode', 'perp_equity', 'spot_usdc_equity')}
                validate_record(legacy)
                if resident:
                    assert fake['prepares'] == 0 and fake['intents'] == []
    print('PASS equity modes: both entry paths, Decimal sizing, USDC only, one mode read, exact gate types and legacy rows', flush=True)


async def check_account_equity_refusals():
    cases = [({'account_mode': mode}, 'account_mode_unknown')
             for mode in ('dexAbstraction', 'newMode', None, {}, [], 1)]
    cases.append(({'account_mode_unavailable': True}, 'account_mode_unknown'))
    for invalid in ('NaN', 'sNaN', 'Infinity', '-Infinity', '-1', None, 'bad', True):
        cases.append(({'perp_equity': invalid}, 'invalid_account_equity'))
        cases.append(({'account_mode': 'unifiedAccount', 'spot_balances': [
            {'coin': 'USDC', 'total': invalid}]}, 'invalid_account_equity'))
    cases.extend(({'account_mode': 'unifiedAccount', 'spot_balances': balances}, 'invalid_account_equity')
                 for balances in (None, {}, [None], [{'coin': 'USDC'}],
                                  [{'coin': 'USDC', 'total': '1'}, {'coin': 'USDC', 'total': '2'}]))
    for resident in (False, True):
        for changes, expected in cases:
            async with fixture(resident=resident) as (runtime, fake):
                fake.update(changes)
                for action in ('size', 'gate', 'run'):
                    value = envelope('equity-refused')
                    result = (await submit_verified_fixture(runtime, value) if resident and action == 'run'
                              else await command_result(runtime, action, value))
                    assert result == {'refused': expected}, (resident, changes, action, result)
                assert fake['prepares'] == 0 and fake['intents'] == []
    print('PASS equity refusals: unknown and unreadable modes, malformed and invalid equity, no coordinator sends', flush=True)


async def check_account_equity_reads():
    runtime = {'config': {'account_address': '<fake-account>'}}
    state = {'marginSummary': {'accountValue': '100'}, 'assetPositions': []}
    read = AsyncMock(side_effect=['unifiedAccount', state, {'balances': [{'coin': 'OTHER', 'total': '999'}]}])
    with patch('desk.signer_delegate_client.read_info', read):
        result = await account_equity(runtime, 'builder:ASSET')
    assert result['equity'] == Decimal('100') and result['spot_usdc_equity'] == Decimal(0)
    assert result['account'] is state
    assert [call.args[1] for call in read.call_args_list] == [
        'userAbstraction', 'clearinghouseState', 'spotClearinghouseState']
    assert [call.kwargs for call in read.call_args_list] == [
        {'user': '<fake-account>'}, {'user': '<fake-account>', 'dex': 'builder'}, {'user': '<fake-account>'}]
    for failure in (TimeoutError(), aiohttp.ClientError(), ValueError(), OSError()):
        for replies, reason in (([failure], 'account_mode_unknown'),
                                (['unifiedAccount', failure], 'invalid_account_equity'),
                                (['unifiedAccount', state, failure], 'invalid_account_equity')):
            with patch('desk.signer_delegate_client.read_info', AsyncMock(side_effect=replies)):
                try:
                    await account_equity(runtime, 'ASSET')
                except ValueError as error:
                    assert str(error) == reason
                else:
                    raise AssertionError(reason)
    read = AsyncMock(side_effect=[state, [], [], state])
    with patch('desk.signer_delegate_client.read_info', read):
        assert await account_state(runtime, 'builder:ASSET') is state
        assert (await reconcile_asset(runtime, 'builder:ASSET'))['account'] is state
    assert [call.args[1] for call in read.call_args_list] == [
        'clearinghouseState', 'frontendOpenOrders', 'userFills', 'clearinghouseState']
    for malformed in (None, {}, {'marginSummary': None}, {'marginSummary': {}},
                      {'marginSummary': {'accountValue': '1e1000000'}}):
        read = AsyncMock(side_effect=['default', malformed])
        with patch('desk.signer_delegate_client.read_info', read):
            try:
                await account_equity(runtime, 'ASSET')
            except ValueError as error:
                assert str(error) == 'invalid_account_equity'
            else:
                raise AssertionError(malformed)
    read = AsyncMock(side_effect=['default', {'marginSummary': {'accountValue': '0'}}])
    with patch('desk.signer_delegate_client.read_info', read):
        assert (await account_equity(runtime, 'ASSET'))['equity'] == Decimal(0)
    print('PASS client reads: builder routing, same user, absent USDC, transport failures and unchanged reconciliation', flush=True)


async def check_account_equity_flatness():
    for resident in (False, True):
        for busy in ({'position': Decimal('0.01')}, {'orders': [{'coin': 'BTC', 'oid': 42}]}):
            async with fixture(resident=resident) as (runtime, fake):
                fake.update(account_mode='unifiedAccount', perp_equity='0',
                            spot_balances=[{'coin': 'USDC', 'total': '100'}], **busy)
                result = await submit_verified_fixture(runtime, envelope('flatness'))
                assert result == {'refused': 'account_not_flat'}, result
                assert fake['prepares'] == 0 and fake['intents'] == []
    print('PASS flatness: positions and orders still refuse both entry paths before coordinator calls', flush=True)


async def main():
    await check_account_equity_modes()
    await check_account_equity_refusals()
    await check_account_equity_reads()
    await check_account_equity_flatness()
    count = check_static_coverage()
    check_tree_style()
    print(f'PASS writer coverage: {count} call sites; repository style and line limits', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
