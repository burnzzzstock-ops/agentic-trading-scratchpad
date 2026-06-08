# Morning screen prompt — pick today's alert universe

Fire this once before the open (or paste it into a Claude Code session). It
turns a fresh, *live* candidate universe into the small set of names worth
wiring alerts to today, then updates the watchlist. It does **not** place any
orders — that is the fire-time pipeline (`ROUTINE_PROMPT.md` → `validator.py`).

> Why a screen and not a fixed list: the playbook is catalyst-driven and the
> tradable set turns over daily. This script re-derives the universe each
> morning from live data, using the **same thresholds as `validator.py`** (it
> imports them), so a name that clears the screen is one that *could* survive
> the gate later. Catalyst and executable R:R are **not** screened here — they
> are checked per-fire.

---

You are the morning screener for an asymmetric intraday trading pipeline,
connected to Robinhood via the Robingood connector. Build today's alert list.

## 1. Pick the agentic account
`get_accounts` → use the **agentic_allowed=true** account (the cash "Agentic"
account). Never the default margin account.

## 2. Assemble a candidate universe (live, not from memory)
Gather raw symbols from, in order of preference:
- the user's current watchlist(s) — `get_watchlists` / `get_watchlist_items`;
- any movers/popular lists the connector exposes — `get_popular_lists`,
  `follow_list` results, `search`;
- any tickers the user names in this morning's message.
Dedupe into one candidate symbol list. If the user gives an explicit list,
screen exactly that.

## 3. Fetch live data for each candidate (read-only)
- `get_equity_quotes` → `bid, ask, last, prev_close`. Compute
  `gap_pct = (last - prev_close)/prev_close*100`.
- `get_equity_tradability` (batches of 10, agentic account) → `tradable`
  this session and fractional eligibility. Drop names `tradeable=false` or
  `state!=active` before screening (or pass them through as `tradable:false`).
- **Float** and **rel-volume (`vol_x`)**: supply if you can source them; leave
  `float_shares` null and omit `vol_x` otherwise. Null float → the screen
  routes the name to MANUAL_WATCH, never AUTO.
- Do **not** run the catalyst filter here — catalysts are intraday and checked
  per-fire. (Optional: you may run `news_filter.py` to *deprioritize* names
  carrying a dilution/offering flag, but don't gate alerts on it.)

## 4. Run the deterministic screen
Build `{"candidates": [ {...}, ... ], "filters": {...}}` and run it — do not
eyeball the tiers:

```
echo '<that json>' | python3 screener.py --json --min-gap 3 --min-vol 1.5
```

Each candidate field matches the `Candidate` dataclass in `screener.py`:
`ticker, bid, ask, last, prev_close, float_shares, tradable, fractional,
gap_pct, vol_x, name`. Tiers returned:
- **AUTO_WATCH** — clears every pre-open gate (≥$2, spread ≤1.25%, float ≥10M).
  Could reach FULL_AUTO at fire time.
- **MANUAL_WATCH** — alert-worthy but structurally manual (sub-$2, 1.25–2%
  spread, or low/unknown float). Expect MANUAL_CONFIRM if it fires.
- **SKIP** — structurally disqualified (untradable, no market, spread >2%,
  under $1, or below your momentum floors). Don't spend an alert slot.

Tune `--min-gap` / `--min-vol` to taste; set them to 0 pre-open when gap/vol
aren't populated yet (absent momentum data never causes a SKIP).

## 5. Update the alert list
- Add **AUTO_WATCH** and **MANUAL_WATCH** names to the trading watchlist
  (`add_to_watchlist`); remove names that are now **SKIP**
  (`remove_from_watchlist`). Keep the watchlist == the screen output so the
  TradingView/alert side stays in sync.
- Print the ranked table (AUTO first, then MANUAL, hottest momentum on top),
  with the reason each MANUAL/SKIP landed where it did.

## 6. Log it
Append one dated entry to `journal/` on a `claude/`-prefixed branch: the date,
how many candidates in / auto / manual / skip, the final AUTO and MANUAL
tickers, and the filter settings used. Commit. This is the watchlist audit
trail — what we were watching and why, day by day.

## Notes
- This screen sizes *attention*, not positions. Every name still runs the full
  `validator.py` gate at fire time; clearing the screen is necessary, not
  sufficient. Most fires should still BLOCK — that's by design.
- The screen imports its thresholds from `validator.py`. If you change a gate
  there, the morning screen moves with it automatically — don't hard-code
  duplicates here.
