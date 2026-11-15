# Signals Desk

Based on [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk) by [Andrew Wilkinson](https://andrewwilkinson.io) and [Galleon Labs](https://github.com/galleonlabs). Used under the MIT license. This is a separate project with its own name because it changes the approval model; it is not HyperGrok and is not affiliated with or endorsed by its authors. See [ATTRIBUTION.md](ATTRIBUTION.md).

Signals Desk is the TraderSwift demo desk for sizing, checking and executing published trade signals through a Signer delegate policy on signer-1. It uses a locally generated delegate key, never a venue signing key. The `main` branch mirrors upstream unchanged; this branch, `signer`, carries our changes. The `traderswift` branch adds chain verification: before intake hands a signal on, it checks the publisher's charter, provider record and the signal's anchor on `https://chain.traderswift.io`.

The Signals service is private. Obtain access and a subscriber token from the signal publisher; this repository does not provide a public signal feed.

Follow [SETUP.md](SETUP.md) to configure five Grok bots in a private Trading Floor: Desk Lead, Position Sizer, Risk Gate, Executor and Reviewer. Their [five skills](skills/README.md) use the shared Python modules in `desk/`. Intake trusts the served publication; it does not verify publisher registration, veto status or receipt anchoring on a chain.

Routine-driven bots currently refuse new entries with `no_resting_stop_until_rotation`. They must keep `resident: false`; selecting `trigger` does not implement resting stop protection. The continuously running executor supports software exits as documented in [desk/README.md](desk/README.md).

[desk/README.md](desk/README.md) describes requirements, configuration, commands and the fake-service scenario checks. From `desk/`, run `make check` in an environment with local socket access and Redis available. Setup and rehearsal place no live orders.

Report problems through this repository's issues with a reproduction and sanitized output. Keep credentials and private account details out of reports.
