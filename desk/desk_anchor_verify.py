import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import aiohttp


def canonical(value):
    if isinstance(value, dict):
        return '{' + ','.join(canonical(k) + ':' + canonical(value[k])
                              for k in sorted(value, key=lambda s: s.encode('utf-16be'))) + '}'
    if isinstance(value, list):
        return '[' + ','.join(canonical(v) for v in value) + ']'
    if value is None or isinstance(value, (str, bool)) or type(value) is int and abs(value) <= 9007199254740991:
        result = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        result.encode('utf-8')
        return result
    raise ValueError('payload', 'must contain only strings, safe integers, booleans and null')


async def read_chain(session, base, path, kind, reads, timeout):
    try:
        async with session.get(base + path, timeout=timeout, allow_redirects=False) as response:
            reads.append(response.status)
            if response.status == 429:
                raise ValueError('chain_unavailable')
            if response.status != 200:
                raise ValueError(kind + '_missing')
            try:
                body = await response.json()
            except (ValueError, aiohttp.ContentTypeError):
                raise ValueError('chain_unavailable') from None
    except (aiohttp.ClientError, TimeoutError):
        raise ValueError('chain_unavailable') from None
    if not isinstance(body, dict):
        raise ValueError('chain_unavailable')
    if body.get('vetoed') is True:
        raise ValueError(kind + '_vetoed')
    return body


async def verify(config, envelope):
    payload = envelope.get('payload') if isinstance(envelope, dict) else None
    signal_id = payload.get('signal_id') if isinstance(payload, dict) else None
    result = {'signal_id': signal_id, 'result': 'pass', 'reads': []}
    try:
        digest = hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {**result, 'result': 'payload_not_canonical'}
    if not isinstance(signal_id, str) or not signal_id.strip():
        return {**result, 'result': 'invalid_envelope'}
    try:
        base = config['chain_url'].rstrip('/')
        provider = quote(config['provider_id'], safe='')
        address = config['provider_address']
        timeout = aiohttp.ClientTimeout(total=float(config.get('request_timeout_seconds', 10)))
        async with aiohttp.ClientSession() as session:
            await read_chain(session, base, '/network/v1/charter/signals', 'charter', result['reads'], timeout)
            record = await read_chain(session, base, '/network/v1/provider/signals/' + provider,
                                      'provider', result['reads'], timeout)
            if record.get('provider_address') != address:
                raise ValueError('provider_address_mismatch')
            anchor = await read_chain(session, base, '/network/v1/anchor/signals/signal/' +
                                      quote(signal_id, safe='') + '/' + provider, 'anchor', result['reads'], timeout)
            if anchor.get('segment_hash') != digest:
                raise ValueError('hash_mismatch')
    except ValueError as error:
        result['result'] = str(error)
    return result


async def main(arguments):
    try:
        config = json.loads(Path(arguments.config).read_text())
        envelope = json.loads(Path(arguments.envelope).read_text())
        result = await verify(config, envelope)
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        result = {'signal_id': None, 'result': 'invalid_input', 'reads': []}
    print(json.dumps(result, ensure_ascii=False))
    print('VERIFY | ' + str(result['signal_id']) + ' | ' + result['result'])
    return int(result['result'] != 'pass')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('envelope')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
