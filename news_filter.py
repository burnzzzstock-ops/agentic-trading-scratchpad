"""Free catalyst / news filter for the Validator pipeline.

Produces the `catalyst` block (and a `dilution_overhang` flag) that
`validator.py` consumes, using only free, no-API-key sources:

  * Google News RSS   — keyword + recency + sentiment. Treated as *evidence*,
                        not proof: news-only catalysts are capped at tier "A-"
                        so they route to MANUAL_CONFIRM, never silent auto-fire.
  * SEC EDGAR 8-K     — official, timestamped material-event filings. An 8-K in
                        the last 24h is a genuinely *verified* catalyst and may
                        grade up to "A".

Honesty note: keyword sentiment is a heuristic. It mis-reads negation and
sarcasm, and a bullish headline is not a vetted thesis. The grading here is
deliberately conservative — concrete, named events score; buzzwords
("AI-powered", "investor awareness") explicitly do not, per the spec's
"not acceptable" list. Network I/O is isolated from the pure scorer so the
logic is unit-testable offline.

Network: the routine environment must allow `news.google.com` and `sec.gov`
hosts (Custom or Full network access); they are not in the default allowlist.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

USER_AGENT = "agentic-trading-scratchpad/1.0 (research; contact: ops@example.com)"
HTTP_TIMEOUT = 8.0
DEFAULT_MAX_AGE_HOURS = 24.0

# --------------------------------------------------------------------------- #
# Keyword rules. Order matters only within a tier; we keep the best match.
# Each pattern is a compiled, case-insensitive regex.
# --------------------------------------------------------------------------- #
def _rx(p):
    return re.compile(p, re.I)

# Concrete, market-moving catalysts (A = definitive).
BULLISH_A = [
    (_rx(r"\bFDA\b.*\b(approv\w*|clear\w*|grant\w*)"), "FDA approval/clearance"),
    (_rx(r"\b(approv\w+)\b.*\bFDA\b"), "FDA approval"),
    (_rx(r"\bphase\s*3\b.*\b(met|success|positive|primary endpoint)\b"), "Phase 3 met endpoint"),
    (_rx(r"\bmet\b.*\bprimary endpoint\b"), "met primary endpoint"),
    (_rx(r"\b(to be acquired|acquisition of|acquires|buyout|to acquire|merger|merge with|take ?private)\b"), "M&A"),
    (_rx(r"\b(awarded|wins|secures|receives)\b.*\bcontract\b"), "contract award"),
    (_rx(r"\$\s?\d[\d.,]*\s*(million|billion|m|bn)\b.*\b(contract|order|deal|award)\b"), "$-sized contract"),
    (_rx(r"\b(beats?|tops?|exceeds?)\b.*\b(estimates?|expectations?|consensus)\b"), "earnings beat"),
    (_rx(r"\braises?\b.*\bguidance\b"), "guidance raise"),
]

# Real but softer catalysts (A- = needs human; news-grade ceiling).
BULLISH_AMINUS = [
    (_rx(r"\b(partnership|collaborat\w+|strategic alliance)\b"), "partnership"),
    (_rx(r"\b(new|major|key)\b.*\bcustomer\b"), "new customer"),
    (_rx(r"\b(positive|topline)\b.*\b(results?|data)\b"), "positive data"),
    (_rx(r"\b(upgrade[ds]?|raised? to (buy|outperform|overweight))\b"), "analyst upgrade"),
    (_rx(r"\b(receives?|wins?)\b.*\b(order|approval|designation)\b"), "order/designation"),
]

# Explicitly NOT acceptable per spec — buzz that should never count as catalyst.
BUZZ = [
    (_rx(r"\bAI[- ]?(powered|driven|enabled|revolution)\b"), "vague AI PR"),
    (_rx(r"\binvestor (awareness|relations|outreach)\b"), "investor awareness"),
    (_rx(r"\bto present at\b|\bto participate in\b|\bconference\b"), "conference/IR fluff"),
    (_rx(r"\bannounces?\b$"), "bare announcement"),
]

# Financing / dilution / distress — these are VETOES, not catalysts.
NEGATIVE = [
    (_rx(r"\b(registered direct|public offering|priced? .*offering|at[- ]the[- ]market|ATM offering)\b"), "offering/dilution"),
    (_rx(r"\b(shelf registration|S-1|S-3|warrant\w*|convertible note|toxic)\b"), "dilution overhang"),
    (_rx(r"\bdilut\w+\b"), "dilution"),
    (_rx(r"\b(going concern|bankrupt\w*|delist\w*|reverse split)\b"), "distress"),
    (_rx(r"\b(investigation|lawsuit|class action|short[- ]seller|fraud|sec charges?)\b"), "legal/short"),
    (_rx(r"\bFDA\b.*\b(reject\w*|crl|complete response letter|declin\w*)\b"), "FDA rejection"),
    (_rx(r"\b(misses?|missed)\b.*\b(estimates?|expectations?)\b"), "earnings miss"),
    (_rx(r"\b(cuts?|lowers?|slashes?)\b.*\bguidance\b"), "guidance cut"),
    (_rx(r"\b(fails? to|failed|halt\w*|delay\w*|denied|rejects?)\b"), "failure/halt"),
    (_rx(r"\b(downgrade[ds]?|cut to (sell|underperform|underweight))\b"), "downgrade"),
]

# Negation tokens that suppress a bullish match when they appear just before it.
_NEGATION = _rx(r"\b(no|not|fails? to|failed|denies|denied|rejects?|delays?|without)\b")

_TIER_RANK = {"": 0, "ambiguous": 1, "B": 2, "A-": 3, "A": 4}


@dataclass
class NewsResult:
    catalyst: dict
    dilution_overhang: bool
    sentiment: str            # bullish | bearish | mixed | neutral
    headlines: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self):
        return {
            "catalyst": self.catalyst,
            "dilution_overhang": self.dilution_overhang,
            "sentiment": self.sentiment,
            "headlines": self.headlines,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- #
# Pure scorer (no network) — this is what the tests exercise.
# --------------------------------------------------------------------------- #
def _match(rules, text):
    for rx, label in rules:
        m = rx.search(text)
        if m:
            # crude negation guard: skip if a negation sits in the 16 chars before
            start = max(0, m.start() - 16)
            if _NEGATION.search(text[start:m.start()]):
                continue
            return label
    return None


def score_headlines(headlines: list, source_is_sec: bool = False) -> NewsResult:
    """headlines: list of {"title": str, "age_hours": float, "source": str}."""
    bull_hits, bear_hits = [], []
    best_tier, best_label, best_age = "", "", None
    dilution = False

    for h in headlines:
        title = h.get("title", "")
        age = h.get("age_hours")

        neg = _match(NEGATIVE, title)
        if neg:
            bear_hits.append({"label": neg, "title": title})
            if "dilution" in neg or "offering" in neg or "overhang" in neg:
                dilution = True
            continue  # a negative headline is not also counted as a catalyst

        for rules, tier in ((BULLISH_A, "A"), (BULLISH_AMINUS, "A-")):
            lbl = _match(rules, title)
            if lbl:
                bull_hits.append({"label": lbl, "tier": tier, "title": title})
                if _TIER_RANK[tier] > _TIER_RANK[best_tier]:
                    best_tier, best_label, best_age = tier, lbl, age
                break
        else:
            if _match(BUZZ, title):
                bull_hits.append({"label": "buzz", "tier": "ambiguous", "title": title})
                if _TIER_RANK["ambiguous"] > _TIER_RANK[best_tier]:
                    best_tier, best_label, best_age = "ambiguous", "buzz", age

    # News-only evidence is capped at A- so it can never silent-auto-fire;
    # only an official SEC 8-K may grade up to A.
    if best_tier == "A" and not source_is_sec:
        best_tier = "A-"

    verified = best_tier in ("A", "A-") and (source_is_sec or bool(bull_hits))
    catalyst = {
        "verified": bool(verified),
        "tier": best_tier,
        "age_hours": best_age if best_age is not None else 999.0,
        "description": best_label,
        "source": "sec" if source_is_sec else "news",
    }

    if bull_hits and bear_hits:
        sentiment = "mixed"
    elif bull_hits:
        sentiment = "bullish"
    elif bear_hits:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    return NewsResult(
        catalyst=catalyst,
        dilution_overhang=dilution,
        sentiment=sentiment,
        headlines=bull_hits + bear_hits,
    )


# --------------------------------------------------------------------------- #
# Network fetchers (best-effort; failures degrade to "no catalyst" = BLOCK).
# --------------------------------------------------------------------------- #
def _get(url, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read()


def fetch_google_news(query: str, max_age_hours=DEFAULT_MAX_AGE_HOURS) -> list:
    q = urllib.parse.quote(f'{query} when:1d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    out = []
    root = ET.fromstring(_get(url, accept="application/rss+xml"))
    now = datetime.now(timezone.utc)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub = item.findtext("pubDate")
        age = None
        if pub:
            try:
                age = (now - parsedate_to_datetime(pub)).total_seconds() / 3600.0
            except Exception:
                age = None
        if age is not None and age > max_age_hours:
            continue
        src = item.findtext("{*}source") or ""
        out.append({"title": title, "age_hours": age, "source": src})
    return out


def fetch_sec_8k(ticker: str, max_age_hours=DEFAULT_MAX_AGE_HOURS) -> list:
    """Return recent 8-K filings within the window as pseudo-headlines."""
    tmap = json.loads(_get("https://www.sec.gov/files/company_tickers.json",
                           accept="application/json"))
    cik = None
    for row in tmap.values():
        if row.get("ticker", "").upper() == ticker.upper():
            cik = int(row["cik_str"])
            break
    if cik is None:
        return []
    subs = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                           accept="application/json"))
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    now = datetime.now(timezone.utc)
    out = []
    for form, d in zip(forms, dates):
        if form != "8-K":
            continue
        try:
            filed = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age = (now - filed).total_seconds() / 3600.0
        if age <= max_age_hours:
            out.append({"title": "8-K material event filed (SEC)",
                        "age_hours": age, "source": "SEC EDGAR"})
    return out


def build_catalyst(ticker: str, company: Optional[str] = None,
                   max_age_hours=DEFAULT_MAX_AGE_HOURS) -> NewsResult:
    """Top-level: prefer an SEC 8-K (verified); fall back to news headlines."""
    errors = []
    # 1) SEC 8-K — highest signal, can grade A.
    try:
        sec = fetch_sec_8k(ticker, max_age_hours)
        if sec:
            res = score_headlines(sec, source_is_sec=True)
            res.errors = errors
            return res
    except Exception as e:  # network/parse — degrade gracefully
        errors.append(f"sec: {e}")

    # 2) Google News — capped at A-.
    try:
        query = company or f"{ticker} stock"
        news = fetch_google_news(query, max_age_hours)
        res = score_headlines(news, source_is_sec=False)
        res.errors = errors
        return res
    except Exception as e:
        errors.append(f"news: {e}")

    # 3) Nothing reachable -> no catalyst -> validator BLOCKs. Fail safe.
    return NewsResult(
        catalyst={"verified": False, "tier": "", "age_hours": 999.0,
                  "description": "", "source": "none"},
        dilution_overhang=False, sentiment="neutral", errors=errors,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Free catalyst/news filter")
    ap.add_argument("ticker")
    ap.add_argument("--company", help="company name for a less-ambiguous news query")
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    args = ap.parse_args(argv)
    res = build_catalyst(args.ticker, args.company, args.max_age_hours)
    print(json.dumps(res.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
