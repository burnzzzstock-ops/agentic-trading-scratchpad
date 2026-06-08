"""Unit tests for the Validator & Risk gate."""

import copy

from validator import (
    Catalyst,
    MarketContext,
    Payload,
    evaluate,
)

# A clean A-tier first-trade setup that should pass FULL_AUTO.
# price 3.00 / stop 2.85 / target 3.60, tight 1-cent spread.
CLEAN_PAYLOAD = {
    "ticker": "XYZ", "setup": "ORB",
    "price": 3.00, "stop": 2.85, "target": 3.60, "rr_modeled": 2.5,
}
CLEAN_CONTEXT = {
    "bid": 2.99, "ask": 3.00, "last": 3.00, "prev_close": 2.70,
    "catalyst": {"verified": True, "tier": "A", "age_hours": 2.0,
                 "description": "FDA clearance"},
    "float_shares": 20_000_000,
    "trades_today": 0, "day_pnl": 0.0, "settled_cash": 250.0,
    "account_agentic": True,
}


def run(payload=None, **ctx_overrides):
    p = Payload.parse(payload or CLEAN_PAYLOAD)
    cdict = copy.deepcopy(CLEAN_CONTEXT)
    cdict.update(ctx_overrides)
    c = MarketContext.parse(cdict)
    return evaluate(p, c)


def test_clean_setup_is_full_auto():
    d = run()
    assert d.action == "FULL_AUTO", (d.action, d.blocks, d.manual)
    assert d.metrics["executable_rr"] >= 2.25


def test_missing_catalyst_blocks():
    d = run(catalyst=None)
    assert d.action == "BLOCK"
    assert any("catalyst" in b for b in d.blocks)


def test_stale_catalyst_blocks():
    d = run(catalyst={"verified": True, "tier": "A", "age_hours": 48.0})
    assert d.action == "BLOCK"
    assert any("catalyst" in b for b in d.blocks)


def test_unverified_catalyst_blocks():
    d = run(catalyst={"verified": False, "tier": "A", "age_hours": 1.0})
    assert d.action == "BLOCK"


def test_weak_catalyst_is_manual():
    d = run(catalyst={"verified": True, "tier": "B", "age_hours": 1.0})
    assert d.action == "MANUAL_CONFIRM"
    assert any("tier" in m for m in d.manual)


def test_low_executable_rr_blocks():
    # target only $0.10 above entry -> R:R well under floor
    p = dict(CLEAN_PAYLOAD, target=3.10)
    d = run(payload=p)
    assert d.action == "BLOCK"
    assert any("R:R" in b for b in d.blocks)


def test_executable_rr_uses_resistance_not_target():
    # Real resistance just above entry kills the edge even if target is far.
    d = run(nearest_resistance=3.10)
    assert d.action == "BLOCK"
    assert any("R:R" in b for b in d.blocks)


def test_third_trade_blocks():
    d = run(trades_today=2)
    assert d.action == "BLOCK"
    assert any("trade #3" in b for b in d.blocks)


def test_second_trade_is_manual():
    d = run(trades_today=1)
    assert d.action == "MANUAL_CONFIRM"
    assert any("second trade" in m for m in d.manual)


def test_daily_loss_limit_blocks():
    d = run(day_pnl=-10.0)
    assert d.action == "BLOCK"
    assert any("daily loss" in b for b in d.blocks)


def test_wide_spread_blocks():
    d = run(bid=2.925, ask=3.00)  # ~2.5%
    assert d.action == "BLOCK"
    assert any("spread" in b for b in d.blocks)


def test_medium_spread_is_manual():
    d = run(bid=2.955, ask=3.00)  # ~1.5%
    assert d.action == "MANUAL_CONFIRM"
    assert any("spread" in m for m in d.manual)


def test_pmh_break_is_manual():
    p = dict(CLEAN_PAYLOAD, setup="PMH_BREAK")
    d = run(payload=p)
    assert d.action == "MANUAL_CONFIRM"
    assert any("premarket-high" in m for m in d.manual)


def test_unknown_setup_blocks():
    p = dict(CLEAN_PAYLOAD, setup="MEME_YOLO")
    d = run(payload=p)
    assert d.action == "BLOCK"
    assert any("outside approved" in b for b in d.blocks)


def test_low_float_is_manual():
    d = run(float_shares=8_000_000)
    assert d.action == "MANUAL_CONFIRM"
    assert any("float" in m for m in d.manual)


def test_micro_float_is_quarantine_manual():
    d = run(float_shares=3_000_000)
    assert d.action == "MANUAL_CONFIRM"
    assert any("quarantin" in m for m in d.manual)


def test_unknown_float_is_manual():
    d = run(float_shares=None)
    assert d.action == "MANUAL_CONFIRM"
    assert any("float unknown" in m for m in d.manual)


def test_sub_two_dollar_is_manual():
    # ACCL-like sub-$2 with an otherwise-fine catalyst -> manual at best.
    p = {"ticker": "ACCL", "setup": "VWAP_RECLAIM",
         "price": 1.68, "stop": 1.55, "target": 2.05}
    d = run(payload=p, bid=1.67, ask=1.68, last=1.675, prev_close=1.55)
    assert d.action == "MANUAL_CONFIRM"
    assert any("under $2" in m for m in d.manual)


def test_accl_real_case_blocks_on_rr():
    # The actual ACCL payload: stop 1.61, target 1.855, no catalyst.
    p = {"ticker": "ACCL", "setup": "VWAP_RECLAIM",
         "price": 1.68, "stop": 1.61, "target": 1.855}
    d = run(payload=p, bid=1.67, ask=1.68, last=1.675, prev_close=1.55,
            catalyst=None, settled_cash=0.0, float_shares=None)
    assert d.action == "BLOCK"
    # Both the catalyst veto and the unsettled-cash veto should fire.
    assert any("catalyst" in b for b in d.blocks)
    assert any("settled cash" in b for b in d.blocks)


def test_halt_prone_blocks():
    d = run(halt_prone=True)
    assert d.action == "BLOCK"


def test_dilution_blocks():
    d = run(dilution_overhang=True)
    assert d.action == "BLOCK"


def test_failed_breakouts_block():
    d = run(failed_breakouts=2)
    assert d.action == "BLOCK"


def test_risk_off_no_catalyst_strength_blocks():
    d = run(tape_risk_off=True,
            catalyst={"verified": True, "tier": "B", "age_hours": 1.0})
    assert d.action == "BLOCK"


def test_risk_off_with_strong_catalyst_is_manual():
    d = run(tape_risk_off=True)  # default catalyst is A-tier
    assert d.action == "MANUAL_CONFIRM"


def test_non_agentic_account_blocks():
    d = run(account_agentic=False)
    assert d.action == "BLOCK"


def test_position_clamped_to_100():
    # Tiny risk-per-share would size a huge position; must clamp to $100.
    p = dict(CLEAN_PAYLOAD, stop=2.995)  # 0.5c stop -> big share count
    d = run(payload=p, nearest_resistance=None)
    assert d.metrics["notional"] <= 100.0 + 1e-9
    assert any("clamped" in n for n in d.notes)


def test_oversized_for_cash_blocks():
    d = run(settled_cash=20.0)  # notional ~80 > settled cash
    assert d.action == "BLOCK"
    assert any("settled cash" in b for b in d.blocks)


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
