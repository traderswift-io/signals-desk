# Set up the Signals Desk demo

This guide documents the **TraderSwift demo desk** in Grok: five bots in a private Trading Floor, five role skills, two service credentials and one routine scheduled in UTC. The Signals service is private; obtain a subscriber token from the signal publisher. Setup generates the Executor's delegate key and places no orders.

This guide is for the `signer` branch; `main` mirrors upstream unchanged. The `traderswift` branch is the same desk plus chain verification of each signal; its SETUP.md covers the extra config. Use a reviewed checkout of this fork and record its commit. Routine-driven entries now use brackets. Brackets need a Signer release that signs the bracket kind; until then the signer refuses the kind and the desk records `bracket_refused`. This change does not configure a live run.

## 1. Prepare the bot workspaces

Place the same reviewed Signals Desk checkout in each bot's persistent workspace. Record `<REVIEWED_COMMIT>` and `<BOT_WORKSPACE>`. Obtain `<SIGNALS_SERVICE_ENDPOINT>` and its subscriber token from the signal publisher, and `<DOOR_ENDPOINT>` and an access service token for the door from the Signer service operator. Use `<ACCOUNT_ADDRESS>` for the dedicated follower account authorized by the delegate policy.

Use Python 3.11 or newer on Linux with `aiohttp`, `PyNaCl` and `redis`, as described in [desk/README.md](desk/README.md). Confirm that all five prompts and five skills below exist in the checkout. They use the shared `desk/` sizing, policy and delegate modules. Keep [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md) with the checkout and any copied instructions.

## 2. Create the five bots and install their skills

In Grok, create one bot per row. Copy its Name, Job and Description from the profile and paste the complete System prompt into its standing-instructions field. If only an initial message is supported, send the full prompt, ask for acknowledgement and record that this does not verify persistent instructions.

| Bot | Prompt | Skill to install and enable |
| --- | --- | --- |
| Desk Lead | `agents/signals-desk-lead.md` | `skills/signals-desk-intake/SKILL.md` |
| Position Sizer | `agents/signals-position-sizer.md` | `skills/signals-position-sizing/SKILL.md` |
| Risk Gate | `agents/signals-risk-gate.md` | `skills/signals-risk-gate/SKILL.md` |
| Executor | `agents/signals-executor.md` | `skills/signals-execution/SKILL.md` |
| Reviewer | `agents/signals-reviewer.md` | `skills/signals-review/SKILL.md` |

Install the complete skill body for each bot. Read back saved instructions where supported and record each bot's ID and verification status. The Desk Lead trusts publications served by the private service and routes their unchanged envelopes; the Position Sizer computes size; the Risk Gate can refuse; the Executor alone submits through the door; the Reviewer reports recorded results. Intake does not verify publisher registration, veto status or receipt anchoring on a chain.

## 3. Create the Trading Floor

Create a private group named **Trading Floor**, add the five bots and record the group and member IDs. Send this welcome message:

> Desk Lead routes publications; Position Sizer computes; Risk Gate checks; Executor alone submits through the door under its Signer policy; Reviewer records. Setup places no orders. Eligible entries use a bracket with a resting stop and optional take-profit, subject to a Signer release that signs the bracket kind and a confirmed delegate policy. Setup does not enable a live run.

## 4. Add the service credentials

Use Grok's secret store for these two credentials. The door credential contains a client ID and client secret; bind both fields as required by the execution skill. Keep values out of chats, prompts, version control, logs and the setup record.

| Credential | Source and purpose | User |
| --- | --- | --- |
| `<DOOR_SERVICE_TOKEN>` | An access service token for the door, obtained from the Signer service operator for `<DOOR_ENDPOINT>`. | Executor |
| `<SIGNALS_SUBSCRIBER_TOKEN>` | A revocable subscriber token obtained from the signal publisher for the private service at `<SIGNALS_SERVICE_ENDPOINT>`. | Desk Lead |

Use only the configured account. The bots do not need a venue signing key or a publisher credential. Generate the delegate seed locally in step 6; never paste it into chat or the secret store. If the private service or subscriber credential is unavailable, intake remains idle. There is no public-feed fallback.

## 5. Schedule one routine in UTC

Create one Grok routine owned by the Desk Lead, with timezone **UTC**, at **:01, :16, :31 and :46** each hour: one minute after each 15m candle close. Replace the three pre-open routines. Record the routine ID and its next UTC run.

Its first step is `python3 -m desk.desk_wake desk/config.json`. This reads public Info once and prints JSON containing `close_ms` and `last_close_utc`, plus `WAKE | yes|no | <reason> | next_close_utc=<iso>`. On `no`, end the run immediately with no posting. On `yes`, the Executor runs `python3 -m desk.desk_executor --config desk/config.json --close-ms <close_ms> wake`, then the Desk Lead reads eligible publications and routes them through sizing and risk checks. Copy the printed integer `close_ms` unchanged into every Executor command and the Risk Gate command as `--close-ms <close_ms>` before the subcommand; pass it with each envelope between seats. Maintenance, gate and submission use that same candle. A missing close or a handoff across the next close refuses `stale_wake_close`, including a boundary crossed during maintenance candle reads before a new invalidation cancellation is recorded or sent; end that handoff and wait for a new wake. Preserve newer publications for a later close.

Configure `symbols` with each traded asset’s `active_utc` intervals and `days`. The example maps BTC to `00:00`–`24:00`, `mon-sun`. Hours apply to the close timestamp in UTC; interval starts are inclusive and ends exclusive. Split overnight sessions into two intervals. There is no implicit holiday calendar or daylight-time conversion; update UTC hours explicitly. An absent asset is published, not traded.

A mapped asset inside active hours wakes for a resting entry, an open position, or the :00 close (the hourly opportunity to check for new signals). Other flat closes stay idle. Outside active hours, positions rely on their venue brackets, subject to the scheduled-cancel limitation documented below. `desk_wake` neither reads Signals nor signs or posts anything. It has no internal scheduler.

## 6. Generate the Executor delegate and register its policy

Prepare private `desk/config.json` from [desk/config.example.json](desk/config.example.json) as described in the execution skill. Replace all placeholders, use `<ACCOUNT_ADDRESS>` consistently for `account_address`, `vault_address` and `policy.vault_address`, and set mode 0600. Routine-driven workspaces must set `resident: false`; either stop-mode label uses brackets. Set `symbols` and `bracket_slippage_bps` (default 50). The example’s resident default is for the continuously running service. Protect the key, journal and control socket directories with owner-only permissions.

Routine-driven (Grok) workspaces must remove `signals_url`, `subscriber_token_file`, `signals_access_client_id` and `signals_access_client_secret` from `desk/config.json`, because only the resident consumer reports deliveries. The resident service reports each stream entry by its stream entry ID; Grok seats make no delivery report, because the service records their GET. Assign one subscriber token to one desk.

For the resident consumer, use an HTTPS `signals_url` without embedded credentials and the four delivery fields documented in [desk/README.md](desk/README.md). Reporting is off when `signals_url` is absent. If it is set but the token file is unreadable, empty or multi-line, reporting stays off and a sanitized `deliveries_disabled` reason is recorded; recovery and trading continue. Invalid delivery URLs or missing delivery credentials also disable reporting. Fix the configuration and restart to enable reports; entries consumed while reporting is disabled are not queued for later reporting.

Protect `<record_path>.deliveries.jsonl` alongside the trading journal, and retain and restore both files together. Delivery rows use that separate file and lock; an unavailable or corrupt store instead records a sanitized disabled-reporting diagnostic in the trading journal and leaves reporting off without blocking recovery. Entries consumed while reporting is disabled are not queued. Startup durably creates the delivery file, fsyncs its parent directory, and repairs only an incomplete or unparseable final record under the delivery lock. Interior corruption requires operator repair. Set `TimeoutStopSec=15` in the resident unit's `[Service]` section and retain service-manager termination of the entire service process group before restart. The reporter waits at most 5 seconds for in-flight persistence during shutdown; a filesystem stuck below that is ended by the service manager's stop timeout. Complete queued or pending rows remain eligible for retry after startup tail recovery when the store is usable.

In the Executor's persistent workspace, run once from the checkout root:

```sh
python3 -m desk.desk_keygen desk/config.json
```

Key generation creates the seed exclusively at mode **0600** and prints only the ed25519 public hex. Verify the file mode without reading the seed. Do not overwrite or delete an existing seed to repeat setup.

Register `<EXECUTOR_PUBLIC_HEX>` as a delegate with **Signer** under a policy, following the public [Signer project at TraderSwift](https://github.com/traderswift-io/signer). Supply only the public key for registration; retain the seed locally. Confirm the registered account, signing-key reference, policy epoch, allowed assets, per-order and daily size caps, expiry and revocation state with the Signer service operator. Record the confirmed policy and copy its values into the private config. A different delegate's policy does not authorize this key. If multiple delegates share an account budget, confirm their allocations fit that budget before enabling execution.

Set `delegate_public_key` to the printed hex in the read-only role configs, together with the issued `key_ref`, current policy and read access to the Executor's current journal. Do not distribute the seed or door credentials to those roles. A lost workspace requires a new locally generated key and a newly registered policy.

## 7. Verify configuration with new entries disabled

Brackets need a Signer release that signs the bracket kind; until then the signer refuses the kind and the desk records `bracket_refused`. It never falls back to an entry without a stop. The Grok delegate’s own public hex and confirmed policy are still required.

The entry is GTC; the stop and optional target are opposite-side, same-size, reduce-only market triggers with bounded slippage. Resident software exits are unchanged. A venue-side scheduled cancel on the account can remove resting orders, stop legs included, between routine runs. This fork does not recommend live use until Signer documents how a bracket's stop survives it. See [desk/README.md](desk/README.md) for wire references and limitations.

Check secret presence without displaying values, bot and skill configuration, and the UTC schedules. Rehearse with the committed fake-service scenarios described in [desk/README.md](desk/README.md). Never place a live order to verify setup. Key generation, cancellation and reduce-only closeout remain available through the existing commands and policy checks.

## 8. Record the setup result

Record the reviewed commit and workspace paths; five bot IDs and prompt/skill verification status; Trading Floor ID and member IDs; and credential names with presence status only. Include the single routine ID and next UTC run, public delegate hex, verified seed mode, registered policy and caps, and bracket signer readiness.

Finish with the actual state: **setup pending** or **configured with live execution pending**. Identify missing credentials, unavailable service access or an unconfirmed policy. Never include credential values or the private seed.
