import argparse
import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

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
    raise ValueError('payload_not_canonical')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('non_json_constant')


def decode_json(text):
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)


def validate_unicode(value):
    if isinstance(value, str):
        value.encode('utf-8')
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_unicode(key)
            validate_unicode(item)
    elif isinstance(value, list):
        for item in value:
            validate_unicode(item)


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value) is not None and value not in ('.', '..')


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('invalid_input')
    address = config.get('provider_address')
    provider = config.get('provider_id')
    base = config.get('chain_url')
    seconds = config.get('request_timeout_seconds', 10)
    if not isinstance(address, str) or not address or not valid_id(provider):
        raise ValueError('invalid_input')
    if type(seconds) not in (int, float) or not 0 < seconds <= 60 or not math.isfinite(seconds):
        raise ValueError('invalid_input')
    if not isinstance(base, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in base):
        raise ValueError('invalid_input')
    if any(c in base for c in ('?', '#', '\\')):
        raise ValueError('invalid_input')
    parts = urlsplit(base)
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.path not in ('', '/') or parts.query or parts.fragment or parts.netloc.endswith(':')):
        raise ValueError('invalid_input')
    parts.port
    return parts, quote(provider, safe=''), address, aiohttp.ClientTimeout(total=seconds)


async def read_chain(session, base, path, kind, reads, timeout):
    try:
        url = base._replace(path=path, query='', fragment='').geturl()
        async with session.get(url, timeout=timeout, allow_redirects=False) as response:
            reads.append(response.status)
            if response.status == 429 or response.status >= 500:
                return None, 'chain_unavailable'
            if response.status != 200:
                return None, kind + '_missing'
            body = await response.json(loads=decode_json)
            validate_unicode(body)
    except (aiohttp.ClientError, TimeoutError, ValueError, LookupError, TypeError, RecursionError):
        return None, 'chain_unavailable'
    if not isinstance(body, dict):
        return None, 'chain_unavailable'
    if body.get('vetoed') is not False:
        return None, kind + '_vetoed'
    return body, None


async def verify(config, envelope):
    result = {'signal_id': None, 'result': 'invalid_input', 'reads': []}
    try:
        base, provider, address, timeout = validate_config(config)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return result
    try:
        payload = envelope.get('payload') if isinstance(envelope, dict) else None
        signal_id = payload.get('signal_id') if isinstance(payload, dict) else None
        if not valid_id(signal_id):
            return {**result, 'result': 'invalid_envelope'}
        result['signal_id'] = signal_id
        digest = hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()
    except RecursionError:
        return result
    except (ValueError, TypeError, AttributeError):
        return {**result, 'result': 'payload_not_canonical'}
    try:
        async with aiohttp.ClientSession() as session:
            paths = [('/network/v1/charter/signals', 'charter'),
                     ('/network/v1/provider/signals/' + provider, 'provider'),
                     ('/network/v1/anchor/signals/signal/' + quote(signal_id, safe='') + '/' + provider, 'anchor')]
            for path, kind in paths:
                body, reason = await read_chain(session, base, path, kind, result['reads'], timeout)
                if reason:
                    return {**result, 'result': reason}
                if kind == 'provider':
                    observed = body.get('provider_address')
                    if not isinstance(observed, str) or not observed or observed != address:
                        return {**result, 'result': 'provider_address_mismatch'}
                if kind == 'anchor' and body.get('segment_hash') != digest:
                    return {**result, 'result': 'hash_mismatch'}
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, RecursionError):
        return {**result, 'result': 'chain_unavailable'}
    return {**result, 'result': 'pass'}


async def main(arguments):
    try:
        config = decode_json(Path(arguments.config).read_text())
        validate_config(config)
        envelope = decode_json(Path(arguments.envelope).read_text())
        result = await verify(config, envelope)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RecursionError):
        result = {'signal_id': None, 'result': 'invalid_input', 'reads': []}
    signal_id = result['signal_id'] if valid_id(result['signal_id']) else '-'
    print(json.dumps({'signal_id': result['signal_id'], 'reads': result['reads']}, ensure_ascii=True))
    print('VERIFY | ' + signal_id + ' | ' + result['result'])
    return int(result['result'] != 'pass')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('envelope')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
