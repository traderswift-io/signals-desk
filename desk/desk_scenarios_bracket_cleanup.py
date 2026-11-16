from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from .desk_executor import reconcile, recover, submit
from .desk_record import latest_tickets, read_records
from .desk_routine import maintain_routine


async def check_leftover_leg_after_stop(fixture, envelope):
    timestamp = int(datetime(2026, 11, 16, 12, 16, tzinfo=timezone.utc).timestamp() * 1000)
    with patch('time.time_ns', return_value=timestamp * 1000000):
        async with fixture() as (runtime, fake):
            assert await submit(runtime, envelope()) == {'state': 'sent'}
            fake['position'] = Decimal('0.004')
            await maintain_routine(runtime, reconcile)
            assert not runtime['tickets']['bracket'].get('entry_done')
            fake['position'] = Decimal('0.01')
            fake['orders'] = [order for order in fake['orders'] if order['oid'] != 42]
            await maintain_routine(runtime, reconcile)
            recorded = next(iter(latest_tickets(runtime['config']).values()))
            assert recorded['entry_done'] and recorded['state'] == 'filled'
            assert not recorded.get('entry_cancel_reason') and len(fake['intents']) == 1
            fake['position'] = Decimal(0)
            fake['orders'] = [order for order in fake['orders'] if order['oid'] != 43]
            runtime['tickets'].clear()
            await recover(runtime)
            assert runtime['tickets']['bracket']['entry_done']
            result = await maintain_routine(runtime, reconcile)
            assert result == {'results': [{'signal_id': 'bracket', 'state': 'exited', 'reason': 'entry_done_flat'}]}
            assert not fake['orders'] and runtime['tickets']['bracket']['state'] == 'exited'
            assert [intent['oid'] for intent in fake['intents'] if intent['kind'] == 'cancel'] == [44]
            assert next(iter(latest_tickets(runtime['config']).values()))['state'] == 'exited'
            assert not any(row.get('reason') in ('reply_by_expired', 'signal_invalidated')
                           for row in read_records(runtime['config']['record_path']))
            assert await submit(runtime, envelope('next')) == {'state': 'sent'}
