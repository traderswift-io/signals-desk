import asyncio
import math
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .desk_delivery_store import append_delivery_record as append_record, prepare_delivery_store as delivery_state
from .desk_record import append_record as diagnostic_record, delivery_path, now_ms


def delivery_headers(config, allow_http):
    endpoint = urlsplit(config['signals_url'])
    local = allow_http and endpoint.scheme == 'http' and endpoint.hostname == '127.0.0.1'
    if (not endpoint.hostname or endpoint.username is not None or endpoint.password is not None
            or '?' in config['signals_url'] or '#' in config['signals_url']
            or not (endpoint.scheme == 'https' or local)):
        raise ValueError('invalid_signals_url')
    if endpoint.port is not None and endpoint.port < 1:
        raise ValueError('invalid_signals_url')
    token = Path(config['subscriber_token_file']).read_text().splitlines()
    if len(token) != 1 or not token[0].strip():
        raise ValueError('invalid_subscriber_token')
    headers = {'Authorization': 'Bearer ' + token[0].strip(),
               'CF-Access-Client-Id': config['signals_access_client_id'],
               'CF-Access-Client-Secret': config['signals_access_client_secret']}
    if any(not isinstance(value, str) or not value.strip() or '\r' in value or '\n' in value
           for value in headers.values()):
        raise ValueError('invalid_delivery_credentials')
    return headers


def delivery_route(config):
    endpoint = urlsplit(config['signals_url'])
    return urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path.rstrip('/') + '/v1/deliveries', '', ''))


async def persist_delivery(runtime, event, values):
    state = runtime['deliveries']
    async with state['writes']:
        await asyncio.to_thread(append_record, delivery_path(runtime['config']), event, **values)
        event_id = values.get('event_id')
        if event == 'delivery_pending':
            state['pending'][event_id] = values
        elif event in ('delivery_reported', 'delivery_abandoned'):
            state['pending'].pop(event_id, None)
            if event == 'delivery_abandoned':
                state['abandoned'].add(event_id)
        if values.get('status') == 429:
            state['not_before'] = max(state['not_before'], values.get('not_before', 0))


async def write_delivery(runtime, event, **values):
    state = runtime['deliveries']
    task = asyncio.create_task(persist_delivery(runtime, event, values))
    state['persistence'].add(task)

    def persisted(done):
        state['persistence'].discard(done)
        if not done.cancelled() and done.exception() is not None:
            asyncio.get_running_loop().call_exception_handler({'message': 'delivery_persistence_failed'})

    task.add_done_callback(persisted)
    await asyncio.shield(task)


async def disable_store(runtime, error, reason=None):
    if reason is None:
        reason = 'delivery_store_unavailable' if isinstance(error, OSError) else 'delivery_store_corrupt'
    try:
        await asyncio.to_thread(diagnostic_record, runtime['config']['record_path'], 'deliveries_disabled',
                                reason=reason, exception=type(error).__name__)
    except Exception:
        asyncio.get_running_loop().call_exception_handler({'message': 'deliveries_disabled_record_failed'})


async def prepare_deliveries(runtime):
    try:
        await initialize_deliveries(runtime)
    except Exception as error:
        runtime.setdefault('deliveries', {})['enabled'] = False
        await disable_store(runtime, error, reason=type(error).__name__)


async def initialize_deliveries(runtime):
    if 'deliveries' in runtime or 'signals_url' not in runtime['config']:
        return
    state = {'enabled': False, 'tasks': set(), 'persistence': set(), 'worker': None, 'writes': asyncio.Lock(),
             'pending': {}, 'abandoned': set(), 'not_before': 0, 'requested': False, 'fresh': None, 'stopping': False}
    runtime['deliveries'] = state
    try:
        pending, abandoned, not_before = await asyncio.to_thread(delivery_state, runtime['config'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        await disable_store(runtime, error)
        return
    try:
        state['headers'] = await asyncio.to_thread(delivery_headers, runtime['config'],
                                                   runtime.get('allow_http_deliveries', False))
        state['url'] = delivery_route(runtime['config'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        try:
            await write_delivery(runtime, 'deliveries_disabled', reason=type(error).__name__)
        except Exception as store_error:
            await disable_store(runtime, store_error)
        return
    state.update(pending=pending, abandoned=abandoned, not_before=not_before, enabled=True)


def request_timeout(config):
    try:
        seconds = float(config.get('request_timeout_seconds', 10))
        if math.isfinite(seconds) and 0 < seconds <= 30:
            return seconds
    except (ValueError, TypeError, OverflowError):
        pass
    return 10


def retry_time(value, timestamp):
    try:
        milliseconds = float(value) * 1000
        if math.isfinite(milliseconds):
            return timestamp + max(0, math.ceil(milliseconds))
    except (ValueError, TypeError):
        pass
    try:
        return max(timestamp, math.ceil(parsedate_to_datetime(value).timestamp() * 1000))
    except (ValueError, TypeError, OverflowError, AttributeError):
        return timestamp


async def queue_delivery(runtime, signal_id, event_id):
    state = runtime.get('deliveries', {})
    if not state.get('enabled') or event_id in state['pending'] or event_id in state['abandoned']:
        return
    await write_delivery(runtime, 'delivery_pending', signal_id=signal_id, event_id=event_id,
                         status='queued', attempts=0, next_attempt=0)


async def post_delivery(runtime, row):
    config, state = runtime['config'], runtime['deliveries']
    values = {name: row[name] for name in ('signal_id', 'event_id')}
    status, not_before, recorded = 'transport_error', 0, False
    try:
        async with runtime['session'].post(state['url'],
                json=values, headers=state['headers'], allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=request_timeout(config))) as response:
            status = response.status
            if status == 429:
                not_before = retry_time(response.headers.get('Retry-After'), now_ms())
            if 200 <= status < 300:
                body = await response.json()
                recorded = isinstance(body, dict) and body.get('recorded') is True
    except TimeoutError:
        status = 'timeout'
    except Exception:
        pass
    if recorded:
        await write_delivery(runtime, 'delivery_reported', **values)
        return status
    attempts = row.get('attempts', 0) + 1
    next_attempt = max(not_before, now_ms() + min(5000 * 2 ** min(attempts - 1, 8), 900000))
    event = 'delivery_abandoned' if attempts >= 20 else 'delivery_pending'
    await write_delivery(runtime, event, **values, status=status, attempts=attempts,
                         next_attempt=next_attempt, not_before=next_attempt if status == 429 else not_before)
    return status


async def report_delivery(runtime, fresh):
    state = runtime['deliveries']
    if state['not_before'] > now_ms():
        return
    eligible = [row for key, row in state['pending'].items() if key != fresh
                and max(row.get('next_attempt', 0), row.get('not_before', 0)) <= now_ms()][:10]
    for row in eligible:
        if max(row.get('next_attempt', 0), row.get('not_before', 0)) > now_ms():
            continue
        if await post_delivery(runtime, row) == 429:
            return
    row = state['pending'].get(fresh)
    if row and max(row.get('next_attempt', 0), row.get('not_before', 0)) <= now_ms():
        await post_delivery(runtime, row)


async def delivery_worker(runtime):
    state = runtime['deliveries']
    while state['requested']:
        fresh = state['fresh']
        state.update(requested=False, fresh=None)
        await report_delivery(runtime, fresh)


def delivery_done(runtime, task):
    state = runtime['deliveries']
    state['tasks'].discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        if state['requested']:
            schedule_delivery(runtime)
        return
    recording = asyncio.create_task(write_delivery(runtime, 'delivery_error', reason=type(error).__name__))
    state['tasks'].add(recording)

    def recorded(done):
        state['tasks'].discard(done)
        if not done.cancelled() and done.exception() is not None:
            asyncio.get_running_loop().call_exception_handler({'message': 'delivery_error_record_failed'})
        if state['requested']:
            schedule_delivery(runtime)

    recording.add_done_callback(recorded)


def schedule_delivery(runtime, event_id=None):
    state = runtime.get('deliveries', {})
    if not state.get('enabled') or state['stopping']:
        return
    state['requested'] = True
    if event_id is not None:
        state['fresh'] = event_id
    if not state['tasks']:
        task = asyncio.create_task(delivery_worker(runtime))
        state['worker'] = task
        state['tasks'].add(task)
        task.add_done_callback(lambda done: delivery_done(runtime, done))


async def finish_deliveries(runtime):
    state = runtime.get('deliveries', {})
    state.update(stopping=True, requested=False)
    deadline = asyncio.get_running_loop().time() + 5
    for task in tuple(state.get('tasks', ())):
        task.cancel()
    while state.get('tasks') or state.get('persistence'):
        tasks = state.get('tasks', set()) | state.get('persistence', set())
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        await asyncio.gather(*done, return_exceptions=True)
        if pending:
            return
