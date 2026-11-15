import asyncio
import math
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp

from .desk_record import append_record, now_ms, pending_deliveries


def prepare_deliveries(runtime):
    if 'deliveries' in runtime or not runtime['config'].get('signals_url'):
        return
    config = runtime['config']
    token = Path(config['subscriber_token_file']).read_text().strip()
    if not token or '\n' in token or '\r' in token:
        raise ValueError('invalid_subscriber_token')
    runtime['deliveries'] = {'lock': asyncio.Lock(), 'tasks': set(), 'headers': {
        'Authorization': 'Bearer ' + token,
        'CF-Access-Client-Id': config['signals_access_client_id'],
        'CF-Access-Client-Secret': config['signals_access_client_secret']}}


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


async def post_delivery(runtime, signal_id, event_id, timestamp):
    config = runtime['config']
    values = {'signal_id': signal_id, 'event_id': event_id}
    status, not_before = 'transport_error', None
    append_record(config['record_path'], 'delivery_pending', **values, status='sending')
    try:
        async with runtime['session'].post(config['signals_url'].rstrip('/') + '/v1/deliveries',
                json=values, headers=runtime['deliveries']['headers'], allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=float(config.get('request_timeout_seconds', 10)))) as response:
            status = response.status
            if status == 429:
                not_before = retry_time(response.headers.get('Retry-After'), timestamp)
            if 200 <= status < 300:
                body = await response.json()
                if isinstance(body, dict) and body.get('recorded') is True:
                    append_record(config['record_path'], 'delivery_reported', **values)
                    return
    except TimeoutError:
        status = 'timeout'
    except (aiohttp.ClientError, ValueError, TypeError):
        pass
    except asyncio.CancelledError:
        append_record(config['record_path'], 'delivery_pending', **values, status='cancelled')
        raise
    if not_before is not None:
        values['not_before'] = not_before
    append_record(config['record_path'], 'delivery_pending', **values, status=status)


async def report_delivery(runtime, signal_id, event_id, timestamp=None):
    timestamp = now_ms() if timestamp is None else timestamp
    async with runtime['deliveries']['lock']:
        pending = pending_deliveries(runtime['config'])
        eligible = [row for row in pending if row.get('not_before', 0) <= timestamp][:10]
        for row in eligible:
            await post_delivery(runtime, row['signal_id'], row['event_id'], timestamp)
        current = next((row for row in pending if row['event_id'] == event_id), {})
        if event_id is not None and not current:
            await post_delivery(runtime, signal_id, event_id, timestamp)


def schedule_delivery(runtime, signal_id=None, event_id=None, timestamp=None):
    if 'deliveries' not in runtime:
        return
    tasks = runtime['deliveries']['tasks']
    task = asyncio.create_task(report_delivery(runtime, signal_id, event_id, timestamp))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def finish_deliveries(runtime):
    if 'deliveries' in runtime:
        await asyncio.gather(*tuple(runtime['deliveries']['tasks']))
