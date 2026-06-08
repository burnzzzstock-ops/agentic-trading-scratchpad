"""Unit tests for the pre-market alert screener."""

import copy

from screener import (
    AUTO,
    MANUAL,
    SKIP,
    Candidate,
    Filters,
    screen,
    screen_one,
)

# A clean auto-watch name: $3, penny spread, ample float, moving.
CLEAN = {
    "ticker": "XYZ", "bid": 2.99, "ask": 3.00, "last": 3.00, "prev_close": 2.70,
    "float_shares": 20_000_000, "tradable": True, "fractional": True,
    "gap_pct": 11.1, "vol_x": 4.0,
}


def one(**overrides):
    d = copy.deepcopy(CLEAN)
    d.update(overrides)
    return screen_one(Candidate.parse(d), Filters(**overrides.pop("_filt", {})))


def filt(**kw):
    return Filters(**kw)


def test_clean_name_is_auto_watch():
    r = one()
    assert r.tier == AUTO, (r.tier, r.reasons)
    assert not r.reasons


def test_not_tradable_is_skip():
    r = one(tradable=False)
    assert r.tier == SKIP
    assert any("tradable" in x for x in r.reasons)


def test_no_market_is_skip():
    r = one(bid=0.0, ask=0.0, last=0.0)
    assert r.tier == SKIP
    assert any("two-sided" in x for x in r.reasons)


def test_wide_spread_is_skip():
    r = one(bid=2.90, ask=3.00)  # ~3.4%
    assert r.tier == SKIP
    assert any("2.00% hard veto" in x for x in r.reasons)


def test_sub_dollar_is_skip():
    r = one(bid=0.94, ask=0.95, last=0.95)
    assert r.tier == SKIP
    assert any("floor" in x for x in r.reasons)


def test_medium_spread_is_manual():
    r = one(bid=2.955, ask=3.00)  # ~1.5%
    assert r.tier == MANUAL
    assert any("band" in x for x in r.reasons)


def test_sub_two_dollar_is_manual():
    r = one(bid=1.66, ask=1.68, last=1.66, prev_close=1.55)
    assert r.tier == MANUAL
    assert any("under $2" in x for x in r.reasons)


def test_unknown_float_is_manual():
    r = one(float_shares=None)
    assert r.tier == MANUAL
    assert any("float unknown" in x for x in r.reasons)


def test_low_float_is_manual():
    r = one(float_shares=8_000_000)
    assert r.tier == MANUAL
    assert any("under 10M" in x for x in r.reasons)


def test_micro_float_is_quarantine_manual():
    r = one(float_shares=3_000_000)
    assert r.tier == MANUAL
    assert any("quarantine" in x for x in r.reasons)


def test_momentum_prefilter_skips_dead_tape():
    c = Candidate.parse(dict(CLEAN, gap_pct=0.5, vol_x=0.8))
    r = screen_one(c, Filters(min_gap_pct=3.0, min_vol_x=1.5))
    assert r.tier == SKIP
    assert any("gap" in x for x in r.reasons)
    assert any("rel-vol" in x for x in r.reasons)


def test_momentum_prefilter_off_by_default():
    # gap/vol present but low; with default (0) filters it still rides on the
    # structural tiers, not the momentum floor.
    c = Candidate.parse(dict(CLEAN, gap_pct=0.5, vol_x=0.8))
    r = screen_one(c, Filters())
    assert r.tier == AUTO


def test_missing_momentum_data_does_not_skip():
    # Pre-open, gap/vol may be absent; a strict filter must not skip on absence.
    c = Candidate.parse(dict(CLEAN, gap_pct=None, vol_x=None))
    r = screen_one(c, Filters(min_gap_pct=5.0, min_vol_x=2.0))
    assert r.tier == AUTO


def test_accl_real_case_is_manual_watch():
    # Today's ACCL: tradable, $1.68, ~1.2% spread, unknown float.
    # Worth an alert, but only ever MANUAL (sub-$2 + unknown float).
    c = Candidate.parse({
        "ticker": "ACCL", "bid": 1.66, "ask": 1.68, "last": 1.66,
        "prev_close": 1.55, "float_shares": None, "tradable": True,
        "gap_pct": 7.1, "vol_x": 3.0,
    })
    r = screen_one(c, Filters())
    assert r.tier == MANUAL
    assert any("under $2" in x for x in r.reasons)
    assert any("float unknown" in x for x in r.reasons)


def test_screen_sorts_auto_then_manual_then_skip_by_score():
    cands = [
        Candidate.parse(dict(CLEAN, ticker="DEAD", tradable=False)),
        Candidate.parse(dict(CLEAN, ticker="MAN", bid=1.66, ask=1.68, last=1.66, gap_pct=5.0, vol_x=2.0)),
        Candidate.parse(dict(CLEAN, ticker="HOT", gap_pct=20.0, vol_x=8.0)),
        Candidate.parse(dict(CLEAN, ticker="WARM", gap_pct=6.0, vol_x=2.0)),
    ]
    out = screen(cands, Filters())
    tiers = [r.tier for r in out]
    tickers = [r.ticker for r in out]
    assert tiers == [AUTO, AUTO, MANUAL, SKIP]
    assert tickers[:2] == ["HOT", "WARM"]   # hotter momentum first within AUTO
    assert tickers[-1] == "DEAD"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
