import ast
import copy
import io
import json
import re
import subprocess
import tempfile
import tokenize
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .desk_executor import create_runtime, submit
from .desk_io import load_config, order_fields
from .desk_keygen import generate_key
from .desk_position_sizer import asset_metadata, size_position
from .desk_record import append_record, now_ms, read_records
from .desk_risk_gate import evaluate_intent
from .desk_scenarios_fakes import configuration, info, meta, signal_envelope, submit_verified_fixture
from .desk_signal import parse_signal
from .desk_ticket import main as ticket_main
from .signer_delegate_client import cancel_fields, combine_meta, resolve_symbol


SIGNALS_PROMPTS = {
    'signals-desk-lead': 'signals-desk-intake',
    'signals-position-sizer': 'signals-position-sizing',
    'signals-risk-gate': 'signals-risk-gate',
    'signals-executor': 'signals-execution',
    'signals-reviewer': 'signals-review',
}

INTAKE_ALLOWED_CALLS = {
    'GET /v1/signals',
    'GET /network/v1/charter/signals',
    'GET /network/v1/provider/signals/<provider_id>',
    'GET /network/v1/anchor/signals/signal/<signal_id>/<provider_id>',
}


def listed_commands(text):
    return {re.sub(r'^(GET )https://chain\.traderswift\.io(?=/network/v1/)', r'\1', command)
            for command in re.findall(r'^- `([^`]+)`', text, re.MULTILINE)}


def check_prompt_front_matter():
    root = Path(__file__).parent.parent
    prompt_sections = ['## Bot profile', '## System prompt', '### What you own',
                       '### The pre-send checklist (all must be true)', '### How you work',
                       '### Boundaries', '### Report format', '### Requests you will see']
    skill_sections = ['## Inputs', '## Pre-send checklist', '## Send', '## Unknown results',
                      '## Reconciliation', '## Cancels, modifies, leverage, closes',
                      '## Rehearsal rule', '## Report', '## Never']
    for name, skill in SIGNALS_PROMPTS.items():
        path = root / 'agents' / (name + '.md')
        text = path.read_text()
        writer = 'true' if name == 'signals-executor' else 'false'
        pattern = (r'---\nname: ' + re.escape(name) + r'\ntitle: [^\n]+\ndescription: [^\n]+\nseat: floor\n'
                   r'skills:\n  - ' + re.escape(skill) + r'\nwrites_to_exchange: ' + writer + r'\n---\n')
        assert re.match(pattern, text), path
        assert re.findall(r'^#{2,3} .+$', text, re.MULTILINE) == prompt_sections, path
        skill_path = root / 'skills' / skill / 'SKILL.md'
        skill_text = skill_path.read_text()
        assert listed_commands(text) == listed_commands(skill_text), name
        assert skill_text.startswith('---\nname: ' + skill + '\n'), skill_path
        metadata, separator, body = skill_text[4:].partition('\n---\n')
        assert separator and '\nlicense: MIT\nmetadata:\n' in metadata, skill_path
        assert '\n  author: Signals Desk\n' in metadata, skill_path
        assert 'based on HyperGrok Trading Desk' in metadata and 'ATTRIBUTION.md' in metadata, skill_path
        assert re.findall(r'^## .+$', body, re.MULTILINE) == skill_sections, skill_path


def must_refuse(function, expected):
    try:
        function()
    except ValueError as error:
        assert expected in str(error), str(error)
        return str(error)
    raise AssertionError('Expected refusal: ' + expected)


def check_intake():
    root = Path(__file__).parent.parent
    prompt = (root / 'agents' / 'signals-desk-lead.md').read_text()
    intake = (root / 'skills' / 'signals-desk-intake' / 'SKILL.md').read_text()
    delegation = 'The loaded `signals-desk-intake` skill defines the intake requirements.'

    def check_pair(common, skill):
        assert delegation in common
        assert 'before ticket posting, routing or downstream action' in common
        assert 'Missing, ambiguous or failed required verification blocks processing.' in common
        assert 'trusts the served publication' not in common and 'no hub verification' not in common
        expected = INTAKE_ALLOWED_CALLS | {'python3 -m desk.desk_wake desk/config.json',
                                         'python3 -m desk.desk_anchor_verify desk/config.json <envelope.json>'}
        assert listed_commands(skill) == listed_commands(common) == expected
        assert 'trusts the served publication' not in skill and 'no hub verification' not in skill

    check_pair(prompt, intake)
    broken_prompts = [prompt.replace(delegation, 'This desk trusts the served publication; no hub verification.'),
                      prompt.replace('- `GET /v1/signals`\n', ''),
                      prompt + '\n- `POST /v1/orders`\n',
                      prompt + '\n- `POST /v1/deliveries`\n',
                      prompt + '\n- `POST /v1/deliveries {signal_id, event_id}`\n',
                      prompt + '\n- `DELETE /v1/signals`\n']
    chain_calls = re.findall(r'^- `(GET https://[^`]+)`', prompt, re.MULTILINE)
    assert len(chain_calls) == 3
    broken_skills = [intake + '\n- `POST /v1/orders`\n',
                     intake + '\n- `GET <hub-read-endpoint>`\n',
                     intake + '\n- `DELETE /v1/signals`\n']
    for command in ('python3 -m desk.desk_wake desk/config.json',
                    'python3 -m desk.desk_anchor_verify desk/config.json <envelope.json>'):
        for replacement in ('', '- `python3 -m desk.unknown desk/config.json`\n'):
            broken_prompts.append(prompt.replace('- `' + command + '`\n', replacement))
            broken_skills.append(intake.replace('- `' + command + '`\n', replacement))
    broken_prompts.append(prompt + '\n- `python3 -m desk.unknown desk/config.json`\n')
    broken_skills.append(intake + '\n- `python3 -m desk.unknown desk/config.json`\n')
    for call in chain_calls:
        broken_prompts.append(prompt.replace('- `' + call + '`\n', ''))
        broken_skills.append(intake.replace('- `' + call + '`\n', ''))
        broken_skills.append(intake.replace(call, call.replace('GET ', 'POST ')))
        broken_skills.append(intake.replace(call, call.replace('https://', 'https://unknown.')))
        broken_skills.append(intake.replace(call, call + '/unknown'))
    for common, skill in ([(broken, intake) for broken in broken_prompts] +
                          [(prompt, broken) for broken in broken_skills]):
        try:
            check_pair(common, skill)
        except AssertionError:
            continue
        raise AssertionError('Conflicting intake prompt accepted')
    for operation in ('POST /v1/orders', 'POST /v1/deliveries', 'POST /v1/deliveries {signal_id, event_id}', 'PUT /v1/signals'):
        try:
            check_pair(prompt, intake + '\n- `' + operation + '`\n')
        except AssertionError:
            continue
        raise AssertionError('Conflicting intake skill accepted')


def check_sizer():
    envelope = signal_envelope()
    signal = parse_signal(envelope, meta(), now_ms())
    asset = asset_metadata(meta(), 0)
    sized = size_position(signal, '1000', '100000', asset, '0.01', '5')
    assert sized['size'] == Decimal('0.01000') and str(sized['size']) == '0.01000'
    assert sized['notional'] == 1000
    for field in ('entry', 'stop'):
        broken = copy.deepcopy(envelope)
        broken['payload']['levels'][field] = None
        must_refuse(lambda: parse_signal(broken, meta(), now_ms()), 'not sized in the demo')
    wrong_stop = copy.deepcopy(signal)
    wrong_stop['levels']['stop'] = '101000'
    must_refuse(lambda: size_position(wrong_stop, '1000', '100000', asset, '0.01', '5'), 'stop_on_losing_side')
    reason = must_refuse(lambda: size_position(signal, '1000', '100000', asset, '0.1', '5'), 'leverage_cap=5')
    assert all(name + '=' in reason for name in ('equity', 'entry', 'stop', 'distance', 'notional', 'size', 'limit'))
    for field in ('invalidation', 'confidence', 'reply_by', 'venue_symbol'):
        broken = copy.deepcopy(envelope)
        broken['payload'].pop(field)
        if field == 'venue_symbol':
            broken['payload'].pop('symbol')
        must_refuse(lambda: parse_signal(broken, meta(), now_ms()), field)
    broken_meta = meta()
    broken_meta['universe'].append(dict(broken_meta['universe'][0]))
    must_refuse(lambda: parse_signal(envelope, broken_meta, now_ms()), 'symbol_ambiguous')
    must_refuse(lambda: parse_signal(signal_envelope(asset='UNKNOWN'), meta(), now_ms()), 'symbol_unmapped')
    tight = copy.deepcopy(signal)
    tight['levels']['stop'] = '98500'
    must_refuse(lambda: size_position(tight, '1000', '100000', asset, '0.6', '40'), 'liquidation_room')
    short = copy.deepcopy(signal)
    short['side'], short['levels']['stop'] = 'short', '101000'
    assert size_position(short, '1000', '100000', asset, '0.01', '5')['liquidation_price'] > Decimal('101000')
    must_refuse(lambda: size_position(signal, '1', '100000', asset, '0.01', '5'), 'minimum_notional=10')


def check_symbol_mapping():
    combined = combine_meta(meta(), {'xyz': meta('xyz')}, [None, {'name': 'other'}, {'name': 'xyz'}])
    for symbol, canonical, asset_index in [('XYZ:TSLA', 'xyz:TSLA', 120001), ('KPEPE', 'kPEPE', 2)]:
        envelope = signal_envelope(asset=symbol)
        resolved = parse_signal(envelope, combined, now_ms())
        assert resolved['venue_symbol'] == resolved['asset'] == canonical and resolved['asset_index'] == asset_index
        envelope['payload'].pop('venue_symbol')
        assert parse_signal(envelope, combined, now_ms()) == {**resolved, 'venue_symbol': canonical}
        for intent in [order_fields(resolved, '0.01', '100000'), cancel_fields(resolved, 42)]:
            assert intent['asset'] == canonical and intent['asset_index'] == asset_index
    ambiguous = copy.deepcopy(combined)
    ambiguous['universe'].append({'name': 'KpEpE', 'asset_index': 120002})
    must_refuse(lambda: resolve_symbol('KPEPE', ambiguous), 'symbol_ambiguous')
    must_refuse(lambda: resolve_symbol('UNKNOWN', combined), 'symbol_unmapped')
    must_refuse(lambda: combine_meta(meta(), {'xyz': meta('xyz')}, [None]), 'builder_dex_unmapped')


def check_gate():
    timestamp = now_ms()
    config = configuration(Path('.'), '<fake>')
    policy = config['policy']
    signal = parse_signal(signal_envelope(), meta(), timestamp)
    intent = {**order_fields(signal, '0.01', '100000'), 'policy_epoch': 1, 'expires_at': timestamp // 1000 + 120,
              'venue': 'hyperliquid', 'vault_address': policy['vault_address']}
    indices = {'BTC': 0, 'ETH': 1}

    def gate(changes=None, current_policy=None, spent='0', reply=None):
        return evaluate_intent({**intent, **(changes or {})}, current_policy or policy, indices, Decimal(spent), timestamp,
                               signal['reply_by'] if reply is None else reply)

    assert gate() is None
    assert gate({'asset': 'ETH', 'asset_index': 1}) == 'asset_not_allowed'
    assert gate({'asset_index': 1}) == 'asset_index_does_not_match_asset'
    near = {**policy, 'expires_at': timestamp // 1000 + 60}
    assert gate(current_policy=near) == 'policy_expires_within_24h'
    assert gate({'reduce_only': True, 'size': '100'}, near) is None
    assert gate({'size': '0.02001'}) == 'over_max_order_size'
    assert gate(spent='0.025') == 'over_max_daily_size'
    assert gate(reply=timestamp - 1) == 'reply_by_expired'
    assert gate({'policy_epoch': 2, 'reduce_only': True}) == 'epoch_mismatch_policy_is_1'
    assert gate({'reduce_only': True}, {**policy, 'expires_at': timestamp // 1000}) == 'policy_expired'
    assert gate({'kind': 'cancel', 'policy_epoch': 2}, {**policy, 'expires_at': timestamp // 1000}) is None
    assert gate({'reduce_only': True}, {**policy, 'revoked': True, 'revoked_at': timestamp // 1000}) == 'policy_revoked'
    assert gate({'vault_address': 'wrong'}) == 'vault_mismatch'


async def check_ticket_local():
    with tempfile.TemporaryDirectory() as directory:
        config = configuration(Path(directory), '<fake>')
        config_path = Path(directory) / 'config.json'
        config_path.write_text(json.dumps(config))
        loaded = load_config(config_path)
        assert loaded['resident'] is True and loaded['stop_mode'] == 'software'
        config['resident'] = False
        generate_key(config)
        runtime = create_runtime({**config, 'resident': False}, None)
        with (patch('desk.desk_executor.refresh_meta', AsyncMock(return_value=meta())),
              patch('desk.desk_executor.run_round', AsyncMock()) as coordinator,
              patch('desk.desk_executor.entry_candle_guard', AsyncMock(return_value=None)),
              patch('desk.desk_executor.entry_preview', AsyncMock(side_effect=ValueError('trigger_price_outside_band'))),
              patch('desk.desk_executor.arm', AsyncMock()) as watcher):
            for stop_mode in ('software', 'trigger'):
                runtime['config']['stop_mode'] = stop_mode
                result = await submit_verified_fixture(runtime, signal_envelope(stop_mode))
                assert result == {'refused': 'trigger_price_outside_band'}
            coordinator.assert_not_awaited()
            watcher.assert_not_awaited()
        rows = read_records(config['record_path'])
        recorded = next(row for row in rows if row['event'] == 'review')
        for identifier, expected in (('software', recorded), ('missing', {'refused': 'unknown_signal_id'})):
            output = io.StringIO()
            with redirect_stdout(output):
                await ticket_main(SimpleNamespace(config=str(config_path), action='review', input=identifier))
            assert json.loads(output.getvalue()) == expected
            assert read_records(config['record_path']) == rows
        for changes, expected in (({'resident': 'false'}, 'invalid_resident'), ({'stop_mode': 'other'}, 'invalid_stop_mode')):
            config_path.write_text(json.dumps({**config, **changes}))
            must_refuse(lambda: load_config(config_path), expected)


async def check_ticket_preview_local():
    with tempfile.TemporaryDirectory() as directory:
        config = configuration(Path(directory), '<fake>')
        config['policy']['asset_caps'][0]['max_order_size'] = '0.001'
        config_path = Path(directory) / 'config.json'
        config_path.write_text(json.dumps(config))
        generate_key(config)
        runtime = create_runtime(config, None)
        config = runtime['config']
        config_path.write_text(json.dumps(config))
        Path(config['delegate_key_file']).unlink()
        fake = {'events': [], 'position': Decimal(0)}

        async def read_fake(runtime, kind, **fields):
            request = SimpleNamespace(app={'state': fake}, json=AsyncMock(return_value={'type': kind, **fields}))
            return json.loads((await info(request)).body)

        with patch('desk.signer_delegate_client.read_info', read_fake), patch('desk.desk_executor.run_round', AsyncMock()) as coordinator:
            for case, side, stop in (('long', 'long', '96000'), ('short', 'short', '98200'), ('budget', 'long', '96000')):
                if case == 'budget':
                    config['policy']['asset_caps'][0].update(max_order_size='0.02', max_daily_size='0.015')
                    append_record(config['record_path'], 'intent', intent={
                        'request_id': 'spent', 'kind': 'order', 'reduce_only': False, 'asset': 'BTC',
                        'delegate_key': config['delegate_public_key'], 'key_ref': config['key_ref'], 'size': '0.01',
                        'asset_index': 0, 'is_buy': True, 'limit_px': '100000', 'tif': 'Ioc'})
                    append_record(config['record_path'], 'reply', request_id='spent', signed=True, ambiguous=True)
                envelope = signal_envelope(case)
                envelope['payload']['side'] = side
                envelope['payload']['levels'] = {'entry': '97123.456', 'stop': stop, 'target': None}
                envelope_path = Path(directory) / 'envelope.json'
                envelope_path.write_text(json.dumps(envelope))
                outputs = {}
                before = read_records(config['record_path'])
                for action in ('size', 'gate'):
                    reader_config = dict(config)
                    if action == 'size':
                        reader_config.pop('delegate_public_key')
                    config_path.write_text(json.dumps(reader_config))
                    output = io.StringIO()
                    with (redirect_stdout(output), patch('desk.signer_delegate_client.signing_key',
                          side_effect=AssertionError('Read-only command accessed signing seed'))):
                        await ticket_main(SimpleNamespace(config=str(config_path), action=action, input=str(envelope_path)))
                    outputs[action] = json.loads(output.getvalue())
                    assert read_records(config['record_path']) == before
                coordinator.assert_not_awaited()
                result = await submit(runtime, envelope)
                recorded = [row for row in read_records(config['record_path']) if row['event'] == 'gate'][-1]
                assert outputs['size'] == outputs['gate']['sizing'] == recorded['sizing']
                assert outputs['gate']['refused'] == result['refused'] == recorded['reason']
                assert outputs['gate']['intent']['entry']['limit_px'] == recorded['entry_price']
                assert {**outputs['gate']['intent'], 'expires_at': recorded['intent']['expires_at']} == recorded['intent']
                if case == 'budget':
                    assert result['refused'] == 'over_max_daily_size'
            for public_key in (None, '', 'g' * 64, '00 ' * 21 + ' '):
                reader_config = {**config, 'delegate_public_key': public_key}
                if public_key is None:
                    reader_config.pop('delegate_public_key')
                config_path.write_text(json.dumps(reader_config))
                output = io.StringIO()
                with redirect_stdout(output):
                    await ticket_main(SimpleNamespace(config=str(config_path), action='gate', input=str(envelope_path)))
                assert json.loads(output.getvalue()) == {'refused': 'invalid_delegate_public_key'}
            coordinator.assert_not_awaited()


def check_tree_style():
    directory = Path(__file__).parent
    paths = list(directory.iterdir())
    paths.extend(directory.parent / 'agents' / (name + '.md') for name in SIGNALS_PROMPTS)
    for skill in SIGNALS_PROMPTS.values():
        paths.extend((directory.parent / 'skills' / skill).rglob('*'))
    for path in paths:
        if not path.is_file() or path.name == 'config.json':
            continue
        text = path.read_text()
        assert len(text.splitlines()) < 400, path.name
        if path.name == 'desk_ticket.py':
            assert len(text.splitlines()) < 150, path.name
        if path.suffix != '.py':
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.ClassDef), path.name
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or '']
                assert all(name != 'os' and not name.startswith('os.') for name in names) or (
                    isinstance(node, ast.ImportFrom) and node.module == 'os' and
                    {alias.name for alias in node.names} <= {
                        'desk_private_storage.py': {'O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'O_RDONLY',
                                                    'close', 'fchmod', 'fstat', 'geteuid', 'open'},
                        'desk_scenarios_journal.py': {'umask'}}.get(path.name, set())), path.name
                if path.name == 'signer_delegate_client.py':
                    assert all(not name.startswith('desk') for name in names), path.name
                    assert not isinstance(node, ast.ImportFrom) or node.level == 0, path.name
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
                assert ast.get_docstring(node) is None, path.name
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                assert path.name == 'signer_delegate_client.py' and token.string == (
                    '# Venue quirk: builder assets use 100000 + perpDexs slot * 10000 + local universe index.'), path.name
            if token.type == tokenize.STRING:
                assert token.string.lstrip('rfbuRFBU').startswith(chr(39)), (path.name, token.start)


def check_timer_audit():
    paths = sorted(path for path in Path(__file__).parent.glob('*.py') if not path.name.startswith('desk_scenarios'))
    result = subprocess.run(['grep', '-nE', r'call_later|call_at|sleep|wait_for|asyncio\.timeout',
                             *map(str, paths)], capture_output=True, text=True, check=False)
    assert result.returncode in (0, 1), result.stderr
    allowed = {('signer_delegate_client.py', 'coordinator_backoff', 'sleep'),
               ('desk_watcher.py', 'websocket_reconnect', 'wait_for')}
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
            if name not in ('call_later', 'call_at', 'sleep', 'wait_for', 'timeout', 'timeout_at', 'Timer'):
                continue
            parent = node
            while parent in parents and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = parents[parent]
            location = (path.name, getattr(parent, 'name', ''), name)
            assert location in allowed, (location, node.lineno)
            found.add(location)
    assert found == allowed, found
    assert len(result.stdout.splitlines()) == 2, result.stdout
    print('PASS timer audit: coordinator_backoff.sleep and websocket_reconnect.wait_for only', flush=True)
