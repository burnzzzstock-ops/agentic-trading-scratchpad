"""Tests for the pure scorer in news_filter (no network)."""

from news_filter import score_headlines


def H(title, age=2.0, source="X"):
    return {"title": title, "age_hours": age, "source": source}


def test_fda_approval_is_A_from_sec():
    r = score_headlines([H("Company receives FDA approval for drug")], source_is_sec=True)
    assert r.catalyst["tier"] == "A"
    assert r.catalyst["verified"] is True
    assert r.sentiment == "bullish"


def test_news_fda_approval_capped_at_Aminus():
    # Same headline from news (not SEC) must cap at A- so it routes to MANUAL.
    r = score_headlines([H("Company receives FDA approval for drug")], source_is_sec=False)
    assert r.catalyst["tier"] == "A-"
    assert r.catalyst["verified"] is True


def test_contract_award_scores():
    r = score_headlines([H("Firm awarded $50 million contract from DoD")])
    assert r.catalyst["tier"] in ("A", "A-")
    assert r.catalyst["verified"] is True


def test_offering_sets_dilution_and_is_not_catalyst():
    r = score_headlines([H("Company prices $20 million registered direct offering")])
    assert r.dilution_overhang is True
    assert r.catalyst["verified"] is False
    assert r.sentiment == "bearish"


def test_vague_ai_pr_is_not_a_catalyst():
    r = score_headlines([H("Startup unveils AI-powered trading platform")])
    assert r.catalyst["tier"] == "ambiguous"
    assert r.catalyst["verified"] is False


def test_investor_awareness_is_buzz():
    r = score_headlines([H("XYZ launches investor awareness campaign")])
    assert r.catalyst["verified"] is False


def test_fda_rejection_is_negative_not_approval():
    r = score_headlines([H("FDA rejects company's drug application")], source_is_sec=True)
    assert r.catalyst["verified"] is False
    assert r.sentiment == "bearish"


def test_negation_suppresses_bullish():
    r = score_headlines([H("Company fails to win major contract")])
    # "fails to" -> matched as negative failure, not a contract-award catalyst
    assert r.catalyst["verified"] is False


def test_mixed_sentiment_with_catalyst_and_dilution():
    r = score_headlines([
        H("Company awarded $30 million contract"),
        H("Company announces $10 million public offering"),
    ])
    assert r.dilution_overhang is True
    assert r.sentiment == "mixed"
    # a real catalyst still surfaces alongside the dilution veto flag
    assert r.catalyst["verified"] is True


def test_earnings_beat_and_guidance_raise():
    r = score_headlines([H("ACME beats estimates and raises guidance")], source_is_sec=True)
    assert r.catalyst["tier"] == "A"


def test_empty_headlines_is_no_catalyst():
    r = score_headlines([])
    assert r.catalyst["verified"] is False
    assert r.catalyst["tier"] == ""
    assert r.sentiment == "neutral"


def test_partnership_is_aminus():
    r = score_headlines([H("Company announces strategic partnership with Microsoft")], source_is_sec=True)
    # partnership is a softer catalyst -> A- even from SEC
    assert r.catalyst["tier"] == "A-"


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
