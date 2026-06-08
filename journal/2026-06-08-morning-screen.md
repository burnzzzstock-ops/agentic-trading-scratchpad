# Journal — Morning screen — 2026-06-08

Pre-market alert screen run via `screener.py`. Read-only data gathering;
no orders. Account: agentic cash ••••3540 (settled cash $0 — every name still
BLOCKs at fire time until the pending deposit settles).

## Universe
34 candidates = Daily Movers (20) + "My First List" (13) + "Watching" (1, CVNA),
deduped. Live quotes + tradability pulled per name.

## Filters
- `min_gap_pct = 3.0`  (drop sub-3% movers as dead tape)
- `min_vol_x = 0`      (rel-volume not sourceable from connector — disabled)
- `whole_share_only = True`  (validator default; ask > $100/trade cap → SKIP)
- float / rel-vol: **not available** from Robingood tools → left null. Null float
  caps every otherwise-clean name at MANUAL_WATCH (cannot confirm ≥10M).

## Result: 0 AUTO · 14 MANUAL · 20 SKIP

**MANUAL_WATCH (14, ranked by gap%):**
TNGX (+55.6), ENHA (+48.3), ABAT (+29.6), GLXY (+23.3), NNNN (−20.6, spread
1.42% manual band), IXHL (+20.2), ALVO (+19.6), KSIOF (+18.9), FAC (+17.5),
TOYO (−16.5), WLTH (−16.1), BRC (−15.4), GHM (−14.4, spread 1.28% manual band),
CVNA (+4.1).

**AUTO_WATCH (0):** none — float is unknown for the whole universe, so nothing
can confirm ≥10M. Wiring a float source is the highest-leverage upgrade; it
would flip the tight-spread ≥$2 names (GLXY, ABAT, ALVO, IXHL, ENHA, …) to AUTO.

**SKIP (20), by reason:**
- 1 whole share over $100/trade cap (NEW gate): CBRS ($243), TSLA ($411)
- spread > 2% hard veto: NDVLY (72%), COGNY (16.6%), TCLCF (11.6%), HAYPY (11.1%), WACLY (8.5%)
- price < $1 floor: LAB, GPRO, NDVLY, COGNY
- below 3% gap floor: AAPL, MSFT, META, NFLX, DIS, SBUX, F, GE, BABA, BAC, SNAP

## Watchlist action
Synced the **"Watching"** list (••••d0d0) to the 14 MANUAL_WATCH names. Left
"My First List" untouched — that's the user's personal large-cap list, not the
scanner alert universe; its names SKIP'd here on the momentum floor by design,
not because they're bad holds.

## Code change this run
Added a per-trade-affordability gate to `screener.py` (+ 4 tests): a name whose
ask exceeds the imported `HARD_PER_TRADE_NOTIONAL` ($100) is SKIP'd under the
whole-share default, since it would BLOCK at fire time. `--allow-fractional`
opts out. Surfaced directly by this dry run (CBRS/TSLA were mislabeled watch-able).

## Outcome
Alert universe set to 14 names for the session. No trades — settled cash $0 and
every name is MANUAL at best. Re-run this screen each morning to refresh.
