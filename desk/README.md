# Signals Desk executor

This is the TraderSwift demo desk. The Signals service is private; obtain a subscriber token from the signal publisher. See [SETUP.md](../SETUP.md) for the five-bot setup, UTC routines and delegate-policy registration with Signer.

Based on HyperGrok Trading Desk. The existing [MIT license](../LICENSE) remains unchanged. This component adds policy-authorized execution through an ed25519 delegate, a position sizer, a local policy mirror, and software exits. It does not use a venue signing key.

Python 3.11 or newer on Linux is required, with `aiohttp`, `PyNaCl`, and `redis`. Scenario checks also require `redis-server` on the command search path.

## Commands

From the repository root, after supplying a private config JSON:

```sh
python3 -m desk.desk_keygen desk/config.json
python3 -m desk.desk_signal_consumer desk/config.json
python3 -m desk.desk_wake desk/config.json
python3 -m desk.desk_executor --config desk/config.json --close-ms <close_ms> wake
python3 -m desk.desk_executor --config desk/config.json --close-ms <close_ms> run signal.json
python3 -m desk.desk_executor --config desk/config.json --close-ms <close_ms> cancel SIGNAL_ID
python3 -m desk.desk_ticket --config desk/config.json size signal.json
python3 -m desk.desk_ticket --config desk/config.json --close-ms <close_ms> gate signal.json
python3 -m desk.desk_ticket --config desk/config.json review SIGNAL_ID
```

For routine commands, replace `<close_ms>` with the integer printed in the initial `desk_wake` JSON. Carry it unchanged through maintenance, gate and submission; resident commands do not require it. Cancel remains available without a close for withdrawals. Missing or outdated closes refuse `stale_wake_close`, including a boundary crossed during maintenance candle reads, prepare or oracle reads. Maintenance rechecks after the candle read before recording or dispatching a new invalidation cancellation. End a stale handoff and wait for a new wake.

The default executor config path is `desk/config.json`, so `python3 -m desk.desk_executor cancel SIGNAL_ID` also works. Key generation runs once on the executor machine, prints only the public key, creates the seed file with mode 600, and refuses to replace any existing destination. Protect the config, record directory, and local control socket directory with owner-only permissions. Config, key, record, and socket paths are operator-configured; relative paths resolve from the working directory.

The ticket CLI also defaults to `desk/config.json` and prints one JSON object. `size` prints the existing sizer's decimal strings or its refusal with the numbers. `gate` prints `status: PASS` with `sizing` and an unsigned `intent` preview, or `refused` with the same policy reason as the executor. Both use the executor's shared entry preview, including venue price rounding and nonresident bracket construction. These commands only read public Info and local files; they never prepare, send, or append records. They never load the delegate seed or need door credentials. Size needs no delegate identity. Gate requires `delegate_public_key` (the public hex printed by keygen), the issued `key_ref`, and reads the Executor's journal for the same daily budget. A missing journal counts as nothing signed today; Signer still enforces the daily cap; a missing or malformed public key refuses `invalid_delegate_public_key`. Read-only seat configs retain the example fields, with unused credential and seed fields left as placeholders. Keep the policy, public identity and journal current; a stale copy cannot establish current budget availability. A gate PASS describes the policy mirror; execution still checks candle eligibility, account occupancy and resident watcher readiness. `review` reads the recorded Reviewer object, including signal entry, fill, slippage and the recorded stop mode; it returns `unknown_signal_id` or `review_unavailable` when absent and never creates a review.

The consumer uses blocking stream reads and persists the last processed stream id. A Redis connection failure is recorded, cancels active tickets through the normal reduce-only closeout, and stops the consumer; restart resumes from the saved cursor. It holds an exclusive record lock. A cancel command uses the running executor's local control socket; when no executor is running, it acquires that lock and reconciles outstanding tickets before cancellation. Resident startup recovery closes outstanding exposure and never resends an entry. Routine startup reconciles existing brackets without closing protected positions or resending entries. Use a single configured record for a follower account and keep it across restarts.

Only the resident consumer sends `POST /v1/deliveries`, with the stream entry ID as `event_id`. Grok seats make no delivery report: the service records each signal returned by their GET for that desk's token. Assign one subscriber token to one desk. Set `signals_url`, `subscriber_token_file` (read once at startup), `signals_access_client_id` and `signals_access_client_secret` for resident delivery reports. Reporting is off when the `signals_url` key is absent. If the URL is set but the token file is unreadable, empty or multi-line, a sanitized `deliveries_disabled` reason is recorded and reporting stays off; recovery and trading continue. Invalid URLs or missing delivery credentials have the same behavior. Fix the configuration and restart to enable reporting; entries consumed while reporting is disabled are not queued.

The consumer waits only for the queued row to be fsynced in the delivery file before writing its cursor; it never waits for a POST. Delivery rows go to `<record_path>.deliveries.jsonl` with its own file lock. Delivery writes run through `asyncio.to_thread` and never hold the trading journal lock. Pending state is reconstructed once at startup from the delivery file alone and maintained in memory; there is no migration from trading journal rows. Existing trading code and its synchronous journal I/O are unchanged.

Startup opens or creates the delivery store under its exclusive lock, fsyncs the file and its parent directory, and only then enables reporting. This directory sync also runs on existing files so a failed earlier creation sync can be retried. Later appends require the file to exist; they never recreate it silently. Any future replacement must fsync the parent directory before a cursor can depend on the replacement. With reporting enabled, failure to persist a queued row prevents cursor advancement.

Under that same startup lock, a final record without a terminating newline or with unparseable JSON is copied before truncating to the preceding complete newline-terminated record. The removed bytes are appended verbatim to `<record_path>.deliveries.jsonl.torn` with mode 0600; the copy and its parent directory are fsynced before truncation. The directory sync runs even for an existing copy, covering a failed earlier creation sync. If copying or either sync fails, the delivery file stays intact and reporting is disabled. The truncation is fsynced, then one `delivery_tail_truncated` row with the `removed_bytes` count is appended and fsynced. Complete queued identities survive. Corrupt interior records are never skipped or repaired; reporting stays off, while recovery and trading continue. A corrupt or unavailable store records `deliveries_disabled` through the trading journal's diagnostic path, with reason `delivery_store_corrupt` or `delivery_store_unavailable` and only the exception type, without exception text. A general guard covers all delivery preparation, including the delivery-state read; unexpected exceptions disable reporting with only their type as the reason and still allow recovery. If that diagnostic cannot be persisted, a fixed loop diagnostic is emitted and delivery initialization still returns to trading recovery. Other delivery diagnostics stay in the delivery file. Fix the store and restart to enable reporting; as with invalid configuration, entries consumed while reporting is disabled advance the trading cursor normally and are not queued for later reports.

Retain and restore the trading journal and delivery file together. Retain any `.torn` copy for inspection; it is not replayed as delivery state. A failed repair may append the same tail again on restart.

One coalescing worker runs a sweep after startup recovery and on stream entries, including malformed entries. Each sweep retries at most ten eligible pending reports, oldest first by first insertion, plus the latest fresh report if present. Entries arriving during a sweep request another sweep; intermediate fresh entries remain in the pending backlog. There is no retry timer: future deadlines and idle backlogs wait for another stream entry or restart. A 429 stops the sweep immediately and persists a subscriber-wide cooldown, leaving fresh reports queued. Retry-After eligibility and deadlines use the local wall clock, never stream timestamps. All failures back off: transport errors, timeouts, non-2xx responses, bad bodies (including decoding exceptions and missing acknowledgement), and 429 use wall-clock delays starting at 5 seconds and doubling up to 15 minutes; Retry-After can extend that delay. After 20 failed attempts, `delivery_abandoned` is terminal across restarts and replays. Each completed attempt writes one outcome row, without a `sending` row. Unexpected task errors record only their exception type as `delivery_error`; filesystem failures are supervised separately from HTTP attempt failures.

Shutdown cancels delivery work, gathers task outcomes, and waits at most 5 seconds for in-flight delivery persistence. It then stops waiting and leaves the on-disk row as it is; complete queued or pending rows are eligible for retry after startup tail recovery when the store is usable. A filesystem operation running in a thread cannot be forcibly cancelled by asyncio and may outlive this budget. Configure `TimeoutStopSec=15` in the resident service unit's `[Service]` section: a filesystem stuck below the application is ended by the service manager's stop timeout. Keep the normal service-manager termination of the entire service process group enabled before allowing a restart.

Acknowledged stream IDs can be reported again on replay, and ambiguous responses can cause repeat POSTs; service idempotency is required. Trading deduplication remains separate. Delivery-file and `.torn` copy growth remain unbounded; retention/compaction and an automatic drain of idle delivery backlogs are not implemented.

From this directory:

```sh
make check
```

Checks use fake coordinator, venue Info, ticker, and websocket services, plus a throwaway Redis server. They require local TCP binding, never contact external services, choose a free Redis port assigned by the operating system, and remove their temporary files. Before starting services, a grep and AST audit permits scheduling only in the named coordinator backoff and websocket reconnect functions. HTTP request timeouts bound transport requests and never schedule ticket expiry. Case 7f covers expiry on the next trade, fills and reply events, reconnect reconciliation, and terminal unfilled IOC records. The suite verifies signatures and signed rearm envelopes, financial arithmetic, refusals, watcher ordering, partial exits, cancellation, recovery, throttling, Redis ACL restrictions and cursors, and file-length and Python-style constraints.

Ticket scenarios compare the `size` and `gate` CLI output against the executor's own records for sizing failures, policy refusals, daily spending and rounded buy/sell entries. They also check read-only review, local nonresident validation refusals with zero coordinator calls, and cancellation and flattening while nonresident. Local config, review and guard checks run before services start.

## Config

Copy `config.example.json` to the ignored `config.json` and replace every placeholder. Numeric policy epochs, Redis ports, expiry times and request timeouts must become JSON numbers. Prices, sizes, fractions and money use decimal strings.

| Field | Meaning |
| --- | --- |
| `coordinator_url` | Coordinator base URL; requests use only its prepare and intent routes. |
| `builder_dexs` | Builder DEX names to search alongside the main perp universe; optional, defaults to an empty list. The example includes `xyz`. |
| `resident` | Boolean, default `true`. Routine-driven bot workspaces must set `false`. |
| `stop_mode` | `software` (default) or `trigger`. Both labels use brackets when `resident: false`; resident execution retains software exits. |
| `bracket_slippage_bps` | Decimal basis points, default `50`. Trigger limits extend below triggers for sells and above for buys. Negative, nonfinite or over-5000 values refuse `trigger_price_outside_band`; every trigger and bound must also stay within 50% of entry. |
| `max_trigger_distance_bps` | Integer, default `1000`, strictly positive and at most `1000`; invalid values refuse at load with `invalid_max_trigger_distance_bps`. Stop and optional target distances are checked against current `markPx` before signing. |
| `symbols` | Canonical asset to `active_utc` intervals and `days` map. Example: `{"BTC": {"active_utc": [["00:00", "24:00"]], "days": "mon-sun"}}`. An absent asset is published, not traded by routines. |
| `info_url`, `websocket_url` | Public venue Info and websocket endpoints. |
| `binance_ticker_url` | Public price-ticker endpoint. |
| `binance_symbols` | Canonical asset to Binance symbol map; the example maps BTC, ETH and SOL. Missing mappings, including builder names, record null with `unmapped` and make no ticker request. |
| `access_client_id`, `access_client_secret` | Access service-token credentials, attached only to coordinator requests. |
| `signals_url` | Signals base URL for resident delivery reports; HTTPS with a hostname, no embedded credentials or raw `?` or `#` delimiters, including empty suffixes. The delivery route is built from parsed components. Absence disables reporting. Only fake scenarios explicitly allow HTTP to loopback. Redirects are disabled. |
| `subscriber_token_file` | Subscriber bearer-token file, read once at startup; exactly one nonempty line, with an optional final newline. Invalid files disable reporting without stopping recovery or trading. |
| `signals_access_client_id` | Signals Access service-token client ID, attached only to delivery requests. |
| `signals_access_client_secret` | Signals Access service-token client secret, attached only to delivery requests. |
| `key_ref` | Policy's signing-key reference. |
| `vault_address`, `account_address` | The same dedicated follower vault; equity, fills, positions and subscriptions use this account. |
| `delegate_key_file` | Private ed25519 seed file. |
| `record_path` | Append-only trading JSONL journal; its adjacent `.lock` file enforces one resident writer. Delivery rows use `<record_path>.deliveries.jsonl` with a separate file lock. Protect both files. |
| `control_socket` | Owner-only local Unix control socket used by the cancel CLI. |
| `risk_fraction` | Fraction of equity risked across entry-to-stop distance. |
| `leverage_cap` | Maximum entry notional divided by equity. |
| `exit_band` | Fractional IOC price allowance beyond a stop/target or current mark, `0 < exit_band ≤ 0.008`. Wider or invalid values refuse at load with `invalid_exit_band`. This pin keeps closes inside the 100 bps Signer band. |
| `request_timeout_seconds` | Shared request timeout override: coordinator default 120 seconds; delivery default 10 seconds. Delivery values must be finite, strictly positive and at most 30 seconds; all other values fall back to 10 seconds. |
| `policy.epoch` | Expected live signer policy epoch. |
| `policy.venues`, `policy.vault_address` | Allowed venues and required vault. |
| `policy.expires_at` | Unix seconds at which orders, including reduce-only exits, cease to be allowed. |
| `policy.revoked`, `policy.revoked_at` | Revocation status and Unix seconds of revocation. |
| `policy.chain_stale` | Optional fail-closed freshness mirror, default false; the signer remains authoritative. |
| `policy.asset_caps` | Array of asset, maximum order size and maximum daily signed size. |
| `redis.host`, `redis.port` | Stream connection endpoint and integer port. |
| `redis.username`, `redis.password` | Reader ACL credentials. Grant only XREAD, XRANGE, XINFO and PING on the configured stream. |
| `redis.stream` | The single publication stream. |

Publication envelopes contain `payload` and adjacent `receipt`; version is 1. The reader accepts an explicit outer `version: 1` and the agreed v1 envelope without an explicit version. Signal payloads need identity, publisher, venue symbol (or symbol fallback), side, invalidation, confidence, numeric entry and stop, and a future Unix-millisecond `reply_by`. A cancel payload needs kind, signal id and publisher. Stream metadata must agree with the embedded identity and kind. Receipt anchoring is outside this implementation. Symbols match case-insensitively across the main and configured builder universes. No match refuses `symbol_unmapped`; multiple matches refuse `symbol_ambiguous`. Tickets store the exact meta name in `venue_symbol` and `asset`; order and cancel intents use that name and its venue asset index. Builder indices use the DEX slot returned by `perpDexs`, including empty slots. Policy cap asset names must use canonical meta spelling.

## Execution and records

The trigger distance check mirrors the Signer desk policy's `max_trigger_distance_bps`, so the desk refuses locally rather than collecting signer refusals. Before signing an entry or bracket, each stop and optional target must satisfy `abs(trigger - markPx) / markPx * 10000 <= max_trigger_distance_bps`. Brackets check both original and rounded trigger prices. A violation refuses the entire signal with `trigger_distance_exceeds_max` and records the asset, leg (`sl` or `tp`) and `distance_bps`; nothing is signed or sent. Resident execution on this branch uses software exits and has no resident backstop replacement path.

Routine-driven bots set `resident: false`. An eligible signal submits exactly one `bracket`: a GTC entry at the signal entry, a resting stop at `levels.stop`, and a take-profit at `levels.target` when present. Venue precision rounding uses the existing price helper. Legs have the same size as the existing sizer’s entry, opposite direction, `reduce_only: true`, and `is_market: true`; their limit prices bound slippage. A signer refusal records `bracket_refused` with the unchanged reason and sends no plain entry. Reviewer records say `stop: bracket`.

The UTC routine runs at :01, :16, :31 and :46. Its first command, `desk_wake`, reads public Info once for the vault and emits a JSON object and a `WAKE` line. On `no`, the routine ends without posting. It uses `frontendOpenOrders` to distinguish resting entries from reduce-only triggers and `clearinghouseState` for positions, including mapped builder markets. Inside mapped active hours it wakes for an entry, a position, or the :00 close’s hourly signal check. It never reads the Signals service or a delegate seed. Failures produce `wake_unavailable` and `no`.

On `yes`, run the Executor’s `wake` command before routing signals. It reconciles existing tickets and cancels resting entries whose `reply_by` has expired or whose invalidation level has closed through. Maintenance, gate and submission use the explicit wake `close_ms` for the closed candle from `candleSnapshot`; missing candles fail closed. Submission checks that this close is still the last fully closed 15m bar immediately before sending and never substitutes a newer bar. A numeric `levels.invalidation`, if present, overrides `levels.stop`; the required prose `invalidation` remains descriptive. Comparison is inclusive: at or below for longs, at or above for shorts. A publication newer than the last close waits for a later close without being claimed. New entries and gate previews use the same guard. Repeated signals cannot create a second entry.

Active UTC intervals include their start and exclude their end, applied to the candle close timestamp and configured weekday range. Split overnight sessions at midnight; there is no implicit holiday or daylight-time calendar. Routine `run` and `wake` exit after one reconciliation pass, without starting a websocket, control server or background timer. Expiry or invalidation cancels only the unfilled entry remainder; a partial position retains its protective legs. Confirmed entry cancellation is persisted in the journal. Reconciliation also persists entry completion when no resting entry remains and the position is nonzero. Once a later pass sees the position flat, remaining legs are cleaned up, including after a stop closes a fully filled position, across restart and without an expiry or invalidation condition. Refused cancellations are recorded and retried on the next pass; the ticket occupies the account until all legs are confirmed gone. Existing positions close through the bracket, unless the signal is withdrawn with the existing cancel command or cancel envelope. Ambiguous outcomes remain held and never cause an entry replay.

Brackets need a Signer release that signs the bracket kind; until then the signer refuses the kind and the desk records `bracket_refused`. A venue-side scheduled cancel on the account can remove resting orders, stop legs included, between routine runs. This fork does not recommend live use until Signer documents how a bracket's stop survives it. Venue errors on any bracket leg are recorded as unresolved; a successful signature alone is not proof of protective orders.

The sizer uses quantity = risk fraction × equity / absolute entry-to-stop distance and rounds quantity down to the venue size precision. Maintenance rates and cumulative tier deductions determine the cross-margin liquidation estimate. The stop needs at least 0.25% of entry price between it and liquidation. Only a dedicated account that is initially flat, with no open orders, in the configured markets (the signal's market and the main and configured builder DEXs) is accepted; one active ticket occupies the account.

Resident entries are IOC at the signal entry rounded to venue price precision, up for buys and down for sells. The gate and intent record the rounded price; the original signal entry remains available for review. An IOC with a confirmed unfilled outcome finishes as `not_filled`; it is never retried. Both websocket subscriptions must acknowledge before submission. Stops and targets are software-triggered reduce-only IOC orders. The first exit round uses the trigger level plus the configured fractional band and cached size precision. After each partial or unfilled round, the next round reads the current mark and applies the band to it. Exits without a trigger read the current mark from the first round. No resting stop or target is created. The reviewer records `stop: software`, entry/fill prices, directional slippage and whether the stop traded.

Every `trades` and `userFills` message and every coordinator reply checks policy expiry: an open position exits reduce-only when now is at or past `policy.expires_at - 60` seconds. `reply_by` gates entry only. An IOC entry has no deadline callback. Websocket heartbeat/read timeout marks the watcher dead and attempts reconnection; it never decides an expiry exit. A successful reconnect emits `watcher_restarted`; the Executor reconciles orders, fills and position through Info, then checks the current mark against stop and target and checks policy expiry. Failed reconnection applies the dead-watcher rule and flattens reduce-only. There is no periodic polling loop. Successful closeout depends on venue availability and fills; an expired or revoked policy prevents further orders.

Trade batches check the cached position without Info requests until closeout is triggered. Pending batches for a ticket append all trades to one queued event, including while waiting for the operation lock, so a later recovery cannot hide a stop wick. Fills, coordinator replies and watcher restarts reconcile. Stop and target closeouts use the cached position for the first round; cancel, expiry, watcher-dead and restart-recovery closeouts reconcile before sending. Every closeout reconciles after each round.

The coordinator contract uses canonical signed envelopes, coordinator-owned nonces, validated signed schedule-cancel rearm, and at most one fresh prepare after a stale-nonce refusal. Request pacing enforces 60 door requests per minute. A rate-limited entry is refused; an exit honors Retry-After or exponential backoff. Missing rearm is refused. Neither service-token credentials nor the seed are journaled.

The state store records `claimed`, `sent`, `filled`, `refused`, `not_filled` and `exited`. `refused`, `not_filled` and `exited` are terminal and skipped on recovery.

The journal syncs before submission and records every send, coordinator reply, both observed oracle prices and their local observation timestamps, transitions, reconciliation and review. A UTC-day budget counts bracket entry size once, excludes protective legs, includes failed signed rounds and conservatively reserves ambiguous sends. Explicit unsigned replies release that reservation. Redelivery never creates another entry, even with a different publisher label for the same follower and signal id.

Before each exit or cancel send, the two oracle observations run concurrently with one attempt per source and a 0.5-second HTTP timeout each. A failed source records null price and timestamp plus its error, and the send continues through the policy gate. Entries refuse failed price reads; an unmapped Binance symbol is recorded without refusing. A stop or target closeout with no cached resting orders makes three HTTP calls before its first intent: one prepare, one venue oracle read and one mapped Binance read, with no reconciliation reads. Other closeout reasons retain the three initial reconciliation reads. An exit without a trigger adds one mark read; an unmapped Binance symbol removes one ticker call.

After an ambiguous send, open orders, fills and account positions are reconciled before further execution. Confirmed exposure is managed; an unresolved entry remains held and blocks new entries. A corrupt journal fails closed. These conditions require reconciliation rather than deleting the record or replaying the signal.

## Files

| File | Responsibility |
| --- | --- |
| `desk_signal.py` | Publication validation and unique asset-index resolution. |
| `desk_position_sizer.py` | Decimal sizing, margin tiers, leverage and liquidation room. |
| `desk_risk_gate.py` | Policy mirror, daily budget and signal expiry. |
| `desk_executor.py` | Serialized lifecycle, recovery, cancellation and command entry point. |
| `desk_bracket.py` | Signal bracket construction and closed-candle guards. |
| `desk_trigger_distance.py` | Decimal trigger distance checks against current mark, with refusal details. |
| `desk_routine.py` | One-shot bracket recovery, outcomes and pending-entry cancellation. |
| `desk_wake.py` | Read-only active-hours and work check, without a timer. |
| `desk_scenarios_bracket.py` | Fifteen socket-free bracket scenarios using the existing fake services. |
| `desk_scenarios_bracket_cleanup.py` | Full-entry fill persistence and leftover take-profit cleanup after a stop. |
| `signer_delegate_client.py` | Standalone signing, prepare/rearm, retries, pacing, Info reconciliation, symbol mapping and intent builders. |
| `desk_coordinator.py` | Desk callbacks for submission gating, watcher checks, state and recording. |
| `desk_watcher.py` | Subscription acknowledgements, websocket reader and reconnect events. |
| `desk_io.py` | Config validation, oracle observations and signal-side order adapter. |
| `desk_signal_consumer.py` | Redis reader, cursor, control socket and service entry point. |
| `desk_deliveries.py` | Resident-consumer delivery reports; durable queue before cursor, background POSTs, pending state from the separate delivery file. |
| `desk_delivery_store.py` | Durable store creation, parent-directory sync, locked final-record recovery and appends to the existing delivery file. |
| `desk_record.py` | Synced journal, state, deduplication, budget, review and writer lock. |
| `desk_keygen.py` | Exclusive private seed creation and public-key output. |
| `desk_scenarios.py` | Required scenario orchestration and execution assertions. |
| `desk_scenarios_checks.py` | Pure financial, policy, source-tree and named timer checks. |
| `desk_scenarios_execution.py` | Trade bursts, price precision, oracle failures, mapping and exit latency regressions. |
| `desk_scenarios_fakes.py` | Local fake coordinator, Info, ticker and websocket. |
| `desk_scenarios_redis.py` | Throwaway Redis process, ACL and restart scenarios. |
| `desk_scenarios_deliveries.py` | Fake Signals service scenarios for reports, retries, recovery and credential handling. |
| `desk_scenarios_delivery_safety.py` | Delivery configuration, persistence ordering, shutdown, backoff and task supervision regressions. |
| `desk_scenarios_delivery_persistence.py` | Separate file locks, shutdown budget, real response decoding, timeout ceiling and empty URL delimiter regressions. |
| `desk_scenarios_delivery_recovery.py` | Consumer restarts with torn tails, corrupt or unavailable stores, and file/directory creation durability failures. |

The missing-asset reason is `asset_not_allowed`, matching the signer. Local-only guards have their own explicit reasons.

## Reusing the delegate client

`signer_delegate_client.py` imports only the standard library, aiohttp and PyNaCl. It works as `desk.signer_delegate_client` or as a standalone module on the import path. It requires no desk state, signal, policy or record module.

Use `create_client(config, session, key=None)` with a reusable aiohttp session and an optional PyNaCl signing key; otherwise the key is loaded from `delegate_key_file`. The client config uses coordinator/Info URLs, Access credentials, key reference, vault/account addresses, optional request timeout and `builder_dexs`. `refresh_meta(client)` returns the combined universe; `resolve_symbol(symbol, meta)` returns canonical `asset` and `asset_index`. `order_fields(asset, size, price, is_buy, reduce_only=False)` and `cancel_fields(asset, order_id)` build IOC and cancel fields without a signal object. `bracket_fields(asset, size, entry_price, is_buy, stop_trigger, stop_limit, take_profit_trigger=None, take_profit_limit=None)` builds normalized GTC bracket fields and refuses wrong-side or out-of-band triggers locally. Coordinator refusal reasons pass through unchanged.

`await run_round(client, fields, before_send=None, validate_rearm=None, on_event=None)` serializes rounds. `before_send(body)` is an async callback run before every intent dispatch, including resends; return a refusal string or None. `validate_rearm(rearm)` is a synchronous extra policy check, returning a refusal string or None. `on_event(event, **values)` is synchronous and receives prepare replies, intent replies, backoffs and nonce retries. The caller supplies policy, authorization and persistence through these callbacks; the client always checks the rearm contract itself.

An ambiguous intent triggers Info reconciliation while the round lock is held and returns a `reconciliation` snapshot containing asset orders, fills and account state. No ambiguous intent is resent. Failed Info reconciliation propagates to the caller, which must retain the unresolved operation. The desk adapter consumes the snapshot and persists ticket transitions separately. Builder market contexts, account state and open orders are read with the resolved DEX; fills are filtered by canonical coin name.

## Bracket wire references

The bracket wire shape is an `order` action with `grouping: normalTpsl`: GTC entry first, reduce-only stop next, optional take-profit last. The desk sends bracket fields to the coordinator; Signer builds and serializes the venue action. The scenario wire fixture checks that translation.

The [Hyperliquid exchange-endpoint documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint) describes order fields `a`, `b`, `p`, `s`, `r`, `t`, optional `c`, and trigger fields `isMarket`, `triggerPx`, `tpsl`. The [Python SDK signing.py](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/utils/signing.py) shows their wire ordering, the `type`, `orders`, `grouping` wrapper and numeric formatting. The desk normalizes Decimal strings; Signer owns final serialization and digest validation. The 50% band, direction and size restrictions are Signer policy, not venue guarantees. The scheduled-cancel limitation above still applies.
