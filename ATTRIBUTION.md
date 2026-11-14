# Attribution

Signals Desk is a fork of [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk), created by Andrew Wilkinson and Galleon Labs and released under the MIT license. Their copyright notice and the full license text are in [LICENSE](LICENSE) and apply to everything in this repository that comes from their work.

## What comes from HyperGrok Trading Desk

The retained repository layout and skill structure, and every file not listed under "What we changed", are theirs, unchanged or lightly edited. The unmodified original is the `main` branch of this repository and the upstream repository linked above.

## What we changed

- The seven original desk roles in `agents/` (desk-lead, execution-trader, market-analyst, research-analyst, risk-manager, strategist, trade-reviewer) are not used on this branch and have been removed from it. They remain, unchanged, on `main` and upstream.
- In their place this branch has five roles written by TraderSwift: Desk Lead, Position Sizer, Risk Gate, Executor and Reviewer, in `agents/` and `skills/`. They follow the original's structure; the text is ours.
- SETUP.md is rewritten as a public guide to the TraderSwift demo desk: it sets up the five roles above on a Grok account, with Signer in place of a local API wallet and routines in UTC. The README and skills index describe this fork and retain the upstream credit.
- The `desk/` Python runtime, configuration example, documentation and scenario checks implement and document the signal consumer, sizing, policy checks, delegate execution and review.
- Removed unused upstream packaging and plugin manifests, release tooling and workflows, installation and product documentation, role-oriented skills, market research utilities and their tests, approval hooks and rules, public bot template, issue templates and promotional assets; also removed the broken notes symlink.
- Orders are not signed with a local API wallet. They are sent to Signer, which signs under an on-chain policy.
- The approval model differs from the original: an order is signed only when it meets the delegate's on-chain policy on signer-1 (asset caps, daily size, expiry), evaluated by the signer set; there is no local wallet and no human click per order. For that reason, and as the original's authors ask of such forks, this project has its own name.

## Our changes

Copyright (c) 2026 TraderSwift, for the modifications, released under the same MIT license.

Attribution does not imply endorsement. Nothing here is represented as the original authors' work beyond what is stated above, and nothing of theirs is represented as ours.
