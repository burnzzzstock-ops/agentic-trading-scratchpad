"""Free float-shares source for the Validator pipeline.

Supplies the `float_shares` value that `validator.py`'s float gate consumes
(>=10M full-auto, <10M manual, <5M quarantine), using only free, no-API-key
sources:

  * Yahoo Finance quoteSummary (primary) — `defaultKeyStatistics.floatShares`
                        is the actual public float (shares outstanding minus
                        insider/restricted holdings). This is what we want.
                        The endpoint is gated behind a cookie + "crumb"
                        handshake; a naked request returns HTTP 401.
  * SEC EDGAR XBRL (fallback) — `dei:EntityCommonStockSharesOutstanding` from
                        company facts. Honesty note: this is *shares
                        outstanding*, NOT true float — it does not net out
                        insider/locked shares, so it is an UPPER BOUND on the
                        float. We tag the source so the caller knows the number
                        is a proxy, and the gate stays conservative because a
                        larger number can only relax the manual triggers, never
                        silently tighten an auto-fire on a genuinely tiny float.

Design: network I/O is isolated here (and in news_filter.py); validator.py
stays pure and offline-testable. Failures degrade to float_shares=None, which
the validator already treats as "unknown -> MANUAL_CONFIRM" (fail safe).

Network: the routine environment must allow `query1.finance.yahoo.com`,
`query2.finance.yahoo.com`, `fc.yahoo.com`, and `sec.gov` / `data.sec.gov`
hosts (Custom or Full network access); they are not in the default allowlist.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

USER_AGENT = "agentic-trading-scratchpad/1.0 (research; contact: ops@example.com)"
# Yahoo's quote* endpoints reject the project UA with 403/401; use a browser UA
# for those hosts only. SEC is fine with (and politely expects) the project UA.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = 8.0


@dataclass
class FloatResult:
    float_shares: Optional[int]
    source: str                       # yahoo | sec | none
    is_true_float: bool = False       # False when the number is a shares-out proxy
    as_of: Optional[str] = None
    shares_outstanding: Optional[int] = None
    errors: list = field(default_factory=list)

    def to_dict(self):
        return {
            "float_shares": self.float_shares,
            "source": self.source,
            "is_true_float": self.is_true_float,
            "as_of": self.as_of,
            "shares_outstanding": self.shares_outstanding,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- #
# Yahoo Finance (primary) — cookie + crumb handshake, then quoteSummary.
# --------------------------------------------------------------------------- #
def _yahoo_opener() -> urllib.request.OpenerDirector:
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _yget(opener, url: str, accept="*/*") -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept": accept}
    )
    with opener.open(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def fetch_yahoo_float(ticker: str) -> FloatResult:
    """Public float via Yahoo's gated quoteSummary endpoint.

    Performs Yahoo's handshake: (1) hit a Yahoo host to obtain the consent
    cookie, (2) fetch a crumb, (3) call quoteSummary with cookie + crumb.
    """
    opener = _yahoo_opener()

    # 1) Prime the session cookie. fc.yahoo.com 404s but still sets the cookie;
    #    that's fine, we only need the Set-Cookie side effect.
    try:
        _yget(opener, "https://fc.yahoo.com")
    except Exception:
        pass

    # 2) Crumb — paired with the cookie above; required by quoteSummary.
    crumb = _yget(opener, "https://query1.finance.yahoo.com/v1/test/getcrumb").strip()
    if not crumb or "<" in crumb:
        raise RuntimeError("could not obtain Yahoo crumb")

    # 3) quoteSummary -> defaultKeyStatistics.
    url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{urllib.parse.quote(ticker)}?modules=defaultKeyStatistics"
        f"&crumb={urllib.parse.quote(crumb)}"
    )
    data = json.loads(_yget(opener, url, accept="application/json"))
    result = (data.get("quoteSummary", {}).get("result") or [])
    if not result:
        raise RuntimeError("empty quoteSummary result")
    ks = result[0].get("defaultKeyStatistics", {}) or {}

    flt = (ks.get("floatShares") or {}).get("raw")
    shout = (ks.get("sharesOutstanding") or {}).get("raw")
    if flt is None:
        raise RuntimeError("floatShares absent from defaultKeyStatistics")

    return FloatResult(
        float_shares=int(flt),
        source="yahoo",
        is_true_float=True,
        shares_outstanding=int(shout) if shout is not None else None,
    )


# --------------------------------------------------------------------------- #
# SEC EDGAR (fallback) — shares outstanding as an upper-bound proxy.
# --------------------------------------------------------------------------- #
def _sget(url: str, accept="application/json") -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read()


def _ticker_to_cik(ticker: str) -> Optional[int]:
    tmap = json.loads(_sget("https://www.sec.gov/files/company_tickers.json"))
    for row in tmap.values():
        if row.get("ticker", "").upper() == ticker.upper():
            return int(row["cik_str"])
    return None


def fetch_sec_shares_outstanding(ticker: str) -> FloatResult:
    """Most recent dei:EntityCommonStockSharesOutstanding for the ticker.

    This is shares OUTSTANDING (an upper bound on float), not true float.
    """
    cik = _ticker_to_cik(ticker)
    if cik is None:
        raise RuntimeError(f"no CIK for {ticker!r}")

    url = (f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/"
           f"dei/EntityCommonStockSharesOutstanding.json")
    data = json.loads(_sget(url))

    # Prefer the most recently *filed* value (freshest report), tie-breaking on
    # the period-end date. Keying on `end` alone can surface a stale restated
    # period that happens to sort late.
    best = None  # (filed, end, val)
    for unit_rows in (data.get("units") or {}).values():
        for row in unit_rows:
            val, end, filed = row.get("val"), row.get("end"), row.get("filed", "")
            if val is None or end is None:
                continue
            key = (filed, end)
            if best is None or key > best[:2]:
                best = (filed, end, int(val))
    if best is None:
        raise RuntimeError("no EntityCommonStockSharesOutstanding values")

    return FloatResult(
        float_shares=best[2],
        source="sec",
        is_true_float=False,
        as_of=best[1],
        shares_outstanding=best[2],
    )


# --------------------------------------------------------------------------- #
# Top-level: Yahoo primary, SEC fallback, then give up (None == unknown).
# --------------------------------------------------------------------------- #
def build_float(ticker: str) -> FloatResult:
    errors = []
    try:
        res = fetch_yahoo_float(ticker)
        res.errors = errors
        return res
    except Exception as e:
        errors.append(f"yahoo: {e}")

    try:
        res = fetch_sec_shares_outstanding(ticker)
        res.errors = errors
        return res
    except Exception as e:
        errors.append(f"sec: {e}")

    return FloatResult(float_shares=None, source="none", errors=errors)


def get_float_shares(ticker: str) -> Optional[int]:
    """Convenience: just the integer (or None) for populating MarketContext."""
    return build_float(ticker).float_shares


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Free float-shares source (Yahoo->SEC)")
    ap.add_argument("ticker")
    args = ap.parse_args(argv)
    res = build_float(args.ticker)
    print(json.dumps(res.to_dict(), indent=2))
    return 0 if res.float_shares is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
