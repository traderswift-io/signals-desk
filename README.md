# Signals Desk

Based on [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk) by [Andrew Wilkinson](https://andrewwilkinson.io) and [Galleon Labs](https://github.com/galleonlabs). Used under the MIT license. This is a separate project with its own name because it changes the approval model; it is not HyperGrok and is not affiliated with or endorsed by its authors. See [ATTRIBUTION.md](ATTRIBUTION.md).

Chain verification runs once at the Desk Lead's intake through `desk_anchor_verify`, which journals a pass bound to the envelope's payload hash; with `resident: false`, the routine Executor `run` and the `desk_ticket` gate and size previews refuse `anchor_not_verified` without a matching record; the other seats act on the envelope the Lead routed; the resident signal consumer does not run the verifier.

The Signals service anchors nothing on chain yet (`receipt: null`), so today the verifier refuses every signal on this branch until anchors are published.

Signals Desk is the TraderSwift demo desk for sizing, checking and executing published trade signals through Signer under a delegate policy. It uses a locally generated delegate key, never a venue signing key. The `main` branch mirrors upstream unchanged; `signer` carries our changes; this branch, `traderswift`, adds chain verification at the Desk Lead's routine intake and a matching journal requirement for routine execution and previews.

The Signals service is private. Obtain access and a subscriber token from the signal publisher; this repository does not provide a public signal feed.

Follow [SETUP.md](SETUP.md) to configure five Grok bots in a private Trading Floor: Desk Lead, Position Sizer, Risk Gate, Executor and Reviewer. Their [five skills](skills/README.md) use the shared Python modules in `desk/`. Before handing a signal on, intake reads the public chain endpoint `https://chain.traderswift.io` three times: the `signals` charter, the pinned provider (registration, veto and address) and the signal's anchor (veto and hash). Any failed read refuses the signal by name.

Routine-driven bots keep `resident: false` and submit a GTC entry, resting reduce-only stop and optional take-profit in one bracket. One UTC routine runs at :01, :16, :31 and :46, beginning with `desk_wake`; no work means no posting. Brackets need a Signer release that signs the bracket kind; until then the signer refuses the kind and the desk records `bracket_refused`. The continuously running executor retains software exits. See [desk/README.md](desk/README.md) for candle-close behavior and the unresolved scheduled-cancel protection limitation.

[desk/README.md](desk/README.md) describes requirements, configuration, commands and the fake-service scenario checks. From `desk/`, run `make check` in an environment with local socket access and Redis available. Setup and rehearsal place no live orders.

Report problems through this repository's issues with a reproduction and sanitized output. Keep credentials and private account details out of reports.
