"""Pre-market screener — picks which tickers deserve an alert today.

Companion to ``validator.py``. The validator runs *per fire* and decides
whether to trade a single candidate. This module runs *once in the morning*
over a candidate universe (movers / gappers / your current watchlist) and
decides which names are even worth wiring an alert to — i.e. which ones
*could* survive the validator later, on the dimensions that are knowable
before the open.

Design contract (same as validator.py)
---------------------------------------
NO network I/O. Pure, deterministic, unit-testable. The morning routine
fetches live data via the Robingood tools (quote/spread, tradability, and
float where sourceable) and feeds a list of ``Candidate`` records in. The
thresholds are *imported from validator.py* so the screen can never drift
out of sync with the gate it is trying to pre-filter for.

What this screens (knowable pre-open):
  * tradable this session + valid two-sided market
  * price band            (>= $1 floor; >= $2 auto, $1-$2 manual)
  * spread                (<= 1.25% auto, 1.25-2% manual, > 2% rejected)
  * float                 (>= 10M auto, 5-10M manual, < 5M quarantine)
  * momentum prefilter    (optional gap_pct / vol_x floors to drop dead tape)

What it deliberately does NOT screen — these are FIRE-TIME, per validator:
  * verified <24h catalyst   (news_filter.py at fire time)
  * executable R:R           (needs the live entry/stop/target of the setup)
  * settled cash / trade count / daily loss
A name can clear this screen and still BLOCK at fire time. That is correct:
the screen decides *where to point alerts*, not whether to trade.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Optional

# Single source of truth: pull the live gate's thresholds so the morning
# screen and the fire-time validator can never disagree.
from validator import (
    APPROVED_SETUPS,
    FLOAT_FULLAUTO_MIN,
    FLOAT_QUARANTINE,
    PRICE_MIN,
    PRICE_MANUAL_BELOW,
    SPREAD_AUTO_MAX,
    SPREAD_MANUAL_MAX,
    _spread_pct,
)

# Momentum prefilter defaults — a name that isn't moving isn't worth an alert
# today. Tunable per morning via CLI flags / the "filters" block. Set to 0 to
# disable (e.g. pre-open when gap/vol aren't populated yet).
DEFAULT_MIN_GAP_PCT = 0.0
DEFAULT_MIN_VOL_X = 0.0

# Tiers
AUTO = "AUTO_WATCH"      # could reach FULL_AUTO at fire time
MANUAL = "MANUAL_WATCH"  # alert it, but expect MANUAL_CONFIRM (sub-$2, thin, low/unknown float)
SKIP = "SKIP"            # don't waste an alert slot — structurally disqualified


@dataclass
class Candidate:
    """One name in the morning universe, with the live data the screen needs."""

    ticker: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    prev_close: float = 0.0
    float_shares: Optional[int] = None
    tradable: bool = True
    fractional: bool = False          # opening fractional eligibility (informational)
    gap_pct: Optional[float] = None   # vs prev close; momentum prefilter + rank
    vol_x: Optional[float] = None     # relative volume; momentum prefilter + rank
    name: str = ""                    # company name, for the fire-time news filter

    @classmethod
    def parse(cls, data) -> "Candidate":
        d = json.loads(data) if isinstance(data, (str, bytes)) else dict(data)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Filters:
    min_gap_pct: float = DEFAULT_MIN_GAP_PCT
    min_vol_x: float = DEFAULT_MIN_VOL_X


@dataclass
class ScreenResult:
    ticker: str
    tier: str
    score: float
    reasons: list = field(default_factory=list)   # why MANUAL / SKIP
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "tier": self.tier,
            "score": self.score,
            "reasons": self.reasons,
            "metrics": self.metrics,
        }


def _ref_price(c: Candidate) -> float:
    """Mirror validator entry logic: marketable-limit buyer pays the ask."""
    return c.ask if c.ask and c.ask > 0 else c.last


def _momentum_score(c: Candidate) -> float:
    """Rough rank for ordering the morning list — bigger move + more volume
    floats to the top. Not a gate, just a sort key."""
    gap = abs(c.gap_pct) if c.gap_pct is not None else 0.0
    vol = c.vol_x if c.vol_x is not None else 0.0
    return round(gap + 2.0 * vol, 3)


def screen_one(c: Candidate, filt: Filters) -> ScreenResult:
    reasons: list = []
    price = _ref_price(c)
    spread_pct = _spread_pct(c.bid, c.ask)
    score = _momentum_score(c)

    metrics = {
        "price_ref": round(price, 4) if price else None,
        "spread_pct": round((spread_pct or 0) * 100, 3) if spread_pct is not None else None,
        "float_shares": c.float_shares,
        "gap_pct": c.gap_pct,
        "vol_x": c.vol_x,
        "fractional": c.fractional,
    }

    # ---- Structural disqualifiers -> SKIP (don't point an alert here) ----
    if not c.tradable:
        reasons.append("not tradable this session")
    if spread_pct is None:
        reasons.append("no valid two-sided market (bid/ask)")
    elif spread_pct > SPREAD_MANUAL_MAX:
        reasons.append(f"spread {spread_pct*100:.2f}% over 2.00% hard veto")
    if not price or price < PRICE_MIN:
        reasons.append(f"price ${price:.2f} under ${PRICE_MIN:.2f} floor")

    # Momentum prefilter (only when the data is present — pre-open it may not be)
    if c.gap_pct is not None and abs(c.gap_pct) < filt.min_gap_pct:
        reasons.append(f"gap {c.gap_pct:.1f}% under {filt.min_gap_pct:.1f}% min")
    if c.vol_x is not None and c.vol_x < filt.min_vol_x:
        reasons.append(f"rel-vol {c.vol_x:.1f}x under {filt.min_vol_x:.1f}x min")

    if reasons:
        return ScreenResult(c.ticker, SKIP, score, reasons, metrics)

    # ---- Survivors: AUTO vs MANUAL on the validator's own thresholds ----
    if price < PRICE_MANUAL_BELOW:
        reasons.append(f"price ${price:.2f} under ${PRICE_MANUAL_BELOW:.0f} (manual)")
    if spread_pct > SPREAD_AUTO_MAX:
        reasons.append(f"spread {spread_pct*100:.2f}% in 1.25-2.00% band (manual)")
    if c.float_shares is None:
        reasons.append("float unknown — cannot confirm >= 10M (manual)")
    elif c.float_shares < FLOAT_QUARANTINE:
        reasons.append(f"micro-float {c.float_shares:,} < 5M (quarantine/manual)")
    elif c.float_shares < FLOAT_FULLAUTO_MIN:
        reasons.append(f"float {c.float_shares:,} under 10M (manual)")

    tier = AUTO if not reasons else MANUAL
    return ScreenResult(c.ticker, tier, score, reasons, metrics)


def screen(candidates: list, filt: Filters) -> list:
    results = [screen_one(c, filt) for c in candidates]
    rank = {AUTO: 0, MANUAL: 1, SKIP: 2}
    # AUTO first, then MANUAL, then SKIP; within a tier, hottest momentum first.
    results.sort(key=lambda r: (rank[r.tier], -r.score))
    return results


def results_to_text(results: list, top: Optional[int] = None) -> str:
    counts = {AUTO: 0, MANUAL: 0, SKIP: 0}
    for r in results:
        counts[r.tier] += 1
    shown = results if top is None else results[:top]
    lines = [
        f"Screened {len(results)} candidates -> "
        f"{counts[AUTO]} auto-watch, {counts[MANUAL]} manual-watch, {counts[SKIP]} skip",
        "(catalyst + R:R are NOT screened here — checked per-fire by validator.py)",
        "",
    ]
    for r in shown:
        m = r.metrics
        head = (
            f"  [{r.tier}] {r.ticker:<6} ${m['price_ref']}  "
            f"spread {m['spread_pct']}%  float {m['float_shares']}  "
            f"gap {m['gap_pct']}  vol {m['vol_x']}x  score {r.score}"
        )
        lines.append(head)
        for why in r.reasons:
            lines.append(f"        - {why}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI: reads {"candidates": [...], "filters": {...}} from stdin or --input
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-market alert screener")
    ap.add_argument("--input", help="JSON file; default stdin")
    ap.add_argument("--json", action="store_true", help="emit JSON not text")
    ap.add_argument("--min-gap", type=float, default=None, help="min |gap_pct| to keep")
    ap.add_argument("--min-vol", type=float, default=None, help="min rel-vol (x) to keep")
    ap.add_argument("--top", type=int, default=None, help="only show the top N")
    args = ap.parse_args(argv)

    raw = open(args.input).read() if args.input else sys.stdin.read()
    obj = json.loads(raw)
    cand_data = obj["candidates"] if isinstance(obj, dict) else obj
    candidates = [Candidate.parse(c) for c in cand_data]

    fblock = obj.get("filters", {}) if isinstance(obj, dict) else {}
    filt = Filters(
        min_gap_pct=args.min_gap if args.min_gap is not None
        else fblock.get("min_gap_pct", DEFAULT_MIN_GAP_PCT),
        min_vol_x=args.min_vol if args.min_vol is not None
        else fblock.get("min_vol_x", DEFAULT_MIN_VOL_X),
    )

    results = screen(candidates, filt)
    out = results if args.top is None else results[: args.top]

    if args.json:
        print(json.dumps([r.to_dict() for r in out], indent=2))
    else:
        print(results_to_text(results, top=args.top))
    # Exit 0 if anything is alert-worthy, 1 if the whole universe is SKIP.
    return 0 if any(r.tier in (AUTO, MANUAL) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
