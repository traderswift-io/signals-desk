# Set up the Signals Desk demo

This guide documents the **TraderSwift demo desk** in Grok: five bots in a private Trading Floor, five role skills, two service credentials and three routines scheduled in UTC. The Signals service is private; obtain a subscriber token from the signal publisher. Setup generates the Executor's delegate key and places no orders.

This guide is for the `traderswift` branch; `main` mirrors upstream unchanged and `signer` is the same desk without chain verification. Use a reviewed checkout of this fork and record its commit. The current release does not support resting stop orders for routine-driven bots, so their new entries remain disabled.

## 1. Prepare the bot workspaces

Place the same reviewed Signals Desk checkout in each bot's persistent workspace. Record `<REVIEWED_COMMIT>` and `<BOT_WORKSPACE>`. Obtain `<SIGNALS_SERVICE_ENDPOINT>` and its subscriber token from the signal publisher, and `<DOOR_ENDPOINT>` and an access service token for the door from the Signer service operator. Use `<ACCOUNT_ADDRESS>` for the dedicated follower account authorized by the delegate policy. Mm1 is the demo's test account.

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

Install the complete skill body for each bot. Read back saved instructions where supported and record each bot's ID and verification status. The Desk Lead refuses a signal unless its charter, provider and anchor reads on the chain all pass, and routes only unchanged envelopes that pass them; the Position Sizer computes size; the Risk Gate can refuse; the Executor alone submits through the door; the Reviewer reports recorded results.

## 3. Create the Trading Floor

Create a private group named **Trading Floor**, add the five bots and record the group and member IDs. Send this welcome message:

> Desk Lead routes publications; Position Sizer computes; Risk Gate checks; Executor alone submits through the door under its Signer policy; Reviewer records. Setup places no orders. New entries remain disabled until resting stop support is implemented and the delegate policy is confirmed.

## 4. Add the service credentials

Use Grok's secret store for these two credentials. The door credential contains a client ID and client secret; bind both fields as required by the execution skill. Keep values out of chats, prompts, version control, logs and the setup record.

| Credential | Source and purpose | User |
| --- | --- | --- |
| `<DOOR_SERVICE_TOKEN>` | An access service token for the door, obtained from the Signer service operator for `<DOOR_ENDPOINT>`. | Executor |
| `<SIGNALS_SUBSCRIBER_TOKEN>` | A revocable subscriber token obtained from the signal publisher for the private service at `<SIGNALS_SERVICE_ENDPOINT>`. | Desk Lead |

Use only the configured account. The bots do not need a venue signing key or a publisher credential. Generate the delegate seed locally in step 6; never paste it into chat or the secret store. If the private service or subscriber credential is unavailable, intake remains idle. There is no public-feed fallback.

## 5. Schedule the three pre-open routines in UTC

Create exactly three Grok routines owned by the Desk Lead, with timezone **UTC**. Each invokes `signals-desk-intake` using the private subscriber credential and routes results through the recorded Trading Floor. Keep entry submission disabled.

Schedule each routine **15 minutes before** its session open. Assume Tokyo opens at 09:00 JST, London at 08:00 UK local time, and New York at 09:30 Eastern Time. These are scheduling assumptions; venue hours may differ.

| Routine | Assumed session open | Open in UTC | Routine in UTC |
| --- | --- | --- | --- |
| Before Tokyo | 09:00 JST (UTC+09:00), year-round | 00:00 | **23:45 on the preceding UTC date** |
| Before London | 08:00 GMT (UTC+00:00), standard time | 08:00 | **07:45** |
| Before London | 08:00 BST (UTC+01:00), daylight time | 07:00 | **06:45** |
| Before New York | 09:30 EST (UTC-05:00), standard time | 14:30 | **14:15** |
| Before New York | 09:30 EDT (UTC-04:00), daylight time | 13:30 | **13:15** |

The seasonal rows are alternatives for the same London and New York routines. Run on session weekdays and skip the relevant market holidays. Tokyo's Monday–Friday opens correspond to Sunday–Thursday routine dates in UTC.

London changes on the last Sunday in March and October; New York changes on the second Sunday in March and first Sunday in November. Adjust each UTC routine independently at its session's transition. Verify the displayed next run in UTC and record the three routine IDs, timezone and next run dates and times. If intake is unavailable, report that fact and leave the desk idle.

## 6. Generate the Executor delegate and register its policy

Prepare private `desk/config.json` from [desk/config.example.json](desk/config.example.json) as described in the execution skill. Replace all placeholders, use `<ACCOUNT_ADDRESS>` consistently for `account_address`, `vault_address` and `policy.vault_address`, and set mode 0600. Routine-driven workspaces must set `resident: false` and `stop_mode: software`; the example's resident default is for the continuously running service. Protect the key, journal and control socket directories with owner-only permissions. Set `chain_url` to `https://chain.traderswift.io`, and `provider_id` and `provider_address` to the signal publisher's provider id and chain address, as given by the publisher; the Desk Lead and intake skill refuse a signal whose chain record does not match them. Run `python3 -m desk.desk_anchor_verify desk/config.json <envelope.json>` before intake handoff. The verifier validates config before opening the envelope: the provider ID must match `^[A-Za-z0-9._:-]{1,128}$` in full, excluding `.` and `..`, and the pinned address must be a nonempty string. Its request timeout defaults to 10 seconds and must be a finite number (not a boolean) greater than 0 and at most 60. The complete anchor verifier protocol is stated below.

In the Executor's persistent workspace, run once from the checkout root:

```sh
python3 -m desk.desk_keygen desk/config.json
```

Key generation creates the seed exclusively at mode **0600** and prints only the ed25519 public hex. Verify the file mode without reading the seed. Do not overwrite or delete an existing seed to repeat setup.

Register `<EXECUTOR_PUBLIC_HEX>` as a delegate with a policy on **signer-1**, following the public [Signer project at TraderSwift](https://github.com/traderswift-io/signer). Supply only the public key for registration; retain the seed locally. Confirm the registered account, signing-key reference, policy epoch, allowed assets, per-order and daily size caps, expiry and revocation state with the Signer service operator. Record the confirmed policy and copy its values into the private config. A different delegate's policy does not authorize this key. If multiple delegates share an account budget, confirm their allocations fit that budget before enabling execution.

Set `delegate_public_key` to the printed hex in the read-only role configs, together with the issued `key_ref`, current policy and read access to the Executor's current journal. Do not distribute the seed or door credentials to those roles. A lost workspace requires a new locally generated key and a newly registered policy.

## 7. Verify configuration with new entries disabled

The current implementation refuses routine-driven entries with `no_resting_stop_until_rotation` for both `software` and `trigger` settings. Selecting `trigger` does not create a resting stop. Keep `resident: false`; do not change residency to bypass this guard.

Readiness for future entries requires implemented resting stop support, a confirmed delegate policy and available service credentials. A service announcement or calendar date alone does not meet those requirements. Existing software exits belong to the continuously running executor and do not protect a routine-driven entry.

Check secret presence without displaying values, bot and skill configuration, and the UTC schedules. Rehearse with the committed fake-service scenarios described in [desk/README.md](desk/README.md). Never place a live order to verify setup. Key generation, cancellation and reduce-only closeout remain available through the existing commands and policy checks.

## 8. Record the setup result

Record the reviewed commit and workspace paths; five bot IDs and prompt/skill verification status; Trading Floor ID and member IDs; and credential names with presence status only. Include the three routine IDs and next UTC runs, public delegate hex, verified seed mode, registered policy and caps, and resting stop support status.

Finish with the actual state: **setup pending** or **configured with new entries disabled** for this version. Identify missing credentials, unavailable service access or an unconfirmed policy. Never include credential values or the private seed.

## Anchor verifier protocol

Run `python3 -m desk.desk_anchor_verify desk/config.json <envelope.json>` from the checkout root before ticket posting or handoff. Validation stops at the first refusal: fully parse and validate config before opening the envelope; parse the envelope and validate its identity; canonicalize and hash its payload; then read charter, provider (including its address pin), and anchor (including its hash). No chain writes, redirects, retries or sleeps occur.

Config must be an object with `chain_url`, `provider_id` and `provider_address`. The base must use HTTPS with a host, an empty or `/` path, and no userinfo, query or fragment, including empty `?` or `#` suffixes. Whitespace, control characters, backslashes and malformed ports are refused; endpoint paths replace parsed URL components. Use `https://chain.traderswift.io`. The pinned address must be a nonempty string and is compared exactly, without trimming or case folding. Both provider and signal IDs must match `^[A-Za-z0-9._:-]{1,128}$` in full, excluding `.` and `..`. `request_timeout_seconds` defaults to 10 and must be a finite JSON number, not a boolean or string, with `0 < x <= 60`.

Config, envelope and chain JSON reject duplicate object keys and bare `NaN`, `Infinity` and `-Infinity` at every depth. Unreadable files, malformed local JSON or file decoding, and input recursion failures yield `invalid_input`. The envelope and its `payload` must be objects with a valid string `payload.signal_id`, checked before canonicalization. Canonicalization supports objects with string keys sorted by UTF-16 code units, arrays, valid Unicode scalar strings, booleans, null and integers with absolute value at most 9007199254740991. All floats, unsupported payload values and unpaired surrogates in payload strings or keys yield `payload_not_canonical`; payload recursion yields `invalid_input`. Whitespace and key order are normalized without Unicode normalization. Escaped valid surrogate pairs decode to supported scalar strings. Unused local fields outside the payload are not recursively Unicode-validated.

The three GET paths, relative to the configured base, are `/network/v1/charter/signals`, `/network/v1/provider/signals/<provider_id>`, and `/network/v1/anchor/signals/signal/<signal_id>/<provider_id>`. Both provider path segments use the config pin; the signal segment uses the validated payload ID. Each response status is checked before its body: 429 or any status >=500 yields `chain_unavailable`; every other non-200, including redirects, yields that read's missing refusal. Only 200 bodies are decoded, with aiohttp JSON MIME checking (including `application/*+json`). Invalid JSON, duplicate keys, non-JSON constants, charset/text decoding errors, invalid Unicode scalar strings or keys at any depth, decoding/validation recursion, or a non-object body yields `chain_unavailable`. Chain numbers are not subject to the payload's restricted numeric subset. Only top-level fields are inspected for veto, address and hash; records are not unwrapped.

The result is `pass` or exactly one of these twelve refusals. Each condition applies only after preceding checks have succeeded:

| Refusal | Condition |
| --- | --- |
| `invalid_input` | Unreadable or malformed config/envelope file, duplicate keys, non-JSON constants, invalid config, or input/payload recursion. Config validation precedes envelope access. |
| `invalid_envelope` | Envelope or payload is not an object, or `payload.signal_id` is missing or violates the ID grammar. |
| `payload_not_canonical` | Payload contains a float, unsafe integer, unsupported type, non-string key or invalid Unicode after identity validation. |
| `chain_unavailable` | Connection, TLS, timeout or other aiohttp client error; 429 or any status >=500; invalid 200 body/MIME/charset/Unicode as above; or recursion during chain processing. |
| `charter_missing` | Charter status is non-200 and below 500, except 429. |
| `charter_vetoed` | Decoded charter object's `vetoed` is missing or anything other than literal JSON `false`. |
| `provider_missing` | Provider status is non-200 and below 500, except 429. |
| `provider_vetoed` | Decoded provider object's `vetoed` is missing or anything other than literal JSON `false`. |
| `provider_address_mismatch` | After provider veto checking, `provider_address` is absent, not a nonempty string, or not exactly equal to the config pin. |
| `anchor_missing` | Anchor status is non-200 and below 500, except 429. |
| `anchor_vetoed` | Decoded anchor object's `vetoed` is missing or anything other than literal JSON `false`. |
| `hash_mismatch` | After anchor veto checking, `segment_hash` is absent or not exactly the lowercase SHA-256 hex of the canonical payload's UTF-8 bytes. |

The CLI prints exactly two lines: JSON metadata with `signal_id` (validated ID or null) and `reads` (received HTTP statuses in order), then `VERIFY | <validated ID or -> | <result>`. Only the VERIFY line carries the result; no raw exception text is a result. Exit status is 0 only for `pass`, otherwise 1. A pass requires all three reads, each with literal `vetoed: false`, the exact provider address and exact recomputed payload hash. Envelope, receipt and stream hashes are ignored. Require both the printed pass and exit 0 before routing; anything else blocks processing. Missing anchors remain pending until the next routine.
