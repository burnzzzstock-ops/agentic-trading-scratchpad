# agentic-trading-scratchpad

Validator / Risk layer for an asymmetric intraday trading pipeline. The Pine
scanner (Scanner Agent) surfaces candidates as JSON alerts; this repo is the
deterministic second half that tries to kill them, sizes the survivors, and
decides the autonomy tier.

## Pipeline

```
TradingView Pine scanner  ──alert(JSON)──▶  Zapier (Webhooks → POST)
        │                                          │
        │                                          ▼
        │                          Routine /fire endpoint  (text = payload)
        │                                          │
        ▼                                          ▼
  candidate surfacer                  Claude Code routine session
                                       1. parse payload
                                       2. fetch live data (Robingood MCP)
                                       3. python validator.py  ◀── this repo
                                       4. BLOCK / MANUAL / FULL_AUTO
                                       5. review → place → bracket → journal
```

The scanner cannot see catalyst, float, live spread, or real overhead
resistance, and its `rr_modeled` is tautological (target is defined as
`entry + (entry-stop)·RR`). The validator re-derives executable R:R against a
live entry + slippage, and blocks anything missing a verified catalyst.

## Files

- `validator.py` — pure-logic gate. No network I/O; the agent feeds it live
  data. Run `echo '{"payload":{…},"context":{…}}' | python3 validator.py`.
  Exit codes: `0` FULL_AUTO, `10` MANUAL_CONFIRM, `20` BLOCK.
- `test_validator.py` — 27 tests. Run `python3 test_validator.py`.
- `ROUTINE_PROMPT.md` — the prompt + Zapier wiring for the cloud routine.

## Spec conflicts (resolved to the stricter reading)

The operating spec contradicts itself in a few places; `validator.py` picks
the conservative interpretation and documents each inline:

| Topic | Conflict | Resolution |
|---|---|---|
| Per-trade size | $100 vs $250 ceiling | hard block above **$100** |
| Daily loss | "4% ($10)" vs "$500" | **4% of equity** = $10 |
| Float manual gate | <5M vs <10M | manual under **10M**, quarantine under 5M |
| Price manual gate | <$1 vs <$2 | manual under **$2** |
| Halt-prone | veto vs manual | **block** (absolute veto) |

## Guardrail note

A routine runs autonomously with no per-call approval prompts, so
`validator.py` is the only runtime guardrail — keep the gates in code, not in
prose the model has to remember. Recommended default: let FULL_AUTO place only
the narrow validated set; treat everything else as manual until trust is built.
