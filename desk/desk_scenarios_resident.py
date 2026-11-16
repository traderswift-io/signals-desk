import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import patch

from .desk_executor import event_worker, process_event, shutdown
from .desk_record import now_ms
from .desk_scenarios_bracket import fixture as bracket_fixture
from .desk_scenarios_fakes import fill_passive


@asynccontextmanager
async def fixture():
    async with bracket_fixture(resident=True) as (runtime, fake):
        async def arm(runtime, ticket):
            ticket['alive'] = True
            fake['events'].append('watcher_armed')
        with patch('desk.desk_executor.arm', arm):
            runtime['worker'] = asyncio.create_task(event_worker(runtime))
            try:
                yield runtime, fake
            finally:
                await shutdown(runtime)


def market_book(price='100000', asset='BTC', depth='0.2'):
    price = Decimal(price)
    return {'channel': 'l2Book', 'data': {'coin': asset, 'levels': [
        [{'px': str(price - 1), 'sz': str(Decimal(depth) * 2)}], [{'px': str(price + 1), 'sz': depth}]]}}


async def next_event(runtime, ticket, event):
    timestamp = max(now_ms(), (ticket.get('last_exit_action_ms') or 0) + 10000 * (ticket.get('exit_action_cost') or 1))
    runtime['config']['fixture_clock']['now_ms'] = timestamp
    with patch('desk.desk_exit_driver.now_ms', return_value=timestamp):
        await process_event(runtime, ticket, event)
    if not runtime.get('worker'):
        while not runtime['events'].empty():
            pending_ticket, pending_event = runtime['events'].get_nowait()
            await process_event(runtime, pending_ticket, pending_event)
            runtime['events'].task_done()
    await runtime['events'].join()


async def finish_exit(runtime, fake, ticket, price='98999'):
    for _ in range(16):
        await next_event(runtime, ticket, market_book(price, ticket['signal']['asset']))
        fill_passive(fake)
        await next_event(runtime, ticket, {'channel': 'userFills'})
        if ticket['state'] == 'exited':
            break
    assert ticket['state'] == 'exited' and fake['position'] == 0 and not fake['orders']
