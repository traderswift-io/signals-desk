import asyncio
import copy
import hashlib
import inspect
import io
import json
import tempfile
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from .desk_anchor_verify import canonical, main as command, verify


JCS_FIXTURES = [
    ({'b': 1, 'a': 'é', 'A': [True, None]}, '{"A":[true,null],"a":"é","b":1}'),
    ({'\ue000': 1, '\U00010000': 2, 'nested': {'z': False, 'a': [0, -9007199254740991, 9007199254740991]}},
     '{"nested":{"a":[0,-9007199254740991,9007199254740991],"z":false},"\U00010000":2,"\ue000":1}'),
    ({'signal_id': 'sample', 'levels': {'stop': None, 'entry': '100'}, 'text': 'line\n"\\é', 'links': []},
     '{"levels":{"entry":"100","stop":null},"links":[],"signal_id":"sample","text":"line\\n\\"\\\\é"}'),
]
PAYLOAD = {'signal_id': 'sample', 'publisher': 'publisher-placeholder', 'thesis': 'A',
           'levels': {'entry': '100', 'stop': '99'}, 'confidence': '0.7'}
PAYLOAD_TEXT = ('{"confidence":"0.7","levels":{"entry":"100","stop":"99"},'
                '"publisher":"publisher-placeholder","signal_id":"sample","thesis":"A"}')
CONFIG = {'chain_url': 'https://chain.traderswift.io', 'provider_id': 'provider-placeholder',
          'provider_address': 'address-placeholder', 'request_timeout_seconds': 2}
PATHS = ['/network/v1/charter/signals', '/network/v1/provider/signals/provider-placeholder',
         '/network/v1/anchor/signals/signal/sample/provider-placeholder']


def responses():
    digest = hashlib.sha256(PAYLOAD_TEXT.encode('utf-8')).hexdigest()
    return [(200, json.dumps(body), 'application/json') for body in
            [{'vetoed': False}, {'vetoed': False, 'provider_address': CONFIG['provider_address']},
             {'vetoed': False, 'segment_hash': digest}]]


def transport(replies, requests, seconds=2, paths=PATHS):
    prepared = []
    parameters = inspect.signature(aiohttp.ClientResponse).parameters
    writers = {name: value for name, value in
               (('writer', None), ('stream_writer', SimpleNamespace(output_size=0))) if name in parameters}
    for path, (status, body, mime) in zip(paths, replies, strict=True):
        parsed = URL(CONFIG['chain_url'] + path)
        headers = CIMultiDictProxy(CIMultiDict({'Content-Type': mime}))
        info = aiohttp.RequestInfo(parsed, 'GET', headers, parsed)
        response = aiohttp.ClientResponse('GET', parsed, **writers, continue100=None, timer=None,
                                          request_info=info, traces=[], loop=asyncio.get_running_loop(), session=None)
        response.status = status
        response._headers = headers
        response._body = body.encode('utf-8')
        prepared.append(response)

    @asynccontextmanager
    async def get(session, url, *, timeout, allow_redirects):
        parsed = URL(url)
        assert parsed.scheme == 'https' and parsed.host == 'chain.traderswift.io'
        assert not parsed.query_string and not parsed.fragment and not parsed.user
        assert parsed.path == paths[len(requests)], url
        assert timeout.total == seconds and allow_redirects is False
        response = prepared[len(requests)]
        requests.append(parsed.path)
        yield response
    return get


async def expect(config, envelope, reason, statuses=(), replies=None, seconds=2, paths=PATHS):
    requests = []
    with patch('aiohttp.ClientSession.get', new=transport(replies or responses(), requests, seconds, paths)):
        result = await verify(config, envelope)
    assert result['result'] == reason and result['reads'] == list(statuses), result
    assert requests == paths[:len(statuses)], requests
    return result


async def run_command(directory, config, envelope, reason, statuses=(), signal_id='sample', replies=None):
    config_path = directory / 'config.json'
    envelope_path = directory / 'envelope.json'
    config_path.write_text(config if isinstance(config, str) else json.dumps(config))
    envelope_path.write_text(envelope if isinstance(envelope, str) else json.dumps(envelope))
    output = io.StringIO()
    requests = []
    with (redirect_stdout(output),
          patch('aiohttp.ClientSession.get', new=transport(replies or responses(), requests))):
        code = await command(SimpleNamespace(config=config_path, envelope=envelope_path))
    lines = output.getvalue().splitlines()
    assert code == int(reason != 'pass') and len(lines) == 2, (code, lines)
    assert json.loads(lines[0]) == {'signal_id': signal_id, 'reads': list(statuses)}, lines
    assert lines[1] == 'VERIFY | ' + (signal_id or '-') + ' | ' + reason, lines
    assert requests == PATHS[:len(statuses)], requests


async def check_transport():
    requests = []
    with patch('aiohttp.ClientSession.get', new=transport(responses(), requests)):
        async with aiohttp.ClientSession() as session:
            for path, (_, body, _) in zip(PATHS, responses(), strict=True):
                async with session.get(CONFIG['chain_url'] + path, timeout=aiohttp.ClientTimeout(total=2),
                                       allow_redirects=False) as response:
                    assert response.status == 200
                    assert await response.json() == json.loads(body)
    assert requests == PATHS, requests
    print('PASS anchor transport: async request contexts and real JSON response decoding', flush=True)


async def check_cases(directory):
    cases = [
        ('pass', None, None, None, [200, 200, 200]),
        ('charter_missing', 0, 404, None, [404]),
        ('charter_vetoed', 0, 200, {'vetoed': True}, [200]),
        ('provider_missing', 1, 404, None, [200, 404]),
        ('provider_vetoed', 1, 200, {'vetoed': True}, [200, 200]),
        ('provider_address_mismatch', 1, 200, {'provider_address': 'different-placeholder'}, [200, 200]),
        ('anchor_missing', 2, 404, None, [200, 200, 404]),
        ('anchor_vetoed', 2, 200, {'vetoed': True}, [200, 200, 200]),
        ('hash_mismatch', None, None, None, [200, 200, 200]),
        ('payload_not_canonical', None, None, None, []),
        ('chain_unavailable', 1, 429, None, [200, 429]),
    ]
    for number, (reason, index, status, changes, reads) in enumerate(cases, 1):
        replies = responses()
        if index is not None:
            body = {**json.loads(replies[index][1]), **(changes or {})}
            replies[index] = (status, json.dumps(body), 'application/json')
        envelope = {'version': 1, 'payload': copy.deepcopy(PAYLOAD), 'segment_hash': 'untrusted',
                    'receipt': {'segment_hash': 'untrusted'}, 'provider_id': 'untrusted'}
        if reason == 'hash_mismatch':
            envelope['payload']['thesis'] = 'B'
            envelope['segment_hash'] = hashlib.sha256(PAYLOAD_TEXT.encode('utf-8')).hexdigest()
            envelope['receipt']['segment_hash'] = envelope['segment_hash']
        if reason == 'payload_not_canonical':
            envelope['payload']['confidence'] = 0.7
        await run_command(directory, CONFIG, envelope, reason, reads, replies=replies)
        print(f'PASS anchor {number}: {reason}, {len(reads)} reads', flush=True)


async def check_canonical():
    for payload, expected in JCS_FIXTURES:
        assert canonical(payload) == expected
        assert canonical(payload).encode('utf-8') == expected.encode('utf-8')
    assert canonical(PAYLOAD) == PAYLOAD_TEXT
    for invalid in (0.7, 9007199254740992, -9007199254740992, float('nan'), float('inf'),
                    '\ud800', {'\ud800': 1}, {1: 'invalid'}, (1,), {1}, b'bytes'):
        await expect(CONFIG, {'payload': {**PAYLOAD, 'invalid': invalid}}, 'payload_not_canonical')
    for failure in (aiohttp.ClientConnectionError(), TimeoutError(), RecursionError()):
        with patch('aiohttp.ClientSession.get', side_effect=failure) as request:
            result = await verify(CONFIG, {'payload': PAYLOAD})
        assert result == {'signal_id': 'sample', 'result': 'chain_unavailable', 'reads': []}, result
        assert request.call_count == 1
    print('PASS anchor 12: JCS fixtures, UTF-16 order, canonical refusals and transport failures', flush=True)


async def check_addresses():
    for address in (None, '', False, 0, [], {}):
        config = {**CONFIG, 'provider_address': address}
        await expect(config, {'payload': PAYLOAD}, 'invalid_input')
        replies = responses()
        replies[1] = (200, json.dumps({'vetoed': False, 'provider_address': address}), 'application/json')
        await expect(CONFIG, {'payload': PAYLOAD}, 'provider_address_mismatch', [200, 200], replies)
    config = dict(CONFIG)
    config.pop('provider_address')
    await expect(config, {'payload': PAYLOAD}, 'invalid_input')
    replies[1] = (200, '{"vetoed":false}', 'application/json')
    await expect(CONFIG, {'payload': PAYLOAD}, 'provider_address_mismatch', [200, 200], replies)
    print('PASS anchor 13: configured and returned address gaps and types', flush=True)


async def check_urls():
    for base in (None, '', 7, 'http://chain.traderswift.io', 'https:///missing',
                 'https://user@chain.traderswift.io', 'https://user:password@chain.traderswift.io',
                 'https://chain.traderswift.io?query=1', 'https://chain.traderswift.io#fragment',
                 'https://chain.traderswift.io?', 'https://chain.traderswift.io#',
                 'https://chain.traderswift.io/network', 'https://chain.traderswift.io//',
                 ' https://chain.traderswift.io', 'https://chain.\ntraderswift.io',
                 'https://chain.traderswift.io:bad', 'https://chain.traderswift.io:65536',
                 'https://[broken', 'https://chain.traderswift.io\\extra'):
        await expect({**CONFIG, 'chain_url': base}, {'payload': PAYLOAD}, 'invalid_input')
    await expect({**CONFIG, 'chain_url': CONFIG['chain_url'] + '/'}, {'payload': PAYLOAD}, 'pass', [200] * 3)
    print('PASS anchor 14: HTTPS base grammar and structurally built request paths', flush=True)


async def check_ids():
    for identifier in ('', '.', '..', ' ', 'a/b', 'a?b', 'a#b', 'a%b', 'é', 'a' * 129, None, 7, False,
                       'sample\n', 'sample|pass', '\ud800'):
        await expect({**CONFIG, 'provider_id': identifier}, {'payload': PAYLOAD}, 'invalid_input')
        await expect(CONFIG, {'payload': {**PAYLOAD, 'signal_id': identifier}}, 'invalid_envelope')
    for identifier in ('a', 'a' * 128, 'A.z_9:-', '...'):
        config = {**CONFIG, 'provider_id': identifier}
        paths = [PATHS[0], '/network/v1/provider/signals/' + identifier,
                 '/network/v1/anchor/signals/signal/sample/' + identifier]
        await expect(config, {'payload': PAYLOAD}, 'pass', [200] * 3, paths=paths)
        payload = {**PAYLOAD, 'signal_id': identifier}
        replies = responses()
        replies[2] = (200, json.dumps({'vetoed': False, 'segment_hash': hashlib.sha256(
            canonical(payload).encode('utf-8')).hexdigest()}), 'application/json')
        paths = [PATHS[0], PATHS[1], '/network/v1/anchor/signals/signal/' + identifier + '/provider-placeholder']
        await expect(CONFIG, {'payload': payload}, 'pass', [200] * 3, replies, paths=paths)
    print('PASS anchor 15: identifier grammar, length boundaries and path containment', flush=True)


async def check_duplicates(directory):
    for index, body in [(0, '{"vetoed":true,"vetoed":false}'),
                        (1, '{"vetoed":false,"provider_address":"wrong","provider_address":"address-placeholder"}'),
                        (2, '{"vetoed":false,"segment_hash":"a","segment_hash":"b"}'),
                        (0, '{"vetoed":false,"extra":{"a":1,"a":2}}')]:
        replies = responses()
        replies[index] = (200, body, 'application/json')
        await expect(CONFIG, {'payload': PAYLOAD}, 'chain_unavailable', [200] * (index + 1), replies)
    config = json.dumps(CONFIG)[:-1] + ',"provider_id":"provider-placeholder"}'
    await run_command(directory, config, {'payload': PAYLOAD}, 'invalid_input', signal_id=None)
    await run_command(directory, CONFIG, '{"payload":{"signal_id":"other","signal_id":"sample"}}',
                      'invalid_input', signal_id=None)
    config = json.dumps(CONFIG)[:-1] + ',"extra":{"a":1,"a":2}}'
    await run_command(directory, config, {'payload': PAYLOAD}, 'invalid_input', signal_id=None)
    print('PASS anchor 16: duplicate chain, envelope and config keys, including nested keys', flush=True)


async def check_output(directory):
    for identifier in ('sample\nVERIFY | innocent | pass\n', 'sample|pass', '\x1b[2J', '.', '..'):
        await run_command(directory, CONFIG, {'payload': {**PAYLOAD, 'signal_id': identifier, 'bad': 1.5}},
                          'invalid_envelope', signal_id=None)
    print('PASS anchor 17: forged VERIFY lines suppressed and result emitted once', flush=True)


async def check_timeouts():
    for seconds in (0, -1, 61, True, False, None, '10', 'nan', float('nan'), float('inf'), -float('inf'), [], {}, 10 ** 400):
        await expect({**CONFIG, 'request_timeout_seconds': seconds}, {'payload': PAYLOAD}, 'invalid_input')
    for seconds in (0.01, 60):
        await expect({**CONFIG, 'request_timeout_seconds': seconds}, {'payload': PAYLOAD}, 'pass', [200] * 3, seconds=seconds)
    config = dict(CONFIG)
    config.pop('request_timeout_seconds')
    await expect(config, {'payload': PAYLOAD}, 'pass', [200] * 3, seconds=10)
    print('PASS anchor 18: finite numeric timeout bounds and default', flush=True)


async def check_recursion(directory):
    deep = '[' * 20000 + '0' + ']' * 20000
    await run_command(directory, CONFIG, deep, 'invalid_input', signal_id=None)
    await run_command(directory, deep, {'payload': PAYLOAD}, 'invalid_input', signal_id=None)
    cycle = []
    cycle.append(cycle)
    await expect(CONFIG, {'payload': {**PAYLOAD, 'deep': cycle}}, 'invalid_input')
    replies = responses()
    replies[0] = (200, deep, 'application/json')
    await expect(CONFIG, {'payload': PAYLOAD}, 'chain_unavailable', [200], replies)
    print('PASS anchor 19: input, payload and chain recursion mapped into protocol', flush=True)


async def check_status_bodies():
    for index, kind in enumerate(('charter', 'provider', 'anchor')):
        for status in (201, 204, 301, 302, 307, 308, 400, 404, 429, 500, 503, 599, 600, 700):
            replies = responses()
            replies[index] = (status, 'not JSON', 'text/plain')
            reason = 'chain_unavailable' if status == 429 or status >= 500 else kind + '_missing'
            await expect(CONFIG, {'payload': PAYLOAD}, reason, [200] * index + [status], replies)
        for body, mime in [('{"vetoed":false}', 'text/plain'), ('bad', 'application/json'),
                           ('', 'application/json'), ('[]', 'application/json'), ('null', 'application/json'),
                           ('false', 'application/json'), ('1', 'application/json'), ('"text"', 'application/json')]:
            replies = responses()
            replies[index] = (200, body, mime)
            await expect(CONFIG, {'payload': PAYLOAD}, 'chain_unavailable', [200] * (index + 1), replies)
        for body in ({}, {'vetoed': None}, {'vetoed': 0}, {'vetoed': 'false'}, {'vetoed': []}):
            replies = responses()
            replies[index] = (200, json.dumps(body), 'application/json')
            await expect(CONFIG, {'payload': PAYLOAD}, kind + '_vetoed', [200] * (index + 1), replies)
    print('PASS anchor 20: status precedence, 600+, MIME, bad bodies and strict vetoes', flush=True)


async def check_input_order(directory):
    for envelope in (None, [], {}, {'payload': None}, {'payload': {}}, {'payload': {'signal_id': False}}):
        await expect(CONFIG, envelope, 'invalid_envelope')
        await run_command(directory, CONFIG, envelope, 'invalid_envelope', signal_id=None)
    print('PASS anchor 21: invalid_envelope shapes and missing identity', flush=True)
    for config in (None, [], {}, {**CONFIG, 'provider_id': ''}, {**CONFIG, 'request_timeout_seconds': 'bad'}):
        for envelope in ({'payload': {**PAYLOAD, 'bad': 1.5}}, {}, None):
            result = await expect(config, envelope, 'invalid_input')
            assert result['signal_id'] is None
        await run_command(directory, config, {'payload': PAYLOAD}, 'invalid_input', signal_id=None)
    for config, envelope in (('{', {'payload': PAYLOAD}), (CONFIG, '{')):
        await run_command(directory, config, envelope, 'invalid_input', signal_id=None)
    output = io.StringIO()
    config_path = directory / 'config.json'
    config_path.write_text('{}')
    with (redirect_stdout(output), patch.object(Path, 'read_text', return_value='{}') as read):
        code = await command(SimpleNamespace(config=config_path, envelope=directory / 'missing.json'))
    assert code == 1 and read.call_count == 1
    with redirect_stdout(output):
        code = await command(SimpleNamespace(config=directory / 'missing.json', envelope=directory / 'missing.json'))
    assert code == 1 and output.getvalue().splitlines()[-1] == 'VERIFY | - | invalid_input'
    print('PASS anchor 22: invalid_input, unreadable files and config-before-envelope precedence', flush=True)


async def main():
    await check_transport()
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        await check_cases(directory)
        await check_canonical()
        await check_addresses()
        await check_urls()
        await check_ids()
        await check_duplicates(directory)
        await check_output(directory)
        await check_timeouts()
        await check_recursion(directory)
        await check_status_bodies()
        await check_input_order(directory)


if __name__ == '__main__':
    asyncio.run(main())
