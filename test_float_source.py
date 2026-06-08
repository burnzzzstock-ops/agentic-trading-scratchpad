"""Unit tests for the SEC float estimator's pure decision core."""

from float_source import decide_float

GATE = 10_000_000


def test_public_float_and_price_gives_estimate():
    # $528M public float / $31.5 ~= 16.8M shares.
    f, method, conf, _ = decide_float(528_400_000, 31.5, 144_647_982, GATE)
    assert conf == "estimate"
    assert method == "sec_public_float/price"
    assert 16_000_000 < f < 18_000_000


def test_estimate_capped_at_shares_outstanding():
    # Price crash would inflate the estimate past shares outstanding; cap it.
    f, _, conf, notes = decide_float(1_000_000_000, 0.01, 5_000_000, GATE)
    assert conf == "estimate"
    assert f == 5_000_000
    assert any("capped" in n for n in notes)


def test_zero_public_float_is_unusable():
    # Recent IPO reports public float 0 -> must not be read as "zero float".
    f, method, conf, notes = decide_float(0, 10.0, None, GATE)
    assert f is None
    assert conf == "none"
    assert any("reported as 0" in n for n in notes)


def test_public_float_without_price_falls_back():
    # Have $float but no price: cannot convert; fall through to shares-out rule.
    f, method, conf, _ = decide_float(500_000_000, None, 8_000_000, GATE)
    assert conf == "upper_bound"
    assert f == 8_000_000


def test_shares_outstanding_below_gate_is_upper_bound():
    f, method, conf, _ = decide_float(None, None, 8_000_000, GATE)
    assert conf == "upper_bound"
    assert method == "sec_shares_outstanding_upper_bound"
    assert f == 8_000_000


def test_micro_shares_outstanding_routes_to_quarantine_band():
    # 3M shares out -> emit 3M; screener/validator will quarantine it.
    f, _, conf, _ = decide_float(None, None, 3_000_000, GATE)
    assert f == 3_000_000
    assert conf == "upper_bound"


def test_large_shares_outstanding_alone_cannot_confirm():
    # 200M shares out but no float number: must NOT confirm >=10M (low-float risk).
    f, method, conf, notes = decide_float(None, None, 200_000_000, GATE)
    assert f is None
    assert conf == "none"
    assert any("cannot confirm" in n for n in notes)


def test_nothing_available_is_none():
    f, method, conf, notes = decide_float(None, None, None, GATE)
    assert f is None
    assert conf == "none"
    assert any("no SEC public float" in n for n in notes)


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
