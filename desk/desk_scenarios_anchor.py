import asyncio
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from .desk_anchor_verify import canonical, verify


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
PATHS = ['/network/v1/charter/signals', '/network/v1/provider/signals/provider-placeholder',
         '/network/v1/anchor/signals/signal/sample/provider-placeholder']


async def run_command(directory, config, envelope, reason, reads):
    config_path = directory / 'config.json'
    envelope_path = directory / 'envelope.json'
    config_path.write_text(json.dumps(config))
    envelope_path.write_text(json.dumps(envelope))
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-B', '-m', 'desk.desk_anchor_verify', str(config_path), str(envelope_path),
        cwd=Path(__file__).parent.parent, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, errors = await process.communicate()
    assert process.returncode == int(reason != 'pass'), (output.decode(), errors.decode())
    lines = output.decode().splitlines()
    assert len(lines) == 2, lines
    assert json.loads(lines[0]) == {'signal_id': 'sample', 'result': reason, 'reads': reads}, lines
    assert lines[1] == 'VERIFY | sample | ' + reason, lines


async def check_cases(directory, config, state):
    digest = hashlib.sha256(PAYLOAD_TEXT.encode('utf-8')).hexdigest()
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
        state['requests'] = []
        bodies = [{'vetoed': False}, {'vetoed': False, 'provider_address': config['provider_address']},
                  {'vetoed': False, 'segment_hash': digest}]
        state['responses'] = [(200, body) for body in bodies]
        if index is not None:
            state['responses'][index] = (status, {**bodies[index], **(changes or {})})
        envelope = {'version': 1, 'payload': copy.deepcopy(PAYLOAD), 'segment_hash': 'untrusted',
                    'receipt': {'segment_hash': 'untrusted'}, 'provider_id': 'untrusted'}
        if reason == 'hash_mismatch':
            envelope['payload']['thesis'] = 'B'
            envelope['segment_hash'] = digest
            envelope['receipt']['segment_hash'] = digest
        if reason == 'payload_not_canonical':
            envelope['payload']['confidence'] = 0.7
        await run_command(directory, config, envelope, reason, reads)
        assert state['requests'] == PATHS[:len(reads)], state['requests']
        print(f'PASS anchor {number}: {reason}, {len(reads)} reads', flush=True)


async def check_canonical(config, state):
    for payload, expected in JCS_FIXTURES:
        assert canonical(payload) == expected
        assert canonical(payload).encode('utf-8') == expected.encode('utf-8')
    assert canonical(PAYLOAD) == PAYLOAD_TEXT
    for invalid in (0.7, 9007199254740992, -9007199254740992, float('nan'), float('inf'),
                    '\ud800', {'\ud800': 1}, {1: 'invalid'}, (1,), {1}, b'bytes'):
        state['requests'] = []
        envelope = {'payload': {**PAYLOAD, 'invalid': invalid}}
        result = await verify(config, envelope)
        assert result == {'signal_id': 'sample', 'result': 'payload_not_canonical', 'reads': []}, result
        assert not state['requests']
    for failure in (aiohttp.ClientConnectionError(), TimeoutError()):
        state['requests'] = []
        with patch('aiohttp.ClientSession.get', side_effect=failure) as request:
            result = await verify(config, {'payload': PAYLOAD})
        assert result == {'signal_id': 'sample', 'result': 'chain_unavailable', 'reads': []}, result
        assert request.call_count == 1 and not state['requests']
    print('PASS anchor 12: JCS fixtures, UTF-16 order, canonical refusals and transport failures', flush=True)


async def main():
    state = {'requests': [], 'responses': []}

    async def handle(request):
        state['requests'].append(request.path)
        status, body = state['responses'][PATHS.index(request.path)]
        return web.json_response(body, status=status, headers={'Retry-After': '1'} if status == 429 else None)

    application = web.Application()
    for path in PATHS:
        application.router.add_get(path, handle)
    runner = web.AppRunner(application)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        config = {'chain_url': f'http://127.0.0.1:{port}', 'provider_id': 'provider-placeholder',
                  'provider_address': 'address-placeholder', 'request_timeout_seconds': 2}
        with tempfile.TemporaryDirectory() as directory:
            await check_cases(Path(directory), config, state)
        await check_canonical(config, state)
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
