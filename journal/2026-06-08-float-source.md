# Journal — Float source wired — 2026-06-08

Added `float_source.py`: free, no-key float estimator using SEC data only
(`sec.gov`/`data.sec.gov`, already allowlisted — no new network permission).

## Method (safe by construction)
- Primary: `dei:EntityPublicFloat` (public float $, non-affiliate) ÷ live price
  → real float-in-shares estimate, capped at shares outstanding.
- Fallback: `dei:EntityCommonStockSharesOutstanding` used ONLY as a sub-10M
  upper bound (shares-out ≥ float, so it can reject the gate, never confirm it).
- Foreign/OTC/recent-IPO or public-float=0 → `float_shares: null` → MANUAL.
- 8 unit tests on the pure `decide_float` core; all green. Validator 27,
  screener 19 still green.

## Re-screen impact (same 14-name watch set, float now populated)
Before float: 0 AUTO / 14 MANUAL. After: **5 AUTO / 9 MANUAL / 0 SKIP.**

AUTO_WATCH (now float-confirmed ≥10M, ≥$2, spread ≤1.25%):
- TNGX  float≈16.8M  (public float $528M @ $31.5)
- ABAT  float≈51.5M  ($208M @ $4.04)
- GLXY  float≈116M   ($3.6B @ $30.99)
- BRC   float≈42.3M
- CVNA  float≈632M

Correctly held at MANUAL by genuine low float (the safety win):
- IXHL  float≈8.3M  (under 10M)
- GHM   float≈6.2M  (under 10M; also spread 1.28% manual band)
Held at MANUAL by unknown float (foreign/OTC/IPO): ENHA, ALVO, KSIOF, FAC,
TOYO, WLTH, NNNN.

## Caveats (recorded honestly)
- Estimate is stale: public float is an annual/quarterly cover value measured
  at that filing's price, divided here by today's price — order-of-magnitude,
  not exact. The validator's 5–10M manual / <5M quarantine bands absorb slop,
  and AUTO still re-runs the full gate at fire time.
- Yahoo / stockanalysis (live exact float) are blocked by the network policy;
  SEC is the only reachable free source. If a paid/allowlisted float feed is
  added later, swap the primary in `float_source.py` — the screener interface
  (`float_shares`) is unchanged.

## Watchlist
No change needed — all 14 names remain AUTO or MANUAL (none dropped to SKIP),
so "Watching" (••••d0d0) is still the correct alert universe.

## Outcome
Float gate is now data-backed. Still no trades — settled cash $0; AUTO names
will MANUAL/BLOCK at fire time until the deposit settles and a real catalyst
appears.
