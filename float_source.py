"""Free float estimator for the Validator pipeline.

Supplies the `float_shares` field that `validator.py` / `screener.py` need to
clear the float gate, using only free, no-API-key SEC data (same hosts the
catalyst filter already requires — `sec.gov` + `data.sec.gov`; no new
allowlist entry). No paid float feed, no Yahoo scrape.

What SEC actually gives us, and how we use it SAFELY
----------------------------------------------------
SEC does NOT publish "tradable float in shares" directly. Two cover-page facts
are available via the XBRL `companyconcept` API:

  * dei:EntityPublicFloat                     -> public float in **dollars**
        (aggregate market value of shares held by non-affiliates, as of a past
         filing date). Divided by a live price this yields a real float-in-
         **shares** estimate. This is the good signal.
  * dei:EntityCommonStockSharesOutstanding    -> **shares outstanding** (count).
        Always >= float. So it can only ever *reject* the >=10M gate, never
        *confirm* it — a 50M-shares-out name can still have 3M tradable float.

Decision (pure, unit-tested in decide_float):
  1. public-float-$ and a price  -> float_est = $float / price, capped at
        shares-outstanding. confidence="estimate".
  2. only shares-outstanding, and it is *below* the 10M gate -> emit it as a
        safe upper bound (float <= it < gate, so routing stays conservative).
        confidence="upper_bound".
  3. otherwise (no public float, or shares-out >= gate with no float number)
        -> None. Unknown float routes to MANUAL by design. confidence="none".

Failures degrade to None (unknown -> manual), mirroring news_filter's
fail-safe posture. The estimate is stale (annual/quarterly cover date) and the
price basis differs from the as-of date, so it is labelled an estimate, not a
fact; the validator's 5-10M manual / <5M quarantine bands absorb the slop.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# Reuse the float gate the estimate is feeding, so the two never drift.
from validator import FLOAT_FULLAUTO_MIN

USER_AGENT = "agentic-trading-scratchpad/1.0 (research; contact: ops@example.com)"
HTTP_TIMEOUT = 12.0

_TICKER_MAP_CACHE: Optional[dict] = None


@dataclass
class FloatResult:
    ticker: str
    float_shares: Optional[int]
    method: Optional[str]
    confidence: str                       # estimate | upper_bound | none
    public_float_usd: Optional[float] = None
    public_float_asof: Optional[str] = None
    shares_outstanding: Optional[int] = None
    shares_outstanding_asof: Optional[str] = None
    price_used: Optional[float] = None
    cik: Optional[int] = None
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "float_shares": self.float_shares,
            "method": self.method,
            "confidence": self.confidence,
            "public_float_usd": self.public_float_usd,
            "public_float_asof": self.public_float_asof,
            "shares_outstanding": self.shares_outstanding,
            "shares_outstanding_asof": self.shares_outstanding_asof,
            "price_used": self.price_used,
            "cik": self.cik,
            "notes": self.notes,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- #
# Pure decision core (no network) — this is what the tests exercise.
# --------------------------------------------------------------------------- #
def decide_float(public_float_usd: Optional[float],
                 price: Optional[float],
                 shares_outstanding: Optional[int],
                 gate_min: int = FLOAT_FULLAUTO_MIN):
    """Return (float_shares|None, method|None, confidence, notes)."""
    notes: list = []

    # 1) Real non-affiliate float, in shares, from $float / price.
    if public_float_usd and public_float_usd > 0 and price and price > 0:
        est = public_float_usd / price
        if shares_outstanding and shares_outstanding > 0 and est > shares_outstanding:
            notes.append(
                f"estimate {est:,.0f} capped at shares outstanding {shares_outstanding:,}"
            )
            est = float(shares_outstanding)
        notes.append("float estimated from SEC public-float $ / live price (stale cover date)")
        return int(round(est)), "sec_public_float/price", "estimate", notes

    if public_float_usd is not None and public_float_usd <= 0:
        notes.append("SEC public float reported as 0 (recent IPO / not yet computed) — unusable")
    if public_float_usd and public_float_usd > 0 and not (price and price > 0):
        notes.append("have public float $ but no price to convert to shares")

    # 2) Shares outstanding as a safe upper bound — only when it ALREADY fails
    #    the gate (float <= shares_out < gate), so routing stays conservative.
    if shares_outstanding and shares_outstanding > 0:
        if shares_outstanding < gate_min:
            notes.append(
                f"only shares-outstanding {shares_outstanding:,} available; it is "
                f"below the {gate_min:,} gate so it bounds float under the threshold"
            )
            return int(shares_outstanding), "sec_shares_outstanding_upper_bound", "upper_bound", notes
        notes.append(
            f"shares outstanding {shares_outstanding:,} >= gate; cannot confirm "
            f"float (possible low-float) — left unknown"
        )
    else:
        notes.append("no SEC public float or shares-outstanding available")

    return None, None, "none", notes


# --------------------------------------------------------------------------- #
# Network (best-effort; failures degrade to unknown float -> MANUAL).
# --------------------------------------------------------------------------- #
def _get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read()


def _cik_for(ticker: str) -> Optional[int]:
    global _TICKER_MAP_CACHE
    if _TICKER_MAP_CACHE is None:
        _TICKER_MAP_CACHE = json.loads(
            _get("https://www.sec.gov/files/company_tickers.json"))
    for row in _TICKER_MAP_CACHE.values():
        if row.get("ticker", "").upper() == ticker.upper():
            return int(row["cik_str"])
    return None


def _latest_concept(cik: int, tag: str, taxonomy: str = "dei"):
    """Return (value, end_date, form) for the most recent reported value, or None."""
    url = (f"https://data.sec.gov/api/xbrl/companyconcept/"
           f"CIK{cik:010d}/{taxonomy}/{tag}.json")
    try:
        data = json.loads(_get(url))
    except Exception:
        return None
    best = None  # (end_date, value, form)
    for arr in data.get("units", {}).values():
        for a in arr:
            end = a.get("end")
            val = a.get("val")
            if end is None or val is None:
                continue
            if best is None or end > best[0]:
                best = (end, val, a.get("form"))
    return best


def get_float(ticker: str, price: Optional[float] = None) -> FloatResult:
    errors: list = []
    cik = None
    try:
        cik = _cik_for(ticker)
    except Exception as e:
        errors.append(f"cik lookup: {e}")

    if cik is None:
        return FloatResult(
            ticker=ticker, float_shares=None, method=None, confidence="none",
            price_used=price, cik=None,
            notes=["ticker not in SEC company map (foreign/OTC/ADR) — float unknown"],
            errors=errors,
        )

    pf = so = None
    try:
        pf = _latest_concept(cik, "EntityPublicFloat")
    except Exception as e:
        errors.append(f"public_float: {e}")
    try:
        so = _latest_concept(cik, "EntityCommonStockSharesOutstanding")
    except Exception as e:
        errors.append(f"shares_outstanding: {e}")

    pf_val = pf[1] if pf else None
    so_val = int(so[1]) if so else None
    fshares, method, conf, notes = decide_float(pf_val, price, so_val)

    return FloatResult(
        ticker=ticker,
        float_shares=fshares,
        method=method,
        confidence=conf,
        public_float_usd=pf_val,
        public_float_asof=pf[0] if pf else None,
        shares_outstanding=so_val,
        shares_outstanding_asof=so[0] if so else None,
        price_used=price,
        cik=cik,
        notes=notes,
        errors=errors,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Free SEC-based float estimator")
    ap.add_argument("ticker")
    ap.add_argument("--price", type=float, default=None,
                    help="live price to convert public-float $ into shares")
    args = ap.parse_args(argv)
    res = get_float(args.ticker, args.price)
    print(json.dumps(res.to_dict(), indent=2))
    # Exit 0 if we produced a usable number, 1 if float is unknown (-> manual).
    return 0 if res.float_shares is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
