from decimal import Decimal, InvalidOperation


def positive(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except InvalidOperation:
        return None


def evaluate_intent(intent, policy, asset_index_by_name, size_spent_today, now_ms, reply_by=None):
    if intent['kind'] == 'bracket':
        intent = {**intent, **intent['entry']}
    now = now_ms // 1000
    if now >= intent['expires_at']:
        return 'intent_expired'
    if intent['expires_at'] > now + 120:
        return 'intent_expiry_too_far'
    if intent['venue'] != 'hyperliquid' or 'hyperliquid' not in policy['venues']:
        return 'venue_not_allowed'
    if intent['vault_address'] != policy['vault_address']:
        return 'vault_mismatch'
    ended = min(policy['revoked_at'], policy['expires_at']) if policy.get('revoked') else policy['expires_at']
    if now >= ended + 600:
        return 'policy_closed'
    if intent['kind'] == 'cancel':
        return None
    if intent['kind'] == 'schedule_cancel':
        scheduled = intent.get('time_ms')
        if scheduled is None:
            return 'schedule_cancel_time_missing'
        if not (now + 15) * 1000 <= scheduled <= (now + 600) * 1000:
            return 'schedule_cancel_time_out_of_range'
        if scheduled > (ended + 600) * 1000:
            return 'schedule_cancel_past_close_out_deadline'
        return None
    if policy.get('revoked'):
        return 'policy_revoked'
    if now >= policy['expires_at']:
        return 'policy_expired'
    if intent['policy_epoch'] != policy['epoch']:
        return 'epoch_mismatch_policy_is_' + str(policy['epoch'])
    asset = intent.get('asset')
    if intent.get('asset_index') is None or asset_index_by_name.get(asset) != intent['asset_index']:
        return 'asset_index_does_not_match_asset'
    cap = next((row for row in policy['asset_caps'] if row['asset'] == asset), None)
    if cap is None:
        return 'asset_not_allowed'
    if intent.get('reduce_only'):
        return None
    if policy.get('chain_stale', False):
        return 'chain_stale_new_risk_stopped'
    if policy['expires_at'] - now < 86400:
        return 'policy_expires_within_24h'
    if reply_by is None or reply_by <= now_ms:
        return 'reply_by_expired'
    if positive(intent.get('limit_px')) is None:
        return 'bad_limit_px'
    size = positive(intent.get('size'))
    if size is None:
        return 'bad_size'
    order_cap, daily_cap = positive(cap['max_order_size']), positive(cap['max_daily_size'])
    if order_cap is None or daily_cap is None:
        return 'bad_policy_cap'
    if size > order_cap:
        return 'over_max_order_size'
    if size + Decimal(str(size_spent_today)) > daily_cap:
        return 'over_max_daily_size'
    return None
