# Signals Desk skills

The TraderSwift demo desk uses five skills, one per bot. Install each complete `SKILL.md` from the same reviewed checkout as its [role prompt](../agents/) and shared `desk/` modules. Follow [SETUP.md](../SETUP.md) for configuration and UTC routines.

| Bot | Skill | Responsibility |
| --- | --- | --- |
| Desk Lead | [signals-desk-intake](signals-desk-intake/SKILL.md) | Read the private signal service, verify each signal on the chain (charter, provider, anchor) with `desk_anchor_verify`, and route only unchanged envelopes that pass. |
| Position Sizer | [signals-position-sizing](signals-position-sizing/SKILL.md) | Report shared sizing results or refusals. |
| Risk Gate | [signals-risk-gate](signals-risk-gate/SKILL.md) | Report the local policy check and unsigned intent preview. |
| Executor | [signals-execution](signals-execution/SKILL.md) | Generate its delegate key, execute eligible intents and cancel through the shared client. |
| Reviewer | [signals-review](signals-review/SKILL.md) | Report recorded fills, slippage and stop mode. |

The signal publisher supplies the subscriber token. Only the Executor holds the delegate seed and door access credential. A policy preview does not authorize bypassing execution checks; routine-driven entries remain disabled until resting stop support is implemented. No skill implements its own sizing, policy or signing path.

The skill layout derives from the upstream project; see [ATTRIBUTION.md](../ATTRIBUTION.md) and [LICENSE](../LICENSE).
