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
- `news_filter.py` — free, no-key catalyst filter. Checks SEC 8-K filings
  (verified → may grade A) then Google News RSS (evidence only → capped at A-,
  i.e. routes to manual). Flags dilution/offering/distress language, which the
  validator vetoes. `python3 news_filter.py <TICKER> --company "<name>"`.
  Keyword sentiment is a screen, not proof — it has false positives, which is
  why news-only never auto-fires. Needs `news.google.com` + `sec.gov` allowed.
- `screener.py` — pure-logic **pre-market screener**. Imports its thresholds
  from `validator.py` so the morning watchlist can't drift from the fire-time
  gate. Buckets a live candidate universe into AUTO_WATCH / MANUAL_WATCH / SKIP
  on the dimensions knowable pre-open (price, spread, tradability, float,
  optional gap/vol floors). Catalyst + R:R stay per-fire.
  `echo '{"candidates":[…]}' | python3 screener.py --json`.
- `test_validator.py` / `test_news_filter.py` / `test_screener.py` — 27 + 12 + 15 tests.
- `ROUTINE_PROMPT.md` — the fire-time prompt + Zapier wiring for the cloud routine.
- `MORNING_SCREEN_PROMPT.md` — the once-a-morning prompt that builds the live
  candidate universe, runs `screener.py`, and syncs the watchlist.

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
