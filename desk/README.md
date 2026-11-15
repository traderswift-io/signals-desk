# Signals Desk executor

This is the TraderSwift demo desk. The Signals service is private; obtain a subscriber token from the signal publisher. See [SETUP.md](../SETUP.md) for the five-bot setup, UTC routines and delegate-policy registration on signer-1.

Based on HyperGrok Trading Desk. The existing [MIT license](../LICENSE) remains unchanged. This component adds policy-authorized execution through an ed25519 delegate, a position sizer, a local policy mirror, and software exits. It does not use a venue signing key.

Python 3.11 or newer on Linux is required, with `aiohttp`, `PyNaCl`, and `redis`. Scenario checks also require `redis-server` on the command search path.

## Commands

From the repository root, after supplying a private config JSON:

```sh
python3 -m desk.desk_keygen desk/config.json
python3 -m desk.desk_signal_consumer desk/config.json
python3 -m desk.desk_executor --config desk/config.json run signal.json
python3 -m desk.desk_executor --config desk/config.json cancel SIGNAL_ID
python3 -m desk.desk_ticket --config desk/config.json size signal.json
python3 -m desk.desk_ticket --config desk/config.json gate signal.json
python3 -m desk.desk_ticket --config desk/config.json review SIGNAL_ID
python3 -m desk.desk_anchor_verify desk/config.json envelope.json
```

The default executor config path is `desk/config.json`, so `python3 -m desk.desk_executor cancel SIGNAL_ID` also works. Key generation runs once on the executor machine, prints only the public key, creates the seed file with mode 600, and refuses to replace any existing destination. Protect the config, record directory, and local control socket directory with owner-only permissions. Config, key, record, and socket paths are operator-supplied; relative paths resolve from the working directory.

The ticket CLI also defaults to `desk/config.json` and prints one JSON object. `size` prints the existing sizer's decimal strings or its refusal with the numbers. `gate` prints `status: PASS` with `sizing` and an unsigned `intent` preview, or `refused` with the same policy reason as the executor. Both use the executor's shared entry preview, including venue price rounding. These commands only read public Info and local files; they never prepare, send, or append records. They never load the delegate seed or need door credentials. Size needs no delegate identity. Gate requires `delegate_public_key` (the public hex printed by keygen), the issued `key_ref`, and reads the Executor's journal for the same daily budget. A missing journal counts as nothing signed today; Signer still enforces the daily cap; a missing or malformed public key refuses `invalid_delegate_public_key`. Read-only seat configs retain the example fields, with unused credential and seed fields left as placeholders. Keep the policy, public identity and journal current; a stale copy cannot establish current budget availability. A gate PASS describes the policy mirror; execution still checks residency, account occupancy and watcher readiness. `review` reads the recorded Reviewer object, including signal entry, fill, slippage and the recorded stop mode; it returns `unknown_signal_id` or `review_unavailable` when absent and never creates a review.

The consumer uses blocking stream reads and persists the last processed stream id. A Redis connection failure is recorded, cancels active tickets through the normal reduce-only closeout, and stops the consumer; restart resumes from the saved cursor. It holds an exclusive record lock. A cancel command uses the running executor's local control socket; when no executor is running, it acquires that lock and reconciles outstanding tickets before cancellation. Startup recovery closes outstanding exposure and never resends an entry. Use a single configured record for a follower account and keep it across restarts.

From this directory:

```sh
make check
```

Checks use fake coordinator, venue Info, ticker, and websocket services, plus a throwaway Redis server. They require local TCP binding, never contact external services, choose a free Redis port assigned by the operating system, and remove their temporary files. Before starting services, a grep and AST audit permits scheduling only in the named coordinator backoff and websocket reconnect functions. HTTP request timeouts bound transport requests and never schedule ticket expiry. Case 7f covers expiry on the next trade, fills and reply events, reconnect reconciliation, and terminal unfilled IOC records. The suite verifies signatures and signed rearm envelopes, financial arithmetic, refusals, watcher ordering, partial exits, cancellation, recovery, throttling, Redis ACL restrictions and cursors, and file-length and Python-style constraints.

Ticket scenarios compare the `size` and `gate` CLI output against the executor's own records for sizing failures, policy refusals, daily spending and rounded buy/sell entries. They also check read-only review, the nonresident `run` refusal with zero coordinator calls, and cancellation and flattening while nonresident. Local config, review and guard checks run before services start.

## Config

Copy `config.example.json` to the ignored `config.json` and replace every placeholder. Numeric policy epochs, Redis ports, expiry times and request timeouts must become JSON numbers. Prices, sizes, fractions and money use decimal strings.

| Field | Meaning |
| --- | --- |
| `chain_url` | Verifier HTTPS base; use `https://chain.traderswift.io`. Requires a host, empty or `/` path, and no userinfo, query or fragment. |
| `provider_id`, `provider_address` | Publisher pin for `desk_anchor_verify.py`: ID matches `^[A-Za-z0-9._:-]{1,128}$` in full, excluding `.` and `..`; address is a nonempty string compared exactly. |
| `coordinator_url` | Coordinator base URL; requests use only its prepare and intent routes. |
| `builder_dexs` | Builder DEX names to search alongside the main perp universe; optional, defaults to an empty list. The example includes `xyz`. |
| `resident` | Boolean, default `true`. Routine-driven bot workspaces must set `false`. |
| `stop_mode` | `software` (default) or `trigger`. With `resident: false`, both modes refuse new entries with `no_resting_stop_until_rotation` before any coordinator call until resting trigger support ships. |
| `info_url`, `websocket_url` | Public venue Info and websocket endpoints. |
| `binance_ticker_url` | Public price-ticker endpoint. |
| `binance_symbols` | Canonical asset to Binance symbol map; the example maps BTC, ETH and SOL. Missing mappings, including builder names, record null with `unmapped` and make no ticker request. |
| `access_client_id`, `access_client_secret` | Access service-token credentials, attached only to coordinator requests. |
| `key_ref` | Policy's signing-key reference. |
| `vault_address`, `account_address` | The same dedicated follower vault; equity, fills, positions and subscriptions use this account. |
| `delegate_key_file` | Private ed25519 seed file. |
| `record_path` | Append-only JSONL journal; its adjacent `.lock` file enforces one writer. |
| `control_socket` | Owner-only local Unix control socket used by the cancel CLI. |
| `risk_fraction` | Fraction of equity risked across entry-to-stop distance. |
| `leverage_cap` | Maximum entry notional divided by equity. |
| `exit_band` | Fractional IOC price allowance beyond a stop/target or current mark. |
| `request_timeout_seconds` | Coordinator request timeout, default 120 seconds. The anchor verifier defaults to 10 seconds and accepts only finite numbers (not booleans) with `0 < x <= 60`. |
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

Routine-driven bots cannot hold the software stop watcher. Their entries remain disabled until resting stop support is implemented: keep `resident: false` and `stop_mode: software` until that support ships. The `trigger` value reserves the configuration choice for that integration; selecting it does not implement a resting stop in this version. Cancel envelopes, cancel commands, reduce-only flattening, key generation and the refused-intent door check remain allowed. Resident software execution keeps its existing behavior. Reviewer records continue to identify the software stops actually implemented here.

The sizer uses quantity = risk fraction × equity / absolute entry-to-stop distance and rounds quantity down to the venue size precision. Maintenance rates and cumulative tier deductions determine the cross-margin liquidation estimate. The stop needs at least 0.25% of entry price between it and liquidation. Only a dedicated account that is initially flat, with no open orders, in the configured markets (the signal's market and the main and configured builder DEXs) is accepted; one active ticket occupies the account.

Entries are IOC at the supplied entry rounded to venue price precision, up for buys and down for sells. The gate and intent record the rounded price; the original signal entry remains available for review. An IOC with a confirmed unfilled outcome finishes as `not_filled`; it is never retried. Both websocket subscriptions must acknowledge before submission. Stops and targets are software-triggered reduce-only IOC orders. The first exit round uses the trigger level plus the configured fractional band and cached size precision. After each partial or unfilled round, the next round reads the current mark and applies the band to it. Exits without a trigger read the current mark from the first round. No resting stop or target is created. The reviewer records `stop: software`, entry/fill prices, directional slippage and whether the stop traded.

Every `trades` and `userFills` message and every coordinator reply checks policy expiry: an open position exits reduce-only when now is at or past `policy.expires_at - 60` seconds. `reply_by` gates entry only. An IOC entry has no deadline callback. Websocket heartbeat/read timeout marks the watcher dead and attempts reconnection; it never decides an expiry exit. A successful reconnect emits `watcher_restarted`; the Executor reconciles orders, fills and position through Info, then checks the current mark against stop and target and checks policy expiry. Failed reconnection applies the dead-watcher rule and flattens reduce-only. There is no periodic polling loop. Successful closeout depends on venue availability and fills; an expired or revoked policy prevents further orders.

Trade batches check the cached position without Info requests until closeout is triggered. Pending batches for a ticket append all trades to one queued event, including while waiting for the operation lock, so a later recovery cannot hide a stop wick. Fills, coordinator replies and watcher restarts reconcile. Stop and target closeouts use the cached position for the first round; cancel, expiry, watcher-dead and restart-recovery closeouts reconcile before sending. Every closeout reconciles after each round.

The coordinator contract uses canonical signed envelopes, coordinator-owned nonces, validated signed schedule-cancel rearm, and at most one fresh prepare after a stale-nonce refusal. Request pacing enforces 60 door requests per minute. A rate-limited entry is refused; an exit honors Retry-After or exponential backoff. Missing rearm is refused. Neither service-token credentials nor the seed are journaled.

The state store records `claimed`, `sent`, `filled`, `refused`, `not_filled` and `exited`. `refused`, `not_filled` and `exited` are terminal and skipped on recovery.

The journal syncs before submission and records every send, coordinator reply, both observed oracle prices and their local observation timestamps, transitions, reconciliation and review. A UTC-day budget includes failed signed rounds and conservatively reserves ambiguous sends. Explicit unsigned replies release that reservation. Redelivery never creates another entry, even with a different publisher label for the same follower and signal id.

Before each exit or cancel send, the two oracle observations run concurrently with one attempt per source and a 0.5-second HTTP timeout each. A failed source records null price and timestamp plus its error, and the send continues through the policy gate. Entries refuse failed price reads; an unmapped Binance symbol is recorded without refusing. A stop or target closeout with no cached resting orders makes three HTTP calls before its first intent: one prepare, one venue oracle read and one mapped Binance read, with no reconciliation reads. Other closeout reasons retain the three initial reconciliation reads. An exit without a trigger adds one mark read; an unmapped Binance symbol removes one ticker call.

After an ambiguous send, open orders, fills and account positions are reconciled before further execution. Confirmed exposure is managed; an unresolved entry remains held and blocks new entries. A corrupt journal fails closed. These conditions require reconciliation rather than deleting the record or replaying the signal.

## Files

| File | Responsibility |
| --- | --- |
| `desk_signal.py` | Publication validation and unique asset-index resolution. |
| `desk_position_sizer.py` | Decimal sizing, margin tiers, leverage and liquidation room. |
| `desk_risk_gate.py` | Policy mirror, daily budget and signal expiry. |
| `desk_executor.py` | Serialized lifecycle, recovery, cancellation and command entry point. |
| `signer_delegate_client.py` | Standalone signing, prepare/rearm, retries, pacing, Info reconciliation, symbol mapping and intent builders. |
| `desk_coordinator.py` | Desk callbacks for submission gating, watcher checks, state and recording. |
| `desk_watcher.py` | Subscription acknowledgements, websocket reader and reconnect events. |
| `desk_io.py` | Config validation, oracle observations and signal-side order adapter. |
| `desk_signal_consumer.py` | Redis reader, cursor, control socket and service entry point. |
| `desk_record.py` | Synced journal, state, deduplication, budget, review and writer lock. |
| `desk_keygen.py` | Exclusive private seed creation and public-key output. |
| `desk_anchor_verify.py` | Chain reads (charter, provider, anchor) and restricted JCS subset payload hash check (floats refused) before intake hands a signal on; prints JSON metadata and one `VERIFY` result line. |
| `desk_scenarios.py` | Required scenario orchestration and execution assertions. |
| `desk_scenarios_checks.py` | Pure financial, policy, source-tree and named timer checks. |
| `desk_scenarios_execution.py` | Trade bursts, price precision, oracle failures, mapping and exit latency regressions. |
| `desk_scenarios_fakes.py` | Local fake coordinator, Info, ticker and websocket. |
| `desk_scenarios_redis.py` | Throwaway Redis process, ACL and restart scenarios. |
| `desk_scenarios_anchor.py` | Mocked HTTPS transport with real aiohttp JSON decoding: transport self-check, pass, all twelve refusal names, address/config/ID validation, duplicate keys, safe output, timeout bounds, recursion, status/MIME precedence and restricted JCS fixtures. No live chain or TLS handshake is exercised. |

The missing-asset reason is `asset_not_allowed`, matching the signer. Local-only guards have their own explicit reasons.

## Reusing the delegate client

`signer_delegate_client.py` imports only the standard library, aiohttp and PyNaCl. It works as `desk.signer_delegate_client` or as a standalone module on the import path. It requires no desk state, signal, policy or record module.

Use `create_client(config, session, key=None)` with a reusable aiohttp session and an optional PyNaCl signing key; otherwise the key is loaded from `delegate_key_file`. The client config uses coordinator/Info URLs, Access credentials, key reference, vault/account addresses, optional request timeout and `builder_dexs`. `refresh_meta(client)` returns the combined universe; `resolve_symbol(symbol, meta)` returns canonical `asset` and `asset_index`. `order_fields(asset, size, price, is_buy, reduce_only=False)` and `cancel_fields(asset, order_id)` build IOC and cancel fields without a signal object.

`await run_round(client, fields, before_send=None, validate_rearm=None, on_event=None)` serializes rounds. `before_send(body)` is an async callback run before every intent dispatch, including resends; return a refusal string or None. `validate_rearm(rearm)` is a synchronous extra policy check, returning a refusal string or None. `on_event(event, **values)` is synchronous and receives prepare replies, intent replies, backoffs and nonce retries. The caller supplies policy, authorization and persistence through these callbacks; the client always checks the rearm contract itself.

An ambiguous intent triggers Info reconciliation while the round lock is held and returns a `reconciliation` snapshot containing asset orders, fills and account state. No ambiguous intent is resent. Failed Info reconciliation propagates to the caller, which must retain the unresolved operation. The desk adapter consumes the snapshot and persists ticket transitions separately. Builder market contexts, account state and open orders are read with the resolved DEX; fills are filtered by canonical coin name.
