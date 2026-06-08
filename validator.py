"""Asymmetric Trading Pipeline — Validator & Risk layer.

Deterministic second half of the TradingView scanner. The Pine script
(Scanner Agent) surfaces a candidate as a JSON payload; this module is the
Validator + Risk Agent that tries to kill it and, if it survives, sizes it
and decides the autonomy tier.

Design contract
---------------
This module does NO network I/O. It is pure logic so the guardrails are
deterministic and unit-testable. The routine/agent is responsible for
fetching live data (quote/spread via get_equity_quotes, positions, portfolio,
order count, float, and a verified 24h catalyst) and passing it in as a
`MarketContext`. Anything the scanner cannot see (catalyst, float, live
spread, real overhead resistance) MUST be supplied here or the trade blocks.

Spec conflicts, resolved to the stricter reading (documented inline):
  * Per-trade size: "MAX POSITION SIZE $100" vs "MAX_NOTIONAL_CEILING $250"
      -> hard block above $100; $250 is an absolute ceiling on top.
  * Daily loss: "4% ($10)" vs "$500" -> 4% of equity ($10 on $250). The $500
      is a leftover from a larger-account template.
  * Float manual gate: "<5M" (sec 3B) vs "<10M" (sec 10) -> manual under 10M,
      quarantine under 5M.
  * Price manual gate: "<$1" (sec 3) vs "<$2" (sec 10) -> manual under $2.
  * Halt-prone: absolute veto (sec 4) vs manual (sec 10) -> default BLOCK.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# Spec constants
# --------------------------------------------------------------------------- #
ACCOUNT_EQUITY = 250.0

APPROVED_SETUPS = {"ORB", "VWAP_RECLAIM", "PMH_BREAK"}

# Risk sizing (% of equity)
BASE_RISK_PCT = 0.02
MAX_RISK_PCT = 0.03
QUARANTINE_RISK_PCT = 0.015

# Per-trade notional: enforce the stricter $100; $250 is an absolute ceiling.
HARD_PER_TRADE_NOTIONAL = 100.0
MAX_NOTIONAL_CEILING = 250.0

# Daily loss limit: 4% of equity.
DAILY_LOSS_LIMIT = ACCOUNT_EQUITY * 0.04  # $10

MAX_TRADES_PER_DAY = 2  # the third trade is auto-blocked

# Bid-ask spread tiers (fraction of mid)
SPREAD_AUTO_MAX = 0.0125    # <= 1.25% -> eligible for full auto
SPREAD_MANUAL_MAX = 0.02    # 1.25%-2.00% -> manual; > 2.00% -> block

# R:R
MIN_EXEC_RR = 2.25          # slippage-adjusted floor; below this -> block

# Slippage buffer floor = max(1% of price, half the live spread)
SLIP_PRICE_FRAC = 0.01

# Universe / autonomy thresholds
PRICE_MIN = 1.00            # full-auto requires price >= $1.00
PRICE_MANUAL_BELOW = 2.00   # under $2 -> manual confirm
FLOAT_FULLAUTO_MIN = 10_000_000   # under this -> manual confirm
FLOAT_QUARANTINE = 5_000_000      # under this -> quarantine spec mode
CASH_MANUAL_FRACTION = 0.50       # needs > 50% of cash -> manual confirm

STRONG_CATALYST_TIERS = {"A", "A-"}
CATALYST_MAX_AGE_HOURS = 24.0


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass
class Payload:
    """The JSON emitted by the Pine scanner (extra keys are ignored)."""

    ticker: str
    setup: str
    price: float
    stop: float
    target: float
    rr_modeled: float = 0.0
    shares: float = 0.0
    notional: float = 0.0
    atr: Optional[float] = None
    gap_atr: Optional[float] = None
    gap_pct: Optional[float] = None
    slip_est: Optional[float] = None
    vol_x: Optional[float] = None

    @classmethod
    def parse(cls, data) -> "Payload":
        d = json.loads(data) if isinstance(data, (str, bytes)) else dict(data)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Catalyst:
    """A verified, fresh news catalyst. The scanner cannot produce this;
    the agent must source it from a news feed and grade it."""

    verified: bool = False
    tier: str = ""            # "A", "A-", "B", "ambiguous", ...
    age_hours: float = 999.0
    description: str = ""

    @property
    def is_fresh_verified(self) -> bool:
        return self.verified and self.age_hours <= CATALYST_MAX_AGE_HOURS

    @property
    def is_strong(self) -> bool:
        return self.is_fresh_verified and self.tier in STRONG_CATALYST_TIERS


@dataclass
class MarketContext:
    """Live data the agent must gather before the validator can run."""

    bid: float
    ask: float
    last: float
    prev_close: float
    catalyst: Optional[Catalyst] = None
    float_shares: Optional[int] = None
    trades_today: int = 0
    day_pnl: float = 0.0               # negative = realized loss so far today
    settled_cash: float = 0.0          # NOT pending deposits
    nearest_resistance: Optional[float] = None  # for executable R:R
    failed_breakouts: int = 0
    first_candle_range_pct: Optional[float] = None
    halt_prone: bool = False
    dilution_overhang: bool = False
    tape_risk_off: bool = False
    account_agentic: bool = True
    risk_pct: float = BASE_RISK_PCT
    quarantine: bool = False

    @classmethod
    def parse(cls, data) -> "MarketContext":
        d = json.loads(data) if isinstance(data, (str, bytes)) else dict(data)
        cat = d.pop("catalyst", None)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        ctx = cls(**known)
        if cat is not None:
            ctx.catalyst = Catalyst(
                **{k: v for k, v in cat.items()
                   if k in Catalyst.__dataclass_fields__}
            )
        return ctx


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    action: str                       # FULL_AUTO | MANUAL_CONFIRM | BLOCK
    ticker: str
    setup: str
    blocks: list = field(default_factory=list)
    manual: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "ticker": self.ticker,
            "setup": self.setup,
            "blocks": self.blocks,
            "manual": self.manual,
            "notes": self.notes,
            "metrics": self.metrics,
        }


def _round(x, n=4):
    return None if x is None else round(x, n)


def _spread_pct(bid: float, ask: float) -> Optional[float]:
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def evaluate(payload: Payload, ctx: MarketContext) -> Decision:
    blocks: list = []
    manual: list = []
    notes: list = []

    # ---- Entry / spread / slippage (use LIVE ask as marketable-limit entry) ---
    entry = ctx.ask if ctx.ask and ctx.ask > 0 else payload.price
    spread_abs = max((ctx.ask or 0) - (ctx.bid or 0), 0.0)
    spread_pct = _spread_pct(ctx.bid, ctx.ask)
    slip = max(entry * SLIP_PRICE_FRAC, 0.5 * spread_abs)

    risk_per_share = (entry - payload.stop) + slip
    reward_ref = ctx.nearest_resistance or payload.target
    reward_per_share = reward_ref - entry
    exec_rr = (reward_per_share / risk_per_share) if risk_per_share > 0 else 0.0

    modeled_rr = (
        (payload.target - payload.price) / (payload.price - payload.stop)
        if payload.price > payload.stop else 0.0
    )

    # ---- Sizing (independent of the scanner's numbers) ----------------------
    risk_dollars = ACCOUNT_EQUITY * ctx.risk_pct
    shares = (risk_dollars / risk_per_share) if risk_per_share > 0 else 0.0
    notional = shares * entry
    if notional > HARD_PER_TRADE_NOTIONAL:
        shares = HARD_PER_TRADE_NOTIONAL / entry if entry > 0 else 0.0
        notional = shares * entry
        notes.append(
            f"position clamped to ${HARD_PER_TRADE_NOTIONAL:.0f} per-trade cap "
            f"(risk-based size was larger)"
        )

    day_change_pct = (
        (ctx.last - ctx.prev_close) / ctx.prev_close * 100.0
        if ctx.prev_close else None
    )
    pct_of_cash = (notional / ctx.settled_cash) if ctx.settled_cash > 0 else None

    metrics = {
        "entry_used": _round(entry),
        "spread_pct": _round((spread_pct or 0) * 100, 3),
        "slippage_buffer": _round(slip),
        "risk_per_share": _round(risk_per_share),
        "modeled_rr": _round(modeled_rr, 2),
        "executable_rr": _round(exec_rr, 2),
        "reward_ref": _round(reward_ref),
        "risk_dollars": _round(risk_dollars, 2),
        "shares": _round(shares, 4),
        "notional": _round(notional, 2),
        "pct_of_cash": _round(pct_of_cash * 100, 1) if pct_of_cash is not None else None,
        "day_change_pct": _round(day_change_pct, 2),
        "float_shares": ctx.float_shares,
    }

    # ===================== ABSOLUTE VETOES (BLOCK) ========================== #
    if not ctx.account_agentic:
        blocks.append("account is not agentic-enabled (cannot route order)")

    if payload.setup not in APPROVED_SETUPS:
        blocks.append(f"setup '{payload.setup}' is outside approved playbook")

    cat = ctx.catalyst
    if cat is None or not cat.is_fresh_verified:
        blocks.append("no fresh, verified catalyst in the last 24h")

    if spread_pct is None:
        blocks.append("no valid bid/ask to compute spread")
    elif spread_pct > SPREAD_MANUAL_MAX:
        blocks.append(f"spread {spread_pct*100:.2f}% exceeds 2.00% hard veto")

    if risk_per_share <= 0:
        blocks.append("non-positive risk per share (entry/stop invalid)")
    elif exec_rr < MIN_EXEC_RR:
        blocks.append(
            f"slippage-adjusted R:R {exec_rr:.2f} below {MIN_EXEC_RR} floor"
        )

    if ctx.trades_today >= MAX_TRADES_PER_DAY:
        blocks.append(
            f"this would be trade #{ctx.trades_today + 1} "
            f"(max {MAX_TRADES_PER_DAY}/day)"
        )

    if ctx.day_pnl <= -DAILY_LOSS_LIMIT:
        blocks.append(
            f"daily loss ${-ctx.day_pnl:.2f} has hit "
            f"${DAILY_LOSS_LIMIT:.2f} limit (read-only)"
        )

    if ctx.dilution_overhang:
        blocks.append("dilution / shelf / toxic financing overhang")

    if ctx.halt_prone:
        # Sec 4 absolute veto; sec 10 would allow manual. Default to BLOCK.
        blocks.append("LULD halt-prone behavior flagged")

    if ctx.failed_breakouts > 1:
        blocks.append(
            f"{ctx.failed_breakouts} failed breakouts already (max 1)"
        )

    if ctx.first_candle_range_pct is not None:
        cap = 12.0 if entry < 5.0 else 8.0
        if ctx.first_candle_range_pct > cap:
            blocks.append(
                f"first-candle range {ctx.first_candle_range_pct:.1f}% "
                f"exceeds {cap:.0f}% veto"
            )

    if ctx.tape_risk_off and not (cat and cat.is_strong):
        blocks.append("risk-off tape with no independent catalyst strength")

    if ctx.settled_cash <= 0:
        blocks.append("no settled cash available (pending deposits don't count)")
    elif notional > ctx.settled_cash:
        blocks.append(
            f"notional ${notional:.2f} exceeds settled cash "
            f"${ctx.settled_cash:.2f}"
        )

    if notional > MAX_NOTIONAL_CEILING:
        blocks.append(
            f"notional ${notional:.2f} exceeds ceiling ${MAX_NOTIONAL_CEILING:.0f}"
        )

    # ===================== MANUAL-CONFIRM TRIGGERS ========================= #
    if ctx.risk_pct > BASE_RISK_PCT:
        manual.append(f"risk {ctx.risk_pct*100:.1f}% above base {BASE_RISK_PCT*100:.0f}%")

    if ctx.quarantine or (ctx.float_shares is not None and ctx.float_shares < FLOAT_QUARANTINE):
        manual.append("quarantined spec mode (true micro-cap float)")
    elif ctx.float_shares is None:
        manual.append("float unknown — cannot confirm >= 10M")
    elif ctx.float_shares < FLOAT_FULLAUTO_MIN:
        manual.append(f"float {ctx.float_shares:,} under 10M")

    if entry < PRICE_MANUAL_BELOW:
        manual.append(f"price ${entry:.2f} under ${PRICE_MANUAL_BELOW:.0f}")

    if spread_pct is not None and SPREAD_AUTO_MAX < spread_pct <= SPREAD_MANUAL_MAX:
        manual.append(f"spread {spread_pct*100:.2f}% in 1.25%-2.00% band")

    if cat is not None and cat.is_fresh_verified and not cat.is_strong:
        manual.append(f"catalyst tier '{cat.tier or '?'}' is not A/A-")

    if ctx.trades_today == 1:
        manual.append("second trade of the day")

    if payload.setup == "PMH_BREAK":
        manual.append("reclaimed premarket-high variant")

    if pct_of_cash is not None and pct_of_cash > CASH_MANUAL_FRACTION:
        manual.append(f"uses {pct_of_cash*100:.0f}% of cash (> 50%)")

    if ctx.tape_risk_off and cat and cat.is_strong:
        manual.append("risk-off tape (allowed only on strong catalyst)")

    # ===================== RESOLVE ========================================= #
    if blocks:
        action = "BLOCK"
    elif manual:
        action = "MANUAL_CONFIRM"
    else:
        action = "FULL_AUTO"

    return Decision(
        action=action,
        ticker=payload.ticker,
        setup=payload.setup,
        blocks=blocks,
        manual=manual,
        notes=notes,
        metrics=metrics,
    )


def decision_to_text(d: Decision) -> str:
    m = d.metrics
    lines = [
        f"[{d.action}] {d.ticker} {d.setup}",
        f"  entry {m['entry_used']}  spread {m['spread_pct']}%  "
        f"exec R:R {m['executable_rr']} (modeled {m['modeled_rr']})",
        f"  shares {m['shares']}  notional ${m['notional']}  "
        f"cash {m['pct_of_cash']}%  day {m['day_change_pct']}%",
    ]
    for b in d.blocks:
        lines.append(f"  BLOCK:  {b}")
    for x in d.manual:
        lines.append(f"  MANUAL: {x}")
    for n in d.notes:
        lines.append(f"  note:   {n}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI: reads {"payload": {...}, "context": {...}} from stdin or --input
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validator & Risk gate")
    ap.add_argument("--input", help="JSON file with payload+context; default stdin")
    ap.add_argument("--json", action="store_true", help="emit JSON not text")
    ap.add_argument(
        "--fetch-float", action="store_true",
        help="if context float_shares is null, source it live (Yahoo->SEC). "
             "This is the only networked step; evaluate() stays pure.",
    )
    args = ap.parse_args(argv)

    raw = open(args.input).read() if args.input else sys.stdin.read()
    obj = json.loads(raw)
    payload = Payload.parse(obj["payload"])
    ctx = MarketContext.parse(obj["context"])

    # Optional live enrichment at the agent/CLI boundary (NOT inside evaluate).
    # The SEC fallback returns shares-outstanding (an upper bound on float), so
    # when that proxy clears the 10M gate we still can't confirm the *true*
    # float is >= 10M and must force a human to confirm it.
    float_is_proxy = False
    if args.fetch_float and ctx.float_shares is None:
        from float_source import build_float
        fr = build_float(payload.ticker)
        ctx.float_shares = fr.float_shares
        float_is_proxy = (
            fr.float_shares is not None
            and not fr.is_true_float
            and fr.float_shares >= FLOAT_FULLAUTO_MIN
        )

    decision = evaluate(payload, ctx)

    if float_is_proxy:
        decision.manual.append(
            "float is SEC shares-outstanding proxy (true float unverified)"
        )
        if decision.action == "FULL_AUTO":
            decision.action = "MANUAL_CONFIRM"

    if args.json:
        print(json.dumps(decision.to_dict(), indent=2))
    else:
        print(decision_to_text(decision))
    # Exit code encodes the action for easy shell branching.
    return {"FULL_AUTO": 0, "MANUAL_CONFIRM": 10, "BLOCK": 20}[decision.action]


if __name__ == "__main__":
    raise SystemExit(main())
