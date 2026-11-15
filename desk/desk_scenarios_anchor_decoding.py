import asyncio
import json
import tempfile
from pathlib import Path

from .desk_scenarios_anchor import CONFIG, PAYLOAD, responses, run_command


def with_extra(body, extra):
    return body[:-1] + ',"extra":' + extra + '}'


async def check_charsets(directory):
    for index in range(3):
        for charset in ('base64_codec', 'hex_codec', 'rot_13'):
            replies = responses()
            replies[index] = (200, replies[index][1], 'application/json; charset=' + charset)
            await run_command(directory, CONFIG, {'payload': PAYLOAD}, 'chain_unavailable',
                              [200] * (index + 1), replies=replies)
        replies = responses()
        replies[index] = (200, b'{"vetoed":false,"extra":"\xff"}', 'application/json; charset=utf-8')
        await run_command(directory, CONFIG, {'payload': PAYLOAD}, 'chain_unavailable',
                          [200] * (index + 1), replies=replies)
    print('PASS anchor 23: real aiohttp charset and byte decoding failures preserve CLI protocol', flush=True)


async def check_input_constants(directory):
    envelope = json.dumps({'payload': PAYLOAD})
    for constant in ('NaN', 'Infinity', '-Infinity'):
        for value in (constant, '[' + constant + ']', '{"nested":[{"value":' + constant + '}]}'):
            for config in (value, with_extra(json.dumps(CONFIG), value)):
                await run_command(directory, config, envelope, 'invalid_input', signal_id=None)
            for body in (value, with_extra(envelope, value),
                         '{"payload":' + with_extra(json.dumps(PAYLOAD), value) + '}'):
                await run_command(directory, CONFIG, body, 'invalid_input', signal_id=None)
    print('PASS anchor 24: non-JSON constants rejected in config and envelope at all nesting positions', flush=True)


async def check_chain_constants(directory):
    for index in range(3):
        for constant in ('NaN', 'Infinity', '-Infinity'):
            for value in (constant, '[' + constant + ']', '{"nested":[{"value":' + constant + '}]}'):
                for body in (value, with_extra(responses()[index][1], value)):
                    replies = responses()
                    replies[index] = (200, body, 'application/json')
                    await run_command(directory, CONFIG, {'payload': PAYLOAD}, 'chain_unavailable',
                                      [200] * (index + 1), replies=replies)
    print('PASS anchor 25: non-JSON constants rejected at every chain read and nesting position', flush=True)


async def check_chain_unicode(directory):
    for index in range(3):
        for surrogate in ('\ud800', '\udbff', '\udc00', '\udfff'):
            for extra in (surrogate, [surrogate], {'nested': [surrogate]},
                          {surrogate: 'value'}, [{'nested': {surrogate: 1}}]):
                replies = responses()
                body = {**json.loads(replies[index][1]), 'extra': extra}
                replies[index] = (200, json.dumps(body), 'application/json')
                await run_command(directory, CONFIG, {'payload': PAYLOAD}, 'chain_unavailable',
                                  [200] * (index + 1), replies=replies)
            replies = responses()
            body = {**json.loads(replies[index][1]), surrogate: 'value'}
            replies[index] = (200, json.dumps(body), 'application/json')
            await run_command(directory, CONFIG, {'payload': PAYLOAD}, 'chain_unavailable',
                              [200] * (index + 1), replies=replies)
    replies = responses()
    for index, (_, body, mime) in enumerate(replies):
        extra = {'\U0001f600': ['é', {'value': '\U00010000'}], 'numbers': [0.7, 1.0, 9007199254740992]}
        replies[index] = (200, with_extra(body, json.dumps(extra)), mime)
    await run_command(directory, CONFIG, {'payload': PAYLOAD}, 'pass', [200] * 3, replies=replies)
    print('PASS anchor 26: recursive chain Unicode validation accepts scalar strings and unrestricted JSON numbers', flush=True)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        await check_charsets(directory)
        await check_input_constants(directory)
        await check_chain_constants(directory)
        await check_chain_unicode(directory)


if __name__ == '__main__':
    asyncio.run(main())
