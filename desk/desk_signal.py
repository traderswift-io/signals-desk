import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .signer_delegate_client import resolve_symbol


def decimal_number(value, name):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'invalid_{name}') from None
    if not number.is_finite():
        raise ValueError(f'invalid_{name}')
    return number


def parse_signal(envelope, meta, now_ms):
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    if not isinstance(envelope, dict) or envelope.get('version', 1) != 1:
        raise ValueError('invalid_envelope_version')
    payload = envelope.get('payload')
    if not isinstance(payload, dict):
        raise ValueError('missing_payload')
    for name in ('signal_id', 'publisher'):
        if not isinstance(payload.get(name), str) or not payload[name].strip():
            raise ValueError(f'missing_{name}')
    if payload.get('kind') == 'cancel':
        return dict(payload)
    if payload.get('kind', 'signal') != 'signal':
        raise ValueError('invalid_kind')
    if not isinstance(payload.get('invalidation'), str) or not payload['invalidation'].strip():
        raise ValueError('missing_invalidation')
    levels = payload.get('levels')
    if not isinstance(levels, dict):
        raise ValueError('missing_levels')
    for name in ('entry', 'stop'):
        if levels.get(name) is None:
            raise ValueError(f'null_{name}: not sized in the demo')
        if decimal_number(levels[name], name) <= 0:
            raise ValueError(f'invalid_{name}')
    if levels.get('target') is not None and decimal_number(levels['target'], 'target') <= 0:
        raise ValueError('invalid_target')
    if payload.get('confidence') is None:
        raise ValueError('missing_confidence')
    if not 0 <= decimal_number(payload['confidence'], 'confidence') <= 1:
        raise ValueError('invalid_confidence')
    exit_by = payload.get('exit_by')
    if exit_by is not None:
        published_at = payload.get('published_at')
        if type(exit_by) is not int or type(published_at) is not int or exit_by <= published_at:
            raise ValueError('invalid_exit_by')
        if exit_by <= now_ms:
            raise ValueError('exit_by_passed')
    reply_by = payload.get('reply_by')
    if not isinstance(reply_by, int) or isinstance(reply_by, bool):
        raise ValueError('missing_or_invalid_reply_by')
    if reply_by <= now_ms:
        raise ValueError('reply_by_expired')
    if payload.get('venue_hint') != 'hyperliquid':
        raise ValueError('venue_not_hyperliquid')
    if payload.get('side') not in ('long', 'short'):
        raise ValueError('invalid_side')
    symbol = payload.get('venue_symbol') or payload.get('symbol')
    if not isinstance(symbol, str) or not symbol:
        raise ValueError('missing_venue_symbol')
    asset = resolve_symbol(symbol, meta)
    return {**payload, 'venue_symbol': asset['name'], 'asset': asset['name'], 'asset_index': asset['asset_index']}


def load_signal(path, meta, now_ms):
    return parse_signal(Path(path).read_text(), meta, now_ms)
