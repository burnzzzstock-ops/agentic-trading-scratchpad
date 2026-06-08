# Journal — Autonomy posture decision — 2026-06-08

Audit-trail record of an explicit operating decision (no trade this entry).

## Decision
The account owner explicitly chose to run the pipeline **fully autonomous**:
step 4 of `ROUTINE_PROMPT.md` is left as the auto-placing default, so a
FULL_AUTO decision executes unattended with no human checkpoint —
`review_equity_order` → `place_equity_order` → protective bracket
(stop_market @ stop + limit @ target, cancel-sibling on either fill) → journal.

The recommended human-checkpoint posture (review + summarize + stop on
FULL_AUTO until the live place/bracket path is proven) was offered and
**declined** in favor of full autonomy. No code change was made — autonomy is
the existing default; this entry records the deliberate choice to keep it.

## State at time of decision
- Agentic cash account ••••3540; settled cash **$0** (cash $250, pending
  deposits $250). Every payload still BLOCKs on settled cash until the deposit
  settles, so no autonomous order can fire today regardless.
- The FULL_AUTO place + OCO-style bracket path has **never executed a live
  order** — the first autonomous fire will be its first real exercise.

## Guardrails that bound autonomous execution (unchanged, deterministic)
- Per-trade notional ≤ $100; absolute ceiling $250.
- Daily loss limit $10 (4% of $250) → read-only block once hit.
- Max 2 trades/day; 3rd auto-blocks.
- FULL_AUTO requires: first trade of day, verified A/A- catalyst <24h (news
  capped at A- → manual; only an SEC 8-K grades A), float ≥10M, price ≥$2,
  spread ≤1.25%, exec R:R ≥2.25, settled cash, agentic account.
- Everything outside that set is MANUAL_CONFIRM or BLOCK by design.

## Owner
Confirmed by account owner (iansellscars24@gmail.com) on 2026-06-08.
