# Signals Desk

Based on [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk) by [Andrew Wilkinson](https://andrewwilkinson.io) and [Galleon Labs](https://github.com/galleonlabs). Used under the MIT license. This is a separate project with its own name because it changes the approval model; it is not HyperGrok and is not affiliated with or endorsed by its authors. See [ATTRIBUTION.md](ATTRIBUTION.md).

Signals Desk is the TraderSwift demo desk for sizing, checking and executing published trade signals through Signer under a delegate policy. It uses a locally generated delegate key, never a venue signing key. The `main` branch mirrors upstream unchanged; this branch, `signer`, carries our changes. The `traderswift` branch adds chain verification: before intake hands a signal on, it checks the publisher's charter, provider record and the signal's anchor on the TraderSwift chain ([traderswift-chain](https://github.com/traderswift-io/traderswift-chain)).

The Signals service is private. Obtain access and a subscriber token from the signal publisher; this repository does not provide a public signal feed.

Follow [SETUP.md](SETUP.md) to configure five Grok bots in a private Trading Floor: Desk Lead, Position Sizer, Risk Gate, Executor and Reviewer. Their [five skills](skills/README.md) use the shared Python modules in `desk/`. Intake trusts the served publication; it does not verify publisher registration, veto status or receipt anchoring on a chain.

Routine-driven bots keep `resident: false` and submit a GTC entry, resting reduce-only stop and optional take-profit in one bracket. One UTC routine runs at :01, :16, :31 and :46, beginning with `desk_wake`; no work means no posting. Brackets need a Signer release that signs the bracket kind; until then the signer refuses the kind and the desk records `bracket_refused`. The continuously running executor retains software exits. See [desk/README.md](desk/README.md) for candle-close behavior and the unresolved scheduled-cancel protection limitation.

[desk/README.md](desk/README.md) describes requirements, configuration, commands and the fake-service scenario checks. From `desk/`, run `make check` in an environment with local socket access and Redis available. Setup and rehearsal place no live orders.

Report problems through this repository's issues with a reproduction and sanitized output. Keep credentials and private account details out of reports.
