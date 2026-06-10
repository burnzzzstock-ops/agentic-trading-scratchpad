"""Premarket catalyst scout — finds hot OFF-WATCHLIST names.

The trading routine is reactive (wakes on TradingView alerts), so it can only
trade the standing watchlist. This scout is the proactive half: run premarket
(via the tv-relay Worker's cron trigger -> routine "scout mode"), it sweeps
the SEC EDGAR *current events* feed for fresh 8-K filings market-wide, maps
filers to tickers, drops anything already on the watchlist, grades each
candidate with news_filter's scorer, and emits a ranked JSON list.

The routine then checks live gaps/prices via Robingood and journals a
"morning candidates" report. The human adds a TradingView chart + alert for
anything worth chasing — the scout NEVER trades.

Sources (free, no keys):
  * https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K
    Atom feed of the newest 8-Ks across all companies.
  * company_tickers.json for CIK -> ticker mapping.
  * Google News RSS (via news_filter) for catalyst color, capped at A-.

Usage:
  python3 scout.py [--watchlist scanner/watchlist_100.txt]
                   [--max-age-hours 18] [--max-candidates 8]
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from news_filter import _get, fetch_google_news, score_headlines

ATOM = "{http://www.w3.org/2005/Atom}"
CURRENT_8K_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?"
                  "action=getcurrent&type=8-K&company=&dateb=&owner=include"
                  "&count=100&output=atom")

# "8-K - ACME CORP (0001234567) (Filer)"
_TITLE_RX = re.compile(r"^8-K[^-]*-\s*(.+?)\s*\((\d{6,10})\)", re.I)


def load_watchlist(path: str) -> set:
    try:
        with open(path) as f:
            return {t.strip().upper() for t in f.read().split(",") if t.strip()}
    except OSError:
        return set()


def cik_to_ticker_map() -> dict:
    tmap = json.loads(_get("https://www.sec.gov/files/company_tickers.json",
                           accept="application/json"))
    return {int(row["cik_str"]): row["ticker"].upper() for row in tmap.values()}


def fetch_current_8ks(max_age_hours: float) -> list:
    """Newest 8-K filings market-wide: [{company, cik, age_hours}]."""
    root = ET.fromstring(_get(CURRENT_8K_URL, accept="application/atom+xml"))
    now = datetime.now(timezone.utc)
    out = []
    for entry in root.iter(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        updated = entry.findtext(f"{ATOM}updated")
        m = _TITLE_RX.match(title)
        if not m:
            continue
        age = None
        if updated:
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = (now - ts).total_seconds() / 3600.0
            except ValueError:
                age = None
        if age is not None and age > max_age_hours:
            continue
        out.append({"company": m.group(1), "cik": int(m.group(2)),
                    "age_hours": age})
    return out


def scout(watchlist_path: str, max_age_hours: float,
          max_candidates: int) -> dict:
    watch = load_watchlist(watchlist_path)
    errors = []

    try:
        filings = fetch_current_8ks(max_age_hours)
    except Exception as e:
        return {"candidates": [], "skipped": [],
                "errors": [f"sec current feed: {e}"]}

    try:
        ciks = cik_to_ticker_map()
    except Exception as e:
        return {"candidates": [], "skipped": [],
                "errors": [f"ticker map: {e}"]}

    seen, candidates, skipped = set(), [], []
    for f in filings:
        ticker = ciks.get(f["cik"])
        if ticker is None:           # funds/private filers — not tradable
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        if ticker in watch:
            skipped.append({"ticker": ticker, "reason": "already on watchlist"})
            continue

        # Grade with news color (capped at A- by design); the 8-K itself is
        # the verified event — the headlines tell us WHAT it likely is.
        entry = {"ticker": ticker, "company": f["company"],
                 "filed_age_hours": round(f["age_hours"], 1)
                                    if f["age_hours"] is not None else None}
        try:
            news = fetch_google_news(f'{f["company"]} stock')
            res = score_headlines(news, source_is_sec=False)
            entry["catalyst"] = res.catalyst
            entry["dilution_overhang"] = res.dilution_overhang
            entry["sentiment"] = res.sentiment
            entry["top_headlines"] = res.headlines[:3]
        except Exception as e:
            entry["catalyst"] = {"verified": False, "tier": "",
                                 "description": "8-K filed; news fetch failed"}
            entry["dilution_overhang"] = False
            entry["sentiment"] = "unknown"
            errors.append(f"news {ticker}: {e}")

        candidates.append(entry)
        if len(candidates) >= max_candidates * 3:   # enough raw material
            break

    # Rank: dilution flags sink; verified bullish tiers float; fresher first.
    tier_rank = {"A": 4, "A-": 3, "B": 2, "ambiguous": 1, "": 0}
    candidates.sort(key=lambda c: (
        c.get("dilution_overhang", False),
        -tier_rank.get(c.get("catalyst", {}).get("tier", ""), 0),
        c.get("filed_age_hours") if c.get("filed_age_hours") is not None else 99,
    ))
    return {"candidates": candidates[:max_candidates],
            "skipped": skipped, "errors": errors}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Premarket off-watchlist catalyst scout")
    ap.add_argument("--watchlist", default="scanner/watchlist_100.txt")
    ap.add_argument("--max-age-hours", type=float, default=18.0,
                    help="8-K freshness window (overnight + premarket)")
    ap.add_argument("--max-candidates", type=int, default=8)
    args = ap.parse_args(argv)
    print(json.dumps(scout(args.watchlist, args.max_age_hours,
                           args.max_candidates), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
