import asyncio
from decimal import Decimal
from json import dumps
from pathlib import Path
from unittest.mock import patch

from .desk_bracket import signal_bracket
from .desk_executor import submit
from .desk_io import load_config
from .desk_record import now_ms, read_records, signed_today
from .desk_scenarios_bracket import envelope, fixture
from .desk_scenarios_checks import must_refuse
from .desk_scenarios_fakes import meta
from .desk_signal import parse_signal
from .signer_delegate_client import sign_intent


async def submit_distance(stop, target, short=False, resident=False, maximum=1000, mark='100000'):
    async with fixture(resident=resident) as (runtime, fake):
        runtime['config']['max_trigger_distance_bps'] = maximum
        fake['mark'] = mark
        value = envelope(short=short)
        value['payload']['levels'].update(stop=stop, target=target)
        distances = [('sl', abs(Decimal(stop) - Decimal(mark)) / Decimal(mark) * 10000)]
        if target is not None:
            distances.append(('tp', abs(Decimal(target) - Decimal(mark)) / Decimal(mark) * 10000))
        refused = next(((leg, distance) for leg, distance in distances if distance > maximum), None)

        async def arm(runtime, ticket):
            ticket['alive'] = True
            fake['events'].append('watcher_armed')

        with (patch('desk.desk_executor.arm', arm),
              patch('desk.signer_delegate_client.sign_intent', wraps=sign_intent) as signing):
            result = await submit(runtime, value)
        rows = read_records(runtime['config']['record_path'])
        if refused:
            assert result == {'refused': 'trigger_distance_exceeds_max'}, result
            signing.assert_not_called()
            assert fake['prepares'] == 0 and not fake['intents'] and not fake['orders']
            assert 'watcher_armed' not in fake['events']
            assert signed_today(runtime['config'], 'BTC') == 0
            refusals = [row for row in rows if row.get('reason') == result['refused']]
            assert len(refusals) == 1
            assert refusals[0]['asset'] == 'BTC' and refusals[0]['leg'] == refused[0]
            assert Decimal(refusals[0]['distance_bps']) == refused[1]
        else:
            assert result == {'state': 'filled' if resident else 'sent'}, result
            assert signing.call_count == 2
            assert fake['prepares'] == 1 and len(fake['intents']) == 1
            intent = fake['intents'][0]
            assert intent['kind'] == ('order' if resident else 'bracket')
            entry = intent if resident else intent['entry']
            assert entry['is_buy'] == (not short)
            assert signed_today(runtime['config'], 'BTC') == Decimal(entry['size']) > 0
            if not resident:
                assert Decimal(intent['stop']['trigger_px']) == Decimal(stop)
                assert intent['stop']['is_buy'] == short
                assert ('take_profit' in intent) == (target is not None)
                if target is not None:
                    assert Decimal(intent['take_profit']['trigger_px']) == Decimal(target)
                    assert intent['take_profit']['is_buy'] == short


async def check_long_stop(accepted):
    for resident in (False, True):
        await submit_distance('91000' if accepted else '89000', '102000', resident=resident)


async def check_target():
    for resident in (False, True):
        await submit_distance('99000', '111000', resident=resident)


async def check_short():
    for resident in (False, True):
        for stop, target in (('109000', '98000'), ('111000', '98000'), ('101000', '89000')):
            await submit_distance(stop, target, short=True, resident=resident)


async def check_boundaries():
    for resident in (False, True):
        await submit_distance('90000', '110000', resident=resident)
        await submit_distance('110000', '90000', short=True, resident=resident)
        await submit_distance('91000', None, resident=resident)
        await submit_distance('91000', None, maximum=800, resident=resident)
        await submit_distance('97900', None, mark='110000', resident=resident)
    signal = parse_signal(envelope(), meta(), now_ms())
    signal['levels'].update(stop='90000.45', target=None)
    try:
        signal_bracket({}, signal, '1', 5, '100000.5')
    except ValueError as error:
        assert str(error) == 'trigger_distance_exceeds_max'
        assert error.details['leg'] == 'sl' and Decimal(error.details['distance_bps']) > 1000
    else:
        raise AssertionError('Rounded trigger outside distance limit accepted')


async def check_config():
    async with fixture() as (runtime, fake):
        config = dict(runtime['config'])
        path = Path(config['record_path']).parent / 'config.json'
        for band in ('0.008', '0.005', 0.008, 0.005):
            config['exit_band'] = band
            path.write_text(dumps(config))
            loaded = load_config(path)
            assert Decimal(str(loaded['exit_band'])) == Decimal(str(band))
            assert loaded['max_trigger_distance_bps'] == 1000
        for band in ('0.0081', 0.0081, '0', '-0.001', 'NaN', 'Infinity', None, True, 'bad'):
            config['exit_band'] = band
            path.write_text(dumps(config))
            must_refuse(lambda: load_config(path), 'invalid_exit_band')
        config['exit_band'] = '0.005'
        for maximum in (1, 800, 1000):
            config['max_trigger_distance_bps'] = maximum
            path.write_text(dumps(config))
            assert load_config(path)['max_trigger_distance_bps'] == maximum
        for maximum in (0, -1, 1001, 1000.0, '1000', None, True, False):
            config['max_trigger_distance_bps'] = maximum
            path.write_text(dumps(config))
            must_refuse(lambda: load_config(path), 'invalid_max_trigger_distance_bps')


async def main():
    cases = [('stop 900 bps below mark accepted and signed', lambda: check_long_stop(True)),
             ('stop 1100 bps below mark refused sl and nothing signed', lambda: check_long_stop(False)),
             ('target 1100 bps from mark refused tp', check_target),
             ('short mirrored', check_short),
             ('exit band 0.008 loads 0.0081 refuses and 0.005 loads', check_config),
             ('trigger distance boundaries, configured cap, current mark and rounded prices', check_boundaries)]
    for label, check in cases:
        await check()
        print('PASS ' + label, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
