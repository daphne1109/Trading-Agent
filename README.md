# Sentinel

**An autonomous trading agent with guardrails it can't override.** Sentinel streams live market data from Deriv's API, asks an AI model which way the price will move, and puts every answer through hard-coded risk rules before anything is executed. Risky calls go to a human. Every step is logged and shown on a live dashboard.

> **The goal is safe, observable, self-improving agent engineering, not profit.** Sentinel trades Deriv's synthetic volatility indices, which are random by design; no model can predict them, and this project makes no profitability claims.

**Status:** prototype, running locally. See [`PLAN.md`](PLAN.md) for the roadmap and [`CHANGELOG.md`](CHANGELOG.md) for progress.

## How a decision flows

```
Deriv live feed ─ticks─▶ MarketFeed ─snapshot─▶ Model (Jev / Claude) ─CALL/PUT/HOLD + probabilities─▶ RiskGate
  (public API)          stale-data guard                ▲                                          (11 rules, code)
                                                   playbook.md                         reject │ ask a human │ approve
                                                                                               ▼
 Postgres decision log ◀── settle on live ticks ◀── fill at the real Deriv quote ◀── Executor: re-quote + re-check
          │
          └──▶ dashboard · shadow baselines (coin flip, trend, always-wait) · nightly reflection (planned)
```

**The model proposes, code decides.** The model only chooses a direction and says how sure it is. Stake, limits, the kill switch and execution are deterministic code the model can't reach.

## Paper mode, and why
Deriv's trading API isn't offered to Malaysian residents, which is where this is built. Sentinel doesn't work around that. In **paper mode** (the default) it:
- streams **live ticks** from Deriv's public endpoint (no account needed);
- gets a **real Deriv price quote** (stake, payout, quote id) for every trade;
- runs **every safety check unchanged**;
- fills on paper at exactly the quoted price and settles on the **live ticks** using Deriv's Rise/Fall rules;
- marks a trade `unknown` instead of guessing if a reconnect or a gap in ticks makes the exit tick uncertain.

A full demo-account executor (real `buy` with virtual money, OTP auth) is built and tested behind `EXECUTION_MODE=demo`, for regions where the API is available.

## Safety model
- **Endpoint guard:** paper mode may only connect to Deriv's public endpoint, and demo mode only to the demo endpoint. Anything else is refused before connecting, and checked again before every order (`agent/deriv/guard.py`).
- **Fixed stake** from config; the quote must match it exactly.
- **RiskGate:** 11 pure-function rules. They cover a confidence floor, max open positions, a daily loss cap (counting stake already at risk), a cooldown after losses, stale data, kill and pause, and input validation that rejects NaN, wrong types and naive times. A rule that crashes counts as failed.
- **Human in the loop:** grey-zone confidence, a fallback model, or a bad day all need human approval, which is checked against the database and expires. Telegram approvals are next on the roadmap; until then those trades simply don't happen.
- **Fail closed everywhere:** an uncertain order is `unknown` and counts against the limits; unreadable control state means halted; only one agent instance can run (a Postgres advisory lock).
- **Spend caps** per AI provider per day, checked before each call.

## Run it locally (about 3 minutes)

Needs Docker Desktop.

```bash
docker compose up -d --build
```

Open **http://localhost:8080**. The agent needs about 2 minutes of price history, then decides every 30 seconds.

- **With no AI key**, a clearly labelled rule-based stand-in answers, so the full pipeline still runs.
- **To use the real AI model**, copy `.env.example` to `.env`, set `TYPESAFE_API_KEY` (Jev) and/or `ANTHROPIC_API_KEY`, and run the same command again.
- **To stop it:** `docker compose down`.

**Developing:**
```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
docker compose up -d postgres
pytest -q                                            # 280+ unit and integration tests
python scripts/run_with_fake_server.py --seconds 45  # whole agent against an offline fake Deriv
```

## Built with Claude Code
Built AI-first, with the AI held to the same bar as CI:
- [`CLAUDE.md`](CLAUDE.md) holds the rules every AI-assisted change must follow.
- A post-edit hook runs lint and the unit tests after each change.
- A [`risk-reviewer`](.claude/agents/risk-reviewer.md) subagent adversarially reviews every change to the safety code. It has caught real bypasses before they shipped, such as NaN inputs that read as "safe" and an uncertain order recorded as "failed".

Those incidents and what they taught are in [`BREAKLOG.md`](BREAKLOG.md).
